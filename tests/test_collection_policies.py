from __future__ import annotations

from unittest import TestCase

from ozon_v2.domain.models import ExistingStoreProduct, OzonCandidate, SeedProduct, SelectedSkuMedia, TargetSku
from ozon_v2.domain.policies import (
    contains_cjk,
    decide_ozon_candidate_dedupe,
    decide_seed_existing_product_dedupe,
    generated_query_terms_are_safe,
    pair_status_for_dedupe_decision,
)
from ozon_v2.domain.validators import (
    validate_attribute_template_result,
    validate_ozon_candidate,
    validate_ozon_collection_result,
)


class CollectionPolicyTests(TestCase):
    def test_attribute_template_rejects_url_category_id_and_placeholder_attributes(self) -> None:
        errors = validate_attribute_template_result(
            {
                "worker": "workbench_browser_bridge",
                "seed_templates": [
                    {
                        "seed_id": "seed-test",
                        "category_candidates": [
                            {
                                "category_path": "Office / Paper trays",
                                "leaf_category": "Paper trays",
                                "category_url": "https://www.ozon.ru/category/paper-trays-123/",
                                "category_id": "https://www.ozon.ru/category/paper-trays-123/",
                            }
                        ],
                        "public_attribute_evidence": {
                            "attribute_table": {"Material": "visible_on_detail_page"},
                        },
                    }
                ],
            },
            ["seed-test"],
        )

        joined = " | ".join(errors)
        self.assertIn("numeric category_id", joined)
        self.assertIn("real product attribute values", joined)

    def test_ozon_candidate_requires_verified_chinese_cross_border_seller(self) -> None:
        candidate = self.ozon_candidate(
            {
                "is_chinese_domestic_seller": None,
                "confidence": "unknown",
                "signals": [],
            }
        )

        errors = validate_ozon_candidate(candidate)

        self.assertIn("Ozon candidate must be from a verified Chinese cross-border seller", errors)

    def test_ozon_candidate_accepts_strong_china_delivery_evidence(self) -> None:
        candidate = self.ozon_candidate(
            {
                "is_chinese_domestic_seller": True,
                "confidence": "high",
                "signals": [
                    {
                        "kind": "delivery_origin_china",
                        "raw_text": "Доставка из Китая",
                    }
                ],
            }
        )

        errors = validate_ozon_candidate(candidate)

        self.assertNotIn("Ozon candidate must be from a verified Chinese cross-border seller", errors)
        self.assertNotIn("Ozon candidate must include strong Chinese cross-border seller evidence", errors)

    def test_ozon_candidate_accepts_chinese_seller_warehouse_evidence(self) -> None:
        candidate = self.ozon_candidate(
            {
                "is_chinese_domestic_seller": True,
                "confidence": "high",
                "signals": [
                    {
                        "kind": "seller_warehouse_china",
                        "raw_text": "Со склада продавца, Beijing",
                    }
                ],
            }
        )

        errors = validate_ozon_candidate(candidate)

        self.assertNotIn("Ozon candidate must include strong Chinese cross-border seller evidence", errors)

    def test_ozon_candidate_accepts_product_origin_china_evidence(self) -> None:
        candidate = self.ozon_candidate(
            {
                "is_chinese_domestic_seller": True,
                "confidence": "high",
                "signals": [
                    {
                        "kind": "product_origin_china",
                        "raw_text": "原产国：中国",
                    }
                ],
            }
        )

        errors = validate_ozon_candidate(candidate)

        self.assertNotIn("Ozon candidate must include strong Chinese cross-border seller evidence", errors)

    def test_product_origin_china_does_not_require_delivery_display_fields(self) -> None:
        candidate = self.ozon_candidate(
            {
                "is_chinese_domestic_seller": True,
                "confidence": "high",
                "signals": [
                    {
                        "kind": "product_origin_china",
                        "raw_text": "原产国：中国",
                    }
                ],
            }
        )
        candidate.delivery_origin = None
        candidate.delivery_time = None
        candidate.fulfillment_label = None

        errors = validate_ozon_candidate(candidate)

        self.assertNotIn("Ozon candidate delivery_origin is required", errors)
        self.assertNotIn("Ozon candidate delivery_time is required", errors)
        self.assertNotIn("Ozon candidate fulfillment_label is required", errors)

    def test_product_origin_china_allows_missing_optional_market_signals(self) -> None:
        candidate = self.ozon_candidate(
            {
                "is_chinese_domestic_seller": True,
                "confidence": "high",
                "signals": [
                    {
                        "kind": "product_origin_china",
                        "raw_text": "Страна-изготовитель: Китай",
                    }
                ],
            }
        )
        candidate.rating = None
        candidate.review_count = None

        errors = validate_ozon_candidate(candidate)

        self.assertNotIn("Ozon candidate rating is required", errors)
        self.assertNotIn("Ozon candidate review_count is required", errors)

    def test_ozon_candidate_rejects_incomplete_or_placeholder_public_data(self) -> None:
        candidate = self.ozon_candidate(self.verified_seller_decision())
        candidate.attributes = {"Материал": "visible_on_detail_page"}
        candidate.selected_sku_media = SelectedSkuMedia()
        candidate.seller_url = None
        candidate.delivery_origin = None

        errors = validate_ozon_candidate(candidate)
        joined = " | ".join(errors)

        self.assertIn("real product attribute values", joined)
        self.assertIn("main gallery images", joined)
        self.assertIn("seller_url", joined)
        self.assertIn("delivery_origin", joined)

    def test_ozon_collection_rejects_multiple_candidates_for_one_seed(self) -> None:
        candidate = self.ozon_candidate(self.verified_seller_decision()).to_dict()

        errors = validate_ozon_collection_result(
            {
                "worker": "workbench_browser_bridge",
                "ozon_candidates": [candidate, candidate],
            },
            ["seed-test"],
        )

        self.assertIn("Ozon collection result must include exactly one candidate per expected seed", errors)
        self.assertIn("Ozon collection result has duplicate seed_ids: seed-test", errors)

    def test_ozon_collection_rejects_candidate_without_matching_subject_evidence(self) -> None:
        candidate = self.ozon_candidate(self.verified_seller_decision()).to_dict()

        errors = validate_ozon_collection_result(
            {
                "worker": "workbench_browser_bridge",
                "ozon_candidates": [candidate],
            },
            ["seed-test"],
            subject_contracts={
                "seed-test": {
                    "seed_id": "seed-test",
                    "required_stems": ["план", "запи", "счет", "ганд"],
                    "minimum_matches": 3,
                    "minimum_match_ratio": 0.70,
                }
            },
        )

        self.assertIn(
            "Ozon candidate seed-test: subject_match_evidence is required",
            errors,
        )

    def test_ozon_collection_rejects_candidate_for_unexpected_seed(self) -> None:
        expected = self.ozon_candidate(self.verified_seller_decision()).to_dict()
        unexpected = dict(expected)
        unexpected["seed_id"] = "seed-other"

        errors = validate_ozon_collection_result(
            {
                "worker": "workbench_browser_bridge",
                "ozon_candidates": [expected, unexpected],
            },
            ["seed-test"],
        )

        self.assertIn("Ozon collection result must include exactly one candidate per expected seed", errors)
        self.assertIn("Ozon collection result has unexpected seed_ids: seed-other", errors)

    def test_ozon_collection_rejects_duplicate_ozon_product_ids_across_seeds(self) -> None:
        first = self.ozon_candidate(self.verified_seller_decision()).to_dict()
        second = dict(first)
        second["seed_id"] = "seed-other"

        errors = validate_ozon_collection_result(
            {
                "worker": "workbench_browser_bridge",
                "ozon_candidates": [first, second],
            },
            ["seed-test", "seed-other"],
        )

        self.assertIn("Ozon collection result has duplicate ozon_product_ids: ozon-1", errors)

    def test_existing_store_product_blocks_seed(self) -> None:
        seed = SeedProduct(seed_id="seed-0001", title_or_keyword="收纳盒", product_clue="收纳盒")
        existing = ExistingStoreProduct(
            store_product_id="store-1",
            title="收纳盒",
            normalized_identity_key="收纳盒",
        )

        decision = decide_seed_existing_product_dedupe(seed, [existing])

        self.assertEqual("duplicate", decision.kind.value)
        self.assertEqual("store-1", decision.matched_product_id)

    def test_existing_store_russian_title_blocks_seed_by_generated_query(self) -> None:
        seed = SeedProduct(
            seed_id="seed-0002",
            title_or_keyword="电子桌面时钟",
            product_clue="电子桌面时钟",
            ozon_query_terms_ru=[
                "электронные настольные часы с календарем",
                "купить электронные настольные часы с календарем",
            ],
            query_generation_status="generated",
        )
        existing = ExistingStoreProduct(
            store_product_id="store-2",
            title="Электронные настольные часы с календарем, белый корпус",
            normalized_identity_key="электронныенастольныечасыскалендарембелыйкорпус",
        )

        decision = decide_seed_existing_product_dedupe(seed, [existing])

        self.assertEqual("duplicate", decision.kind.value)
        self.assertEqual("store-2", decision.matched_product_id)
        self.assertEqual("generated_query", decision.evidence["matched_by"])

    def test_existing_store_exact_ozon_product_id_blocks_renamed_candidate(self) -> None:
        candidate = self.ozon_candidate(self.verified_seller_decision())
        candidate.title = "Completely renamed public title"
        existing = ExistingStoreProduct(
            store_product_id="ozon-1",
            title="An older unrelated title",
            normalized_identity_key="anolderunrelatedtitle",
        )

        decision = decide_ozon_candidate_dedupe(candidate, [existing])

        self.assertEqual("duplicate", decision.kind.value)
        self.assertEqual("ozon-1", decision.matched_product_id)
        self.assertEqual("ozon_product_id", decision.evidence["matched_by"])

    def test_chinese_query_terms_are_not_safe_for_ozon(self) -> None:
        self.assertTrue(contains_cjk("收纳盒"))
        self.assertFalse(generated_query_terms_are_safe("收纳盒", ["收纳盒"]))
        self.assertTrue(generated_query_terms_are_safe("收纳盒", ["органайзер для хранения"]))

    def test_possible_duplicate_maps_to_manual_review(self) -> None:
        seed = SeedProduct(seed_id="seed-empty", title_or_keyword="", product_clue="")

        decision = decide_seed_existing_product_dedupe(seed, [])

        self.assertEqual("possible_duplicate", decision.kind.value)
        self.assertEqual("needs_manual_review", pair_status_for_dedupe_decision(decision).value)

    def ozon_candidate(self, decision: dict) -> OzonCandidate:
        return OzonCandidate(
            seed_id="seed-test",
            seed_title_or_keyword="速干T恤",
            seed_source_language="zh-CN",
            ozon_query_terms_ru=["быстросохнущая футболка"],
            ozon_product_id="ozon-1",
            ozon_url="https://www.ozon.ru/product/test-1/",
            title="Test product",
            seller_name="Test seller",
            seller_url="https://www.ozon.ru/seller/test-seller-1/",
            seller_evidence={"raw_text": "Доставка из Китая"},
            target_sku=TargetSku(sku_id="sku-1", selected_options={"single_sku": "visible"}),
            selected_sku_media=SelectedSkuMedia(
                main_gallery_images=["https://cdn.ozon.ru/product/main.jpg"],
                selected_sku_images=["https://cdn.ozon.ru/product/main.jpg"],
            ),
            category_path="Одежда / Футболки",
            leaf_category="Футболка",
            category_url="https://www.ozon.ru/category/futbolki-1/",
            category_id="1",
            price="799",
            currency="RUB",
            rating="4.9",
            review_count=120,
            delivery_origin="Доставка из Китая",
            delivery_time="15-30 дней",
            fulfillment_label="Ozon Global",
            attributes={"Материал": "Резина"},
            domestic_seller_decision=decision,
            content_score_evidence={
                "title_raw": "Test product",
                "attribute_table": {"Материал": "Резина"},
                "main_gallery_images": ["https://cdn.ozon.ru/product/main.jpg"],
            },
        )

    def verified_seller_decision(self) -> dict:
        return {
            "is_chinese_domestic_seller": True,
            "confidence": "high",
            "signals": [
                {
                    "kind": "delivery_origin_china",
                    "raw_text": "Delivery from China",
                }
            ],
        }
