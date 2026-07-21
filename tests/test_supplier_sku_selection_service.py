from __future__ import annotations

from copy import deepcopy

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import WorkbenchAction, WorkbenchState
from ozon_v2.domain.supplier_sku import SupplierSkuSelectionReceipt
from ozon_v2.services.workbench_service import WorkbenchService

from tests.helpers import RuntimeTestCase


class SupplierSkuSelectionServiceTests(RuntimeTestCase):
    supplier_url = "https://detail.1688.com/offer/123456789012.html"

    def setUp(self) -> None:
        super().setUp()
        self.repo = FsRepo(self.context)
        self.service = WorkbenchService(self.repo)
        run = self.repo.create_workbench_batch_record(target_count=1)
        self.run_id = run["run_id"]
        run["status"] = WorkbenchState.SUPPLIER_COLLECTING.value
        self.repo.save_run(run)
        self.repo.save_supplier_collection_contract(
            self.run_id,
            {
                "run_id": self.run_id,
                "items": [{"seed_id": "seed-1", "supplier_url": self.supplier_url}],
            },
        )
        self.repo.save_ozon_collection_result(
            self.run_id,
            {
                "run_id": self.run_id,
                "ozon_candidates": [
                    {
                        "seed_id": "seed-1",
                        "ozon_product_id": "ozon-1",
                        "target_sku": {
                            "sku_id": "ozon-sku-1",
                            "selected_options": {"颜色": "粉色", "数量": "4"},
                        },
                    }
                ],
            },
        )

    def test_ingestion_rejects_missing_real_sku_matrix(self) -> None:
        payload = self.supplier_payload()
        payload["supplier_products"][0].pop("sku_options")

        result = self.service.ingest_supplier_collection_result(self.run_id, payload)

        self.assertFalse(result.ok)
        self.assertEqual("supplier_collection.invalid", result.code)
        self.assertTrue(any("sku_options" in error for error in result.errors))

    def test_ingestion_rejects_incomplete_real_sku_option(self) -> None:
        payload = self.supplier_payload()
        payload["supplier_products"][0]["sku_options"][0].update(
            {"complete": False, "evidence_source": "dom_option_labels"}
        )

        result = self.service.ingest_supplier_collection_result(self.run_id, payload)

        self.assertFalse(result.ok)
        self.assertTrue(any("complete supplier SKU evidence" in error for error in result.errors))

    def test_valid_matrix_waits_for_user_selection_then_persists_receipt(self) -> None:
        ingested = self.service.ingest_supplier_collection_result(self.run_id, self.supplier_payload())

        self.assertTrue(ingested.ok, ingested.errors)
        blocked = self.service.run_until_blocked(self.run_id)
        self.assertEqual("supplier_sku_selection_required", blocked.data["blocked_reason"])

        selected = self.service.confirm_supplier_sku(
            self.run_id,
            seed_id="seed-1",
            supplier_sku_id="sku-pink-4",
            differences=[],
        )

        self.assertTrue(selected.ok)
        stored = self.repo.load_supplier_sku_selections(self.run_id)
        receipt = SupplierSkuSelectionReceipt.from_dict(stored["selections"]["seed-1"])
        self.assertTrue(receipt.verify_hash())
        self.assertEqual(4, receipt.supplier_sku.set_quantity)
        self.assertTrue(selected.data["selection_status"]["complete"])

    def test_confirmed_supplier_sku_receipt_cannot_be_overwritten(self) -> None:
        self.assertTrue(
            self.service.ingest_supplier_collection_result(self.run_id, self.supplier_payload()).ok
        )
        first = self.service.confirm_supplier_sku(
            self.run_id,
            seed_id="seed-1",
            supplier_sku_id="sku-pink-4",
            differences=[],
        )
        original = self.repo.load_supplier_sku_selections(self.run_id)["selections"]["seed-1"]

        repeated = self.service.confirm_supplier_sku(
            self.run_id,
            seed_id="seed-1",
            supplier_sku_id="sku-pink-4",
            differences=[{"field": "quantity", "supplier": "1"}],
        )

        self.assertTrue(first.ok)
        self.assertFalse(repeated.ok)
        self.assertEqual("supplier_sku_selection.locked", repeated.code)
        stored = self.repo.load_supplier_sku_selections(self.run_id)["selections"]["seed-1"]
        self.assertEqual(original["selection_sha256"], stored["selection_sha256"])
        self.assertEqual(original, stored)

    def test_no_variant_page_recovers_one_confirmable_supplier_sku(self) -> None:
        product = self.supplier_payload()["supplier_products"][0]
        product["sku"] = {
            "selected_options": {"visible_sku_labels": ["单一 SKU（页面无可选规格）"]},
            "evidence": "no_visible_variant_selector",
            "evidence_source": "dom_option_labels",
            "complete": False,
        }
        product["sku_groups"] = []
        product["sku_options"] = []
        self.repo.save_supplier_collection_result(
            self.run_id,
            {
                "run_id": self.run_id,
                "network": {"mode": "direct", "proxy_disabled": True},
                "supplier_products": [product],
            },
        )
        run = self.repo.load_run(self.run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        selected = self.service.confirm_supplier_sku(
            self.run_id,
            seed_id="seed-1",
            supplier_sku_id="123456789012",
            differences=[],
        )

        self.assertTrue(selected.ok, selected.errors)
        self.assertEqual("single_sku_detail_page", selected.data["receipt"]["supplier_sku"]["evidence_source"])
        self.assertEqual("unknown", selected.data["receipt"]["supplier_sku"]["stock"]["status"])
        self.assertTrue(selected.data["selection_status"]["complete"])

    def test_visible_variant_groups_do_not_become_a_fake_single_sku(self) -> None:
        product = self.supplier_payload()["supplier_products"][0]
        product["sku"] = {
            "selected_options": {"visible_sku_labels": ["黑色", "白色"]},
            "evidence": "visible_selected_or_available_sku_labels",
            "evidence_source": "dom_option_labels",
            "complete": False,
        }
        product["sku_groups"] = [
            {"name": "颜色", "options": [{"label": "黑色"}, {"label": "白色"}]}
        ]
        product["sku_options"] = []
        self.repo.save_supplier_collection_result(
            self.run_id,
            {
                "run_id": self.run_id,
                "network": {"mode": "direct", "proxy_disabled": True},
                "supplier_products": [product],
            },
        )
        run = self.repo.load_run(self.run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        selected = self.service.confirm_supplier_sku(
            self.run_id,
            seed_id="seed-1",
            supplier_sku_id="123456789012",
            differences=[],
        )

        self.assertFalse(selected.ok)
        self.assertEqual("supplier_sku_selection.option_missing", selected.code)

    def test_tampered_selection_receipt_blocks_image_processing(self) -> None:
        ingested = self.service.ingest_supplier_collection_result(self.run_id, self.supplier_payload())
        self.assertTrue(ingested.ok, ingested.errors)
        self.assertTrue(
            self.service.confirm_supplier_sku(
                self.run_id,
                seed_id="seed-1",
                supplier_sku_id="sku-pink-4",
                differences=[],
            ).ok
        )
        stored = self.repo.load_supplier_sku_selections(self.run_id)
        tampered = deepcopy(stored)
        tampered["selections"]["seed-1"]["supplier_sku"]["set_quantity"] = 1
        self.repo.save_supplier_sku_selections(self.run_id, tampered)

        result = self.service.dispatch(self.run_id, WorkbenchAction.START_IMAGE_PROCESSING.value)

        self.assertFalse(result.ok)
        self.assertEqual("supplier_sku_selection.invalid", result.code)

    def supplier_payload(self) -> dict:
        return {
            "run_id": self.run_id,
            "network": {"mode": "direct", "proxy_disabled": True},
            "supplier_products": [
                {
                    "seed_id": "seed-1",
                    "supplier_product_id": "123456789012",
                    "offer_id": "123456789012",
                    "supplier_url": self.supplier_url,
                    "title": "四支修正笔套装",
                    "seller": {"shop_name": "测试供应商"},
                    "sku": {
                        "selected_options": {"visible_sku_labels": ["粉色", "4支套装"]},
                        "evidence_source": "dom_option_labels",
                        "complete": False,
                    },
                    "sku_options": [self.real_sku_option()],
                    "images": ["https://cbu01.alicdn.com/img/ibank/main.jpg"],
                    "price": {"currency": "CNY", "visible_text": "12.80"},
                    "attributes": {"材质": "塑料"},
                    "domestic_shipping_evidence": {"visible_text": "包邮", "fee": 0},
                }
            ],
        }

    def real_sku_option(self) -> dict:
        return {
            "supplier_sku_id": "sku-pink-4",
            "combination_key": "粉色>4支套装",
            "raw_label": "粉色 / 4支套装",
            "selected_options": {"颜色": "粉色", "数量": "4支套装"},
            "set_quantity": 4,
            "set_composition": ["4支套装"],
            "price": {"currency": "CNY", "amount": "12.80"},
            "stock": {"status": "in_stock", "quantity": 88},
            "image_urls": ["https://cbu01.alicdn.com/img/ibank/sku-pink.jpg"],
            "evidence_source": "embedded_sku_map",
            "complete": True,
            "evidence": {"sku_map_key": "粉色>4支套装"},
        }
