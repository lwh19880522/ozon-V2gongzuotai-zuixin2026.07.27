from __future__ import annotations

import threading
import time

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.app.result import Result
from ozon_v2.domain.models import SeedProduct, WorkbenchState
from ozon_v2.services.workbench_service import WorkbenchService
from ozon_v2.workbench.runner import WorkbenchBackgroundRunner

from tests.helpers import FakeSellerApiAdapter, RuntimeTestCase


class WorkbenchBackgroundRunnerTests(RuntimeTestCase):
    def test_runner_runs_until_store_binding_block_without_chat_command(self) -> None:
        repo = FsRepo(self.context)
        service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())
        runner = WorkbenchBackgroundRunner(service)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = runner.start(run_id)
        status = self.wait_until_idle(runner, run_id)
        events = [event.event_type for event in repo.load_run_events(run_id)]

        self.assertTrue(result.ok)
        self.assertEqual("runner.started", result.code)
        self.assertFalse(status["running"])
        self.assertEqual("blocked", status["state"])
        self.assertEqual("store_binding_required", status["blocked_reason"])
        self.assertIn("runner.started", events)
        self.assertIn("autopilot.blocked", events)
        self.assertIn("runner.blocked", events)

    def test_runner_blocks_retired_template_collection_without_invoking_worker(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["зеркало для макияжа"],
            query_generation_status="generated",
        )
        repo.save_active_seeds([seed])
        service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        run["attribute_template_contract_ready"] = True
        run["candidate_slots"] = [
            {
                "slot_id": "slot-0001",
                "seed_id": seed.seed_id,
                "candidate_revision": 1,
                "ozon_product_id": "ozon-runner-1",
            }
        ]
        repo.save_run(run)
        repo.save_sampled_seeds(run_id, [seed])
        repo.save_ozon_collection_result(
            run_id,
            {
                "run_id": run_id,
                "ozon_candidates": [
                    {
                        "seed_id": seed.seed_id,
                        "ozon_product_id": "ozon-runner-1",
                        "slot_id": "slot-0001",
                        "candidate_revision": 1,
                    }
                ],
            },
        )
        worker = FakeAttributeTemplateWorker(run_id, seed.seed_id)
        runner = WorkbenchBackgroundRunner(service, attribute_template_worker=worker)

        result = runner.start(run_id)
        status = self.wait_until_idle(runner, run_id)
        events = [event.event_type for event in repo.load_run_events(run_id)]

        self.assertTrue(result.ok)
        self.assertEqual(0, worker.collect_count)
        self.assertFalse(status["running"])
        self.assertEqual("blocked", status["state"])
        self.assertEqual("legacy_batch_restart_required", status["blocked_reason"])
        self.assertFalse((repo.run_dir(run_id) / "attribute_template_result.json").exists())
        self.assertNotIn("attribute_template.ingested", events)

    def test_runner_blocks_retired_collected_template_batch(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(
            seed_id="seed-test",
            title_or_keyword="makeup mirror",
            product_clue="makeup mirror",
            ozon_query_terms_ru=["зеркало для макияжа"],
            query_generation_status="generated",
        )
        service = WorkbenchService(repo)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED.value
        repo.save_run(run)
        repo.save_sampled_seeds(run_id, [seed])
        runner = WorkbenchBackgroundRunner(service, attribute_template_worker=FailingAttributeTemplateWorker())

        runner.start(run_id)
        status = self.wait_until_idle(runner, run_id)

        self.assertEqual("blocked", status["state"])
        self.assertEqual("legacy_batch_restart_required", status["blocked_reason"])
        self.assertIn("retired", status["message"])

    def test_runner_stops_for_collection_review_after_supplier_collection(self) -> None:
        repo = FsRepo(self.context)
        service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())
        run = repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        run["status"] = WorkbenchState.SUPPLIER_COLLECTING.value
        repo.save_run(run)
        repo.save_supplier_collection_contract(
            run_id,
            {
                "run_id": run_id,
                "network": {"mode": "direct", "proxy_disabled": True},
                "items": [
                    {
                        "seed_id": "seed-test",
                        "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                        "user_verified_exact_match": True,
                    }
                ],
            },
        )
        worker = FakeSupplierWorker(run_id)
        runner = WorkbenchBackgroundRunner(service, supplier_worker=worker)

        runner.start(run_id)
        status = self.wait_until_idle(runner, run_id)

        self.assertEqual(1, worker.collect_count)
        self.assertEqual("blocked", status["state"])
        self.assertEqual("supplier_sku_selection_required", status["blocked_reason"])
        self.assertEqual(WorkbenchState.SUPPLIER_COLLECTED.value, repo.load_run(run_id)["status"])
        self.assertTrue((repo.run_dir(run_id) / "supplier_collection_result.json").exists())

    def test_runner_stop_marks_batch_as_user_stopped(self) -> None:
        repo = FsRepo(self.context)
        service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())
        runner = WorkbenchBackgroundRunner(service)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = runner.stop(run_id)
        status = runner.status(run_id)
        events = [event.event_type for event in repo.load_run_events(run_id)]

        self.assertTrue(result.ok)
        self.assertEqual("runner.stopped", result.code)
        self.assertFalse(status["running"])
        self.assertEqual("stopped", status["state"])
        self.assertEqual("user_stopped", status["blocked_reason"])
        self.assertTrue(status["stop_requested"])
        self.assertIn("runner.stopped", events)

    def test_stop_is_not_overwritten_when_background_thread_exits(self) -> None:
        repo = FsRepo(self.context)
        service = CooperativeBlockingService(repo)
        runner = WorkbenchBackgroundRunner(service)
        run_id = repo.create_workbench_batch_record(target_count=1)["run_id"]

        runner.start(run_id)
        self.assertTrue(service.entered.wait(timeout=2))
        runner.stop(run_id)
        self.assertTrue(service.exited.wait(timeout=1))
        status = self.wait_until_idle(runner, run_id)
        time.sleep(0.05)

        self.assertEqual("stopped", status["state"])
        self.assertEqual("user_stopped", status["blocked_reason"])
        event_types = [event.event_type for event in repo.load_run_events(run_id)]
        stopped_index = event_types.index("runner.stopped")
        self.assertNotIn("runner.blocked", event_types[stopped_index + 1 :])

    def test_stop_all_and_wait_quiesces_active_runner_before_batch_clear(self) -> None:
        repo = FsRepo(self.context)
        service = CooperativeBlockingService(repo)
        runner = WorkbenchBackgroundRunner(service)
        run_id = repo.create_workbench_batch_record(target_count=1)["run_id"]
        runner.start(run_id)
        self.assertTrue(service.entered.wait(timeout=2))

        result = runner.stop_all_and_wait([run_id], timeout_seconds=2)

        self.assertTrue(result.ok)
        self.assertEqual([run_id], result.data["stopped_run_ids"])
        self.assertTrue(service.exited.is_set())
        runner.forget([run_id])
        self.assertEqual("idle", runner.status(run_id)["state"])

    def wait_until_idle(self, runner: WorkbenchBackgroundRunner, run_id: str) -> dict:
        deadline = time.time() + 5
        while time.time() < deadline:
            status = runner.status(run_id)
            if not status["running"]:
                return status
            time.sleep(0.05)
        raise AssertionError("runner did not finish before timeout")


class FakeAttributeTemplateWorker:
    def __init__(self, run_id: str, seed_id: str) -> None:
        self.run_id = run_id
        self.seed_id = seed_id
        self.collect_count = 0

    def collect(self, run_id: str) -> Result:
        self.collect_count += 1
        return Result.success(
            "attribute_template_worker.collected",
            "Fake attribute template collected.",
            {
                "payload": {
                    "run_id": self.run_id,
                    "worker": "workbench_browser_bridge",
                    "source": "fake_worker",
                    "seed_templates": [
                        {
                            "seed_id": self.seed_id,
                            "source_ozon_product_id": "ozon-runner-1",
                            "slot_id": "slot-0001",
                            "candidate_revision": 1,
                            "category_candidates": [
                                {
                                    "category_path": "Красота / Зеркала",
                                    "leaf_category": "Зеркала",
                                    "category_url": "https://www.ozon.ru/category/zerkala/",
                                    "category_id": "17000001",
                                }
                            ],
                            "public_attribute_evidence": {
                                "attribute_labels": ["Цвет"],
                                "attribute_table": {"Цвет": "белый"},
                                "visible_public_schema_guess": [
                                    {
                                        "attribute_id": "color",
                                        "attribute_label": "Цвет",
                                        "attribute_type": "text",
                                        "is_required": False,
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        )


class CooperativeBlockingService:
    def __init__(self, repo: FsRepo) -> None:
        self.repo = repo
        self.entered = threading.Event()
        self.exited = threading.Event()

    def run_until_blocked(self, run_id, max_steps=20, should_stop=None):
        self.entered.set()
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                if should_stop is not None and should_stop():
                    return Result.success(
                        "autopilot.blocked",
                        "Stopped by user.",
                        {"blocked_reason": "user_stopped"},
                    )
                time.sleep(0.01)
            raise AssertionError("stop probe was not observed")
        finally:
            self.exited.set()


class FailingAttributeTemplateWorker:
    def collect(self, run_id: str) -> Result:
        return Result.failure(
            "attribute_template_worker.failed",
            "Ozon captcha blocked attribute template collection.",
        )


class FakeSupplierWorker:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.collect_count = 0

    def collect(self, run_id: str) -> Result:
        self.collect_count += 1
        return Result.success(
            "supplier_worker.collected",
            "Fake supplier product collected.",
            {
                "payload": {
                    "run_id": self.run_id,
                    "worker": "local_playwright_direct",
                    "source": "1688_user_verified_link",
                    "network": {"mode": "direct", "proxy_disabled": True},
                    "supplier_products": [
                        {
                            "seed_id": "seed-test",
                            "supplier_product_id": "123456789012",
                            "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                            "title": "Test supplier product",
                            "seller": {"shop_name": "Test supplier"},
                            "sku": {"selected_options": {"color": "black"}},
                            "sku_options": [
                                {
                                    "supplier_sku_id": "supplier-sku-black",
                                    "combination_key": "color:black",
                                    "raw_label": "black single item",
                                    "selected_options": {"color": "black"},
                                    "set_quantity": 1,
                                    "set_composition": ["single item"],
                                    "price": {"currency": "CNY", "amount": "12.80"},
                                    "stock": {"status": "in_stock", "quantity": 20},
                                    "image_urls": ["https://img.example/1688-main.jpg"],
                                    "evidence_source": "embedded_sku_map",
                                    "complete": True,
                                }
                            ],
                            "images": ["https://img.example/1688-main.jpg"],
                            "price": {"currency": "CNY", "visible_text": "12.80"},
                            "attributes": {"material": "plastic"},
                            "domestic_shipping_evidence": {"visible_text": "包邮", "fee": "0"},
                        }
                    ],
                }
            },
        )
