from __future__ import annotations

import json
import re
from typing import Any


_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "brand": ("бренд", "марка", "brand", "品牌"),
    "type": ("тип", "тип товара", "вид товара", "type", "类型", "品类"),
    "model": ("модель", "название модели", "model", "型号"),
    "article": ("артикул", "код модели", "model code", "货号"),
    "color": ("цвет", "цвет товара", "название цвета", "color", "颜色"),
    "material": ("материал", "material", "材质"),
    "country": ("страна изготовитель", "страна производства", "country of origin", "产地"),
    "quantity": (
        "количество в упаковке шт",
        "количество товара в оед",
        "единиц в одном товаре",
        "quantity",
        "数量",
    ),
    "package_contents": ("комплектация", "состав комплекта", "package contents", "包装清单"),
    "size": ("размер", "размеры мм", "size", "尺寸", "规格"),
    "length": ("длина мм", "длина см", "length", "长度"),
    "width": ("ширина мм", "ширина см", "width", "宽度"),
    "height": ("высота мм", "высота см", "height", "高度"),
    "weight": ("вес товара г", "вес г", "weight", "重量"),
    "volume": ("объем мл", "объём мл", "volume", "容量"),
    "gender": ("пол ребенка", "пол", "gender", "性别"),
    "title": ("название", "title"),
    "description": ("аннотация", "описание", "description"),
    "rich_content": ("rich контент json", "rich content", "rich_content"),
}

_ALIASES = {
    alias: canonical
    for canonical, aliases in _ALIAS_GROUPS.items()
    for alias in aliases
}
_CREATIVE_FIELDS = {"title", "description", "rich_content"}


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
        required = schema_field.get("is_required") is True
        base = {
            "field_key": field_key,
            "label": label,
            "required": required,
            "attribute_type": schema_field.get("attribute_type"),
            "dictionary_id": schema_field.get("dictionary_id"),
        }
        if canonical_label in _CREATIVE_FIELDS:
            mapped_fields.append(
                {
                    **base,
                    "status": "excluded",
                    "value": None,
                    "source": None,
                    "evidence_ref": None,
                    "mapping_method": "creative_field_boundary",
                    "reason": "Creative content is handled outside objective attribute mapping.",
                }
            )
            continue

        candidates = [
            item
            for item in evidence
            if item["normalized_label"] == normalized_label
            or item["canonical_label"] == canonical_label
        ]
        candidates.sort(
            key=lambda item: (
                0 if item["normalized_label"] == normalized_label else 1,
                item["priority"],
            )
        )
        selected = candidates[0] if candidates else None
        if selected is None and canonical_label == "model":
            selected = next(
                (item for item in evidence if item["canonical_label"] == "article"),
                None,
            )
            if selected is not None:
                selected = {**selected, "mapping_method": "model_identifier_fallback"}

        if selected is None:
            mapped_fields.append(
                {
                    **base,
                    "status": "unresolved",
                    "value": None,
                    "source": None,
                    "evidence_ref": None,
                    "mapping_method": None,
                    "reason": "No evidence-backed value matches this template field.",
                }
            )
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
                "reason": "Mapped from frozen Ozon or user-confirmed supplier evidence.",
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

    add("Бренд", ozon_candidate.get("brand"), "ozon_structured", "ozon.brand", 5)
    target_sku = ozon_candidate.get("target_sku") or {}
    for label, value in _mapping_items(target_sku.get("selected_options")):
        add(label, value, "ozon_selected_sku", f"ozon.target_sku.selected_options.{label}", 10)
    for label, value in _mapping_items(ozon_candidate.get("attributes")):
        add(label, value, "ozon_attributes", f"ozon.attributes.{label}", 20)

    if supplier_selection:
        supplier_sku = supplier_selection.get("supplier_sku") or {}
        for label, value in _mapping_items(supplier_sku.get("selected_options")):
            add(
                label,
                value,
                "confirmed_supplier_sku",
                f"supplier_selection.supplier_sku.selected_options.{label}",
                30,
            )
    if supplier_product:
        for label, value in _mapping_items(supplier_product.get("attributes")):
            add(label, value, "supplier_attributes", f"supplier.attributes.{label}", 40)
    return items


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
