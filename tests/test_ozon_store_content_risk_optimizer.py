from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "ozon-store-content-risk-optimizer"
    / "scripts"
    / "store_content_optimizer.py"
)


@pytest.fixture(scope="module")
def optimizer():
    spec = importlib.util.spec_from_file_location("store_content_optimizer", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def product(
    product_id: str,
    offer_id: str,
    *,
    visibility: str = "IN_SALE",
    stock: int = 7,
) -> dict[str, Any]:
    item = {
        "product_id": product_id,
        "sku": f"sku-{product_id}",
        "offer_id": offer_id,
        "visibility": visibility,
        "name": "Настенный светильник LED 5 Вт",
        "description": "Компактный светильник с питанием от USB.",
        "rich_content": {"content": []},
        "attributes": [
            {"id": 6318, "complex_id": 0, "values": [{"value": "1"}]}
        ],
        "images": ["https://example.test/image.jpg"],
        "primary_image": "https://example.test/image.jpg",
        "price": "599.00",
        "stocks": [{"present": stock, "reserved": 0}],
        "description_category_id": 123,
        "type_id": 456,
        "content_score_groups": {
            "text": {"earned": 35, "maximum": 40},
            "attributes": {"earned": 45, "maximum": 60},
            "media": {"earned": 0, "maximum": 30},
        },
    }
    item["seller_api_item"] = dict(item)
    return item


class FakeGateway:
    def __init__(self, products: list[dict[str, Any]]) -> None:
        self.products = products

    def fetch_catalog(self) -> list[dict[str, Any]]:
        return self.products


class FakeEvidence:
    def __init__(self, *, insufficient: bool = False) -> None:
        self.insufficient = insufficient

    def for_offer(self, offer_id: str) -> dict[str, Any]:
        return {
            "offer_id": offer_id,
            "run_id": "wb-test",
            "locked_supplier_sku": not self.insufficient,
            "evidence_insufficient": self.insufficient,
            "objective_evidence": {"6318": {"value": "1", "refs": ["seller.attributes.6318"]}},
            "pricing_evidence": {},
        }


def rule_hashes(value: str = "a") -> dict[str, str]:
    return {"language": value, "attributes": "a", "risk": "a"}


def test_store_key_never_contains_the_client_id(optimizer) -> None:
    key = optimizer.store_key("123456789")
    assert key == optimizer.hashlib.sha256(b"123456789").hexdigest()[:16]
    assert "123456789" not in key


def test_non_media_score_renormalizes_only_available_text_groups(optimizer) -> None:
    groups = {
        "text": {"earned": 35, "maximum": 40},
        "attributes": {"earned": 45, "maximum": 60},
        "media": {"earned": 0, "maximum": 30},
    }
    assert optimizer.non_media_score(groups) == 80
    assert optimizer.non_media_score({"media": {"earned": 30, "maximum": 30}}) is None


def test_scan_skips_archived_and_does_not_requeue_unchanged_completed(
    optimizer, tmp_path: Path
) -> None:
    gateway = FakeGateway(
        [
            product("11", "sale-11", visibility="IN_SALE"),
            product("12", "archived-12", visibility="ARCHIVED"),
        ]
    )
    ledger = optimizer.Ledger(tmp_path / "optimizer.sqlite3")
    first = optimizer.scan_store(gateway, ledger, FakeEvidence(), rule_hashes())
    assert first == {
        "queued": 1,
        "skipped_archived": 1,
        "skipped_unchanged": 0,
    }
    task = ledger.next_task()
    assert task is not None
    ledger.mark_completed("11", task["source_fingerprint"], "proposal-a")
    second = optimizer.scan_store(gateway, ledger, FakeEvidence(), rule_hashes())
    assert second == {
        "queued": 0,
        "skipped_archived": 1,
        "skipped_unchanged": 1,
    }


def test_changed_content_and_affected_rule_domain_requeue_product(
    optimizer, tmp_path: Path
) -> None:
    gateway = FakeGateway([product("11", "sale-11")])
    ledger = optimizer.Ledger(tmp_path / "optimizer.sqlite3")
    optimizer.scan_store(gateway, ledger, FakeEvidence(), rule_hashes("a"))
    task = ledger.next_task()
    assert task is not None
    ledger.mark_completed("11", task["source_fingerprint"], "proposal-a")

    gateway.products[0]["name"] = "Изменённое название"
    assert optimizer.scan_store(
        gateway, ledger, FakeEvidence(), rule_hashes("a")
    )["queued"] == 1
    task = ledger.next_task()
    assert task is not None
    ledger.mark_completed("11", task["source_fingerprint"], "proposal-b")

    assert optimizer.scan_store(
        gateway, ledger, FakeEvidence(), rule_hashes("b")
    )["queued"] == 1


def test_unchanged_evidence_insufficient_product_is_not_regenerated(
    optimizer, tmp_path: Path
) -> None:
    gateway = FakeGateway([product("11", "sale-11")])
    ledger = optimizer.Ledger(tmp_path / "optimizer.sqlite3")
    optimizer.scan_store(
        gateway, ledger, FakeEvidence(insufficient=True), rule_hashes()
    )
    task = ledger.next_task()
    assert task is not None
    ledger.mark_status("11", "evidence_insufficient")

    result = optimizer.scan_store(
        gateway, ledger, FakeEvidence(insufficient=True), rule_hashes()
    )
    assert result["queued"] == 0
    assert result["skipped_unchanged"] == 1


def test_workbench_evidence_ignores_non_workbench_runs_and_matches_exact_offer(
    optimizer,
) -> None:
    class FakeRepo:
        def list_runs(self):
            return [
                {"run_id": "other-newer", "kind": "other"},
                {"run_id": "wb-correct", "kind": "workbench_batch"},
            ]

        def load_upload_submissions(self, run_id):
            if run_id == "other-newer":
                return {"items": [{"offer_id": "sale-11", "source": "wrong"}]}
            return {
                "items": [
                    {"offer_id": "sale-110", "source": "fuzzy-wrong"},
                    {"offer_id": "sale-11", "source": "exact"},
                ]
            }

        def load_upload_previews(self, run_id):
            return {}

        def load_supplier_sku_selections(self, run_id):
            return {
                "items": [
                    {
                        "offer_id": "sale-11",
                        "locked": True,
                        "supplier_sku": "locked-1",
                    }
                ]
            }

        def load_required_attribute_evidence(self, run_id):
            return {"6318": {"value": "1"}}

        def load_pricing_evidence(self, run_id):
            return {}

    result = optimizer.WorkbenchEvidence(FakeRepo()).for_offer("sale-11")
    assert result["run_id"] == "wb-correct"
    assert result["upload_submission"]["source"] == "exact"
    assert result["locked_supplier_sku"] is True


def test_ledger_schema_is_limited_to_product_state_and_action_log(
    optimizer, tmp_path: Path
) -> None:
    ledger = optimizer.Ledger(tmp_path / "optimizer.sqlite3")
    names = {
        row[0]
        for row in ledger.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if not str(row[0]).startswith("sqlite_")
    }
    assert names == {"product_state", "action_log"}
