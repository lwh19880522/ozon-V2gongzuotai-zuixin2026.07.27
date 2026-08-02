from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.seller_api import SellerApiError
from ozon_v2.app.result import Result
from ozon_v2.domain.models import RunStatus, SeedProduct
from ozon_v2.domain.models import WorkbenchAction, WorkbenchState
from ozon_v2.domain.supplier_sku import SupplierSkuOption, SupplierSkuSelectionReceipt
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.queue import ImageGenerationQueue
from ozon_v2.images.task_inbox import ImageTaskInbox
from ozon_v2.services.collection_contract_service import CollectionContractService
from ozon_v2.services.attribute_mapping_service import map_template_attributes
from ozon_v2.services.workbench_service import (
    WorkbenchService,
    _dictionary_upload_value,
    _normalize_ozon_rich_content_value,
    _pricing_upload_core_fields,
    _product_import_status,
    _seller_api_attribute,
    _seller_api_import_item,
    _valid_ozon_hashtags,
    _video_template_fields,
)
from ozon_v2.workbench.local_server import build_upload_workspace_html, create_handler

from tests.helpers import FakeSellerApiAdapter, RuntimeTestCase


class FakeBusyRunner:
    def __init__(self, service: WorkbenchService) -> None:
        self.service = service

    def status(self, run_id: str) -> dict:
        return {"run_id": run_id, "running": True, "state": "running"}


class FakeRuntimeController:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def schedule(
        self,
        action: str,
        server: ThreadingHTTPServer,
        *,
        open_edge_after_restart: bool = False,
    ) -> dict:
        self.actions.append((action, open_edge_after_restart))
        return {
            "ok": True,
            "code": f"runtime.{action}_scheduled",
            "message": f"Runtime {action} scheduled.",
            "data": {"action": action},
            "errors": [],
        }


def test_video_template_fields_classify_video_and_cover_inputs() -> None:
    fields = _video_template_fields(
        [
            {
                "attribute_id": "10",
                "attribute_label": "Озон.Видеообложка: ссылка",
            },
            {
                "attribute_id": "11",
                "attribute_label": "Озон.Видео: ссылка",
            },
            {
                "attribute_id": "12",
                "attribute_label": "Озон.Видео: название",
            },
            {
                "attribute_id": "13",
                "attribute_label": "Озон.Видеообложка: изображение-превью",
            },
        ]
    )

    assert {field["attribute_id"]: field["kind"] for field in fields} == {
        "10": "video_cover_url",
        "11": "video_url",
        "12": "video_title",
        "13": "video_cover_image_url",
    }


class WorkbenchLocalServerTests(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = FsRepo(self.context)
        self.repo.save_browser_bridge_status(
            {
                "source": "background_interval",
                "extension_version": self.required_extension_version(),
                "stage": "idle",
                "code": "browser_task.none",
            }
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.repo))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def prepare_reviewable_image_job(self) -> tuple[str, str, ImageGenerationQueue]:
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        run["status"] = WorkbenchState.IMAGE_PROCESSING.value
        self.repo.save_run(run)
        sku = SupplierSkuOption(
            supplier_sku_id="supplier-review-sku",
            combination_key="white>single",
            raw_label="white / single",
            selected_options={"color": "white", "quantity": "1"},
            set_quantity=1,
            set_composition=["single unit"],
            price={"currency": "CNY", "amount": "19.90"},
            stock={"status": "in_stock", "quantity": 10},
            image_urls=["https://cbu01.alicdn.com/img/ibank/review-sku.jpg"],
            evidence_source="embedded_sku_map",
            complete=True,
        )
        receipt = SupplierSkuSelectionReceipt.confirmed(
            run_id=run_id,
            product_id="review-product",
            supplier_offer_id="review-offer",
            supplier_sku=sku,
            ozon_target_sku={"sku_id": "ozon-review-sku", "selected_options": {}},
            differences=[],
            confirmed_at="2026-07-19T00:00:00+00:00",
        )
        subject_path = self.tmpdir / "review-subject.bin"
        subject_path.write_bytes(b"locked-review-subject")
        subject = SubjectMasterSelection.create(
            receipt=receipt,
            source_path=subject_path,
            source_image_url=sku.image_urls[0],
            visible_subject_quantity=1,
            white_background_confirmed=False,
            confirmed_at="2026-07-19T00:01:00+00:00",
        )
        queue = ImageGenerationQueue(
            self.context.runtime_root / "image_generation" / "ozon_image_jobs.sqlite3"
        )
        job = queue.enqueue(receipt=receipt, subject_master=subject)
        with queue._connect() as connection:
            for slot in queue.list_slots(job["job_id"]):
                output = self.tmpdir / f"{slot['slot_id']}.png"
                output.write_bytes(f"accepted-{slot['slot_id']}".encode("utf-8"))
                connection.execute(
                    """
                    UPDATE image_slots
                    SET status = 'accepted', accepted_path = ?, receipt_json = ?
                    WHERE job_id = ? AND slot_id = ?
                    """,
                    (
                        str(output.resolve()),
                        json.dumps({"slot_id": slot["slot_id"]}),
                        job["job_id"],
                        slot["slot_id"],
                    ),
                )
            connection.execute(
                """
                UPDATE image_jobs
                SET status = 'manual_review_required', worker_id = NULL,
                    lease_expires = NULL, heartbeat_at = NULL
                WHERE job_id = ?
                """,
                (job["job_id"],),
            )
        return run_id, job["job_id"], queue

    def attach_completed_image_job(self, run_id: str, seed_id: str) -> dict:
        sku = SupplierSkuOption(
            supplier_sku_id=f"approved-{seed_id}",
            combination_key=f"approved>{seed_id}",
            raw_label=f"approved {seed_id}",
            selected_options={"颜色": "Черный", "数量": "1"},
            set_quantity=1,
            set_composition=["Черный", "1 шт."],
            price={"currency": "CNY", "amount": "12.80"},
            stock={"status": "in_stock", "quantity": 10},
            image_urls=[f"https://cbu01.alicdn.com/img/ibank/{seed_id}.jpg"],
            evidence_source="trusted_sku_map",
            complete=True,
        )
        receipt = SupplierSkuSelectionReceipt.confirmed(
            run_id=run_id,
            product_id=seed_id,
            supplier_offer_id=f"offer-{seed_id}",
            supplier_sku=sku,
            ozon_target_sku={"sku_id": f"ozon-{seed_id}", "selected_options": {}},
            differences=[],
            confirmed_at="2026-07-22T00:00:00+00:00",
        )
        subject_path = self.tmpdir / f"{seed_id}-subject.bin"
        subject_path.write_bytes(f"subject-{seed_id}".encode("utf-8"))
        subject = SubjectMasterSelection.create(
            receipt=receipt,
            source_path=subject_path,
            source_image_url=sku.image_urls[0],
            visible_subject_quantity=1,
            white_background_confirmed=False,
            confirmed_at="2026-07-22T00:01:00+00:00",
        )
        queue = ImageGenerationQueue(
            self.context.runtime_root / "image_generation" / "ozon_image_jobs.sqlite3"
        )
        job = queue.enqueue(receipt=receipt, subject_master=subject)
        with queue._connect() as connection:
            for slot in queue.list_slots(job["job_id"]):
                output = self.tmpdir / f"{seed_id}-{slot['slot_id']}.png"
                Image.new("RGB", (600, 800), (34, 58, 78)).save(output)
                connection.execute(
                    "UPDATE image_slots SET status = 'accepted', accepted_path = ? "
                    "WHERE job_id = ? AND slot_id = ?",
                    (str(output.resolve()), job["job_id"], slot["slot_id"]),
                )
            connection.execute(
                "UPDATE image_jobs SET status = 'completed' WHERE job_id = ?",
                (job["job_id"],),
            )
        subject_items: dict = {}
        subject_path_json = self.repo.run_dir(run_id) / "subject_masters.json"
        if subject_path_json.exists():
            subject_items = dict(self.repo.load_subject_masters(run_id).get("items") or {})
        subject_items[seed_id] = {
            "subject_master": subject.to_dict(),
            "image_job_id": job["job_id"],
        }
        self.repo.save_subject_masters(run_id, {"run_id": run_id, "items": subject_items})
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        selections = (
            self.repo.load_supplier_sku_selections(run_id)
            if selection_path.exists()
            else {"run_id": run_id, "selections": {}}
        )
        selections.setdefault("selections", {})[seed_id] = receipt.to_dict()
        self.repo.save_supplier_sku_selections(run_id, selections)
        return job

    def attach_locked_subject_master(self, run_id: str, seed_id: str) -> dict:
        sku = SupplierSkuOption(
            supplier_sku_id=f"locked-{seed_id}",
            combination_key=f"locked>{seed_id}",
            raw_label=f"locked {seed_id}",
            selected_options={"颜色": "Черный", "数量": "1"},
            set_quantity=1,
            set_composition=["Черный", "1 шт."],
            price={"currency": "CNY", "amount": "12.80"},
            stock={"status": "in_stock", "quantity": 10},
            image_urls=[f"https://cbu01.alicdn.com/img/ibank/{seed_id}.jpg"],
            evidence_source="trusted_sku_map",
            complete=True,
        )
        receipt = SupplierSkuSelectionReceipt.confirmed(
            run_id=run_id,
            product_id=seed_id,
            supplier_offer_id=f"offer-{seed_id}",
            supplier_sku=sku,
            ozon_target_sku={"sku_id": f"ozon-{seed_id}", "selected_options": {}},
            differences=[],
            confirmed_at="2026-07-25T00:00:00+00:00",
        )
        subject_path = self.tmpdir / f"{seed_id}-locked-subject.bin"
        subject_path.write_bytes(f"locked-subject-{seed_id}".encode("utf-8"))
        subject = SubjectMasterSelection.create(
            receipt=receipt,
            source_path=subject_path,
            source_image_url=sku.image_urls[0],
            visible_subject_quantity=1,
            white_background_confirmed=False,
            confirmed_at="2026-07-25T00:01:00+00:00",
        )
        subject_items: dict = {}
        subject_path_json = self.repo.run_dir(run_id) / "subject_masters.json"
        if subject_path_json.exists():
            subject_items = dict(
                self.repo.load_subject_masters(run_id).get("items") or {}
            )
        subject_items[seed_id] = {
            "subject_master": subject.to_dict(),
            "image_task_mode": "post_upload_package",
        }
        self.repo.save_subject_masters(
            run_id,
            {"run_id": run_id, "items": subject_items},
        )
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        selections = (
            self.repo.load_supplier_sku_selections(run_id)
            if selection_path.exists()
            else {"run_id": run_id, "selections": {}}
        )
        selections.setdefault("selections", {})[seed_id] = receipt.to_dict()
        self.repo.save_supplier_sku_selections(run_id, selections)
        return subject.to_dict()

    def prepare_pricing_sources(self, run_id: str, seed_id: str) -> None:
        supplier_product = self.supplier_product_payload(seed_id)
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "supplier_products": [supplier_product],
            },
        )
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        selections = (
            self.repo.load_supplier_sku_selections(run_id)
            if selection_path.exists()
            else {"run_id": run_id, "selections": {}}
        )
        selections.setdefault("selections", {}).setdefault(
            seed_id,
            {
                "supplier_offer_id": supplier_product["offer_id"],
                "supplier_sku": supplier_product["sku_options"][0],
                "ozon_target_sku": {
                    "sku_id": "ozon-sku-1",
                    "selected_options": {"single_sku": "visible"},
                },
            },
        )
        self.repo.save_supplier_sku_selections(run_id, selections)

    def pricing_input_payload(self, seed_id: str) -> dict:
        return {
            "seed_id": seed_id,
            "purchase_price_cny": "9.9",
            "domestic_shipping_cny": "7",
            "package_weight_g": "380",
            "package_length_cm": "28",
            "package_width_cm": "11",
            "package_height_cm": "2.5",
            "target_margin_rate": "0.20",
        }

    def test_user_can_exclude_cross_border_unsuitable_product_without_refill(self) -> None:
        run_id, heavy_seed = self.prepare_supplier_review_run()
        retained_seed = SeedProduct(
            seed_id="seed-retained",
            title_or_keyword="light cross-border product",
            product_clue="light cross-border product",
            ozon_query_terms_ru=["легкий товар"],
            query_generation_status="generated",
        )
        self.repo.save_sampled_seeds(run_id, [heavy_seed, retained_seed])
        self.repo.save_active_seeds([heavy_seed, retained_seed])

        template_payload = self.repo.load_attribute_template_result(run_id)
        retained_template = json.loads(
            json.dumps(template_payload["seed_templates"][0])
        )
        retained_template["seed_id"] = retained_seed.seed_id
        template_payload["seed_templates"].append(retained_template)
        self.repo.save_attribute_template_result(run_id, template_payload)

        ozon_payload = self.repo.load_ozon_collection_result(run_id)
        ozon_payload["ozon_candidates"] = [
            self.ozon_candidate_for_seed(heavy_seed, "ozon-heavy"),
            self.ozon_candidate_for_seed(retained_seed, "ozon-retained"),
        ]
        self.repo.save_ozon_collection_result(run_id, ozon_payload)
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.IMAGE_PROCESSING.value
        run["sampled_seed_ids"] = [heavy_seed.seed_id, retained_seed.seed_id]
        self.repo.save_run(run)

        excluded = self.post_json(
            f"/api/batches/{run_id}/products/{heavy_seed.seed_id}/exclude",
            {
                "confirmed": True,
                "reason": "包装重量超出跨境可接受范围",
            },
        )

        self.assertEqual("product_exclusion.completed", excluded["code"])
        self.assertEqual(1, excluded["data"]["remaining_product_count"])
        self.assertIsNone(excluded["data"]["replacement_seed_id"])
        self.assertEqual(
            [retained_seed.seed_id],
            [seed.seed_id for seed in self.repo.load_sampled_seeds(run_id)],
        )
        self.assertEqual(
            [retained_seed.seed_id],
            [seed.seed_id for seed in self.repo.load_active_seeds()],
        )
        self.assertIn(heavy_seed.seed_id, self.repo.load_blacklisted_seed_ids())
        self.assertIn("ozon-heavy", self.repo.load_blacklisted_ozon_product_ids())
        self.assertNotIn(
            "replacement_pending_seed_ids",
            self.repo.load_run(run_id),
        )
        self.assertEqual(
            [retained_seed.seed_id],
            [
                item["seed_id"]
                for item in self.get_json(f"/api/batches/{run_id}/upload")[
                    "data"
                ]["items"]
            ],
        )
        blacklist_rows = [
            json.loads(line)
            for line in self.repo.seed_blacklist_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(
            "cross_border_unsuitable_by_user",
            blacklist_rows[-1]["reason_code"],
        )

    def test_upload_pricing_editor_exposes_permanent_exclusion_without_refill(self) -> None:
        html = build_upload_workspace_html("wb-exclusion-button")

        self.assertIn("剔除商品（不补位）", html)
        self.assertIn("永久加入黑名单", html)
        self.assertIn("/products/${encodeURIComponent(item.seed_id)}/exclude", html)

    def test_pricing_evidence_round_trips_without_touching_other_batch_files(
        self,
    ) -> None:
        run = self.repo.create_workbench_batch_record(target_count=1)
        payload = {
            "schema_version": 1,
            "run_id": run["run_id"],
            "items": {"seed-1": {"status": "confirmed"}},
        }

        saved_path = self.repo.save_pricing_evidence(run["run_id"], payload)

        self.assertEqual(saved_path.name, "pricing_evidence.json")
        self.assertEqual(
            self.repo.load_pricing_evidence(run["run_id"]),
            payload,
        )
        self.assertTrue((self.repo.run_dir(run["run_id"]) / "run.json").exists())

    def test_image_task_package_repository_uses_fixed_pending_directory(self) -> None:
        payload = {
            "schema_version": 1,
            "package_id": "img-task:test/seed",
            "status": "pending",
        }

        first = self.repo.save_image_task_package(payload["package_id"], payload)
        second = self.repo.save_image_task_package(payload["package_id"], payload)

        self.assertEqual(first, second)
        self.assertEqual(
            self.context.runtime_root / "image_tasks" / "pending",
            first.parent,
        )
        self.assertEqual(payload, json.loads(first.read_text(encoding="utf-8")))

    def test_pricing_settings_use_fixed_store_defaults_and_round_trip(self) -> None:
        defaults = self.repo.load_pricing_settings()
        self.assertEqual(defaults["commission_rate"], "0.15")
        self.assertEqual(defaults["packaging_fee_cny"], "2.00")
        self.assertEqual(defaults["rub_per_cny"], "12")

        updated = {**defaults, "rub_per_cny": "12.5"}
        saved_path = self.repo.save_pricing_settings(updated)

        self.assertEqual(saved_path.name, "pricing_settings.json")
        self.assertEqual(self.repo.load_pricing_settings(), updated)

    def test_package_weight_maps_only_to_package_weight_field(self) -> None:
        result = map_template_attributes(
            [
                {
                    "attribute_id": 1,
                    "attribute_label": "Вес с упаковкой, г",
                    "is_required": False,
                },
                {
                    "attribute_id": 2,
                    "attribute_label": "Вес товара, г",
                    "is_required": False,
                },
                {
                    "attribute_id": 3,
                    "attribute_label": "Длина упаковки, см",
                    "is_required": False,
                },
                {
                    "attribute_id": 4,
                    "attribute_label": "Длина, мм",
                    "is_required": False,
                },
            ],
            {},
            pricing_evidence={
                "package_weight_g": "380",
                "package_length_cm": "28",
            },
        )

        fields = {field["field_key"]: field for field in result["fields"]}
        self.assertEqual(fields["1"]["value"], "380")
        self.assertEqual(
            fields["1"]["source"],
            "user_confirmed_pricing_evidence",
        )
        self.assertEqual(fields["2"]["status"], "missing_fact")
        self.assertEqual(fields["3"]["value"], "28")
        self.assertEqual(fields["4"]["status"], "missing_fact")

    def test_home_page_loads(self) -> None:
        body = self.get_text("/")

        self.assertIn('class="app-shell"', body)
        self.assertIn('class="sidebar"', body)
        self.assertIn('class="topbar"', body)
        self.assertIn('class="workspace"', body)
        self.assertIn('id="diagnosticsPanel"', body)
        self.assertIn('id="toggleDiagnostics"', body)
        self.assertIn('id="exportDiagnostics"', body)
        self.assertIn('id="openStoreBinding"', body)
        self.assertIn('id="storeBindingPanel"', body)
        self.assertIn("运营驾驶舱 (Operations Cockpit)", body)
        self.assertIn("Ozon V2 工具台 (Workbench)", body)
        self.assertIn("开始自动执行 (Run Until Blocked)", body)
        self.assertIn("继续自动执行 (Continue Autopilot)", body)
        self.assertIn("执行器 (Runner)", body)
        self.assertIn("浏览器桥接 (Browser Bridge)", body)
        self.assertIn("Stop Current Task", body)
        self.assertIn('id="clearAllBatches"', body)
        self.assertIn("清空全部批次 (Clear All Batches)", body)
        self.assertIn("永久删除全部当前及历史批次运行数据", body)
        self.assertIn('/api/batches/clear', body)
        self.assertIn("/api/browser-bridge/status", body)
        self.assertIn('class="event-log"', body)
        self.assertIn("max-height: min(52vh, 560px)", body)
        self.assertIn("position: sticky", body)
        self.assertIn("扩展版本 (Extension Version)", body)
        self.assertIn("版本状态 (Version Status)", body)
        self.assertIn('id="bridgeVersion"', body)
        self.assertIn('id="bridgeVersionStatus"', body)
        self.assertIn('id="bridgeVersionWarning"', body)
        self.assertIn('id="startBatch" class="primary" disabled', body)
        self.assertIn('id="stageNavigation"', body)

        self.assertIn('id="ozonCollectionProgress"', body)
        self.assertIn('id="ozonProgressBar"', body)
        self.assertIn('role="progressbar"', body)
        self.assertIn('aria-valuemin="0"', body)
        self.assertIn('id="ozonProcessed"', body)
        self.assertIn('id="ozonSucceeded"', body)
        self.assertIn('id="ozonFailed"', body)
        self.assertIn('id="ozonReplaced"', body)
        self.assertIn('id="ozonPending"', body)
        self.assertIn("等待采集数据", body)
        self.assertIn("function eventPresentation(event)", body)
        self.assertIn("采集已入库", body)
        self.assertIn("后台执行器已启动", body)
        self.assertIn("替补商品恢复补采", body)
        self.assertIn("替补采集断点已清理", body)
        self.assertIn("系统事件（查看原始信息）", body)
        self.assertIn("event.event_type", body)
        self.assertIn("textContent", body)
        self.assertIn('id="batchOverviewNav"', body)
        self.assertIn('id="supplierReviewNav"', body)
        self.assertIn('new URLSearchParams(window.location.search).get("run_id")', body)
        self.assertIn("供应商审核 (Supplier Review)", body)
        self.assertNotIn("图片处理 (Images)", body)
        self.assertIn("上传草稿 (Upload)", body)
        self.assertIn('supplierNav.href = `/batches/${encodeURIComponent(runId)}/supplier-review`;', body)
        self.assertIn('const AUTO_ADVANCE_RUN_KEY = "ozon_v2_auto_advance_run_id";', body)
        self.assertIn("localStorage.setItem(AUTO_ADVANCE_RUN_KEY, state.runId);", body)
        self.assertIn('localStorage.getItem(AUTO_ADVANCE_RUN_KEY) === runId', body)
        self.assertIn("localStorage.removeItem(AUTO_ADVANCE_RUN_KEY);", body)
        self.assertIn('window.location.assign(`/batches/${encodeURIComponent(runId)}/supplier-review`);', body)
        self.assertIn("async function resolveLatestRunId()", body)
        self.assertIn("/api/operations/batches", body)
        self.assertIn('error.code === "workbench.batch_not_found"', body)
        self.assertIn('localStorage.removeItem("ozon_v2_workbench_run_id");', body)
        self.assertIn('$("startBatch").disabled = !ready;', body)
        self.assertIn("请在 Edge 扩展页面重新加载", body)
        self.assertNotIn("宸ュ叿", body)
        self.assertNotIn("鍏嶈垂", body)

    def test_unknown_batch_returns_structured_failure(self) -> None:
        result = WorkbenchService(self.repo).allowed_actions("wb-deleted-or-stale")

        self.assertFalse(result.ok)
        self.assertEqual("workbench.batch_not_found", result.code)
        self.assertEqual("wb-deleted-or-stale", result.data["run_id"])

    def test_browser_task_endpoint_degrades_to_none_for_deleted_batch(self) -> None:
        try:
            result = self.get_json("/api/batches/wb-deleted-or-stale/browser-task")
        except Exception as exc:
            self.fail(f"browser task lookup crashed on a deleted batch: {exc}")

        self.assertTrue(result["ok"])
        self.assertEqual("browser_task.none", result["code"])
        self.assertEqual("wb-deleted-or-stale", result["data"]["stale_run_id"])

    def test_allowed_actions_hide_internal_lifecycle_transitions(self) -> None:
        run_id, _seeds = self.prepare_ozon_collecting_run(count=1)

        result = WorkbenchService(self.repo).allowed_actions(run_id)

        self.assertTrue(result.ok)
        self.assertFalse(
            [action for action in result.data["allowed_actions"] if action.startswith("mark_")]
        )

    def test_dispatch_rejects_internal_lifecycle_transition(self) -> None:
        run_id, _seeds = self.prepare_ozon_collecting_run(count=1)

        result = WorkbenchService(self.repo).dispatch(
            run_id,
            WorkbenchAction.MARK_OZON_COLLECTED.value,
        )

        self.assertFalse(result.ok)
        self.assertEqual("workbench.internal_action_forbidden", result.code)
        self.assertEqual(
            WorkbenchState.OZON_COLLECTING.value,
            self.repo.load_run(run_id)["status"],
        )

    def test_home_page_contains_stage_specific_ozon_restart_control(self) -> None:
        page = self.get_text("/")

        self.assertIn('id="restartOzonCollection"', page)
        self.assertIn("重新启动 Ozon 原商品采集", page)
        self.assertIn("已完成商品及其原始属性字段不会重复采集", page)
        self.assertIn("/browser-task/restart", page)
        self.assertIn('status === "ozon_collecting"', page)
        self.assertIn("replacement_pending_seed_ids", page)
        self.assertIn("继续补采替补商品", page)

    def test_runtime_status_reports_service_extension_and_task(self) -> None:
        result = self.get_json("/api/runtime/status")

        self.assertTrue(result["ok"])
        self.assertEqual("online", result["data"]["service"]["code"])
        self.assertEqual("ready", result["data"]["extension"]["code"])
        self.assertIn(
            result["data"]["task"]["code"],
            {"idle", "running", "blocked", "stopped", "failed"},
        )

    def test_runtime_status_ignores_legacy_run_records(self) -> None:
        self.repo.create_run_record(
            target_count=1,
            sampled_seeds=[],
            random_seed=1,
            status=RunStatus.READY_FOR_OZON_COLLECTION,
        )

        try:
            result = self.get_json("/api/runtime/status")
        except Exception as exc:
            self.fail(f"runtime status crashed on a legacy run record: {exc}")

        self.assertEqual("idle", result["data"]["task"]["code"])
        self.assertIsNone(result["data"]["task"]["run_id"])

    def test_runtime_status_marks_unfinished_idle_run_as_blocked(self) -> None:
        run = self.repo.create_workbench_batch_record(target_count=1)
        run["status"] = WorkbenchState.IMAGE_PROCESSING.value
        self.repo.save_run(run)

        result = self.get_json("/api/runtime/status")

        self.assertEqual("blocked", result["data"]["task"]["code"])
        self.assertEqual(run["run_id"], result["data"]["task"]["run_id"])

    def test_runtime_stop_and_restart_use_injected_controller(self) -> None:
        controller = FakeRuntimeController()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(self.repo, runtime_controller=controller),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        try:
            for action in ("restart", "stop"):
                request_payload = {"open_edge": True} if action == "restart" else {}
                request = Request(
                    base_url + f"/api/runtime/{action}",
                    data=json.dumps(request_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(f"runtime.{action}_scheduled", result["code"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual([("restart", True), ("stop", False)], controller.actions)

    def test_runtime_capsule_is_injected_once_on_every_workbench_page(self) -> None:
        run_id = "runtime-capsule-test"
        paths = [
            "/",
            f"/batches/{run_id}/supplier-review",
            f"/batches/{run_id}/images",
            f"/batches/{run_id}/upload",
            "/diagnostics",
        ]

        for path in paths:
            with self.subTest(path=path):
                body = self.get_text(path)
                self.assertEqual(1, body.count('id="ozonRuntimeCapsule"'))
                self.assertIn("ozon_v2_runtime_capsule_position", body)
                self.assertIn("/api/runtime/status", body)
                self.assertIn("/api/runtime/restart", body)
                self.assertIn("/api/runtime/stop", body)
                self.assertIn('"open_edge": true', body)
                self.assertIn("确认停止 (Confirm Stop)", body)
                self.assertIn("服务 (Service)", body)
                self.assertIn("扩展 (Extension)", body)
                self.assertIn("任务 (Task)", body)
                self.assertIn("background: rgba(20, 24, 23, .96)", body)
                self.assertIn("const summaryLabels = {", body)
                self.assertIn("服务在线，扩展离线", body)
                self.assertIn("重启并连接 (Reconnect)", body)

    def test_batch_api_creates_batch_and_events(self) -> None:
        created = self.post_json("/api/batches", {"target_count": 2})
        run_id = created["data"]["run"]["run_id"]

        loaded = self.get_json(f"/api/batches/{run_id}")
        events = self.get_json(f"/api/batches/{run_id}/events")

        self.assertTrue(created["ok"])
        self.assertIn(loaded["data"]["status"], {WorkbenchState.CREATED.value, WorkbenchState.NEEDS_CREDENTIALS.value})
        self.assertIn("runner", loaded["data"])
        self.assertEqual("workbench.batch_created", events["data"]["events"][0]["event_type"])
        self.assertEqual(
            "Workbench batch created with publish locked.",
            events["data"]["events"][0]["message"],
        )

    def test_supplier_review_can_reject_missing_supplier_and_auto_refill(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        replacement = SeedProduct(
            seed_id="seed-replacement",
            title_or_keyword="replacement product",
            product_clue="replacement product",
            ozon_query_terms_ru=["replacement product ru"],
            query_generation_status="generated",
        )
        self.repo.save_active_seeds([seed, replacement])

        page = self.get_text(f"/batches/{run_id}/supplier-review")
        result = self.post_json(
            f"/api/batches/{run_id}/supplier-review/reject",
            {
                "seed_id": seed.seed_id,
                "reason": "User could not find an exact 1688 supplier.",
            },
        )

        self.assertIn("找不到供应商 (No Supplier Found)", page)
        self.assertIn("supplier-review/reject", page)
        self.assertIn('rejectButton.disabled = state.status !== "supplier_review";', page)
        self.assertNotIn('state.status !== "supplier_review" || !!item.supplier_url', page)
        self.assertTrue(result["ok"])
        self.assertEqual("supplier_review.candidate_replaced", result["code"])
        self.assertEqual([replacement.seed_id], self.repo.load_run(run_id)["replacement_pending_seed_ids"])

    def test_replacement_collection_merges_with_retained_evidence_and_links(self) -> None:
        rejected = SeedProduct(
            seed_id="seed-rejected",
            title_or_keyword="rejected product",
            product_clue="rejected product",
            ozon_query_terms_ru=["rejected product ru"],
            query_generation_status="generated",
        )
        retained = SeedProduct(
            seed_id="seed-retained",
            title_or_keyword="retained product",
            product_clue="retained product",
            ozon_query_terms_ru=["retained product ru"],
            query_generation_status="generated",
        )
        replacement = SeedProduct(
            seed_id="seed-replacement",
            title_or_keyword="replacement product",
            product_clue="replacement product",
            ozon_query_terms_ru=["replacement product ru"],
            query_generation_status="generated",
        )
        self.repo.save_active_seeds([rejected, retained, replacement])
        run = self.repo.create_workbench_batch_record(target_count=2)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [rejected, retained])
        templates = self.seller_attribute_template_payload(run_id, rejected.seed_id)
        templates["seed_templates"].append(
            self.seller_attribute_template_payload(run_id, retained.seed_id)["seed_templates"][0]
        )
        self.repo.save_attribute_template_result(run_id, templates)
        rejected_candidate = self.ozon_candidate_payload(rejected)
        rejected_candidate["ozon_product_id"] = "ozon-rejected"
        retained_candidate = self.ozon_candidate_payload(retained)
        retained_candidate["ozon_product_id"] = "ozon-retained"
        self.repo.save_ozon_collection_result(
            run_id,
            {"run_id": run_id, "worker": "workbench_browser_bridge", "ozon_candidates": [rejected_candidate, retained_candidate]},
        )
        self.repo.save_supplier_review(
            run_id,
            {
                "run_id": run_id,
                "items": [
                    {"seed_id": rejected.seed_id, "ozon_product_id": "ozon-rejected"},
                    {
                        "seed_id": retained.seed_id,
                        "ozon_product_id": "ozon-retained",
                        "supplier_url": "https://detail.1688.com/offer/retained.html",
                        "user_verified_exact_match": True,
                    },
                ],
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_REVIEW.value
        run["sampled_seed_ids"] = [rejected.seed_id, retained.seed_id]
        run["attribute_template_collected"] = True
        self.repo.save_run(run)
        service = WorkbenchService(self.repo, seller_api_adapter=FakeSellerApiAdapter())
        replaced = service.reject_supplier_candidate(run_id, rejected.seed_id, "supplier missing", random_seed=3)
        self.assertTrue(replaced.ok)

        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        self.repo.save_run(run)
        replacement_template = self.seller_attribute_template_payload(run_id, replacement.seed_id)
        ingested_template = service.ingest_attribute_template_result(run_id, replacement_template)
        self.assertTrue(ingested_template.ok)
        self.assertEqual(
            {retained.seed_id, replacement.seed_id},
            {item["seed_id"] for item in self.repo.load_attribute_template_result(run_id)["seed_templates"]},
        )

        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        self.repo.save_run(run)
        replacement_candidate = self.ozon_candidate_payload(replacement)
        replacement_candidate["ozon_product_id"] = "ozon-replacement"
        replacement_candidate["ozon_url"] = "https://www.ozon.ru/product/replacement-123/"
        ingested_ozon = service.ingest_ozon_collection_result(
            run_id,
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test",
                "ozon_candidates": [replacement_candidate],
            },
        )

        self.assertTrue(ingested_ozon.ok)
        self.assertEqual(
            {retained.seed_id, replacement.seed_id},
            {item["seed_id"] for item in self.repo.load_ozon_collection_result(run_id)["ozon_candidates"]},
        )
        review = self.repo.load_supplier_review(run_id)
        retained_review = next(item for item in review["items"] if item["seed_id"] == retained.seed_id)
        self.assertEqual("https://detail.1688.com/offer/retained.html", retained_review["supplier_url"])
        self.assertNotIn("replacement_pending_seed_ids", self.repo.load_run(run_id))

    def test_replacement_ozon_merge_failure_is_recorded_without_raising(self) -> None:
        retained = SeedProduct(
            seed_id="seed-retained",
            title_or_keyword="retained product",
            product_clue="retained product",
            ozon_query_terms_ru=["retained product ru"],
            query_generation_status="generated",
        )
        pending = SeedProduct(
            seed_id="seed-pending",
            title_or_keyword="pending product",
            product_clue="pending product",
            ozon_query_terms_ru=["pending product ru"],
            query_generation_status="generated",
        )
        run = self.repo.create_workbench_batch_record(target_count=2)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [retained, pending])
        templates = self.seller_attribute_template_payload(run_id, retained.seed_id)
        templates["seed_templates"].append(
            self.seller_attribute_template_payload(run_id, pending.seed_id)["seed_templates"][0]
        )
        self.repo.save_attribute_template_result(run_id, templates)
        self.repo.save_ozon_collection_result(
            run_id,
            {"ozon_candidates": [{"seed_id": retained.seed_id}]},
        )
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        run["replacement_pending_seed_ids"] = [pending.seed_id]
        self.repo.save_run(run)

        result = WorkbenchService(self.repo).ingest_ozon_collection_result(
            run_id,
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test",
                "ozon_candidates": [self.ozon_candidate_payload(pending)],
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual("workbench.ozon_collection_merge_invalid", result.code)
        self.assertEqual("ozon_collection.merge_invalid", self.repo.load_run_events(run_id)[-1].event_type)

    def test_clear_all_batches_requires_explicit_confirmation(self) -> None:
        run = self.repo.create_workbench_batch_record(target_count=1)

        result = self.post_json("/api/batches/clear", {"confirm": False}, ok=False)

        self.assertEqual("workbench.clear_confirmation_required", result["code"])
        self.assertTrue(self.repo.run_dir(run["run_id"]).exists())

    def test_clear_all_batches_archives_sampled_seed_usage_before_deletion(self) -> None:
        seed = self.repo.load_active_seeds()[0]
        run = self.repo.create_workbench_batch_record(target_count=1)
        self.repo.save_sampled_seeds(run["run_id"], [seed])

        result = self.post_json("/api/batches/clear", {"confirm": True})

        self.assertEqual("workbench.batches_cleared", result["code"])
        self.assertIn(self.repo.seed_identity_key(seed), self.repo.load_used_seed_identity_keys())
        self.assertNotIn(
            self.repo.seed_identity_key(seed),
            {self.repo.seed_identity_key(item) for item in self.repo.load_active_seeds()},
        )

    def test_clear_all_batches_removes_workbench_runs_and_resets_bridge_task(self) -> None:
        first = self.repo.create_workbench_batch_record(target_count=1)
        second = self.repo.create_workbench_batch_record(target_count=2)
        legacy_dir = self.repo.run_dir("run-legacy-clear")
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "run.json").write_text(
            json.dumps({"run_id": "run-legacy-clear", "status": "finalized"}),
            encoding="utf-8",
        )
        malformed_dir = self.repo.run_dir("wb-malformed-clear")
        malformed_dir.mkdir(parents=True)
        self.repo.save_browser_bridge_status(
            {
                "source": "ozon_content_script",
                "extension_version": self.required_extension_version(),
                "run_id": second["run_id"],
                "task_type": "ozon_collection",
                "stage": "collecting",
                "code": "browser_task.running",
            }
        )
        seeds_before = self.repo.active_seed_path.read_bytes()
        used_before = self.repo.used_seed_path.read_bytes()
        blacklist_before = self.repo.seed_blacklist_path.read_bytes()
        dedupe_before = self.repo.existing_store_dedupe_path.read_bytes()

        result = self.post_json("/api/batches/clear", {"confirm": True})

        self.assertEqual("workbench.batches_cleared", result["code"])
        self.assertEqual(
            {first["run_id"], second["run_id"], "run-legacy-clear", "wb-malformed-clear"},
            set(result["data"]["deleted_run_ids"]),
        )
        self.assertFalse(self.repo.run_dir(first["run_id"]).exists())
        self.assertFalse(self.repo.run_dir(second["run_id"]).exists())
        self.assertFalse(legacy_dir.exists())
        self.assertFalse(malformed_dir.exists())
        self.assertEqual(seeds_before, self.repo.active_seed_path.read_bytes())
        self.assertEqual(used_before, self.repo.used_seed_path.read_bytes())
        self.assertEqual(blacklist_before, self.repo.seed_blacklist_path.read_bytes())
        self.assertEqual(dedupe_before, self.repo.existing_store_dedupe_path.read_bytes())
        bridge = self.repo.load_browser_bridge_status()
        self.assertIsNone(bridge["run_id"])
        self.assertIsNone(bridge["task_type"])
        self.assertEqual("idle", bridge["stage"])
        self.assertEqual("browser_task.none", bridge["code"])

    def test_batch_post_starts_background_runner(self) -> None:
        created = self.post_json("/api/batches", {"target_count": 1})
        run_id = created["data"]["run"]["run_id"]

        status = self.wait_for_runner_idle(run_id)
        events = self.get_json(f"/api/batches/{run_id}/events")["data"]["events"]
        event_types = [event["event_type"] for event in events]

        self.assertEqual("blocked", status["state"])
        self.assertEqual("store_binding_required", status["blocked_reason"])
        self.assertIn("runner.started", event_types)
        self.assertIn("autopilot.blocked", event_types)
        self.assertIn("runner.blocked", event_types)

    def test_batch_api_rejects_offline_bridge_without_creating_or_sampling(self) -> None:
        self.repo.browser_bridge_status_path.unlink(missing_ok=True)

        result = self.assert_batch_rejected_without_side_effects("browser_bridge.offline")

        self.assertEqual("offline", result["data"]["browser_bridge"]["readiness_code"])

    def test_batch_api_rejects_unknown_or_mismatched_extension_without_side_effects(self) -> None:
        cases = (
            (None, "version_unknown"),
            ("0.1.9", "version_mismatch"),
        )
        for loaded_version, readiness_code in cases:
            with self.subTest(loaded_version=loaded_version):
                self.repo.save_browser_bridge_status(
                    {
                        "source": "background_interval",
                        "extension_version": loaded_version,
                        "stage": "idle",
                        "code": "browser_task.none",
                    }
                )

                result = self.assert_batch_rejected_without_side_effects("browser_bridge.update_required")

                self.assertEqual(readiness_code, result["data"]["browser_bridge"]["readiness_code"])

    def test_batch_api_rejects_expired_compatible_heartbeat(self) -> None:
        status = self.repo.load_browser_bridge_status()
        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=91)).isoformat()
        status["updated_at"] = expired_at
        status["connection_updated_at"] = expired_at
        self.repo.browser_bridge_status_path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self.assert_batch_rejected_without_side_effects("browser_bridge.offline")

    def test_browser_task_endpoint_returns_attribute_template_contract(self) -> None:
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["zerkalo dlya makiyazha"],
            query_generation_status="generated",
        )
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [seed])
        contract = CollectionContractService(self.repo).build_attribute_template_contract(run_id).data
        self.repo.save_attribute_template_contract(run_id, contract)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        run["attribute_template_contract_ready"] = True
        self.repo.save_run(run)

        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertTrue(task["ok"])
        self.assertEqual("browser_task.attribute_template_ready", task["code"])
        self.assertEqual("ozon_attribute_template", task["data"]["task_type"])
        self.assertEqual(run["created_at"], task["data"]["created_at"])
        self.assertEqual("workbench_browser_bridge", task["data"]["result_worker"])
        self.assertEqual(f"/api/batches/{run_id}/attribute-template", task["data"]["ingest_url"])

    def test_attribute_template_unexpected_error_returns_json_and_records_event(self) -> None:
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]

        with patch.object(
            WorkbenchService,
            "ingest_attribute_template_result",
            side_effect=RuntimeError("template submit exploded"),
        ):
            result = self.post_json(
                f"/api/batches/{run_id}/attribute-template",
                {"run_id": run_id, "seed_templates": []},
                ok=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual("http.internal_error", result["code"])
        self.assertEqual("RuntimeError", result["data"]["error_type"])
        event = self.repo.load_run_events(run_id)[-1]
        self.assertEqual("http.request_failed", event.event_type)
        self.assertEqual("RuntimeError", event.data["error_type"])

    def test_current_batch_diagnostics_endpoint_downloads_zip(self) -> None:
        run = self.repo.create_workbench_batch_record(target_count=2)
        run_id = run["run_id"]
        self.repo.append_run_event(run_id, "runner.failed", "Test failure.", {"api_key": "must-not-leak"})

        with urlopen(f"{self.base_url}/api/batches/{run_id}/diagnostics.zip", timeout=5) as response:
            content = response.read()
            content_type = response.headers.get("Content-Type")
            disposition = response.headers.get("Content-Disposition")

        self.assertEqual("application/zip", content_type)
        self.assertIn(f"ozon-v2-diagnostics-{run_id}.zip", disposition)
        self.assertGreater(len(content), 100)

    def test_browser_task_endpoint_returns_ozon_collection_ingest_url(self) -> None:
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["zerkalo dlya makiyazha"],
            query_generation_status="generated",
        )
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [seed])
        contract = CollectionContractService(self.repo).build_ozon_collection_contract(run_id).data
        self.repo.save_ozon_collection_contract(run_id, contract)
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        run["ozon_collection_contract_ready"] = True
        self.repo.save_run(run)

        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertTrue(task["ok"])
        self.assertEqual("browser_task.ozon_collection_ready", task["code"])
        self.assertEqual("ozon_collection", task["data"]["task_type"])
        self.assertEqual(f"/api/batches/{run_id}/ozon-collection", task["data"]["ingest_url"])

    def test_ozon_collection_progress_persists_candidates_in_contract_order(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=2)
        first = self.ozon_candidate_for_seed(seeds[0], "ozon-progress-1")
        second = self.ozon_candidate_for_seed(seeds[1], "ozon-progress-2")
        second["attributes"] = {"Материал": "сталь", "Цвет": "черный"}
        second["content_score_evidence"]["attribute_table"] = dict(second["attributes"])

        for candidate in (second, first, first):
            result = self.post_json(
                f"/api/batches/{run_id}/ozon-collection-progress",
                {
                    "run_id": run_id,
                    "worker": "workbench_browser_bridge",
                    "source": "test_browser_checkpoint",
                    "ozon_candidate": candidate,
                },
            )
            self.assertTrue(result["ok"])

        draft = self.repo.load_ozon_collection_draft(run_id)
        self.assertEqual(
            [seed.seed_id for seed in seeds],
            [item["seed_id"] for item in draft["ozon_candidates"]],
        )
        self.assertEqual(2, len(draft["ozon_candidates"]))
        self.assertEqual(second["attributes"], draft["ozon_candidates"][1]["attributes"])
        self.assertEqual(
            second["content_score_evidence"]["attribute_table"],
            draft["ozon_candidates"][1]["content_score_evidence"]["attribute_table"],
        )

    def test_ozon_collection_progress_uses_durable_checkpoint_after_status_is_overwritten(
        self,
    ) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=2)
        candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-progress-durable")
        saved = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": candidate,
            },
        )
        self.assertTrue(saved["ok"])
        self.repo.save_browser_bridge_status(
            {
                "source": "background_interval",
                "run_id": run_id,
                "task_type": "ozon_collection",
                "stage": "poll_failed",
                "code": "browser_bridge.poll_failed",
                "message": "A later heartbeat must not erase durable progress.",
                "details": None,
            }
        )

        progress = self.get_json(f"/api/batches/{run_id}")["data"]["progress"][
            "ozon_collection_progress"
        ]

        self.assertEqual(1, progress["success_count"])
        self.assertEqual(1, progress["processed_count"])
        self.assertEqual(1, progress["pending_count"])

    def test_ozon_collection_progress_rejects_run_seed_and_candidate_conflicts(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=2)
        first = self.ozon_candidate_for_seed(seeds[0], "ozon-progress-1")
        saved = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": first,
            },
        )
        self.assertTrue(saved["ok"])

        wrong_run = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": "wb-other",
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": first,
            },
            ok=False,
        )
        unexpected = self.ozon_candidate_for_seed(seeds[1], "ozon-progress-2")
        unexpected["seed_id"] = "seed-not-in-contract"
        wrong_seed = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": unexpected,
            },
            ok=False,
        )
        conflicting = self.ozon_candidate_for_seed(seeds[0], "ozon-progress-conflict")
        conflict = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": conflicting,
            },
            ok=False,
        )
        duplicate_product = self.ozon_candidate_for_seed(seeds[1], "ozon-progress-1")
        duplicate = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": duplicate_product,
            },
            ok=False,
        )

        self.assertEqual("ozon_collection.progress_run_mismatch", wrong_run["code"])
        self.assertEqual("ozon_collection.progress_seed_not_expected", wrong_seed["code"])
        self.assertEqual("ozon_collection.progress_conflict", conflict["code"])
        self.assertEqual("ozon_collection.progress_duplicate_product", duplicate["code"])
        self.assertEqual([first], self.repo.load_ozon_collection_draft(run_id)["ozon_candidates"])

    def test_ozon_collection_progress_rejects_invalid_public_attributes(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=1)
        candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-progress-invalid")
        candidate["attributes"] = {}
        candidate["content_score_evidence"]["attribute_table"] = {}

        result = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": candidate,
            },
            ok=False,
        )

        self.assertEqual("ozon_collection.progress_invalid", result["code"])
        self.assertFalse((self.repo.run_dir(run_id) / "ozon_collection_draft.json").exists())

    def test_ozon_collection_progress_reports_corrupt_existing_draft(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=1)
        draft_path = self.repo.run_dir(run_id) / "ozon_collection_draft.json"
        draft_path.write_text("{not-json", encoding="utf-8")
        original_bytes = draft_path.read_bytes()

        result = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": self.ozon_candidate_for_seed(seeds[0], "ozon-progress-1"),
            },
            ok=False,
        )

        self.assertEqual("ozon_collection.checkpoint_invalid", result["code"])
        self.assertEqual(original_bytes, draft_path.read_bytes())

    def test_ozon_collection_progress_rejects_other_batch_stage(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=1)
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_REVIEW.value
        self.repo.save_run(run)

        result = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": self.ozon_candidate_for_seed(seeds[0], "ozon-progress-1"),
            },
            ok=False,
        )

        self.assertEqual("ozon_collection.progress_not_expected", result["code"])

    def test_ozon_browser_task_exposes_verified_checkpoint_and_progress_url(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=2)
        candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-progress-1")
        self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": candidate,
            },
        )

        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertEqual(
            f"/api/batches/{run_id}/ozon-collection-progress",
            task["data"]["progress_url"],
        )
        self.assertEqual([candidate], task["data"]["resume_candidates"])

    def test_restart_browser_task_preserves_ozon_checkpoint_and_refreshes_token(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=2)
        candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-progress-1")
        self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": candidate,
            },
        )
        contract_before = (self.repo.run_dir(run_id) / "ozon_collection_contract.json").read_bytes()
        draft_before = (self.repo.run_dir(run_id) / "ozon_collection_draft.json").read_bytes()
        token_before = self.get_json(f"/api/batches/{run_id}/browser-task")["data"]["dispatch_token"]
        self.post_json(f"/api/batches/{run_id}/runner/stop", {})

        restarted = self.post_json(
            f"/api/batches/{run_id}/browser-task/restart",
            {"task_type": "supplier_selection"},
        )
        task_after = self.get_json(f"/api/batches/{run_id}/browser-task")
        run_after = self.repo.load_run(run_id)

        self.assertEqual("browser_task.restart_requested", restarted["code"])
        self.assertEqual("ozon_collection", restarted["data"]["task_type"])
        self.assertEqual(1, restarted["data"]["completed_count"])
        self.assertEqual(1, restarted["data"]["pending_count"])
        self.assertEqual("waiting_for_extension", restarted["data"]["dispatch_state"])
        self.assertNotEqual(token_before, task_after["data"]["dispatch_token"])
        self.assertFalse(run_after["browser_task_cancelled"])
        self.assertNotIn("browser_task_cancelled_at", run_after)
        self.assertNotIn("browser_task_cancel_reason", run_after)
        self.assertEqual(WorkbenchState.OZON_COLLECTING.value, run_after["status"])
        self.assertEqual(contract_before, (self.repo.run_dir(run_id) / "ozon_collection_contract.json").read_bytes())
        self.assertEqual(draft_before, (self.repo.run_dir(run_id) / "ozon_collection_draft.json").read_bytes())
        self.assertEqual(
            "browser_task.user_restart_requested",
            self.repo.load_run_events(run_id)[-1].event_type,
        )

    def test_autopilot_recovers_failed_batch_with_pending_replacement(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=1)
        stale_candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-stale-autopilot")
        stale_candidate["seed_id"] = "seed-rejected-before-replacement"
        self.repo.save_ozon_collection_draft(
            run_id,
            {
                "schema_version": 1,
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "stale_rejected_product",
                "ozon_candidates": [stale_candidate],
                "created_at": "2026-07-24T00:00:00+00:00",
                "updated_at": "2026-07-24T00:00:00+00:00",
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.FAILED_BLOCKED.value
        run["replacement_pending_seed_ids"] = [seeds[0].seed_id]
        self.repo.save_run(run)

        result = WorkbenchService(self.repo).run_until_blocked(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("collection_worker_required", result.data["blocked_reason"])
        self.assertEqual(
            WorkbenchState.OZON_COLLECTING.value,
            self.repo.load_run(run_id)["status"],
        )
        self.assertIn(
            "supplier_review.replacement_recovery_started",
            [event.event_type for event in self.repo.load_run_events(run_id)],
        )
        self.assertFalse(
            (self.repo.run_dir(run_id) / "ozon_collection_draft.json").exists()
        )

    def test_autopilot_restores_complete_result_before_pending_replacement_recovery(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=1)
        candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-complete-autopilot")
        payload = {
            "run_id": run_id,
            "worker": "workbench_browser_bridge",
            "ozon_candidates": [candidate],
        }
        self.repo.save_ozon_collection_result(run_id, payload)
        service = WorkbenchService(self.repo)
        self.repo.save_supplier_review(run_id, service._build_supplier_review(run_id, payload))
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.FAILED_BLOCKED.value
        run["replacement_pending_seed_ids"] = [seeds[0].seed_id]
        run["ozon_collected"] = False
        self.repo.save_run(run)

        result = service.run_until_blocked(run_id)
        loaded = self.repo.load_run(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("supplier_review_required", result.data["blocked_reason"])
        self.assertEqual(WorkbenchState.SUPPLIER_REVIEW.value, loaded["status"])
        self.assertNotIn("replacement_pending_seed_ids", loaded)

    def test_restart_browser_task_recovers_failed_replacement_and_discards_stale_draft(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=2)
        stale_candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-stale")
        self.repo.save_ozon_collection_draft(
            run_id,
            {
                "schema_version": 1,
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "stale_rejected_product",
                "ozon_candidates": [stale_candidate],
                "created_at": "2026-07-24T00:00:00+00:00",
                "updated_at": "2026-07-24T00:00:00+00:00",
            },
        )
        contract = self.repo.load_ozon_collection_contract(run_id)
        contract["payload"]["seeds"] = [
            seed
            for seed in contract["payload"]["seeds"]
            if seed["seed_id"] == seeds[1].seed_id
        ]
        self.repo.save_ozon_collection_contract(run_id, contract)
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.FAILED_BLOCKED.value
        run["replacement_pending_seed_ids"] = [seeds[1].seed_id]
        self.repo.save_run(run)

        result = WorkbenchService(self.repo).restart_browser_task(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("ozon_collection", result.data["task_type"])
        self.assertEqual(0, result.data["completed_count"])
        self.assertEqual(1, result.data["pending_count"])
        self.assertEqual(
            WorkbenchState.OZON_COLLECTING.value,
            self.repo.load_run(run_id)["status"],
        )
        self.assertFalse(
            (self.repo.run_dir(run_id) / "ozon_collection_draft.json").exists()
        )

    def test_restart_browser_task_maps_supplier_review_and_keeps_only_pending_channels(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()
        review = self.repo.load_supplier_review(run_id)
        base = dict(review["items"][0])
        review["items"] = []
        for index in range(3):
            item = dict(base)
            item.update(
                {
                    "seed_id": f"seed-supplier-{index}",
                    "ozon_product_id": f"ozon-supplier-{index}",
                    "ozon_title": f"Ozon supplier product {index}",
                    "ozon_main_image": f"https://img.example/supplier-{index}.jpg",
                    "supplier_url": None,
                }
            )
            review["items"].append(item)
        self.repo.save_supplier_review(run_id, review)
        self.repo.save_supplier_selection_draft(
            run_id,
            {
                "schema_version": 1,
                "run_id": run_id,
                "supplier_products": [
                    {
                        "seed_id": "seed-supplier-0",
                        "supplier_url": "https://detail.1688.com/offer/100.html",
                    }
                ],
            },
        )
        token_before = self.get_json(f"/api/batches/{run_id}/browser-task")["data"]["dispatch_token"]

        restarted = self.post_json(f"/api/batches/{run_id}/browser-task/restart", {})
        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertEqual("supplier_selection", restarted["data"]["task_type"])
        self.assertEqual(1, restarted["data"]["completed_count"])
        self.assertEqual(2, restarted["data"]["pending_count"])
        self.assertEqual("dispatched", restarted["data"]["dispatch_state"])
        self.assertNotEqual(token_before, task["data"]["dispatch_token"])
        self.assertEqual(
            ["seed-supplier-1", "seed-supplier-2"],
            [item["seed_id"] for item in task["data"]["contract"]["items"]],
        )

    def test_restart_browser_task_maps_supplier_collecting_without_changing_contract(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        service = WorkbenchService(self.repo)
        saved = service.save_supplier_review_links(
            run_id,
            [
                {
                    "seed_id": seed.seed_id,
                    "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                }
            ],
        )
        self.assertTrue(saved.ok)
        started = service.start_supplier_collection(run_id)
        self.assertTrue(started.ok)
        contract_path = self.repo.run_dir(run_id) / "supplier_collection_contract.json"
        contract_before = contract_path.read_bytes()
        token_before = self.get_json(f"/api/batches/{run_id}/browser-task")["data"]["dispatch_token"]

        restarted = self.post_json(f"/api/batches/{run_id}/browser-task/restart", {})
        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertEqual("supplier_collection", restarted["data"]["task_type"])
        self.assertEqual(0, restarted["data"]["completed_count"])
        self.assertEqual(1, restarted["data"]["pending_count"])
        self.assertEqual(WorkbenchState.SUPPLIER_COLLECTING.value, self.repo.load_run(run_id)["status"])
        self.assertEqual(contract_before, contract_path.read_bytes())
        self.assertNotEqual(token_before, task["data"]["dispatch_token"])

    def test_restart_browser_task_rejects_completed_and_unrelated_stages(self) -> None:
        supplier_run_id, _seed = self.prepare_supplier_review_run()
        supplier_run = self.repo.load_run(supplier_run_id)
        supplier_run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(supplier_run)

        unrelated = self.repo.create_workbench_batch_record(target_count=1)
        unrelated_run_id = unrelated["run_id"]

        ozon_run_id, seeds = self.prepare_ozon_collecting_run(count=1)
        self.repo.save_ozon_collection_result(
            ozon_run_id,
            {"ozon_candidates": [self.ozon_candidate_for_seed(seeds[0], "ozon-final")]},
        )

        completed_supplier = self.post_json(
            f"/api/batches/{supplier_run_id}/browser-task/restart",
            {},
            ok=False,
        )
        unrelated_result = self.post_json(
            f"/api/batches/{unrelated_run_id}/browser-task/restart",
            {},
            ok=False,
        )
        completed_ozon = self.post_json(
            f"/api/batches/{ozon_run_id}/browser-task/restart",
            {},
            ok=False,
        )

        self.assertEqual("browser_task.restart_not_allowed", completed_supplier["code"])
        self.assertEqual("browser_task.restart_not_allowed", unrelated_result["code"])
        self.assertEqual("browser_task.restart_completed", completed_ozon["code"])
        self.assertNotIn("browser_task_resumed_at", self.repo.load_run(ozon_run_id))

    def test_restart_browser_task_rejects_corrupt_ozon_checkpoint_without_new_token(self) -> None:
        run_id, _seeds = self.prepare_ozon_collecting_run(count=1)
        draft_path = self.repo.run_dir(run_id) / "ozon_collection_draft.json"
        draft_path.write_text("{not-json", encoding="utf-8")
        original_bytes = draft_path.read_bytes()

        result = self.post_json(
            f"/api/batches/{run_id}/browser-task/restart",
            {},
            ok=False,
        )

        self.assertEqual("browser_task.restart_checkpoint_invalid", result["code"])
        self.assertNotIn("browser_task_resumed_at", self.repo.load_run(run_id))
        self.assertEqual(original_bytes, draft_path.read_bytes())

    def test_restart_browser_task_waits_for_offline_extension(self) -> None:
        run_id, _seeds = self.prepare_ozon_collecting_run(count=1)

        with patch("ozon_v2.workbench.local_server.BRIDGE_HEARTBEAT_TIMEOUT_SECONDS", -1):
            result = self.post_json(f"/api/batches/{run_id}/browser-task/restart", {})

        self.assertTrue(result["ok"])
        self.assertEqual("waiting_for_extension", result["data"]["dispatch_state"])
        self.assertFalse(result["data"]["browser_bridge"]["online"])
        self.assertIn("browser_task_resumed_at", self.repo.load_run(run_id))

    def test_browser_task_endpoint_returns_supplier_collection_contract(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        saved = WorkbenchService(self.repo).save_supplier_review_links(
            run_id,
            [
                {
                    "seed_id": seed.seed_id,
                    "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                }
            ],
        )
        self.assertTrue(saved.ok)
        started = WorkbenchService(self.repo).start_supplier_collection(run_id)
        self.assertTrue(started.ok)

        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertTrue(task["ok"])
        self.assertEqual("browser_task.supplier_collection_ready", task["code"])
        self.assertEqual("supplier_collection", task["data"]["task_type"])
        self.assertEqual(
            "https://detail.1688.com/offer/123456789012.html",
            task["data"]["contract"]["items"][0]["supplier_url"],
        )
        self.assertEqual(
            f"/api/batches/{run_id}/supplier-collection-result",
            task["data"]["ingest_url"],
        )

    def test_starting_supplier_collection_clears_stale_browser_cancellation(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        service = WorkbenchService(self.repo)
        saved = service.save_supplier_review_links(
            run_id,
            [
                {
                    "seed_id": seed.seed_id,
                    "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                }
            ],
        )
        self.assertTrue(saved.ok)
        stopped = self.post_json(f"/api/batches/{run_id}/runner/stop", {})
        self.assertTrue(stopped["ok"])
        self.assertTrue(self.repo.load_run(run_id)["browser_task_cancelled"])

        started = self.post_json(f"/api/batches/{run_id}/supplier-collection", {})
        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertTrue(started["ok"])
        self.assertFalse(self.repo.load_run(run_id)["browser_task_cancelled"])
        self.assertEqual("browser_task.supplier_collection_ready", task["code"])
        self.assertEqual("supplier_collection", task["data"]["task_type"])

    def test_browser_task_endpoint_returns_managed_supplier_selection_channels(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        review = self.repo.load_supplier_review(run_id)
        review["items"][0]["ozon_reference_images"] = [
            "https://img.example/main.jpg",
            "https://img.example/alternate.jpg",
        ]
        self.repo.save_supplier_review(run_id, review)

        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertTrue(task["ok"])
        self.assertEqual("browser_task.supplier_selection_ready", task["code"])
        self.assertEqual("supplier_selection", task["data"]["task_type"])
        self.assertEqual(
            f"/api/batches/{run_id}/supplier-selection/capture",
            task["data"]["capture_url"],
        )
        self.assertEqual(
            f"/api/batches/{run_id}/supplier-review/reject",
            task["data"]["reject_url"],
        )
        channel = task["data"]["contract"]["items"][0]
        self.assertEqual(0, channel["channel_index"])
        self.assertEqual(seed.seed_id, channel["seed_id"])
        self.assertEqual("ozon-1", channel["ozon_product_id"])
        self.assertEqual("https://img.example/main.jpg", channel["reference_image_url"])
        self.assertEqual(
            ["https://img.example/main.jpg", "https://img.example/alternate.jpg"],
            channel["reference_image_urls"],
        )

    def test_supplier_review_combines_sku_and_gallery_reference_images(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=1)
        candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-image-fallbacks")
        candidate["selected_sku_media"] = {
            "selected_sku_images": ["https://img.example/wc100/main.jpg"],
            "main_gallery_images": [
                "https://img.example/wc300/main.jpg",
                "https://img.example/wc200/alternate.jpg",
            ],
        }

        review = WorkbenchService(self.repo)._build_supplier_review(
            run_id,
            {"run_id": run_id, "ozon_candidates": [candidate]},
        )

        self.assertEqual(
            ["https://img.example/main.jpg", "https://img.example/alternate.jpg"],
            review["items"][0]["ozon_reference_images"],
        )

    def test_restart_restores_complete_ozon_result_before_replacement_recovery(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=2)
        candidates = [
            self.ozon_candidate_for_seed(seed, f"ozon-complete-{index}")
            for index, seed in enumerate(seeds)
        ]
        payload = {
            "run_id": run_id,
            "worker": "workbench_browser_bridge",
            "ozon_candidates": candidates,
        }
        self.repo.save_ozon_collection_result(run_id, payload)
        service = WorkbenchService(self.repo)
        self.repo.save_supplier_review(run_id, service._build_supplier_review(run_id, payload))
        self.repo.save_ozon_collection_draft(
            run_id,
            {
                "schema_version": 1,
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "completed_before_late_retry",
                "ozon_candidates": candidates,
                "created_at": "2026-07-31T00:00:00+00:00",
                "updated_at": "2026-07-31T00:00:00+00:00",
            },
        )
        contract = self.repo.load_ozon_collection_contract(run_id)
        contract["payload"]["seeds"] = [
            seed for seed in contract["payload"]["seeds"]
            if seed["seed_id"] == seeds[1].seed_id
        ]
        self.repo.save_ozon_collection_contract(run_id, contract)
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        run["replacement_pending_seed_ids"] = [seeds[1].seed_id]
        run["ozon_collected"] = False
        run["browser_task_cancelled"] = True
        run["browser_task_cancel_reason"] = "user_stopped"
        self.repo.save_run(run)

        restarted = service.restart_browser_task(run_id)
        loaded = self.repo.load_run(run_id)
        draft = self.repo.load_ozon_collection_draft(run_id)

        self.assertTrue(restarted.ok)
        self.assertEqual("supplier_selection", restarted.data["task_type"])
        self.assertEqual(WorkbenchState.SUPPLIER_REVIEW.value, loaded["status"])
        self.assertTrue(loaded["ozon_collected"])
        self.assertNotIn("replacement_pending_seed_ids", loaded)
        self.assertFalse(loaded["browser_task_cancelled"])
        self.assertEqual([seed.seed_id for seed in seeds], [item["seed_id"] for item in draft["ozon_candidates"]])

    def test_managed_supplier_task_returns_only_five_pending_channels(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()
        review = self.repo.load_supplier_review(run_id)
        base = dict(review["items"][0])
        review["items"] = []
        for index in range(9):
            item = dict(base)
            item.update(
                {
                    "seed_id": f"seed-{index}",
                    "ozon_product_id": f"ozon-{index}",
                    "ozon_title": f"Ozon product {index}",
                    "ozon_main_image": f"https://img.example/{index}.jpg",
                }
            )
            review["items"].append(item)
        self.repo.save_supplier_review(run_id, review)
        self.repo.save_supplier_selection_draft(
            run_id,
            {
                "schema_version": 1,
                "run_id": run_id,
                "supplier_products": [
                    {"seed_id": "seed-0", "supplier_url": "https://detail.1688.com/offer/100.html"},
                    {"seed_id": "seed-1", "supplier_url": "https://detail.1688.com/offer/101.html"},
                ],
            },
        )

        task = self.get_json(f"/api/batches/{run_id}/browser-task")
        channels = task["data"]["contract"]["items"]

        self.assertEqual(5, len(channels))
        self.assertEqual([2, 3, 4, 5, 6], [item["channel_index"] for item in channels])
        self.assertEqual(
            ["seed-2", "seed-3", "seed-4", "seed-5", "seed-6"],
            [item["seed_id"] for item in channels],
        )

    def test_supplier_review_uses_managed_lanes_without_url_inputs(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/supplier-review")

        self.assertNotIn('input.type = "url"', page)
        self.assertIn("五通道采集", page)
        self.assertIn("找不到供应商 (No Supplier Found)", page)
        self.assertIn("重新采集 (Re-collect)", page)
        self.assertIn("/supplier-selection/reset", page)
        self.assertIn('["supplier_review", "supplier_collecting"].includes(state.status)', page)

    def test_supplier_review_contains_stage_specific_1688_restart_control(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/supplier-review")

        self.assertIn('id="collect"', page)
        self.assertIn("重新启动 1688 采集", page)
        self.assertIn("只重新打开未完成通道", page)
        self.assertIn("/browser-task/restart", page)
        self.assertIn('["supplier_review", "supplier_collecting"].includes(state.status)', page)

    def test_supplier_selection_capture_rejects_wrong_channel_binding(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 9,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "supplier_product": self.supplier_product_payload(seed.seed_id),
            },
            ok=False,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("supplier_selection.channel_mismatch", result["code"])
        self.assertEqual(WorkbenchState.SUPPLIER_REVIEW.value, self.repo.load_run(run_id)["status"])

    def test_supplier_selection_capture_recovers_completed_ozon_state_without_invalidating_lane(self) -> None:
        run_id, seeds = self.prepare_ozon_collecting_run(count=1)
        candidate = self.ozon_candidate_for_seed(seeds[0], "ozon-recovered-capture")
        payload = {
            "run_id": run_id,
            "worker": "workbench_browser_bridge",
            "ozon_candidates": [candidate],
        }
        service = WorkbenchService(self.repo)
        self.repo.save_ozon_collection_result(run_id, payload)
        review = service._build_supplier_review(run_id, payload)
        self.repo.save_supplier_review(run_id, review)
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        run["ozon_collected"] = False
        self.repo.save_run(run)

        result = service.capture_supplier_selection_product(
            run_id,
            {
                "channel_index": 0,
                "seed_id": seeds[0].seed_id,
                "ozon_product_id": "ozon-recovered-capture",
                "dispatch_token": review["created_at"],
                "supplier_product": self.supplier_product_payload(seeds[0].seed_id),
            },
        )

        self.assertTrue(result.ok, result.to_dict())
        self.assertNotEqual("supplier_selection.not_expected", result.code)

    def test_supplier_selection_capture_rejects_stale_dispatch_generation(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        current_task = self.get_json(f"/api/batches/{run_id}/browser-task")
        current_token = current_task["data"]["dispatch_token"]

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "dispatch_token": f"{current_token}-stale",
                "supplier_product": self.supplier_product_payload(seed.seed_id),
            },
            ok=False,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("supplier_selection.dispatch_stale", result["code"])
        self.assertFalse((self.repo.run_dir(run_id) / "supplier_selection_draft.json").exists())
        self.assertEqual(WorkbenchState.SUPPLIER_REVIEW.value, self.repo.load_run(run_id)["status"])

    def test_supplier_selection_capture_rejects_mixed_offer_identity(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        task = self.get_json(f"/api/batches/{run_id}/browser-task")
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["final_url"] = supplier_product["supplier_url"]
        supplier_product["offer_id"] = "999999999999"

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "dispatch_token": task["data"]["dispatch_token"],
                "supplier_product": supplier_product,
            },
            ok=False,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("supplier_selection.offer_identity_mismatch", result["code"])
        self.assertFalse((self.repo.run_dir(run_id) / "supplier_selection_draft.json").exists())

    def test_supplier_selection_capture_rejects_offer_already_assigned_to_another_product(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        review = self.repo.load_supplier_review(run_id)
        second_item = dict(review["items"][0])
        second_item.update(
            {
                "channel_index": 1,
                "seed_id": "seed-second",
                "ozon_product_id": "ozon-2",
                "ozon_title": "Second Ozon product",
                "supplier_url": None,
                "user_verified_exact_match": False,
            }
        )
        review["items"].append(second_item)
        self.repo.save_supplier_review(run_id, review)
        task = self.get_json(f"/api/batches/{run_id}/browser-task")
        dispatch_token = task["data"]["dispatch_token"]

        first = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "dispatch_token": dispatch_token,
                "supplier_product": self.supplier_product_payload(seed.seed_id),
            },
        )
        second_product = self.supplier_product_payload("seed-second")
        second = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 1,
                "seed_id": "seed-second",
                "ozon_product_id": "ozon-2",
                "dispatch_token": dispatch_token,
                "supplier_product": second_product,
            },
            ok=False,
        )

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual("supplier_selection.offer_already_assigned", second["code"])
        self.assertEqual(seed.seed_id, second["data"]["conflicting_seed_id"])
        draft = self.repo.load_supplier_selection_draft(run_id)
        self.assertEqual(
            [seed.seed_id],
            [item["seed_id"] for item in draft["supplier_products"]],
        )

    def test_supplier_selection_reset_removes_wrong_capture_and_reopens_lane(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        review = self.repo.load_supplier_review(run_id)
        review["items"][0].update(
            {
                "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                "user_verified_exact_match": True,
                "verified_at": "2026-07-20T00:00:00+00:00",
            }
        )
        self.repo.save_supplier_review(run_id, review)
        self.repo.save_supplier_selection_draft(
            run_id,
            {
                "schema_version": 1,
                "run_id": run_id,
                "supplier_products": [self.supplier_product_payload(seed.seed_id)],
            },
        )

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/reset",
            {"seed_id": seed.seed_id},
        )

        self.assertTrue(result["ok"])
        self.assertEqual("supplier_selection.recapture_requested", result["code"])
        reset_item = self.repo.load_supplier_review(run_id)["items"][0]
        self.assertIsNone(reset_item["supplier_url"])
        self.assertFalse(reset_item["user_verified_exact_match"])
        self.assertIsNone(reset_item["verified_at"])
        self.assertEqual([], self.repo.load_supplier_selection_draft(run_id)["supplier_products"])
        restarted = self.post_json(f"/api/batches/{run_id}/browser-task/restart", {})
        self.assertEqual(1, restarted["data"]["pending_count"])

    def test_incomplete_supplier_sku_can_be_recaptured_after_image_stage_without_rolling_back_batch(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        incomplete = self.supplier_product_payload(seed.seed_id)
        incomplete["sku_options"] = []
        incomplete["sku_groups"] = [
            {
                "name": "规格",
                "options": [
                    {
                        "label": "儿童飞机脚踏板【黑色】",
                        "supplier_sku_id": "",
                        "image_url": "https://cbu01.alicdn.com/img/ibank/hammock-black.jpg",
                    }
                ],
            }
        ]
        captured = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "supplier_product": incomplete,
            },
        )
        self.assertTrue(captured["ok"])

        collection = self.repo.load_supplier_collection_result(run_id)
        retained_product = self.supplier_product_payload("seed-retained")
        retained_product["title"] = "Already completed supplier product"
        retained_product["supplier_url"] = (
            "https://detail.1688.com/offer/987654321098.html"
        )
        retained_product["final_url"] = retained_product["supplier_url"]
        retained_product["offer_id"] = "987654321098"
        collection["supplier_products"].append(retained_product)
        self.repo.save_supplier_collection_result(run_id, collection)
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.IMAGE_PROCESSING.value
        self.repo.save_run(run)

        reset = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/reset",
            {"seed_id": seed.seed_id},
        )
        restarted = self.post_json(f"/api/batches/{run_id}/browser-task/restart", {})
        task = self.get_json(f"/api/batches/{run_id}/browser-task")

        self.assertTrue(reset["ok"])
        self.assertEqual(WorkbenchState.IMAGE_PROCESSING.value, self.repo.load_run(run_id)["status"])
        self.assertEqual([seed.seed_id], self.repo.load_run(run_id)["supplier_recapture_seed_ids"])
        self.assertEqual(
            ["seed-retained"],
            [item["seed_id"] for item in self.repo.load_supplier_collection_result(run_id)["supplier_products"]],
        )
        self.assertTrue(restarted["ok"])
        self.assertEqual("supplier_selection", restarted["data"]["task_type"])
        self.assertEqual(1, restarted["data"]["pending_count"])
        self.assertEqual("supplier_selection", task["data"]["task_type"])
        self.assertEqual([seed.seed_id], [item["seed_id"] for item in task["data"]["contract"]["items"]])

        recaptured_product = self.supplier_product_payload(seed.seed_id)
        recaptured = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "supplier_product": recaptured_product,
            },
        )
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")

        self.assertTrue(recaptured["ok"])
        self.assertEqual("supplier_selection.recapture_complete", recaptured["code"])
        self.assertEqual(WorkbenchState.IMAGE_PROCESSING.value, self.repo.load_run(run_id)["status"])
        self.assertEqual([], self.repo.load_run(run_id).get("supplier_recapture_seed_ids"))
        self.assertEqual(
            {seed.seed_id, "seed-retained"},
            {
                item["seed_id"]
                for item in self.repo.load_supplier_collection_result(run_id)["supplier_products"]
            },
        )
        self.assertEqual(1, len(review["data"]["items"][0]["supplier_sku_options"]))

    def test_supplier_review_page_enables_targeted_recapture_for_incomplete_late_stage_product(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/supplier-review")

        self.assertIn("function canRecaptureSupplier(item)", page)
        self.assertIn('["supplier_review", "supplier_collected", "image_processing"].includes(state.status)', page)
        self.assertNotIn("&& !!item.supplier_url", page)
        self.assertIn("recaptureButton.disabled = !canRecaptureSupplier(item);", page)
        self.assertIn("重新采集此商品 SKU", page)

    def test_supplier_selection_capture_writes_back_and_finalizes_single_channel(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["sku_groups"] = [
            {
                "name": "颜色",
                "options": [
                    {
                        "label": "黑色",
                        "supplier_sku_id": "color-black",
                        "image_url": "https://cbu01.alicdn.com/img/ibank/test.jpg",
                    }
                ],
            }
        ]

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "supplier_product": supplier_product,
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual("supplier_selection.batch_complete", result["code"])
        self.assertEqual(1, result["data"]["captured_count"])
        self.assertEqual(1, result["data"]["total_count"])
        self.assertTrue(result["data"]["accepted"])
        self.assertEqual("collected", result["data"]["lane_terminal"])
        self.assertTrue((self.repo.run_dir(run_id) / "supplier_selection_draft.json").exists())
        self.assertTrue((self.repo.run_dir(run_id) / "supplier_collection_result.json").exists())
        stored = self.repo.load_supplier_collection_result(run_id)["supplier_products"][0]
        self.assertEqual(supplier_product["attributes"], stored["attributes"])
        self.assertEqual(supplier_product["sku_groups"], stored["sku_groups"])
        self.assertEqual(supplier_product["sku_options"], stored["sku_options"])
        self.assertEqual(WorkbenchState.SUPPLIER_COLLECTED.value, self.repo.load_run(run_id)["status"])

    def test_supplier_selection_capture_defers_missing_sku_matrix_to_user_review(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["sku_options"] = []
        supplier_product["sku_groups"] = [
            {
                "name": "颜色",
                "options": [
                    {
                        "label": "黑色",
                        "supplier_sku_id": "",
                        "image_url": "https://cbu01.alicdn.com/img/ibank/test.jpg",
                    }
                ],
            }
        ]

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "supplier_product": supplier_product,
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual("supplier_selection.batch_complete", result["code"])
        stored = self.repo.load_supplier_collection_result(run_id)["supplier_products"][0]
        self.assertEqual([], stored["sku_options"])
        self.assertEqual("manual_confirmation_required", stored["sku_matrix_status"])
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        item = review["data"]["items"][0]
        self.assertEqual("颜色", item["supplier_sku_groups"][0]["name"])
        self.assertEqual(1, len(item["supplier_sku_options"]))
        self.assertEqual(
            "single_visible_sku_combination",
            item["supplier_sku_options"][0]["evidence_source"],
        )
        self.assertEqual(
            {"颜色": "黑色"},
            item["supplier_sku_options"][0]["selected_options"],
        )
        self.assertTrue(item["supplier_sku_options"][0]["complete"])
        self.assertEqual(WorkbenchState.SUPPLIER_COLLECTED.value, self.repo.load_run(run_id)["status"])

    def test_supplier_review_exposes_multi_sku_candidates_without_variant_images_read_only(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        task = self.get_json(f"/api/batches/{run_id}/browser-task")
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["sku_groups"] = [
            {
                "name": "颜色",
                "options": [
                    {"label": "银色一个", "supplier_sku_id": "", "image_url": ""},
                    {"label": "蓝色一个", "supplier_sku_id": "", "image_url": ""},
                ],
            }
        ]
        supplier_product["sku_options"] = [
            {
                "supplier_sku_id": "sku-silver",
                "combination_key": "银色一个",
                "raw_label": "银色一个",
                "selected_options": {"颜色": "银色一个"},
                "set_quantity": 1,
                "set_composition": ["银色一个"],
                "price": {"currency": "CNY", "amount": "110.00"},
                "stock": {"status": "in_stock", "quantity": 4753},
                "image_urls": [],
                "evidence_source": "embedded_sku_map",
                "complete": False,
            },
            {
                "supplier_sku_id": "sku-blue",
                "combination_key": "蓝色一个",
                "raw_label": "蓝色一个",
                "selected_options": {"颜色": "蓝色一个"},
                "set_quantity": 1,
                "set_composition": ["蓝色一个"],
                "price": {"currency": "CNY", "amount": "110.00"},
                "stock": {"status": "in_stock", "quantity": 4946},
                "image_urls": [],
                "evidence_source": "embedded_sku_map",
                "complete": False,
            },
        ]

        captured = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "dispatch_token": task["data"]["dispatch_token"],
                "supplier_product": supplier_product,
            },
        )
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        item = review["data"]["items"][0]
        page = self.get_text(f"/batches/{run_id}/supplier-review")

        self.assertTrue(captured["ok"])
        self.assertEqual([], item["supplier_sku_options"])
        self.assertEqual(
            ["sku-silver", "sku-blue"],
            [candidate["supplier_sku_id"] for candidate in item["supplier_sku_candidates"]],
        )
        self.assertTrue(all(not candidate["image_urls"] for candidate in item["supplier_sku_candidates"]))
        self.assertIn("supplier_sku_candidates", page)
        self.assertIn("已识别但不可锁定", page)
        self.assertIn("公共商品图", page)

    def test_supplier_selection_capture_exposes_page_unique_sku_for_confirmation(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["sku"] = {
            "selected_options": {"visible_sku_labels": ["单一 SKU（页面无可选规格）"]},
            "evidence": "no_visible_variant_selector",
            "evidence_source": "dom_option_labels",
            "complete": False,
        }
        supplier_product["sku_options"] = []
        supplier_product["sku_groups"] = []

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "supplier_product": supplier_product,
            },
        )
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        options = review["data"]["items"][0]["supplier_sku_options"]
        selected = self.post_json(
            f"/api/batches/{run_id}/supplier-sku",
            {"seed_id": seed.seed_id, "supplier_sku_id": "123456789012", "differences": []},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(options))
        self.assertEqual("123456789012", options[0]["supplier_sku_id"])
        self.assertEqual("single_sku_detail_page", options[0]["evidence_source"])
        self.assertTrue(selected["ok"])
        self.assertTrue(selected["data"]["selection_status"]["complete"])

    def test_supplier_selection_capture_repairs_page_unique_composite_quantity(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["sku"] = {
            "selected_options": {"visible_sku_labels": ["清洁剂100ml*2+刷子*1"]},
            "evidence": "visible_selected_or_available_sku_labels",
            "evidence_source": "dom_option_labels",
            "complete": False,
        }
        supplier_product["sku_groups"] = [
            {
                "name": "颜色分类",
                "options": [
                    {
                        "label": "清洁剂100ml*2+刷子*1",
                        "supplier_sku_id": "",
                        "image_url": "https://cbu01.alicdn.com/img/ibank/cleaning-kit.jpg",
                        "disabled": False,
                        "selected": False,
                        "option_index": 0,
                    }
                ],
            }
        ]
        supplier_product["sku_options"] = [
            {
                "supplier_sku_id": "visible-123456789012-cleaning-kit",
                "combination_key": "颜色分类>清洁剂100ml*2+刷子*1",
                "raw_label": "清洁剂100ml*2+刷子*1",
                "selected_options": {"颜色分类": "清洁剂100ml*2+刷子*1"},
                "set_quantity": 1,
                "set_composition": ["清洁剂100ml*2+刷子*1"],
                "price": {"currency": "CNY", "amount": "10.00"},
                "stock": {"status": "in_stock", "quantity": None},
                "image_urls": ["https://cbu01.alicdn.com/img/ibank/cleaning-kit.jpg"],
                "evidence_source": "dom_single_group_sku",
                "complete": True,
                "evidence": {
                    "group_names": ["颜色分类"],
                    "option_index": 0,
                    "native_supplier_sku_id": False,
                },
            }
        ]

        captured = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "supplier_product": supplier_product,
            },
        )
        stored = self.repo.load_supplier_collection_result(run_id)["supplier_products"][0]
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        option = review["data"]["items"][0]["supplier_sku_options"][0]
        selected = self.post_json(
            f"/api/batches/{run_id}/supplier-sku",
            {
                "seed_id": seed.seed_id,
                "supplier_sku_id": "visible-123456789012-cleaning-kit",
                "differences": [],
            },
        )

        self.assertTrue(captured["ok"])
        self.assertEqual("complete", stored["sku_matrix_status"])
        self.assertEqual(3, stored["sku_options"][0]["set_quantity"])
        self.assertEqual(3, option["set_quantity"])
        self.assertTrue(selected["ok"])

    def test_supplier_review_recovers_existing_page_unique_composite_candidate(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["sku_options"] = []
        supplier_product["sku_groups"] = [
            {
                "name": "颜色分类",
                "options": [
                    {
                        "label": "清洁剂100ml*2+刷子*1",
                        "supplier_sku_id": "",
                        "image_url": "https://cbu01.alicdn.com/img/ibank/cleaning-kit.jpg",
                        "disabled": False,
                        "selected": False,
                        "option_index": 0,
                    }
                ],
            }
        ]
        supplier_product["sku_option_candidates"] = [
            {
                "supplier_sku_id": "visible-123456789012-cleaning-kit",
                "combination_key": "颜色分类>清洁剂100ml*2+刷子*1",
                "raw_label": "清洁剂100ml*2+刷子*1",
                "selected_options": {"颜色分类": "清洁剂100ml*2+刷子*1"},
                "set_quantity": 1,
                "set_composition": ["清洁剂100ml*2+刷子*1"],
                "price": {"currency": "CNY", "amount": "10.00"},
                "stock": {"status": "in_stock", "quantity": None},
                "image_urls": ["https://cbu01.alicdn.com/img/ibank/cleaning-kit.jpg"],
                "evidence_source": "dom_single_group_sku",
                "complete": True,
                "evidence": {
                    "group_names": ["颜色分类"],
                    "option_index": 0,
                    "native_supplier_sku_id": False,
                },
                "validation_errors": ["set_composition quantity must match set_quantity"],
            }
        ]
        supplier_product["sku_matrix_status"] = "manual_confirmation_required"
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "network": {"mode": "direct", "proxy_disabled": True},
                "supplier_products": [supplier_product],
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        option = review["data"]["items"][0]["supplier_sku_options"][0]
        selected = self.post_json(
            f"/api/batches/{run_id}/supplier-sku",
            {
                "seed_id": seed.seed_id,
                "supplier_sku_id": "visible-123456789012-cleaning-kit",
                "differences": [],
            },
        )

        self.assertEqual("visible-123456789012-cleaning-kit", option["supplier_sku_id"])
        self.assertEqual(3, option["set_quantity"])
        self.assertTrue(selected["ok"])

    def test_supplier_review_does_not_treat_age_size_or_model_numbers_as_quantity(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["sku_options"] = []
        supplier_product["sku_groups"] = [
            {
                "name": "颜色",
                "options": [
                    {
                        "label": "黑色均码",
                        "supplier_sku_id": "",
                        "image_url": "https://cbu01.alicdn.com/img/ibank/black.jpg",
                        "disabled": False,
                        "selected": False,
                        "option_index": 0,
                    }
                ],
            }
        ]
        supplier_product["sku_option_candidates"] = [
            {
                "supplier_sku_id": "visible-123456789012-black",
                "combination_key": "颜色>黑色均码",
                "raw_label": "黑色均码",
                "selected_options": {"颜色": "黑色均码"},
                "set_quantity": 18,
                "set_composition": ["黑色均码", "适用年龄 12-18 个月", "型号 1020"],
                "price": {"currency": "CNY", "amount": ""},
                "stock": {"status": "in_stock", "quantity": None},
                "image_urls": ["https://cbu01.alicdn.com/img/ibank/black.jpg"],
                "evidence_source": "single_visible_sku_combination",
                "complete": False,
                "evidence": {"user_confirmation_required": True},
            }
        ]
        supplier_product["sku_matrix_status"] = "manual_confirmation_required"
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "network": {"mode": "direct", "proxy_disabled": True},
                "supplier_products": [supplier_product],
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        option = review["data"]["items"][0]["supplier_sku_options"][0]

        self.assertEqual(1, option["set_quantity"])
        self.assertTrue(option["evidence"]["ignored_unverified_candidate_quantity"])

    def test_supplier_selection_capture_recovers_skus_from_specification_matrix(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_product = self.supplier_product_payload(seed.seed_id)
        supplier_product["sku"] = {
            "selected_options": {"visible_sku_labels": ["单一 SKU（页面无可选规格）"]},
            "evidence": "no_visible_variant_selector",
            "evidence_source": "dom_option_labels",
            "complete": False,
        }
        supplier_product["sku_options"] = []
        supplier_product["sku_groups"] = []
        supplier_product["attributes"] = {
            "品牌": "梅芳",
            "刃口材质": "碳钢",
            "型号": "全长(mm)",
            "1018（6寸鸡眼钳带锁扣）": "135",
            "1020（9寸多功能三合一皮带打孔钳）": "210",
            "1022A（耐用升级款）": "300",
            "1022D（红柄打孔钳）": ".",
            "产品规格": "全长(mm)",
            "全长": "全部 135 210 300",
            "打孔直径": "全部 2.5 3.0 展开参数",
        }

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-selection/capture",
            {
                "channel_index": 0,
                "seed_id": seed.seed_id,
                "ozon_product_id": "ozon-1",
                "supplier_product": supplier_product,
            },
        )
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        item = review["data"]["items"][0]
        options = item["supplier_sku_options"]

        self.assertTrue(result["ok"])
        self.assertEqual(3, len(options))
        target = next(option for option in options if option["selected_options"]["型号"] == "1020")
        self.assertEqual(
            {"型号": "1020", "规格": "9寸多功能三合一皮带打孔钳", "全长": "210毫米"},
            target["selected_options"],
        )
        self.assertEqual("dom_specification_table", target["evidence_source"])
        self.assertTrue(target["complete"])
        decision = WorkbenchService(self.repo)._supplier_sku_decision(
            {"title": "Пробойник", "attributes": {"Длина, мм": "210"}},
            options,
        )
        self.assertEqual(target["supplier_sku_id"], decision["recommended_sku_id"])
        stored = self.repo.load_supplier_collection_result(run_id)["supplier_products"][0]
        self.assertNotIn("型号", stored["attributes"])
        self.assertNotIn("1020（9寸多功能三合一皮带打孔钳）", stored["attributes"])
        self.assertNotIn("1022D（红柄打孔钳）", stored["attributes"])
        self.assertNotIn("产品规格", stored["attributes"])
        self.assertNotIn("全长", stored["attributes"])

    def test_supplier_browser_result_is_ingested_and_waits_for_collection_review(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_url = "https://detail.1688.com/offer/123456789012.html"
        WorkbenchService(self.repo).save_supplier_review_links(
            run_id,
            [{"seed_id": seed.seed_id, "supplier_url": supplier_url}],
        )
        WorkbenchService(self.repo).start_supplier_collection(run_id)

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-collection-result",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "1688_user_verified_link",
                "network": {"mode": "direct", "proxy_disabled": True},
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "supplier_url": supplier_url,
                        "title": "Test supplier product",
                        "seller": {"shop_name": "Test supplier"},
                        "sku": {"selected_options": {"visible_sku_labels": ["黑色"]}},
                        "offer_id": "123456789012",
                        "sku_options": [
                            {
                                "supplier_sku_id": "sku-black-1",
                                "combination_key": "black-1",
                                "raw_label": "黑色 1 件",
                                "selected_options": {"颜色": "黑色", "数量": "1"},
                                "set_quantity": 1,
                                "set_composition": ["黑色", "1 件"],
                                "price": {"currency": "CNY", "amount": "12.80"},
                                "stock": {"status": "in_stock", "quantity": 100},
                                "image_urls": ["https://cbu01.alicdn.com/img/ibank/test.jpg"],
                                "evidence_source": "trusted_sku_map",
                                "complete": True,
                                "evidence": {"source": "trusted_sku_map"},
                            }
                        ],
                        "images": ["https://cbu01.alicdn.com/img/ibank/test.jpg"],
                        "price": {"currency": "CNY", "visible_text": "¥12.80"},
                        "attributes": {"颜色": "黑色"},
                        "domestic_shipping_evidence": {
                            "visible_text": "包邮",
                            "fee": 0,
                            "free_shipping_visible": True,
                        },
                    }
                ],
            },
        )

        self.assertTrue(result["ok"])
        self.assertTrue((self.repo.run_dir(run_id) / "supplier_collection_result.json").exists())
        self.wait_for_runner_idle(run_id)
        self.assertEqual(WorkbenchState.SUPPLIER_COLLECTED.value, self.repo.load_run(run_id)["status"])

    def test_supplier_collection_progress_is_persisted_and_returned_to_review_page(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_url = "https://detail.1688.com/offer/123456789012.html"
        service = WorkbenchService(self.repo)
        service.save_supplier_review_links(run_id, [{"seed_id": seed.seed_id, "supplier_url": supplier_url}])
        service.start_supplier_collection(run_id)

        progress = self.post_json(
            f"/api/batches/{run_id}/supplier-collection-progress",
            {
                "run_id": run_id,
                "seed_id": seed.seed_id,
                "supplier_url": supplier_url,
                "partial_product": {
                    "seed_id": seed.seed_id,
                    "supplier_url": supplier_url,
                    "title": "Partial product",
                    "seller": {"shop_name": "Partial supplier"},
                    "sku": {"selected_options": {"visible_sku_labels": ["黑色"]}},
                    "images": ["https://cbu01.alicdn.com/test.jpg"],
                    "price": None,
                    "domestic_shipping_evidence": {"visible_text": "包邮", "fee": 0},
                },
                "missing_fields": ["price"],
            },
        )
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")

        self.assertTrue(progress["ok"])
        self.assertTrue((self.repo.run_dir(run_id) / "supplier_collection_progress.json").exists())
        self.assertEqual(["price"], review["data"]["collection_progress"]["missing_fields"])
        self.assertEqual(
            "Partial supplier",
            review["data"]["collection_progress"]["partial_product"]["seller"]["shop_name"],
        )

    def test_extension_manifest_registers_1688_supplier_content_script(self) -> None:
        manifest_path = self.project_root / "browser_extension" / "ozon_v2_bridge" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("0.1.67", manifest["version"])

        supplier_scripts = [
            item
            for item in manifest["content_scripts"]
            if "supplier_content.js" in item.get("js", [])
        ]

        self.assertEqual(1, len(supplier_scripts))
        self.assertTrue(any("1688.com" in pattern for pattern in supplier_scripts[0]["matches"]))

    def test_ozon_content_script_reports_seed_and_resets_for_new_dispatch_token(self) -> None:
        content_path = self.project_root / "browser_extension" / "ozon_v2_bridge" / "content.js"
        content = content_path.read_text(encoding="utf-8")

        self.assertIn("seed_id: seedId", content)
        self.assertIn("state.dispatchToken === dispatchToken", content)
        self.assertIn("dispatchToken: task.data.dispatch_token", content)
        self.assertIn("dispatch_token: dispatchToken", content)
        self.assertIn("dispatch_token: state.dispatchToken", content)

    def test_runner_stop_cancels_current_browser_task(self) -> None:
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["zerkalo dlya makiyazha"],
            query_generation_status="generated",
        )
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [seed])
        contract = CollectionContractService(self.repo).build_attribute_template_contract(run_id).data
        self.repo.save_attribute_template_contract(run_id, contract)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        run["attribute_template_contract_ready"] = True
        self.repo.save_run(run)

        ready = self.get_json(f"/api/batches/{run_id}/browser-task")
        stopped = self.post_json(f"/api/batches/{run_id}/runner/stop", {})
        task = self.get_json(f"/api/batches/{run_id}/browser-task")
        events = self.get_json(f"/api/batches/{run_id}/events")["data"]["events"]
        event_types = [event["event_type"] for event in events]

        self.assertEqual("browser_task.attribute_template_ready", ready["code"])
        self.assertTrue(stopped["ok"])
        self.assertEqual("runner.stopped", stopped["code"])
        self.assertEqual("browser_task.none", task["code"])
        self.assertTrue(task["data"]["cancelled"])
        self.assertTrue(self.repo.load_run(run_id)["browser_task_cancelled"])
        self.assertIn("browser_task.cancelled", event_types)
        self.assertIn("runner.stopped", event_types)

    def test_active_browser_task_does_not_fall_back_to_older_pending_run(self) -> None:
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["zerkalo dlya makiyazha"],
            query_generation_status="generated",
        )
        older = self.repo.create_workbench_batch_record(target_count=1)
        older_run_id = older["run_id"]
        self.repo.save_sampled_seeds(older_run_id, [seed])
        older_contract = CollectionContractService(self.repo).build_attribute_template_contract(older_run_id).data
        self.repo.save_attribute_template_contract(older_run_id, older_contract)
        older["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        older["attribute_template_contract_ready"] = True
        self.repo.save_run(older)
        time.sleep(0.02)

        newer = self.repo.create_workbench_batch_record(target_count=1)
        newer_run_id = newer["run_id"]
        newer["browser_task_cancelled"] = True
        self.repo.save_run(newer)
        future_mtime = time.time() + 5
        os.utime(self.repo.run_dir(older_run_id), (future_mtime, future_mtime))

        task = self.get_json("/api/browser-task/active")

        self.assertEqual("browser_task.none", task["code"])
        self.assertEqual(newer_run_id, task["data"]["run_id"])
        self.assertTrue(task["data"]["cancelled"])

    def test_ozon_collection_ingest_completes_ozon_gate(self) -> None:
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["zerkalo dlya makiyazha"],
            query_generation_status="generated",
        )
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [seed])
        self.repo.save_attribute_template_result(run_id, self.seller_attribute_template_payload(run_id, seed.seed_id))
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        self.repo.save_run(run)

        result = self.post_json(
            f"/api/batches/{run_id}/ozon-collection",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test",
                "ozon_candidates": [self.ozon_candidate_payload(seed)],
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual("workbench.ozon_collection_ingested", result["code"])
        self.assertTrue((self.repo.run_dir(run_id) / "ozon_collection_result.json").exists())
        self.assertEqual(WorkbenchState.SUPPLIER_REVIEW.value, self.repo.load_run(run_id)["status"])
        self.assertTrue((self.repo.run_dir(run_id) / "supplier_review.json").exists())

    def test_supplier_review_page_and_api_show_ozon_evidence(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/supplier-review")
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        row = review["data"]["items"][0]

        self.assertIn("供应商审核 (Supplier Review)", page)
        self.assertIn('["supplier_review", "supplier_collecting"].includes(state.status)', page)
        self.assertIn("只重新打开未完成通道；已回传的 1688 商品保持完成", page)
        self.assertIn("1688 采集反馈 (Collection Feedback)", page)
        self.assertIn("collection_progress", page)
        self.assertIn("setInterval(poll, 1500)", page)
        self.assertNotIn('if (state.status === "supplier_collected" || state.status === "image_processing")', page)
        self.assertIn('id="approveCollection"', page)
        self.assertIn('id="ozonEvidence"', page)
        self.assertIn('id="supplierEvidence"', page)
        self.assertIn("const item = state.items[state.evidenceIndex]", page)
        self.assertIn('const skuLabels = ((product.sku || {}).selected_options || {}).visible_sku_labels || [];', page)
        self.assertIn('class="app-shell"', page)
        self.assertIn('class="sidebar"', page)
        self.assertIn('class="topbar"', page)
        self.assertIn('class="workspace"', page)
        self.assertIn("运营驾驶舱 (Operations Cockpit)", page)
        self.assertIn("重新启动 1688 采集", page)
        self.assertIn('.join("\\n");', page)
        self.assertNotIn('.join("\n");', page)
        self.assertIn('$("status").textContent = "加载失败 (Load Failed)";', page)
        self.assertIn('id="stageNavigation"', page)
        self.assertIn('id="batchOverviewNav"', page)
        self.assertIn('id="supplierReviewNav"', page)
        self.assertIn(f'href="/?run_id={run_id}"', page)
        self.assertIn(f'href="/batches/{run_id}/supplier-review"', page)
        self.assertIn('aria-current="page"', page)
        self.assertNotIn("图片处理 (Images)", page)
        self.assertIn("上传草稿 (Upload)", page)
        self.assertNotIn('window.setTimeout(() => window.location.assign("/"), 800);', page)
        self.assertEqual(seed.seed_id, row["seed_id"])
        self.assertEqual("Test product", row["ozon_title"])
        self.assertEqual("https://img.example/main.jpg", row["ozon_main_image"])
        self.assertEqual({"single_sku": "visible"}, row["selected_options"])
        self.assertEqual("https://www.ozon.ru/product/test-123/", row["ozon_url"])

    def test_supplier_review_poll_preserves_open_sku_details_and_current_choice(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/supplier-review")

        self.assertIn('const previousItemSeedId = optionsRoot.dataset.seedId || "";', page)
        self.assertIn('selectedSupplierSkuId: previousSelection ? previousSelection.value : ""', page)
        self.assertIn('const preservedInteraction = previousItemSeedId === String(item.seed_id || "")', page)
        self.assertIn('selectedSupplierSkuId: preservedInteraction.selectedSupplierSkuId || ""', page)
        self.assertIn('context.selectedSupplierSkuId === option.supplier_sku_id', page)
        self.assertIn('otherDetails.open = preservedInteraction.otherOptionsOpen === true;', page)
        self.assertIn("function isPageUniqueSupplierSku(item, options)", page)
        self.assertIn('id="lockSingleSupplierSku"', page)
        self.assertIn("function lockSingleSupplierSku()", page)
        self.assertIn("单一 SKU，直接锁定主体", page)
        self.assertIn("await submitSupplierSkuLock(item, option);", page)
        self.assertIn("source_image_urls:subjectUrls", page)
        self.assertIn("visible_subject_quantity:Number(option.set_quantity || 1)", page)
        self.assertNotIn('option.evidence_source !== "dom_single_group_sku"', page)
        self.assertNotIn("if (!groups.length) return false;", page)
        self.assertNotIn("return groups.every((group)", page)
        self.assertIn("serverDecision.single_option_confirmable === true", page)
        self.assertIn("serverDecision.single_option_supplier_sku_id", page)
        self.assertNotIn(
            "return Array.isArray(options) && options.length === 1;",
            page,
        )

    def test_supplier_review_poll_preserves_unsubmitted_subject_evidence_draft(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/supplier-review")

        self.assertIn("const subjectDrafts = new Map();", page)
        self.assertIn("function rememberSubjectDraft(seedId)", page)
        self.assertIn("sourceImageUrls: selectedSubjectEvidenceUrls()", page)
        self.assertIn("rememberSubjectDraft(previousSubjectSeedId);", page)
        self.assertIn('const subjectDraft = subjectDrafts.get(String(item.seed_id || "")) || null;', page)
        self.assertIn("subjectDraft ? subjectDraft.sourceImageUrls", page)
        self.assertIn("subjectDraft.visibleSubjectQuantity", page)
        self.assertIn('checkbox.addEventListener("change", () => {', page)

    def test_supplier_review_evidence_uses_single_item_pagers(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/supplier-review")

        self.assertIn('id="ozonEvidencePager"', page)
        self.assertIn('id="supplierEvidencePager"', page)
        self.assertIn('id="ozonEvidencePrev"', page)
        self.assertIn('id="ozonEvidenceNext"', page)
        self.assertIn('id="supplierEvidencePrev"', page)
        self.assertIn('id="supplierEvidenceNext"', page)
        self.assertIn("const item = state.items[state.evidenceIndex]", page)
        self.assertIn("const previousOzonScroll = ozonBody.scrollTop", page)
        self.assertIn("const previousSupplierScroll = supplierBody.scrollTop", page)
        self.assertIn("renderEvidence({ resetScroll: true })", page)
        self.assertIn("ozonBody.scrollTop = resetScroll ? 0 : previousOzonScroll", page)
        self.assertIn("supplierBody.scrollTop = resetScroll ? 0 : previousSupplierScroll", page)

    def test_collection_review_returns_full_evidence_and_requires_explicit_approval(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "network": {"mode": "direct", "proxy_disabled": True},
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                        "final_url": "https://detail.1688.com/offer/123456789012.html",
                        "offer_id": "123456789012",
                        "title": "Supplier product title",
                        "seller": {"shop_name": "Supplier factory"},
                        "sku": {"selected_options": {"visible_sku_labels": ["black"]}},
                        "sku_options": [
                            {
                                "supplier_sku_id": "sku-black-1",
                                "combination_key": "black-1",
                                "raw_label": "black 1 piece",
                                "selected_options": {"color": "black", "quantity": "1"},
                                "set_quantity": 1,
                                "set_composition": ["black", "1 piece"],
                                "price": {"currency": "CNY", "amount": "19.90"},
                                "stock": {"status": "in_stock", "quantity": 100},
                                "image_urls": ["https://cbu01.alicdn.com/img/ibank/product-main.jpg"],
                                "evidence_source": "trusted_sku_map",
                                "complete": True,
                                "evidence": {"source": "trusted_sku_map"},
                            }
                        ],
                        "images": ["https://cbu01.alicdn.com/img/ibank/product-main.jpg"],
                        "price": {"currency": "CNY", "visible_text": "19.90"},
                        "attributes": {"Material": "polyester"},
                        "domestic_shipping_evidence": {
                            "visible_text": "freight 3 CNY",
                            "fee": 3,
                            "free_shipping_visible": False,
                        },
                    }
                ],
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        row = review["data"]["items"][0]

        self.assertEqual("Office / Paper trays", row["ozon_product"]["category_path"])
        self.assertEqual("Test seller", row["ozon_product"]["seller_name"])
        self.assertEqual("Supplier product title", row["supplier_product"]["title"])
        self.assertEqual(3, row["supplier_product"]["domestic_shipping_evidence"]["fee"])
        self.assertEqual([], row["ozon_completeness"]["missing_fields"])
        self.assertEqual([], row["supplier_completeness"]["missing_fields"])
        self.assertTrue(review["data"]["can_approve"])

        selected = self.post_json(
            f"/api/batches/{run_id}/supplier-sku",
            {"seed_id": seed.seed_id, "supplier_sku_id": "sku-black-1", "differences": []},
        )

        approved = self.post_json(
            f"/api/batches/{run_id}/actions",
            {"action": WorkbenchAction.START_IMAGE_PROCESSING.value},
            ok=False,
        )

        self.assertTrue(selected["ok"])
        self.assertFalse(approved["ok"])
        self.assertEqual("subject_master.required", approved["code"])
        self.assertEqual(WorkbenchState.SUPPLIER_COLLECTED.value, self.repo.load_run(run_id)["status"])

    def test_collection_review_rejects_supplier_company_name_as_product_title(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        company_name = "Supplier Factory Limited"
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "network": {"mode": "direct", "proxy_disabled": True},
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                        "title": company_name,
                        "seller": {"shop_name": company_name},
                        "sku": {"selected_options": {"visible_sku_labels": ["black"]}},
                        "images": ["https://cbu01.alicdn.com/img/ibank/product-main.jpg"],
                        "price": {"currency": "CNY", "visible_text": "19.90"},
                        "attributes": {"Material": "polyester"},
                        "domestic_shipping_evidence": {"visible_text": "freight 3 CNY", "fee": 3},
                    }
                ],
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        review = self.get_json(f"/api/batches/{run_id}/supplier-review")

        self.assertIn("title", review["data"]["items"][0]["supplier_completeness"]["missing_fields"])
        self.assertFalse(review["data"]["can_approve"])

    def test_supplier_review_exposes_real_sku_lock_and_subject_master_routes(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        subject_url = "https://cbu01.alicdn.com/img/ibank/set-x4.jpg"
        gallery_url = "https://cbu01.alicdn.com/img/ibank/set-x4-detail.jpg"
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "network": {"proxy_disabled": True},
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
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
                                "image_urls": [subject_url],
                                "evidence_source": "trusted_sku_map",
                                "complete": True,
                                "evidence": {"source": "trusted_sku_map"},
                            }
                        ],
                        "images": [subject_url, gallery_url],
                        "price": "18.80",
                        "attributes": {"quantity": "4"},
                        "domestic_shipping_evidence": {"fee": "0"},
                    }
                ],
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        page = self.get_text(f"/batches/{run_id}/supplier-review")
        review = self.get_json(f"/api/batches/{run_id}/supplier-review")
        selected = self.post_json(
            f"/api/batches/{run_id}/supplier-sku",
            {
                "seed_id": seed.seed_id,
                "supplier_sku_id": "supplier-sku-set-x4",
                "differences": [{"field": "quantity", "ozon": "1", "supplier": "4"}],
            },
        )
        rejected_subject = self.post_json(
            f"/api/batches/{run_id}/subject-master",
            {
                "seed_id": seed.seed_id,
                "source_image_urls": [
                    subject_url,
                    "https://cbu01.alicdn.com/img/ibank/not-in-supplier.jpg",
                ],
                "visible_subject_quantity": 4,
            },
            ok=False,
        )
        rejected_gallery_subject = self.post_json(
            f"/api/batches/{run_id}/subject-master",
            {
                "seed_id": seed.seed_id,
                "source_image_urls": [subject_url, gallery_url],
                "visible_subject_quantity": 4,
            },
            ok=False,
        )
        reopened = self.post_json(
            f"/api/batches/{run_id}/supplier-sku/reopen",
            {"seed_id": seed.seed_id},
        )

        self.assertIn('id="skuDecisionPanel"', page)
        self.assertIn("锁定真实 SKU (Lock Real SKU)", page)
        self.assertIn("Ozon 原商品目标", page)
        self.assertIn("1688 可采购规格", page)
        self.assertIn("页面只有一个真实 SKU，无需选择规格", page)
        self.assertIn('id="lockSingleSupplierSku"', page)
        self.assertIn("单一 SKU，直接锁定主体", page)
        self.assertIn("确认所选 SKU", page)
        self.assertIn('id="reopenSupplierSku"', page)
        self.assertIn("重新选择 SKU", page)
        self.assertIn("系统没有找到可证明的唯一对应项", page)
        self.assertIn("function analyzeSupplierSkuOptions", page)
        self.assertIn('const strongMatches = matches.filter((token) => !token.startsWith("包装数量:"));', page)
        self.assertIn("score:serverCandidate ? Number(serverCandidate.score || 0) : strongMatches.length", page)
        self.assertIn("function skuPriceText", page)
        self.assertIn("function skuStockText", page)
        self.assertIn("Ozon 标题", page)
        self.assertIn("符合 Ozon 目标", page)
        self.assertIn("查看其他规格", page)
        self.assertIn("function renderSupplierSkuCandidate", page)
        self.assertIn("function renderSupplierSkuGroup", page)
        self.assertIn('image.className = "sku-option-image"', page)
        self.assertIn('image.referrerPolicy = "no-referrer"', page)
        self.assertIn('image.addEventListener("error"', page)
        self.assertIn("supplierFallbackImages", page)
        self.assertIn("matchingCandidates", page)
        self.assertIn("otherCandidates", page)
        self.assertIn("strongestScore", page)
        self.assertIn("价格 ${skuPriceText(option.price)} · ${skuStockText(option.stock)}", page)
        self.assertNotIn("价格 ${valueText(option.price)} · 库存 ${valueText(option.stock)}", page)
        self.assertIn('radio.addEventListener("change", updateSkuLockButton)', page)
        self.assertIn("主体证据图 (Subject Evidence)", page)
        self.assertIn("确认主体证据 (Confirm Subject Evidence)", page)
        self.assertIn('id="subjectEvidenceCount"', page)
        self.assertIn('checkbox.type = "checkbox"', page)
        self.assertIn("SKU 绑定图", page)
        self.assertIn("供应商商品图", page)
        self.assertIn(
            "const candidates = skuImages.length ? skuImages : supplierImages.slice(0, 1);",
            page,
        )
        self.assertIn("source_image_urls:selectedUrls", page)
        self.assertNotIn('id="whiteBackgroundConfirmed"', page)
        self.assertNotIn("White Background Confirmed", page)
        self.assertIn("/supplier-sku", page)
        self.assertIn("/supplier-sku/reopen", page)
        self.assertIn("/subject-master", page)
        self.assertEqual(1, len(review["data"]["items"][0]["supplier_sku_options"]))
        self.assertTrue(selected["ok"])
        self.assertEqual("supplier-sku-set-x4", selected["data"]["receipt"]["supplier_sku_id"])
        self.assertFalse(rejected_subject["ok"])
        self.assertEqual("subject_master.image_not_in_supplier", rejected_subject["code"])
        self.assertFalse(rejected_gallery_subject["ok"])
        self.assertEqual(
            "subject_master.image_not_in_locked_sku",
            rejected_gallery_subject["code"],
        )
        self.assertTrue(reopened["ok"])
        self.assertNotIn(
            seed.seed_id,
            self.repo.load_supplier_sku_selections(run_id)["selections"],
        )

    def test_upload_workspace_repairs_legacy_subject_and_pending_package_to_locked_sku_images(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        supplier_product = self.supplier_product_payload(seed.seed_id)
        selected_url = supplier_product["sku_options"][0]["image_urls"][0]
        wrong_variant_url = "https://cbu01.alicdn.com/img/ibank/wrong-variant.webp"
        supplier_product["images"] = [selected_url, wrong_variant_url]
        self.repo.save_supplier_collection_result(
            run_id,
            {"run_id": run_id, "supplier_products": [supplier_product]},
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        service = WorkbenchService(
            self.repo,
            seller_api_adapter=FakeSellerApiAdapter(),
        )
        sku = SupplierSkuOption.from_dict(supplier_product["sku_options"][0])
        ozon_product_id = str(
            self.repo.load_ozon_collection_result(run_id)["ozon_candidates"][0][
                "ozon_product_id"
            ]
        )
        receipt = SupplierSkuSelectionReceipt.confirmed(
            run_id=run_id,
            product_id=ozon_product_id,
            supplier_offer_id=supplier_product["offer_id"],
            supplier_sku=sku,
            ozon_target_sku={
                "sku_id": "ozon-sku-1",
                "selected_options": {"single_sku": "visible"},
            },
            differences=[],
            confirmed_at="2026-08-02T00:00:00+00:00",
        )
        self.repo.save_supplier_sku_selections(
            run_id,
            {
                "run_id": run_id,
                "selections": {seed.seed_id: receipt.to_dict()},
            },
        )
        selected_path = self.tmpdir / "selected-sku.jpg"
        selected_path.write_bytes(b"selected-sku")
        wrong_path = self.tmpdir / "wrong-variant.webp"
        wrong_path.write_bytes(b"wrong-variant")
        legacy_subject = SubjectMasterSelection.create(
            receipt=receipt,
            source_paths=[selected_path, wrong_path],
            source_image_urls=[selected_url, wrong_variant_url],
            visible_subject_quantity=1,
            confirmed_at="2026-08-02T00:00:00+00:00",
        )
        self.repo.save_subject_masters(
            run_id,
            {
                "run_id": run_id,
                "items": {
                    seed.seed_id: {
                        "subject_master": legacy_subject.to_dict(),
                        "image_task_mode": "post_upload_package",
                    }
                },
            },
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )
        preview = service.preview_product_upload(run_id, seed.seed_id)
        self.assertTrue(preview.ok, preview.to_dict())
        submitted = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )
        self.assertTrue(submitted.ok, submitted.to_dict())
        package_id = submitted.data["image_task_package_id"]
        package_path = (
            self.context.runtime_root
            / "image_tasks"
            / "pending"
            / f"{package_id}.json"
        )
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["evidence"]["subject_master"] = legacy_subject.to_dict()
        package["failure"] = {"reason": "legacy mixed-variant evidence"}
        package["failed_at"] = "2026-08-02T00:01:00+00:00"
        package["assignment"] = {"worker_id": "stale-worker"}
        package_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.repo.save_subject_masters(
            run_id,
            {
                "run_id": run_id,
                "items": {
                    seed.seed_id: {
                        "subject_master": legacy_subject.to_dict(),
                        "image_task_mode": "post_upload_package",
                    }
                },
            },
        )

        workspace = service.upload_workspace(run_id)

        self.assertTrue(workspace.ok, workspace.to_dict())
        repaired_subject = self.repo.load_subject_masters(run_id)["items"][
            seed.seed_id
        ]["subject_master"]
        repaired_package = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertEqual([selected_url], repaired_subject["source_image_urls"])
        self.assertEqual(
            [selected_url],
            repaired_package["evidence"]["subject_master"]["source_image_urls"],
        )
        self.assertEqual(
            selected_url,
            repaired_package["bootstrap_image"]["url"],
        )
        self.assertNotIn("failure", repaired_package)
        self.assertNotIn("failed_at", repaired_package)
        self.assertNotIn("assignment", repaired_package)

    def test_image_workspace_page_and_api_show_real_source_assets(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "network": {"proxy_disabled": True},
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "supplier_url": "https://detail.1688.com/offer/123.html",
                        "title": "Supplier product",
                        "seller": "Supplier seller",
                        "sku": {"single_sku": "visible"},
                        "images": [
                            "https://img.alicdn.com/imgextra/icon-55-tps-16-16.svg",
                            "https://cbu01.alicdn.com/img/ibank/product-a_!!111-0-cib.jpg_.webp",
                            "https://cbu01.alicdn.com/img/ibank/product-b_!!111-0-cib.jpg_.webp",
                            "https://cbu01.alicdn.com/img/ibank/recommend-a_!!222-0-cib.jpg_.webp",
                            "https://cbu01.alicdn.com/img/ibank/product-a_!!111-0-cib.jpg_sum.jpg",
                        ],
                        "price": "19.90",
                        "domestic_shipping_evidence": {"fee": "0", "free_shipping": True},
                    }
                ],
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.IMAGE_PROCESSING.value
        self.repo.save_run(run)

        page = self.get_text(f"/batches/{run_id}/images")
        workspace = self.get_json(f"/api/batches/{run_id}/images")
        item = workspace["data"]["items"][0]

        self.assertIn("上传草稿 (Upload)", page)
        self.assertNotIn("图片处理 (Images)", page)
        self.assertNotIn('id="imageProcessingNav"', page)
        self.assertNotIn('id="imageItems"', page)
        self.assertIn("单原图", page)
        self.assertEqual(["https://img.example/main.jpg"], item["ozon_reference_images"])
        self.assertEqual(
            [
                "https://cbu01.alicdn.com/img/ibank/product-a_!!111-0-cib.jpg_.webp",
                "https://cbu01.alicdn.com/img/ibank/product-b_!!111-0-cib.jpg_.webp",
            ],
            item["supplier_source_images"],
        )
        self.assertEqual("waiting_for_supplier_sku", item["generation_status"])
        queue_summary = workspace["data"]["image_queue_summary"]
        self.assertEqual(1, queue_summary["total_products"])
        self.assertEqual(1, queue_summary["waiting_for_supplier_sku"])
        self.assertEqual(0, queue_summary["manual_review_required"])
        self.assertEqual(0, queue_summary["pending"])
        self.assertFalse(workspace["data"]["image_gate"]["ready"])
        self.assertEqual("supplier_sku_selection_required", workspace["data"]["image_gate"]["code"])

    def test_image_workspace_final_gallery_opens_original_slot_files(self) -> None:
        run_id, job_id, queue = self.prepare_reviewable_image_job()
        slot = queue.list_slots(job_id)[0]
        original_path = self.context.runtime_root / "image_generation" / "test_outputs" / f"{slot['slot_id']}.png"
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(f"accepted-{slot['slot_id']}".encode("utf-8"))
        with queue._connect() as connection:
            connection.execute(
                "UPDATE image_slots SET accepted_path = ? WHERE job_id = ? AND slot_id = ?",
                (str(original_path.resolve()), job_id, slot["slot_id"]),
            )

        page = self.get_text(f"/batches/{run_id}/images")
        with urlopen(
            f"{self.base_url}/api/batches/{run_id}/image-job/{job_id}/slot/{slot['slot_id']}/file",
            timeout=5,
        ) as response:
            original_bytes = response.read()

        self.assertIn("上传草稿 (Upload)", page)
        self.assertNotIn("最终成图总览 (Final Gallery)", page)
        self.assertEqual(f"accepted-{slot['slot_id']}".encode("utf-8"), original_bytes)

    def test_image_workspace_carries_ordered_references_and_slot_mapping_receipts(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        job = self.attach_completed_image_job(run_id, seed.seed_id)
        queue = ImageGenerationQueue(
            self.context.runtime_root / "image_generation" / "ozon_image_jobs.sqlite3"
        )
        first_slot = queue.list_slots(job["job_id"])[0]
        with queue._connect() as connection:
            connection.execute(
                "UPDATE image_slots SET receipt_json = ? "
                "WHERE job_id = ? AND slot_id = ?",
                (
                    json.dumps(
                        {
                            "validation": {
                                "reference_mapping_version": "ozon-reference-map-v1",
                                "primary_ozon_reference_sha256": "a" * 64,
                                "reference_slot_index": 1,
                                "reference_reused": False,
                                "reference_composition_followed": True,
                                "locked_subject_preserved": True,
                            }
                        }
                    ),
                    job["job_id"],
                    first_slot["slot_id"],
                ),
            )

        workspace = self.get_json(f"/api/batches/{run_id}/images")["data"]
        item = workspace["items"][0]
        mapping = item["image_job"]["slots"][0]["ozon_reference_mapping"]
        page = self.get_text(f"/batches/{run_id}/images")

        self.assertEqual(
            [
                {"reference_slot_index": 1, "url": item["ozon_reference_images"][0]}
            ],
            item["ozon_reference_inputs"],
        )
        self.assertEqual(1, mapping["reference_slot_index"])
        self.assertEqual(item["ozon_reference_images"][0], mapping["reference_url"])
        self.assertEqual("a" * 64, mapping["primary_ozon_reference_sha256"])
        self.assertIn("上传草稿 (Upload)", page)
        self.assertNotIn("对应 Ozon 参考图", page)

    def test_image_repair_api_requeues_only_selected_slots_and_records_event(self) -> None:
        run_id, job_id, queue = self.prepare_reviewable_image_job()

        result = self.post_json(
            f"/api/batches/{run_id}/image-job/{job_id}/repair",
            {
                "repairs": [
                    {
                        "slot_id": "detail_02",
                        "issue_code": "composition",
                        "note": "主体被遮挡",
                    }
                ]
            },
        )

        self.assertEqual("image_job.repair_requested", result["code"])
        image_job = result["data"]["image_job"]
        self.assertEqual("pending", image_job["status"])
        slots = {slot["slot_id"]: slot for slot in image_job["slots"]}
        self.assertEqual("repair_pending", slots["detail_02"]["status"])
        self.assertEqual("composition", slots["detail_02"]["review_issue_code"])
        self.assertEqual("accepted", slots["detail_03"]["status"])
        self.assertEqual("pending", queue.get_job(job_id)["status"])
        event = self.repo.load_run_events(run_id)[-1]
        self.assertEqual("image_job.repair_requested", event.event_type)
        self.assertEqual(["detail_02"], event.data["slot_ids"])

    def test_image_approval_api_completes_a_verified_review_job_and_records_event(self) -> None:
        run_id, job_id, queue = self.prepare_reviewable_image_job()

        def approve(active_queue: ImageGenerationQueue, active_job_id: str) -> dict:
            with active_queue._connect() as connection:
                connection.execute(
                    "UPDATE image_jobs SET status = 'completed' WHERE job_id = ?",
                    (active_job_id,),
                )
            return active_queue.get_job(active_job_id)

        with patch.object(
            ImageGenerationQueue,
            "approve_review",
            autospec=True,
            side_effect=approve,
        ):
            result = self.post_json(
                f"/api/batches/{run_id}/image-job/{job_id}/approve", {}
            )

        self.assertEqual("image_job.approved", result["code"])
        self.assertEqual("completed", result["data"]["image_job"]["status"])
        event = self.repo.load_run_events(run_id)[-1]
        self.assertEqual("image_job.approved", event.event_type)
        self.assertEqual(8, event.data["accepted_image_count"])

    def test_image_stop_api_persists_user_visible_reason_and_event(self) -> None:
        run_id, job_id, queue = self.prepare_reviewable_image_job()

        result = self.post_json(
            f"/api/batches/{run_id}/image-job/{job_id}/stop",
            {},
        )

        self.assertEqual("image_job.stopped", result["code"])
        image_job = result["data"]["image_job"]
        self.assertEqual("stopped", image_job["status"])
        self.assertEqual("用户在工具台手动停止生图", image_job["stop_reason"])
        self.assertEqual("workbench_user", image_job["stopped_by"])
        self.assertIsNotNone(image_job["stopped_at"])
        self.assertEqual(image_job["stop_reason"], queue.get_job(job_id)["stop_reason"])
        event = self.repo.load_run_events(run_id)[-1]
        self.assertEqual("image_job.stopped", event.event_type)
        self.assertEqual("用户在工具台手动停止生图", event.data["stop_reason"])

    def test_image_repair_api_rejects_wrong_batch_and_invalid_feedback_atomically(self) -> None:
        run_id, job_id, queue = self.prepare_reviewable_image_job()
        other_run_id = self.repo.create_workbench_batch_record(target_count=1)["run_id"]
        before = queue.snapshot(job_id)

        wrong_batch = self.post_json(
            f"/api/batches/{other_run_id}/image-job/{job_id}/repair",
            {
                "repairs": [
                    {"slot_id": "main_01", "issue_code": "composition", "note": ""}
                ]
            },
            ok=False,
        )
        invalid_feedback = self.post_json(
            f"/api/batches/{run_id}/image-job/{job_id}/repair",
            {
                "repairs": [
                    {"slot_id": "main_01", "issue_code": "other", "note": "   "}
                ]
            },
            ok=False,
        )

        self.assertEqual("image_job.not_found", wrong_batch["code"])
        self.assertEqual("image_job.repair_feedback_invalid", invalid_feedback["code"])
        self.assertEqual(before, queue.snapshot(job_id))

    def test_upload_workspace_page_and_api_show_prefill_plan_and_locked_gate(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/upload")
        workspace = self.get_json(f"/api/batches/{run_id}/upload")
        item = workspace["data"]["items"][0]

        self.assertIn("上传草稿 (Upload)", page)
        self.assertIn('class="app-shell"', page)
        self.assertIn('class="sidebar"', page)
        self.assertIn('class="topbar"', page)
        self.assertIn('class="workspace"', page)
        self.assertIn('id="uploadDraftNav"', page)
        self.assertIn('id="draftItems"', page)
        self.assertIn('id="uploadGate"', page)
        self.assertNotIn('id="buildDraft"', page)
        self.assertIn('id="pricingWorkspace"', page)
        self.assertIn('id="pricingProductList"', page)
        self.assertIn('id="pricingEditor"', page)
        self.assertIn('id="pricingProgress"', page)
        self.assertIn("价格与包装证据", page)
        self.assertIn("打开 1688 商品页", page)
        self.assertIn("采购价（人工确认）", page)
        self.assertIn("目标净利润率", page)
        self.assertIn("确认并写入本件价格与包装证据", page)
        self.assertIn("/pricing-evidence/preview", page)
        self.assertIn("/pricing-evidence/confirm", page)
        self.assertIn("pricingState", page)
        self.assertIn("发布锁已开启 (Publish Lock Active)", page)
        self.assertIn("逐商品上传门禁", page)
        self.assertIn("合格商品不等待整批", page)
        self.assertIn("类目模板自动映射结果", page)
        self.assertIn("上传基础字段（价格与包装物流）", page)
        self.assertIn("item.upload_core_fields", page)
        self.assertIn(
            "已由用户确认；构建草稿时自动写入。包装字段不会冒充商品净尺寸字段",
            page,
        )
        self.assertIn("查看全部模板字段（只读）", page)
        self.assertIn("item.manual_required_fields", page)
        self.assertIn("item.skill_pending_required_fields", page)
        self.assertIn("无需用户填写", page)
        self.assertNotIn(
            "const fields = (item.missing_required_fields || [])",
            page,
        )
        self.assertIn("details.open = false", page)
        self.assertIn("待原创", page)
        self.assertIn("缺少事实", page)
        self.assertIn("未提供可选素材", page)
        self.assertIn("required_attributes", page)
        self.assertIn("ready_to_build_count", page)
        self.assertIn("item.blocking_gates", page)
        self.assertIn("预览单原图建品", page)
        self.assertIn("确认上传到 Ozon", page)
        self.assertIn("提交后立即输出独立生图任务包", page)
        self.assertIn("建品成功后再绑定上传", page)
        self.assertNotIn('href="/images"', page)
        self.assertEqual(seed.seed_id, item["seed_id"])
        self.assertEqual(1, item["required_attribute_count"])
        self.assertEqual(1, item["prefill_plan_count"])
        self.assertFalse(workspace["data"]["gates"]["images_ready"])
        self.assertFalse(workspace["data"]["gates"]["draft_ready"])
        self.assertTrue(workspace["data"]["gates"]["publish_locked"])

    def test_upload_pricing_product_list_uses_fixed_five_item_pages(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/upload")

        self.assertIn('id="pricingPager"', page)
        self.assertIn('id="pricingPrevPage"', page)
        self.assertIn('id="pricingPageLabel"', page)
        self.assertIn('id="pricingNextPage"', page)
        self.assertIn("pageSize:5", page)
        self.assertIn(
            "slice(pageStart, pageStart + pricingState.pageSize)",
            page,
        )
        self.assertIn(
            "Math.ceil(pricingState.items.length / pricingState.pageSize)",
            page,
        )
        self.assertIn("每页 5 件", page)
        self.assertIn(".pricing-product-column { height:470px;", page)

    def test_pricing_prefill_uses_locked_supplier_cost_shipping_and_package_facts(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        supplier_result = self.repo.load_supplier_collection_result(run_id)
        supplier_product = supplier_result["supplier_products"][0]
        supplier_product["attributes"] = {
            "包装重量": "380克",
            "包装尺寸": "28 × 11 × 2.5 厘米",
        }
        supplier_product["domestic_shipping_evidence"] = {
            "visible_text": "运费 ¥7",
            "fee": "7",
            "free_shipping_visible": False,
        }
        self.repo.save_supplier_collection_result(run_id, supplier_result)

        item = self.get_json(f"/api/batches/{run_id}/upload")["data"]["items"][0]
        prefill = item["pricing_prefill"]

        self.assertEqual(
            {
                "purchase_price_cny": "12.80",
                "domestic_shipping_cny": "7",
                "package_weight_g": "380",
                "package_length_cm": "28",
                "package_width_cm": "11",
                "package_height_cm": "2.5",
            },
            prefill["values"],
        )
        self.assertEqual(
            "locked_supplier_sku.price",
            prefill["fields"]["purchase_price_cny"]["source"],
        )
        self.assertEqual(
            "supplier.domestic_shipping_evidence",
            prefill["fields"]["domestic_shipping_cny"]["source"],
        )
        self.assertEqual(
            "supplier.attributes.package_weight",
            prefill["fields"]["package_weight_g"]["source"],
        )
        self.assertEqual(
            "supplier.attributes.package_dimensions",
            prefill["fields"]["package_length_cm"]["source"],
        )

    def test_pricing_prefill_falls_back_to_ozon_package_attributes(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        supplier_result = self.repo.load_supplier_collection_result(run_id)
        supplier_product = supplier_result["supplier_products"][0]
        supplier_product["attributes"] = {}
        supplier_product["domestic_shipping_evidence"] = {
            "visible_text": "包邮",
            "fee": 0,
            "free_shipping_visible": True,
        }
        self.repo.save_supplier_collection_result(run_id, supplier_result)
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"].update(
            {
                "Вес с упаковкой, г": "380",
                "Размер упаковки (Длина х Ширина х Высота), мм": (
                    "280 × 110 × 25"
                ),
            }
        )
        self.repo.save_ozon_collection_result(run_id, ozon_result)

        item = self.get_json(f"/api/batches/{run_id}/upload")["data"]["items"][0]
        prefill = item["pricing_prefill"]

        self.assertEqual("0", prefill["values"]["domestic_shipping_cny"])
        self.assertEqual("380", prefill["values"]["package_weight_g"])
        self.assertEqual("28", prefill["values"]["package_length_cm"])
        self.assertEqual("11", prefill["values"]["package_width_cm"])
        self.assertEqual("2.5", prefill["values"]["package_height_cm"])
        self.assertEqual(
            "ozon.attributes.package_weight",
            prefill["fields"]["package_weight_g"]["source"],
        )
        self.assertEqual(
            "ozon.attributes.package_dimensions",
            prefill["fields"]["package_height_cm"]["source"],
        )

    def test_pricing_prefill_does_not_treat_net_size_or_unknown_shipping_as_package_fact(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        supplier_result = self.repo.load_supplier_collection_result(run_id)
        supplier_product = supplier_result["supplier_products"][0]
        supplier_product["attributes"] = {
            "产品重量": "380克",
            "产品尺寸": "28 × 11 × 2.5 厘米",
        }
        supplier_product["domestic_shipping_evidence"] = {
            "visible_text": "现付，预计明天达",
            "fee": None,
            "free_shipping_visible": False,
        }
        self.repo.save_supplier_collection_result(run_id, supplier_result)

        item = self.get_json(f"/api/batches/{run_id}/upload")["data"]["items"][0]

        self.assertEqual(
            {"purchase_price_cny": "12.80"},
            item["pricing_prefill"]["values"],
        )

    def test_pricing_editor_uses_evidence_prefill_without_overriding_saved_values(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)

        page = self.get_text(f"/batches/{run_id}/upload")

        self.assertIn("item.pricing_prefill && item.pricing_prefill.values", page)
        self.assertIn(
            'const draft = { target_margin_rate:"0.20", ...prefilled, ...saved };',
            page,
        )
        self.assertIn("该项已按", page)
        self.assertIn("预填，仍需用户确认", page)

    def test_upload_mapping_results_use_fixed_five_item_pages(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/upload")

        self.assertIn('id="draftPager"', page)
        self.assertIn('id="draftPrevPage"', page)
        self.assertIn('id="draftPageLabel"', page)
        self.assertIn('id="draftNextPage"', page)
        self.assertIn(
            "const draftState = { page:0, pageSize:5, items:[] }",
            page,
        )
        self.assertIn(
            "slice(pageStart, pageStart + draftState.pageSize)",
            page,
        )
        self.assertIn(
            "Math.ceil(draftState.items.length / draftState.pageSize)",
            page,
        )
        self.assertIn("第 1 / 1 页 · 每页 5 件", page)
        self.assertIn("changeDraftPage(-1)", page)
        self.assertIn("changeDraftPage(1)", page)

    def test_pricing_preview_is_side_effect_free_and_confirm_persists_one_product(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        pricing_path = self.repo.run_dir(run_id) / "pricing_evidence.json"
        template = self.repo.load_attribute_template_result(run_id)
        template["seed_templates"][0]["upload_attribute_schema"].extend(
            [
                {
                    "attribute_id": "package-weight",
                    "attribute_label": "Вес с упаковкой, г",
                    "is_required": False,
                },
                {
                    "attribute_id": "package-length",
                    "attribute_label": "Длина упаковки, см",
                    "is_required": False,
                },
                {
                    "attribute_id": "package-width",
                    "attribute_label": "Ширина упаковки, см",
                    "is_required": False,
                },
                {
                    "attribute_id": "package-height",
                    "attribute_label": "Высота упаковки, см",
                    "is_required": False,
                },
            ]
        )
        self.repo.save_attribute_template_result(run_id, template)

        preview = self.post_json(
            f"/api/batches/{run_id}/pricing-evidence/preview",
            self.pricing_input_payload(seed.seed_id),
        )["data"]

        self.assertFalse(pricing_path.exists())
        self.assertEqual("preview", preview["status"])
        self.assertEqual(
            "https://detail.1688.com/offer/123456789012.html",
            preview["supplier_url"],
        )
        self.assertEqual(
            {"currency": "CNY", "amount": "12.80"},
            preview["supplier_reference_price"],
        )
        self.assertEqual("55.90", preview["calculation"]["listing_price_cny"])
        self.assertEqual("671", preview["calculation"]["listing_price_rub"])

        confirmed = self.post_json(
            f"/api/batches/{run_id}/pricing-evidence/confirm",
            self.pricing_input_payload(seed.seed_id),
        )["data"]

        self.assertTrue(pricing_path.exists())
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual("workbench_user", confirmed["confirmed_by"])
        stored = self.repo.load_pricing_evidence(run_id)
        self.assertEqual(
            confirmed["calculation"],
            stored["items"][seed.seed_id]["calculation"],
        )

        workspace = self.get_json(f"/api/batches/{run_id}/upload")["data"]
        item = workspace["items"][0]
        self.assertTrue(item["pricing_ready"])
        self.assertEqual("confirmed", item["pricing_status"])
        self.assertNotIn("pricing", item["blocking_gates"])
        self.assertEqual(1, workspace["gates"]["pricing_ready_count"])
        mapped = {
            field["field_key"]: field
            for field in item["attribute_mapping"]
        }
        self.assertEqual("380", mapped["package-weight"]["value"])
        self.assertEqual(
            "user_confirmed_pricing_evidence",
            mapped["package-weight"]["source"],
        )
        self.assertEqual("28", mapped["package-length"]["value"])
        self.assertEqual("11", mapped["package-width"]["value"])
        self.assertEqual("2.5", mapped["package-height"]["value"])

    def test_confirmed_pricing_evidence_populates_upload_core_fields(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        self.post_json(
            f"/api/batches/{run_id}/pricing-evidence/confirm",
            self.pricing_input_payload(seed.seed_id),
        )

        item = self.get_json(f"/api/batches/{run_id}/upload")["data"]["items"][0]

        self.assertEqual(
            {
                "price": "671",
                "old_price": "839",
                "currency_code": "RUB",
                "depth": "280",
                "width": "110",
                "height": "25",
                "dimension_unit": "mm",
                "weight": "380",
                "weight_unit": "g",
            },
            item["upload_core_fields"],
        )

    def test_decimal_package_dimensions_are_rounded_up_to_int32_millimeters(
        self,
    ) -> None:
        core = _pricing_upload_core_fields(
            {
                "inputs": {
                    "package_length_cm": "2.02",
                    "package_width_cm": "2.01",
                    "package_height_cm": "1.5",
                    "package_weight_g": "300",
                },
                "calculation": {
                    "listing_price_rub": "383",
                    "old_price_rub": "479",
                },
            }
        )

        self.assertIsNotNone(core)
        self.assertEqual("21", core["depth"])
        self.assertEqual("21", core["width"])
        self.assertEqual("15", core["height"])
        seller_item = _seller_api_import_item(
            {
                "upload_core_fields": core,
                "description_category_id": 17028745,
                "type_id": 92767,
                "source_title": "Plant clips",
                "seed_id": "seed-plant-clips",
                "attributes": [],
            },
            ["https://example.test/plant-clips.jpg"],
        )
        self.assertEqual(21, seller_item["depth"])
        self.assertEqual(21, seller_item["width"])
        self.assertEqual(15, seller_item["height"])
        self.assertIsInstance(seller_item["depth"], int)

    def test_pricing_confirmation_rejects_impossible_package_density(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        payload = self.pricing_input_payload(seed.seed_id)
        payload.update(
            {
                "package_weight_g": "300",
                "package_length_cm": "2.02",
                "package_width_cm": "2.01",
                "package_height_cm": "1.5",
            }
        )

        result = WorkbenchService(self.repo).confirm_pricing_evidence(
            run_id,
            payload,
        )

        self.assertFalse(result.ok)
        self.assertEqual("pricing_evidence.calculation_invalid", result.code)
        self.assertIn("package density", " ".join(result.errors))

    def test_multiword_hashtags_are_normalized_to_valid_ozon_tokens(self) -> None:
        value = _dictionary_upload_value(
            {"category_path": "Дом и сад / Садовый декор"},
            {
                "field_key": "hashtags",
                "label": "#Хештеги",
                "value": (
                    "#растения #подвязка растений "
                    "#держатели для цветов #садовый декор"
                ),
            },
        )

        self.assertEqual(
            (
                "#растения #подвязка_растений "
                "#держатели_для_цветов #садовый_декор"
            ),
            value,
        )

    def test_hashtag_validator_rejects_internal_spaces(self) -> None:
        self.assertFalse(
            _valid_ozon_hashtags(
                "#растения #подвязка растений #садовый декор",
                minimum_count=3,
            )
        )
        self.assertTrue(
            _valid_ozon_hashtags(
                "#растения #подвязка_растений #садовый_декор",
                minimum_count=3,
            )
        )

    def test_saved_confirmed_pricing_is_revalidated_before_upload(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        service = WorkbenchService(self.repo)
        confirmed = service.confirm_pricing_evidence(
            run_id,
            self.pricing_input_payload(seed.seed_id),
        )
        self.assertTrue(confirmed.ok, confirmed.to_dict())
        stored = self.repo.load_pricing_evidence(run_id)
        stored["items"][seed.seed_id]["inputs"].update(
            {
                "package_weight_g": "300",
                "package_length_cm": "2.02",
                "package_width_cm": "2.01",
                "package_height_cm": "1.5",
            }
        )
        self.repo.save_pricing_evidence(run_id, stored)

        workspace = service.upload_workspace(run_id)

        self.assertTrue(workspace.ok, workspace.to_dict())
        item = workspace.data["items"][0]
        self.assertFalse(item["pricing_ready"])
        self.assertEqual("invalid", item["pricing_status"])
        self.assertIn("pricing", item["blocking_gates"])
        self.assertIn(
            "package density",
            " ".join(item["pricing_validation_errors"]),
        )
        self.assertIsNone(item["upload_preview"])

    def test_submit_revalidates_saved_preview_before_calling_seller_api(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(self.repo, seller_api_adapter=adapter)
        confirmed = service.confirm_pricing_evidence(
            run_id,
            self.pricing_input_payload(seed.seed_id),
        )
        self.assertTrue(confirmed.ok, confirmed.to_dict())
        preview = service.preview_product_upload(run_id, seed.seed_id)
        self.assertTrue(preview.ok, preview.to_dict())

        stored = self.repo.load_pricing_evidence(run_id)
        stored["items"][seed.seed_id]["inputs"].update(
            {
                "package_weight_g": "300",
                "package_length_cm": "2.02",
                "package_width_cm": "2.01",
                "package_height_cm": "1.5",
            }
        )
        self.repo.save_pricing_evidence(run_id, stored)

        workspace = service.upload_workspace(run_id)
        self.assertTrue(workspace.ok, workspace.to_dict())
        self.assertIsNone(workspace.data["items"][0]["upload_preview"])

        submitted = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )

        self.assertFalse(submitted.ok)
        self.assertEqual("product_upload.product_not_ready", submitted.code)
        self.assertEqual([], adapter.imported_items)
        self.assertEqual(
            [],
            list(
                (self.context.runtime_root / "image_tasks" / "pending").glob(
                    "*.json"
                )
            ),
        )

    def test_submit_requires_confirmation_again_when_valid_payload_changed(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(self.repo, seller_api_adapter=adapter)
        confirmed = service.confirm_pricing_evidence(
            run_id,
            self.pricing_input_payload(seed.seed_id),
        )
        self.assertTrue(confirmed.ok, confirmed.to_dict())
        preview = service.preview_product_upload(run_id, seed.seed_id)
        self.assertTrue(preview.ok, preview.to_dict())

        stored = self.repo.load_pricing_evidence(run_id)
        stored["items"][seed.seed_id]["inputs"]["package_length_cm"] = "29"
        self.repo.save_pricing_evidence(run_id, stored)

        submitted = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )

        self.assertFalse(submitted.ok)
        self.assertEqual("product_upload.confirmation_mismatch", submitted.code)
        self.assertNotEqual(
            preview.data["confirmation_token"],
            submitted.data["confirmation_token"],
        )
        self.assertEqual([], adapter.imported_items)

    def test_zero_hazard_class_uses_the_official_ozon_dictionary_label(self) -> None:
        value = _dictionary_upload_value(
            {"category_path": "Бытовая химия / Чистящее средство"},
            {
                "field_key": "9782",
                "label": "Класс опасности товара",
                "value": "0",
            },
        )

        self.assertEqual("Не опасен", value)

    def test_imported_item_with_error_level_is_not_accepted_for_image_tasks(
        self,
    ) -> None:
        status = _product_import_status(
            {
                "items": [
                    {
                        "status": "imported",
                        "product_id": 5762820950,
                        "errors": [
                            {
                                "code": "INCORRECT_DENSITY",
                                "level": "error",
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual("failed", status)

    def test_imported_item_with_warning_only_remains_accepted(self) -> None:
        status = _product_import_status(
            {
                "items": [
                    {
                        "status": "imported",
                        "product_id": 5762846829,
                        "errors": [
                            {
                                "code": "BR_hashtag_validation",
                                "level": "warning",
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual("accepted_by_ozon", status)

    def test_ozon_enum_error_level_is_blocking(self) -> None:
        status = _product_import_status(
            {
                "items": [
                    {
                        "status": "imported",
                        "product_id": 5763454848,
                        "errors": [
                            {
                                "code": "INCORRECT_DENSITY",
                                "level": "ERROR_LEVEL_ERROR",
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual("failed", status)

    def test_pricing_confirmation_requires_locked_supplier_sku(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()

        result = self.post_json(
            f"/api/batches/{run_id}/pricing-evidence/confirm",
            self.pricing_input_payload(seed.seed_id),
            ok=False,
        )

        self.assertEqual("pricing_evidence.supplier_sku_required", result["code"])
        self.assertFalse(
            (self.repo.run_dir(run_id) / "pricing_evidence.json").exists()
        )

    def test_pricing_policy_change_marks_confirmed_evidence_stale(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.prepare_pricing_sources(run_id, seed.seed_id)
        self.post_json(
            f"/api/batches/{run_id}/pricing-evidence/confirm",
            self.pricing_input_payload(seed.seed_id),
        )
        settings = self.repo.load_pricing_settings()
        settings["commission_rate"] = "0.16"
        self.repo.save_pricing_settings(settings)

        workspace = self.get_json(f"/api/batches/{run_id}/upload")["data"]
        item = workspace["items"][0]

        self.assertFalse(item["pricing_ready"])
        self.assertEqual("stale", item["pricing_status"])
        self.assertIn("pricing", item["blocking_gates"])

    def test_upload_workspace_reads_generated_and_user_approved_image_jobs(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        sku = SupplierSkuOption(
            supplier_sku_id="approved-sku",
            combination_key="black>single",
            raw_label="black single",
            selected_options={"颜色": "黑色"},
            set_quantity=1,
            set_composition=["1 件"],
            price={"currency": "CNY", "amount": "12.80"},
            stock={"status": "in_stock", "quantity": 10},
            image_urls=["https://cbu01.alicdn.com/img/ibank/approved.jpg"],
            evidence_source="trusted_sku_map",
            complete=True,
        )
        receipt = SupplierSkuSelectionReceipt.confirmed(
            run_id=run_id,
            product_id=seed.seed_id,
            supplier_offer_id="approved-offer",
            supplier_sku=sku,
            ozon_target_sku={"sku_id": "ozon-approved", "selected_options": {}},
            differences=[],
            confirmed_at="2026-07-22T00:00:00+00:00",
        )
        subject_path = self.tmpdir / "approved-subject.bin"
        subject_path.write_bytes(b"approved-subject")
        subject = SubjectMasterSelection.create(
            receipt=receipt,
            source_path=subject_path,
            source_image_url=sku.image_urls[0],
            visible_subject_quantity=1,
            white_background_confirmed=False,
            confirmed_at="2026-07-22T00:01:00+00:00",
        )
        queue = ImageGenerationQueue(
            self.context.runtime_root / "image_generation" / "ozon_image_jobs.sqlite3"
        )
        job = queue.enqueue(receipt=receipt, subject_master=subject)
        with queue._connect() as connection:
            for slot in queue.list_slots(job["job_id"]):
                output = self.tmpdir / f"approved-{slot['slot_id']}.png"
                output.write_bytes(slot["slot_id"].encode("utf-8"))
                connection.execute(
                    "UPDATE image_slots SET status = 'accepted', accepted_path = ? "
                    "WHERE job_id = ? AND slot_id = ?",
                    (str(output.resolve()), job["job_id"], slot["slot_id"]),
                )
            connection.execute(
                "UPDATE image_jobs SET status = 'completed' WHERE job_id = ?",
                (job["job_id"],),
            )
        self.repo.save_subject_masters(
            run_id,
            {
                "run_id": run_id,
                "items": {
                    seed.seed_id: {
                        "subject_master": subject.to_dict(),
                        "image_job_id": job["job_id"],
                    }
                },
            },
        )

        workspace = self.get_json(f"/api/batches/{run_id}/upload")["data"]

        self.assertEqual(8, workspace["gates"]["generated_image_count"])
        self.assertEqual(1, workspace["gates"]["approved_product_count"])
        self.assertTrue(workspace["items"][0]["generated_images_ready"])
        self.assertTrue(workspace["gates"]["images_ready"])

    def test_upload_workspace_allows_ready_product_without_waiting_for_blocked_product(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ready_candidate = ozon_result["ozon_candidates"][0]
        ready_candidate["attributes"]["Цвет"] = "белый"
        blocked_candidate = json.loads(json.dumps(ready_candidate))
        blocked_candidate["seed_id"] = "seed-blocked"
        blocked_candidate["product_id"] = "ozon-blocked"
        blocked_candidate["title"] = "Blocked product"
        ozon_result["ozon_candidates"].append(blocked_candidate)
        self.repo.save_ozon_collection_result(run_id, ozon_result)

        template_result = self.repo.load_attribute_template_result(run_id)
        blocked_template = json.loads(json.dumps(template_result["seed_templates"][0]))
        blocked_template["seed_id"] = "seed-blocked"
        template_result["seed_templates"].append(blocked_template)
        self.repo.save_attribute_template_result(run_id, template_result)

        self.attach_completed_image_job(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        self.post_json(
            f"/api/batches/{run_id}/pricing-evidence/confirm",
            self.pricing_input_payload(seed.seed_id),
        )

        workspace = self.get_json(f"/api/batches/{run_id}/upload")["data"]
        items = {item["seed_id"]: item for item in workspace["items"]}

        self.assertTrue(items[seed.seed_id]["ready_to_build"])
        self.assertEqual([], items[seed.seed_id]["blocking_gates"])
        self.assertEqual("mapped", items[seed.seed_id]["attribute_mapping"][0]["status"])
        self.assertFalse(items["seed-blocked"]["ready_to_build"])
        self.assertIn(
            "bootstrap_image",
            items["seed-blocked"]["blocking_gates"],
        )
        self.assertTrue(workspace["gates"]["ready_to_build"])
        self.assertEqual(1, workspace["gates"]["ready_to_build_count"])
        self.assertEqual(1, workspace["gates"]["blocked_product_count"])
        self.assertFalse(workspace["gates"]["all_products_ready"])
        self.assertFalse(workspace["gates"]["images_ready"])

        draft = self.post_json(f"/api/batches/{run_id}/upload-draft", {})["data"]
        self.assertEqual(1, draft["prepared_product_count"])
        self.assertEqual([seed.seed_id], [item["seed_id"] for item in draft["items"]])
        self.assertEqual("85", draft["items"][0]["attributes"][0]["attribute_id"])
        self.assertEqual(90, draft["items"][0]["content_optimization"]["target_score"])
        self.assertEqual(
            ready_candidate["attributes"],
            draft["items"][0]["content_optimization"]["objective_evidence"]["ozon_attributes"],
        )
        self.assertIn(
            "ozon_content_score_evidence",
            draft["items"][0]["content_optimization"]["objective_evidence"],
        )
        self.assertEqual(
            items[seed.seed_id]["upload_core_fields"],
            draft["items"][0]["upload_core_fields"],
        )
        self.assertTrue((self.repo.run_dir(run_id) / "upload_draft.json").exists())

    def test_upload_workspace_bulk_maps_required_template_fields_from_evidence(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        candidate = ozon_result["ozon_candidates"][0]
        candidate["brand"] = "Test Brand"
        candidate["title"] = "Щетка для уборки"
        candidate["category_path"] = "Дом и сад / Инвентарь для уборки / Щетки"
        candidate["attributes"] = {
            "Тип": "Щетка для уборки",
            "Артикул": "MODEL-42",
            "Цвет": "Белый",
        }
        candidate["content_score_evidence"]["attribute_table"] = {
            **candidate["attributes"],
            "Ширина, мм": "120",
        }
        self.repo.save_ozon_collection_result(run_id, ozon_result)

        template_result = self.repo.load_attribute_template_result(run_id)
        template = template_result["seed_templates"][0]
        template["category_candidates"][0]["category_path"] = candidate["category_path"]
        template["category_candidates"][0]["leaf_category"] = "Щетки"
        template["seller_attribute_template"]["matched_category_path"] = (
            "Дом и сад / Инвентарь для уборки / Щетка для уборки"
        )
        template["upload_attribute_schema"] = [
            {"attribute_id": "85", "attribute_label": "Бренд", "is_required": True},
            {"attribute_id": "8229", "attribute_label": "Тип", "is_required": True},
            {
                "attribute_id": "9048",
                "attribute_label": "Название модели (для объединения в одну карточку)",
                "is_required": True,
            },
            {"attribute_id": "10096", "attribute_label": "Цвет товара", "is_required": False},
            {"attribute_id": "9799", "attribute_label": "Ширина, мм", "is_required": False},
            {"attribute_id": "4180", "attribute_label": "Название", "is_required": False},
            {"attribute_id": "4191", "attribute_label": "Аннотация", "is_required": False},
            {"attribute_id": "11254", "attribute_label": "Rich-контент JSON", "is_required": False},
        ]
        self.repo.save_attribute_template_result(run_id, template_result)

        workspace = self.get_json(f"/api/batches/{run_id}/upload")["data"]
        item = workspace["items"][0]
        mapped = {field["field_key"]: field for field in item["attribute_mapping"]}

        self.assertEqual(5, item["mapped_attribute_count"])
        self.assertEqual(3, item["rewrite_required_count"])
        self.assertEqual(3, workspace["gates"]["rewrite_required_count"])
        self.assertEqual(3, item["required_mapped_count"])
        self.assertEqual([], item["missing_required_fields"])
        self.assertTrue(item["required_attributes_ready"])
        self.assertEqual("Test Brand", mapped["85"]["value"])
        self.assertEqual("Щетка для уборки", mapped["8229"]["value"])
        self.assertEqual("MODEL-42", mapped["9048"]["value"])
        self.assertEqual("ozon.attributes.Артикул", mapped["9048"]["evidence_ref"])
        self.assertEqual("Белый", mapped["10096"]["value"])
        self.assertEqual("120", mapped["9799"]["value"])
        self.assertEqual("rewrite_required", mapped["4180"]["status"])
        self.assertEqual("rewrite_required", mapped["4191"]["status"])
        self.assertEqual("rewrite_required", mapped["11254"]["status"])

    def test_ozon_content_tasks_recreate_creative_fields_from_collected_evidence(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        candidate = ozon_result["ozon_candidates"][0]
        candidate["brand"] = "PRO SEWING"
        candidate["title"] = "Исходный дырокол для кожи 3 в 1"
        candidate["attributes"] = {
            "Тип": "Пробойник для кожи",
            "Материал": "Сталь",
            "Длина, мм": "210",
        }
        candidate["content_score_evidence"]["attribute_table"] = dict(candidate["attributes"])
        candidate["content_score_evidence"]["description_or_rich_content_blocks"] = [
            "Исходное описание карточки Ozon для стилевого и структурного анализа."
        ]
        self.repo.save_ozon_collection_result(run_id, ozon_result)

        template_result = self.repo.load_attribute_template_result(run_id)
        template = template_result["seed_templates"][0]
        template["upload_attribute_schema"] = [
            {"attribute_id": "85", "attribute_label": "Бренд", "is_required": True},
            {"attribute_id": "4180", "attribute_label": "Название", "is_required": False},
            {"attribute_id": "4191", "attribute_label": "Аннотация", "is_required": False},
            {"attribute_id": "23171", "attribute_label": "#Хештеги", "is_required": False},
            {"attribute_id": "11254", "attribute_label": "Rich-контент JSON", "is_required": False},
        ]
        self.repo.save_attribute_template_result(run_id, template_result)

        pending_workspace = self.get_json(f"/api/batches/{run_id}/upload")["data"]
        pending_item = pending_workspace["items"][0]
        self.assertFalse(pending_item["original_content_ready"])
        self.assertIn("original_content", pending_item["blocking_gates"])
        self.assertEqual(0, pending_workspace["gates"]["original_content_ready_count"])

        tasks = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"]
        self.assertEqual(1, tasks["summary"]["pending"])
        self.assertEqual(seed.seed_id, tasks["items"][0]["seed_id"])
        self.assertEqual(4, len(tasks["items"][0]["rewrite_fields"]))
        self.assertEqual(candidate["attributes"], tasks["items"][0]["evidence"]["ozon_attributes"])
        self.assertEqual(
            candidate["content_score_evidence"],
            tasks["items"][0]["evidence"]["ozon_content_score_evidence"],
        )

        invalid = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "4180": candidate["title"],
                    "4191": "Коротко",
                    "23171": "#кожа",
                    "11254": "{}",
                },
            },
            ok=False,
        )
        self.assertEqual("content_task.validation_failed", invalid["code"])

        completed = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "4180": "Пробойник для кожи PRO SEWING, стальной инструмент 3 в 1",
                    "4191": (
                        "Стальной пробойник предназначен для аккуратной работы с кожей, тканью "
                        "и резиной. Формат 3 в 1 помогает подготавливать отверстия и устанавливать "
                        "фурнитуру, а длина инструмента составляет 210 мм."
                    ),
                    "23171": "#пробойник #кожа #швейныйинструмент #фурнитура",
                    "11254": json.dumps(
                        {
                            "content": [
                                {"title": "Точная работа", "text": "Стальной инструмент 3 в 1"},
                                {"title": "Размер", "text": "Длина 210 мм"},
                            ]
                        },
                        ensure_ascii=False,
                    ),
                },
            },
        )["data"]
        self.assertEqual("completed", completed["status"])
        self.assertTrue((self.repo.run_dir(run_id) / "generated_content_result.json").exists())

        refreshed_tasks = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"]
        self.assertEqual(0, refreshed_tasks["summary"]["pending"])
        self.assertEqual(1, refreshed_tasks["summary"]["completed"])
        item = self.get_json(f"/api/batches/{run_id}/upload")["data"]["items"][0]
        mapped = {field["field_key"]: field for field in item["attribute_mapping"]}
        self.assertEqual(0, item["rewrite_required_count"])
        self.assertTrue(item["original_content_ready"])
        self.assertNotIn("original_content", item["blocking_gates"])
        self.assertEqual("generated_original_content", mapped["4180"]["source"])
        self.assertEqual("mapped", mapped["11254"]["status"])

        page = self.get_text(f"/batches/{run_id}/upload")
        self.assertIn('id="contentControllerCommand"', page)
        self.assertIn('id="copyContentControllerCommand"', page)
        self.assertIn('id="originalContentGate"', page)
        self.assertIn("复制整批智能字段草稿命令", page)
        self.assertIn("$ozon-intelligent-field-drafter", page)
        self.assertIn("skills/ozon-intelligent-field-drafter/SKILL.md", page)
        self.assertIn("identify the locked SKU primary product subject", page)
        self.assertIn("subject_analysis", page)
        self.assertIn("accessories, packaging, backgrounds", page)
        self.assertNotIn("yandex", page.casefold())

    def test_intelligent_field_tasks_cover_missing_objective_fields_and_require_evidence(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        candidate = ozon_result["ozon_candidates"][0]
        candidate["title"] = "Исходный комплект ручных инструментов"
        candidate["attributes"] = {"Комплектация": "1 инструмент"}
        candidate["content_score_evidence"]["attribute_table"] = dict(candidate["attributes"])
        self.repo.save_ozon_collection_result(run_id, ozon_result)

        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"] = [
            {
                "attribute_id": "tools-count",
                "attribute_label": "Количество инструментов в наборе, шт.",
                "is_required": True,
            },
            {
                "attribute_id": "warranty",
                "attribute_label": "Гарантия",
                "is_required": False,
            },
            {
                "attribute_id": "title",
                "attribute_label": "Название",
                "is_required": False,
            },
        ]
        self.repo.save_attribute_template_result(run_id, template_result)

        tasks = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"]
        task = tasks["items"][0]
        field_tasks = {field["field_key"]: field for field in task["field_tasks"]}
        self.assertEqual(1, tasks["summary"]["pending"])
        self.assertEqual(3, tasks["summary"]["pending_fields"])
        self.assertEqual("evidence_inference", field_tasks["tools-count"]["mode"])
        self.assertEqual("evidence_inference", field_tasks["warranty"]["mode"])
        self.assertEqual("creative_rewrite", field_tasks["title"]["mode"])
        self.assertTrue(task["rules"]["required_fields_must_be_completed_first"])
        self.assertTrue(
            task["rules"]["manual_entry_only_after_intelligence_unresolved"]
        )
        self.assertEqual(
            "1 инструмент",
            task["evidence_index"]["ozon.attributes.Комплектация"],
        )

        invalid = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "tools-count": {
                        "decision": "filled",
                        "value": "1",
                        "evidence_refs": [],
                        "reason": "Получено из комплектации.",
                    },
                    "warranty": {
                        "decision": "unresolved",
                        "reason": "Гарантия не указана в собранных данных.",
                    },
                    "title": (
                        "Комплект ручных инструментов для точной работы, 1 предмет"
                    ),
                },
            },
            ok=False,
        )
        self.assertEqual("content_task.validation_failed", invalid["code"])
        self.assertTrue(
            any("evidence_refs" in error for error in invalid.get("errors", []))
        )

        unrelated = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "tools-count": {
                        "decision": "filled",
                        "value": "1",
                        "evidence_refs": ["ozon.attributes.Комплектация"],
                        "reason": "Количество извлечено из собранной комплектации.",
                    },
                    "warranty": {
                        "decision": "filled",
                        "value": "1 год",
                        "evidence_refs": ["ozon.attributes.Комплектация"],
                        "reason": "Гарантия якобы выведена из комплектации.",
                    },
                    "title": (
                        "Комплект ручных инструментов для точной работы, 1 предмет"
                    ),
                },
            },
            ok=False,
        )
        self.assertEqual("content_task.validation_failed", unrelated["code"])
        self.assertTrue(
            any("field-specific evidence_ref" in error for error in unrelated["errors"])
        )

        completed = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "tools-count": {
                        "decision": "filled",
                        "value": "1",
                        "evidence_refs": ["ozon.attributes.Комплектация"],
                        "reason": "Количество извлечено из собранной комплектации.",
                    },
                    "warranty": {
                        "decision": "unresolved",
                        "reason": "Гарантия не указана в собранных данных Ozon и 1688.",
                        "evidence_refs": [],
                        "resolution_class": "source_fact_missing",
                    },
                    "title": (
                        "Комплект ручных инструментов для точной работы, 1 предмет"
                    ),
                },
            },
        )["data"]
        self.assertEqual("completed_with_gaps", completed["status"])
        self.assertEqual(1, completed["unresolved_field_count"])

        refreshed = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"]
        self.assertEqual(0, refreshed["summary"]["pending"])
        self.assertEqual(0, refreshed["summary"]["pending_fields"])
        self.assertEqual(1, refreshed["summary"]["unresolved_fields"])
        self.assertEqual(1, refreshed["summary"]["completed_with_gaps"])
        self.assertEqual("completed_with_gaps", refreshed["items"][0]["status"])
        workspace_item = self.get_json(f"/api/batches/{run_id}/upload")["data"]["items"][0]
        mapped = {
            field["field_key"]: field
            for field in workspace_item["attribute_mapping"]
        }
        self.assertEqual("generated_evidence_completion", mapped["tools-count"]["source"])
        self.assertEqual("unresolved", mapped["warranty"]["intelligence_decision"])
        self.assertEqual("generated_original_content", mapped["title"]["source"])

        page = self.get_text(f"/batches/{run_id}/upload")
        self.assertIn("智能字段草稿", page)
        self.assertIn("$ozon-intelligent-field-drafter", page)
        self.assertIn("skills/ozon-intelligent-field-drafter/SKILL.md", page)
        self.assertNotIn("mode=evidence_inference", page)
        self.assertNotIn("JSON 为", page)

    def test_content_tasks_expose_full_locked_supplier_sku_evidence(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "offer_id": "559479796544",
                        "title": "两用打孔钳",
                        "attributes": {"规格": "两用打孔钳"},
                    }
                ],
            },
        )
        self.repo.save_supplier_sku_selections(
            run_id,
            {
                "run_id": run_id,
                "selections": {
                    seed.seed_id: {
                        "supplier_offer_id": "559479796544",
                        "supplier_sku": {
                            "supplier_sku_id": "559479796544",
                            "combination_key": "页面唯一 SKU",
                            "raw_label": "页面唯一 SKU（无需选择规格）",
                            "selected_options": {"规格": "页面唯一 SKU"},
                            "set_quantity": 1,
                            "set_composition": ["单件商品"],
                            "price": {"currency": "CNY", "amount": "4.70"},
                            "stock": {"status": "unknown", "quantity": None},
                        },
                        "ozon_target_sku": {
                            "sku_id": "ozon-test",
                            "selected_options": {"Цвет": "Белый"},
                        },
                    }
                },
            },
        )

        task = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"]["items"][0]

        self.assertEqual(
            1,
            task["evidence"]["confirmed_supplier_sku"]["set_quantity"],
        )
        self.assertEqual(
            ["单件商品"],
            task["evidence"]["confirmed_supplier_sku"]["set_composition"],
        )
        self.assertEqual(
            "页面唯一 SKU（无需选择规格）",
            task["evidence"]["confirmed_supplier_sku"]["raw_label"],
        )
        self.assertEqual(
            {"Цвет": "Белый"},
            task["evidence"]["ozon_selected_sku"],
        )
        self.assertEqual(
            1,
            task["evidence_index"]["supplier_selection.supplier_sku.set_quantity"],
        )
        self.assertEqual(
            "单件商品",
            task["evidence_index"][
                "supplier_selection.supplier_sku.set_composition.0"
            ],
        )
        self.assertEqual(
            "Белый",
            task["evidence_index"]["ozon.target_sku.selected_options.Цвет"],
        )

    def test_content_tasks_reopen_polluted_mapped_field_for_normalization(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.repo.save_supplier_sku_selections(
            run_id,
            {
                "run_id": run_id,
                "selections": {
                    seed.seed_id: {
                        "supplier_offer_id": "559479796544",
                        "supplier_sku": {
                            "supplier_sku_id": "sku-black",
                            "combination_key": "颜色=黑色",
                            "raw_label": "黑色+黑色鞋底（现货当天发）",
                            "selected_options": {
                                "颜色": "黑色+黑色鞋底（现货当天发）",
                            },
                            "set_quantity": 1,
                            "set_composition": ["单件商品"],
                            "complete": True,
                        },
                    }
                },
            },
        )
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"] = [
            {
                "attribute_id": "color",
                "attribute_label": "Цвет товара",
                "is_required": True,
            }
        ]
        self.repo.save_attribute_template_result(run_id, template_result)

        task = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"][
            "items"
        ][0]
        field = task["field_tasks"][0]

        self.assertEqual("color", field["field_key"])
        self.assertEqual("evidence_inference", field["mode"])
        self.assertEqual("rewrite_required", field["current_mapping_status"])
        self.assertIn(
            "supplier_selection.supplier_sku.selected_options.颜色",
            field["candidate_evidence_refs"],
        )
        self.assertEqual(
            "黑色+黑色鞋底（现货当天发）",
            task["evidence_index"][
                "supplier_selection.supplier_sku.selected_options.颜色"
            ],
        )

    def test_content_tasks_reopen_visual_fields_and_expose_only_supplier_images(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_image = "https://cbu01.alicdn.com/img/ibank/locked-sku.jpg"
        second_supplier_image = (
            "https://cbu01.alicdn.com/img/ibank/locked-sku-detail.jpg"
        )
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "offer_id": "559479796544",
                        "title": "两用打孔钳",
                        "attributes": {},
                        "images": [supplier_image, second_supplier_image],
                    }
                ],
            },
        )
        self.repo.save_supplier_sku_selections(
            run_id,
            {
                "run_id": run_id,
                "selections": {
                    seed.seed_id: {
                        "supplier_offer_id": "559479796544",
                        "supplier_sku": {
                            "supplier_sku_id": "559479796544",
                            "combination_key": "页面唯一 SKU",
                            "raw_label": "页面唯一 SKU（无需选择规格）",
                            "selected_options": {"规格": "页面唯一 SKU"},
                            "set_quantity": 1,
                            "set_composition": ["单件商品"],
                            "image_urls": [supplier_image],
                            "evidence_source": "single_sku_detail_page",
                            "complete": True,
                        },
                    }
                },
            },
        )
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"] = [
            {
                "attribute_id": "10096",
                "attribute_label": "Цвет товара",
                "is_required": False,
                "dictionary_id": 100,
                "allowed_values": ["Серебристый", "Черный"],
            },
            {
                "attribute_id": "10097",
                "attribute_label": "Название цвета",
                "is_required": False,
            },
            {
                "attribute_id": "11650",
                "attribute_label": "Количество заводских упаковок",
                "is_required": False,
            },
            {
                "attribute_id": "9285",
                "attribute_label": "Количество инструментов в наборе, шт.",
                "is_required": False,
            },
        ]
        self.repo.save_attribute_template_result(run_id, template_result)
        unresolved = {
            field_key: {
                "decision": "unresolved",
                "value": None,
                "evidence_refs": [],
                "reason": "В структурированных данных нет подтвержденного значения.",
                "mode": "evidence_inference",
                "resolution_class": "source_fact_missing",
            }
            for field_key in ("10096", "10097", "11650", "9285")
        }
        self.repo.save_generated_content_result(
            run_id,
            {
                "schema_version": 2,
                "run_id": run_id,
                "items": {
                    seed.seed_id: {
                        "status": "completed_with_gaps",
                        "field_results": unresolved,
                    }
                },
            },
        )

        task = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"][
            "items"
        ][0]
        fields = {field["field_key"]: field for field in task["field_tasks"]}

        self.assertTrue(task["evidence"]["supplier_visual_evidence"]["page_single_sku"])
        self.assertEqual(
            supplier_image,
            task["evidence_index"][
                "supplier_selection.supplier_sku.image_urls.0"
            ],
        )
        self.assertEqual(
            second_supplier_image,
            task["evidence_index"]["supplier.images.1"],
        )
        self.assertNotIn("generated.images.0", task["evidence_index"])
        for field_key in ("10096", "10097", "11650", "9285"):
            self.assertEqual("pending", fields[field_key]["status"])
            self.assertTrue(fields[field_key]["visual_inference_supported"])
            self.assertIn(
                "supplier_selection.supplier_sku.image_urls.0",
                fields[field_key]["visual_evidence_refs"],
            )
            self.assertEqual(
                "locked_sku_primary",
                fields[field_key]["visual_evidence_roles"][
                    "supplier_selection.supplier_sku.image_urls.0"
                ],
            )
            self.assertEqual(
                "single_sku_gallery",
                fields[field_key]["visual_evidence_roles"]["supplier.images.1"],
            )
        self.assertEqual(
            "primary_product",
            fields["10096"]["visual_target_scope"],
        )
        self.assertEqual(
            "factory_packaging",
            fields["11650"]["visual_target_scope"],
        )
        self.assertEqual(
            "complete_set",
            fields["9285"]["visual_target_scope"],
        )
        self.assertEqual(
            ["Серебристый", "Черный"],
            fields["10096"]["allowed_values"],
        )

    def test_visual_field_requires_a_complete_field_specific_analysis_receipt(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_image = "https://cbu01.alicdn.com/img/ibank/locked-sku.jpg"
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "offer_id": "559479796544",
                        "title": "银色两用打孔钳",
                        "attributes": {},
                        "images": [supplier_image],
                    }
                ],
            },
        )
        selections = {
            "run_id": run_id,
            "selections": {
                seed.seed_id: {
                    "supplier_offer_id": "559479796544",
                    "supplier_sku": {
                        "supplier_sku_id": "559479796544",
                        "combination_key": "页面唯一 SKU",
                        "raw_label": "页面唯一 SKU（无需选择规格）",
                        "selected_options": {"规格": "页面唯一 SKU"},
                        "set_quantity": 1,
                        "set_composition": ["单件商品"],
                        "image_urls": [supplier_image],
                        "evidence_source": "dom_option_labels",
                        "complete": True,
                    },
                }
            },
        }
        self.repo.save_supplier_sku_selections(run_id, selections)
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"] = [
            {
                "attribute_id": "10096",
                "attribute_label": "Цвет товара",
                "is_required": False,
                "dictionary_id": 100,
                "allowed_values": ["Серебристый", "Черный"],
            }
        ]
        self.repo.save_attribute_template_result(run_id, template_result)
        evidence_ref = "supplier_selection.supplier_sku.image_urls.0"
        base_field = {
            "decision": "filled",
            "value": "Серебристый",
            "evidence_refs": [evidence_ref],
            "reason": "На оригинальном фото выбранного SKU виден серебристый металл.",
        }

        invalid = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {"10096": base_field},
            },
            ok=False,
        )

        self.assertTrue(
            any("visual_analysis" in error for error in invalid["errors"]),
            invalid,
        )

        field_specific_analysis = {
            "result": "observed",
            "confidence": "high",
            "field_finding": (
                "Рабочие части и корпус имеют серебристый "
                "металлический цвет."
            ),
            "inspected_refs": [evidence_ref],
            "observations": [
                {
                    "evidence_ref": evidence_ref,
                    "finding": (
                        "На полном изображении locked SKU виден "
                        "серебристый металлический инструмент."
                    ),
                }
            ],
        }
        missing_subject_scope = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "10096": {
                        **base_field,
                        "visual_analysis": field_specific_analysis,
                    }
                },
            },
            ok=False,
        )
        self.assertTrue(
            any(
                "subject_analysis" in error
                for error in missing_subject_scope["errors"]
            ),
            missing_subject_scope,
        )

        subject_analysis = {
            "primary_subject": "Двухфункциональный ручной пробойник",
            "target_scope": "primary_product",
            "basis_refs": [evidence_ref],
            "excluded_elements": [
                {
                    "element": "Люверсы рядом с инструментом",
                    "role": "accessory",
                    "colors": ["Золотистый"],
                }
            ],
            "subject_state": "single_color",
            "subject_colors": ["Серебристый"],
            "normalized_value": "Серебристый",
        }
        unresolved_observed_subject = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "10096": {
                        "decision": "unresolved",
                        "value": None,
                        "evidence_refs": [evidence_ref],
                        "reason": (
                            "Серебристый корпус и золотистые аксессуары "
                            "ошибочно сочтены конфликтом цвета."
                        ),
                        "resolution_class": "supplier_identity_missing",
                        "visual_analysis": {
                            **field_specific_analysis,
                            "result": "ambiguous",
                            "subject_analysis": subject_analysis,
                        },
                    }
                },
            },
            ok=False,
        )
        self.assertTrue(
            any(
                "single_color" in error and "filled" in error
                for error in unresolved_observed_subject["errors"]
            ),
            unresolved_observed_subject,
        )

        completed = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "10096": {
                        **base_field,
                        "visual_analysis": {
                            **field_specific_analysis,
                            "subject_analysis": subject_analysis,
                        },
                    }
                },
            },
        )["data"]
        saved = self.repo.load_generated_content_result(run_id)["items"][
            seed.seed_id
        ]["field_results"]["10096"]

        self.assertEqual("completed", completed["status"])
        self.assertEqual("observed", saved["visual_analysis"]["result"])
        self.assertEqual(
            "single_color",
            saved["visual_analysis"]["subject_analysis"]["subject_state"],
        )

    def test_content_task_rejects_chinese_customer_facing_objective_text(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "offer_id": "559479796544",
                        "title": "两用打孔钳",
                        "attributes": {"规格": "两用打孔钳"},
                    }
                ],
            },
        )
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"] = [
            {
                "attribute_id": "model",
                "attribute_label": "Название модели (для объединения в одну карточку)",
                "attribute_type": "String",
                "is_required": False,
            }
        ]
        self.repo.save_attribute_template_result(run_id, template_result)

        invalid = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "model": {
                        "decision": "filled",
                        "value": "两用打孔钳",
                        "evidence_refs": ["supplier.attributes.规格"],
                        "reason": "Translated model name from the supplier specification.",
                    }
                },
            },
            ok=False,
        )

        self.assertEqual("content_task.validation_failed", invalid["code"])
        self.assertTrue(
            any("Russian text is required" in error for error in invalid["errors"])
        )

    def test_unresolved_fields_require_classification_and_do_not_report_ready(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"] = [
            {
                "attribute_id": "warranty",
                "attribute_label": "Гарантия",
                "attribute_type": "String",
                "is_required": False,
            }
        ]
        self.repo.save_attribute_template_result(run_id, template_result)

        invalid = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "warranty": {
                        "decision": "unresolved",
                        "reason": "Гарантия отсутствует в собранных данных.",
                        "evidence_refs": [],
                    }
                },
            },
            ok=False,
        )
        self.assertTrue(
            any("resolution_class" in error for error in invalid["errors"])
        )

        completed = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "warranty": {
                        "decision": "unresolved",
                        "reason": "Гарантия отсутствует в собранных данных.",
                        "evidence_refs": [],
                        "resolution_class": "source_fact_missing",
                    }
                },
            },
        )["data"]
        refreshed = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"]

        self.assertEqual("completed_with_gaps", completed["status"])
        self.assertEqual("completed_with_gaps", refreshed["items"][0]["status"])
        self.assertEqual(1, refreshed["summary"]["completed_with_gaps"])
        self.assertEqual(0, refreshed["summary"]["ready"])

    def test_legacy_unclassified_unresolved_decision_is_reopened(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"] = [
            {
                "attribute_id": "warranty",
                "attribute_label": "Гарантия",
                "attribute_type": "String",
                "is_required": False,
            }
        ]
        self.repo.save_attribute_template_result(run_id, template_result)
        self.repo.save_generated_content_result(
            run_id,
            {
                "schema_version": 2,
                "run_id": run_id,
                "items": {
                    seed.seed_id: {
                        "field_results": {
                            "warranty": {
                                "decision": "unresolved",
                                "reason": "Legacy unresolved decision.",
                                "evidence_refs": [],
                                "mode": "evidence_inference",
                            }
                        }
                    }
                },
            },
        )

        task = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"]["items"][0]

        self.assertEqual("pending", task["status"])
        self.assertEqual(1, task["pending_field_count"])
        self.assertEqual("pending", task["field_tasks"][0]["status"])

    def test_completing_reopened_task_drops_superseded_unresolved_results(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "supplier_products": [
                    {
                        "seed_id": seed.seed_id,
                        "offer_id": "559479796544",
                        "title": "两用打孔钳",
                        "attributes": {},
                    }
                ],
            },
        )
        self.repo.save_supplier_sku_selections(
            run_id,
            {
                "run_id": run_id,
                "selections": {
                    seed.seed_id: {
                        "supplier_offer_id": "559479796544",
                        "supplier_sku": {
                            "supplier_sku_id": "559479796544",
                            "combination_key": "页面唯一 SKU",
                            "selected_options": {"规格": "页面唯一 SKU"},
                            "set_quantity": 1,
                            "set_composition": ["单件商品"],
                        },
                    }
                },
            },
        )
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"] = [
            {
                "attribute_id": "quantity",
                "attribute_label": "Количество товара в УЕИ",
                "attribute_type": "Integer",
                "is_required": True,
            },
            {
                "attribute_id": "seller-code",
                "attribute_label": "Код продавца",
                "attribute_type": "String",
                "is_required": True,
            },
            {
                "attribute_id": "warranty",
                "attribute_label": "Гарантия",
                "attribute_type": "String",
                "is_required": False,
            },
        ]
        self.repo.save_attribute_template_result(run_id, template_result)
        legacy_results = {
            field_key: {
                "decision": "unresolved",
                "reason": "Legacy unresolved decision.",
                "evidence_refs": [],
                "mode": "evidence_inference",
            }
            for field_key in ("quantity", "seller-code", "warranty")
        }
        self.repo.save_generated_content_result(
            run_id,
            {
                "schema_version": 2,
                "run_id": run_id,
                "items": {
                    seed.seed_id: {
                        "field_results": legacy_results,
                    }
                },
            },
        )

        task = self.get_json(f"/api/batches/{run_id}/content-tasks")["data"]["items"][0]
        self.assertEqual(
            ["warranty"],
            [field["field_key"] for field in task["field_tasks"]],
        )
        completed = self.post_json(
            f"/api/batches/{run_id}/content-tasks/complete",
            {
                "seed_id": seed.seed_id,
                "fields": {
                    "warranty": {
                        "decision": "unresolved",
                        "resolution_class": "source_fact_missing",
                        "reason": "Гарантия отсутствует в собранных данных.",
                        "evidence_refs": [],
                    }
                },
            },
        )["data"]
        saved = self.repo.load_generated_content_result(run_id)["items"][seed.seed_id]

        self.assertEqual(1, completed["unresolved_field_count"])
        self.assertEqual(["warranty"], sorted(saved["field_results"]))

    def test_upload_workspace_flags_cross_domain_template_instead_of_mapping_it(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        candidate = ozon_result["ozon_candidates"][0]
        candidate["title"] = "Детский гамак для самолета"
        candidate["category_path"] = "Детские товары / Переноски для детей / SUFEITE"
        candidate["attributes"] = {"Тип": "Гамак детский в самолет", "Материал": "Рипстоп"}
        self.repo.save_ozon_collection_result(run_id, ozon_result)

        template_result = self.repo.load_attribute_template_result(run_id)
        template = template_result["seed_templates"][0]
        template["category_candidates"][0]["category_path"] = candidate["category_path"]
        template["category_candidates"][0]["leaf_category"] = "SUFEITE"
        template["seller_attribute_template"]["matched_category_path"] = (
            "Продукты питания / Соль, сахар, специи / Мак"
        )
        template["upload_attribute_schema"] = [
            {"attribute_id": "8229", "attribute_label": "Тип", "is_required": True},
            {"attribute_id": "7578", "attribute_label": "Срок годности в днях", "is_required": True},
        ]
        self.repo.save_attribute_template_result(run_id, template_result)

        item = self.get_json(f"/api/batches/{run_id}/upload")["data"]["items"][0]

        self.assertFalse(item["template_ready"])
        self.assertEqual("cross_domain_category_mismatch", item["category_template_assessment"]["reason"])
        self.assertIn("category_template", item["blocking_gates"])

    def test_required_not_applicable_field_marks_template_for_automatic_refresh(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"].append(
            {
                "attribute_id": "900",
                "attribute_label": "Форма выпуска средства",
                "attribute_type": "string",
                "is_required": True,
                "schema_source": (
                    "ozon_seller_api_description_category_attribute"
                ),
            }
        )
        self.repo.save_attribute_template_result(run_id, template_result)
        self.repo.save_generated_content_result(
            run_id,
            {
                "schema_version": 2,
                "run_id": run_id,
                "items": {
                    seed.seed_id: {
                        "status": "completed_with_gaps",
                        "field_results": {
                            "900": {
                                "decision": "unresolved",
                                "resolution_class": "not_applicable",
                                "reason": (
                                    "The selected product is not a chemical "
                                    "cleaning agent."
                                ),
                                "evidence_refs": [],
                            }
                        },
                    }
                },
            },
        )

        item = WorkbenchService(self.repo).upload_workspace(run_id).data[
            "items"
        ][0]

        self.assertFalse(item["template_ready"])
        self.assertEqual(
            "required_template_fields_not_applicable",
            item["category_template_assessment"]["reason"],
        )
        self.assertEqual(
            ["900"],
            [
                field["field_key"]
                for field in item["category_template_assessment"][
                    "incompatible_required_fields"
                ]
            ],
        )
        self.assertIn("category_template", item["blocking_gates"])

    def test_upload_draft_resolves_required_dictionary_value_ids(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"][0]["dictionary_id"] = 100
        self.repo.save_attribute_template_result(run_id, template_result)
        self.attach_completed_image_job(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        selections = self.repo.load_supplier_sku_selections(run_id)
        selections["selections"][seed.seed_id]["supplier_sku"][
            "selected_options"
        ] = {"Цвет": "белый"}
        self.repo.save_supplier_sku_selections(run_id, selections)
        supplier_result = self.repo.load_supplier_collection_result(run_id)
        supplier_result["supplier_products"][0]["attributes"] = {
            "Цвет": "белый"
        }
        self.repo.save_supplier_collection_result(run_id, supplier_result)
        service = WorkbenchService(self.repo)
        pricing = service.confirm_pricing_evidence(
            run_id,
            self.pricing_input_payload(seed.seed_id),
        )
        self.assertTrue(pricing.ok)

        with patch.object(
            service.seller_api_adapter,
            "resolve_attribute_dictionary_value",
            return_value={"dictionary_value_id": 501, "value": "Белый"},
        ) as resolver:
            result = service.build_upload_draft(run_id)

        self.assertTrue(result.ok, result.to_dict())
        attribute = result.data["items"][0]["attributes"][0]
        self.assertEqual(501, attribute["dictionary_value_id"])
        resolver.assert_called_once_with(
            description_category_id=17000001,
            type_id=970001,
            attribute_id=85,
            value="белый",
        )

    def test_product_preview_reports_the_exact_unresolved_required_dictionary(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"][0][
            "dictionary_id"
        ] = 100
        self.repo.save_attribute_template_result(run_id, template_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        selections = self.repo.load_supplier_sku_selections(run_id)
        selections["selections"][seed.seed_id]["supplier_sku"][
            "selected_options"
        ] = {"Цвет": "белый"}
        self.repo.save_supplier_sku_selections(run_id, selections)
        supplier_result = self.repo.load_supplier_collection_result(run_id)
        supplier_result["supplier_products"][0]["attributes"] = {
            "Цвет": "белый"
        }
        self.repo.save_supplier_collection_result(run_id, supplier_result)
        service = WorkbenchService(self.repo)
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )

        with patch.object(
            service.seller_api_adapter,
            "resolve_attribute_dictionary_value",
            side_effect=SellerApiError("no exact Ozon dictionary value"),
        ):
            result = service.preview_product_upload(run_id, seed.seed_id)

        self.assertFalse(result.ok)
        self.assertEqual(
            "product_upload.required_dictionary_values_unresolved",
            result.code,
        )
        self.assertEqual(
            "required_dictionary_value_unresolved",
            result.data["blocked_item"]["reason"],
        )
        self.assertEqual(
            "85",
            result.data["blocked_item"]["fields"][0]["field_key"],
        )
        self.assertIn("Цвет", result.message)

    def test_upload_draft_omits_an_unresolved_optional_dictionary_value(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        template_result = self.repo.load_attribute_template_result(run_id)
        required_field = template_result["seed_templates"][0][
            "upload_attribute_schema"
        ][0]
        required_field["dictionary_id"] = 100
        field = {
            "attribute_id": "10096",
            "attribute_label": "Цвет товара",
            "is_required": False,
            "dictionary_id": 101,
        }
        template_result["seed_templates"][0]["upload_attribute_schema"].append(field)
        self.repo.save_attribute_template_result(run_id, template_result)
        self.attach_completed_image_job(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        service = WorkbenchService(self.repo)
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )

        def resolve_dictionary(**kwargs):
            if kwargs["attribute_id"] == 10096:
                raise ValueError("no exact dictionary value")
            return {"dictionary_value_id": 501, "value": kwargs["value"]}

        with patch.object(
            service.seller_api_adapter,
            "resolve_attribute_dictionary_value",
            side_effect=resolve_dictionary,
        ):
            result = service.build_upload_draft(run_id)

        self.assertTrue(result.ok, result.to_dict())
        item = result.data["items"][0]
        self.assertNotIn(
            field["attribute_id"],
            {attribute["attribute_id"] for attribute in item["attributes"]},
        )
        self.assertEqual(
            field["attribute_id"],
            item["omitted_optional_dictionary_fields"][0]["field_key"],
        )

    def test_rich_content_is_serialized_to_the_ozon_widget_schema(self) -> None:
        source = json.dumps(
            {
                "title": "Пробойник-щипцы для кожи",
                "description": "Ручной инструмент для аккуратной работы с кожей.",
                "blocks": [
                    {
                        "type": "facts",
                        "heading": "Характеристики",
                        "items": ["Длина: 210 мм", "Материал: сталь"],
                    }
                ],
            },
            ensure_ascii=False,
        )

        normalized = json.loads(_normalize_ozon_rich_content_value(source))

        self.assertEqual(0.3, normalized["version"])
        self.assertEqual("raTextBlock", normalized["content"][0]["widgetName"])
        self.assertEqual("default", normalized["content"][0]["theme"])
        text = normalized["content"][0]["blocks"][0]["text"]
        self.assertIn("Пробойник-щипцы для кожи", text)
        self.assertIn("Длина: 210 мм", text)

    def test_upload_draft_uses_category_leaf_for_type_and_no_brand_for_supplier_name(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        candidate = ozon_result["ozon_candidates"][0]
        candidate["attributes"] = {
            "Тип": "Инструмент для работы с кожей, мехом",
            "Бренд": "梅芳",
        }
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        template_result = self.repo.load_attribute_template_result(run_id)
        template = template_result["seed_templates"][0]
        template["category_candidates"][0]["category_path"] = (
            "Строительство и ремонт / Инструменты / Просекатель"
        )
        template["category_candidates"][0]["leaf_category"] = "Просекатель"
        template["seller_attribute_template"]["matched_category_path"] = (
            "Строительство и ремонт / Инструменты / Просекатель"
        )
        template["upload_attribute_schema"] = [
            {
                "attribute_id": "8229",
                "attribute_label": "Тип",
                "is_required": True,
                "dictionary_id": 1960,
            },
            {
                "attribute_id": "85",
                "attribute_label": "Бренд",
                "is_required": True,
                "dictionary_id": 28732849,
            },
        ]
        self.repo.save_attribute_template_result(run_id, template_result)
        self.attach_completed_image_job(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        supplier_result = self.repo.load_supplier_collection_result(run_id)
        supplier_result["supplier_products"][0]["attributes"]["品牌"] = "梅芳"
        self.repo.save_supplier_collection_result(run_id, supplier_result)
        service = WorkbenchService(self.repo, seller_api_adapter=FakeSellerApiAdapter())
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )

        result = service.build_upload_draft(run_id)

        self.assertTrue(result.ok, result.to_dict())
        attributes = {
            item["attribute_id"]: item
            for item in result.data["items"][0]["attributes"]
        }
        self.assertEqual("Просекатель", attributes["8229"]["value"])
        self.assertEqual("Нет бренда", attributes["85"]["value"])

    def test_upload_no_longer_waits_for_generated_images(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        service = WorkbenchService(
            self.repo,
            seller_api_adapter=FakeSellerApiAdapter(),
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )

        item = service.upload_workspace(run_id).data["items"][0]

        self.assertTrue(item["bootstrap_image_ready"])
        self.assertFalse(item["generated_images_ready"])
        self.assertNotIn("images", item["blocking_gates"])
        self.assertTrue(item["ready_to_build"])

    def test_upload_recognizes_subject_locked_through_real_supplier_sku_flow(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        supplier_product = self.supplier_product_payload(seed.seed_id)
        self.repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "supplier_products": [supplier_product],
            },
        )
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_COLLECTED.value
        self.repo.save_run(run)

        def download_subject(_url, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"locked supplier subject")
            return target

        service = WorkbenchService(
            self.repo,
            supplier_image_downloader=download_subject,
        )
        selected = service.confirm_supplier_sku(
            run_id,
            seed_id=seed.seed_id,
            supplier_sku_id="sku-black-1",
        )
        self.assertTrue(selected.ok, selected.to_dict())
        self.assertEqual("ozon-1", selected.data["receipt"]["product_id"])
        self.assertNotEqual(seed.seed_id, selected.data["receipt"]["product_id"])
        confirmed = service.confirm_subject_master(
            run_id,
            seed_id=seed.seed_id,
            source_image_urls=supplier_product["images"],
            visible_subject_quantity=1,
        )
        self.assertTrue(confirmed.ok, confirmed.to_dict())

        workspace = service.upload_workspace(run_id)
        item = workspace.data["items"][0]

        self.assertTrue(item["bootstrap_image_ready"])
        self.assertEqual(
            supplier_product["images"][0],
            item["bootstrap_image_url"],
        )
        self.assertNotIn("bootstrap_image", item["blocking_gates"])
        self.assertEqual(
            1,
            workspace.data["gates"]["bootstrap_image_ready_count"],
        )

    def test_product_upload_emits_one_deferred_image_task_then_binds_after_ozon_acceptance(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        subject = self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(
            self.repo,
            seller_api_adapter=adapter,
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )

        with patch.dict(os.environ, {}, clear=False):
            preview = service.preview_product_upload(run_id, seed.seed_id)
            self.assertTrue(preview.ok, preview.to_dict())
            self.assertEqual(seed.seed_id, preview.data["seed_id"])
            self.assertEqual(
                [subject["source_image_url"]],
                preview.data["seller_api_item"]["images"],
            )
            self.assertEqual(
                subject["source_image_url"],
                preview.data["seller_api_item"]["primary_image"],
            )
            self.assertEqual([], adapter.imported_items)
            self.assertEqual(
                "CNY",
                preview.data["seller_api_item"]["currency_code"],
            )
            self.assertEqual("55.9", preview.data["seller_api_item"]["price"])
            self.assertEqual("69.9", preview.data["seller_api_item"]["old_price"])

            rejected = service.submit_product_upload(
                run_id,
                seed.seed_id,
                confirmation_token="wrong-token",
            )
            self.assertFalse(rejected.ok)
            self.assertEqual([], adapter.imported_items)

            submitted = service.submit_product_upload(
                run_id,
                seed.seed_id,
                confirmation_token=preview.data["confirmation_token"],
            )

        self.assertTrue(submitted.ok, submitted.to_dict())
        self.assertEqual(7001, submitted.data["task_id"])
        deferred_package_id = submitted.data["image_task_package_id"]
        self.assertEqual("pending", submitted.data["image_task_package_status"])
        self.assertEqual(
            1,
            len(
                list(
                    (self.context.runtime_root / "image_tasks" / "pending").glob(
                        "*.json"
                    )
                )
            ),
        )
        repeated = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )
        self.assertTrue(repeated.ok)
        self.assertEqual(deferred_package_id, repeated.data["image_task_package_id"])
        inbox = ImageTaskInbox(self.context.runtime_root)
        generated = inbox.claim_next()
        self.assertEqual(deferred_package_id, generated["package_id"])
        self.assertEqual("grid_generation", generated["assignment"]["phase"])
        inbox.stage_grid(
            deferred_package_id,
            {
                "raw_grid_path": "C:/generated/gallery-grid.png",
                "raw_grid_sha256": "a" * 64,
                "white_anchor_path": "C:/generated/white-anchor.png",
                "white_anchor_sha256": "b" * 64,
            },
        )
        claimed = inbox.claim_next()
        self.assertEqual("grid_crop", claimed["assignment"]["phase"])
        waiting = inbox.await_product(
            deferred_package_id,
            {
                "images": [
                    {
                        "slot_id": f"slot-{index}",
                        "path": f"C:/generated/{index}.jpg",
                    }
                    for index in range(8)
                ],
                "video": "C:/generated/slideshow.mp4",
                "video_cover": "C:/generated/video_cover.jpg",
            },
        )
        self.assertEqual("awaiting_product", waiting["status"])

        accepted = service.refresh_product_upload_status(run_id, seed.seed_id)

        self.assertTrue(accepted.ok, accepted.to_dict())
        self.assertEqual("accepted_by_ozon", accepted.data["status"])
        package_path = self.context.runtime_root / "image_tasks" / "pending" / (
            accepted.data["image_task_package_id"] + ".json"
        )
        self.assertEqual(
            str(package_path),
            accepted.data["image_task_package_path"],
        )
        package = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertEqual(3, package["schema_version"])
        self.assertEqual("pending", package["status"])
        self.assertEqual("upload_only", package["resume_mode"])
        self.assertEqual(
            7001,
            package["store_target"]["seller_import_task_id"],
        )
        self.assertEqual(900001, package["store_target"]["product_id"])
        self.assertEqual("bound", package["store_target"]["binding_state"])
        self.assertEqual(
            subject["source_image_url"],
            package["bootstrap_image"]["url"],
        )
        self.assertFalse(
            package["generation_contract"]["return_to_workbench"],
        )
        self.assertEqual(
            "single_thread_8_grid",
            package["generation_contract"]["generation_mode"],
        )
        self.assertEqual("4x2", package["generation_contract"]["grid_layout"])
        self.assertEqual(
            "auto_quick_tunnel",
            package["generation_contract"]["public_media"],
        )
        self.assertEqual(
            {
                "required": True,
                "source": "generated_white_anchor",
                "reference_index": 1,
                "reference_count": 1,
                "additional_image_references_allowed": False,
                "reuse_for_all_finished_calls": True,
                "reuse_for_repairs": True,
                "product_identity_source": "white_anchor_only",
                "composition_source": "fixed_skill_prompt_only",
            },
            package["generation_contract"]["identity_reference"],
        )
        self.assertNotIn("ozon_reference_images", package["evidence"])
        self.assertEqual(
            {
                "required": True,
                "source": "eight_accepted_images",
                "format": "mp4",
                "layout": "3:4_vertical_slideshow",
            },
            package["generation_contract"]["video"],
        )
        self.assertEqual(
            {
                "required": True,
                "source": "main_01",
                "format": "jpg",
                "aspect_ratio": "3:4",
            },
            package["generation_contract"]["video_cover"],
        )
        self.assertEqual(1, len(adapter.imported_items))
        self.assertEqual(
            preview.data["seller_api_item"]["offer_id"],
            adapter.imported_items[0]["offer_id"],
        )
        repeated_status = service.refresh_product_upload_status(
            run_id,
            seed.seed_id,
        )
        self.assertTrue(repeated_status.ok)
        self.assertEqual(
            accepted.data["image_task_package_id"],
            repeated_status.data["image_task_package_id"],
        )
        package["store_target"]["product_id"] = None
        package_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        repaired_status = service.refresh_product_upload_status(
            run_id,
            seed.seed_id,
        )
        repaired_package = json.loads(package_path.read_text(encoding="utf-8"))

        self.assertTrue(repaired_status.ok)
        self.assertEqual(900001, repaired_package["store_target"]["product_id"])
        self.assertEqual(
            1,
            len(
                list(
                    (self.context.runtime_root / "image_tasks" / "pending").glob(
                        "*.json"
                    )
                )
            ),
        )
        reloaded_item = service.upload_workspace(run_id).data["items"][0]
        self.assertEqual(
            preview.data["confirmation_token"],
            reloaded_item["upload_preview"]["confirmation_token"],
        )
        self.assertEqual(7001, reloaded_item["upload_submission"]["task_id"])

    def test_refresh_keeps_claimed_image_task_bound_without_pending_file_error(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(self.repo, seller_api_adapter=adapter)
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )
        preview = service.preview_product_upload(run_id, seed.seed_id)
        submitted = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )
        self.assertTrue(submitted.ok, submitted.to_dict())
        accepted = service.refresh_product_upload_status(run_id, seed.seed_id)
        package_id = accepted.data["image_task_package_id"]
        claimed = ImageTaskInbox(self.context.runtime_root).claim_next()
        self.assertEqual(package_id, claimed["package_id"])

        refreshed = service.refresh_product_upload_status(run_id, seed.seed_id)

        self.assertTrue(refreshed.ok, refreshed.to_dict())
        self.assertEqual("in_progress", refreshed.data["image_task_package_status"])
        self.assertIn("image_tasks\\in_progress", refreshed.data["image_task_package_path"])
        self.assertNotIn("image_task_package_error", refreshed.data)
        self.assertEqual(
            [],
            list((self.context.runtime_root / "image_tasks" / "pending").glob("*.json")),
        )

    def test_upload_workspace_recreates_missing_package_for_confirmed_product(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(self.repo, seller_api_adapter=adapter)
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )
        preview = service.preview_product_upload(run_id, seed.seed_id)
        self.assertTrue(preview.ok, preview.to_dict())
        self.assertTrue(
            service.submit_product_upload(
                run_id,
                seed.seed_id,
                confirmation_token=preview.data["confirmation_token"],
            ).ok
        )
        accepted = service.refresh_product_upload_status(run_id, seed.seed_id)
        package_id = accepted.data["image_task_package_id"]
        package_path = (
            self.context.runtime_root
            / "image_tasks"
            / "pending"
            / f"{package_id}.json"
        )
        package_path.unlink()
        submissions = self.repo.load_upload_submissions(run_id)
        record = submissions["items"][seed.seed_id]
        record.pop("image_task_package_id", None)
        record.pop("image_task_package_path", None)
        self.repo.save_upload_submissions(run_id, submissions)

        workspace = service.upload_workspace(run_id)

        self.assertTrue(workspace.ok, workspace.to_dict())
        submission = workspace.data["items"][0]["upload_submission"]
        self.assertEqual(package_id, submission["image_task_package_id"])
        self.assertEqual("pending", submission["image_task_package_status"])
        self.assertTrue(package_path.is_file())
        self.assertEqual(1, workspace.data["gates"]["submission_accepted_count"])
        self.assertEqual(1, workspace.data["gates"]["image_task_package_count"])
        self.assertEqual(0, workspace.data["gates"]["image_task_missing_count"])

    def test_upload_page_reports_submitted_accepted_failed_and_image_task_counts(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/upload")

        self.assertIn("已提交", page)
        self.assertIn("Ozon 已确认", page)
        self.assertIn("上传失败", page)
        self.assertIn("生图任务包", page)
        self.assertIn("已提交 Seller API，但 Ozon 建品校验失败", page)
        self.assertIn("productUploadState.previews.delete(item.seed_id)", page)
        self.assertNotIn('submission.image_task_package_id || "已写入"', page)

    def test_failed_ozon_upload_keeps_deferred_image_task_package(self) -> None:
        class FailedImportAdapter(FakeSellerApiAdapter):
            def get_product_import_info(self, task_id: int) -> dict:
                return {
                    "task_id": task_id,
                    "items": [
                        {
                            "status": "failed",
                            "offer_id": item.get("offer_id"),
                            "errors": [{"code": "invalid_product"}],
                        }
                        for item in self.imported_items
                    ],
                }

        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        service = WorkbenchService(
            self.repo,
            seller_api_adapter=FailedImportAdapter(),
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )
        preview = service.preview_product_upload(run_id, seed.seed_id)
        self.assertTrue(preview.ok, preview.to_dict())
        submitted = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )
        self.assertTrue(submitted.ok, submitted.to_dict())
        package_id = submitted.data["image_task_package_id"]
        package_path = Path(submitted.data["image_task_package_path"])
        package = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertEqual(3, package["schema_version"])
        self.assertEqual("awaiting_ozon_product", package["store_target"]["binding_state"])
        self.assertIsNone(package["store_target"]["product_id"])

        failed = service.refresh_product_upload_status(run_id, seed.seed_id)

        self.assertTrue(failed.ok, failed.to_dict())
        self.assertEqual("failed", failed.data["status"])
        self.assertEqual(package_id, failed.data["image_task_package_id"])
        self.assertEqual("pending", failed.data["image_task_package_status"])
        self.assertTrue(package_path.is_file())
        self.assertFalse(
            (
                self.context.runtime_root
                / "image_tasks"
                / "quarantined"
                / f"{package_id}.json"
            ).exists()
        )
        workspace = service.upload_workspace(run_id)
        self.assertEqual(1, workspace.data["gates"]["submission_attempted_count"])
        self.assertEqual(1, workspace.data["gates"]["image_task_package_count"])
        self.assertEqual(0, workspace.data["gates"]["image_task_missing_count"])

    def test_skipped_import_is_reconciled_to_created_product_final_state(self) -> None:
        class SkippedCreatedAdapter(FakeSellerApiAdapter):
            def get_product_import_info(self, task_id: int) -> dict:
                return {
                    "task_id": task_id,
                    "items": [
                        {
                            "status": "skipped",
                            "offer_id": item.get("offer_id"),
                            "errors": [],
                        }
                        for item in self.imported_items
                    ],
                }

            def get_product_state_by_offer_id(self, offer_id: str) -> dict:
                return {
                    "offer_id": offer_id,
                    "product_id": 5763448787,
                    "sku": 5301617949,
                    "is_created": True,
                    "validation_status": "success",
                    "errors": [
                        {
                            "code": "BR_hashtag_validation",
                            "level": "warning",
                        }
                    ],
                }

        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        service = WorkbenchService(
            self.repo,
            seller_api_adapter=SkippedCreatedAdapter(),
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )
        preview = service.preview_product_upload(run_id, seed.seed_id)
        self.assertTrue(preview.ok, preview.to_dict())
        submitted = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )
        self.assertTrue(submitted.ok, submitted.to_dict())

        accepted = service.refresh_product_upload_status(run_id, seed.seed_id)

        self.assertTrue(accepted.ok, accepted.to_dict())
        self.assertEqual("accepted_by_ozon", accepted.data["status"])
        self.assertEqual(
            5763448787,
            accepted.data["seller_product_state"]["product_id"],
        )
        self.assertIn("image_task_package_id", accepted.data)

    def test_skipped_import_with_final_density_error_is_failed(self) -> None:
        class SkippedFailedAdapter(FakeSellerApiAdapter):
            def get_product_import_info(self, task_id: int) -> dict:
                return {
                    "task_id": task_id,
                    "items": [
                        {
                            "status": "skipped",
                            "offer_id": item.get("offer_id"),
                            "errors": [],
                        }
                        for item in self.imported_items
                    ],
                }

            def get_product_state_by_offer_id(self, offer_id: str) -> dict:
                return {
                    "offer_id": offer_id,
                    "product_id": 5763454848,
                    "sku": 0,
                    "is_created": False,
                    "validation_status": "pending",
                    "errors": [
                        {
                            "code": "INCORRECT_DENSITY",
                            "level": "error",
                        }
                    ],
                }

        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        service = WorkbenchService(
            self.repo,
            seller_api_adapter=SkippedFailedAdapter(),
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )
        preview = service.preview_product_upload(run_id, seed.seed_id)
        self.assertTrue(preview.ok, preview.to_dict())
        submitted = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )
        self.assertTrue(submitted.ok, submitted.to_dict())

        failed = service.refresh_product_upload_status(run_id, seed.seed_id)

        self.assertTrue(failed.ok, failed.to_dict())
        self.assertEqual("failed", failed.data["status"])
        self.assertEqual("pending", failed.data["image_task_package_status"])
        package_path = Path(failed.data["image_task_package_path"])
        package = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertIsNone(package["store_target"]["product_id"])
        self.assertEqual(
            "awaiting_ozon_product",
            package["store_target"]["binding_state"],
        )

    def test_upload_page_separates_blocking_errors_from_warnings_and_optional_gaps(
        self,
    ) -> None:
        html = build_upload_workspace_html("wb-diagnostic-layering")

        self.assertIn("sellerUploadDiagnostics", html)
        self.assertIn("ERROR_LEVEL_WARNING", html)
        self.assertIn("non-blocking-warning", html)
        self.assertIn("optional-evidence-gap", html)
        self.assertIn("不阻止上传", html)
        self.assertIn("pricing_validation_errors", html)
        self.assertIn("包装重量或尺寸不符合 Ozon 密度范围", html)

    def test_product_upload_preview_builds_only_the_requested_product_draft(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        service = WorkbenchService(
            self.repo,
            seller_api_adapter=FakeSellerApiAdapter(),
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )

        with patch.object(
            service,
            "build_upload_draft",
            wraps=service.build_upload_draft,
        ) as build_draft:
            preview = service.preview_product_upload(run_id, seed.seed_id)

        self.assertTrue(preview.ok, preview.to_dict())
        self.assertEqual(
            [seed.seed_id],
            build_draft.call_args.kwargs["seed_ids"],
        )

    def test_required_attribute_evidence_only_fills_missing_required_fields(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"].append(
            {
                "attribute_id": "900",
                "attribute_label": "Гарантия",
                "attribute_type": "string",
                "is_required": True,
                "schema_source": "ozon_seller_api_description_category_attribute",
            }
        )
        self.repo.save_attribute_template_result(run_id, template_result)
        service = WorkbenchService(self.repo)

        before = service.upload_workspace(run_id)
        self.assertTrue(before.ok, before.to_dict())
        item_before = before.data["items"][0]
        self.assertIn(
            "900",
            [field["field_key"] for field in item_before["missing_required_fields"]],
        )
        self.assertEqual([], item_before["manual_required_fields"])

        pending_save = service.save_required_attribute_evidence(
            run_id,
            seed.seed_id,
            {"900": "1 год"},
        )
        self.assertFalse(pending_save.ok)

        self.repo.save_generated_content_result(
            run_id,
            {
                "schema_version": 2,
                "run_id": run_id,
                "items": {
                    seed.seed_id: {
                        "status": "blocked",
                        "field_results": {
                            "85": {
                                "decision": "filled",
                                "value": "белый",
                                "evidence_refs": ["ozon.attributes.Цвет"],
                                "reason": "Цвет подтвержден собранными данными.",
                            },
                            "900": {
                                "decision": "unresolved",
                                "resolution_class": "source_fact_missing",
                                "evidence_refs": [],
                                "reason": "Гарантия отсутствует в собранных данных.",
                            },
                        },
                    }
                },
            },
        )
        after_skill = service.upload_workspace(run_id)
        self.assertEqual(
            ["900"],
            [
                field["field_key"]
                for field in after_skill.data["items"][0]["manual_required_fields"]
            ],
        )

        rejected = service.save_required_attribute_evidence(
            run_id,
            seed.seed_id,
            {"85": "красный", "unknown": "value"},
        )
        saved = service.save_required_attribute_evidence(
            run_id,
            seed.seed_id,
            {"900": "1 год"},
        )
        after = service.upload_workspace(run_id)

        self.assertFalse(rejected.ok)
        self.assertTrue(saved.ok, saved.to_dict())
        item_after = after.data["items"][0]
        mapped = {
            field["field_key"]: field
            for field in item_after["attribute_mapping"]
        }
        self.assertTrue(item_after["required_attributes_ready"])
        self.assertEqual([], item_after["missing_required_fields"])
        self.assertEqual("1 год", mapped["900"]["value"])
        self.assertEqual(
            "user_confirmed_required_attribute",
            mapped["900"]["source"],
        )

    def test_marking_code_decision_is_promoted_to_a_pre_upload_required_field(
        self,
    ) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["upload_attribute_schema"].append(
            {
                "attribute_id": "901",
                "attribute_label": "Нужен код маркировки",
                "attribute_type": "Boolean",
                "is_required": False,
                "schema_source": (
                    "ozon_seller_api_description_category_attribute"
                ),
            }
        )
        template_result["seed_templates"][0]["upload_attribute_schema"].append(
            {
                "attribute_id": "902",
                "attribute_label": "Подпись 18+",
                "attribute_type": "Boolean",
                "is_required": False,
                "schema_source": (
                    "ozon_seller_api_description_category_attribute"
                ),
            }
        )
        self.repo.save_attribute_template_result(run_id, template_result)
        service = WorkbenchService(self.repo)
        upload_html = build_upload_workspace_html(run_id)
        self.assertIn("需要标记代码", upload_html)
        self.assertIn("商品需要 18+ 标识", upload_html)

        before = service.upload_workspace(run_id)

        self.assertTrue(before.ok, before.to_dict())
        item_before = before.data["items"][0]
        missing = {
            field["field_key"]: field
            for field in item_before["missing_required_fields"]
        }
        self.assertIn("901", missing)
        self.assertEqual(
            "ozon_compliance_decision",
            missing["901"]["required_reason"],
        )
        self.assertEqual(
            "ozon_compliance_decision",
            missing["902"]["required_reason"],
        )
        self.assertIn("required_attributes", item_before["blocking_gates"])
        self.assertEqual(3, item_before["required_attribute_count"])
        self.assertEqual(
            ["901", "902"],
            [
                field["field_key"]
                for field in item_before["manual_required_fields"]
            ],
        )
        self.assertEqual(
            [False, True],
            item_before["manual_required_fields"][0]["allowed_values"],
        )
        self.assertEqual(
            [False, True],
            item_before["manual_required_fields"][1]["allowed_values"],
        )
        tasks = service.content_tasks(run_id)
        self.assertTrue(tasks.ok, tasks.to_dict())
        task_field_keys = {
            field["field_key"]
            for item in tasks.data["items"]
            for field in item["field_tasks"]
        }
        self.assertTrue(
            {"901", "902"}.isdisjoint(task_field_keys),
            (
                "compliance decisions must remain user-owned instead of being "
                "sent to the field drafting Skill"
            ),
        )

        saved = service.save_required_attribute_evidence(
            run_id,
            seed.seed_id,
            {"901": "false", "902": "true"},
        )
        after = service.upload_workspace(run_id)

        self.assertTrue(saved.ok, saved.to_dict())
        self.assertTrue(after.ok, after.to_dict())
        item_after = after.data["items"][0]
        mapped = {
            field["field_key"]: field
            for field in item_after["attribute_mapping"]
        }
        self.assertIs(False, mapped["901"]["value"])
        self.assertIs(True, mapped["902"]["value"])
        self.assertEqual(
            "user_confirmed_required_attribute",
            mapped["901"]["source"],
        )
        self.assertNotIn(
            "901",
            [field["field_key"] for field in item_after["missing_required_fields"]],
        )
        self.assertNotIn(
            "902",
            [field["field_key"] for field in item_after["missing_required_fields"]],
        )

    def test_seller_api_attribute_serializes_boolean_values_for_ozon(self) -> None:
        for stored_value, seller_value in (
            (False, "false"),
            (True, "true"),
            ("false", "false"),
            ("true", "true"),
        ):
            with self.subTest(stored_value=stored_value):
                attribute = _seller_api_attribute(
                    {
                        "attribute_id": "901",
                        "attribute_type": "Boolean",
                        "value": stored_value,
                    }
                )

                self.assertEqual(
                    {
                        "complex_id": 0,
                        "id": 901,
                        "values": [{"value": seller_value}],
                    },
                    attribute,
                )
        with self.assertRaisesRegex(ValueError, "requires true or false"):
            _seller_api_attribute(
                {
                    "attribute_id": "901",
                    "attribute_type": "Boolean",
                    "value": "Нет",
                }
            )

    def test_batch_upload_requires_confirmation_and_submits_ready_product(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(self.repo, seller_api_adapter=adapter)
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )

        rejected = service.batch_upload_products(
            run_id,
            seed_ids=[seed.seed_id],
            confirmed=False,
        )
        submitted = service.batch_upload_products(
            run_id,
            seed_ids=[seed.seed_id],
            confirmed=True,
        )

        self.assertFalse(rejected.ok)
        self.assertTrue(submitted.ok, submitted.to_dict())
        self.assertEqual(1, submitted.data["submitted_product_count"])
        self.assertEqual("submitted", submitted.data["items"][0]["status"])
        self.assertEqual(1, len(adapter.imported_items))
        package_paths = list(
            (self.context.runtime_root / "image_tasks" / "pending").glob(
                "*.json"
            )
        )
        self.assertEqual(1, len(package_paths))
        package = json.loads(package_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(3, package["schema_version"])
        self.assertEqual("awaiting_ozon_product", package["store_target"]["binding_state"])

    def test_batch_upload_validates_and_submits_products_concurrently(self) -> None:
        run = self.repo.create_workbench_batch_record(target_count=2)
        run_id = run["run_id"]

        class ConcurrentSellerApiAdapter(FakeSellerApiAdapter):
            def __init__(self) -> None:
                super().__init__()
                self._lock = threading.Lock()
                self._active = 0
                self.max_active = 0
                self._next_task_id = 7100

            def import_products(self, items: list[dict]) -> dict:
                with self._lock:
                    self._active += 1
                    self.max_active = max(self.max_active, self._active)
                    self._next_task_id += 1
                    task_id = self._next_task_id
                time.sleep(0.05)
                with self._lock:
                    self.imported_items.extend(items)
                    self._active -= 1
                return {"task_id": task_id}

        adapter = ConcurrentSellerApiAdapter()
        service = WorkbenchService(self.repo, seller_api_adapter=adapter)
        preview_lock = threading.Lock()
        preview_active = 0
        preview_max_active = 0

        def preview(seed_id: str) -> Result:
            nonlocal preview_active, preview_max_active
            with preview_lock:
                preview_active += 1
                preview_max_active = max(
                    preview_max_active,
                    preview_active,
                )
            time.sleep(0.05)
            with preview_lock:
                preview_active -= 1
            return Result.success(
                "product_upload.preview_ready",
                "ready",
                {
                    "seed_id": seed_id,
                    "confirmation_token": f"token-{seed_id}",
                    "seller_api_item": {
                        "offer_id": f"offer-{seed_id}",
                        "name": f"Product {seed_id}",
                    },
                    "image_task_context": {},
                    "bootstrap_image": {},
                },
            )

        workspace = Result.success(
            "upload_workspace.loaded",
            "loaded",
            {
                "items": [
                    {"seed_id": "seed-a", "template_ready": True},
                    {"seed_id": "seed-b", "template_ready": True},
                ]
            },
        )
        with (
            patch.object(service, "upload_workspace", return_value=workspace),
            patch.object(
                service,
                "preview_product_upload",
                side_effect=lambda _run_id, seed_id: preview(seed_id),
            ),
        ):
            result = service.batch_upload_products(
                run_id,
                confirmed=True,
                max_workers=4,
            )

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(2, result.data["submitted_product_count"])
        self.assertGreaterEqual(preview_max_active, 2)
        self.assertGreaterEqual(adapter.max_active, 2)
        self.assertEqual(2, len(adapter.imported_items))

    def test_product_upload_ignores_legacy_generated_image_aspect_ratio(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        job = self.attach_completed_image_job(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        queue = ImageGenerationQueue(
            self.context.runtime_root / "image_generation" / "ozon_image_jobs.sqlite3"
        )
        first_slot = queue.list_slots(job["job_id"])[0]
        Image.new("RGB", (800, 800), "navy").save(first_slot["accepted_path"])
        service = WorkbenchService(
            self.repo,
            seller_api_adapter=FakeSellerApiAdapter(),
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )
        preview = service.preview_product_upload(run_id, seed.seed_id)

        self.assertTrue(preview.ok, preview.to_dict())
        self.assertEqual(
            [f"https://cbu01.alicdn.com/img/ibank/{seed.seed_id}.jpg"],
            preview.data["seller_api_item"]["images"],
        )

    def test_failed_ozon_submission_can_be_reprepared_and_retried(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_completed_image_job(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(
            self.repo,
            seller_api_adapter=adapter,
        )
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )
        preview = service.preview_product_upload(run_id, seed.seed_id)
        first = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=preview.data["confirmation_token"],
        )
        submissions = self.repo.load_upload_submissions(run_id)
        submissions["items"][seed.seed_id]["status"] = "failed"
        submissions["items"][seed.seed_id]["seller_api_status"] = {
            "items": [
                {
                    "status": "failed",
                    "errors": [
                        {"code": "currency_differs_from_contract"}
                    ],
                }
            ]
        }
        self.repo.save_upload_submissions(run_id, submissions)
        adapter.import_task_id = 7002
        second_preview = service.preview_product_upload(run_id, seed.seed_id)

        retried = service.submit_product_upload(
            run_id,
            seed.seed_id,
            confirmation_token=second_preview.data["confirmation_token"],
        )

        self.assertTrue(first.ok)
        self.assertTrue(retried.ok, retried.to_dict())
        self.assertEqual(7002, retried.data["task_id"])
        self.assertEqual(2, len(adapter.imported_items))
        self.assertEqual(1, len(retried.data["attempt_history"]))

    def test_upload_page_exposes_per_product_preview_and_confirm_controls(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        page = self.get_text(f"/batches/{run_id}/upload")

        self.assertIn("逐商品真实上传", page)
        self.assertIn("previewProductUpload", page)
        self.assertIn("confirmProductUpload", page)
        self.assertIn("确认上传到 Ozon", page)
        self.assertIn("预览单原图建品", page)
        self.assertIn("只有 Ozon 确认上传成功后才输出图片生成与上传任务包", page)
        self.assertNotIn('id="publicMediaBaseUrl"', page)
        self.assertNotIn("/api/settings/public-media", page)
        self.assertNotIn("图片门禁 (Image Gate)", page)
        self.assertNotIn(f"/batches/{run_id}/images", page)
        self.assertNotIn('id="buildDraft"', page)
        self.assertIn("pollProductUploadStatus", page)
        self.assertIn("scheduleProductUploadStatusRetry", page)
        self.assertIn("批量校验并上传可用商品", page)
        self.assertIn("batchUploadProducts", page)
        self.assertIn("/product-upload/batch", page)
        self.assertIn("补充技能无法确认的必填字段", page)
        self.assertIn("保存必填证据", page)
        self.assertIn("/required-attributes/", page)

    def test_public_media_configuration_is_not_a_user_setting(self) -> None:
        service = WorkbenchService(self.repo)

        self.assertFalse(hasattr(service, "configure_public_media_base_url"))
        self.assertFalse(hasattr(service, "public_media_configuration"))
        self.assertFalse(hasattr(self.repo, "load_public_media_settings"))

    def test_refresh_attribute_template_replaces_only_the_mismatched_product(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["seller_attribute_template"][
            "matched_category_path"
        ] = "Продукты питания / Соль, сахар, специи / Мак"
        self.repo.save_attribute_template_result(run_id, template_result)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(self.repo, seller_api_adapter=adapter)

        result = service.refresh_attribute_template(run_id, seed.seed_id)

        self.assertTrue(result.ok)
        saved = self.repo.load_attribute_template_result(run_id)["seed_templates"][0]
        self.assertEqual("Красота / Зеркала", saved["seller_attribute_template"]["matched_category_path"])
        self.assertEqual(1, adapter.resolve_count)
        self.assertEqual(seed.seed_id, result.data["seed_id"])

    def test_product_preview_self_heals_mismatched_category_template_once(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()
        template_result = self.repo.load_attribute_template_result(run_id)
        template_result["seed_templates"][0]["seller_attribute_template"][
            "matched_category_path"
        ] = "Продукты питания / Соль, сахар, специи / Мак"
        self.repo.save_attribute_template_result(run_id, template_result)
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_result["ozon_candidates"][0]["attributes"]["Цвет"] = "белый"
        self.repo.save_ozon_collection_result(run_id, ozon_result)
        self.attach_locked_subject_master(run_id, seed.seed_id)
        self.prepare_pricing_sources(run_id, seed.seed_id)
        adapter = FakeSellerApiAdapter()
        service = WorkbenchService(self.repo, seller_api_adapter=adapter)
        self.assertTrue(
            service.confirm_pricing_evidence(
                run_id,
                self.pricing_input_payload(seed.seed_id),
            ).ok
        )

        preview = service.preview_product_upload(run_id, seed.seed_id)

        self.assertTrue(preview.ok, preview.to_dict())
        self.assertEqual(seed.seed_id, preview.data["seed_id"])
        self.assertEqual(1, adapter.resolve_count)
        saved = self.repo.load_attribute_template_result(run_id)["seed_templates"][0]
        self.assertEqual(
            "Красота / Зеркала",
            saved["seller_attribute_template"]["matched_category_path"],
        )

    def test_operations_cockpit_pages_and_read_only_apis_use_runtime_data(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()
        pages = {
            "/batches": ("批次历史 (Batch History)", "batchHistoryContent"),
            "/products": ("商品资料库 (Product Library)", "productLibraryContent"),
            "/store": ("店铺授权 (Store Authorization)", "storeContent"),
            "/settings": ("系统设置 (System Settings)", "settingsContent"),
            "/diagnostics": ("诊断中心 (Diagnostics)", "diagnosticsContent"),
        }
        for path, (title, content_id) in pages.items():
            with self.subTest(path=path):
                page = self.get_text(path)
                self.assertIn(title, page)
                self.assertIn('class="app-shell"', page)
                self.assertIn('class="sidebar"', page)
                self.assertIn('class="topbar"', page)
                self.assertIn(f'id="{content_id}"', page)
                self.assertIn('href="/batches"', page)
                self.assertIn('href="/products"', page)
                self.assertIn('href="/store"', page)
                self.assertIn('href="/settings"', page)
                self.assertIn('href="/diagnostics"', page)

        history = self.get_json("/api/operations/batches")["data"]
        products = self.get_json("/api/operations/products")["data"]
        store = self.get_json("/api/operations/store")["data"]
        settings = self.get_json("/api/operations/settings")["data"]
        diagnostics = self.get_json("/api/operations/diagnostics")["data"]

        self.assertIn(run_id, {item["run_id"] for item in history["items"]})
        self.assertIn("Test product", {item["title"] for item in products["items"]})
        self.assertIn("credentials", store)
        self.assertTrue(settings["boundaries"]["publish_locked_by_default"])
        self.assertEqual("proxy_guest", settings["routes"]["ozon"])
        self.assertEqual("direct_user_session", settings["routes"]["1688"])
        self.assertIn("browser_bridge", diagnostics)
        self.assertIn("runtime", diagnostics)

    def test_existing_ozon_collected_batch_is_migrated_to_supplier_review(self) -> None:
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["zerkalo dlya makiyazha"],
            query_generation_status="generated",
        )
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [seed])
        self.repo.save_ozon_collection_result(run_id, {"ozon_candidates": [self.ozon_candidate_payload(seed)]})
        run["status"] = WorkbenchState.OZON_COLLECTED.value
        self.repo.save_run(run)

        loaded = self.get_json(f"/api/batches/{run_id}")

        self.assertEqual(WorkbenchState.SUPPLIER_REVIEW.value, loaded["data"]["run"]["status"])
        self.assertTrue((self.repo.run_dir(run_id) / "supplier_review.json").exists())

    def test_supplier_review_rejects_non_1688_product_link(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-review",
            {"links": [{"seed_id": seed.seed_id, "supplier_url": "https://s.1688.com/selloffer/offer_search.html"}]},
            ok=False,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("supplier_review.invalid_link", result["code"])
        self.assertEqual(WorkbenchState.SUPPLIER_REVIEW.value, self.repo.load_run(run_id)["status"])

    def test_supplier_review_saves_verified_1688_product_link(self) -> None:
        run_id, seed = self.prepare_supplier_review_run()

        result = self.post_json(
            f"/api/batches/{run_id}/supplier-review",
            {
                "links": [
                    {
                        "seed_id": seed.seed_id,
                        "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                    }
                ]
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual("supplier_review.links_saved", result["code"])
        item = self.repo.load_supplier_review(run_id)["items"][0]
        self.assertTrue(item["user_verified_exact_match"])
        self.assertEqual("https://detail.1688.com/offer/123456789012.html", item["supplier_url"])

    def test_supplier_collection_cannot_start_until_every_link_is_saved(self) -> None:
        run_id, _seed = self.prepare_supplier_review_run()

        result = self.post_json(f"/api/batches/{run_id}/supplier-collection", {}, ok=False)

        self.assertFalse(result["ok"])
        self.assertEqual("supplier_review.links_required", result["code"])
        self.assertEqual(WorkbenchState.SUPPLIER_REVIEW.value, self.repo.load_run(run_id)["status"])

    def test_active_browser_task_returns_latest_pending_task(self) -> None:
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["zerkalo dlya makiyazha"],
            query_generation_status="generated",
        )
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [seed])
        contract = CollectionContractService(self.repo).build_attribute_template_contract(run_id).data
        self.repo.save_attribute_template_contract(run_id, contract)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        run["attribute_template_contract_ready"] = True
        self.repo.save_run(run)

        task = self.get_json("/api/browser-task/active")

        self.assertEqual("browser_task.attribute_template_ready", task["code"])
        self.assertEqual(run_id, task["data"]["run_id"])

    def test_browser_bridge_script_is_served(self) -> None:
        body = self.get_text("/bridge/ozon-v2-browser-bridge.js")

        self.assertIn("OzonV2BrowserBridge", body)
        self.assertIn("workbench_browser_bridge", body)
        self.assertIn("/api/browser-task/active", body)

    def test_browser_bridge_heartbeat_status_is_visible(self) -> None:
        self.repo.browser_bridge_status_path.unlink(missing_ok=True)
        run_id = self.repo.create_workbench_batch_record(target_count=1)["run_id"]
        before = self.get_json("/api/browser-bridge/status")

        saved = self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "workbench_content_script",
                "run_id": run_id,
                "task_type": "ozon_attribute_template",
                "stage": "task_ready",
                "code": "browser_task.attribute_template_ready",
                "message": "Task is ready.",
                "extension_version": self.required_extension_version(),
            },
        )
        after = self.get_json("/api/browser-bridge/status")

        self.assertTrue(before["ok"])
        self.assertFalse(before["data"]["online"])
        self.assertTrue(saved["ok"])
        self.assertTrue(after["data"]["online"])
        self.assertEqual("workbench_content_script", after["data"]["source"])
        self.assertEqual(run_id, after["data"]["run_id"])
        self.assertEqual("task_ready", after["data"]["stage"])

    def test_browser_bridge_heartbeat_discards_deleted_batch_reference(self) -> None:
        saved = self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "workbench_content_script",
                "run_id": "wb-deleted-batch",
                "task_type": "ozon_attribute_template",
                "stage": "task_ready",
                "code": "browser_task.attribute_template_ready",
                "message": "Task is ready.",
                "extension_version": self.required_extension_version(),
            },
        )
        status = self.get_json("/api/browser-bridge/status")

        self.assertTrue(saved["ok"])
        self.assertIsNone(status["data"]["run_id"])
        self.assertIsNone(status["data"]["task_type"])
        self.assertEqual("stale_task_ignored", status["data"]["stage"])
        self.assertEqual("browser_task.stale_ignored", status["data"]["code"])

    def test_bridge_status_reports_loaded_required_and_ready_versions(self) -> None:
        required = self.required_extension_version()
        self.repo.save_browser_bridge_status(
            {
                "source": "background_interval",
                "extension_version": required,
                "stage": "idle",
                "code": "browser_task.none",
            }
        )

        status = self.get_json("/api/browser-bridge/status")["data"]

        self.assertEqual(required, status.get("loaded_extension_version"))
        self.assertEqual(required, status.get("required_extension_version"))
        self.assertIs(True, status.get("version_ready"))
        self.assertEqual("ready", status.get("readiness_code"))

    def test_connection_only_heartbeat_keeps_task_evidence_and_refreshes_liveness(self) -> None:
        required = self.required_extension_version()
        task_status = self.repo.save_browser_bridge_status(
            {
                "source": "ozon_content_script",
                "run_id": "wb-test",
                "task_type": "ozon_attribute_template",
                "stage": "detail_blocked",
                "code": "bridge.detail_blocked",
                "message": "Ozon product detail page is blocked by challenge.",
                "url": "https://www.ozon.ru/product/test-1/",
                "extension_version": required,
            }
        )
        task_status["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.repo.browser_bridge_status_path.write_text(
            json.dumps(task_status, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        saved = self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "background_alarm",
                "extension_version": required,
                "connection_only": True,
            },
        )
        status = self.get_json("/api/browser-bridge/status")["data"]

        self.assertTrue(saved["ok"])
        self.assertTrue(status["online"])
        self.assertEqual("detail_blocked", status["stage"])
        self.assertEqual("bridge.detail_blocked", status["code"])
        self.assertEqual("ozon_content_script", status["source"])
        self.assertEqual("background_alarm", status["connection_source"])
        self.assertIsNotNone(status.get("connection_updated_at"))

    def test_invalidated_old_page_heartbeat_cannot_replace_recent_compatible_background(self) -> None:
        required = self.required_extension_version()
        self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "background_interval",
                "extension_version": required,
                "stage": "idle",
                "code": "browser_task.none",
                "message": "Background bridge is healthy.",
            },
        )

        ignored = self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "workbench_content_script",
                "extension_version": "0.1.9",
                "stage": "workbench_poll_failed",
                "code": "browser_bridge.workbench_poll_failed",
                "message": "Extension context invalidated.",
            },
        )
        status = self.get_json("/api/browser-bridge/status")["data"]

        self.assertEqual("browser_bridge.heartbeat_ignored", ignored["code"])
        self.assertEqual("background_interval", status["source"])
        self.assertEqual(required, status.get("loaded_extension_version"))
        self.assertIs(True, status.get("version_ready"))

    def test_compatible_heartbeat_tolerates_one_minute_background_throttling(self) -> None:
        required = self.required_extension_version()
        saved = self.repo.save_browser_bridge_status(
            {
                "source": "background_interval",
                "extension_version": required,
                "stage": "idle",
                "code": "browser_task.none",
            }
        )
        throttled_at = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        saved["updated_at"] = throttled_at
        saved["connection_updated_at"] = throttled_at
        self.repo.browser_bridge_status_path.write_text(
            json.dumps(saved, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        status = self.get_json("/api/browser-bridge/status")["data"]

        self.assertTrue(status["online"])
        self.assertIs(True, status.get("version_ready"))
        self.assertEqual("ready", status.get("readiness_code"))

    def test_compatible_heartbeat_expires_after_ninety_seconds(self) -> None:
        required = self.required_extension_version()
        saved = self.repo.save_browser_bridge_status(
            {
                "source": "background_interval",
                "extension_version": required,
                "stage": "idle",
                "code": "browser_task.none",
            }
        )
        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=91)).isoformat()
        saved["updated_at"] = expired_at
        saved["connection_updated_at"] = expired_at
        self.repo.browser_bridge_status_path.write_text(
            json.dumps(saved, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        status = self.get_json("/api/browser-bridge/status")["data"]

        self.assertFalse(status["online"])
        self.assertIs(False, status.get("version_ready"))
        self.assertEqual("offline", status.get("readiness_code"))

    def test_rejected_browser_candidate_is_persisted_in_run_events(self) -> None:
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]

        self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "ozon_content_script",
                "run_id": run_id,
                "task_type": "ozon_attribute_template",
                "stage": "candidate_rejected",
                "code": "bridge.attribute_template.candidate_rejected",
                "message": "Candidate was rejected.",
                "url": "https://www.ozon.ru/product/rejected-1/",
                "details": {
                    "title": "Rejected product",
                    "decision": {
                        "is_chinese_domestic_seller": False,
                        "confidence": "high",
                        "signals": [{"kind": "local_warehouse_russia", "raw_text": "Со склада Ozon"}],
                    },
                },
            },
        )

        status = self.get_json("/api/browser-bridge/status")
        events = self.get_json(f"/api/batches/{run_id}/events")["data"]["events"]

        self.assertEqual("Rejected product", status["data"]["details"]["title"])
        self.assertEqual("browser_candidate.rejected", events[-1]["event_type"])
        self.assertEqual("https://www.ozon.ru/product/rejected-1/", events[-1]["data"]["url"])
        self.assertEqual("local_warehouse_russia", events[-1]["data"]["details"]["decision"]["signals"][0]["kind"])

    def test_exhausted_attribute_template_heartbeat_replaces_seed_and_continues(self) -> None:
        self.repo.initialize_runtime()
        rejected_seed = self.repo.load_active_seeds()[0]
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [rejected_seed])
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        run["sampled_seed_ids"] = [rejected_seed.seed_id]
        run["attribute_template_contract_ready"] = True
        self.repo.save_run(run)

        self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "ozon_content_script",
                "run_id": run_id,
                "task_type": "ozon_attribute_template",
                "stage": "no_cross_border_candidate",
                "code": "bridge.attribute_template.no_cross_border_candidate",
                "message": "No verified Chinese cross-border product was found.",
                "details": {"seed_id": rejected_seed.seed_id, "rejected_candidate_count": 8},
                "extension_version": self.required_extension_version(),
            },
        )

        deadline = time.time() + 3
        while time.time() < deadline and self.repo.load_sampled_seeds(run_id)[0].seed_id == rejected_seed.seed_id:
            time.sleep(0.05)
        replacement = self.repo.load_sampled_seeds(run_id)[0]
        event_types = [event.event_type for event in self.repo.load_run_events(run_id)]
        self.assertNotEqual(rejected_seed.seed_id, replacement.seed_id)
        self.assertIn("browser_candidate.exhausted", event_types)
        self.assertIn("seed_sampling.replaced_after_exhaustion", event_types)
        self.assertEqual(4999, len(self.repo.load_active_seeds()))
        self.assertIn(self.repo.seed_identity_key(replacement), self.repo.load_used_seed_identity_keys())
        progress = self.get_json(f"/api/batches/{run_id}")["data"]["progress"]["ozon_collection_progress"]
        self.assertEqual(0, progress["replacement_count"])
        self.assertEqual(0, progress["failure_count"])

    def test_exhausted_ozon_collection_heartbeat_replaces_seed_and_continues(self) -> None:
        self.repo.initialize_runtime()
        rejected_seed = self.repo.load_active_seeds()[0]
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [rejected_seed])
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        run["sampled_seed_ids"] = [rejected_seed.seed_id]
        run["attribute_template_collected"] = True
        run["ozon_collection_contract_ready"] = True
        self.repo.save_run(run)

        self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "ozon_content_script",
                "run_id": run_id,
                "task_type": "ozon_collection",
                "stage": "no_cross_border_candidate",
                "code": "bridge.ozon_collection.no_cross_border_candidate",
                "message": "No verified Chinese cross-border product was found.",
                "details": {"seed_id": rejected_seed.seed_id, "rejected_candidate_count": 8},
                "extension_version": self.required_extension_version(),
            },
        )

        deadline = time.time() + 3
        while time.time() < deadline and self.repo.load_sampled_seeds(run_id)[0].seed_id == rejected_seed.seed_id:
            time.sleep(0.05)
        replacement = self.repo.load_sampled_seeds(run_id)[0]
        loaded = self.repo.load_run(run_id)
        event_types = [event.event_type for event in self.repo.load_run_events(run_id)]
        self.assertNotEqual(rejected_seed.seed_id, replacement.seed_id)
        self.assertEqual([replacement.seed_id], loaded["replacement_pending_seed_ids"])
        self.assertFalse(loaded["ozon_collection_contract_ready"])
        self.assertIn("browser_candidate.exhausted", event_types)
        self.assertIn("seed_sampling.replaced_after_exhaustion", event_types)
        self.assertEqual(1, event_types.count("browser_candidate.replaced"))
        self.assertNotIn("browser_candidate.failed", event_types)
        self.assertEqual(4999, len(self.repo.load_active_seeds()))
        self.assertIn(self.repo.seed_identity_key(replacement), self.repo.load_used_seed_identity_keys())

        self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "ozon_content_script",
                "run_id": run_id,
                "task_type": "ozon_collection",
                "stage": "no_cross_border_candidate",
                "code": "bridge.ozon_collection.no_cross_border_candidate",
                "message": "Duplicate stale exhausted heartbeat.",
                "details": {"seed_id": rejected_seed.seed_id, "rejected_candidate_count": 8},
                "extension_version": self.required_extension_version(),
            },
        )
        outcomes = [
            event
            for event in self.repo.load_run_events(run_id)
            if event.event_type in {"browser_candidate.replaced", "browser_candidate.failed"}
        ]
        self.assertEqual(1, len(outcomes))
        self.assertEqual("browser_candidate.replaced", outcomes[0].event_type)
        self.assertEqual("ozon_collection", outcomes[0].data["task_type"])

    def test_exhausted_ozon_seed_without_replacement_is_one_final_failure(self) -> None:
        self.repo.initialize_runtime()
        active = self.repo.load_active_seeds()
        rejected_seed = active[0]
        self.repo.append_used_seeds("wb-other", active, "used")
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [rejected_seed])
        run = self.repo.load_run(run_id)
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        run["sampled_seed_ids"] = [rejected_seed.seed_id]
        run["attribute_template_collected"] = True
        run["ozon_collection_contract_ready"] = True
        self.repo.save_run(run)
        heartbeat = {
            "source": "ozon_content_script",
            "run_id": run_id,
            "task_type": "ozon_collection",
            "stage": "no_cross_border_candidate",
            "code": "bridge.ozon_collection.no_cross_border_candidate",
            "message": "No verified Chinese cross-border product was found.",
            "details": {"seed_id": rejected_seed.seed_id, "rejected_candidate_count": 8},
            "extension_version": self.required_extension_version(),
        }

        self.post_json("/api/browser-bridge/heartbeat", heartbeat)
        self.post_json("/api/browser-bridge/heartbeat", heartbeat)

        failures = [
            event
            for event in self.repo.load_run_events(run_id)
            if event.event_type == "browser_candidate.failed"
        ]
        self.assertEqual(1, len(failures))
        self.assertEqual("ozon_collection", failures[0].data["task_type"])
        self.assertEqual("workbench.exhausted_seed_no_replacement", failures[0].data["result_code"])
        progress = self.get_json(f"/api/batches/{run_id}")["data"]["progress"]["ozon_collection_progress"]
        self.assertEqual(1, progress["failure_count"])
        self.assertEqual(1, progress["processed_count"])

    def test_ozon_collection_progress_merges_live_and_scoped_outcomes(self) -> None:
        self.repo.initialize_runtime()
        seeds = self.repo.load_active_seeds()[:5]
        run = self.repo.create_workbench_batch_record(target_count=5)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, seeds)
        self.repo.save_browser_bridge_status(
            {
                "source": "ozon_content_script",
                "run_id": run_id,
                "task_type": "ozon_collection",
                "details": {
                    "collection_progress": {
                        "total_count": 5,
                        "processed_count": 2,
                        "success_count": 2,
                        "failure_count": 0,
                        "replacement_count": 0,
                        "pending_count": 3,
                    }
                },
            }
        )
        self.repo.append_run_event(
            run_id,
            "browser_candidate.replaced",
            "replaced",
            {"seed_id": "old-1", "task_type": "ozon_collection"},
        )
        self.repo.append_run_event(
            run_id,
            "browser_candidate.replaced",
            "duplicate",
            {"seed_id": "old-1", "task_type": "ozon_collection"},
        )
        self.repo.append_run_event(
            run_id,
            "browser_candidate.failed",
            "stale opposite",
            {"seed_id": "old-1", "task_type": "ozon_collection"},
        )
        self.repo.append_run_event(
            run_id,
            "browser_candidate.replaced",
            "attribute replacement",
            {"seed_id": "attribute-old", "task_type": "ozon_attribute_template"},
        )

        progress = self.get_json(f"/api/batches/{run_id}")["data"]["progress"]["ozon_collection_progress"]

        self.assertEqual(
            {
                "total_count": 5,
                "processed_count": 2,
                "success_count": 2,
                "failure_count": 0,
                "replacement_count": 1,
                "pending_count": 3,
            },
            progress,
        )

    def test_ozon_collection_progress_ignores_stale_status_and_final_result_wins(self) -> None:
        self.repo.initialize_runtime()
        seeds = self.repo.load_active_seeds()[:3]
        run = self.repo.create_workbench_batch_record(target_count=3)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, seeds)
        self.repo.save_browser_bridge_status(
            {
                "source": "ozon_content_script",
                "run_id": "wb-stale",
                "task_type": "ozon_collection",
                "details": {"collection_progress": {"success_count": 99}},
            }
        )
        progress = self.get_json(f"/api/batches/{run_id}")["data"]["progress"]["ozon_collection_progress"]
        self.assertEqual(0, progress["success_count"])
        self.assertEqual(3, progress["pending_count"])

        self.repo.save_browser_bridge_status(
            {
                "source": "ozon_content_script",
                "run_id": run_id,
                "task_type": "ozon_attribute_template",
                "details": {"collection_progress": {"success_count": 99}},
            }
        )
        progress = self.get_json(f"/api/batches/{run_id}")["data"]["progress"]["ozon_collection_progress"]
        self.assertEqual(0, progress["success_count"])

        self.repo.save_browser_bridge_status(
            {
                "source": "ozon_content_script",
                "run_id": run_id,
                "task_type": "ozon_collection",
                "details": {"collection_progress": {"success_count": 99}},
            }
        )
        self.repo.save_ozon_collection_result(run_id, {"run_id": run_id, "ozon_candidates": [{}, {}]})
        progress = self.get_json(f"/api/batches/{run_id}")["data"]["progress"]["ozon_collection_progress"]
        self.assertEqual(2, progress["success_count"])
        self.assertEqual(2, progress["processed_count"])
        self.assertEqual(1, progress["pending_count"])

    def test_store_binding_api_saves_credentials_without_echoing_secret(self) -> None:
        secret = "seller-api-key-super-secret"

        result = self.post_json("/api/store-binding", {"client_id": "client-123", "api_key": secret})

        self.assertTrue(result["ok"])
        self.assertEqual("store_binding.bound", result["code"])
        self.assertEqual("client-123", result["data"]["client_id"])
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))

    def test_autopilot_endpoint_runs_until_store_binding_block(self) -> None:
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]

        result = self.post_json(f"/api/batches/{run_id}/autopilot", {"max_steps": 5})

        self.assertTrue(result["ok"])
        self.assertEqual("autopilot.blocked", result["code"])
        self.assertEqual("store_binding_required", result["data"]["blocked_reason"])
        self.assertEqual(WorkbenchState.NEEDS_CREDENTIALS.value, self.repo.load_run(run_id)["status"])

    def test_supervised_action_is_rejected_while_runner_is_active(self) -> None:
        service = WorkbenchService(self.repo)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(self.repo, FakeBusyRunner(service)),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        original_status = run["status"]
        request = Request(
            base_url + f"/api/batches/{run_id}/actions",
            data=json.dumps({"action": WorkbenchAction.CHECK_CREDENTIALS.value}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=5)
            result = json.loads(caught.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertFalse(result["ok"])
        self.assertEqual("runner.busy", result["code"])
        self.assertEqual(original_status, self.repo.load_run(run_id)["status"])

    def test_publish_request_is_locked_at_http_boundary(self) -> None:
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        run["status"] = WorkbenchState.DRAFT_READY.value
        self.repo.save_run(run)

        result = self.post_json(f"/api/batches/{run_id}/actions", {"action": WorkbenchAction.REQUEST_PUBLISH.value}, ok=False)

        self.assertFalse(result["ok"])
        self.assertEqual("publish.locked", result["code"])
        self.assertEqual(WorkbenchState.DRAFT_READY.value, self.repo.load_run(run_id)["status"])

    def get_json(self, path: str) -> dict:
        with urlopen(self.base_url + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def required_extension_version(self) -> str:
        manifest_path = self.project_root / "browser_extension" / "ozon_v2_bridge" / "manifest.json"
        return str(json.loads(manifest_path.read_text(encoding="utf-8"))["version"])

    def assert_batch_rejected_without_side_effects(self, expected_code: str) -> dict:
        active_before = self.repo.active_seed_path.read_bytes()
        used_before = self.repo.used_seed_path.read_bytes()
        run_dirs_before = sorted(path.name for path in self.repo.runs_dir.iterdir() if path.is_dir())

        result = self.post_json("/api/batches", {"target_count": 1}, ok=False)

        self.assertFalse(result["ok"])
        self.assertEqual(expected_code, result["code"])
        self.assertEqual(active_before, self.repo.active_seed_path.read_bytes())
        self.assertEqual(used_before, self.repo.used_seed_path.read_bytes())
        self.assertEqual(
            run_dirs_before,
            sorted(path.name for path in self.repo.runs_dir.iterdir() if path.is_dir()),
        )
        return result

    def post_json(self, path: str, payload: dict, ok: bool = True) -> dict:
        supplier_capture_match = re.fullmatch(
            r"/api/batches/([^/]+)/supplier-selection/capture",
            path,
        )
        if supplier_capture_match and "dispatch_token" not in payload:
            task = self.get_json(
                f"/api/batches/{supplier_capture_match.group(1)}/browser-task"
            )
            payload = {
                **payload,
                "dispatch_token": task["data"]["dispatch_token"],
            }
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if ok:
                raise
            return json.loads(exc.read().decode("utf-8"))

    def get_text(self, path: str) -> str:
        with urlopen(self.base_url + path, timeout=5) as response:
            return response.read().decode("utf-8")

    def wait_for_runner_idle(self, run_id: str) -> dict:
        deadline = time.time() + 5
        while time.time() < deadline:
            result = self.get_json(f"/api/batches/{run_id}/runner")
            status = result["data"]["runner"]
            if not status["running"]:
                return status
            time.sleep(0.05)
        raise AssertionError("runner did not finish before timeout")

    def ozon_candidate_payload(self, seed: SeedProduct) -> dict:
        return {
            "seed_id": seed.seed_id,
            "seed_title_or_keyword": seed.title_or_keyword,
            "seed_source_language": seed.source_language,
            "ozon_query_terms_ru": seed.ozon_query_terms_ru,
            "ozon_product_id": "ozon-1",
            "ozon_url": "https://www.ozon.ru/product/test-123/",
            "title": "Test product",
            "seller_name": "Test seller",
            "seller_url": "https://www.ozon.ru/seller/test-seller-1/",
            "seller_evidence": {"raw_text": "Доставка из Китая"},
            "target_sku": {"sku_id": "ozon-sku-1", "selected_options": {"single_sku": "visible"}},
            "selected_sku_media": {
                "main_gallery_images": ["https://img.example/main.jpg"],
                "selected_sku_images": ["https://img.example/main.jpg"],
            },
            "category_path": "Office / Paper trays",
            "leaf_category": "Paper trays",
            "category_url": "https://www.ozon.ru/category/paper-trays-123/",
            "category_id": "123",
            "price": "799",
            "currency": "RUB",
            "rating": "4.9",
            "review_count": 120,
            "delivery_origin": "Доставка из Китая",
            "delivery_time": "15-30 дней",
            "fulfillment_label": "Ozon Global",
            "attributes": {"Material": "plastic"},
            "content_score_evidence": {
                "title_raw": "Test product",
                "attribute_table": {"Material": "plastic"},
                "main_gallery_images": ["https://img.example/main.jpg"],
                "missing_fields": {"monthly_sales": "external analytics unavailable"},
            },
            "domestic_seller_decision": {
                "is_chinese_domestic_seller": True,
                "confidence": "high",
                "signals": [
                    {
                        "kind": "delivery_origin_china",
                        "raw_text": "Доставка из Китая",
                    }
                ],
            },
        }

    def ozon_candidate_for_seed(self, seed: SeedProduct, product_id: str) -> dict:
        candidate = self.ozon_candidate_payload(seed)
        image_url = f"https://img.example/{product_id}.jpg"
        candidate["ozon_product_id"] = product_id
        candidate["ozon_url"] = f"https://www.ozon.ru/product/{product_id}/"
        candidate["title"] = f"Test product {product_id}"
        candidate["target_sku"]["sku_id"] = f"sku-{product_id}"
        candidate["selected_sku_media"]["main_gallery_images"] = [image_url]
        candidate["selected_sku_media"]["selected_sku_images"] = [image_url]
        candidate["content_score_evidence"]["title_raw"] = candidate["title"]
        candidate["content_score_evidence"]["main_gallery_images"] = [image_url]
        return candidate

    def prepare_ozon_collecting_run(self, count: int = 2) -> tuple[str, list[SeedProduct]]:
        seeds = [
            SeedProduct(
                seed_id=f"seed-progress-{index}",
                title_or_keyword=f"test product {index}",
                product_clue=f"test product {index}",
                ozon_query_terms_ru=[f"тестовый товар {index}"],
                query_generation_status="generated",
            )
            for index in range(1, count + 1)
        ]
        run = self.repo.create_workbench_batch_record(target_count=count)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, seeds)
        contract = CollectionContractService(self.repo).build_ozon_collection_contract(run_id)
        self.assertTrue(contract.ok)
        self.repo.save_ozon_collection_contract(run_id, contract.data)
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        run["ozon_collection_contract_ready"] = True
        run["ozon_collection_contract_path"] = str(
            self.repo.run_dir(run_id) / "ozon_collection_contract.json"
        )
        self.repo.save_run(run)
        return run_id, seeds

    def prepare_supplier_review_run(self) -> tuple[str, SeedProduct]:
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["zerkalo dlya makiyazha"],
            query_generation_status="generated",
        )
        run = self.repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        self.repo.save_sampled_seeds(run_id, [seed])
        self.repo.save_attribute_template_result(run_id, self.seller_attribute_template_payload(run_id, seed.seed_id))
        run["status"] = WorkbenchState.OZON_COLLECTING.value
        self.repo.save_run(run)
        result = WorkbenchService(self.repo).ingest_ozon_collection_result(
            run_id,
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test",
                "ozon_candidates": [self.ozon_candidate_payload(seed)],
            },
        )
        self.assertTrue(result.ok)
        return run_id, seed

    def supplier_product_payload(self, seed_id: str) -> dict:
        supplier_url = "https://detail.1688.com/offer/123456789012.html"
        return {
            "seed_id": seed_id,
            "supplier_url": supplier_url,
            "title": "Test supplier product",
            "seller": {"shop_name": "Test supplier"},
            "sku": {"selected_options": {"visible_sku_labels": ["黑色"]}},
            "offer_id": "123456789012",
            "sku_options": [
                {
                    "supplier_sku_id": "sku-black-1",
                    "combination_key": "black-1",
                    "raw_label": "Черный, 1 шт.",
                    "selected_options": {"颜色": "Черный", "数量": "1"},
                    "set_quantity": 1,
                    "set_composition": ["Черный", "1 шт."],
                    "price": {"currency": "CNY", "amount": "12.80"},
                    "stock": {"status": "in_stock", "quantity": 100},
                    "image_urls": ["https://cbu01.alicdn.com/img/ibank/test.jpg"],
                    "evidence_source": "trusted_sku_map",
                    "complete": True,
                    "evidence": {"source": "trusted_sku_map"},
                }
            ],
            "images": ["https://cbu01.alicdn.com/img/ibank/test.jpg"],
            "price": {"currency": "CNY", "visible_text": "¥12.80"},
            "attributes": {"颜色": "Черный"},
            "domestic_shipping_evidence": {
                "visible_text": "包邮",
                "fee": 0,
                "free_shipping_visible": True,
            },
        }

    def seller_attribute_template_payload(self, run_id: str, seed_id: str) -> dict:
        return {
            "run_id": run_id,
            "worker": "workbench_browser_bridge",
            "source": "test",
            "schema_resolution": {
                "source": "ozon_seller_api_description_category_attribute",
                "public_page_schema_is_evidence_only": True,
            },
            "seed_templates": [
                {
                    "seed_id": seed_id,
                    "category_candidates": [
                        {
                            "category_path": "Красота / Зеркала",
                            "leaf_category": "Зеркала",
                            "category_url": "https://www.ozon.ru/category/zerkala/",
                            "category_id": "17000001",
                        }
                    ],
                    "public_attribute_evidence": {
                        "attribute_table": {"Цвет": "белый"},
                    },
                    "seller_attribute_template": {
                        "source": "ozon_seller_api_description_category_attribute",
                        "description_category_id": 17000001,
                        "type_id": 970001,
                        "matched_category_path": "Красота / Зеркала",
                        "match_confidence": "high",
                    },
                    "upload_attribute_schema": [
                        {
                            "attribute_id": "85",
                            "attribute_label": "Цвет",
                            "attribute_type": "dictionary",
                            "is_required": True,
                            "schema_source": "ozon_seller_api_description_category_attribute",
                        }
                    ],
                    "draft_prefill_plan": [
                        {
                            "field_key": "85",
                            "source": "seller_api_template_plus_supplier_or_ozon_objective_fact",
                            "prefill_allowed": True,
                            "rewrite_required": False,
                        }
                    ],
                }
            ],
        }
