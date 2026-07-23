from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "brand": ("бренд", "марка", "brand", "品牌"),
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
    "seller_code": ("код продавца", "seller code", "offer id"),
    "package_contents": ("комплектация", "состав комплекта", "package contents", "包装清单"),
    "size": ("размер", "размеры мм", "size", "尺寸", "规格"),
    "length": ("длина мм", "длина см", "length", "长度", "全长"),
    "width": ("ширина мм", "ширина см", "width", "宽度"),
    "height": ("высота мм", "высота см", "height", "高度"),
    "weight": ("вес товара г", "вес г", "weight", "重量"),
    "volume": ("объем мл", "объём мл", "volume", "容量"),
    "gender": ("пол ребенка", "пол", "gender", "性别"),
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
_SUPPLIER_IDENTITY_FIELDS = {"brand", "model", "article", "color"}
_SUPPLIER_TRUTH_SOURCES = {"confirmed_supplier_sku", "supplier_attributes"}
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


def map_template_attributes(
    upload_schema: list[dict[str, Any]],
    ozon_candidate: dict[str, Any],
    *,
    supplier_product: dict[str, Any] | None = None,
    supplier_selection: dict[str, Any] | None = None,
    rewritten_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = _collect_evidence(
        ozon_candidate,
        supplier_product=supplier_product,
        supplier_selection=supplier_selection,
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
            "attribute_type": schema_field.get("attribute_type"),
            "dictionary_id": schema_field.get("dictionary_id"),
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
                    "Mapped from the user-confirmed supplier truth source."
                    if selected["source"] in _SUPPLIER_TRUTH_SOURCES
                    else "Mapped from frozen Ozon reference evidence."
                ),
            }
        )

    required_fields = [field for field in mapped_fields if field["required"]]
    required_mapped = [field for field in required_fields if field["status"] == "mapped"]
    missing_required = [
        {"field_key": field["field_key"], "label": field["label"], "status": field["status"]}
        for field in required_fields
        if field["status"] != "mapped"
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
        "not_applicable_count": sum(
            1 for field in mapped_fields if field["status"] == "not_applicable"
        ),
        "excluded_attribute_count": sum(
            1 for field in mapped_fields if field["status"] == "excluded"
        ),
        "required_attribute_count": len(required_fields),
        "required_mapped_count": len(required_mapped),
        "missing_required_fields": missing_required,
        "required_attributes_ready": bool(required_fields) and not missing_required,
    }


def _collect_evidence(
    ozon_candidate: dict[str, Any],
    *,
    supplier_product: dict[str, Any] | None,
    supplier_selection: dict[str, Any] | None,
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

    add("Бренд", ozon_candidate.get("brand"), "ozon_structured", "ozon.brand", 50)
    target_sku = ozon_candidate.get("target_sku") or {}
    for label, value in _mapping_items(target_sku.get("selected_options")):
        add(label, value, "ozon_selected_sku", f"ozon.target_sku.selected_options.{label}", 60)
    for label, value in _mapping_items(ozon_candidate.get("attributes")):
        add(label, value, "ozon_attributes", f"ozon.attributes.{label}", 70)
    content_score_evidence = ozon_candidate.get("content_score_evidence") or {}
    for label, value in _mapping_items(content_score_evidence.get("attribute_table")):
        add(
            label,
            value,
            "ozon_content_score_evidence",
            f"ozon.content_score_evidence.attribute_table.{label}",
            80,
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
    return items


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


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


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
    }:
        return True
    return not bool(re.search(r"[\u3400-\u9fff]", str(value or "")))


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
