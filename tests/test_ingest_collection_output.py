from __future__ import annotations

import csv

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import ExistingStoreProduct
from ozon_v2.services.run_service import RunService

from tests.helpers import RuntimeTestCase


def build_pair_payload(seed) -> dict:
    return {
        "pair_id": "pair-1",
        "seed_product": seed.to_dict(),
        "ozon_candidate": {
            "seed_id": seed.seed_id,
            "seed_title_or_keyword": seed.title_or_keyword,
            "seed_source_language": seed.source_language,
            "ozon_query_terms_ru": seed.ozon_query_terms_ru,
            "ozon_product_id": "ozon-1",
            "ozon_url": "https://www.ozon.ru/product/test",
            "title": "Test product",
            "seller_name": "CN seller",
            "seller_url": "https://www.ozon.ru/seller/cn-seller-1/",
            "seller_evidence": {"raw_text": "Доставка из Китая"},
            "target_sku": {
                "sku_id": "ozon-sku-1",
                "selected_options": {"color": "white"},
            },
            "selected_sku_media": {
                "main_gallery_images": ["https://img.example/main.jpg"],
                "selected_sku_images": ["https://img.example/main.jpg"],
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
            "category_path": "Home / Storage",
            "leaf_category": "Storage",
            "category_url": "https://www.ozon.ru/category/storage-1234/",
            "category_id": "1234",
            "price": "799",
            "currency": "RUB",
            "rating": "4.8",
            "review_count": 120,
            "delivery_origin": "Доставка из Китая",
            "delivery_time": "15-30 дней",
            "fulfillment_label": "Ozon Global",
            "attributes": {"Материал": "пластик"},
            "content_score_evidence": {
                "title_raw": "Test product",
                "attribute_table": {"Материал": "пластик"},
                "main_gallery_images": ["https://img.example/main.jpg"],
                "rating": "4.8",
                "review_count": 120,
                "missing_fields": {"monthly_sales": "external analytics unavailable"},
            },
        },
        "supplier_match": {
            "supplier_product_id": "supplier-1",
            "supplier_url": "https://detail.1688.com/offer/test.html",
            "title": "Test product",
            "shop_name": "Factory",
            "matched_supplier_sku": {
                "sku_id": "supplier-sku-1",
                "selected_options": {"color": "white"},
            },
            "exact_match_decision": {"is_exact": True, "confidence": "high"},
            "domestic_shipping_fee": "¥6",
            "domestic_shipping_destination": "广东阳江 -> 福建泉州",
            "domestic_shipping_evidence": {
                "raw_text": "广东阳江 送至 福建泉州 | 2件以内 | 运费 ¥6 起",
                "quantity": 1,
            },
        },
        "final_decision": "accepted",
        "reasons": ["single SKU exact match"],
    }


class IngestCollectionOutputTests(RuntimeTestCase):
    def test_valid_ingest_writes_evidence_and_finalizes_seed_removal(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=11).data["run"]["run_id"]
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {sampled_seed.seed_id: ["органайзер для хранения"]})
        sampled_seed = repo.load_sampled_seeds(run_id)[0]

        result = service.ingest_collection_output(run_id, {"collection_pairs": [build_pair_payload(sampled_seed)]})

        self.assertTrue(result.ok)
        self.assertEqual("ingest.accepted", result.code)
        evidence_path = repo.run_dir(run_id) / "evidence.csv"
        self.assertTrue(evidence_path.exists())
        with evidence_path.open("r", encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual("Home / Storage", row["category_path"])
        self.assertEqual("Storage", row["leaf_category"])
        self.assertEqual("https://www.ozon.ru/category/storage-1234/", row["category_url"])
        self.assertEqual("1234", row["category_id"])
        self.assertIn("attribute_table", row["ozon_content_score_evidence"])
        self.assertEqual("¥6", row["supplier_domestic_shipping_fee"])
        self.assertEqual("广东阳江 -> 福建泉州", row["supplier_domestic_shipping_destination"])
        self.assertIn("运费 ¥6 起", row["supplier_domestic_shipping_evidence"])
        self.assertEqual(1999, len(repo.load_active_seeds()))
        self.assertIn(sampled_seed.seed_id, repo.load_used_seed_ids())

    def test_partial_batch_is_rejected_without_finalizing_sampled_seeds(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=2, random_seed=17).data["run"]["run_id"]
        sampled_seeds = repo.load_sampled_seeds(run_id)
        service.attach_seed_queries(
            run_id,
            {seed.seed_id: ["органайзер для хранения"] for seed in sampled_seeds},
        )
        sampled_seeds = repo.load_sampled_seeds(run_id)

        result = service.ingest_collection_output(run_id, {"collection_pairs": [build_pair_payload(sampled_seeds[0])]})

        self.assertFalse(result.ok)
        self.assertEqual("ingest.invalid_collection_output", result.code)
        self.assertIn("must include exactly 2 pairs", " ".join(result.errors))
        self.assertIn(sampled_seeds[1].seed_id, " ".join(result.errors))
        self.assertEqual(2000, len(repo.load_active_seeds()))
        self.assertEqual(set(), repo.load_used_seed_ids())

    def test_replaced_seed_attempt_is_removed_on_finalization(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=2, random_seed=17).data["run"]["run_id"]
        sampled_seeds = repo.load_sampled_seeds(run_id)
        service.attach_seed_queries(
            run_id,
            {seed.seed_id: ["органайзер для хранения"] for seed in sampled_seeds},
        )
        rejected_seed_id = sampled_seeds[0].seed_id

        replace_result = service.replace_sampled_seed(
            run_id,
            rejected_seed_id,
            reason="no Chinese cross-border Ozon candidate found",
            random_seed=99,
        )

        self.assertTrue(replace_result.ok)
        replacement_seed_id = replace_result.data["replacement_seed"]["seed_id"]
        current_sampled = repo.load_sampled_seeds(run_id)
        self.assertEqual(2, len(current_sampled))
        self.assertNotIn(rejected_seed_id, {seed.seed_id for seed in current_sampled})
        self.assertIn(replacement_seed_id, {seed.seed_id for seed in current_sampled})
        self.assertEqual("next.ozon_collection_contract", service.next_action(run_id).code)
        service.attach_seed_queries(
            run_id,
            {seed.seed_id: ["органайзер для хранения"] for seed in current_sampled},
        )
        current_sampled = repo.load_sampled_seeds(run_id)
        pairs = []
        for index, seed in enumerate(current_sampled, start=1):
            pair = build_pair_payload(seed)
            pair["pair_id"] = f"pair-{index}"
            pairs.append(pair)

        result = service.ingest_collection_output(run_id, {"collection_pairs": pairs})

        self.assertTrue(result.ok)
        self.assertEqual(1997, len(repo.load_active_seeds()))
        used_ids = repo.load_used_seed_ids()
        self.assertIn(rejected_seed_id, used_ids)
        self.assertIn(replacement_seed_id, used_ids)
        run = repo.load_run(run_id)
        self.assertIn(rejected_seed_id, run["rejected_seed_ids"])
        self.assertIn(rejected_seed_id, run["removed_seed_ids"])

    def test_non_exact_supplier_match_is_rejected_by_validation(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=12).data["run"]["run_id"]
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {sampled_seed.seed_id: ["органайзер для хранения"]})
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        pair = build_pair_payload(sampled_seed)
        pair["supplier_match"]["exact_match_decision"] = {"is_exact": False, "confidence": "low"}

        result = service.ingest_collection_output(run_id, {"collection_pairs": [pair]})

        self.assertFalse(result.ok)
        self.assertEqual("ingest.invalid_collection_output", result.code)

    def test_duplicate_existing_store_candidate_is_rejected_by_ingest(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        repo.replace_existing_products(
            [
                ExistingStoreProduct(
                    store_product_id="existing-1",
                    title="Test product",
                    normalized_identity_key="testproduct",
                )
            ]
        )
        run_id = service.start_run(target_count=1, random_seed=13).data["run"]["run_id"]
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {sampled_seed.seed_id: ["органайзер для хранения"]})
        sampled_seed = repo.load_sampled_seeds(run_id)[0]

        result = service.ingest_collection_output(run_id, {"collection_pairs": [build_pair_payload(sampled_seed)]})

        self.assertFalse(result.ok)
        self.assertIn("matches existing store product", " ".join(result.errors))

    def test_missing_precise_category_is_rejected_by_validation(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=14).data["run"]["run_id"]
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {sampled_seed.seed_id: ["органайзер для хранения"]})
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        pair = build_pair_payload(sampled_seed)
        pair["ozon_candidate"]["leaf_category"] = ""

        result = service.ingest_collection_output(run_id, {"collection_pairs": [pair]})

        self.assertFalse(result.ok)
        self.assertIn("precise leaf_category", " ".join(result.errors))

    def test_missing_content_score_evidence_is_rejected_by_validation(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=18).data["run"]["run_id"]
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {sampled_seed.seed_id: ["органайзер для хранения"]})
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        pair = build_pair_payload(sampled_seed)
        pair["ozon_candidate"]["content_score_evidence"] = {}

        result = service.ingest_collection_output(run_id, {"collection_pairs": [pair]})

        self.assertFalse(result.ok)
        self.assertIn("content_score_evidence", " ".join(result.errors))

    def test_missing_supplier_shipping_fee_is_rejected_by_validation(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=15).data["run"]["run_id"]
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {sampled_seed.seed_id: ["органайзер для хранения"]})
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        pair = build_pair_payload(sampled_seed)
        pair["supplier_match"]["domestic_shipping_fee"] = ""

        result = service.ingest_collection_output(run_id, {"collection_pairs": [pair]})

        self.assertFalse(result.ok)
        self.assertIn("real domestic shipping fee", " ".join(result.errors))

    def test_free_shipping_label_is_rejected_as_supplier_shipping_fee(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=16).data["run"]["run_id"]
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {sampled_seed.seed_id: ["органайзер для хранения"]})
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        pair = build_pair_payload(sampled_seed)
        pair["supplier_match"]["domestic_shipping_fee"] = "包邮"

        result = service.ingest_collection_output(run_id, {"collection_pairs": [pair]})

        self.assertFalse(result.ok)
        self.assertIn("not free-shipping label", " ".join(result.errors))

    def test_concrete_free_shipping_context_can_be_recorded_as_zero_fee(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=17).data["run"]["run_id"]
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        service.attach_seed_queries(run_id, {sampled_seed.seed_id: ["органайзер для хранения"]})
        sampled_seed = repo.load_sampled_seeds(run_id)[0]
        pair = build_pair_payload(sampled_seed)
        pair["supplier_match"]["moq"] = "1件起批"
        pair["supplier_match"]["domestic_shipping_fee"] = "¥0"
        pair["supplier_match"]["domestic_shipping_destination"] = "广东汕头 -> 福建泉州"
        pair["supplier_match"]["domestic_shipping_evidence"] = {
            "raw_text": "1件起批 | 广东汕头 送至 福建泉州 | 50件以内 | 包邮",
            "quantity_context": "50件以内",
            "normalization": "concrete free-shipping delivery line recorded as ¥0",
        }

        result = service.ingest_collection_output(run_id, {"collection_pairs": [pair]})

        self.assertTrue(result.ok, result.errors)
