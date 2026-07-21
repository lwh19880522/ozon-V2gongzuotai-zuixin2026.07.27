from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import SeedProduct
from ozon_v2.domain.models import WorkbenchAction, WorkbenchState
from ozon_v2.domain.supplier_sku import SupplierSkuOption, SupplierSkuSelectionReceipt
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.queue import ImageGenerationQueue
from ozon_v2.services.collection_contract_service import CollectionContractService
from ozon_v2.services.workbench_service import WorkbenchService
from ozon_v2.workbench.local_server import create_handler

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
        self.assertIn("系统事件（查看原始信息）", body)
        self.assertIn("event.event_type", body)
        self.assertIn("textContent", body)
        self.assertIn('id="batchOverviewNav"', body)
        self.assertIn('id="supplierReviewNav"', body)
        self.assertIn('new URLSearchParams(window.location.search).get("run_id")', body)
        self.assertIn("供应商审核 (Supplier Review)", body)
        self.assertIn("图片处理 (Images)", body)
        self.assertIn("上传草稿 (Upload)", body)
        self.assertIn('supplierNav.href = `/batches/${encodeURIComponent(runId)}/supplier-review`;', body)
        self.assertIn('const AUTO_ADVANCE_RUN_KEY = "ozon_v2_auto_advance_run_id";', body)
        self.assertIn("localStorage.setItem(AUTO_ADVANCE_RUN_KEY, state.runId);", body)
        self.assertIn('localStorage.getItem(AUTO_ADVANCE_RUN_KEY) === runId', body)
        self.assertIn("localStorage.removeItem(AUTO_ADVANCE_RUN_KEY);", body)
        self.assertIn('window.location.assign(`/batches/${encodeURIComponent(runId)}/supplier-review`);', body)
        self.assertIn('$("startBatch").disabled = !ready;', body)
        self.assertIn("请在 Edge 扩展页面重新加载", body)
        self.assertNotIn("宸ュ叿", body)
        self.assertNotIn("鍏嶈垂", body)

    def test_home_page_contains_stage_specific_ozon_restart_control(self) -> None:
        page = self.get_text("/")

        self.assertIn('id="restartOzonCollection"', page)
        self.assertIn("重新启动 Ozon 原商品采集", page)
        self.assertIn("已完成商品及其原始属性字段不会重复采集", page)
        self.assertIn("/browser-task/restart", page)
        self.assertIn('status === "ozon_collecting"', page)

    def test_runtime_status_reports_service_extension_and_task(self) -> None:
        result = self.get_json("/api/runtime/status")

        self.assertTrue(result["ok"])
        self.assertEqual("online", result["data"]["service"]["code"])
        self.assertEqual("ready", result["data"]["extension"]["code"])
        self.assertIn(
            result["data"]["task"]["code"],
            {"idle", "running", "blocked", "stopped", "failed"},
        )

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

    def test_clear_all_batches_removes_workbench_runs_and_resets_bridge_task(self) -> None:
        first = self.repo.create_workbench_batch_record(target_count=1)
        second = self.repo.create_workbench_batch_record(target_count=2)
        legacy_dir = self.repo.run_dir("run-legacy-keep")
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "run.json").write_text(
            json.dumps({"run_id": "run-legacy-keep", "status": "finalized"}),
            encoding="utf-8",
        )
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
        dedupe_before = self.repo.existing_store_dedupe_path.read_bytes()

        result = self.post_json("/api/batches/clear", {"confirm": True})

        self.assertEqual("workbench.batches_cleared", result["code"])
        self.assertEqual({first["run_id"], second["run_id"]}, set(result["data"]["deleted_run_ids"]))
        self.assertFalse(self.repo.run_dir(first["run_id"]).exists())
        self.assertFalse(self.repo.run_dir(second["run_id"]).exists())
        self.assertTrue(legacy_dir.exists())
        self.assertEqual(seeds_before, self.repo.active_seed_path.read_bytes())
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
        self.assertEqual([], item["supplier_sku_options"])
        self.assertEqual(WorkbenchState.SUPPLIER_COLLECTED.value, self.repo.load_run(run_id)["status"])

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
        self.assertEqual("0.1.59", manifest["version"])

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
        self.assertIn("图片处理 (Images)", page)
        self.assertIn("上传草稿 (Upload)", page)
        self.assertNotIn('window.setTimeout(() => window.location.assign("/"), 800);', page)
        self.assertEqual(seed.seed_id, row["seed_id"])
        self.assertEqual("Test product", row["ozon_title"])
        self.assertEqual("https://img.example/main.jpg", row["ozon_main_image"])
        self.assertEqual({"single_sku": "visible"}, row["selected_options"])
        self.assertEqual("https://www.ozon.ru/product/test-123/", row["ozon_url"])

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

        self.assertIn('id="skuDecisionPanel"', page)
        self.assertIn("锁定真实 SKU (Lock Real SKU)", page)
        self.assertIn("Ozon 原商品目标", page)
        self.assertIn("1688 可采购规格", page)
        self.assertIn("页面只有一个真实 SKU，无需选择规格", page)
        self.assertIn("确认页面唯一 SKU", page)
        self.assertIn("确认所选 SKU", page)
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
        self.assertIn("source_image_urls:selectedUrls", page)
        self.assertNotIn('id="whiteBackgroundConfirmed"', page)
        self.assertNotIn("White Background Confirmed", page)
        self.assertIn("/supplier-sku", page)
        self.assertIn("/subject-master", page)
        self.assertEqual(1, len(review["data"]["items"][0]["supplier_sku_options"]))
        self.assertTrue(selected["ok"])
        self.assertEqual("supplier-sku-set-x4", selected["data"]["receipt"]["supplier_sku_id"])
        self.assertFalse(rejected_subject["ok"])
        self.assertEqual("subject_master.image_not_in_supplier", rejected_subject["code"])

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

        self.assertIn("图片处理 (Images)", page)
        self.assertIn('class="app-shell"', page)
        self.assertIn('class="sidebar"', page)
        self.assertIn('class="topbar"', page)
        self.assertIn('class="workspace"', page)
        self.assertIn('id="imageProcessingNav"', page)
        self.assertIn('id="imageItems"', page)
        self.assertIn('id="previousImageItem"', page)
        self.assertIn('id="nextImageItem"', page)
        self.assertIn('id="imageItemPosition"', page)
        self.assertIn('class="image-items image-items-viewport"', page)
        self.assertIn("function renderImageItemAt(index)", page)
        self.assertNotIn("items.forEach((item) =>", page)
        self.assertIn("images.forEach((src) =>", page)
        self.assertNotIn("images.slice(0, 6)", page)
        self.assertIn(".image-product { height:100%;", page)
        self.assertIn("grid-template-rows:auto minmax(0,1fr)", page)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", page)
        self.assertIn("overflow-y:auto", page)
        self.assertIn('id="imageGate"', page)
        self.assertIn('aria-current="page"', page)
        self.assertIn("Ozon 参考图 (Ozon Reference)", page)
        self.assertIn("供应商原图 (Supplier Source)", page)
        self.assertIn('id="imageJobControls"', page)
        self.assertIn(".generated-review-card", page)
        self.assertIn('checkbox.type = "checkbox"', page)
        self.assertIn('select.className = "repair-issue"', page)
        self.assertIn('textarea.className = "repair-note"', page)
        self.assertIn('id="submitImageRepairs"', page)
        self.assertIn("提交选中图片返修 (Repair Selected)", page)
        self.assertIn("issue_code: issue.value", page)
        self.assertIn("review_issue_code", page)
        self.assertIn("/repair`,", page)
        self.assertIn("slot.attempt_count", page)
        self.assertIn("slot.review_requested_at", page)
        self.assertNotIn(
            'sources.append(sourcePanel("生成结果 (Generated)", generatedImageUrls(item)',
            page,
        )
        self.assertIn('id="imageControllerCommand"', page)
        self.assertIn('id="copyImageControllerCommand"', page)
        self.assertIn("skills/ozon-image-generation-controller/SKILL.md", page)
        for index in range(1, 6):
            self.assertIn(f"ozon-image-worker-{index:02d}", page)
        self.assertNotIn("ozon-image-worker-06", page)
        self.assertIn("spawn_agent", page)
        self.assertIn("create_thread", page)
        self.assertIn("\u6700\u591a 5 \u4e2a\u52a8\u6001 Codex \u751f\u56fe\u5b50\u667a\u80fd\u4f53", page)
        self.assertIn("\u6bcf\u8f6e\u65b0\u589e\u6570\u91cf\u53d6", page)
        self.assertIn("\u4e0d\u5f97\u56e0\u6ca1\u6709\u65b0\u589e\u7a7a\u4f4d\u800c\u505c\u6b62\u73b0\u6709\u5b50\u667a\u80fd\u4f53", page)
        self.assertIn("\u53ef\u7528\u5e76\u53d1\u4f4d\u5c11\u4e8e 5 \u65f6\u7ee7\u7eed", page)
        self.assertIn("\u4e0d\u5f97\u9000\u56de create_thread", page)
        self.assertNotIn("5 \u4e2a\u56fa\u5b9a Codex \u751f\u56fe\u5b50\u667a\u80fd\u4f53", page)
        self.assertNotIn("5 \u4e2a\u56fa\u5b9a\u751f\u56fe\u5de5\u4f5c\u4efb\u52a1", page)
        self.assertNotIn("Codex \u751f\u56fe\u7ebf\u7a0b", page)
        self.assertIn("\u4e0d\u5f97\u521b\u5efa\u7b2c 6 \u4e2a\u5e38\u89c4\u751f\u56fe worker", page)
        self.assertIn("\u4fdd\u7559 1 \u4e2a\u5b50\u667a\u80fd\u4f53\u4f4d\u7f6e\u7528\u4e8e\u5931\u8d25\u6062\u590d\u3001\u8bca\u65ad\u6216\u4eba\u5de5\u4ecb\u5165", page)
        self.assertIn(run_id, page)
        self.assertIn("navigator.clipboard.writeText", page)
        self.assertIn('document.execCommand("copy")', page)
        self.assertIn("停止生图 (Stop Generation)", page)
        self.assertIn("继续生图 (Resume Generation)", page)
        self.assertIn("/image-job/", page)
        self.assertEqual(["https://img.example/main.jpg"], item["ozon_reference_images"])
        self.assertEqual(
            [
                "https://cbu01.alicdn.com/img/ibank/product-a_!!111-0-cib.jpg_.webp",
                "https://cbu01.alicdn.com/img/ibank/product-b_!!111-0-cib.jpg_.webp",
            ],
            item["supplier_source_images"],
        )
        self.assertEqual("waiting_for_supplier_sku", item["generation_status"])
        self.assertFalse(workspace["data"]["image_gate"]["ready"])
        self.assertEqual("supplier_sku_selection_required", workspace["data"]["image_gate"]["code"])

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
        self.assertIn('id="buildDraft"', page)
        self.assertIn("发布锁已开启 (Publish Lock Active)", page)
        self.assertIn('id="buildDraft" class="primary" disabled', page)
        self.assertEqual(seed.seed_id, item["seed_id"])
        self.assertEqual(1, item["required_attribute_count"])
        self.assertEqual(1, item["prefill_plan_count"])
        self.assertFalse(workspace["data"]["gates"]["images_ready"])
        self.assertFalse(workspace["data"]["gates"]["draft_ready"])
        self.assertTrue(workspace["data"]["gates"]["publish_locked"])

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
        before = self.get_json("/api/browser-bridge/status")

        saved = self.post_json(
            "/api/browser-bridge/heartbeat",
            {
                "source": "workbench_content_script",
                "run_id": "wb-test",
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
        self.assertEqual("wb-test", after["data"]["run_id"])
        self.assertEqual("task_ready", after["data"]["stage"])

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
        self.assertEqual(2000, len(self.repo.load_active_seeds()))
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
        self.assertEqual(2000, len(self.repo.load_active_seeds()))

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




