from __future__ import annotations

from pathlib import Path

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.app.result import Result
from ozon_v2.domain.models import SeedProduct, WorkbenchAction, WorkbenchState
from ozon_v2.services.seed_query_service import SeedQueryService
from ozon_v2.services.workbench_service import WorkbenchService

from tests.helpers import FakeSellerApiAdapter, RuntimeTestCase


class FakeMissingCredentialService:
    def status(self) -> Result:
        return Result.success(
            "credentials.missing",
            "missing",
            {"configured": False, "client_id": None, "api_key_masked": None},
        )


class FakeDedupeRefreshService:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh_from_adapter(self) -> Result:
        self.refresh_count += 1
        return Result.success(
            "dedupe.refreshed",
            "refreshed",
            {"existing_product_count": 0, "refreshed_at": "2026-07-08T00:00:00+00:00"},
        )


class WorkbenchSkeletonTests(RuntimeTestCase):
    def test_image_skill_builds_white_subject_from_locked_multi_image_evidence(self) -> None:
        root = Path(__file__).resolve().parents[1] / "skills" / "ozon-product-media-generator"
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        contract = (root / "references" / "prompt-contract.md").read_text(encoding="utf-8")
        white_prompt_path = root / "assets" / "white-subject-prompt.txt"

        self.assertTrue(white_prompt_path.is_file())
        white_prompt = white_prompt_path.read_text(encoding="utf-8")
        self.assertIn("white-subject-prompt.txt", skill)
        self.assertIn("all locked subject evidence images", skill)
        self.assertIn("generate one reusable clean white-background subject", skill)
        self.assertIn("exact set quantity and set composition", skill)
        self.assertIn("Locked supplier subject evidence images", contract)
        self.assertIn("Generated clean white-background subject", contract)
        self.assertIn("stop instead of guessing", white_prompt)
        self.assertIn("exact quantity", white_prompt)
        self.assertIn("set composition", white_prompt)

    def test_supplier_not_found_blacklists_and_refills_only_replacement_seed(self) -> None:
        repo = FsRepo(self.context)
        rejected = self.ready_seed("seed-rejected", "rejected product")
        retained = self.ready_seed("seed-retained", "retained product")
        replacement = self.ready_seed("seed-replacement", "replacement product")
        repo.save_active_seeds([rejected, retained, replacement])
        run = repo.create_workbench_batch_record(target_count=2)
        run_id = run["run_id"]
        repo.save_sampled_seeds(run_id, [rejected, retained])
        repo.save_attribute_template_result(
            run_id,
            {
                "run_id": run_id,
                "seed_templates": [
                    self.saved_attribute_template(rejected.seed_id),
                    self.saved_attribute_template(retained.seed_id),
                ],
            },
        )
        repo.save_ozon_collection_result(
            run_id,
            {
                "run_id": run_id,
                "ozon_candidates": [
                    self.saved_ozon_candidate(rejected.seed_id, "ozon-rejected"),
                    self.saved_ozon_candidate(retained.seed_id, "ozon-retained"),
                ],
            },
        )
        repo.save_supplier_review(
            run_id,
            {
                "run_id": run_id,
                "items": [
                    {
                        "seed_id": rejected.seed_id,
                        "ozon_product_id": "ozon-rejected",
                        "ozon_title": "Rejected",
                        "supplier_url": None,
                        "user_verified_exact_match": False,
                    },
                    {
                        "seed_id": retained.seed_id,
                        "ozon_product_id": "ozon-retained",
                        "ozon_title": "Retained",
                        "supplier_url": "https://detail.1688.com/offer/123.html",
                        "user_verified_exact_match": True,
                    },
                ],
            },
        )
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.SUPPLIER_REVIEW.value
        run["sampled_seed_ids"] = [rejected.seed_id, retained.seed_id]
        run["attribute_template_collected"] = True
        run["ozon_collected"] = True
        repo.save_run(run)

        result = WorkbenchService(repo).reject_supplier_candidate(
            run_id,
            rejected.seed_id,
            reason="User could not find an exact 1688 supplier.",
            random_seed=7,
        )

        self.assertTrue(result.ok)
        self.assertEqual("supplier_review.candidate_replaced", result.code)
        sampled = repo.load_sampled_seeds(run_id)
        self.assertEqual(2, len(sampled))
        self.assertEqual({retained.seed_id, replacement.seed_id}, {seed.seed_id for seed in sampled})
        loaded = repo.load_run(run_id)
        self.assertEqual(WorkbenchState.SEED_SELECTED.value, loaded["status"])
        self.assertEqual([replacement.seed_id], loaded["replacement_pending_seed_ids"])
        self.assertEqual(2, loaded["target_count"])
        self.assertNotIn(rejected.seed_id, {seed.seed_id for seed in repo.load_active_seeds()})
        self.assertIn(rejected.seed_id, repo.load_blacklisted_seed_ids())
        self.assertIn("ozon-rejected", repo.load_blacklisted_ozon_product_ids())
        self.assertEqual(
            [retained.seed_id],
            [item["seed_id"] for item in repo.load_attribute_template_result(run_id)["seed_templates"]],
        )
        self.assertEqual(
            [retained.seed_id],
            [item["seed_id"] for item in repo.load_ozon_collection_result(run_id)["ozon_candidates"]],
        )
        review = repo.load_supplier_review(run_id)
        self.assertEqual([retained.seed_id], [item["seed_id"] for item in review["items"]])
        self.assertEqual("https://detail.1688.com/offer/123.html", review["items"][0]["supplier_url"])
        self.assertEqual(1, len(repo.load_supplier_rejections(run_id)))

        template_contract = WorkbenchService(repo).collection_contract_service.build_attribute_template_contract(run_id)
        self.assertTrue(template_contract.ok)
        self.assertEqual(
            [replacement.seed_id],
            [item["seed_id"] for item in template_contract.data["payload"]["seeds"]],
        )

    def test_autopilot_stop_probe_prevents_next_transition(self) -> None:
        repo = FsRepo(self.context)
        service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())
        run = repo.create_workbench_batch_record(target_count=1)
        run["status"] = WorkbenchState.STORE_DEDUPED.value
        repo.save_run(run)

        result = service.run_until_blocked(
            run["run_id"],
            max_steps=5,
            should_stop=lambda: True,
        )

        self.assertTrue(result.ok)
        self.assertEqual("autopilot.blocked", result.code)
        self.assertEqual("user_stopped", result.data["blocked_reason"])
        self.assertEqual(
            WorkbenchState.STORE_DEDUPED.value,
            repo.load_run(run["run_id"])["status"],
        )

    def test_start_batch_creates_locked_batch_and_event_log(self) -> None:
        repo = FsRepo(self.context)
        result = WorkbenchService(repo).start_batch(target_count=3)

        self.assertTrue(result.ok)
        run = result.data["run"]
        self.assertEqual("workbench_batch", run["kind"])
        self.assertEqual(WorkbenchState.CREATED.value, run["status"])
        self.assertTrue(run["publish_locked"])
        self.assertIn(WorkbenchAction.CHECK_CREDENTIALS.value, result.data["allowed_actions"])

        events = repo.load_run_events(run["run_id"])
        self.assertEqual(1, len(events))
        self.assertEqual("workbench.batch_created", events[0].event_type)

    def test_invalid_action_is_rejected_by_state_machine_and_logged(self) -> None:
        repo = FsRepo(self.context)
        seller_api = FakeSellerApiAdapter()
        service = WorkbenchService(repo, seller_api_adapter=seller_api)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = service.dispatch(run_id, WorkbenchAction.START_OZON_COLLECTION.value)

        self.assertFalse(result.ok)
        self.assertEqual("workbench.invalid_action", result.code)
        self.assertEqual(WorkbenchState.CREATED.value, repo.load_run(run_id)["status"])
        self.assertEqual("workbench.action_rejected", repo.load_run_events(run_id)[-1].event_type)

    def test_publish_request_is_locked_even_when_draft_is_ready(self) -> None:
        repo = FsRepo(self.context)
        seller_api = FakeSellerApiAdapter()
        service = WorkbenchService(repo, seller_api_adapter=seller_api)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.DRAFT_READY.value
        repo.save_run(run)

        allowed = service.allowed_actions(run_id)
        result = service.dispatch(run_id, WorkbenchAction.REQUEST_PUBLISH.value)

        self.assertTrue(allowed.ok)
        self.assertNotIn(WorkbenchAction.REQUEST_PUBLISH.value, allowed.data["allowed_actions"])
        self.assertFalse(result.ok)
        self.assertEqual("publish.locked", result.code)
        self.assertEqual(WorkbenchState.DRAFT_READY.value, repo.load_run(run_id)["status"])
        self.assertEqual("publish.locked", repo.load_run_events(run_id)[-1].event_type)

    def test_check_credentials_requires_store_binding_when_missing(self) -> None:
        repo = FsRepo(self.context)
        credentials = FakeMissingCredentialService()
        service = WorkbenchService(repo, credential_service=credentials)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = service.dispatch(run_id, WorkbenchAction.CHECK_CREDENTIALS.value)

        self.assertTrue(result.ok)
        self.assertEqual("workbench.store_binding_required", result.code)
        self.assertEqual(WorkbenchState.NEEDS_CREDENTIALS.value, repo.load_run(run_id)["status"])
        self.assertEqual("store_binding.required", repo.load_run_events(run_id)[-1].event_type)

    def test_autopilot_stops_at_store_binding_when_missing(self) -> None:
        repo = FsRepo(self.context)
        credentials = FakeMissingCredentialService()
        dedupe = FakeDedupeRefreshService()
        service = WorkbenchService(repo, credential_service=credentials, seller_history_service=dedupe)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = service.run_until_blocked(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("autopilot.blocked", result.code)
        self.assertEqual("store_binding_required", result.data["blocked_reason"])
        self.assertEqual(WorkbenchState.NEEDS_CREDENTIALS.value, repo.load_run(run_id)["status"])
        self.assertEqual(0, dedupe.refresh_count)
        self.assertEqual("autopilot.blocked", repo.load_run_events(run_id)[-1].event_type)

    def test_start_dedupe_requires_credentials_before_refresh(self) -> None:
        repo = FsRepo(self.context)
        credentials = FakeMissingCredentialService()
        dedupe = FakeDedupeRefreshService()
        service = WorkbenchService(repo, credential_service=credentials, seller_history_service=dedupe)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = service.dispatch(run_id, WorkbenchAction.START_DEDUPE.value)

        self.assertTrue(result.ok)
        self.assertEqual("workbench.store_binding_required", result.code)
        self.assertEqual(WorkbenchState.NEEDS_CREDENTIALS.value, repo.load_run(run_id)["status"])
        self.assertEqual(0, dedupe.refresh_count)
        self.assertEqual("dedupe.blocked_missing_store_binding", repo.load_run_events(run_id)[-1].event_type)

    def test_start_dedupe_refreshes_store_dedupe_when_credentials_exist(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        dedupe = FakeDedupeRefreshService()
        service = WorkbenchService(repo, seller_history_service=dedupe)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = service.dispatch(run_id, WorkbenchAction.START_DEDUPE.value)

        self.assertTrue(result.ok)
        self.assertEqual("workbench.store_deduped", result.code)
        self.assertEqual(WorkbenchState.STORE_DEDUPED.value, repo.load_run(run_id)["status"])
        self.assertEqual(1, dedupe.refresh_count)
        self.assertEqual("dedupe.refreshed", repo.load_run_events(run_id)[-1].event_type)

    def test_autopilot_resumes_interrupted_store_dedupe(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        dedupe = FakeDedupeRefreshService()
        service = WorkbenchService(repo, seller_history_service=dedupe)
        run_id = service.start_batch(target_count=5).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.DEDUPING_STORE.value
        repo.save_run(run)

        result = service.run_until_blocked(run_id, max_steps=1)

        self.assertTrue(result.ok)
        self.assertEqual(1, dedupe.refresh_count)
        self.assertEqual(WorkbenchState.STORE_DEDUPED.value, repo.load_run(run_id)["status"])

    def test_autopilot_auto_generates_queries_until_attribute_template_worker_gate(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(seed_id="seed-test", title_or_keyword="夏凉被", product_clue="夏凉被")
        repo.save_active_seeds([seed])
        dedupe = FakeDedupeRefreshService()
        service = WorkbenchService(repo, seller_history_service=dedupe)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = service.run_until_blocked(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("autopilot.blocked", result.code)
        self.assertEqual("attribute_template_worker_required", result.data["blocked_reason"])
        self.assertEqual(WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value, repo.load_run(run_id)["status"])
        sampled = repo.load_sampled_seeds(run_id)
        self.assertEqual(1, len(sampled))
        self.assertEqual(["летнее одеяло", "легкое одеяло"], sampled[0].ozon_query_terms_ru)
        self.assertEqual(1, dedupe.refresh_count)

    def test_select_seeds_samples_without_removing_active_pool(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seller_api = FakeSellerApiAdapter()
        service = WorkbenchService(repo, seller_api_adapter=seller_api)
        run_id = service.start_batch(target_count=2).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.STORE_DEDUPED.value
        repo.save_run(run)

        result = service.dispatch(run_id, WorkbenchAction.SELECT_SEEDS.value)

        self.assertTrue(result.ok)
        self.assertEqual("workbench.seeds_selected", result.code)
        self.assertEqual(WorkbenchState.SEED_SELECTED.value, repo.load_run(run_id)["status"])
        self.assertEqual(2, len(repo.load_sampled_seeds(run_id)))
        self.assertEqual(2000, len(repo.load_active_seeds()))
        self.assertEqual("seed_sampling.selected", repo.load_run_events(run_id)[-1].event_type)

    def test_exhausted_attribute_template_seed_is_replaced_without_removing_active_pool(self) -> None:
        repo = FsRepo(self.context)
        repo.initialize_runtime()
        active_before = repo.load_active_seeds()
        rejected_seed = active_before[0]
        repo.save_sampled_seeds("wb-replace", [rejected_seed])
        run = repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        repo.save_sampled_seeds(run_id, [rejected_seed])
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        run["sampled_seed_ids"] = [rejected_seed.seed_id]
        run["replacement_pending_seed_ids"] = [rejected_seed.seed_id]
        run["attribute_template_contract_ready"] = True
        run["attribute_template_contract_path"] = "stale-contract.json"
        repo.save_run(run)

        result = WorkbenchService(repo).replace_exhausted_attribute_template_seed(
            run_id,
            rejected_seed.seed_id,
            "no verified Chinese cross-border product",
            random_seed=9,
        )

        self.assertTrue(result.ok)
        self.assertEqual("workbench.exhausted_seed_replaced", result.code)
        replacement = repo.load_sampled_seeds(run_id)[0]
        self.assertNotEqual(rejected_seed.seed_id, replacement.seed_id)
        loaded = repo.load_run(run_id)
        self.assertEqual(WorkbenchState.SEED_SELECTED.value, loaded["status"])
        self.assertEqual([replacement.seed_id], loaded["replacement_pending_seed_ids"])
        self.assertFalse(loaded["attribute_template_contract_ready"])
        self.assertNotIn("attribute_template_contract_path", loaded)
        self.assertEqual(2000, len(repo.load_active_seeds()))
        self.assertEqual(rejected_seed.seed_id, repo.load_rejected_seed_attempts(run_id)[0]["seed_id"])
        self.assertEqual("seed_sampling.replaced_after_exhaustion", repo.load_run_events(run_id)[-1].event_type)

    def test_generate_queries_without_mapping_keeps_query_gate_closed(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(seed_id="seed-unknown", title_or_keyword="神秘新品", product_clue="神秘新品")
        repo.save_active_seeds([seed])
        seller_api = FakeSellerApiAdapter()
        service = WorkbenchService(repo, seller_api_adapter=seller_api)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.STORE_DEDUPED.value
        repo.save_run(run)
        service.dispatch(run_id, WorkbenchAction.SELECT_SEEDS.value)

        result = service.dispatch(run_id, WorkbenchAction.GENERATE_OZON_QUERIES.value)

        self.assertTrue(result.ok)
        self.assertEqual("workbench.queries_need_generation", result.code)
        self.assertEqual(WorkbenchState.SEED_SELECTED.value, repo.load_run(run_id)["status"])
        self.assertEqual(0, result.data["progress"]["query_ready_count"])
        self.assertEqual("query_generation.needs_input", repo.load_run_events(run_id)[-1].event_type)

    def test_generate_queries_blocks_different_seeds_with_same_primary_query(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seeds = [
            SeedProduct(seed_id="seed-a", title_or_keyword="鞋袋", product_clue="鞋袋"),
            SeedProduct(seed_id="seed-b", title_or_keyword="鞋跟贴", product_clue="鞋跟贴"),
        ]
        repo.save_active_seeds(seeds)
        service = WorkbenchService(
            repo,
            seed_query_service=SeedQueryService({"鞋袋": ["обувь"], "鞋跟贴": ["обувь"]}),
            seller_api_adapter=FakeSellerApiAdapter(),
        )
        run_id = service.start_batch(target_count=2).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.STORE_DEDUPED.value
        repo.save_run(run)
        service.dispatch(run_id, WorkbenchAction.SELECT_SEEDS.value)

        result = service.dispatch(run_id, WorkbenchAction.GENERATE_OZON_QUERIES.value)

        self.assertTrue(result.ok)
        self.assertEqual("workbench.queries_need_generation", result.code)
        collisions = result.data["invalid_queries"]
        self.assertEqual(1, len(collisions))
        self.assertEqual("duplicate_primary_query", collisions[0]["code"])

    def test_generate_queries_with_mapping_prepares_ozon_contract_without_browser(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(seed_id="seed-test", title_or_keyword="收纳盒", product_clue="收纳盒")
        repo.save_active_seeds([seed])
        service = WorkbenchService(
            repo,
            seed_query_service=SeedQueryService({"收纳盒": ["органайзер для хранения"]}),
            seller_api_adapter=FakeSellerApiAdapter(),
        )
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.STORE_DEDUPED.value
        repo.save_run(run)
        service.dispatch(run_id, WorkbenchAction.SELECT_SEEDS.value)

        query_result = service.dispatch(run_id, WorkbenchAction.GENERATE_OZON_QUERIES.value)
        template_result = service.dispatch(run_id, WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION.value)
        premature_ozon_result = service.dispatch(run_id, WorkbenchAction.START_OZON_COLLECTION.value)
        ingest_template_result = service.ingest_attribute_template_result(run_id, self.attribute_template_payload(run_id, "seed-test"))
        contract_result = service.dispatch(run_id, WorkbenchAction.START_OZON_COLLECTION.value)

        self.assertTrue(query_result.ok)
        self.assertEqual("workbench.queries_generated", query_result.code)
        self.assertTrue(template_result.ok)
        self.assertEqual("workbench.attribute_template_contract_ready", template_result.code)
        self.assertFalse(premature_ozon_result.ok)
        self.assertEqual("workbench.invalid_action", premature_ozon_result.code)
        self.assertTrue(ingest_template_result.ok)
        self.assertEqual("workbench.attribute_template_ingested", ingest_template_result.code)
        self.assertTrue(contract_result.ok)
        self.assertEqual("workbench.ozon_contract_ready", contract_result.code)
        self.assertEqual(WorkbenchState.OZON_COLLECTING.value, repo.load_run(run_id)["status"])
        self.assertIn("contract", contract_result.data)
        self.assertEqual("ozon_collection.contract_ready", repo.load_run_events(run_id)[-1].event_type)

    def test_ingest_attribute_template_result_completes_template_gate(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(seed_id="seed-test", title_or_keyword="夏凉被", product_clue="夏凉被")
        repo.save_active_seeds([seed])
        seller_api = FakeSellerApiAdapter()
        service = WorkbenchService(repo, seller_api_adapter=seller_api)
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.STORE_DEDUPED.value
        repo.save_run(run)
        service.dispatch(run_id, WorkbenchAction.SELECT_SEEDS.value)
        service.dispatch(run_id, WorkbenchAction.GENERATE_OZON_QUERIES.value)
        service.dispatch(run_id, WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION.value)

        result = service.ingest_attribute_template_result(run_id, self.attribute_template_payload(run_id, "seed-test"))

        self.assertTrue(result.ok)
        self.assertEqual("workbench.attribute_template_ingested", result.code)
        self.assertEqual(1, seller_api.resolve_count)
        self.assertEqual("Sample Ozon product", seller_api.last_category_candidate["product_title"])
        loaded = repo.load_run(run_id)
        self.assertEqual(WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED.value, loaded["status"])
        self.assertTrue(loaded["attribute_template_collected"])
        result_payload = repo.load_attribute_template_result(run_id)
        template = result_payload["seed_templates"][0]
        self.assertEqual("ozon_seller_api_description_category_attribute", template["seller_attribute_template"]["source"])
        self.assertEqual("85", template["upload_attribute_schema"][0]["attribute_id"])
        self.assertIn("visible_public_schema_guess", template["public_attribute_evidence"])
        self.assertEqual("attribute_template.ingested", repo.load_run_events(run_id)[-1].event_type)

    def test_ingest_attribute_template_result_rejects_invalid_payload(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(seed_id="seed-test", title_or_keyword="夏凉被", product_clue="夏凉被")
        repo.save_active_seeds([seed])
        service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        repo.save_run(run)
        repo.save_sampled_seeds(run_id, [seed])

        result = service.ingest_attribute_template_result(run_id, {"worker": "manual"})

        self.assertFalse(result.ok)
        self.assertEqual("workbench.attribute_template_invalid", result.code)
        self.assertEqual(WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value, repo.load_run(run_id)["status"])

    def test_replacement_attribute_merge_failure_is_recorded_without_raising(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        retained = self.ready_seed("seed-retained", "retained")
        pending = self.ready_seed("seed-pending", "pending")
        run = repo.create_workbench_batch_record(target_count=2)
        run_id = run["run_id"]
        repo.save_sampled_seeds(run_id, [retained, pending])
        repo.save_attribute_template_result(
            run_id,
            {"seed_templates": [{"seed_id": retained.seed_id}]},
        )
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        run["replacement_pending_seed_ids"] = [pending.seed_id]
        repo.save_run(run)
        service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())

        result = service.ingest_attribute_template_result(
            run_id,
            self.attribute_template_payload(run_id, pending.seed_id),
        )

        self.assertFalse(result.ok)
        self.assertEqual("workbench.attribute_template_merge_invalid", result.code)
        self.assertEqual("attribute_template.merge_invalid", repo.load_run_events(run_id)[-1].event_type)

    def test_replacement_expected_seed_ids_use_full_batch_without_retained_stage_result(self) -> None:
        repo = FsRepo(self.context)
        retained = self.ready_seed("seed-retained", "retained")
        pending = self.ready_seed("seed-pending", "pending")
        run = repo.create_workbench_batch_record(target_count=2)
        run_id = run["run_id"]
        repo.save_sampled_seeds(run_id, [retained, pending])
        run["replacement_pending_seed_ids"] = [pending.seed_id]
        repo.save_run(run)
        service = WorkbenchService(repo)

        expected = service._pending_or_all_seed_ids(
            run,
            [retained.seed_id, pending.seed_id],
            result_kind="ozon_collection",
        )

        self.assertEqual([retained.seed_id, pending.seed_id], expected)

    def test_replacement_expected_seed_ids_use_pending_when_retained_stage_result_is_complete(self) -> None:
        repo = FsRepo(self.context)
        retained = self.ready_seed("seed-retained", "retained")
        pending = self.ready_seed("seed-pending", "pending")
        run = repo.create_workbench_batch_record(target_count=2)
        run_id = run["run_id"]
        repo.save_sampled_seeds(run_id, [retained, pending])
        repo.save_ozon_collection_result(
            run_id,
            {"ozon_candidates": [self.saved_ozon_candidate(retained.seed_id, "ozon-retained")]},
        )
        run["replacement_pending_seed_ids"] = [pending.seed_id]
        repo.save_run(run)
        service = WorkbenchService(repo)

        expected = service._pending_or_all_seed_ids(
            run,
            [retained.seed_id, pending.seed_id],
            result_kind="ozon_collection",
        )

        self.assertEqual([pending.seed_id], expected)

    def test_ingest_attribute_template_result_accepts_in_app_browser_worker(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(seed_id="seed-test", title_or_keyword="summer quilt", product_clue="summer quilt")
        repo.save_active_seeds([seed])
        service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        repo.save_run(run)
        repo.save_sampled_seeds(run_id, [seed])
        payload = self.attribute_template_payload(run_id, "seed-test")
        payload["worker"] = "codex_in_app_browser"
        payload["source"] = "codex_in_app_browser_attribute_template_worker"

        result = service.ingest_attribute_template_result(run_id, payload)

        self.assertTrue(result.ok)
        self.assertEqual("workbench.attribute_template_ingested", result.code)
        self.assertEqual(WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED.value, repo.load_run(run_id)["status"])

    def test_ingest_attribute_template_result_rejects_when_seller_template_missing(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(seed_id="seed-test", title_or_keyword="夏凉被", product_clue="夏凉被")
        repo.save_active_seeds([seed])
        service = WorkbenchService(repo, seller_api_adapter=FailingSellerApiAdapter())
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]
        run = repo.load_run(run_id)
        run["status"] = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value
        repo.save_run(run)
        repo.save_sampled_seeds(run_id, [seed])

        result = service.ingest_attribute_template_result(run_id, self.attribute_template_payload(run_id, "seed-test"))

        self.assertFalse(result.ok)
        self.assertEqual("workbench.seller_attribute_template_unavailable", result.code)
        self.assertEqual(WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value, repo.load_run(run_id)["status"])

    def test_autopilot_runs_until_attribute_template_worker_gate_when_queries_are_ready(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        seed = SeedProduct(seed_id="seed-test", title_or_keyword="收纳盒", product_clue="收纳盒")
        repo.save_active_seeds([seed])
        dedupe = FakeDedupeRefreshService()
        service = WorkbenchService(
            repo,
            seller_history_service=dedupe,
            seed_query_service=SeedQueryService({"收纳盒": ["органайзер для хранения"]}),
        )
        run_id = service.start_batch(target_count=1).data["run"]["run_id"]

        result = service.run_until_blocked(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("autopilot.blocked", result.code)
        self.assertEqual("attribute_template_worker_required", result.data["blocked_reason"])
        self.assertEqual(WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING.value, repo.load_run(run_id)["status"])
        self.assertTrue(repo.load_run(run_id)["attribute_template_contract_ready"])
        self.assertEqual(1, dedupe.refresh_count)

    def attribute_template_payload(self, run_id: str, seed_id: str) -> dict:
        return {
            "run_id": run_id,
            "worker": "microsoft_playwright_mcp",
            "source": "playwright_mcp_attribute_template_worker",
            "seed_templates": [
                {
                    "seed_id": seed_id,
                    "category_candidates": [
                        {
                            "category_path": "Дом и сад / Текстиль / Одеяла",
                            "leaf_category": "Одеяла",
                            "category_url": "https://www.ozon.ru/category/odeyala/",
                            "category_id": "17000001",
                            "confidence": "medium",
                            "source_evidence": "Ozon public category evidence",
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
                    "evidence": {
                        "product_title": "Sample Ozon product",
                        "product_url": "https://www.ozon.ru/product/sample-123/",
                    },
                }
            ],
        }

    def ready_seed(self, seed_id: str, title: str) -> SeedProduct:
        return SeedProduct(
            seed_id=seed_id,
            title_or_keyword=title,
            product_clue=title,
            ozon_query_terms_ru=[f"{title} ru"],
            query_generation_status="generated",
        )

    def saved_attribute_template(self, seed_id: str) -> dict:
        return {
            "seed_id": seed_id,
            "category_candidates": [{"category_id": "1", "category_path": "Test", "leaf_category": "Test"}],
            "public_attribute_evidence": {"attribute_table": {"Material": "plastic"}},
            "seller_attribute_template": {
                "source": "ozon_seller_api_description_category_attribute",
                "description_category_id": 1,
                "type_id": 1,
                "matched_category_path": "Test",
                "match_confidence": "high",
            },
            "upload_attribute_schema": [
                {
                    "attribute_id": "1",
                    "attribute_label": "Material",
                    "attribute_type": "text",
                    "is_required": True,
                    "schema_source": "ozon_seller_api_description_category_attribute",
                }
            ],
            "draft_prefill_plan": [],
        }

    def saved_ozon_candidate(self, seed_id: str, product_id: str) -> dict:
        return {
            "seed_id": seed_id,
            "ozon_product_id": product_id,
            "ozon_url": f"https://www.ozon.ru/product/{product_id}/",
            "title": product_id,
            "target_sku": {"sku_id": product_id, "selected_options": {"Color": "black"}},
            "selected_sku_media": {"main_gallery_images": [f"https://img.example/{product_id}.jpg"]},
            "attributes": {"Material": "plastic"},
        }


class FailingSellerApiAdapter:
    def resolve_attribute_template(self, category_candidate: dict) -> dict:
        from ozon_v2.adapters.seller_api import SellerApiError

        raise SellerApiError("category template not found")
