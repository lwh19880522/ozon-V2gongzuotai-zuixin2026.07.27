from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any


_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "brand": (
        "бренд",
        "бренд в одежде и обуви",
        "марка",
        "brand",
        "品牌",
    ),
    "type": ("тип", "тип товара", "вид товара", "type", "类型", "品类"),
    "model": ("модель", "название модели", "model", "型号"),
    "article": ("артикул", "код модели", "model code", "货号"),
    "color": ("цвет", "цвет товара", "название цвета", "color", "颜色"),
    "material": ("материал", "material", "材质", "刃口材质"),
    "country": ("страна изготовитель", "страна производства", "country of origin", "产地"),
    "quantity": (
        "количество в упаковке шт",
        "количество товара в оед",
        "количество товара в уеи",
        "единиц в одном товаре",
        "quantity",
        "数量",
    ),
    "factory_package_count": ("количество заводских упаковок",),
    "set_item_count": (
        "количество инструментов в наборе шт",
        "количество предметов в наборе шт",
    ),
    "seller_code": ("код продавца", "seller code", "offer id"),
    "package_contents": ("комплектация", "состав комплекта", "package contents", "包装清单"),
    "size": ("размер", "размеры мм", "size", "尺寸", "规格"),
    "length": ("длина мм", "длина см", "length", "长度", "全长"),
    "width": ("ширина мм", "ширина см", "width", "宽度"),
    "height": ("высота мм", "высота см", "height", "高度"),
    "weight": ("вес товара г", "вес г", "weight", "重量"),
    "package_length": (
        "длина упаковки см",
        "длина упаковки мм",
        "package length",
        "包装长度",
    ),
    "package_width": (
        "ширина упаковки см",
        "ширина упаковки мм",
        "package width",
        "包装宽度",
    ),
    "package_height": (
        "высота упаковки см",
        "высота упаковки мм",
        "package height",
        "包装高度",
    ),
    "package_weight": (
        "вес с упаковкой г",
        "вес в упаковке г",
        "package weight",
        "包装重量",
    ),
    "volume": ("объем мл", "объём мл", "volume", "容量"),
    "gender": ("пол ребенка", "пол", "gender", "性别"),
    "upper_material": (
        "материал верха",
        "upper material",
        "鞋面材质",
        "帮面材质",
        "鞋帮材质",
    ),
    "lining_material": (
        "материал подкладки обуви",
        "материал подкладки",
        "lining material",
        "内里材质",
        "里料材质",
        "鞋里材质",
    ),
    "sole_material": (
        "материал подошвы обуви",
        "материал подошвы",
        "sole material",
        "鞋底材质",
    ),
    "fastening_type": (
        "вид застежки",
        "fastening type",
        "闭合方式",
        "扣合方式",
        "鞋子闭合方式",
    ),
    "target_audience": ("целевая аудитория", "target audience", "适用人群"),
    "heel_height": ("высота каблука см", "heel height", "跟高", "鞋跟高度"),
    "shaft_height": ("высота голенища см", "shaft height", "筒高", "靴筒高度"),
    "insole_length": ("длина стельки см", "insole length", "鞋垫长", "内长"),
    "style": ("стиль", "style", "风格"),
    "decorative_elements": (
        "декоративные элементы",
        "decorative elements",
        "流行元素",
        "装饰",
    ),
    "season": ("сезон", "season", "适用季节", "上市季节"),
    "waterproof": ("непромокаемые", "waterproof", "防水"),
    "fit": ("посадка", "fit", "版型"),
    "collection": ("коллекция", "collection", "系列"),
    "manufacturer_size": (
        "размер производителя",
        "manufacturer size",
        "厂家尺码",
        "尺码",
    ),
    "size_information": (
        "информация о размерах",
        "size information",
        "尺码说明",
    ),
    "sole_attachment": (
        "метод крепления подошвы",
        "sole attachment",
        "鞋底工艺",
    ),
    "disable_product_grouping": (
        "объединить на одной карточке",
        "объединить в похожие товары",
    ),
    "title": ("название", "title"),
    "description": ("аннотация", "описание", "description"),
    "rich_content": ("rich контент json", "rich content", "rich_content"),
    "hashtags": ("хештеги", "hashtags"),
}

_ALIASES = {
    alias: canonical
    for canonical, aliases in _ALIAS_GROUPS.items()
    for alias in aliases
}
_CREATIVE_FIELDS = {"title", "description", "rich_content", "hashtags"}
_FIELD_CANDIDATE_POLICY_VERSION = 2
_VISUAL_INFERENCE_FIELDS = {
    "color",
    "factory_package_count",
    "set_item_count",
}
_SUPPLIER_IDENTITY_FIELDS = {"brand", "model", "article", "color"}
_SUPPLIER_TRUTH_SOURCES = {
    "confirmed_supplier_sku",
    "supplier_attributes",
    "locked_supplier_truth",
}
_STORE_FIXED_FIELDS: dict[str, dict[str, str]] = {
    "country": {
        "value": "Китай",
        "evidence_ref": "workflow.defaults.country_of_manufacture",
        "reason": (
            "The store ships products purchased from China; the user-approved "
            "country of manufacture is fixed to China for this workflow."
        ),
    },
    "disable_product_grouping": {
        "value": "Нет",
        "evidence_ref": "workflow.defaults.disable_product_grouping",
        "reason": (
            "The workbench creates one independently reviewed supplier SKU per "
            "Ozon product and does not auto-group it with other product cards."
        ),
    },
}
_OPTIONAL_ASSET_FIELDS = {
    "озон видеообложка ссылка",
    "озон видео название",
    "озон видео ссылка",
    "озон видео товары на видео",
    "документ pdf",
    "название файла pdf",
}


def normalize_attribute_label(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return " ".join(part for part in re.split(r"[^\w]+", text) if part)


def canonical_attribute_label(value: Any) -> str:
    normalized = normalize_attribute_label(value)
    if normalized.startswith("название модели"):
        return "model"
    return _ALIASES.get(normalized, normalized)


def is_visual_inference_field(value: Any) -> bool:
    return canonical_attribute_label(value) in _VISUAL_INFERENCE_FIELDS


def is_locked_supplier_internal_conflict(result: dict[str, Any]) -> bool:
    """Return whether an unresolved conflict is proven inside 1688 truth.

    Ozon fields are market-reference evidence only.  A difference between an
    Ozon reference and a locked supplier fact must therefore be re-drafted from
    the supplier fact, not treated as proof that the supplier binding is wrong.
    """

    if str(result.get("resolution_class") or "").strip() != "evidence_conflict":
        return False
    evidence_refs = {
        str(reference).strip()
        for reference in result.get("evidence_refs") or []
        if str(reference or "").strip()
    }
    if any(reference.startswith("ozon.") for reference in evidence_refs):
        return False
    supplier_refs = {
        reference
        for reference in evidence_refs
        if reference.startswith(
            ("supplier.", "supplier_selection.", "supplier_truth.")
        )
    }
    return len(supplier_refs) >= 2


def attribute_content_score_progress(
    fields: list[dict[str, Any]],
) -> dict[str, Any]:
    scorable = [
        field
        for field in fields
        if canonical_attribute_label(field.get("label")) not in _CREATIVE_FIELDS
        and normalize_attribute_label(field.get("label")) not in _OPTIONAL_ASSET_FIELDS
        and field.get("status") not in {"excluded", "not_applicable"}
    ]
    filled = sum(1 for field in scorable if field.get("status") == "mapped")
    total = len(scorable)
    completion = round((filled / total * 100), 1) if total else 0.0
    if completion >= 70:
        points = 30
        next_band_points = None
    elif completion >= 50:
        points = 15
        next_band_points = 30
    elif completion >= 15:
        points = 7.5
        next_band_points = 15
    else:
        points = 0
        next_band_points = 7.5
    target_50 = math.ceil(total * 0.5)
    target_70 = math.ceil(total * 0.7)
    return {
        "scorable_attribute_count": total,
        "filled_attribute_count": filled,
        "completion_percent": completion,
        "estimated_attribute_points": points,
        "fields_to_50_percent": max(0, target_50 - filled),
        "fields_to_70_percent": max(0, target_70 - filled),
        "next_band_points": next_band_points,
    }


def map_template_attributes(
    upload_schema: list[dict[str, Any]],
    ozon_candidate: dict[str, Any],
    *,
    supplier_product: dict[str, Any] | None = None,
    supplier_selection: dict[str, Any] | None = None,
    supplier_truth_profile: dict[str, Any] | None = None,
    rewritten_content: dict[str, Any] | None = None,
    pricing_evidence: dict[str, Any] | None = None,
    user_confirmed_required_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = _collect_evidence(
        ozon_candidate,
        supplier_product=supplier_product,
        supplier_selection=supplier_selection,
        supplier_truth_profile=supplier_truth_profile,
        pricing_evidence=pricing_evidence,
    )
    mapped_fields: list[dict[str, Any]] = []
    for schema_field in upload_schema:
        field_key = str(schema_field.get("attribute_id") or "").strip()
        label = str(schema_field.get("attribute_label") or "").strip()
        normalized_label = normalize_attribute_label(label)
        canonical_label = canonical_attribute_label(label)
        generated_result = _generated_field_result(
            (rewritten_content or {}).get(field_key)
        )
        required = schema_field.get("is_required") is True
        base = {
            "field_key": field_key,
            "label": label,
            "required": required,
            "required_reason": schema_field.get("required_reason"),
            "attribute_type": schema_field.get("attribute_type"),
            "dictionary_id": schema_field.get("dictionary_id"),
            "allowed_values": (
                list(schema_field.get("allowed_values"))
                if isinstance(schema_field.get("allowed_values"), list)
                else []
            ),
            "visual_inference_supported": is_visual_inference_field(label),
        }
        if canonical_label in _CREATIVE_FIELDS:
            if (
                generated_result
                and generated_result["decision"] == "filled"
                and _has_value(generated_result.get("value"))
            ):
                mapped_fields.append(
                    {
                        **base,
                        "status": "mapped",
                        "value": _plain_value(generated_result["value"]),
                        "source": "generated_original_content",
                        "source_label": label,
                        "evidence_ref": f"generated_content.fields.{field_key}",
                        "evidence_refs": generated_result.get("evidence_refs", []),
                        "mapping_method": "validated_original_content",
                        "dictionary_resolution_required": bool(schema_field.get("dictionary_id")),
                        "reason": generated_result.get("reason")
                        or "Validated original Russian content generated from collected facts.",
                    }
                )
                continue
            mapped_fields.append(
                {
                    **base,
                    "status": "rewrite_required",
                    "value": None,
                    "source": None,
                    "evidence_ref": None,
                    "mapping_method": "ozon_original_content_required",
                    "reference_evidence": _creative_reference_evidence(
                        canonical_label,
                        ozon_candidate,
                        supplier_product=supplier_product,
                        supplier_selection=supplier_selection,
                    ),
                    "reason": (
                        "Ozon original content must be generated from collected facts; "
                        "the source listing text must not be copied."
                    ),
                }
            )
            continue

        fixed_field = _STORE_FIXED_FIELDS.get(canonical_label)
        if fixed_field is not None:
            mapped_fields.append(
                {
                    **base,
                    "status": "mapped",
                    "value": fixed_field["value"],
                    "source": "store_fixed_policy",
                    "source_label": label,
                    "evidence_ref": fixed_field["evidence_ref"],
                    "mapping_method": "store_fixed_value",
                    "dictionary_resolution_required": bool(
                        schema_field.get("dictionary_id")
                    ),
                    "reason": fixed_field["reason"],
                }
            )
            continue

        candidates = [
            item
            for item in evidence
            if item["normalized_label"] == normalized_label
            or item["canonical_label"] == canonical_label
        ]
        if canonical_label in _SUPPLIER_IDENTITY_FIELDS and (supplier_product or supplier_selection):
            candidates = [
                item for item in candidates if item["source"] in _SUPPLIER_TRUTH_SOURCES
            ]
        candidates.sort(
            key=lambda item: (
                item["priority"],
                0 if item["normalized_label"] == normalized_label else 1,
            )
        )
        selected = candidates[0] if candidates else None
        if selected is None and canonical_label == "model":
            selected = next(
                (
                    item
                    for item in evidence
                    if item["canonical_label"] == "article"
                    and (
                        not (supplier_product or supplier_selection)
                        or item["source"] in _SUPPLIER_TRUTH_SOURCES
                    )
                ),
                None,
            )
            if selected is not None:
                selected = {**selected, "mapping_method": "model_identifier_fallback"}

        if selected is None and canonical_label == "brand":
            mapped_fields.append(
                {
                    **base,
                    "status": "mapped",
                    "value": "Нет бренда",
                    "source": "approved_no_brand_policy",
                    "source_label": label,
                    "evidence_ref": "workbench.policy.no_brand",
                    "mapping_method": "approved_no_brand_fallback",
                    "dictionary_resolution_required": bool(
                        schema_field.get("dictionary_id")
                    ),
                    "reason": (
                        "No verified brand was found in the locked supplier or "
                        "Ozon evidence; the user-approved Ozon no-brand value is used."
                    ),
                }
            )
            continue

        manual_entry = (user_confirmed_required_fields or {}).get(field_key)
        manual_value = (
            manual_entry.get("value")
            if isinstance(manual_entry, dict)
            else manual_entry
        )
        if selected is None and required and _has_value(manual_value):
            mapped_fields.append(
                {
                    **base,
                    "status": "mapped",
                    "value": _plain_value(manual_value),
                    "source": "user_confirmed_required_attribute",
                    "source_label": label,
                    "evidence_ref": (
                        f"required_attribute_evidence.items."
                        f"{ozon_candidate.get('seed_id')}.{field_key}"
                    ),
                    "mapping_method": "user_confirmed_missing_required_field",
                    "dictionary_resolution_required": bool(
                        schema_field.get("dictionary_id")
                    ),
                    "reason": (
                        "The user supplied this value specifically for a "
                        "missing Seller API required field."
                    ),
                }
            )
            continue

        if selected is not None and _customer_facing_value_needs_normalization(
            canonical_label,
            selected.get("value"),
        ):
            if (
                generated_result
                and generated_result.get("structured") is True
                and generated_result["decision"] == "filled"
                and _has_value(generated_result.get("value"))
                and _generated_objective_value_is_acceptable(
                    canonical_label,
                    generated_result.get("value"),
                )
            ):
                evidence_refs = generated_result.get("evidence_refs", [])
                mapped_fields.append(
                    {
                        **base,
                        "status": "mapped",
                        "value": _plain_value(generated_result["value"]),
                        "source": "generated_evidence_completion",
                        "source_label": label,
                        "evidence_ref": (
                            evidence_refs[0]
                            if evidence_refs
                            else selected["evidence_ref"]
                        ),
                        "evidence_refs": evidence_refs,
                        "mapping_method": "customer_facing_normalization",
                        "dictionary_resolution_required": bool(
                            schema_field.get("dictionary_id")
                        ),
                        "reason": generated_result.get("reason")
                        or (
                            "Supplier truth was translated and normalized for "
                            "the Russian customer-facing field."
                        ),
                    }
                )
            else:
                mapped_fields.append(
                    {
                        **base,
                        "status": "rewrite_required",
                        "value": None,
                        "source": selected["source"],
                        "source_label": selected["source_label"],
                        "evidence_ref": selected["evidence_ref"],
                        "reference_evidence": [selected["evidence_ref"]],
                        "mapping_method": (
                            "customer_facing_normalization_required"
                        ),
                        "dictionary_resolution_required": bool(
                            schema_field.get("dictionary_id")
                        ),
                        "reason": (
                            "Verified supplier truth contains Chinese text or "
                            "supplier fulfillment/promotional language. The "
                            "field Skill must preserve the product fact, "
                            "translate it to Russian, and remove the unrelated "
                            "seller wording before upload."
                        ),
                    }
                )
            continue

        if selected is None:
            if (
                generated_result
                and generated_result.get("structured") is True
                and generated_result["decision"] == "filled"
                and _has_value(generated_result.get("value"))
                and _generated_objective_value_is_acceptable(
                    canonical_label,
                    generated_result.get("value"),
                )
            ):
                evidence_refs = generated_result.get("evidence_refs", [])
                mapped_fields.append(
                    {
                        **base,
                        "status": "mapped",
                        "value": _plain_value(generated_result["value"]),
                        "source": "generated_evidence_completion",
                        "source_label": label,
                        "evidence_ref": (
                            evidence_refs[0]
                            if evidence_refs
                            else f"generated_content.field_results.{field_key}"
                        ),
                        "evidence_refs": evidence_refs,
                        "mapping_method": "evidence_supported_inference",
                        "dictionary_resolution_required": bool(
                            schema_field.get("dictionary_id")
                        ),
                        "reason": generated_result.get("reason")
                        or "Derived from collected Ozon and supplier evidence.",
                    }
                )
                continue
            status = (
                "not_applicable"
                if not required and normalized_label in _OPTIONAL_ASSET_FIELDS
                else "missing_fact"
            )
            missing_field = {
                **base,
                "status": status,
                "value": None,
                "source": None,
                "evidence_ref": None,
                "mapping_method": None,
                "reason": (
                    "Optional asset was not collected for this product."
                    if status == "not_applicable"
                    else "No evidence-backed value matches this template field."
                ),
            }
            if (
                status == "missing_fact"
                and generated_result
                and generated_result.get("structured") is True
                and generated_result["decision"] == "unresolved"
                and (
                    generated_result.get("resolution_class")
                    != "evidence_conflict"
                    or is_locked_supplier_internal_conflict(generated_result)
                )
                and (
                    generated_result.get("resolution_class")
                    != "source_fact_missing"
                    or generated_result.get("candidate_policy_version", 0)
                    >= _FIELD_CANDIDATE_POLICY_VERSION
                    or not _current_policy_candidate_available(
                        label,
                        ozon_candidate=ozon_candidate,
                        supplier_product=supplier_product,
                        supplier_truth_profile=supplier_truth_profile,
                    )
                )
            ):
                missing_field.update(
                    {
                        "intelligence_decision": "unresolved",
                        "evidence_refs": generated_result.get("evidence_refs", []),
                        "reason": generated_result.get("reason")
                        or missing_field["reason"],
                    }
                )
            mapped_fields.append(missing_field)
            continue

        mapped_fields.append(
            {
                **base,
                "status": "mapped",
                "value": selected["value"],
                "source": selected["source"],
                "source_label": selected["source_label"],
                "evidence_ref": selected["evidence_ref"],
                "mapping_method": selected.get("mapping_method")
                or (
                    "exact_label"
                    if selected["normalized_label"] == normalized_label
                    else "verified_alias"
                ),
                "dictionary_resolution_required": bool(schema_field.get("dictionary_id")),
                "reason": (
                    "Mapped from user-confirmed package evidence."
                    if selected["source"] == "user_confirmed_pricing_evidence"
                    else (
                        "Mapped from the user-confirmed supplier truth source."
                        if selected["source"] in _SUPPLIER_TRUTH_SOURCES
                        else "Mapped from frozen Ozon reference evidence."
                    )
                ),
            }
        )

    required_fields = [field for field in mapped_fields if field["required"]]
    required_mapped = [field for field in required_fields if field["status"] == "mapped"]
    missing_required_fact_count = sum(
        1
        for field in mapped_fields
        if field["status"] == "missing_fact" and field["required"]
    )
    missing_optional_fact_count = sum(
        1
        for field in mapped_fields
        if field["status"] == "missing_fact" and not field["required"]
    )
    missing_required = [
        {
            key: field.get(key)
            for key in (
                "field_key",
                "label",
                "status",
                "attribute_type",
                "dictionary_id",
                "allowed_values",
                "required_reason",
                "intelligence_decision",
                "reason",
            )
        }
        for field in required_fields
        if field["status"] != "mapped"
    ]
    manual_required = [
        field
        for field in missing_required
        if field.get("intelligence_decision") == "unresolved"
        or field.get("required_reason") == "ozon_compliance_decision"
    ]
    skill_pending_required = [
        field
        for field in missing_required
        if field not in manual_required
    ]
    return {
        "fields": mapped_fields,
        "mapped_attribute_count": sum(1 for field in mapped_fields if field["status"] == "mapped"),
        "rewrite_required_count": sum(
            1 for field in mapped_fields if field["status"] == "rewrite_required"
        ),
        "missing_fact_count": sum(
            1 for field in mapped_fields if field["status"] == "missing_fact"
        ),
        "missing_required_fact_count": missing_required_fact_count,
        "missing_optional_fact_count": missing_optional_fact_count,
        "not_applicable_count": sum(
            1 for field in mapped_fields if field["status"] == "not_applicable"
        ),
        "excluded_attribute_count": sum(
            1 for field in mapped_fields if field["status"] == "excluded"
        ),
        "required_attribute_count": len(required_fields),
        "required_mapped_count": len(required_mapped),
        "missing_required_fields": missing_required,
        "skill_pending_required_fields": skill_pending_required,
        "manual_required_fields": manual_required,
        "required_attributes_ready": bool(required_fields) and not missing_required,
        "attribute_score_progress": attribute_content_score_progress(mapped_fields),
    }


def _collect_evidence(
    ozon_candidate: dict[str, Any],
    *,
    supplier_product: dict[str, Any] | None,
    supplier_selection: dict[str, Any] | None,
    supplier_truth_profile: dict[str, Any] | None,
    pricing_evidence: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def add(label: Any, value: Any, source: str, evidence_ref: str, priority: int) -> None:
        if not _has_value(value):
            return
        normalized = normalize_attribute_label(label)
        if not normalized:
            return
        items.append(
            {
                "source_label": str(label),
                "normalized_label": normalized,
                "canonical_label": canonical_attribute_label(label),
                "value": _plain_value(value),
                "source": source,
                "evidence_ref": evidence_ref,
                "priority": priority,
            }
        )

    # Ozon is a market-language reference only. It must never supply identity,
    # category, dimensions, material, quantity, or another objective value for
    # the locked 1688 product.
    for key, label in (
        ("package_weight_g", "Вес с упаковкой, г"),
        ("package_length_cm", "Длина упаковки, см"),
        ("package_width_cm", "Ширина упаковки, см"),
        ("package_height_cm", "Высота упаковки, см"),
    ):
        add(
            label,
            (pricing_evidence or {}).get(key),
            "user_confirmed_pricing_evidence",
            f"pricing_evidence.inputs.{key}",
            1,
        )
    if supplier_selection:
        supplier_sku = supplier_selection.get("supplier_sku") or {}
        for label, value in _mapping_items(supplier_sku.get("selected_options")):
            add(
                label,
                value,
                "confirmed_supplier_sku",
                f"supplier_selection.supplier_sku.selected_options.{label}",
                10,
            )
        locked_weight = _locked_sku_explicit_weight(supplier_sku)
        if locked_weight is not None:
            add(
                "Вес товара, г",
                locked_weight[0],
                "confirmed_supplier_sku",
                locked_weight[1],
                4,
            )
        add(
            "Количество товара в УЕИ",
            supplier_sku.get("set_quantity"),
            "confirmed_supplier_sku",
            "supplier_selection.supplier_sku.set_quantity",
            5,
        )
        seller_code = _stable_seller_code(
            supplier_offer_id=(
                supplier_selection.get("supplier_offer_id")
                or (supplier_product or {}).get("offer_id")
            ),
            supplier_sku_id=supplier_sku.get("supplier_sku_id"),
            combination_key=supplier_sku.get("combination_key"),
        )
        add(
            "Код продавца",
            seller_code,
            "workflow_generated",
            "workflow.defaults.seller_code",
            5,
        )
    if supplier_product:
        for label, value in _mapping_items(supplier_product.get("attributes")):
            if _is_supplier_specification_artifact(label, value):
                continue
            add(label, value, "supplier_attributes", f"supplier.attributes.{label}", 20)
    if supplier_truth_profile:
        objective_fields = supplier_truth_profile.get("objective_fields") or {}
        truth_attributes = objective_fields.get("attributes") or {}
        for label, value in _mapping_items(truth_attributes):
            add(
                label,
                value,
                "locked_supplier_truth",
                f"supplier_truth.objective_fields.attributes.{label}",
                2,
            )
    return items


_EXPLICIT_WEIGHT_RE = re.compile(
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(kg|кг|公斤|千克|g|гр|г|克)(?![a-zа-я])",
    flags=re.IGNORECASE,
)


def _locked_sku_explicit_weight(
    supplier_sku: dict[str, Any],
) -> tuple[str, str] | None:
    candidates: list[tuple[Any, str]] = [
        (
            supplier_sku.get("raw_label"),
            "supplier_selection.supplier_sku.raw_label",
        ),
        (
            supplier_sku.get("combination_key"),
            "supplier_selection.supplier_sku.combination_key",
        ),
    ]
    candidates.extend(
        (
            value,
            f"supplier_selection.supplier_sku.selected_options.{label}",
        )
        for label, value in _mapping_items(supplier_sku.get("selected_options"))
    )
    matches: dict[str, str] = {}
    for raw_value, evidence_ref in candidates:
        for number, unit in _EXPLICIT_WEIGHT_RE.findall(str(raw_value or "")):
            try:
                grams = Decimal(number.replace(",", "."))
            except InvalidOperation:
                continue
            if unit.casefold() in {"kg", "кг", "公斤", "千克"}:
                grams *= 1000
            normalized = format(grams.normalize(), "f")
            matches.setdefault(normalized, evidence_ref)
    if len(matches) != 1:
        return None
    value, evidence_ref = next(iter(matches.items()))
    return value, evidence_ref


def _is_supplier_specification_artifact(label: Any, value: Any) -> bool:
    raw_label = str(label or "").strip().rstrip("：:")
    raw_value = str(value or "").strip()
    if raw_label in {"型号", "款号", "货号", "产品规格"} and re.fullmatch(
        r"(?:全长|长度|宽度|高度|直径|尺寸)\s*(?:[（(]\s*(?:mm|cm|毫米|厘米)\s*[)）])?",
        raw_value,
        flags=re.I,
    ):
        return True
    if re.fullmatch(
        r"[a-z0-9][a-z0-9._/-]{1,31}\s*[（(].{2,120}[)）]",
        raw_label,
        flags=re.I,
    ) and re.fullmatch(r"\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米)?", raw_value, flags=re.I):
        return True
    return raw_value.startswith(("全部", "全选", "不限")) or raw_value.endswith(
        ("展开参数", "收起参数")
    )


def _creative_reference_evidence(
    canonical_label: str,
    ozon_candidate: dict[str, Any],
    *,
    supplier_product: dict[str, Any] | None,
    supplier_selection: dict[str, Any] | None,
) -> list[str]:
    references = ["ozon.attributes", "ozon.content_score_evidence.attribute_table"]
    content_score_evidence = ozon_candidate.get("content_score_evidence") or {}
    if canonical_label == "title" and _has_value(ozon_candidate.get("title")):
        references.append("ozon.title")
    if canonical_label in {"description", "rich_content", "hashtags"} and _has_value(
        content_score_evidence.get("description_or_rich_content_blocks")
    ):
        references.append("ozon.content_score_evidence.description_or_rich_content_blocks")
    if supplier_product:
        references.extend(["supplier.title", "supplier.attributes"])
    if supplier_selection:
        references.append("supplier_selection.supplier_sku.selected_options")
    return references


def _mapping_items(value: Any) -> list[tuple[Any, Any]]:
    return list(value.items()) if isinstance(value, dict) else []


def _current_policy_candidate_available(
    label: Any,
    *,
    ozon_candidate: dict[str, Any],
    supplier_product: dict[str, Any] | None,
    supplier_truth_profile: dict[str, Any] | None,
) -> bool:
    canonical_label = canonical_attribute_label(label)
    ozon_attributes = ozon_candidate.get("attributes")
    if isinstance(ozon_attributes, dict) and any(
        canonical_attribute_label(source_label) == canonical_label
        and _has_value(value)
        for source_label, value in ozon_attributes.items()
    ):
        return True
    if canonical_label != "type":
        return False
    if _has_value((supplier_product or {}).get("title")):
        return True
    subject = (supplier_truth_profile or {}).get("subject")
    return isinstance(subject, dict) and _has_value(subject.get("value"))


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _candidate_policy_version(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _plain_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _stable_seller_code(
    *,
    supplier_offer_id: Any,
    supplier_sku_id: Any,
    combination_key: Any,
) -> str | None:
    offer_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(supplier_offer_id or "").strip())
    identity = "|".join(
        str(value or "").strip()
        for value in (supplier_sku_id, combination_key)
        if str(value or "").strip()
    )
    if not offer_id or not identity:
        return None
    suffix = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8].upper()
    return f"OZV2-{offer_id}-{suffix}"


def _generated_objective_value_is_acceptable(
    canonical_label: str,
    value: Any,
) -> bool:
    if canonical_label not in {
        "model",
        "type",
        "package_contents",
        "color",
        "material",
        "country",
        "gender",
        "upper_material",
        "lining_material",
        "sole_material",
        "fastening_type",
        "target_audience",
        "style",
        "decorative_elements",
        "season",
        "fit",
        "collection",
        "size_information",
        "sole_attachment",
    }:
        return True
    return not _customer_facing_value_needs_normalization(
        canonical_label,
        value,
    )


_CUSTOMER_FACING_FIELDS = {
    "model",
    "type",
    "package_contents",
    "color",
    "material",
    "gender",
    "upper_material",
    "lining_material",
    "sole_material",
    "fastening_type",
    "target_audience",
    "style",
    "decorative_elements",
    "season",
    "fit",
    "collection",
    "size_information",
    "sole_attachment",
}
_SUPPLIER_FULFILLMENT_OR_PROMOTION_RE = re.compile(
    r"(?:现货|当天发|当日发|速发|发货|包邮|一件代发|厂家直销|批发|跨境专供)"
)


def _customer_facing_value_needs_normalization(
    canonical_label: str,
    value: Any,
) -> bool:
    if canonical_label not in _CUSTOMER_FACING_FIELDS:
        return False
    text = str(value or "").strip()
    return bool(
        re.search(r"[\u3400-\u9fff]", text)
        or _SUPPLIER_FULFILLMENT_OR_PROMOTION_RE.search(text)
    )


def _generated_field_result(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and str(value.get("decision") or "").strip():
        evidence_refs = value.get("evidence_refs")
        return {
            "decision": str(value.get("decision") or "").strip(),
            "value": value.get("value"),
            "evidence_refs": [
                str(reference)
                for reference in evidence_refs
                if str(reference or "").strip()
            ]
            if isinstance(evidence_refs, list)
            else [],
            "reason": str(value.get("reason") or "").strip(),
            "resolution_class": str(
                value.get("resolution_class") or ""
            ).strip(),
            "candidate_policy_version": _candidate_policy_version(
                value.get("candidate_policy_version")
            ),
            "structured": True,
        }
    if _has_value(value):
        return {
            "decision": "filled",
            "value": value,
            "evidence_refs": [],
            "reason": "",
            "structured": False,
        }
    return None
