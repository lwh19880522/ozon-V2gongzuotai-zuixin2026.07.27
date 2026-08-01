from __future__ import annotations

import importlib.util
from copy import deepcopy
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


def task(
    *,
    evidence_insufficient: bool = False,
    objective_value: str = "1",
) -> dict[str, Any]:
    original = product("11", "sale-11")
    seller_api_item = deepcopy(original["seller_api_item"])
    for runtime_key in (
        "product_id",
        "sku",
        "visibility",
        "archived",
        "stocks",
        "content_score_groups",
    ):
        seller_api_item.pop(runtime_key, None)
    return {
        "product_id": "11",
        "sku": "sku-11",
        "offer_id": "sale-11",
        "source_fingerprint": "base-fingerprint",
        "visibility": "IN_SALE",
        "seller_api_item": seller_api_item,
        "current": {
            "name": original["name"],
            "description": original["description"],
            "rich_content": original["rich_content"],
            "attributes": original["attributes"],
        },
        "attribute_schema": [
            {"id": 6318, "name": "Количество", "objective": True},
            {
                "id": 100,
                "name": "Тип монтажа",
                "dictionary": True,
                "dictionary_values": [{"id": 10, "value": "Настенный"}],
            },
        ],
        "objective_evidence": {
            "6318": {
                "value": objective_value,
                "refs": ["seller.attributes.6318"],
            },
            "quantity": {
                "value": objective_value,
                "refs": ["seller.attributes.6318"],
            },
        },
        "read_only_images": original["images"],
        "product_url": "https://www.ozon.ru/product/11/",
        "storefront_facts": {},
        "pricing_evidence": {},
        "evidence_insufficient": evidence_insufficient,
        "non_media_score": 80,
    }


def valid_proposal(
    *,
    attribute_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "base_fingerprint": "base-fingerprint",
        "product_id": "11",
        "name": "Настенный светильник LED 5 Вт с питанием от USB",
        "description": (
            "Компактный настенный светильник создаёт мягкое освещение. "
            "Подходит для установки рядом с рабочим столом или кроватью."
        ),
        "rich_content": {
            "content": [
                {
                    "widgetName": "raTextBlock",
                    "text": "Три режима света и плавная регулировка яркости.",
                }
            ]
        },
        "storefront_observations": [],
        "attribute_decisions": attribute_decisions
        if attribute_decisions is not None
        else [
            {
                "id": 6318,
                "decision": "keep",
                "values": [{"value": "1"}],
                "evidence_refs": ["seller.attributes.6318"],
            }
        ],
        "risk_findings": [
            {
                "code": "quantity_consistent",
                "level": "low",
                "resolution": "verified",
                "evidence_refs": ["seller.attributes.6318"],
            }
        ],
    }


@pytest.mark.parametrize(
    "bad_text",
    [
        "Настенный светильник 墙灯",
        "Товар 1688 от поставщика",
        "Опт, дропшиппинг и доставка от фабрики",
        "лампа лампа лампа лампа LED LED LED",
        "РЎРІРµС‚РёР»СЊРЅРёРє",
    ],
)
def test_customer_text_gate_rejects_forbidden_or_broken_text(
    optimizer, bad_text: str
) -> None:
    proposal = valid_proposal()
    proposal["name"] = bad_text
    with pytest.raises(optimizer.ValidationError):
        optimizer.validate_proposal(task(), proposal)


def test_objective_attribute_requires_exact_evidence(optimizer) -> None:
    proposal = valid_proposal(
        attribute_decisions=[
            {
                "id": 6318,
                "decision": "set",
                "values": [{"value": "29"}],
                "evidence_refs": ["seller.attributes.6318"],
            }
        ]
    )
    with pytest.raises(optimizer.ValidationError, match="objective evidence mismatch"):
        optimizer.validate_proposal(task(objective_value="1"), proposal)


def test_missing_workbench_evidence_keeps_objective_fields_and_blocks_stock(
    optimizer,
) -> None:
    proposal = valid_proposal(attribute_decisions=[])
    result = optimizer.validate_proposal(
        task(evidence_insufficient=True), proposal
    )
    assert result["evidence_insufficient"] is True
    assert result["allow_inventory_restore"] is False


def test_merge_preserves_every_media_value_exactly(optimizer) -> None:
    original = task()["seller_api_item"]
    merged = optimizer.merge_proposal(task(), valid_proposal())
    for key in optimizer.MEDIA_KEYS:
        assert merged.get(key) == original.get(key)


def test_rich_content_must_be_valid_non_media_json(optimizer) -> None:
    proposal = valid_proposal()
    optimizer.validate_proposal(task(), proposal)
    proposal["rich_content"] = {
        "content": [
            {"widgetName": "video", "url": "https://example.test/v.mp4"}
        ]
    }
    with pytest.raises(optimizer.ValidationError, match="media"):
        optimizer.validate_proposal(task(), proposal)


def test_dictionary_attribute_rejects_unknown_value_id(optimizer) -> None:
    proposal = valid_proposal(
        attribute_decisions=[
            {
                "id": 100,
                "decision": "set",
                "values": [{"dictionary_value_id": 99, "value": "Потолочный"}],
                "evidence_refs": ["seller.attributes.100"],
            }
        ]
    )
    with pytest.raises(optimizer.ValidationError, match="dictionary"):
        optimizer.validate_proposal(task(), proposal)


def test_unproven_compliance_claim_is_rejected(optimizer) -> None:
    proposal = valid_proposal()
    proposal["description"] += " Сертифицированная безопасность гарантирована."
    with pytest.raises(optimizer.ValidationError, match="compliance"):
        optimizer.validate_proposal(task(), proposal)


def test_stale_fingerprint_is_rejected_before_merge(optimizer) -> None:
    proposal = valid_proposal()
    proposal["base_fingerprint"] = "stale"
    with pytest.raises(optimizer.ValidationError, match="stale"):
        optimizer.validate_proposal(task(), proposal)


def test_storefront_quantity_divergence_blocks_inventory_restore(optimizer) -> None:
    proposal = valid_proposal()
    proposal["storefront_observations"] = [
        {
            "field_key": "quantity",
            "value": "29",
            "source_url": "https://www.ozon.ru/product/11/",
        }
    ]
    result = optimizer.validate_proposal(task(), proposal)
    assert "storefront_divergence:quantity" in result["risk_codes"]
    assert result["risk_level"] == "high"
    assert result["allow_inventory_restore"] is False


def test_known_price_loss_is_classified_as_severe(optimizer) -> None:
    current_task = task()
    current_task["seller_api_item"]["price"] = "50.00"
    current_task["pricing_evidence"] = {"minimum_safe_price": "100.00"}
    result = optimizer.validate_proposal(current_task, valid_proposal())
    assert "price_loss" in result["risk_codes"]
    assert result["risk_level"] == "severe"


class FakeActionGateway:
    def __init__(
        self,
        current: dict[str, Any] | None = None,
        *,
        active_orders: set[str] | None = None,
        warehouses: list[dict[str, Any]] | None = None,
        readback_is_bad: bool = False,
        rollback_fails: bool = False,
        import_status: str = "imported",
    ) -> None:
        self.current = deepcopy(current or product("11", "sale-11"))
        self.active_orders = active_orders or set()
        self.warehouses = warehouses if warehouses is not None else [
            {
                "warehouse_id": 501,
                "status": "created",
                "is_rfbs": True,
                "warehouse_type": "rfbs",
            }
        ]
        self.readback_is_bad = readback_is_bad
        self.rollback_fails = rollback_fails
        self.import_status_value = import_status
        self.import_requests: list[list[dict[str, Any]]] = []
        self.stock_requests: list[dict[str, Any]] = []
        self.archive_requests: list[dict[str, Any]] = []

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        assert str(self.current["product_id"]) == str(product_id)
        return deepcopy(self.current)

    def import_product(self, item: dict[str, Any]) -> int:
        self.import_requests.append([deepcopy(item)])
        if self.rollback_fails and len(self.import_requests) >= 2:
            raise RuntimeError("rollback failed")
        self.current.update(deepcopy(item))
        self.current["seller_api_item"] = deepcopy(item)
        if self.readback_is_bad and len(self.import_requests) == 1:
            self.current["name"] = "Неверное название"
        return len(self.import_requests)

    def import_status(self, task_id: int) -> dict[str, Any]:
        return {"items": [{"status": self.import_status_value}]}

    def list_rfbs_warehouses(self) -> list[dict[str, Any]]:
        return deepcopy(self.warehouses)

    def active_order_offer_ids(self) -> set[str]:
        return set(self.active_orders)

    def set_stock(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        self.stock_requests.extend(deepcopy(rows))
        stock = int(rows[0]["stock"])
        self.current["stocks"] = [{"present": stock, "reserved": 0}]
        return {"result": [{"updated": True}]}


def seeded_ledger(optimizer, tmp_path: Path, current_task: dict[str, Any]):
    ledger = optimizer.Ledger(tmp_path / "optimizer.sqlite3")
    ledger.upsert_task(current_task, rule_hashes())
    return ledger


def test_in_sale_product_is_optimized_without_stock_overwrite(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    gateway = FakeActionGateway(product("11", "sale-11", visibility="IN_SALE", stock=7))
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "completed"
    assert gateway.stock_requests == []


def test_safe_ready_to_supply_product_gets_stock_ten_after_verified_import(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    current_task["visibility"] = "READY_TO_SUPPLY"
    gateway = FakeActionGateway(
        product("11", "sale-11", visibility="READY_TO_SUPPLY", stock=0)
    )
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "completed"
    assert gateway.stock_requests == [
        {
            "offer_id": "sale-11",
            "product_id": 11,
            "stock": 10,
            "warehouse_id": 501,
        }
    ]


def test_archived_product_is_never_imported_or_restocked(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    gateway = FakeActionGateway(
        product("11", "sale-11", visibility="ARCHIVED", stock=0)
    )
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "skipped_archived"
    assert gateway.import_requests == []
    assert gateway.stock_requests == []


def test_unresolved_severe_risk_sets_stock_zero_without_archiving(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    proposal = valid_proposal()
    proposal["risk_findings"] = [
        {
            "code": "quantity_conflict",
            "level": "severe",
            "resolution": "unresolved",
            "evidence_refs": ["seller.attributes.6318"],
        }
    ]
    gateway = FakeActionGateway(product("11", "sale-11", stock=7))
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        proposal,
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "paused"
    assert gateway.stock_requests[0]["stock"] == 0
    assert gateway.archive_requests == []
    assert gateway.import_requests == []


def test_order_race_blocks_every_stock_mutation(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    current_task["visibility"] = "READY_TO_SUPPLY"
    gateway = FakeActionGateway(
        product("11", "sale-11", visibility="READY_TO_SUPPLY", stock=0),
        active_orders={"sale-11"},
    )
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "pending_risk"
    assert gateway.stock_requests == []


def test_failed_readback_rolls_back_original_payload(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    original = deepcopy(current_task["seller_api_item"])
    gateway = FakeActionGateway(readback_is_bad=True)
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "rolled_back"
    assert gateway.import_requests[-1] == [original]


def test_rollback_failure_pauses_with_stock_zero(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    gateway = FakeActionGateway(readback_is_bad=True, rollback_fails=True)
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "paused"
    assert gateway.stock_requests[-1]["stock"] == 0


def test_ambiguous_rfbs_warehouse_blocks_inventory_mutation(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    current_task["visibility"] = "READY_TO_SUPPLY"
    gateway = FakeActionGateway(
        product("11", "sale-11", visibility="READY_TO_SUPPLY", stock=0),
        warehouses=[],
    )
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "pending_risk"
    assert gateway.stock_requests == []


def test_evidence_insufficient_product_is_never_restocked(
    optimizer, tmp_path: Path
) -> None:
    current_task = task(evidence_insufficient=True)
    current_task["visibility"] = "READY_TO_SUPPLY"
    gateway = FakeActionGateway(
        product("11", "sale-11", visibility="READY_TO_SUPPLY", stock=0)
    )
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(attribute_decisions=[]),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "evidence_insufficient"
    assert gateway.stock_requests == []


def test_duplicate_successful_import_is_not_submitted_again_or_left_applying(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    gateway = FakeActionGateway(product("11", "sale-11", visibility="IN_SALE"))
    ledger = seeded_ledger(optimizer, tmp_path, current_task)
    first = optimizer.apply_one(
        gateway,
        ledger,
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    second = optimizer.apply_one(
        gateway,
        ledger,
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )
    assert first["status"] == "completed"
    assert second["status"] == "duplicate_skipped"
    assert len(gateway.import_requests) == 1
    assert ledger.get_state("11")["status"] == "completed"


def test_execute_command_drives_scan_next_and_status_without_live_api(
    optimizer, tmp_path: Path
) -> None:
    runtime = optimizer.Runtime(
        ledger=optimizer.Ledger(tmp_path / "optimizer.sqlite3"),
        gateway=FakeGateway([product("11", "sale-11")]),
        evidence=FakeEvidence(),
        rule_hashes=rule_hashes(),
    )
    scan_result = optimizer.execute_command("scan", runtime)
    assert scan_result["status"] == "scanned"
    assert scan_result["counts"]["queued"] == 1
    next_result = optimizer.execute_command("next", runtime)
    assert next_result["status"] == "task"
    assert next_result["task"]["product_id"] == "11"
    status_result = optimizer.execute_command("status", runtime)
    assert status_result["counts"] == {"drafting": 1}
