from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PIL import Image

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import SeedProduct, WorkbenchState
from ozon_v2.services.workbench_service import WorkbenchService

from tests.helpers import RuntimeTestCase


class ImageWorkflowServiceTests(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = FsRepo(self.context)
        self.seed = SeedProduct(
            seed_id="seed-set",
            title_or_keyword="correction pen set",
            product_clue="correction pen set",
            ozon_query_terms_ru=["nabor korrektorov"],
            query_generation_status="generated",
        )
        self.run = self.repo.create_workbench_batch_record(target_count=1)
        self.run_id = self.run["run_id"]
        self.repo.save_sampled_seeds(self.run_id, [self.seed])
        self.repo.save_ozon_collection_result(
            self.run_id,
            {
                "run_id": self.run_id,
                "ozon_candidates": [
                    {
                        "seed_id": self.seed.seed_id,
                        "ozon_product_id": "ozon-product-1",
                        "title": "Correction pen set",
                        "target_sku": {
                            "sku_id": "ozon-sku-1",
                            "selected_options": {"quantity": "4"},
                        },
                        "selected_sku_media": {
                            "selected_sku_images": ["https://img.example/ozon.jpg"]
                        },
                    }
                ],
            },
        )
        self.subject_url = "https://cbu01.alicdn.com/img/ibank/set-x4.jpg"
        self.gallery_url = "https://cbu01.alicdn.com/img/ibank/set-x4-detail.jpg"
        self.repo.save_supplier_collection_result(
            self.run_id,
            {
                "run_id": self.run_id,
                "network": {"proxy_disabled": True},
                "supplier_products": [
                    {
                        "seed_id": self.seed.seed_id,
                        "supplier_url": "https://detail.1688.com/offer/123.html",
                        "offer_id": "123",
                        "title": "Correction pen set",
                        "seller": {"shop_name": "Supplier"},
                        "sku": {"visible": "set x4"},
                        "sku_options": [
                            {
                                "supplier_sku_id": "supplier-sku-set-x4",
                                "combination_key": "set-x4",
                                "raw_label": "set x4",
                                "selected_options": {"combination": "set x4"},
                                "set_quantity": 4,
                                "set_composition": ["set x4"],
                                "price": {"currency": "CNY", "amount": "18.80"},
                                "stock": {"status": "in_stock", "quantity": 100},
                                "image_urls": [self.subject_url],
                                "evidence_source": "trusted_sku_map",
                                "complete": True,
                                "evidence": {"source": "trusted_sku_map"},
                            }
                        ],
                        "images": [self.subject_url, self.gallery_url],
                        "price": "18.80",
                        "domestic_shipping_evidence": {"fee": "0"},
                    }
                ],
            },
        )
        self.run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(self.run)

        def download(_url: str, target: Path) -> Path:
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (200, 200), "white").save(target, format="PNG")
            return target

        self.service = WorkbenchService(
            self.repo,
            supplier_image_downloader=download,
        )

    def test_last_subject_master_confirmation_enters_image_stage_and_enqueues_job(self) -> None:
        selected = self.service.confirm_supplier_sku(
            self.run_id,
            seed_id=self.seed.seed_id,
            supplier_sku_id="supplier-sku-set-x4",
            differences=[{"field": "quantity", "ozon": "4", "supplier": "4"}],
        )
        self.assertTrue(selected.ok)

        confirmed = self.service.confirm_subject_master(
            self.run_id,
            seed_id=self.seed.seed_id,
            source_image_urls=[self.subject_url, self.gallery_url],
            visible_subject_quantity=4,
        )

        self.assertTrue(confirmed.ok, confirmed.errors)
        self.assertEqual(WorkbenchState.IMAGE_PROCESSING.value, self.repo.load_run(self.run_id)["status"])
        self.assertEqual("pending", confirmed.data["image_job"]["status"])
        self.assertEqual(8, len(confirmed.data["image_job"]["slots"]))
        self.assertEqual(4, confirmed.data["subject_master"]["set_quantity"])
        self.assertEqual(
            [self.subject_url, self.gallery_url],
            confirmed.data["subject_master"]["source_image_urls"],
        )
        self.assertEqual(
            ["set x4"],
            confirmed.data["subject_master"]["set_composition"],
        )
        self.assertTrue(
            confirmed.data["subject_master"]["white_background_generation_required"]
        )
        self.assertFalse(confirmed.data["subject_master"]["white_background_confirmed"])

        workspace = self.service.image_workspace(self.run_id)
        self.assertTrue(workspace.ok)
        item = workspace.data["items"][0]
        self.assertEqual("supplier-sku-set-x4", item["supplier_sku_selection"]["supplier_sku_id"])
        self.assertEqual(4, item["subject_master"]["visible_subject_quantity"])
        self.assertEqual("pending", item["image_job"]["status"])
        self.assertEqual("waiting_for_codex_workers", workspace.data["image_gate"]["code"])

    def test_subject_master_rejects_quantity_that_does_not_match_real_supplier_set(self) -> None:
        self.assertTrue(
            self.service.confirm_supplier_sku(
                self.run_id,
                seed_id=self.seed.seed_id,
                supplier_sku_id="supplier-sku-set-x4",
            ).ok
        )

        result = self.service.confirm_subject_master(
            self.run_id,
            seed_id=self.seed.seed_id,
            source_image_urls=[self.subject_url],
            visible_subject_quantity=1,
        )

        self.assertFalse(result.ok)
        self.assertEqual("subject_master.quantity_mismatch", result.code)
        self.assertEqual(WorkbenchState.SUPPLIER_COLLECTED.value, self.repo.load_run(self.run_id)["status"])

    def test_subject_evidence_rejects_image_outside_verified_supplier_product(self) -> None:
        self.assertTrue(
            self.service.confirm_supplier_sku(
                self.run_id,
                seed_id=self.seed.seed_id,
                supplier_sku_id="supplier-sku-set-x4",
            ).ok
        )

        result = self.service.confirm_subject_master(
            self.run_id,
            seed_id=self.seed.seed_id,
            source_image_urls=[
                self.subject_url,
                "https://images.example/foreign-product.jpg",
            ],
            visible_subject_quantity=4,
        )

        self.assertFalse(result.ok)
        self.assertEqual("subject_master.image_not_in_supplier", result.code)
        self.assertEqual(
            WorkbenchState.SUPPLIER_COLLECTED.value,
            self.repo.load_run(self.run_id)["status"],
        )

    def test_confirmed_subject_master_cannot_be_overwritten(self) -> None:
        self.assertTrue(
            self.service.confirm_supplier_sku(
                self.run_id,
                seed_id=self.seed.seed_id,
                supplier_sku_id="supplier-sku-set-x4",
            ).ok
        )
        first = self.service.confirm_subject_master(
            self.run_id,
            seed_id=self.seed.seed_id,
            source_image_urls=[self.subject_url],
            visible_subject_quantity=4,
        )
        original = self.repo.load_subject_masters(self.run_id)["items"][self.seed.seed_id]

        repeated = self.service.confirm_subject_master(
            self.run_id,
            seed_id=self.seed.seed_id,
            source_image_urls=[self.gallery_url],
            visible_subject_quantity=4,
        )

        self.assertTrue(first.ok)
        self.assertFalse(repeated.ok)
        self.assertEqual("subject_master.locked", repeated.code)
        stored = self.repo.load_subject_masters(self.run_id)["items"][self.seed.seed_id]
        self.assertEqual(original, stored)

    def test_image_job_can_stop_and_resume_without_changing_locked_evidence(self) -> None:
        self.assertTrue(
            self.service.confirm_supplier_sku(
                self.run_id,
                seed_id=self.seed.seed_id,
                supplier_sku_id="supplier-sku-set-x4",
            ).ok
        )
        confirmed = self.service.confirm_subject_master(
            self.run_id,
            seed_id=self.seed.seed_id,
            source_image_urls=[self.subject_url, self.gallery_url],
            visible_subject_quantity=4,
        )
        job_id = confirmed.data["image_job"]["job_id"]
        selection_sha256 = confirmed.data["image_job"]["selection_sha256"]

        stopped = self.service.stop_image_job(self.run_id, job_id)
        resumed = self.service.resume_image_job(self.run_id, job_id)

        self.assertTrue(stopped.ok)
        self.assertEqual("stopped", stopped.data["image_job"]["status"])
        self.assertTrue(resumed.ok)
        self.assertEqual("pending", resumed.data["image_job"]["status"])
        self.assertEqual(selection_sha256, resumed.data["image_job"]["selection_sha256"])

    def test_pending_supplier_sku_can_be_reopened_with_audit_history(self) -> None:
        supplier_result = self.repo.load_supplier_collection_result(self.run_id)
        alternate = deepcopy(supplier_result["supplier_products"][0]["sku_options"][0])
        alternate.update(
            {
                "supplier_sku_id": "supplier-sku-single",
                "combination_key": "single",
                "raw_label": "single item",
                "selected_options": {"combination": "single item"},
                "set_quantity": 1,
                "set_composition": ["single item"],
            }
        )
        supplier_result["supplier_products"][0]["sku_options"].append(alternate)
        self.repo.save_supplier_collection_result(self.run_id, supplier_result)
        self.assertTrue(
            self.service.confirm_supplier_sku(
                self.run_id,
                seed_id=self.seed.seed_id,
                supplier_sku_id="supplier-sku-set-x4",
            ).ok
        )
        confirmed = self.service.confirm_subject_master(
            self.run_id,
            seed_id=self.seed.seed_id,
            source_image_urls=[self.subject_url],
            visible_subject_quantity=4,
        )
        job_id = confirmed.data["image_job"]["job_id"]
        self.assertEqual(WorkbenchState.IMAGE_PROCESSING.value, self.repo.load_run(self.run_id)["status"])

        reopened = self.service.reopen_supplier_sku_selection(
            self.run_id,
            seed_id=self.seed.seed_id,
        )

        self.assertTrue(reopened.ok, reopened.errors)
        self.assertEqual("stopped", self.service._image_generation_queue().get_job(job_id)["status"])
        selections = self.repo.load_supplier_sku_selections(self.run_id)
        subjects = self.repo.load_subject_masters(self.run_id)
        self.assertNotIn(self.seed.seed_id, selections["selections"])
        self.assertNotIn(self.seed.seed_id, subjects["items"])
        self.assertEqual("supplier-sku-set-x4", selections["history"][-1]["receipt"]["supplier_sku_id"])
        self.assertEqual(job_id, subjects["history"][-1]["subject_entry"]["image_job_id"])
        reselection = self.service.confirm_supplier_sku(
            self.run_id,
            seed_id=self.seed.seed_id,
            supplier_sku_id="supplier-sku-single",
        )
        self.assertTrue(reselection.ok, reselection.errors)
        self.assertEqual("supplier-sku-single", reselection.data["receipt"]["supplier_sku_id"])

    def test_started_image_job_blocks_supplier_sku_reopen(self) -> None:
        self.assertTrue(
            self.service.confirm_supplier_sku(
                self.run_id,
                seed_id=self.seed.seed_id,
                supplier_sku_id="supplier-sku-set-x4",
            ).ok
        )
        confirmed = self.service.confirm_subject_master(
            self.run_id,
            seed_id=self.seed.seed_id,
            source_image_urls=[self.subject_url],
            visible_subject_quantity=4,
        )
        job_id = confirmed.data["image_job"]["job_id"]
        queue = self.service._image_generation_queue()
        claimed = queue.claim_next("ozon-image-worker-01")
        self.assertEqual(job_id, claimed["job_id"])

        reopened = self.service.reopen_supplier_sku_selection(
            self.run_id,
            seed_id=self.seed.seed_id,
        )

        self.assertFalse(reopened.ok)
        self.assertEqual("supplier_sku_selection.reopen_blocked", reopened.code)
        self.assertIn(self.seed.seed_id, self.repo.load_supplier_sku_selections(self.run_id)["selections"])
        self.assertIn(self.seed.seed_id, self.repo.load_subject_masters(self.run_id)["items"])
        self.assertEqual("in_progress", queue.get_job(job_id)["status"])
