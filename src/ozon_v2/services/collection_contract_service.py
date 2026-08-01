from __future__ import annotations

import json

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.playwright_mcp_contract import PlaywrightMcpContractAdapter
from ozon_v2.app.result import Result
from ozon_v2.domain.models import CollectionPair
from ozon_v2.domain.policies import seed_has_generated_ozon_query


REQUIRED_PUBLIC_CONTENT_SCORE_FIELDS = [
    "title_raw",
    "title_length",
    "description_or_rich_content_blocks",
    "attribute_table",
    "required_or_visible_attributes",
    "category_path",
    "leaf_category",
    "brand",
    "price",
    "currency",
    "old_price_or_discount",
    "seller_name",
    "seller_url",
    "delivery_origin",
    "delivery_time",
    "fulfillment_label",
    "main_gallery_images",
    "selected_sku_images",
    "detail_page_images",
    "image_style_notes",
]

RECOMMENDED_MARKET_CONTENT_SCORE_SIGNALS = [
    "rating",
    "review_count",
    "search_result_position",
    "visible_badges",
    "sales_or_popularity_signal",
    "cart_or_order_popularity",
    "promo_or_discount_signal",
    "price_competitiveness_signal",
    "competitor_count_or_repeated_presence",
]

OPTIONAL_EXTERNAL_ANALYTICS_FIELDS = [
    "monthly_sales",
    "monthly_revenue",
    "month_over_month_trend",
    "advertising_cost_share",
    "promotion_days",
    "promotion_discount",
    "paid_promo_days",
    "following_or_competing_sellers",
    "lowest_price",
    "highest_price",
    "product_views",
    "add_to_cart_rate",
    "search_category_views",
    "search_category_add_to_cart_rate",
    "display_total",
    "display_conversion_rate",
    "click_share",
    "fbs_commission",
    "fbp_commission",
    "commission_rate",
]

OBJECTIVE_ATTRIBUTE_PREFILL_FIELDS = [
    "dimensions",
    "weight",
    "material",
    "color",
    "size",
    "capacity",
    "quantity",
    "package_contents",
    "compatibility",
    "model",
    "voltage",
    "power",
    "age_group",
    "gender",
]

CREATIVE_FIELDS_REQUIRING_REWRITE = [
    "title",
    "description",
    "rich_content",
    "marketing_claims",
    "bullet_points",
    "seo_keywords",
    "image_text",
]


class CollectionContractService:
    def __init__(
        self,
        repo: FsRepo | None = None,
        adapter: PlaywrightMcpContractAdapter | None = None,
    ) -> None:
        self.repo = repo or FsRepo()
        self.adapter = adapter or PlaywrightMcpContractAdapter()

    def build_attribute_template_contract(self, run_id: str) -> Result:
        seeds = self._contract_seeds(run_id, result_kind="attribute_template")
        missing_query = [seed.seed_id for seed in seeds if not seed_has_generated_ozon_query(seed)]
        if missing_query:
            return Result.failure(
                "contract.missing_seed_queries",
                "Attribute template collection requires generated Russian-first query terms.",
                data={"missing_seed_ids": missing_query},
            )
        payload = {
            "run_id": run_id,
            "purpose": "fetch Ozon category and attribute templates immediately after seed selection",
            "rules": {
                "after_seed_selection_required": True,
                "before_ozon_product_collection_required": True,
                "do_not_search_raw_chinese_seed_text": True,
                "collect_category_candidates": True,
                "collect_public_attribute_evidence": True,
                "seller_api_upload_attribute_schema_required": True,
                "prefill_objective_attributes_only": True,
                "do_not_copy_creative_content": True,
                "ozon_proxy_required": True,
                "visitor_access_required": True,
                "no_logged_in_account": True,
            },
            "required_template_output": {
                "category_candidates": [
                    "category_path",
                    "leaf_category",
                    "category_url",
                    "category_id",
                    "confidence",
                    "source_evidence",
                ],
                "upload_attribute_schema": [
                    "attribute_id",
                    "attribute_label",
                    "attribute_type",
                    "is_required",
                    "allowed_values",
                    "unit",
                    "group",
                    "example_value_when_visible",
                ],
                "seller_attribute_template": [
                    "source",
                    "description_category_id",
                    "type_id",
                    "matched_category_path",
                    "match_confidence",
                ],
                "public_attribute_evidence": [
                    "visible_public_schema_guess",
                    "attribute_labels",
                ],
                "draft_prefill_plan": [
                    "field_key",
                    "source",
                    "prefill_allowed",
                    "rewrite_required",
                    "reason",
                ],
            },
            "draft_prefill_policy": {
                "objective_copy_allowed_fields": OBJECTIVE_ATTRIBUTE_PREFILL_FIELDS,
                "creative_rewrite_required_fields": CREATIVE_FIELDS_REQUIRING_REWRITE,
                "same_product_objective_values_may_match_ozon": True,
                "title_description_rich_content_must_not_match_ozon": True,
                "supplier_truth_overrides_conflicting_ozon_attributes": True,
            },
            "seeds": [
                {
                    "seed_id": seed.seed_id,
                    "source_text_zh": seed.title_or_keyword,
                    "ozon_query_terms_ru": seed.ozon_query_terms_ru,
                    "auxiliary_query_terms_en": seed.auxiliary_query_terms_en,
                }
                for seed in seeds
            ],
        }
        return Result.success(
            "contract.attribute_template.ready",
            "Ozon attribute template task contract generated.",
            self.adapter.ozon_attribute_template_contract(payload),
        )

    def build_ozon_collection_contract(self, run_id: str) -> Result:
        seeds = self._contract_seeds(run_id, result_kind="ozon_collection")
        missing_query = [seed.seed_id for seed in seeds if not seed_has_generated_ozon_query(seed)]
        if missing_query:
            return Result.failure(
                "contract.missing_seed_queries",
                "Ozon collection contract requires generated Russian-first query terms.",
                data={"missing_seed_ids": missing_query},
            )
        reusable_evidence: dict[str, dict] = {}
        try:
            template_result = self.repo.load_attribute_template_result(run_id)
        except (FileNotFoundError, json.JSONDecodeError):
            template_result = {}
        for item in template_result.get("seed_templates", []):
            if not isinstance(item, dict):
                continue
            seed_id = str(item.get("seed_id") or "").strip()
            evidence = item.get("evidence")
            if seed_id and isinstance(evidence, dict):
                reusable_evidence[seed_id] = evidence

        excluded_product_ids = set(self.repo.load_blacklisted_ozon_product_ids())
        try:
            previous_ozon_result = self.repo.load_ozon_collection_result(run_id)
        except (FileNotFoundError, json.JSONDecodeError):
            previous_ozon_result = {}
        excluded_product_ids.update(
            str(item.get("ozon_product_id"))
            for item in previous_ozon_result.get("ozon_candidates", [])
            if isinstance(item, dict) and item.get("ozon_product_id")
        )

        payload = {
            "run_id": run_id,
            "rules": {
                "single_sku_only": True,
                "do_not_search_raw_chinese_seed_text": True,
                "existing_store_dedupe_required": True,
                "precise_category_required": True,
                "required_precise_category_fields": [
                    "category_path",
                    "leaf_category",
                    "category_url",
                    "category_id",
                ],
                "content_score_evidence_required": True,
                "record_unavailable_content_score_fields": True,
                "attribute_template_prerequisite_required": True,
            },
            "attribute_template_prerequisite": {
                "must_run_after_seed_selection": True,
                "must_complete_before_ozon_collection": True,
                "objective_attributes_may_be_prefilled": OBJECTIVE_ATTRIBUTE_PREFILL_FIELDS,
                "creative_fields_must_be_rewritten": CREATIVE_FIELDS_REQUIRING_REWRITE,
            },
            "content_score_evidence": {
                "purpose": "collect detailed Ozon evidence for later content score optimization",
                "required_public_fields": REQUIRED_PUBLIC_CONTENT_SCORE_FIELDS,
                "recommended_market_signals": RECOMMENDED_MARKET_CONTENT_SCORE_SIGNALS,
                "optional_external_analytics_fields": OPTIONAL_EXTERNAL_ANALYTICS_FIELDS,
                "missing_field_policy": "record unavailable fields with reason; do not invent metrics",
            },
            "existing_store_dedupe": [product.to_dict() for product in self.repo.load_existing_products()],
            "excluded_ozon_product_ids": sorted(excluded_product_ids),
            "seeds": [
                {
                    "seed_id": seed.seed_id,
                    "source_text_zh": seed.title_or_keyword,
                    "ozon_query_terms_ru": seed.ozon_query_terms_ru,
                    "auxiliary_query_terms_en": seed.auxiliary_query_terms_en,
                    **(
                        {
                            "public_product_snapshot": reusable_evidence[seed.seed_id]["public_product_snapshot"],
                            "domestic_seller_decision": reusable_evidence[seed.seed_id].get("domestic_seller_decision", {}),
                        }
                        if isinstance(reusable_evidence.get(seed.seed_id, {}).get("public_product_snapshot"), dict)
                        else {}
                    ),
                }
                for seed in seeds
            ],
        }
        return Result.success(
            "contract.ozon_collection.ready",
            "Ozon collection task contract generated.",
            self.adapter.ozon_collection_contract(payload),
        )

    def _contract_seeds(self, run_id: str, *, result_kind: str) -> list:
        seeds = self.repo.load_sampled_seeds(run_id)
        run = self.repo.load_run(run_id)
        pending_ids = {
            str(seed_id)
            for seed_id in run.get("replacement_pending_seed_ids", [])
            if seed_id
        }
        if not pending_ids:
            return seeds
        try:
            if result_kind == "attribute_template":
                retained_result = self.repo.load_attribute_template_result(run_id)
                retained_items = retained_result.get("seed_templates", [])
            else:
                retained_result = self.repo.load_ozon_collection_result(run_id)
                retained_items = retained_result.get("ozon_candidates", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return seeds
        retained_seed_ids = {
            str(item.get("seed_id") or "")
            for item in retained_items
            if isinstance(item, dict) and item.get("seed_id")
        }
        required_retained_ids = {seed.seed_id for seed in seeds} - pending_ids
        if not required_retained_ids.issubset(retained_seed_ids):
            return seeds
        return [seed for seed in seeds if seed.seed_id in pending_ids]

    def build_supplier_collection_contract(self, pair: CollectionPair) -> Result:
        payload = {
            "pair_id": pair.pair_id,
            "seed_id": pair.seed_product.seed_id,
            "source_text_zh": pair.seed_product.title_or_keyword,
            "ozon_candidate": {
                "ozon_product_id": pair.ozon_candidate.ozon_product_id,
                "ozon_url": pair.ozon_candidate.ozon_url,
                "title": pair.ozon_candidate.title,
                "target_sku": pair.ozon_candidate.target_sku.to_dict(),
                "selected_sku_media": pair.ozon_candidate.selected_sku_media.to_dict(),
            },
            "rules": {
                "exact_same_product_required": True,
                "single_supplier_sku_only": True,
                "similar_product_is_rejected": True,
                "direct_network_no_proxy_required": True,
                "1688_homepage_entry_required": True,
                "1688_homepage_url": "https://www.1688.com/",
                "local_image_upload_required": True,
                "select_product_subject_before_search": True,
                "real_domestic_shipping_fee_required": True,
                "free_shipping_label_is_not_shipping_fee": True,
            },
        }
        return Result.success(
            "contract.supplier_collection.ready",
            "1688 exact-match task contract generated.",
            self.adapter.supplier_collection_contract(payload),
        )
