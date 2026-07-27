from __future__ import annotations

from ozon_v2.domain.models import (
    CollectionPair,
    OzonCandidate,
    PairStatus,
    QueryGenerationStatus,
    SeedProduct,
    SeedSearchQuery,
    SupplierMatch,
)
from ozon_v2.domain.policies import contains_cjk, generated_query_terms_are_safe, seed_has_generated_ozon_query

ATTRIBUTE_TEMPLATE_WORKERS = {"microsoft_playwright_mcp", "codex_in_app_browser", "workbench_browser_bridge"}


def validate_seed_product(seed: SeedProduct) -> list[str]:
    errors: list[str] = []
    if not seed.seed_id.strip():
        errors.append("seed_id is required")
    if not seed.title_or_keyword.strip():
        errors.append("title_or_keyword is required")
    if seed.source_language != "zh-CN":
        errors.append("seed source_language must be zh-CN for bundled seeds")
    return errors


def validate_seed_search_query(query: SeedSearchQuery) -> list[str]:
    errors: list[str] = []
    if not query.seed_id.strip():
        errors.append("query seed_id is required")
    if not generated_query_terms_are_safe(query.source_text_zh, query.ozon_query_terms_ru):
        errors.append("Ozon query terms must be non-Chinese market terms, not raw Chinese seed text")
    return errors


def validate_seed_ready_for_ozon(seed: SeedProduct) -> list[str]:
    errors = validate_seed_product(seed)
    if not seed_has_generated_ozon_query(seed):
        errors.append("generated Russian-first Ozon query terms are required before Ozon collection")
    return errors


def validate_attribute_template_result(
    payload: dict,
    expected_seed_ids: list[str],
    *,
    require_seller_schema: bool = False,
) -> list[str]:
    errors: list[str] = []
    if payload.get("worker") not in ATTRIBUTE_TEMPLATE_WORKERS:
        errors.append("attribute template result must come from an approved browser worker")
    templates = payload.get("seed_templates")
    if not isinstance(templates, list) or not templates:
        errors.append("attribute template result must include seed_templates")
        return errors
    seen_seed_ids = {str(item.get("seed_id", "")).strip() for item in templates if isinstance(item, dict)}
    missing_seed_ids = [seed_id for seed_id in expected_seed_ids if seed_id not in seen_seed_ids]
    if missing_seed_ids:
        errors.append(f"attribute template result missing seed_ids: {', '.join(missing_seed_ids)}")
    for item in templates:
        if not isinstance(item, dict):
            errors.append("each seed template must be an object")
            continue
        seed_id = str(item.get("seed_id", "")).strip()
        if not seed_id:
            errors.append("seed template seed_id is required")
        category_candidates = item.get("category_candidates")
        if not isinstance(category_candidates, list) or not category_candidates:
            errors.append(f"seed {seed_id}: category_candidates are required")
        else:
            candidate = category_candidates[0] if isinstance(category_candidates[0], dict) else {}
            for field in ["category_path", "leaf_category", "category_url", "category_id"]:
                if not str(candidate.get(field, "")).strip():
                    errors.append(f"seed {seed_id}: first category candidate missing {field}")
            category_id = str(candidate.get("category_id", "")).strip()
            if category_id and not category_id.isdigit():
                errors.append(f"seed {seed_id}: first category candidate must include numeric category_id")
        public_evidence = item.get("public_attribute_evidence")
        attribute_table = public_evidence.get("attribute_table") if isinstance(public_evidence, dict) else None
        placeholder_values = {"visible_on_detail_page", "unavailable", "unknown", "n/a"}
        if not isinstance(attribute_table, dict) or not attribute_table or any(
            not str(key).strip() or str(value).strip().lower() in placeholder_values
            for key, value in (attribute_table or {}).items()
        ):
            errors.append(f"seed {seed_id}: public_attribute_evidence must include real product attribute values")
        if require_seller_schema:
            seller_template = item.get("seller_attribute_template")
            if not isinstance(seller_template, dict):
                errors.append(f"seed {seed_id}: seller_attribute_template is required")
            else:
                if seller_template.get("source") != "ozon_seller_api_description_category_attribute":
                    errors.append(f"seed {seed_id}: seller_attribute_template must come from Ozon Seller API")
                for field in ["description_category_id", "type_id", "matched_category_path"]:
                    if not str(seller_template.get(field, "")).strip():
                        errors.append(f"seed {seed_id}: seller_attribute_template missing {field}")
            attribute_schema = item.get("upload_attribute_schema")
            if not isinstance(attribute_schema, list) or not attribute_schema:
                errors.append(f"seed {seed_id}: upload_attribute_schema is required")
            else:
                first_attribute = attribute_schema[0] if isinstance(attribute_schema[0], dict) else {}
                for field in ["attribute_id", "attribute_label", "attribute_type", "is_required"]:
                    if field not in first_attribute:
                        errors.append(f"seed {seed_id}: first attribute missing {field}")
                for index, attribute in enumerate(attribute_schema, start=1):
                    if not isinstance(attribute, dict):
                        errors.append(f"seed {seed_id}: attribute {index} must be an object")
                        continue
                    if attribute.get("schema_source") != "ozon_seller_api_description_category_attribute":
                        errors.append(f"seed {seed_id}: attribute {index} is not from Ozon Seller API schema")
            prefill_plan = item.get("draft_prefill_plan")
            if not isinstance(prefill_plan, list) or not prefill_plan:
                errors.append(f"seed {seed_id}: draft_prefill_plan is required")
    return errors


def validate_ozon_collection_result(payload: dict, expected_seed_ids: list[str]) -> list[str]:
    errors: list[str] = []
    if payload.get("worker") not in ATTRIBUTE_TEMPLATE_WORKERS:
        errors.append("Ozon collection result must come from an approved browser worker")
    candidates = payload.get("ozon_candidates")
    if not isinstance(candidates, list) or not candidates:
        errors.append("Ozon collection result must include ozon_candidates")
        return errors
    candidate_seed_ids = [
        str(item.get("seed_id", "")).strip()
        for item in candidates
        if isinstance(item, dict)
    ]
    seen_seed_ids = set(candidate_seed_ids)
    if len(candidates) != len(expected_seed_ids):
        errors.append("Ozon collection result must include exactly one candidate per expected seed")
    duplicate_seed_ids = sorted(
        seed_id for seed_id in seen_seed_ids if seed_id and candidate_seed_ids.count(seed_id) > 1
    )
    if duplicate_seed_ids:
        errors.append(f"Ozon collection result has duplicate seed_ids: {', '.join(duplicate_seed_ids)}")
    candidate_product_ids = [
        str(item.get("ozon_product_id", "")).strip()
        for item in candidates
        if isinstance(item, dict)
    ]
    seen_product_ids = set(candidate_product_ids)
    duplicate_product_ids = sorted(
        product_id for product_id in seen_product_ids
        if product_id and candidate_product_ids.count(product_id) > 1
    )
    if duplicate_product_ids:
        errors.append(
            f"Ozon collection result has duplicate ozon_product_ids: {', '.join(duplicate_product_ids)}"
        )
    expected_seed_id_set = set(expected_seed_ids)
    unexpected_seed_ids = sorted(seed_id for seed_id in seen_seed_ids if seed_id not in expected_seed_id_set)
    if unexpected_seed_ids:
        errors.append(f"Ozon collection result has unexpected seed_ids: {', '.join(unexpected_seed_ids)}")
    missing_seed_ids = [seed_id for seed_id in expected_seed_ids if seed_id not in seen_seed_ids]
    if missing_seed_ids:
        errors.append(f"Ozon collection result missing seed_ids: {', '.join(missing_seed_ids)}")
    for index, item in enumerate(candidates, start=1):
        if not isinstance(item, dict):
            errors.append(f"Ozon candidate {index} must be an object")
            continue
        try:
            candidate = OzonCandidate.from_dict(item)
        except (TypeError, ValueError, KeyError) as exc:
            errors.append(f"Ozon candidate {index} has invalid shape: {exc}")
            continue
        errors.extend([f"Ozon candidate {candidate.seed_id}: {error}" for error in validate_ozon_candidate(candidate)])
    return errors


def validate_ozon_candidate(candidate: OzonCandidate) -> list[str]:
    errors: list[str] = []
    if not candidate.seed_id.strip():
        errors.append("Ozon candidate seed_id is required")
    if not candidate.ozon_product_id.strip():
        errors.append("ozon_product_id is required")
    if not candidate.ozon_url.strip():
        errors.append("ozon_url is required")
    if not candidate.title.strip():
        errors.append("title is required")
    if not candidate.seller_name.strip() or candidate.seller_name.strip().lower() in {"unavailable", "unknown"}:
        errors.append("seller_name is required")
    if not (candidate.seller_url or "").strip():
        errors.append("seller_url is required")
    if not str((candidate.seller_evidence or {}).get("raw_text") or "").strip():
        errors.append("seller_evidence raw_text is required")
    if not candidate.target_sku.sku_id.strip():
        errors.append("one Ozon target_sku is required")
    if not candidate.target_sku.selected_options:
        errors.append("one Ozon target_sku must include selected_options")
    if not candidate.selected_sku_media.main_gallery_images:
        errors.append("Ozon candidate must include main gallery images")
    if not candidate.selected_sku_media.selected_sku_images:
        errors.append("Ozon candidate must include selected SKU images")
    if not candidate.ozon_query_terms_ru:
        errors.append("Ozon candidate must include generated ozon_query_terms_ru")
    if any(contains_cjk(term) for term in candidate.ozon_query_terms_ru):
        errors.append("Ozon query terms must not be raw Chinese seed text")
    if not (candidate.category_path or "").strip():
        errors.append("Ozon candidate must include precise category_path")
    if not (candidate.leaf_category or "").strip():
        errors.append("Ozon candidate must include precise leaf_category")
    if not (candidate.category_url or "").strip():
        errors.append("Ozon candidate must include precise category_url")
    if not (candidate.category_id or "").strip():
        errors.append("Ozon candidate must include precise category_id")
    elif not str(candidate.category_id).isdigit():
        errors.append("Ozon candidate precise category_id must be numeric")
    if not (candidate.price or "").strip():
        errors.append("Ozon candidate price is required")
    if not (candidate.currency or "").strip():
        errors.append("Ozon candidate currency is required")
    if not (candidate.rating or "").strip():
        errors.append("Ozon candidate rating is required")
    if candidate.review_count is None:
        errors.append("Ozon candidate review_count is required")
    seller_decision = candidate.domestic_seller_decision or {}
    signals = seller_decision.get("signals") or []
    has_china_product_origin = (
        seller_decision.get("is_chinese_domestic_seller") is True
        and seller_decision.get("confidence") == "high"
        and any(
            isinstance(signal, dict)
            and signal.get("kind") == "product_origin_china"
            and bool(str(signal.get("raw_text") or "").strip())
            for signal in signals
        )
    )
    if not has_china_product_origin:
        for field in ["delivery_origin", "delivery_time", "fulfillment_label"]:
            if not str(getattr(candidate, field) or "").strip():
                errors.append(f"Ozon candidate {field} is required")
    placeholder_values = {"visible_on_detail_page", "unavailable", "unknown", "n/a"}
    if not candidate.attributes or any(
        not str(key).strip() or str(value).strip().lower() in placeholder_values
        for key, value in candidate.attributes.items()
    ):
        errors.append("Ozon candidate must include real product attribute values")
    if not candidate.content_score_evidence:
        errors.append("Ozon candidate must include content_score_evidence for later content optimization")
    else:
        for field in ["title_raw", "attribute_table", "main_gallery_images"]:
            if not candidate.content_score_evidence.get(field):
                errors.append(f"Ozon candidate content_score_evidence missing {field}")
    if (
        seller_decision.get("is_chinese_domestic_seller") is not True
        or seller_decision.get("confidence") != "high"
    ):
        errors.append("Ozon candidate must be from a verified Chinese cross-border seller")
    strong_signal_kinds = {
        "delivery_origin_china",
        "ozon_global_china",
        "product_origin_china",
        "seller_legal_entity_china",
        "seller_warehouse_china",
    }
    has_strong_signal = any(
        isinstance(signal, dict)
        and signal.get("kind") in strong_signal_kinds
        and bool(str(signal.get("raw_text") or "").strip())
        for signal in signals
    )
    if not has_strong_signal:
        errors.append("Ozon candidate must include strong Chinese cross-border seller evidence")
    return errors


def validate_supplier_match(supplier_match: SupplierMatch) -> list[str]:
    errors: list[str] = []
    if not supplier_match.supplier_product_id.strip():
        errors.append("supplier_product_id is required")
    if not supplier_match.supplier_url.strip():
        errors.append("supplier_url is required")
    if not supplier_match.matched_supplier_sku.sku_id.strip():
        errors.append("one matched_supplier_sku is required")
    decision = supplier_match.exact_match_decision
    if not decision:
        errors.append("exact_match_decision evidence is required")
    elif decision.get("is_exact") is not True:
        errors.append("1688 supplier match must be exact")
    shipping_fee = (supplier_match.domestic_shipping_fee or "").strip()
    shipping_evidence = supplier_match.domestic_shipping_evidence
    if not shipping_fee:
        errors.append("1688 supplier match must include real domestic shipping fee")
    elif shipping_fee.lower() in {"free", "free shipping"} or "包邮" in shipping_fee:
        errors.append("1688 domestic shipping fee must be a real fee, not free-shipping label")
    if not (supplier_match.domestic_shipping_destination or "").strip():
        errors.append("1688 supplier match must include domestic shipping destination")
    if not shipping_evidence or not str(shipping_evidence.get("raw_text", "")).strip():
        errors.append("1688 supplier match must include raw domestic shipping evidence")
    return errors


def validate_collection_pair(pair: CollectionPair) -> list[str]:
    errors: list[str] = []
    errors.extend(validate_seed_ready_for_ozon(pair.seed_product))
    errors.extend(validate_ozon_candidate(pair.ozon_candidate))
    errors.extend(validate_supplier_match(pair.supplier_match))
    if pair.seed_product.seed_id != pair.ozon_candidate.seed_id:
        errors.append("seed_id mismatch between seed and Ozon candidate")
    if pair.final_decision == PairStatus.ACCEPTED and pair.supplier_match.exact_match_decision.get("is_exact") is not True:
        errors.append("accepted pair requires exact supplier match")
    return errors
