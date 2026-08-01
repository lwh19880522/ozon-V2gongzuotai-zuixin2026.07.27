from __future__ import annotations

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import (
    CollectionPair,
    MatchedSupplierSku,
    OzonCandidate,
    PairStatus,
    SeedProduct,
    SupplierMatch,
    TargetSku,
)
from ozon_v2.services.collection_contract_service import CollectionContractService
from ozon_v2.services.run_service import RunService

from tests.helpers import RuntimeTestCase


class CollectionContractServiceTests(RuntimeTestCase):
    def test_ozon_contract_requires_generated_queries(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        run_id = RunService(repo).start_run(target_count=1, random_seed=10).data["run"]["run_id"]
        seeds = repo.load_sampled_seeds(run_id)
        seeds[0].ozon_query_terms_ru = []
        seeds[0].auxiliary_query_terms_en = []
        repo.save_sampled_seeds(run_id, seeds)

        result = CollectionContractService(repo).build_ozon_collection_contract(run_id)

        self.assertFalse(result.ok)
        self.assertEqual("contract.missing_seed_queries", result.code)

    def test_ozon_contract_uses_generated_query_not_raw_chinese(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=10).data["run"]["run_id"]
        sampled_seed_id = repo.load_sampled_seeds(run_id)[0].seed_id
        service.attach_seed_queries(run_id, {sampled_seed_id: ["органайзер для хранения"]})

        result = CollectionContractService(repo).build_ozon_collection_contract(run_id)

        self.assertTrue(result.ok)
        seed_payload = result.data["payload"]["seeds"][0]
        self.assertEqual(["органайзер для хранения"], seed_payload["ozon_query_terms_ru"])
        self.assertNotEqual(seed_payload["source_text_zh"], seed_payload["ozon_query_terms_ru"][0])
        self.assertTrue(result.data["payload"]["rules"]["do_not_search_raw_chinese_seed_text"])
        self.assertTrue(result.data["payload"]["rules"]["precise_category_required"])
        self.assertEqual(
            ["category_path", "leaf_category", "category_url", "category_id"],
            result.data["payload"]["rules"]["required_precise_category_fields"],
        )
        self.assertTrue(result.data["payload"]["rules"]["content_score_evidence_required"])
        content_score = result.data["payload"]["content_score_evidence"]
        self.assertIn("title_raw", content_score["required_public_fields"])
        self.assertIn("attribute_table", content_score["required_public_fields"])
        self.assertIn("main_gallery_images", content_score["required_public_fields"])
        self.assertNotIn("rating", content_score["required_public_fields"])
        self.assertNotIn("review_count", content_score["required_public_fields"])
        self.assertIn("rating", content_score["recommended_market_signals"])
        self.assertIn("review_count", content_score["recommended_market_signals"])
        self.assertIn("monthly_sales", content_score["optional_external_analytics_fields"])
        self.assertEqual("record unavailable fields with reason; do not invent metrics", content_score["missing_field_policy"])
        self.assertTrue(result.data["payload"]["rules"]["attribute_template_prerequisite_required"])
        prerequisite = result.data["payload"]["attribute_template_prerequisite"]
        self.assertTrue(prerequisite["must_complete_before_ozon_collection"])
        self.assertIn("dimensions", prerequisite["objective_attributes_may_be_prefilled"])
        self.assertIn("title", prerequisite["creative_fields_must_be_rewritten"])

    def test_ozon_contract_reuses_verified_public_snapshot_from_template_stage(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=10).data["run"]["run_id"]
        seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {seed.seed_id: ["органайзер для хранения"]})
        snapshot = {"product_id": "123", "title": "Product", "attributes": {"Материал": "пластик"}}
        decision = {"is_chinese_domestic_seller": True, "confidence": "high", "signals": []}
        repo.save_attribute_template_result(
            run_id,
            {
                "seed_templates": [
                    {
                        "seed_id": seed.seed_id,
                        "evidence": {
                            "public_product_snapshot": snapshot,
                            "domestic_seller_decision": decision,
                        },
                    }
                ]
            },
        )

        result = CollectionContractService(repo).build_ozon_collection_contract(run_id)

        self.assertTrue(result.ok)
        seed_payload = result.data["payload"]["seeds"][0]
        self.assertEqual(snapshot, seed_payload["public_product_snapshot"])
        self.assertEqual(decision, seed_payload["domestic_seller_decision"])

    def test_attribute_template_contract_defines_prefill_and_rewrite_policy(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=10).data["run"]["run_id"]
        sampled_seed_id = repo.load_sampled_seeds(run_id)[0].seed_id
        service.attach_seed_queries(run_id, {sampled_seed_id: ["органайзер для хранения"]})

        result = CollectionContractService(repo).build_attribute_template_contract(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("ozon_attribute_template", result.data["contract_type"])
        self.assertIn("codex_in_app_browser", result.data["approved_browser_executors"])
        self.assertIn("workbench_browser_bridge", result.data["approved_browser_executors"])
        rules = result.data["payload"]["rules"]
        self.assertTrue(rules["after_seed_selection_required"])
        self.assertTrue(rules["before_ozon_product_collection_required"])
        self.assertTrue(rules["prefill_objective_attributes_only"])
        self.assertTrue(rules["collect_public_attribute_evidence"])
        self.assertTrue(rules["seller_api_upload_attribute_schema_required"])
        self.assertTrue(rules["do_not_copy_creative_content"])
        self.assertTrue(rules["ozon_proxy_required"])
        self.assertTrue(rules["visitor_access_required"])
        self.assertTrue(rules["no_logged_in_account"])
        output = result.data["payload"]["required_template_output"]
        self.assertIn("attribute_id", output["upload_attribute_schema"])
        self.assertIn("description_category_id", output["seller_attribute_template"])
        self.assertIn("visible_public_schema_guess", output["public_attribute_evidence"])
        policy = result.data["payload"]["draft_prefill_policy"]
        self.assertIn("dimensions", policy["objective_copy_allowed_fields"])
        self.assertIn("description", policy["creative_rewrite_required_fields"])
        self.assertTrue(policy["title_description_rich_content_must_not_match_ozon"])

    def test_replacement_attribute_contract_recollects_full_batch_without_retained_result(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=2, random_seed=10).data["run"]["run_id"]
        seeds = repo.load_sampled_seeds(run_id)
        service.attach_seed_queries(
            run_id,
            {seed.seed_id: [f"query {index}"] for index, seed in enumerate(seeds, start=1)},
        )
        run = repo.load_run(run_id)
        run["replacement_pending_seed_ids"] = [seeds[-1].seed_id]
        repo.save_run(run)

        result = CollectionContractService(repo).build_attribute_template_contract(run_id)

        self.assertTrue(result.ok)
        self.assertEqual(
            {seed.seed_id for seed in seeds},
            {item["seed_id"] for item in result.data["payload"]["seeds"]},
        )

    def test_replacement_attribute_contract_scopes_to_pending_when_retained_result_is_complete(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=2, random_seed=10).data["run"]["run_id"]
        seeds = repo.load_sampled_seeds(run_id)
        service.attach_seed_queries(
            run_id,
            {seed.seed_id: [f"query {index}"] for index, seed in enumerate(seeds, start=1)},
        )
        pending_seed = seeds[-1]
        retained_seed = seeds[0]
        repo.save_attribute_template_result(
            run_id,
            {"seed_templates": [{"seed_id": retained_seed.seed_id}]},
        )
        run = repo.load_run(run_id)
        run["replacement_pending_seed_ids"] = [pending_seed.seed_id]
        repo.save_run(run)

        result = CollectionContractService(repo).build_attribute_template_contract(run_id)

        self.assertTrue(result.ok)
        self.assertEqual(
            [pending_seed.seed_id],
            [item["seed_id"] for item in result.data["payload"]["seeds"]],
        )

    def test_replacement_ozon_contract_recollects_full_batch_without_retained_ozon_result(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=2, random_seed=10).data["run"]["run_id"]
        seeds = repo.load_sampled_seeds(run_id)
        service.attach_seed_queries(
            run_id,
            {seed.seed_id: [f"query {index}"] for index, seed in enumerate(seeds, start=1)},
        )
        run = repo.load_run(run_id)
        run["replacement_pending_seed_ids"] = [seeds[-1].seed_id]
        repo.save_run(run)

        result = CollectionContractService(repo).build_ozon_collection_contract(run_id)

        self.assertTrue(result.ok)
        self.assertEqual(
            {seed.seed_id for seed in seeds},
            {item["seed_id"] for item in result.data["payload"]["seeds"]},
        )

    def test_supplier_contract_requires_direct_1688_image_search_flow(self) -> None:
        pair = CollectionPair(
            pair_id="pair-1",
            seed_product=SeedProduct(seed_id="seed-0001", title_or_keyword="收纳筐", product_clue="收纳筐"),
            ozon_candidate=OzonCandidate(
                seed_id="seed-0001",
                seed_title_or_keyword="收纳筐",
                seed_source_language="zh-CN",
                ozon_query_terms_ru=["korzina"],
                ozon_product_id="ozon-1",
                ozon_url="https://www.ozon.ru/product/test/",
                title="test",
                seller_name="seller",
                target_sku=TargetSku(sku_id="sku-1", selected_options={"color": "white"}),
            ),
            supplier_match=SupplierMatch(
                supplier_product_id="offer-1",
                supplier_url="https://detail.1688.com/offer/offer-1.html",
                title="supplier",
                shop_name="shop",
                matched_supplier_sku=MatchedSupplierSku(sku_id="sku-1", selected_options={"color": "white"}),
                exact_match_decision={"decision": "accepted"},
            ),
            final_decision=PairStatus.ACCEPTED,
            reasons=[],
        )

        result = CollectionContractService().build_supplier_collection_contract(pair)

        self.assertTrue(result.ok)
        rules = result.data["payload"]["rules"]
        self.assertTrue(rules["direct_network_no_proxy_required"])
        self.assertTrue(rules["1688_homepage_entry_required"])
        self.assertEqual("https://www.1688.com/", rules["1688_homepage_url"])
        self.assertTrue(rules["local_image_upload_required"])
        self.assertTrue(rules["select_product_subject_before_search"])
