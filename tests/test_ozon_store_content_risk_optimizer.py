from __future__ import annotations

import importlib.util
from copy import deepcopy
import json
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
        "barcode": "",
        "complex_attributes": [],
        "currency_code": "CNY",
        "depth": 280,
        "dimension_unit": "mm",
        "height": 50,
        "price": "599.00",
        "old_price": "699.00",
        "pdf_list": [],
        "vat": "0",
        "weight": 250,
        "weight_unit": "g",
        "width": 60,
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
        self.attribute_update_requests: list[dict[str, Any]] = []
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
            current_attributes = {
                int(attribute["id"]): deepcopy(attribute)
                for attribute in self.current.get("attributes") or []
            }
            current_attributes[4180] = {
                "id": 4180,
                "complex_id": 0,
                "values": [{"dictionary_value_id": 0, "value": "Неверное название"}],
            }
            self.current["attributes"] = list(current_attributes.values())
        return len(self.import_requests)

    def update_product_attributes(self, item: dict[str, Any]) -> int:
        self.attribute_update_requests.append(deepcopy(item))
        if self.rollback_fails and len(self.attribute_update_requests) >= 2:
            raise RuntimeError("rollback failed")
        current_attributes = {
            int(attribute["id"]): deepcopy(attribute)
            for attribute in self.current.get("attributes") or []
        }
        for attribute in item.get("attributes") or []:
            current_attributes[int(attribute["id"])] = deepcopy(attribute)
        self.current["attributes"] = list(current_attributes.values())
        self.current["seller_api_item"] = deepcopy(self.current)
        self.current["name"] = str(
            current_attributes.get(4180, {}).get("values", [{}])[0].get("value")
            or self.current.get("name")
            or ""
        )
        self.current["description"] = str(
            current_attributes.get(4191, {}).get("values", [{}])[0].get("value")
            or self.current.get("description")
            or ""
        )
        rich_text = str(
            current_attributes.get(11254, {}).get("values", [{}])[0].get("value")
            or ""
        )
        if rich_text:
            self.current["rich_content"] = json.loads(rich_text)
        if self.readback_is_bad and len(self.attribute_update_requests) == 1:
            self.current["name"] = "Неверное название"
            current_attributes[4180] = {
                "id": 4180,
                "complex_id": 0,
                "values": [{"dictionary_value_id": 0, "value": "Неверное название"}],
            }
            self.current["attributes"] = list(current_attributes.values())
        return len(self.attribute_update_requests)

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
    original = optimizer.build_original_content_import(current_task)
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


def test_successful_evidence_insufficient_update_is_not_requeued_unchanged(
    optimizer, tmp_path: Path
) -> None:
    current_task = task(evidence_insufficient=True)
    gateway = FakeActionGateway(product("11", "sale-11", visibility="IN_SALE"))
    ledger = seeded_ledger(optimizer, tmp_path, current_task)

    result = optimizer.apply_one(
        gateway,
        ledger,
        current_task,
        valid_proposal(attribute_decisions=[]),
        sleep_fn=lambda _seconds: None,
    )
    scan = optimizer.scan_store(
        FakeGateway([gateway.current]),
        ledger,
        FakeEvidence(insufficient=True),
        rule_hashes(),
    )

    assert result["status"] == "evidence_insufficient"
    assert scan["queued"] == 0
    assert scan["skipped_unchanged"] == 1


def test_evidence_insufficient_unchanged_product_needs_no_media_or_seller_write(
    optimizer, tmp_path: Path
) -> None:
    current_task = task(evidence_insufficient=True)
    current_task["read_only_images"] = []
    current_task["seller_api_item"]["images"] = []
    current_task["seller_api_item"]["primary_image"] = ""
    proposal = valid_proposal(attribute_decisions=[])
    proposal["name"] = current_task["current"]["name"]
    proposal["description"] = current_task["current"]["description"]
    proposal["rich_content"] = current_task["current"]["rich_content"]
    live_product = product("11", "sale-11", visibility="IN_SALE")
    live_product["images"] = []
    live_product["primary_image"] = ""
    live_product["seller_api_item"] = deepcopy(live_product)
    gateway = FakeActionGateway(live_product)

    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        proposal,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "evidence_insufficient"
    assert gateway.import_requests == []
    assert gateway.stock_requests == []


def test_missing_media_uses_attribute_update_when_title_is_unchanged(
    optimizer, tmp_path: Path
) -> None:
    current_task = task(evidence_insufficient=True)
    current_task["read_only_images"] = []
    current_task["seller_api_item"]["images"] = []
    current_task["seller_api_item"]["primary_image"] = ""
    proposal = valid_proposal(attribute_decisions=[])
    proposal["name"] = current_task["current"]["name"]
    live_product = product("11", "sale-11", visibility="IN_SALE")
    live_product["images"] = []
    live_product["primary_image"] = ""
    live_product["seller_api_item"] = deepcopy(live_product)
    gateway = FakeActionGateway(live_product)

    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        proposal,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "evidence_insufficient"
    assert gateway.import_requests == []
    assert len(gateway.attribute_update_requests) == 1


def test_existing_media_still_uses_attribute_update_when_title_is_unchanged(
    optimizer, tmp_path: Path
) -> None:
    current_task = task(evidence_insufficient=True)
    proposal = valid_proposal(attribute_decisions=[])
    proposal["name"] = current_task["current"]["name"]
    gateway = FakeActionGateway(product("11", "sale-11", visibility="IN_SALE"))

    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        proposal,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "evidence_insufficient"
    assert gateway.import_requests == []
    assert len(gateway.attribute_update_requests) == 1


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


def test_fetch_attributes_accepts_the_live_v4_list_result_shape(optimizer) -> None:
    class Adapter:
        def _post_json(self, path, payload):
            assert path == "/v4/product/info/attributes"
            return {
                "result": [
                    {
                        "id": 11,
                        "offer_id": "sale-11",
                        "attributes": [
                            {
                                "id": 4180,
                                "complex_id": 0,
                                "values": [{"value": "Исходное название"}],
                            }
                        ],
                    }
                ]
            }

    result = optimizer.SellerGateway(Adapter())._fetch_attributes([11])
    assert result["11"]["offer_id"] == "sale-11"


def test_fetch_product_uses_focused_readback_instead_of_full_catalog(optimizer) -> None:
    class Adapter:
        def _fetch_product_info(self, product_ids):
            assert product_ids == [11]
            return {
                "11": {
                    "id": 11,
                    "offer_id": "sale-11",
                    "price": "599.00",
                    "stocks": {"has_stock": True, "stocks": [{"present": 7}]},
                    "statuses": {"status_name": "Продается"},
                }
            }

        def _post_json(self, path, payload):
            assert path == "/v4/product/info/attributes"
            return {
                "result": [
                    {
                        "id": 11,
                        "sku": 70011,
                        "offer_id": "sale-11",
                        "name": "Точное название",
                        "attributes": [
                            {
                                "id": 4180,
                                "complex_id": 0,
                                "values": [{"value": "Точное название"}],
                            }
                        ],
                        "images": ["https://example.test/image.jpg"],
                        "primary_image": "https://example.test/image.jpg",
                    }
                ]
            }

        def _fetch_product_refs(self):
            raise AssertionError("full catalog must not be fetched for one read-back")

    result = optimizer.SellerGateway(Adapter()).fetch_product("11")

    assert result["product_id"] == "11"
    assert result["name"] == "Точное название"
    assert result["visibility"] == "IN_SALE"


def test_build_task_reads_title_description_and_rich_content_attributes(
    optimizer,
) -> None:
    rich_content = {
        "version": 0.3,
        "content": [
            {
                "widgetName": "raTextBlock",
                "blocks": [{"text": "Полезное описание товара."}],
            }
        ],
    }
    current_product = product("11", "sale-11")
    current_product["name"] = "Резервное название"
    current_product.pop("description", None)
    current_product.pop("rich_content", None)
    current_product["attributes"] = [
        {"id": 4180, "complex_id": 0, "values": [{"value": "Точное название"}]},
        {"id": 4191, "complex_id": 0, "values": [{"value": "Точное описание"}]},
        {
            "id": 11254,
            "complex_id": 0,
            "values": [{"value": optimizer.canonical_json(rich_content)}],
        },
    ]
    current_product["seller_api_item"] = deepcopy(current_product)

    built = optimizer._build_task(current_product, {"evidence_insufficient": True})

    assert built["current"]["name"] == "Точное название"
    assert built["current"]["description"] == "Точное описание"
    assert built["current"]["rich_content"] == rich_content


def test_content_import_uses_exact_allowlist_and_preserves_media(optimizer) -> None:
    proposal = valid_proposal(
        attribute_decisions=[
            {
                "id": 100,
                "decision": "set",
                "values": [{"dictionary_value_id": 10, "value": "Настенный"}],
                "evidence_refs": ["seller.attributes.100"],
            }
        ]
    )
    import_item = optimizer.build_content_import(task(), proposal)

    assert set(import_item) == {
        "attributes",
        "barcode",
        "complex_attributes",
        "currency_code",
        "depth",
        "description_category_id",
        "dimension_unit",
        "height",
        "images",
        "name",
        "offer_id",
        "old_price",
        "pdf_list",
        "premium_price",
        "price",
        "primary_image",
        "type_id",
        "vat",
        "weight",
        "weight_unit",
        "width",
    }
    assert import_item["offer_id"] == "sale-11"
    assert import_item["images"] == task()["seller_api_item"]["images"]
    assert import_item["primary_image"] == task()["seller_api_item"]["primary_image"]
    by_id = {int(item["id"]): item for item in import_item["attributes"]}
    assert set(by_id) == {100, 4180, 4191, 6318, 11254}
    assert by_id[4180]["values"] == [{"dictionary_value_id": 0, "value": proposal["name"]}]
    assert by_id[4191]["values"] == [
        {"dictionary_value_id": 0, "value": proposal["description"]}
    ]
    assert optimizer.json.loads(by_id[11254]["values"][0]["value"]) == proposal[
        "rich_content"
    ]


def test_rejected_attribute_update_with_unchanged_readback_never_zeros_stock(
    optimizer, tmp_path: Path
) -> None:
    class RejectingGateway(FakeActionGateway):
        import_called = False
        attribute_update_called = False

        def update_product_attributes(self, item: dict[str, Any]) -> int:
            self.attribute_update_called = True
            raise AssertionError("attribute-only update must not be used")

        def import_product(self, item: dict[str, Any]) -> int:
            self.import_called = True
            raise RuntimeError("request rejected before mutation")

    current_task = task()
    gateway = RejectingGateway(product("11", "sale-11", stock=7))
    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "pending_risk"
    assert result["reason"] == "write_rejected_no_change"
    assert gateway.import_called is True
    assert gateway.attribute_update_called is False
    assert gateway.stock_requests == []


@pytest.mark.parametrize(
    ("status_name", "expected"),
    [
        ("Продается", "IN_SALE"),
        ("Готов к продаже", "READY_TO_SUPPLY"),
        ("Не продается", "NOT_IN_SALE"),
    ],
)
def test_visibility_normalization_does_not_treat_every_sale_word_as_in_sale(
    optimizer, status_name: str, expected: str
) -> None:
    assert optimizer._normalize_visibility(
        {}, {"statuses": {"status_name": status_name}}
    ) == expected


def test_build_task_exposes_seller_errors_required_gaps_and_raw_status(
    optimizer,
) -> None:
    current_product = product("11", "sale-11", visibility="NOT_IN_SALE")
    current_product["statuses"] = {
        "status_name": "Не продается",
        "status_description": "Ошибка валидации",
        "validation_status": "failed",
        "is_created": False,
    }
    current_product["errors"] = [
        {
            "code": "INCORRECT_DENSITY",
            "level": "ERROR_LEVEL_ERROR",
            "attribute_id": 8229,
            "texts": {"message": "Неверная плотность"},
        }
    ]
    current_product["attribute_schema"] = [
        {"id": 8229, "name": "Тип", "required": True, "objective": True}
    ]
    current_product["seller_api_item"] = deepcopy(current_product)

    built = optimizer._build_task(current_product, {"evidence_insufficient": True})

    assert built["seller_status"]["status_name"] == "Не продается"
    assert built["seller_errors"][0]["code"] == "INCORRECT_DENSITY"
    assert built["required_missing_attribute_ids"] == [8229]
    assert "seller_error:INCORRECT_DENSITY" in built["deterministic_risk_codes"]
    assert "missing_required_attribute:8229" in built["deterministic_risk_codes"]


@pytest.mark.parametrize(
    "bad_title",
    [
        "Многоразовая пеленка 70 90 см",
        "Белый шарик для стирки 6 4 см, 17 г",
        "Настольная игра для 2 8 игроков",
        "Термометр от 50 до +400 C",
    ],
)
def test_numeric_separator_risks_are_rejected_before_any_write(
    optimizer, bad_title: str
) -> None:
    proposal = valid_proposal()
    proposal["name"] = bad_title

    with pytest.raises(optimizer.ValidationError, match="numeric"):
        optimizer.validate_proposal(task(), proposal)


def test_customer_text_cannot_invent_a_new_numeric_product_fact(optimizer) -> None:
    proposal = valid_proposal()
    proposal["name"] = "Настенный светильник LED, комплект 29 шт."

    with pytest.raises(optimizer.ValidationError, match="unsupported numeric"):
        optimizer.validate_proposal(task(), proposal)


def test_unresolved_seller_error_blocks_content_write_and_completion(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    current_task["visibility"] = "NOT_IN_SALE"
    current_task["seller_status"] = {
        "status_name": "Не продается",
        "validation_status": "failed",
        "is_created": False,
    }
    current_task["seller_errors"] = [
        {
            "code": "INCORRECT_DENSITY",
            "level": "ERROR_LEVEL_ERROR",
            "attribute_id": 8229,
            "message": "Неверная плотность",
        }
    ]
    current_task["deterministic_risk_codes"] = [
        "seller_error:INCORRECT_DENSITY"
    ]
    gateway = FakeActionGateway(
        product("11", "sale-11", visibility="NOT_IN_SALE", stock=0)
    )

    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "paused"
    assert gateway.import_requests == []
    assert gateway.stock_requests[0]["stock"] == 0


def test_changed_product_without_official_non_media_score_is_not_completed_or_restocked(
    optimizer, tmp_path: Path
) -> None:
    current_task = task()
    current_task["visibility"] = "READY_TO_SUPPLY"
    current_task["non_media_score"] = None
    live_product = product(
        "11", "sale-11", visibility="READY_TO_SUPPLY", stock=0
    )
    live_product["content_score_groups"] = {}
    gateway = FakeActionGateway(live_product)

    result = optimizer.apply_one(
        gateway,
        seeded_ledger(optimizer, tmp_path, current_task),
        current_task,
        valid_proposal(),
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "score_unverified"
    assert gateway.stock_requests == []


def test_seller_error_change_requeues_a_previously_terminal_product(
    optimizer, tmp_path: Path
) -> None:
    current_product = product("11", "sale-11")
    gateway = FakeGateway([current_product])
    ledger = optimizer.Ledger(tmp_path / "optimizer.sqlite3")
    optimizer.scan_store(gateway, ledger, FakeEvidence(), rule_hashes())
    queued = ledger.next_task()
    assert queued is not None
    ledger.mark_status("11", "pending_risk")

    current_product["errors"] = [
        {"code": "INCORRECT_DENSITY", "level": "ERROR_LEVEL_ERROR"}
    ]
    current_product["seller_api_item"] = deepcopy(current_product)
    result = optimizer.scan_store(gateway, ledger, FakeEvidence(), rule_hashes())

    assert result["queued"] == 1
    assert result["skipped_unchanged"] == 0


def test_catalog_audit_reports_risks_and_score_evidence_without_mutation(
    optimizer,
) -> None:
    clean = product("11", "sale-11")
    broken = product("12", "sale-12", visibility="NOT_IN_SALE", stock=0)
    broken["content_score_groups"] = {}
    broken["statuses"] = {"status_name": "Не продается"}
    broken["errors"] = [
        {"code": "INCORRECT_DENSITY", "level": "ERROR_LEVEL_ERROR"}
    ]
    broken["attribute_schema"] = [
        {"id": 8229, "name": "Тип", "required": True, "objective": True}
    ]
    broken["seller_api_item"] = deepcopy(broken)

    result = optimizer.audit_catalog([clean, broken])

    assert result["catalog_count"] == 2
    assert result["problem_product_count"] == 1
    assert result["score_evidence"]["available"] == 1
    assert result["score_evidence"]["unavailable"] == 1
    issue = result["problem_products"][0]
    assert issue["product_id"] == "12"
    assert "seller_error:INCORRECT_DENSITY" in issue["risk_codes"]
    assert "missing_required_attribute:8229" in issue["risk_codes"]


def test_dictionary_values_are_resolved_exactly_through_seller_api_before_apply(
    optimizer,
) -> None:
    current_task = task()
    current_task["attribute_schema"] = [
        {
            "id": 100,
            "name": "Тип монтажа",
            "dictionary": True,
            "dictionary_values": [],
            "objective": False,
        }
    ]
    proposal = valid_proposal(
        attribute_decisions=[
            {
                "id": 100,
                "decision": "set",
                "values": [{"dictionary_value_id": 999999, "value": "Настенный"}],
                "evidence_refs": ["seller.name"],
            }
        ]
    )

    class ExactDictionaryGateway:
        def resolve_dictionary_value(self, task, attribute_id, value):
            assert attribute_id == 100
            assert value == "Настенный"
            return {"dictionary_value_id": 10, "value": "Настенный"}

    resolved = optimizer.resolve_proposal_dictionary_values(
        ExactDictionaryGateway(), current_task, proposal
    )

    assert resolved["attribute_decisions"][0]["values"] == [
        {"dictionary_value_id": 10, "value": "Настенный"}
    ]
