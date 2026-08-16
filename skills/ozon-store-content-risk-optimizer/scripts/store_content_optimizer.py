from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any
import unicodedata


_OWN_REPO_CANDIDATE = Path(__file__).resolve().parents[3]
_REPO_CANDIDATES = (Path.cwd(), *Path.cwd().parents, _OWN_REPO_CANDIDATE)
SOURCE_ROOT = next(
    (
        candidate / "src"
        for candidate in _REPO_CANDIDATES
        if (candidate / "src" / "ozon_v2").is_dir()
    ),
    _OWN_REPO_CANDIDATE / "src",
)
REPO_ROOT = SOURCE_ROOT.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.seller_api import SellerApiAdapter, SellerApiError
from ozon_v2.domain.pricing import PricingInput


RULE_VERSION = "1.2.1"
MEDIA_KEYS = frozenset(
    {"images", "primary_image", "images360", "video", "video_cover"}
)
TITLE_ATTRIBUTE_ID = 4180
DESCRIPTION_ATTRIBUTE_ID = 4191
RICH_CONTENT_ATTRIBUTE_ID = 11254
HASHTAG_ATTRIBUTE_ID = 23171
TYPE_ATTRIBUTE_ID = 8229
MIN_NON_MEDIA_SCORE = 80
CONTENT_ATTRIBUTE_IDS = {
    "name": TITLE_ATTRIBUTE_ID,
    "description": DESCRIPTION_ATTRIBUTE_ID,
    "rich_content": RICH_CONTENT_ATTRIBUTE_ID,
}
TERMINAL_UNCHANGED_STATUSES = frozenset(
    {
        "completed",
        "pending_risk",
        "score_below_target",
        "score_unverified",
        "evidence_insufficient",
        "paused",
    }
)
PROPOSAL_KEYS = frozenset(
    {
        "base_fingerprint",
        "product_id",
        "name",
        "description",
        "rich_content",
        "storefront_observations",
        "attribute_decisions",
        "risk_findings",
    }
)
REQUIRED_PROPOSAL_KEYS = PROPOSAL_KEYS
SEVERITY = {"low": 1, "medium": 2, "high": 3, "severe": 4}
SAFE_RESOLUTIONS = frozenset({"verified", "fixed", "removed", "not_applicable"})
SUPPLIER_PHRASES = (
    "1688",
    "поставщик",
    "locked-",
    "locked ",
    "опт",
    "дропшип",
    "от фабрик",
    "доставка от фабрик",
    "supplier",
    "wholesale",
    "dropship",
    "factory shipping",
    "供应商",
    "批发",
    "代发",
)
COMPLIANCE_PHRASES = (
    "сертифицирован",
    "сертификация",
    "гарантированная безопасность",
    "медицинский эффект",
    "лечебный",
    "certified",
    "guaranteed safe",
)
MOJIBAKE_MARKERS = (
    "РЎ",
    "РІ",
    "Рµ",
    "С‚",
    "Рё",
    "Р»",
    "СЊ",
    "РЅ",
)


class ValidationError(ValueError):
    pass


class ApplyError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_key(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_attribute_schema(schema: Any) -> list[dict[str, Any]]:
    stable: list[dict[str, Any]] = []
    for field in schema or []:
        if not isinstance(field, dict) or field.get("id") is None:
            continue
        current_values = list(field.get("current_values") or [])
        stable.append(
            {
                "id": int(field["id"]),
                "name": str(field.get("name") or ""),
                "type": str(field.get("type") or ""),
                "required": bool(field.get("required")),
                "dictionary": bool(field.get("dictionary")),
                "dictionary_id": field.get("dictionary_id"),
                "objective": bool(field.get("objective")),
                "current_values": sorted(current_values, key=canonical_json),
            }
        )
    return sorted(stable, key=lambda item: int(item["id"]))


def task_integrity_fingerprint(task: dict[str, Any]) -> str:
    return payload_hash(
        {
            key: task.get(key)
            for key in (
                "product_id",
                "offer_id",
                "source_fingerprint",
                "seller_api_item",
                "current",
                "attribute_schema",
                "deterministic_findings",
                "objective_evidence",
                "pricing_evidence",
                "evidence_insufficient",
                "prior_risk_level",
                "prior_risk_codes",
            )
        }
    )


def task_review_fingerprint(task: dict[str, Any]) -> str:
    """Stable non-media facts that justify reviewing an existing product again."""
    return payload_hash(
        {
            "product_id": task.get("product_id"),
            "offer_id": task.get("offer_id"),
            "visibility": task.get("visibility"),
            "current": task.get("current"),
            "attribute_schema": stable_attribute_schema(
                task.get("attribute_schema")
            ),
            "seller_status": task.get("seller_status"),
            "seller_errors": task.get("seller_errors"),
            "objective_evidence": task.get("objective_evidence"),
            "pricing_evidence": task.get("pricing_evidence"),
            "evidence_insufficient": task.get("evidence_insufficient"),
            "non_media_score": task.get("non_media_score"),
        }
    )


def _attribute_map(attributes: Any) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in attributes or []:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        try:
            result[int(item["id"])] = item
        except (TypeError, ValueError):
            continue
    return result


def _first_attribute_text(attributes: Any, attribute_id: int) -> str:
    item = _attribute_map(attributes).get(int(attribute_id)) or {}
    for value in item.get("values") or []:
        if isinstance(value, dict) and value.get("value") is not None:
            return str(value["value"]).strip()
    return ""


def _product_content(product: dict[str, Any]) -> dict[str, Any]:
    seller_item = product.get("seller_api_item") or {}
    attributes = product.get("attributes") or seller_item.get("attributes") or []
    name = _first_attribute_text(attributes, TITLE_ATTRIBUTE_ID) or str(
        product.get("name") or seller_item.get("name") or ""
    ).strip()
    description = _first_attribute_text(attributes, DESCRIPTION_ATTRIBUTE_ID) or str(
        product.get("description") or seller_item.get("description") or ""
    ).strip()
    rich_text = _first_attribute_text(attributes, RICH_CONTENT_ATTRIBUTE_ID)
    rich_content = product.get("rich_content") or seller_item.get("rich_content") or {}
    if rich_text:
        try:
            decoded = json.loads(rich_text)
        except json.JSONDecodeError:
            decoded = {}
        if isinstance(decoded, dict):
            rich_content = decoded
    return {
        "name": name,
        "description": description,
        "rich_content": rich_content if isinstance(rich_content, dict) else {},
        "attributes": attributes,
    }


def _seller_status(product: dict[str, Any]) -> dict[str, Any]:
    status = product.get("seller_status") or product.get("statuses")
    if not isinstance(status, dict):
        status = (product.get("seller_api_item") or {}).get("statuses")
    return deepcopy(status) if isinstance(status, dict) else {}


def _normalized_seller_errors(product: dict[str, Any]) -> list[dict[str, Any]]:
    source = (
        product.get("seller_errors")
        or product.get("errors")
        or product.get("item_errors")
    )
    if not source:
        seller_item = product.get("seller_api_item") or {}
        source = seller_item.get("errors") or seller_item.get("item_errors") or []
    result: list[dict[str, Any]] = []
    for item in source or []:
        if not isinstance(item, dict):
            continue
        texts = item.get("texts") or {}
        result.append(
            {
                "code": str(item.get("code") or "UNKNOWN_SELLER_ERROR"),
                "level": str(item.get("level") or ""),
                "attribute_id": item.get("attribute_id"),
                "message": str(
                    texts.get("message")
                    or texts.get("description")
                    or item.get("message")
                    or ""
                ),
            }
        )
    return result


def _missing_required_attribute_ids(
    product: dict[str, Any], schema: list[dict[str, Any]] | None = None
) -> list[int]:
    current = _attribute_map(_product_content(product).get("attributes") or [])
    seller_item = product.get("seller_api_item") or {}
    type_id = product.get("type_id") or seller_item.get("type_id")
    missing: list[int] = []
    for field in (
        schema if schema is not None else product.get("attribute_schema") or []
    ):
        if not isinstance(field, dict) or not field.get("required"):
            continue
        try:
            attribute_id = int(field["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if attribute_id == TYPE_ATTRIBUTE_ID and type_id not in (None, 0, "", "0"):
            continue
        values = (current.get(attribute_id) or {}).get("values") or []
        populated = any(
            isinstance(value, dict)
            and (
                value.get("dictionary_value_id") not in (None, 0, "", "0")
                or str(value.get("value") or "").strip()
            )
            for value in values
        )
        if not populated:
            missing.append(attribute_id)
    return sorted(set(missing))


def _seller_error_severity(level: str) -> str:
    folded = str(level or "").casefold()
    if "warn" in folded:
        return "medium"
    if any(token in folded for token in ("error", "fatal", "critical")):
        return "severe"
    return "high"


def _normalize_hashtag_text(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw or not raw.startswith("#"):
        return None
    parts = raw.split("#")
    if parts[0].strip():
        return None
    normalized: list[str] = []
    for part in parts[1:]:
        body = re.sub(r"\s+", "_", part.strip())
        if not body:
            return None
        normalized.append(f"#{body}")
    return " ".join(normalized)


def _safe_hashtag_values(product: dict[str, Any]) -> list[dict[str, Any]] | None:
    has_warning = any(
        str(item.get("code") or "") == "BR_hashtag_validation"
        for item in _normalized_seller_errors(product)
    )
    if not has_warning:
        return None
    attributes = _attribute_map(product.get("attributes") or [])
    current = attributes.get(HASHTAG_ATTRIBUTE_ID) or {}
    values = current.get("values") or []
    if not values:
        return None
    normalized_values: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            return None
        normalized_text = _normalize_hashtag_text(str(value.get("value") or ""))
        if normalized_text is None:
            return None
        normalized_values.append(
            {
                "dictionary_value_id": int(value.get("dictionary_value_id") or 0),
                "value": normalized_text,
            }
        )
    if canonical_json(normalized_values) == canonical_json(values):
        return None
    return normalized_values


def _decimal_json_number(value: Decimal) -> int | float:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)
    return float(normalized)


def _confirmed_package_import_fields(
    pricing_evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(pricing_evidence.get("status") or "") != "confirmed" or not str(
        pricing_evidence.get("confirmed_by") or ""
    ).strip():
        raise ValidationError("Confirmed workbench package evidence is missing")
    inputs = pricing_evidence.get("inputs")
    if not isinstance(inputs, dict):
        raise ValidationError("Confirmed workbench package inputs are missing")
    try:
        parsed = PricingInput.from_values(
            purchase_price_cny=inputs.get("purchase_price_cny"),
            domestic_shipping_cny=inputs.get("domestic_shipping_cny"),
            package_weight_g=inputs.get("package_weight_g"),
            package_length_cm=inputs.get("package_length_cm"),
            package_width_cm=inputs.get("package_width_cm"),
            package_height_cm=inputs.get("package_height_cm"),
            target_margin_rate=inputs.get("target_margin_rate"),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc

    def millimeters(value: Decimal) -> int:
        return int(
            (value * Decimal("10")).to_integral_value(
                rounding=ROUND_CEILING
            )
        )

    return {
        "depth": millimeters(parsed.package_length_cm),
        "width": millimeters(parsed.package_width_cm),
        "height": millimeters(parsed.package_height_cm),
        "dimension_unit": "mm",
        "weight": _decimal_json_number(parsed.package_weight_g),
        "weight_unit": "g",
    }


def _deterministic_findings(
    product: dict[str, Any], schema: list[dict[str, Any]] | None = None
) -> list[dict[str, str]]:
    findings = [
        {
            "code": f"seller_error:{item['code']}",
            "level": _seller_error_severity(str(item.get("level") or "")),
            "resolution": "unresolved",
        }
        for item in _normalized_seller_errors(product)
    ]
    status = _seller_status(product)
    validation_status = str(status.get("validation_status") or "").casefold()
    if validation_status and validation_status not in {
        "success",
        "valid",
        "validated",
    }:
        findings.append(
            {
                "code": f"seller_validation_status:{validation_status}",
                "level": "high",
                "resolution": "unresolved",
            }
        )
    if status.get("is_created") is False:
        findings.append(
            {
                "code": "seller_not_created",
                "level": "high",
                "resolution": "unresolved",
            }
        )
    if status.get("status_failed"):
        findings.append(
            {
                "code": "seller_status_failed",
                "level": "high",
                "resolution": "unresolved",
            }
        )
    for attribute_id in _missing_required_attribute_ids(product, schema):
        findings.append(
            {
                "code": f"missing_required_attribute:{attribute_id}",
                "level": "high",
                "resolution": "unresolved",
            }
        )
    for field_name, value in _customer_text_values(product).items():
        folded = str(value or "").casefold()
        if _has_cjk(value):
            findings.append(
                {
                    "code": f"prohibited_cjk_text:{field_name}",
                    "level": "severe",
                    "resolution": "unresolved",
                }
            )
        if any(phrase in folded for phrase in SUPPLIER_PHRASES):
            findings.append(
                {
                    "code": f"prohibited_supplier_language:{field_name}",
                    "level": "severe",
                    "resolution": "unresolved",
                }
            )
        if sum(marker in str(value or "") for marker in MOJIBAKE_MARKERS) >= 3:
            findings.append(
                {
                    "code": f"mojibake_text:{field_name}",
                    "level": "severe",
                    "resolution": "unresolved",
                }
            )
        if _has_duplicated_full_text(value):
            findings.append(
                {
                    "code": f"duplicated_full_text:{field_name}",
                    "level": "severe",
                    "resolution": "unresolved",
                }
            )
        if _has_ambiguous_numeric_separator(value):
            findings.append(
                {
                    "code": f"ambiguous_numeric_separator:{field_name}",
                    "level": "severe",
                    "resolution": "unresolved",
                }
            )
    unique: dict[str, dict[str, str]] = {}
    for finding in findings:
        unique[finding["code"]] = finding
    return list(unique.values())


def source_fingerprint(product: dict[str, Any]) -> str:
    tracked = {
        key: product.get(key)
        for key in (
            "product_id",
            "sku",
            "offer_id",
            "visibility",
            "name",
            "description",
            "rich_content",
            "attributes",
            "price",
            "description_category_id",
            "type_id",
            "statuses",
            "errors",
            "item_errors",
            "attribute_schema",
            "seller_objective_evidence",
            "content_score_groups",
            "content_rating",
        )
    }
    tracked["attribute_schema"] = stable_attribute_schema(
        product.get("attribute_schema")
    )
    return payload_hash(tracked)


def non_media_score(groups: dict[str, dict[str, float]]) -> int | None:
    included = [
        value
        for key, value in groups.items()
        if key.casefold() != "media" and isinstance(value, dict)
    ]
    maximum = sum(float(item.get("maximum") or 0) for item in included)
    if maximum <= 0:
        return None
    earned = sum(float(item.get("earned") or 0) for item in included)
    return round(100 * earned / maximum)


def normalize_rating_groups(rating_product: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize Ozon's weighted content groups without changing their meaning."""
    normalized: dict[str, dict[str, Any]] = {}
    for raw_group in rating_product.get("groups") or []:
        if not isinstance(raw_group, dict):
            continue
        key = str(raw_group.get("key") or "").strip()
        if not key:
            continue
        rating = float(raw_group.get("rating") or 0)
        weight = float(raw_group.get("weight") or 0)
        normalized[key] = {
            "name": str(raw_group.get("name") or ""),
            "rating": rating,
            "weight": weight,
            "earned": rating * weight / 100,
            "maximum": weight,
            "conditions": deepcopy(raw_group.get("conditions") or []),
            "improve_attributes": deepcopy(
                raw_group.get("improve_attributes") or []
            ),
        }
    return normalized


def _normalized_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _has_ambiguous_numeric_separator(value: Any) -> bool:
    folded = _normalized_text(value)
    return bool(
        re.search(
            r"\b\d+(?:[.,]\d+)?\s+\d+(?:[.,]\d+)?\s*"
            r"(?:мм|см|м|мл|л|г|кг|вт|ватт|в|вольт|шт|штук|игрок)",
            folded,
        )
        or re.search(r"\bот\s+\d+(?:[.,]\d+)?\s+до\s+\+\d", folded)
    )


def _has_duplicated_full_text(value: Any) -> bool:
    text = str(value or "").strip()
    if len(text) < 20 or len(text) % 2:
        return False
    midpoint = len(text) // 2
    return text[:midpoint] == text[midpoint:]


def _customer_text_values(product: dict[str, Any]) -> dict[str, str]:
    content = _product_content(product)
    rich_fragments: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            rich_fragments.append(value)

    visit(content.get("rich_content") or {})
    return {
        "name": str(content.get("name") or ""),
        "description": str(content.get("description") or ""),
        "rich_content": " ".join(rich_fragments),
    }


def _numeric_atoms(value: Any) -> set[str]:
    return set(re.findall(r"\d+", unicodedata.normalize("NFKC", str(value or ""))))


def _evidence_value_texts(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        if "value" in value and not isinstance(value.get("value"), (dict, list)):
            result.append(str(value.get("value") or ""))
        for child in value.values():
            if isinstance(child, (dict, list)):
                result.extend(_evidence_value_texts(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_evidence_value_texts(child))
    return result


def _validate_supported_numeric_facts(
    task: dict[str, Any], proposal: dict[str, Any]
) -> None:
    current_text = " ".join(_customer_text_values(task).values())
    evidence_text = " ".join(
        _evidence_value_texts(task.get("objective_evidence") or {})
    )
    allowed = _numeric_atoms(f"{current_text} {evidence_text}")
    proposed_product = {
        "name": proposal.get("name"),
        "description": proposal.get("description"),
        "rich_content": proposal.get("rich_content"),
    }
    proposed_text = " ".join(_customer_text_values(proposed_product).values())
    attribute_text = " ".join(
        value
        for item in proposal.get("attribute_decisions") or []
        if isinstance(item, dict) and str(item.get("decision") or "") == "set"
        for value in _decision_value_strings(item)
    )
    proposed_numbers = _numeric_atoms(f"{proposed_text} {attribute_text}")
    unsupported = sorted(proposed_numbers - allowed)
    if unsupported:
        raise ValidationError(
            f"proposal contains unsupported numeric facts: {', '.join(unsupported)}"
        )


def _has_cjk(text: str) -> bool:
    return bool(
        re.search(
            "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]",
            text,
        )
    )


def _supports_compliance(task: dict[str, Any]) -> bool:
    evidence = canonical_json(task.get("objective_evidence") or {}).casefold()
    return any(
        token in evidence
        for token in ("certificate", "certification", "сертифик", "认证")
    )


def _validate_customer_text(
    field_name: str,
    value: Any,
    *,
    compliance_supported: bool,
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be non-empty text")
    text = unicodedata.normalize("NFKC", value).strip()
    folded = text.casefold()
    if _has_cjk(text):
        raise ValidationError(f"{field_name} contains Chinese or Japanese text")
    if any(phrase in folded for phrase in SUPPLIER_PHRASES):
        raise ValidationError(f"{field_name} contains supplier language")
    if sum(marker in text for marker in MOJIBAKE_MARKERS) >= 3:
        raise ValidationError(f"{field_name} contains mojibake")
    if not re.search("[А-Яа-яЁё]", text):
        raise ValidationError(f"{field_name} must contain natural Russian")
    tokens = re.findall("[A-Za-zА-Яа-яЁё0-9]+", folded)
    if re.search(r"\b([A-Za-zА-Яа-яЁё0-9]+)(?:\s+\1){3,}\b", folded):
        raise ValidationError(f"{field_name} repeats keywords")
    if len(tokens) >= 7:
        most_common = max(tokens.count(token) for token in set(tokens))
        if most_common >= 3 and most_common / len(tokens) > 0.30:
            raise ValidationError(f"{field_name} contains keyword stuffing")
    if re.search(r"https?://|www\.|@[A-Za-z0-9_.-]+", text, re.IGNORECASE):
        raise ValidationError(f"{field_name} contains external contact information")
    if re.search(r"(?:\+?\d[\s()-]*){10,}", text):
        raise ValidationError(f"{field_name} contains a phone number")
    if _has_ambiguous_numeric_separator(text):
        raise ValidationError(
            f"{field_name} contains an ambiguous numeric separator"
        )
    if any(
        phrase in folded
        for phrase in (
            "доставка за один день",
            "доставим сегодня",
            "самый лучший",
            "номер один",
            "100% гарантия",
        )
    ):
        raise ValidationError(f"{field_name} contains an unsupported claim")
    if not compliance_supported and any(
        phrase in folded for phrase in COMPLIANCE_PHRASES
    ):
        raise ValidationError(f"{field_name} contains unsupported compliance claim")


def _validate_rich_content(
    value: Any,
    *,
    compliance_supported: bool,
) -> None:
    if not isinstance(value, dict):
        raise ValidationError("Rich Content must be a JSON object")
    media_tokens = ("image", "video", "media", "url", "cover")
    customer_text_keys = {"text", "title", "subtitle", "description", "caption"}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                folded_key = str(key).casefold()
                if any(token in folded_key for token in media_tokens):
                    raise ValidationError("Rich Content media is not allowed")
                if folded_key == "widgetname" and any(
                    token in str(child).casefold() for token in media_tokens
                ):
                    raise ValidationError("Rich Content media widget is not allowed")
                if folded_key in customer_text_keys and isinstance(child, str):
                    _validate_customer_text(
                        f"rich_content.{key}",
                        child,
                        compliance_supported=compliance_supported,
                    )
                else:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)


def _schema_by_id(task: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in task.get("attribute_schema") or []:
        if not isinstance(item, dict):
            continue
        try:
            attribute_id = int(item.get("id") or item.get("attribute_id"))
        except (TypeError, ValueError):
            continue
        result[attribute_id] = item
    return result


def _decision_value_strings(decision: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in decision.get("values") or []:
        if isinstance(item, dict) and item.get("value") is not None:
            values.append(_normalized_text(item["value"]))
    return values


def _evidence_for_attribute(
    task: dict[str, Any], attribute_id: int
) -> dict[str, Any]:
    evidence = task.get("objective_evidence") or {}
    item = evidence.get(str(attribute_id)) or evidence.get(attribute_id) or {}
    return item if isinstance(item, dict) else {}


def _seller_text_supports_decision(
    task: dict[str, Any], decision: dict[str, Any]
) -> bool:
    current = task.get("current") or {}
    sources = {
        "seller.name": str(current.get("name") or ""),
        "seller.description": str(current.get("description") or ""),
        "seller.current_content": " ".join(
            (
                str(current.get("name") or ""),
                str(current.get("description") or ""),
                canonical_json(current.get("rich_content") or {}),
            )
        ),
    }
    refs = [
        str(value)
        for value in decision.get("evidence_refs") or []
        if str(value) in sources
    ]
    values = _decision_value_strings(decision)
    if not refs or not values:
        return False
    for value in values:
        normalized_value = re.sub(r"\s+", " ", _normalized_text(value))
        if not normalized_value:
            return False
        pattern = re.compile(
            rf"(?<![\w]){re.escape(normalized_value)}(?![\w])",
            flags=re.IGNORECASE,
        )
        if not any(
            pattern.search(re.sub(r"\s+", " ", _normalized_text(sources[ref])))
            for ref in refs
        ):
            return False
    return True


def _validate_attribute_decisions(
    task: dict[str, Any],
    proposal: dict[str, Any],
    *,
    compliance_supported: bool,
) -> None:
    schema = _schema_by_id(task)
    seen: set[int] = set()
    for decision in proposal.get("attribute_decisions") or []:
        if not isinstance(decision, dict):
            raise ValidationError("attribute decision must be an object")
        try:
            attribute_id = int(decision.get("id"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("attribute decision id is invalid") from exc
        if attribute_id in seen:
            raise ValidationError(f"attribute {attribute_id} is duplicated")
        seen.add(attribute_id)
        field = schema.get(attribute_id)
        if field is None:
            raise ValidationError(f"attribute {attribute_id} is outside current schema")
        action = str(decision.get("decision") or "")
        if action not in {"keep", "set"}:
            raise ValidationError(f"attribute {attribute_id} decision is invalid")
        if action == "keep":
            continue
        values = decision.get("values")
        if not isinstance(values, list) or not values:
            raise ValidationError(f"attribute {attribute_id} needs values")
        dictionary_values = field.get("dictionary_values") or []
        if dictionary_values:
            allowed_ids = {
                str(item.get("id") or item.get("dictionary_value_id"))
                for item in dictionary_values
                if isinstance(item, dict)
                and (item.get("id") is not None or item.get("dictionary_value_id") is not None)
            }
            proposed_ids = {
                str(item.get("dictionary_value_id") or item.get("id"))
                for item in values
                if isinstance(item, dict)
            }
            if not proposed_ids or not proposed_ids.issubset(allowed_ids):
                raise ValidationError(
                    f"attribute {attribute_id} dictionary value is not allowed"
                )
        elif field.get("dictionary"):
            proposed_ids = [
                int(item.get("dictionary_value_id") or item.get("id") or 0)
                for item in values
                if isinstance(item, dict)
            ]
            if not proposed_ids or any(value_id <= 0 for value_id in proposed_ids):
                raise ValidationError(
                    f"attribute {attribute_id} dictionary value was not resolved"
                )
        evidence = _evidence_for_attribute(task, attribute_id)
        objective = bool(field.get("objective")) or bool(evidence)
        if objective:
            if evidence:
                expected = _normalized_text(evidence.get("value"))
                proposed = _decision_value_strings(decision)
                evidence_refs = {
                    str(value) for value in evidence.get("refs") or []
                }
                proposed_refs = {
                    str(value) for value in decision.get("evidence_refs") or []
                }
                supported = bool(
                    proposed == [expected]
                    and expected
                    and proposed_refs.intersection(evidence_refs)
                )
            else:
                supported = _seller_text_supports_decision(task, decision)
            if not supported:
                raise ValidationError(
                    f"attribute {attribute_id} objective evidence mismatch"
                )
        for value in values:
            if not isinstance(value, dict) or value.get("value") is None:
                continue
            visible = str(value["value"])
            if _has_cjk(visible) or any(
                phrase in visible.casefold() for phrase in SUPPLIER_PHRASES
            ):
                raise ValidationError(
                    f"attribute {attribute_id} contains prohibited customer text"
                )


def _price_loss(task: dict[str, Any]) -> bool:
    evidence = task.get("pricing_evidence") or {}
    minimum = evidence.get("minimum_safe_price")
    current = (task.get("seller_api_item") or {}).get("price")
    if minimum is None or current is None:
        return False
    try:
        return Decimal(str(current)) < Decimal(str(minimum))
    except InvalidOperation:
        return False


def _risk_summary(
    task: dict[str, Any], proposal: dict[str, Any]
) -> tuple[str | None, list[str], bool]:
    findings_by_code: dict[str, dict[str, str]] = {}
    for item in proposal.get("risk_findings") or []:
        if not isinstance(item, dict):
            raise ValidationError("risk finding must be an object")
        level = str(item.get("level") or "").casefold()
        if level not in SEVERITY:
            raise ValidationError("risk finding level is invalid")
        code = str(item.get("code") or "unspecified")
        findings_by_code[code] = {
            "code": code,
            "level": level,
            "resolution": str(item.get("resolution") or "unresolved").casefold(),
        }

    prior_risk_codes = [
        str(code) for code in task.get("prior_risk_codes") or [] if code
    ]
    missing_reviews = [
        code for code in prior_risk_codes if code not in findings_by_code
    ]
    if missing_reviews:
        raise ValidationError(
            "prior risk must be reviewed explicitly: "
            + ", ".join(missing_reviews)
        )

    deterministic = task.get("deterministic_findings")
    if not isinstance(deterministic, list):
        deterministic = _deterministic_findings(
            task, list(task.get("attribute_schema") or [])
        )
    set_attribute_ids = {
        int(item["id"])
        for item in proposal.get("attribute_decisions") or []
        if isinstance(item, dict)
        and str(item.get("decision") or "") == "set"
        and item.get("id") is not None
    }
    for item in deterministic:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "unspecified")
        proposed = findings_by_code.get(code) or {}
        resolution = str(proposed.get("resolution") or "unresolved").casefold()
        if code.startswith("missing_required_attribute:"):
            try:
                missing_id = int(code.rsplit(":", 1)[1])
            except (TypeError, ValueError):
                missing_id = -1
            if missing_id in set_attribute_ids:
                resolution = "fixed"
        if code.startswith("ambiguous_numeric_separator:"):
            field_name = code.rsplit(":", 1)[1]
            proposed_value: Any = proposal.get(field_name)
            if field_name == "rich_content":
                proposed_value = canonical_json(proposal.get("rich_content") or {})
            if not _has_ambiguous_numeric_separator(proposed_value):
                resolution = "fixed"
        findings_by_code[code] = {
            "code": code,
            "level": str(item.get("level") or "high").casefold(),
            "resolution": resolution,
        }

    product_url = str(task.get("product_url") or "")
    objective = task.get("objective_evidence") or {}
    for observation in proposal.get("storefront_observations") or []:
        if not isinstance(observation, dict):
            raise ValidationError("storefront observation must be an object")
        if str(observation.get("source_url") or "") != product_url:
            raise ValidationError("storefront observation source URL is invalid")
        field_key = str(observation.get("field_key") or "")
        seller_fact = objective.get(field_key) or {}
        expected = _normalized_text(
            seller_fact.get("value") if isinstance(seller_fact, dict) else seller_fact
        )
        observed = _normalized_text(observation.get("value"))
        if expected and observed and expected != observed:
            code = f"storefront_divergence:{field_key}"
            findings_by_code[code] = {
                "code": code,
                "level": "high",
                "resolution": "unresolved",
            }

    if _price_loss(task):
        findings_by_code["price_loss"] = {
            "code": "price_loss",
            "level": "severe",
            "resolution": "unresolved",
        }
    findings = list(findings_by_code.values())
    if not findings:
        return None, [], False
    highest = max(findings, key=lambda item: SEVERITY[item["level"]])["level"]
    codes = [item["code"] for item in findings]
    unresolved_blocking = any(
        SEVERITY[item["level"]] >= SEVERITY["high"]
        and item["resolution"] not in SAFE_RESOLUTIONS
        for item in findings
    )
    return highest, codes, unresolved_blocking


def validate_proposal(
    task: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        raise ValidationError("proposal must be a JSON object")
    integrity = str(task.get("task_integrity_fingerprint") or "")
    if integrity and integrity != task_integrity_fingerprint(task):
        raise ValidationError("task evidence changed after it was queued")
    unknown = set(proposal) - PROPOSAL_KEYS
    missing = REQUIRED_PROPOSAL_KEYS - set(proposal)
    if unknown:
        raise ValidationError(f"proposal contains unknown keys: {sorted(unknown)}")
    if missing:
        raise ValidationError(f"proposal is missing keys: {sorted(missing)}")
    if str(proposal.get("product_id")) != str(task.get("product_id")):
        raise ValidationError("proposal product_id does not match task")
    if str(proposal.get("base_fingerprint")) != str(
        task.get("source_fingerprint")
    ):
        raise ValidationError("proposal base fingerprint is stale")
    compliance_supported = _supports_compliance(task)
    _validate_customer_text(
        "name",
        proposal.get("name"),
        compliance_supported=compliance_supported,
    )
    _validate_customer_text(
        "description",
        proposal.get("description"),
        compliance_supported=compliance_supported,
    )
    _validate_rich_content(
        proposal.get("rich_content"),
        compliance_supported=compliance_supported,
    )
    _validate_attribute_decisions(
        task,
        proposal,
        compliance_supported=compliance_supported,
    )
    _validate_supported_numeric_facts(task, proposal)
    risk_level, risk_codes, unresolved_blocking = _risk_summary(task, proposal)
    evidence_insufficient = bool(task.get("evidence_insufficient"))
    return {
        "risk_level": risk_level,
        "risk_codes": risk_codes,
        "unresolved_blocking_risk": unresolved_blocking,
        "evidence_insufficient": evidence_insufficient,
        "allow_inventory_restore": not evidence_insufficient
        and not unresolved_blocking,
    }


def merge_proposal(
    task: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any]:
    validate_proposal(task, proposal)
    original = deepcopy(task.get("seller_api_item") or {})
    merged = deepcopy(original)
    merged["name"] = str(proposal["name"]).strip()
    merged["description"] = str(proposal["description"]).strip()
    merged["rich_content"] = deepcopy(proposal["rich_content"])
    attributes = {
        int(item["id"]): deepcopy(item)
        for item in merged.get("attributes") or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    for decision in proposal.get("attribute_decisions") or []:
        if str(decision.get("decision")) != "set":
            continue
        attribute_id = int(decision["id"])
        prior = attributes.get(attribute_id, {"id": attribute_id, "complex_id": 0})
        prior["values"] = deepcopy(decision["values"])
        attributes[attribute_id] = prior
    merged["attributes"] = list(attributes.values())
    for key in MEDIA_KEYS:
        if key in original:
            merged[key] = deepcopy(original[key])
        else:
            merged.pop(key, None)
    return merged


def resolve_proposal_dictionary_values(
    gateway: Any,
    task: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    resolved_proposal = deepcopy(proposal)
    schema = _schema_by_id(task)
    for decision in resolved_proposal.get("attribute_decisions") or []:
        if not isinstance(decision, dict) or decision.get("id") is None:
            continue
        attribute_id = int(decision["id"])
        field = schema.get(attribute_id) or {}
        if str(decision.get("decision") or "") != "set" or not field.get(
            "dictionary"
        ):
            continue
        resolved_values = []
        for value in decision.get("values") or []:
            visible = str((value or {}).get("value") or "").strip()
            if not visible:
                raise ValidationError(
                    f"attribute {attribute_id} dictionary value is empty"
                )
            resolved_values.append(
                gateway.resolve_dictionary_value(task, attribute_id, visible)
            )
        decision["values"] = resolved_values
    return resolved_proposal


def _attribute_update(
    attribute_id: int,
    values: list[dict[str, Any]],
    *,
    complex_id: int = 0,
) -> dict[str, Any]:
    normalized_values = []
    for value in values:
        normalized_values.append(
            {
                "dictionary_value_id": int(value.get("dictionary_value_id") or 0),
                "value": str(value.get("value") or ""),
            }
        )
    return {
        "id": int(attribute_id),
        "complex_id": int(complex_id),
        "values": normalized_values,
    }


def _updated_attributes(
    task: dict[str, Any], proposal: dict[str, Any]
) -> list[dict[str, Any]]:
    attributes = _attribute_map(
        (task.get("current") or {}).get("attributes")
        or (task.get("seller_api_item") or {}).get("attributes")
        or []
    )
    changed = [
        _attribute_update(
            TITLE_ATTRIBUTE_ID,
            [{"value": str(proposal["name"]).strip()}],
        ),
        _attribute_update(
            DESCRIPTION_ATTRIBUTE_ID,
            [{"value": str(proposal["description"]).strip()}],
        ),
        _attribute_update(
            RICH_CONTENT_ATTRIBUTE_ID,
            [{"value": canonical_json(proposal["rich_content"])}],
        ),
    ]
    for decision in proposal.get("attribute_decisions") or []:
        if str(decision.get("decision") or "") != "set":
            continue
        changed.append(
            _attribute_update(
                int(decision["id"]),
                list(decision.get("values") or []),
                complex_id=int(decision.get("complex_id") or 0),
            )
        )
    for item in changed:
        attributes[int(item["id"])] = item
    return list(attributes.values())


def build_attribute_update(
    task: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any]:
    updated = _attribute_map(_updated_attributes(task, proposal))
    changed_ids = {
        TITLE_ATTRIBUTE_ID,
        DESCRIPTION_ATTRIBUTE_ID,
        RICH_CONTENT_ATTRIBUTE_ID,
    }
    changed_ids.update(
        int(decision["id"])
        for decision in proposal.get("attribute_decisions") or []
        if str(decision.get("decision") or "") == "set"
    )
    return {
        "offer_id": str(task.get("offer_id") or ""),
        "attributes": [updated[attribute_id] for attribute_id in changed_ids],
    }


def build_original_attribute_update(
    task: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any]:
    current = task.get("current") or {}
    original_attributes = _attribute_map(current.get("attributes") or [])
    values = {
        TITLE_ATTRIBUTE_ID: _attribute_update(
            TITLE_ATTRIBUTE_ID, [{"value": str(current.get("name") or "")}]
        ),
        DESCRIPTION_ATTRIBUTE_ID: _attribute_update(
            DESCRIPTION_ATTRIBUTE_ID,
            [{"value": str(current.get("description") or "")}],
        ),
        RICH_CONTENT_ATTRIBUTE_ID: _attribute_update(
            RICH_CONTENT_ATTRIBUTE_ID,
            [{"value": canonical_json(current.get("rich_content") or {})}],
        ),
    }
    for decision in proposal.get("attribute_decisions") or []:
        if str(decision.get("decision") or "") != "set":
            continue
        attribute_id = int(decision["id"])
        prior = original_attributes.get(attribute_id) or {
            "id": attribute_id,
            "complex_id": int(decision.get("complex_id") or 0),
            "values": [],
        }
        values[attribute_id] = deepcopy(prior)
    return {
        "offer_id": str(task.get("offer_id") or ""),
        "attributes": list(values.values()),
    }


def _first_media_value(value: Any) -> str:
    if isinstance(value, list):
        return str(next((item for item in value if item), ""))
    return str(value or "")


def _media_snapshot(product: dict[str, Any]) -> dict[str, Any]:
    images = deepcopy(product.get("images") or [])
    primary_image = _first_media_value(product.get("primary_image"))
    if not primary_image:
        primary_image = _first_media_value(images)
    return {
        "images": images,
        "primary_image": primary_image,
        "images360": deepcopy(product.get("images360") or []),
        "video": deepcopy(product.get("video") or []),
        "video_cover": deepcopy(product.get("video_cover") or []),
    }


def _sanitized_import_item(
    source: dict[str, Any],
    *,
    name: str,
    attributes: list[dict[str, Any]],
) -> dict[str, Any]:
    item = {
        "attributes": deepcopy(attributes),
        "barcode": str(source.get("barcode") or ""),
        "complex_attributes": deepcopy(source.get("complex_attributes") or []),
        "currency_code": str(source.get("currency_code") or ""),
        "depth": source.get("depth"),
        "description_category_id": source.get("description_category_id"),
        "dimension_unit": str(source.get("dimension_unit") or ""),
        "height": source.get("height"),
        "images": deepcopy(source.get("images") or []),
        "name": str(name).strip(),
        "offer_id": str(source.get("offer_id") or ""),
        "old_price": str(source.get("old_price") or ""),
        "pdf_list": deepcopy(source.get("pdf_list") or []),
        "premium_price": str(source.get("premium_price") or ""),
        "price": str(source.get("price") or ""),
        "primary_image": _first_media_value(source.get("primary_image")),
        "type_id": source.get("type_id"),
        "vat": str(source.get("vat") or "0"),
        "weight": source.get("weight"),
        "weight_unit": str(source.get("weight_unit") or ""),
        "width": source.get("width"),
    }
    required = (
        "currency_code",
        "depth",
        "description_category_id",
        "dimension_unit",
        "height",
        "images",
        "name",
        "offer_id",
        "price",
        "primary_image",
        "type_id",
        "weight",
        "weight_unit",
        "width",
    )
    missing = [key for key in required if item.get(key) in (None, "", [])]
    if missing:
        raise ApplyError(
            f"Seller import preflight is missing required fields: {', '.join(missing)}"
        )
    return item


def build_content_import(
    task: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any]:
    source = task.get("seller_api_item") or {}
    return _sanitized_import_item(
        source,
        name=str(proposal["name"]),
        attributes=_updated_attributes(task, proposal),
    )


def build_original_content_import(task: dict[str, Any]) -> dict[str, Any]:
    source = task.get("seller_api_item") or {}
    current = task.get("current") or {}
    return _sanitized_import_item(
        source,
        name=str(current.get("name") or source.get("name") or ""),
        attributes=deepcopy(current.get("attributes") or source.get("attributes") or []),
    )


def proposal_changes_product(
    task: dict[str, Any], proposal: dict[str, Any]
) -> bool:
    current = task.get("current") or {}
    for key in ("name", "description", "rich_content"):
        if canonical_json(proposal.get(key)) != canonical_json(current.get(key)):
            return True
    current_attributes = _attribute_values_by_id(
        {"attributes": current.get("attributes") or []}
    )
    for decision in proposal.get("attribute_decisions") or []:
        if str(decision.get("decision") or "") != "set":
            continue
        attribute_id = int(decision["id"])
        normalized = _attribute_update(
            attribute_id,
            list(decision.get("values") or []),
            complex_id=int(decision.get("complex_id") or 0),
        )["values"]
        if canonical_json(current_attributes.get(attribute_id) or []) != canonical_json(
            normalized
        ):
            return True
    return False


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS product_state (
                product_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                rule_hashes_json TEXT NOT NULL,
                task_json TEXT NOT NULL,
                status TEXT NOT NULL,
                risk_level TEXT,
                risk_codes_json TEXT NOT NULL DEFAULT '[]',
                proposal_fingerprint TEXT,
                original_snapshot_json TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(product_id, action, payload_hash)
            );
            """
        )
        self.connection.commit()

    def get_state(self, product_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM product_state WHERE product_id = ?",
            (str(product_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_task(
        self,
        task: dict[str, Any],
        rule_hashes: dict[str, str],
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO product_state (
                product_id, sku, offer_id, source_fingerprint,
                rule_hashes_json, task_json, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
            ON CONFLICT(product_id) DO UPDATE SET
                sku = excluded.sku,
                offer_id = excluded.offer_id,
                source_fingerprint = excluded.source_fingerprint,
                rule_hashes_json = excluded.rule_hashes_json,
                task_json = excluded.task_json,
                status = 'queued',
                proposal_fingerprint = NULL,
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                str(task["product_id"]),
                str(task.get("sku") or ""),
                str(task.get("offer_id") or ""),
                str(task["source_fingerprint"]),
                canonical_json(rule_hashes),
                canonical_json(task),
                now,
            ),
        )
        self.connection.commit()

    def refresh_task(
        self,
        task: dict[str, Any],
        rule_hashes: dict[str, str],
        *,
        status: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE product_state
            SET sku = ?, offer_id = ?, source_fingerprint = ?,
                rule_hashes_json = ?, task_json = ?,
                status = COALESCE(?, status), updated_at = ?
            WHERE product_id = ?
            """,
            (
                str(task.get("sku") or ""),
                str(task.get("offer_id") or ""),
                str(task["source_fingerprint"]),
                canonical_json(rule_hashes),
                canonical_json(task),
                status,
                utc_now(),
                str(task["product_id"]),
            ),
        )
        self.connection.commit()

    def next_task(self) -> dict[str, Any] | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT product_id, task_json
                FROM product_state
                WHERE status = 'queued'
                ORDER BY updated_at, product_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                "UPDATE product_state SET status = 'drafting', updated_at = ? WHERE product_id = ?",
                (utc_now(), str(row["product_id"])),
            )
            self.connection.commit()
            return json.loads(str(row["task_json"]))
        except Exception:
            self.connection.rollback()
            raise

    def mark_completed(
        self,
        product_id: str,
        fingerprint: str,
        proposal_fingerprint: str,
    ) -> None:
        self.mark_applied(
            product_id,
            "completed",
            fingerprint,
            proposal_fingerprint,
        )

    def mark_applied(
        self,
        product_id: str,
        status: str,
        fingerprint: str,
        proposal_fingerprint: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE product_state
            SET status = ?, source_fingerprint = ?,
                proposal_fingerprint = ?, risk_level = NULL,
                risk_codes_json = '[]', last_error = NULL, updated_at = ?
            WHERE product_id = ?
            """,
            (
                str(status),
                fingerprint,
                proposal_fingerprint,
                utc_now(),
                str(product_id),
            ),
        )
        self.connection.commit()

    def mark_status(
        self,
        product_id: str,
        status: str,
        *,
        risk_level: str | None = None,
        risk_codes: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        normalized_risk_codes = list(
            dict.fromkeys(str(code) for code in (risk_codes or []) if code)
        )
        self.connection.execute(
            """
            UPDATE product_state
            SET status = ?, risk_level = ?, risk_codes_json = ?,
                last_error = ?, updated_at = ?
            WHERE product_id = ?
            """,
            (
                status,
                risk_level,
                canonical_json(normalized_risk_codes),
                error,
                utc_now(),
                str(product_id),
            ),
        )
        self.connection.commit()

    def save_original(self, product_id: str, original: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE product_state
            SET original_snapshot_json = ?, status = 'applying', updated_at = ?
            WHERE product_id = ?
            """,
            (canonical_json(original), utc_now(), str(product_id)),
        )
        self.connection.commit()

    def log_action(
        self,
        product_id: str,
        action: str,
        request_payload: Any,
        result: str,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO action_log (
                product_id, action, payload_hash, result, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(product_id, action, payload_hash) DO UPDATE SET
                result = excluded.result,
                created_at = excluded.created_at
            """,
            (
                str(product_id),
                action,
                payload_hash(request_payload),
                result,
                utc_now(),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def action_succeeded(
        self,
        product_id: str,
        action: str,
        request_payload: Any,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT result FROM action_log
            WHERE product_id = ? AND action = ? AND payload_hash = ?
            """,
            (str(product_id), action, payload_hash(request_payload)),
        ).fetchone()
        return row is not None and str(row["result"]) == "success"

    def status_summary(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM product_state GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def action_summary(self) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT action, result, COUNT(*) AS count
            FROM action_log
            GROUP BY action, result
            ORDER BY action, result
            """
        ).fetchall()
        return {
            f"{row['action']}:{row['result']}": int(row["count"])
            for row in rows
        }

    def product_summary(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT product_id, offer_id, status, risk_level,
                   risk_codes_json, last_error
            FROM product_state
            ORDER BY product_id
            """
        ).fetchall()
        return [
            {
                "product_id": str(row["product_id"]),
                "offer_id": str(row["offer_id"]),
                "status": str(row["status"]),
                "risk_level": row["risk_level"],
                "risk_codes": json.loads(str(row["risk_codes_json"])),
                "last_error": row["last_error"],
            }
            for row in rows
        ]


def _is_archived(product: dict[str, Any]) -> bool:
    return bool(product.get("archived")) or str(
        product.get("visibility") or ""
    ).upper() == "ARCHIVED"


def _build_task(
    product: dict[str, Any],
    evidence_bundle: dict[str, Any],
) -> dict[str, Any]:
    fingerprint = source_fingerprint(product)
    if evidence_bundle.get("locked_supplier_sku") is True:
        selection = evidence_bundle.get("supplier_selection") or {}
        supplier_sku = (
            selection.get("supplier_sku")
            if isinstance(selection, dict)
            else {}
        ) or {}
        fingerprint = payload_hash(
            {
                "seller_product": fingerprint,
                "locked_evidence": {
                    "run_id": evidence_bundle.get("run_id"),
                    "seed_id": evidence_bundle.get("seed_id"),
                    "selection_sha256": selection.get("selection_sha256")
                    if isinstance(selection, dict)
                    else None,
                    "supplier_sku_id": supplier_sku.get("supplier_sku_id")
                    if isinstance(supplier_sku, dict)
                    else None,
                    "objective_evidence": evidence_bundle.get(
                        "objective_evidence"
                    )
                    or {},
                    "pricing_evidence": evidence_bundle.get("pricing_evidence")
                    or {},
                },
            }
        )
    images = [str(value) for value in product.get("images") or [] if value]
    product_id = str(product.get("product_id") or product.get("id") or "")
    current = _product_content(product)
    schema = list(product.get("attribute_schema") or [])
    seller_status = _seller_status(product)
    seller_errors = _normalized_seller_errors(product)
    required_missing = _missing_required_attribute_ids(product, schema)
    deterministic_findings = _deterministic_findings(product, schema)
    pricing_evidence = evidence_bundle.get("pricing_evidence") or {}
    if str(pricing_evidence.get("status") or "") == "confirmed":
        try:
            _confirmed_package_import_fields(pricing_evidence)
        except ValidationError as exc:
            deterministic_findings.append(
                {
                    "code": "invalid_user_confirmed_package_density",
                    "level": "severe",
                    "resolution": "unresolved",
                    "message": str(exc),
                }
            )
    objective_evidence = deepcopy(product.get("seller_objective_evidence") or {})
    objective_evidence.update(
        deepcopy(evidence_bundle.get("objective_evidence") or {})
    )
    return {
        "product_id": product_id,
        "sku": str(product.get("sku") or ""),
        "offer_id": str(product.get("offer_id") or ""),
        "source_fingerprint": fingerprint,
        "visibility": str(product.get("visibility") or ""),
        "seller_api_item": product.get("seller_api_item") or dict(product),
        "current": current,
        "attribute_schema": schema,
        "seller_status": seller_status,
        "seller_errors": seller_errors,
        "required_missing_attribute_ids": required_missing,
        "deterministic_findings": deterministic_findings,
        "deterministic_risk_codes": [
            item["code"] for item in deterministic_findings
        ],
        "objective_evidence": objective_evidence,
        "read_only_images": images,
        "product_url": str(
            product.get("product_url")
            or f"https://www.ozon.ru/product/{product_id}/"
        ),
        "storefront_facts": product.get("storefront_facts") or {},
        "pricing_evidence": pricing_evidence,
        "evidence_insufficient": bool(
            evidence_bundle.get("evidence_insufficient", True)
        ),
        "non_media_score": non_media_score(
            product.get("content_score_groups") or {}
        ),
    }


def scan_store(
    gateway: Any,
    ledger: Ledger,
    evidence: Any,
    rule_hashes: dict[str, str],
    *,
    force: bool = False,
    problems_only: bool = False,
    product_ids: set[str] | None = None,
) -> dict[str, int]:
    counts = {
        "queued": 0,
        "skipped_archived": 0,
        "skipped_unchanged": 0,
    }
    if problems_only:
        counts["skipped_problem_free"] = 0
    for product in gateway.fetch_catalog():
        product_id = str(product.get("product_id") or product.get("id") or "")
        if product_ids and product_id not in product_ids:
            continue
        if _is_archived(product):
            counts["skipped_archived"] += 1
            continue
        if not product_id:
            continue
        bundle = evidence.for_offer(str(product.get("offer_id") or ""))
        task = _build_task(product, bundle)
        current = ledger.get_state(product_id)
        if current:
            task["prior_risk_level"] = current.get("risk_level")
            task["prior_risk_codes"] = json.loads(
                str(current.get("risk_codes_json") or "[]")
            )
        task["task_integrity_fingerprint"] = task_integrity_fingerprint(task)
        unchanged = bool(
            current
            and str(current["source_fingerprint"]) == task["source_fingerprint"]
            and json.loads(str(current["rule_hashes_json"])) == rule_hashes
        )
        prior_risk_codes = (
            json.loads(str(current.get("risk_codes_json") or "[]"))
            if current
            else []
        )
        score = task.get("non_media_score")
        live_problem = bool(
            task.get("deterministic_findings")
            or prior_risk_codes
            or score is None
            or int(score) < MIN_NON_MEDIA_SCORE
        )
        prior_task = (
            json.loads(str(current.get("task_json") or "{}"))
            if current
            else {}
        )
        review_unchanged = bool(
            current
            and task_review_fingerprint(prior_task)
            == task_review_fingerprint(task)
        )
        if (
            problems_only
            and current
            and not live_problem
            and review_unchanged
        ):
            restored_status = str(current["status"])
            if restored_status not in TERMINAL_UNCHANGED_STATUSES:
                restored_status = (
                    "evidence_insufficient"
                    if task.get("evidence_insufficient")
                    else "completed"
                )
            ledger.refresh_task(
                task,
                rule_hashes,
                status=restored_status,
            )
            counts["skipped_problem_free"] += 1
            continue
        if (
            not force
            and not (problems_only and live_problem)
            and unchanged
            and str(current["status"]) in TERMINAL_UNCHANGED_STATUSES
        ):
            counts["skipped_unchanged"] += 1
            continue
        ledger.upsert_task(task, rule_hashes)
        counts["queued"] += 1
    return counts


def audit_catalog(
    products: list[dict[str, Any]],
    *,
    ledger_products: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ledger_by_product_id = {
        str(item.get("product_id") or ""): item
        for item in ledger_products or []
        if item.get("product_id")
    }
    status_counts: dict[str, int] = {}
    score_available = 0
    score_unavailable = 0
    problems: list[dict[str, Any]] = []
    catalog_rows: list[dict[str, Any]] = []
    for product in products:
        visibility = str(product.get("visibility") or "NOT_IN_SALE")
        status_counts[visibility] = status_counts.get(visibility, 0) + 1
        score = non_media_score(
            product.get("content_score_groups")
            or product.get("content_rating")
            or {}
        )
        if score is None:
            score_unavailable += 1
        else:
            score_available += 1
        findings = _deterministic_findings(
            product, list(product.get("attribute_schema") or [])
        )
        risk_codes = [item["code"] for item in findings]
        ledger_item = ledger_by_product_id.get(
            str(product.get("product_id") or product.get("id") or ""), {}
        )
        for code in ledger_item.get("risk_codes") or []:
            if code not in risk_codes:
                risk_codes.append(str(code))
        if score is None:
            risk_codes.append("official_non_media_score_unavailable")
        elif score < MIN_NON_MEDIA_SCORE:
            risk_codes.append(f"non_media_score_below_{MIN_NON_MEDIA_SCORE}")
        status = _seller_status(product)
        row = {
            "product_id": str(
                product.get("product_id") or product.get("id") or ""
            ),
            "offer_id": str(product.get("offer_id") or ""),
            "name": _product_content(product)["name"],
            "visibility": visibility,
            "seller_status_name": str(status.get("status_name") or ""),
            "non_media_score": score,
            "risk_codes": risk_codes,
            "optimizer_status": str(ledger_item.get("status") or ""),
        }
        catalog_rows.append(row)
        if risk_codes:
            problems.append(row)
    return {
        "catalog_count": len(products),
        "status_counts": status_counts,
        "score_evidence": {
            "available": score_available,
            "unavailable": score_unavailable,
        },
        "problem_product_count": len(problems),
        "problem_products": problems,
        "products": catalog_rows,
    }


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _exact_offer_record(payload: Any, offer_id: str) -> dict[str, Any] | None:
    target = offer_id.strip().casefold()
    if not target:
        return None
    for item in _walk_dicts(payload):
        candidate = str(item.get("offer_id") or "").strip().casefold()
        if candidate == target:
            return item
    return None


def _container_record(
    payload: Any,
    record_id: str,
) -> dict[str, Any] | None:
    target = str(record_id or "")
    if not target or not isinstance(payload, dict):
        return None
    for container_key in ("items", "selections"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            record = container.get(target)
            if isinstance(record, dict):
                return record
        elif isinstance(container, list):
            for record in container:
                if not isinstance(record, dict):
                    continue
                if str(record.get("seed_id") or record.get("id") or "") == target:
                    return record
    return None


def _exact_offer_seed_and_record(
    payload: Any,
    offer_id: str,
) -> tuple[str, dict[str, Any]] | None:
    target = str(offer_id or "").strip().casefold()
    if not target or not isinstance(payload, dict):
        return None
    for container_key in ("items", "selections"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            records = container.items()
        elif isinstance(container, list):
            records = ((str(index), item) for index, item in enumerate(container))
        else:
            continue
        for record_key, record in records:
            if not isinstance(record, dict):
                continue
            candidates = (
                record.get("offer_id"),
                (record.get("seller_api_item") or {}).get("offer_id")
                if isinstance(record.get("seller_api_item"), dict)
                else None,
            )
            if any(str(value or "").strip().casefold() == target for value in candidates):
                seed_id = str(record.get("seed_id") or record_key)
                return seed_id, record
    record = _exact_offer_record(payload, offer_id)
    if record is None:
        return None
    return str(record.get("seed_id") or ""), record


def _locked_supplier_selection(selection: dict[str, Any]) -> bool:
    if _has_locked_supplier_sku(selection):
        return True
    supplier_sku = selection.get("supplier_sku")
    return bool(
        str(selection.get("decision") or "").casefold() == "confirmed_match"
        and selection.get("confirmed_by")
        and selection.get("selection_sha256")
        and isinstance(supplier_sku, dict)
        and supplier_sku.get("complete") is True
        and supplier_sku.get("supplier_sku_id")
    )


def _normalized_workbench_objective_evidence(
    payload: dict[str, Any],
    *,
    run_id: str,
    seed_id: str,
) -> dict[str, dict[str, Any]]:
    record = _container_record(payload, seed_id)
    if record is None:
        record = payload
    values = record.get("values") if isinstance(record, dict) else None
    if not isinstance(values, dict):
        values = record if isinstance(record, dict) else {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_id, evidence in values.items():
        attribute_id = str(raw_id)
        if not attribute_id.isdigit() or not isinstance(evidence, dict):
            continue
        if "value" not in evidence:
            continue
        normalized[attribute_id] = {
            "value": deepcopy(evidence["value"]),
            "refs": [
                f"workbench:{run_id}:{seed_id}:attribute:{attribute_id}"
            ],
        }
    return normalized


def _has_locked_supplier_sku(payload: Any) -> bool:
    for item in _walk_dicts(payload):
        if item.get("locked_supplier_sku") is True or item.get("locked") is True:
            return True
        status = str(
            item.get("selection_status") or item.get("status") or ""
        ).casefold()
        if status in {"confirmed", "locked", "selected"} and any(
            key in item for key in ("supplier_sku", "sku_id", "sku")
        ):
            return True
    return False


class WorkbenchEvidence:
    def __init__(self, repo: FsRepo | None = None) -> None:
        self.repo = repo or FsRepo()

    def _safe_load(self, method_name: str, run_id: str) -> dict[str, Any]:
        try:
            payload = getattr(self.repo, method_name)(run_id)
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def for_offer(self, offer_id: str) -> dict[str, Any]:
        for run in self.repo.list_runs():
            if str(run.get("kind") or "") != "workbench_batch":
                continue
            run_id = str(run.get("run_id") or "")
            if not run_id:
                continue
            submissions = self._safe_load("load_upload_submissions", run_id)
            previews = self._safe_load("load_upload_previews", run_id)
            submission_match = _exact_offer_seed_and_record(
                submissions, offer_id
            )
            preview_match = _exact_offer_seed_and_record(previews, offer_id)
            matched_submission = submission_match[1] if submission_match else None
            matched_preview = preview_match[1] if preview_match else None
            if not matched_submission and not matched_preview:
                continue
            seed_id = str(
                (submission_match or preview_match or ("", {}))[0]
            )
            selections = self._safe_load("load_supplier_sku_selections", run_id)
            objective_payload = self._safe_load(
                "load_required_attribute_evidence", run_id
            )
            pricing_payload = self._safe_load("load_pricing_evidence", run_id)
            selection = (
                _container_record(selections, seed_id)
                or _exact_offer_record(selections, offer_id)
                or {}
            )
            locked = _locked_supplier_selection(selection) or (
                not seed_id and _has_locked_supplier_sku(selections)
            )
            objective = _normalized_workbench_objective_evidence(
                objective_payload,
                run_id=run_id,
                seed_id=seed_id,
            )
            supplier_sku = selection.get("supplier_sku") or {}
            if locked and isinstance(supplier_sku, dict):
                if supplier_sku.get("set_quantity") is not None:
                    objective["quantity"] = {
                        "value": deepcopy(supplier_sku["set_quantity"]),
                        "refs": [
                            f"workbench:{run_id}:{seed_id}:supplier_sku:set_quantity"
                        ],
                    }
                if supplier_sku.get("set_composition"):
                    objective["set_composition"] = {
                        "value": deepcopy(supplier_sku["set_composition"]),
                        "refs": [
                            f"workbench:{run_id}:{seed_id}:supplier_sku:set_composition"
                        ],
                    }
            pricing = _container_record(pricing_payload, seed_id) or {}
            return {
                "offer_id": offer_id,
                "run_id": run_id,
                "seed_id": seed_id,
                "locked_supplier_sku": locked,
                "evidence_insufficient": not locked,
                "objective_evidence": objective,
                "pricing_evidence": pricing,
                "upload_submission": matched_submission or {},
                "upload_preview": matched_preview or {},
                "supplier_selection": selection if locked else {},
            }
        return {
            "offer_id": offer_id,
            "locked_supplier_sku": False,
            "evidence_insufficient": True,
            "objective_evidence": {},
            "pricing_evidence": {},
        }


def completion_gate(products: list[dict[str, Any]]) -> dict[str, Any]:
    completed_count = sum(
        1 for item in products if str(item.get("status") or "") == "completed"
    )
    unresolved_count = len(products) - completed_count
    return {
        "run_complete": bool(products) and unresolved_count == 0,
        "completed_count": completed_count,
        "unresolved_count": unresolved_count,
    }


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _description_type_names(tree: Any) -> dict[int, str]:
    result: dict[int, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            type_id = value.get("type_id")
            type_name = str(value.get("type_name") or "").strip()
            if type_id is not None and type_name:
                try:
                    result[int(type_id)] = type_name
                except (TypeError, ValueError):
                    pass
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(tree)
    return result


def _normalize_visibility(ref: dict[str, Any], info: dict[str, Any]) -> str:
    if ref.get("archived") or info.get("archived"):
        return "ARCHIVED"
    raw = str(
        ref.get("visibility")
        or info.get("visibility")
        or (info.get("statuses") or {}).get("status_name")
        or ""
    ).strip()
    folded = raw.casefold()
    machine = re.sub(r"[\s-]+", "_", folded)
    if any(
        token in folded
        for token in ("не прода", "ошибка", "отклон", "заблокирован")
    ) or machine in {"not_in_sale", "validation_failed", "failed", "rejected"}:
        return "NOT_IN_SALE"
    if "готов" in folded or machine in {"ready_to_supply", "ready_for_sale"}:
        return "READY_TO_SUPPLY"
    if folded == "продается" or machine in {"in_sale", "on_sale"}:
        return "IN_SALE"
    if ref.get("has_fbs_stocks") or ref.get("has_fbo_stocks"):
        return "IN_SALE"
    return raw.upper().replace(" ", "_") or "NOT_IN_SALE"


class SellerGateway:
    def __init__(self, adapter: SellerApiAdapter | None = None) -> None:
        self.adapter = adapter or SellerApiAdapter()

    def _fetch_attributes(self, product_ids: list[int]) -> dict[str, dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(product_ids, 100):
            payload = self.adapter._post_json(
                "/v4/product/info/attributes",
                {"filter": {"product_id": chunk}, "limit": 100},
            )
            result = payload.get("result", payload)
            if isinstance(result, list):
                items = result
            elif isinstance(result, dict):
                items = result.get("items", [])
            else:
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                product_id = str(item.get("id") or item.get("product_id") or "")
                if product_id:
                    by_id[product_id] = item
        return by_id

    def _fetch_content_ratings(
        self, skus: list[int]
    ) -> dict[str, dict[str, Any]]:
        by_sku: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(skus, 100):
            payload = self._retry_call(
                lambda chunk=chunk: self.adapter._post_json(
                    "/v1/product/rating-by-sku", {"skus": chunk}
                )
            )
            for item in payload.get("products") or []:
                if not isinstance(item, dict) or item.get("sku") is None:
                    continue
                by_sku[str(item["sku"])] = item
        return by_sku

    def fetch_catalog(self) -> list[dict[str, Any]]:
        refs = self.adapter._fetch_product_refs()
        product_ids = [int(item["product_id"]) for item in refs]
        info_by_id = self.adapter._fetch_product_info(product_ids)
        attributes_by_id = self._fetch_attributes(product_ids)
        numeric_skus = sorted(
            {
                int(sku)
                for ref in refs
                for sku in (
                    ref.get("sku")
                    or attributes_by_id.get(str(ref["product_id"]), {}).get("sku")
                    or info_by_id.get(str(ref["product_id"]), {}).get("sku"),
                )
                if str(sku or "").isdigit()
            }
        )
        rating_by_sku = self._fetch_content_ratings(numeric_skus)
        try:
            type_names = _description_type_names(
                self.adapter._fetch_description_category_tree()
            )
        except SellerApiError:
            type_names = {}
        products: list[dict[str, Any]] = []
        schema_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for ref in refs:
            product_id = str(ref["product_id"])
            info = info_by_id.get(product_id, {})
            attributes = attributes_by_id.get(product_id, {})
            merged = {**info, **attributes}
            sku = str(ref.get("sku") or merged.get("sku") or "")
            rating_product = rating_by_sku.get(sku, {})
            images = merged.get("images") or []
            raw_stocks = merged.get("stocks") or []
            stocks = (
                raw_stocks.get("stocks", [])
                if isinstance(raw_stocks, dict)
                else raw_stocks
            )
            seller_item = dict(merged)
            seller_item.setdefault("offer_id", ref.get("offer_id"))
            description_category_id = merged.get("description_category_id")
            type_id = merged.get("type_id")
            schema: list[dict[str, Any]] = []
            if description_category_id is not None and type_id is not None:
                schema_key = (int(description_category_id), int(type_id))
                if schema_key not in schema_cache:
                    raw_schema = self.adapter._fetch_description_category_attributes(
                        *schema_key
                    )
                    current_attributes = _attribute_map(merged.get("attributes") or [])
                    normalized_schema = []
                    for field in raw_schema:
                        if not isinstance(field, dict) or field.get("id") is None:
                            continue
                        attribute_id = int(field["id"])
                        dictionary = bool(field.get("dictionary_id"))
                        current_values = (
                            current_attributes.get(attribute_id) or {}
                        ).get("values") or []
                        normalized_schema.append(
                            {
                                "id": attribute_id,
                                "name": str(field.get("name") or ""),
                                "type": str(field.get("type") or ""),
                                "required": bool(field.get("is_required")),
                                "dictionary": dictionary,
                                "dictionary_id": field.get("dictionary_id"),
                                "dictionary_values": [],
                                "current_values": list(current_values),
                                "objective": attribute_id
                                not in {
                                    TITLE_ATTRIBUTE_ID,
                                    DESCRIPTION_ATTRIBUTE_ID,
                                    RICH_CONTENT_ATTRIBUTE_ID,
                                    23171,
                                },
                            }
                        )
                    schema_cache[schema_key] = normalized_schema
                schema = schema_cache[schema_key]
            current = _product_content({**merged, "seller_api_item": seller_item})
            seller_objective_evidence: dict[str, dict[str, Any]] = {}
            if type_id is not None and int(type_id) in type_names:
                seller_objective_evidence[str(TYPE_ATTRIBUTE_ID)] = {
                    "value": type_names[int(type_id)],
                    "refs": [f"seller.type_id:{int(type_id)}"],
                }
            products.append(
                {
                    **merged,
                    "product_id": product_id,
                    "sku": sku,
                    "offer_id": str(
                        ref.get("offer_id") or merged.get("offer_id") or ""
                    ),
                    "visibility": _normalize_visibility(ref, merged),
                    "archived": bool(ref.get("archived")),
                    "name": current["name"],
                    "description": current["description"],
                    "rich_content": current["rich_content"],
                    "attributes": merged.get("attributes") or [],
                    "attribute_schema": schema,
                    "seller_objective_evidence": seller_objective_evidence,
                    "images": images,
                    "primary_image": merged.get("primary_image")
                    or (images[0] if images else None),
                    "price": merged.get("price"),
                    "stocks": stocks,
                    "description_category_id": merged.get(
                        "description_category_id"
                    ),
                    "type_id": merged.get("type_id"),
                    "content_score_groups": normalize_rating_groups(
                        rating_product
                    ),
                    "content_rating": rating_product.get("rating"),
                    "seller_api_item": seller_item,
                }
            )
        return products

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        target = str(product_id)
        try:
            numeric_id = int(target)
        except ValueError as exc:
            raise ApplyError(f"Seller product ID {target} is invalid") from exc
        info = self.adapter._fetch_product_info([numeric_id]).get(target, {})
        attributes = self._fetch_attributes([numeric_id]).get(target, {})
        if not info and not attributes:
            raise ApplyError(f"Seller product {target} was not found")
        merged = {**info, **attributes}
        sku = str(merged.get("sku") or "")
        rating_product = (
            self._fetch_content_ratings([int(sku)]).get(sku, {})
            if sku.isdigit()
            else {}
        )
        seller_item = dict(merged)
        current = _product_content({**merged, "seller_api_item": seller_item})
        images = merged.get("images") or []
        stocks = merged.get("stocks") or []
        return {
            **merged,
            "product_id": target,
            "sku": sku,
            "offer_id": str(merged.get("offer_id") or ""),
            "visibility": _normalize_visibility({}, merged),
            "archived": bool(merged.get("archived") or merged.get("is_archived")),
            "name": current["name"],
            "description": current["description"],
            "rich_content": current["rich_content"],
            "attributes": merged.get("attributes") or [],
            "images": images,
            "primary_image": merged.get("primary_image")
            or (images[0] if images else None),
            "price": merged.get("price"),
            "stocks": stocks.get("stocks", [])
            if isinstance(stocks, dict)
            else stocks,
            "description_category_id": merged.get("description_category_id"),
            "type_id": merged.get("type_id"),
            "content_score_groups": normalize_rating_groups(rating_product),
            "content_rating": rating_product.get("rating"),
            "seller_api_item": seller_item,
        }

    def resolve_dictionary_value(
        self,
        task: dict[str, Any],
        attribute_id: int,
        value: str,
    ) -> dict[str, Any]:
        source = task.get("seller_api_item") or {}
        description_category_id = source.get("description_category_id")
        type_id = source.get("type_id")
        if description_category_id is None or type_id is None:
            raise ValidationError(
                f"attribute {attribute_id} dictionary context is missing"
            )
        resolved = self.adapter.resolve_attribute_dictionary_value(
            description_category_id=int(description_category_id),
            type_id=int(type_id),
            attribute_id=int(attribute_id),
            value=str(value),
        )
        return {
            "dictionary_value_id": int(resolved["dictionary_value_id"]),
            "value": str(resolved["value"]),
        }

    def _retry_call(self, operation: Any) -> Any:
        delays = (1, 2, 4)
        for attempt in range(len(delays) + 1):
            try:
                return operation()
            except SellerApiError as exc:
                text = str(exc).casefold()
                transient = "http 429" in text or "request failed" in text
                if not transient or attempt >= len(delays):
                    raise
                time.sleep(delays[attempt])
        raise ApplyError("Seller API retry loop ended unexpectedly")

    def import_product(self, item: dict[str, Any]) -> int:
        result = self._retry_call(lambda: self.adapter.import_products([item]))
        return int(result["task_id"])

    def update_product_attributes(self, item: dict[str, Any]) -> int:
        payload = self._retry_call(
            lambda: self.adapter._post_json(
                "/v1/product/attributes/update", {"items": [item]}
            )
        )
        task_id = payload.get("task_id") or (payload.get("result") or {}).get(
            "task_id"
        )
        if task_id is None:
            raise ApplyError("Seller attribute update did not return a task_id")
        return int(task_id)

    def import_status(self, task_id: int) -> dict[str, Any]:
        return self._retry_call(
            lambda: self.adapter.get_product_import_info(task_id)
        )

    def list_rfbs_warehouses(self) -> list[dict[str, Any]]:
        payload = self._retry_call(
            lambda: self.adapter._post_json("/v2/warehouse/list", {})
        )
        return [
            item
            for item in payload.get("warehouses", [])
            if isinstance(item, dict)
            and item.get("status") == "created"
            and item.get("is_rfbs") is True
            and item.get("warehouse_type") == "rfbs"
        ]

    def set_stock(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return self._retry_call(
            lambda: self.adapter._post_json(
                "/v2/products/stocks", {"stocks": rows}
            )
        )

    def active_order_offer_ids(self) -> set[str]:
        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        date_to = (now + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        requests = (
            (
                "/v3/posting/fbs/list",
                {
                    "dir": "ASC",
                    "filter": {"since": date_from, "to": date_to},
                    "limit": 1000,
                    "offset": 0,
                    "with": {"analytics_data": False, "financial_data": False},
                },
            ),
            (
                "/v2/posting/fbo/list",
                {
                    "dir": "ASC",
                    "filter": {"since": date_from, "to": date_to},
                    "limit": 1000,
                    "offset": 0,
                    "translit": True,
                    "with": {"analytics_data": False, "financial_data": False},
                },
            ),
        )
        active: set[str] = set()
        terminal = {
            "cancelled",
            "canceled",
            "delivered",
            "disposed",
            "returned",
        }
        for path, request_payload in requests:
            response = self._retry_call(
                lambda path=path, request_payload=request_payload: self.adapter._post_json(
                    path, request_payload
                )
            )
            result = response.get("result", response)
            postings = result.get("postings", []) if isinstance(result, dict) else []
            for posting in postings:
                if not isinstance(posting, dict):
                    continue
                status = str(posting.get("status") or "").casefold()
                if status in terminal:
                    continue
                for item in posting.get("products") or []:
                    if isinstance(item, dict) and item.get("offer_id"):
                        active.add(str(item["offer_id"]))
        return active


def _product_import_status(payload: dict[str, Any]) -> str:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return "processing"
    statuses = {
        str(item.get("status") or item.get("state") or "").casefold()
        for item in items
        if isinstance(item, dict)
    }
    if any(value in {"failed", "error", "declined"} for value in statuses):
        return "failed"
    if statuses and all(
        value in {"imported", "success", "processed", "moderating"}
        for value in statuses
    ):
        return "accepted"
    return "processing"


def _poll_import(
    gateway: Any,
    task_id: int,
    *,
    sleep_fn: Any,
) -> dict[str, Any]:
    delays = (1, 2, 4, 8, 15, 15)
    latest: dict[str, Any] = {}
    for index, delay in enumerate(delays):
        latest = gateway.import_status(task_id)
        status = _product_import_status(latest)
        if status == "accepted":
            return latest
        if status == "failed":
            raise ApplyError(f"Seller import task {task_id} was rejected")
        if index < len(delays) - 1:
            sleep_fn(delay)
    raise ApplyError(f"Seller import task {task_id} did not reach a terminal status")


def _attribute_values_by_id(product: dict[str, Any]) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for item in product.get("attributes") or []:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        result[int(item["id"])] = item.get("values") or []
    return result


def _verify_readback(
    task: dict[str, Any],
    proposal: dict[str, Any],
    actual: dict[str, Any],
) -> None:
    actual_content = _product_content(actual)
    for key in ("name", "description", "rich_content"):
        if canonical_json(actual_content.get(key)) != canonical_json(proposal.get(key)):
            raise ApplyError(f"Seller read-back mismatch for {key}")
    actual_attributes = _attribute_values_by_id(actual)
    for decision in proposal.get("attribute_decisions") or []:
        if str(decision.get("decision")) != "set":
            continue
        attribute_id = int(decision["id"])
        if canonical_json(actual_attributes.get(attribute_id)) != canonical_json(
            decision.get("values") or []
        ):
            raise ApplyError(
                f"Seller read-back mismatch for attribute {attribute_id}"
            )
    original_media = _media_snapshot(task.get("seller_api_item") or {})
    actual_media = _media_snapshot(actual)
    for key in MEDIA_KEYS:
        if canonical_json(actual_media[key]) != canonical_json(original_media[key]):
            raise ApplyError(f"Seller read-back changed media field {key}")
    before_score = task.get("non_media_score")
    after_score = non_media_score(actual.get("content_score_groups") or {})
    if (
        before_score is not None
        and after_score is not None
        and int(after_score) < int(before_score)
    ):
        raise ApplyError("Seller non-media score decreased after write")


def _wait_for_verified_readback(
    gateway: Any,
    task: dict[str, Any],
    proposal: dict[str, Any],
    *,
    sleep_fn: Any,
) -> dict[str, Any]:
    product_id = str(task["product_id"])
    latest_error: Exception | None = None
    for delay in (0, 2, 4, 8, 15):
        if delay:
            sleep_fn(delay)
        actual = gateway.fetch_product(product_id)
        try:
            _verify_readback(task, proposal, actual)
        except ApplyError as exc:
            latest_error = exc
            continue
        return actual
    if latest_error is not None:
        raise latest_error
    raise ApplyError("Seller read-back verification did not run")


def _original_snapshot_matches(
    task: dict[str, Any],
    proposal: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    current = task.get("current") or {}
    actual_content = _product_content(actual)
    for key in ("name", "description", "rich_content"):
        if canonical_json(actual_content.get(key)) != canonical_json(current.get(key)):
            return False
    original_attributes = _attribute_values_by_id(
        {"attributes": current.get("attributes") or []}
    )
    actual_attributes = _attribute_values_by_id(actual)
    for decision in proposal.get("attribute_decisions") or []:
        if str(decision.get("decision")) != "set":
            continue
        attribute_id = int(decision["id"])
        if canonical_json(actual_attributes.get(attribute_id) or []) != canonical_json(
            original_attributes.get(attribute_id) or []
        ):
            return False
    if _media_snapshot(actual) != _media_snapshot(
        task.get("seller_api_item") or {}
    ):
        return False
    return True


def _snapshot_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    keys = {
        "name",
        "description",
        "rich_content",
        "attributes",
        *MEDIA_KEYS,
    }
    return all(
        canonical_json(actual.get(key)) == canonical_json(expected.get(key))
        for key in keys
    )


def _stock_count(product: dict[str, Any]) -> int:
    count = 0
    for row in product.get("stocks") or []:
        if not isinstance(row, dict):
            continue
        count += int(row.get("present") or row.get("stock") or 0)
    return count


def _completion_assessment(
    task: dict[str, Any], actual: dict[str, Any]
) -> dict[str, Any]:
    findings = _deterministic_findings(
        actual, list(task.get("attribute_schema") or [])
    )
    if findings:
        highest = max(findings, key=lambda item: SEVERITY[item["level"]])["level"]
        return {
            "status": "pending_risk",
            "risk_level": highest,
            "risk_codes": [item["code"] for item in findings],
            "non_media_score": non_media_score(
                actual.get("content_score_groups")
                or actual.get("content_rating")
                or {}
            ),
        }
    score = non_media_score(
        actual.get("content_score_groups")
        or actual.get("content_rating")
        or {}
    )
    if score is None:
        risk_codes = ["official_non_media_score_unavailable"]
        if task.get("evidence_insufficient"):
            risk_codes.append("objective_evidence_insufficient")
        return {
            "status": "score_unverified",
            "risk_level": None,
            "risk_codes": risk_codes,
            "non_media_score": None,
        }
    if task.get("evidence_insufficient"):
        return {
            "status": "evidence_insufficient",
            "risk_level": None,
            "risk_codes": ["objective_evidence_insufficient"],
            "non_media_score": score,
        }
    if score < MIN_NON_MEDIA_SCORE:
        return {
            "status": "score_below_target",
            "risk_level": "medium",
            "risk_codes": [f"non_media_score_below_{MIN_NON_MEDIA_SCORE}"],
            "non_media_score": score,
        }
    return {
        "status": "completed",
        "risk_level": None,
        "risk_codes": [],
        "non_media_score": score,
    }


def _guarded_stock_action(
    gateway: Any,
    ledger: Ledger,
    task: dict[str, Any],
    stock: int,
) -> tuple[bool, str]:
    offer_id = str(task.get("offer_id") or "")
    if offer_id in gateway.active_order_offer_ids():
        return False, "active_order"
    warehouses = gateway.list_rfbs_warehouses()
    if len(warehouses) != 1:
        return False, "rfbs_warehouse_ambiguous"
    try:
        product_id = int(task["product_id"])
        warehouse_id = int(warehouses[0]["warehouse_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplyError("Product or RFBS warehouse ID is invalid") from exc
    row = {
        "offer_id": offer_id,
        "product_id": product_id,
        "stock": int(stock),
        "warehouse_id": warehouse_id,
    }
    action = f"stock_{stock}"
    if ledger.action_succeeded(str(product_id), action, row):
        return True, "duplicate_skipped"
    gateway.set_stock([row])
    ledger.log_action(str(product_id), action, row, "success")
    return True, "updated"


def _rollback_product(
    gateway: Any,
    ledger: Ledger,
    task: dict[str, Any],
    proposal: dict[str, Any],
    original_request: dict[str, Any],
    *,
    use_import: bool,
    sleep_fn: Any,
) -> bool:
    product_id = str(task["product_id"])
    try:
        rollback_task_id = (
            gateway.import_product(original_request)
            if use_import
            else gateway.update_product_attributes(original_request)
        )
        _poll_import(
            gateway,
            rollback_task_id,
            sleep_fn=sleep_fn,
        )
        readback = gateway.fetch_product(product_id)
        if not _original_snapshot_matches(task, proposal, readback):
            raise ApplyError("Rollback read-back does not match original payload")
    except Exception:
        ledger.log_action(product_id, "rollback", original_request, "failed")
        return False
    ledger.log_action(product_id, "rollback", original_request, "success")
    return True


def _package_fields_match(
    product: dict[str, Any], expected: dict[str, Any]
) -> bool:
    source = {**(product.get("seller_api_item") or {}), **product}
    for key in ("depth", "width", "height", "weight"):
        try:
            if Decimal(str(source.get(key))) != Decimal(str(expected.get(key))):
                return False
        except (InvalidOperation, TypeError, ValueError):
            return False
    return (
        str(source.get("dimension_unit") or "").casefold()
        == str(expected.get("dimension_unit") or "").casefold()
        and str(source.get("weight_unit") or "").casefold()
        == str(expected.get("weight_unit") or "").casefold()
    )


def _apply_safe_hashtag_repair(
    gateway: Any,
    ledger: Ledger,
    task: dict[str, Any],
    live_before: dict[str, Any],
    *,
    sleep_fn: Any,
) -> dict[str, Any] | None:
    normalized_values = _safe_hashtag_values(live_before)
    if normalized_values is None:
        return None
    attributes = _attribute_map(live_before.get("attributes") or [])
    original_attribute = deepcopy(attributes.get(HASHTAG_ATTRIBUTE_ID) or {})
    if not original_attribute:
        return None
    updated_attribute = deepcopy(original_attribute)
    updated_attribute["values"] = normalized_values
    offer_id = str(live_before.get("offer_id") or task.get("offer_id") or "")
    current = _product_content(live_before)
    use_import = _seller_status(live_before).get("is_created") is False
    expected_package: dict[str, Any] | None = None
    if use_import:
        try:
            expected_package = _confirmed_package_import_fields(
                task.get("pricing_evidence") or {}
            )
        except ValidationError as exc:
            return {
                "status": "blocked",
                "error": str(exc),
            }
        updated_attributes = deepcopy(attributes)
        updated_attributes[HASHTAG_ATTRIBUTE_ID] = updated_attribute
        source = live_before.get("seller_api_item") or live_before
        request = _sanitized_import_item(
            source,
            name=current["name"],
            attributes=list(updated_attributes.values()),
        )
        request.update(expected_package)
        original_request = _sanitized_import_item(
            source,
            name=current["name"],
            attributes=list(attributes.values()),
        )
    else:
        request = {"offer_id": offer_id, "attributes": [updated_attribute]}
        original_request = {"offer_id": offer_id, "attributes": [original_attribute]}
    verification_task = deepcopy(task)
    verification_task["seller_api_item"] = deepcopy(live_before)
    verification_task["current"] = current
    verification_task["non_media_score"] = non_media_score(
        live_before.get("content_score_groups") or {}
    )
    safe_proposal = {
        "base_fingerprint": str(task.get("source_fingerprint") or ""),
        "product_id": str(task["product_id"]),
        "name": current["name"],
        "description": current["description"],
        "rich_content": current["rich_content"],
        "storefront_observations": [],
        "attribute_decisions": [
            {
                "id": HASHTAG_ATTRIBUTE_ID,
                "decision": "set",
                "values": normalized_values,
                "evidence_refs": [f"seller.attributes.{HASHTAG_ATTRIBUTE_ID}"],
            }
        ],
        "risk_findings": [],
    }
    product_id = str(task["product_id"])
    try:
        task_id = (
            gateway.import_product(request)
            if use_import
            else gateway.update_product_attributes(request)
        )
        _poll_import(gateway, task_id, sleep_fn=sleep_fn)
    except Exception as exc:
        ledger.log_action(product_id, "safe_hashtag_update", request, "failed")
        return {"status": "failed", "error": str(exc)}
    try:
        live_after = _wait_for_verified_readback(
            gateway,
            verification_task,
            safe_proposal,
            sleep_fn=sleep_fn,
        )
        if expected_package is not None and not _package_fields_match(
            live_after, expected_package
        ):
            raise ApplyError("Seller read-back mismatch for confirmed package fields")
    except Exception as exc:
        live_after = gateway.fetch_product(product_id)
        actual_values = _attribute_values_by_id(live_after).get(
            HASHTAG_ATTRIBUTE_ID
        ) or []
        original_values = original_attribute.get("values") or []
        if canonical_json(actual_values) == canonical_json(original_values):
            ledger.log_action(
                product_id,
                "safe_hashtag_update",
                request,
                "pending_readback",
            )
            return {
                "status": "pending_readback",
                "product": live_after,
                "error": str(exc),
            }
        if (
            canonical_json(actual_values) == canonical_json(normalized_values)
            and (
                expected_package is None
                or _package_fields_match(live_after, expected_package)
            )
        ):
            ledger.log_action(
                product_id, "safe_hashtag_update", request, "success"
            )
            return {"status": "success", "product": live_after}
        ledger.log_action(product_id, "safe_hashtag_update", request, "failed")
        _rollback_product(
            gateway,
            ledger,
            verification_task,
            safe_proposal,
            original_request,
            use_import=use_import,
            sleep_fn=sleep_fn,
        )
        return {"status": "failed", "error": str(exc)}
    ledger.log_action(product_id, "safe_hashtag_update", request, "success")
    return {"status": "success", "product": live_after}


def _remaining_findings_after_safe_repair(
    task: dict[str, Any],
    proposal: dict[str, Any],
    live_after: dict[str, Any],
) -> list[dict[str, str]]:
    findings = _deterministic_findings(
        live_after, list(task.get("attribute_schema") or [])
    )
    by_code = {str(item["code"]): item for item in findings}
    for item in proposal.get("risk_findings") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "unspecified")
        resolution = str(item.get("resolution") or "unresolved").casefold()
        if code == "seller_error:BR_hashtag_validation":
            continue
        if resolution in SAFE_RESOLUTIONS:
            continue
        by_code.setdefault(
            code,
            {
                "code": code,
                "level": str(item.get("level") or "high").casefold(),
                "resolution": resolution,
            },
        )
    return list(by_code.values())


def apply_one(
    gateway: Any,
    ledger: Ledger,
    task: dict[str, Any],
    proposal: dict[str, Any],
    *,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    product_id = str(task["product_id"])
    live_before = gateway.fetch_product(product_id)
    if _is_archived(live_before):
        ledger.mark_status(product_id, "pending_risk", risk_codes=["archived"])
        return {"status": "skipped_archived", "product_id": product_id}

    proposal = resolve_proposal_dictionary_values(gateway, task, proposal)
    validation = validate_proposal(task, proposal)
    safe_repair = _apply_safe_hashtag_repair(
        gateway,
        ledger,
        task,
        live_before,
        sleep_fn=sleep_fn,
    )
    if safe_repair and safe_repair["status"] == "pending_readback":
        updated = False
        reason = "safe_hashtag_update_pending_readback"
        if validation["unresolved_blocking_risk"]:
            updated, stock_reason = _guarded_stock_action(
                gateway, ledger, task, 0
            )
            if not updated:
                reason = stock_reason
        status = (
            "paused"
            if validation["unresolved_blocking_risk"] and updated
            else "pending_risk"
        )
        risk_codes = list(
            dict.fromkeys(
                [
                    *validation["risk_codes"],
                    "safe_hashtag_update_pending_readback",
                    *([reason] if not updated else []),
                ]
            )
        )
        ledger.mark_status(
            product_id,
            status,
            risk_level=validation["risk_level"],
            risk_codes=risk_codes,
            error=str(safe_repair.get("error") or "")[:500],
        )
        return {
            "status": status,
            "product_id": product_id,
            "safe_repairs_submitted": ["hashtag_format"],
            "remaining_risk_codes": risk_codes,
        }
    if safe_repair and safe_repair["status"] == "success":
        live_after = safe_repair["product"]
        findings = _remaining_findings_after_safe_repair(
            task, proposal, live_after
        )
        if findings:
            highest = max(
                findings, key=lambda item: SEVERITY[item["level"]]
            )["level"]
            blocking = any(
                SEVERITY[item["level"]] >= SEVERITY["high"]
                for item in findings
            )
            updated = False
            reason = "safe_repair_applied"
            if blocking:
                updated, reason = _guarded_stock_action(
                    gateway, ledger, task, 0
                )
            status = "paused" if blocking and updated else "pending_risk"
            risk_codes = [str(item["code"]) for item in findings]
            if blocking and not updated:
                risk_codes.append(reason)
            ledger.mark_status(
                product_id,
                status,
                risk_level=highest,
                risk_codes=risk_codes,
            )
            return {
                "status": status,
                "product_id": product_id,
                "safe_repairs": ["hashtag_format"],
                "remaining_risk_codes": risk_codes,
            }
        assessment = _completion_assessment(task, live_after)
        status = str(assessment["status"])
        ledger.mark_applied(
            product_id,
            status,
            source_fingerprint(live_after),
            payload_hash(proposal),
        )
        return {
            "status": status,
            "product_id": product_id,
            "safe_repairs": ["hashtag_format"],
            "risk_codes": assessment["risk_codes"],
            "non_media_score": assessment["non_media_score"],
        }
    if validation["unresolved_blocking_risk"]:
        updated, reason = _guarded_stock_action(
            gateway, ledger, task, 0
        )
        status = "paused" if updated else "pending_risk"
        safe_repair_failed = bool(
            safe_repair and safe_repair.get("status") == "failed"
        )
        risk_codes = [
            *validation["risk_codes"],
            *(["safe_hashtag_update_failed"] if safe_repair_failed else []),
            *([reason] if not updated else []),
        ]
        ledger.mark_status(
            product_id,
            status,
            risk_level=validation["risk_level"],
            risk_codes=risk_codes,
            error=(
                str(safe_repair.get("error") or "")[:500]
                if safe_repair_failed
                else None
            ),
        )
        return {
            "status": status,
            "product_id": product_id,
            "reason": (
                "safe_hashtag_update_failed"
                if safe_repair_failed
                else reason
            ),
        }

    if not proposal_changes_product(task, proposal):
        assessment = _completion_assessment(task, live_before)
        status = str(assessment["status"])
        if status == "pending_risk":
            ledger.mark_status(
                product_id,
                status,
                risk_level=assessment["risk_level"],
                risk_codes=assessment["risk_codes"],
            )
        else:
            ledger.mark_applied(
                product_id,
                status,
                source_fingerprint(live_before),
                payload_hash(proposal),
            )
        return {
            "status": "reviewed_unchanged" if status == "completed" else status,
            "product_id": product_id,
            "risk_codes": assessment["risk_codes"],
            "non_media_score": assessment["non_media_score"],
        }

    original = deepcopy(task.get("seller_api_item") or {})
    use_import = bool(
        _has_duplicated_full_text((task.get("current") or {}).get("name"))
        and str(proposal.get("name") or "")
        != str((task.get("current") or {}).get("name") or "")
        and bool((task.get("seller_api_item") or {}).get("images"))
        and bool((task.get("seller_api_item") or {}).get("primary_image"))
    )
    request = (
        build_content_import(task, proposal)
        if use_import
        else build_attribute_update(task, proposal)
    )
    original_request = (
        build_original_content_import(task)
        if use_import
        else build_original_attribute_update(task, proposal)
    )
    action = "content_import" if use_import else "content_attribute_update"
    if ledger.action_succeeded(product_id, action, request):
        return {"status": "duplicate_skipped", "product_id": product_id}
    ledger.save_original(product_id, original)

    try:
        import_task_id = (
            gateway.import_product(request)
            if use_import
            else gateway.update_product_attributes(request)
        )
        _poll_import(gateway, import_task_id, sleep_fn=sleep_fn)
        _wait_for_verified_readback(
            gateway,
            task,
            proposal,
            sleep_fn=sleep_fn,
        )
        ledger.log_action(product_id, action, request, "success")
    except Exception as exc:
        ledger.log_action(product_id, action, request, "failed")
        try:
            unchanged = _original_snapshot_matches(
                task, proposal, gateway.fetch_product(product_id)
            )
        except Exception:
            unchanged = False
        if unchanged:
            ledger.mark_status(
                product_id,
                "pending_risk",
                risk_level="high",
                risk_codes=["write_rejected_no_change"],
                error=str(exc),
            )
            return {
                "status": "pending_risk",
                "product_id": product_id,
                "reason": "write_rejected_no_change",
            }
        if _rollback_product(
            gateway,
            ledger,
            task,
            proposal,
            original_request,
            use_import=use_import,
            sleep_fn=sleep_fn,
        ):
            ledger.mark_status(
                product_id,
                "pending_risk",
                risk_level="high",
                risk_codes=["write_rolled_back"],
                error=str(exc),
            )
            return {"status": "rolled_back", "product_id": product_id}
        updated, reason = _guarded_stock_action(gateway, ledger, task, 0)
        status = "paused" if updated else "failed"
        ledger.mark_status(
            product_id,
            status,
            risk_level="severe",
            risk_codes=["rollback_failed", reason],
            error=str(exc),
        )
        return {"status": status, "product_id": product_id, "reason": reason}

    live_after = gateway.fetch_product(product_id)
    assessment = _completion_assessment(task, live_after)
    completion_status = str(assessment["status"])
    if completion_status == "pending_risk":
        ledger.mark_status(
            product_id,
            "pending_risk",
            risk_level=assessment["risk_level"],
            risk_codes=assessment["risk_codes"],
        )
        return {
            "status": "pending_risk",
            "product_id": product_id,
            "risk_codes": assessment["risk_codes"],
            "non_media_score": assessment["non_media_score"],
        }
    if completion_status != "completed":
        ledger.mark_applied(
            product_id,
            completion_status,
            source_fingerprint(live_after),
            payload_hash(proposal),
        )
        return {
            "status": completion_status,
            "product_id": product_id,
            "risk_codes": assessment["risk_codes"],
            "non_media_score": assessment["non_media_score"],
        }
    if str(live_after.get("visibility") or "").upper() == "IN_SALE":
        ledger.mark_completed(
            product_id,
            source_fingerprint(live_after),
            payload_hash(proposal),
        )
        return {"status": "completed", "product_id": product_id}
    if _stock_count(live_after) == 0:
        updated, reason = _guarded_stock_action(gateway, ledger, task, 10)
        if not updated:
            ledger.mark_status(
                product_id,
                "pending_risk",
                risk_codes=[reason],
            )
            return {
                "status": "pending_risk",
                "product_id": product_id,
                "reason": reason,
            }
    ledger.mark_completed(
        product_id,
        source_fingerprint(gateway.fetch_product(product_id)),
        payload_hash(proposal),
    )
    return {"status": "completed", "product_id": product_id}


class Runtime:
    def __init__(
        self,
        *,
        ledger: Ledger,
        gateway: Any,
        evidence: Any,
        rule_hashes: dict[str, str],
    ) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self.evidence = evidence
        self.rule_hashes = rule_hashes


def policy_rule_hashes(path: Path | None = None) -> dict[str, str]:
    policy_path = path or (
        Path(__file__).resolve().parents[1]
        / "references"
        / "optimization-policy.md"
    )
    text = policy_path.read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {}
    current = "policy"
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().casefold()
            sections[current] = [line]
        else:
            sections.setdefault(current, []).append(line)
    return {
        key: payload_hash("\n".join(lines))
        for key, lines in sections.items()
    }


def build_runtime(repo: FsRepo | None = None) -> Runtime:
    selected_repo = repo or FsRepo()
    credentials = selected_repo.load_credentials()
    if credentials is None:
        raise ApplyError("Seller credentials are missing")
    key = store_key(credentials.client_id)
    ledger_path = (
        selected_repo.runtime_root
        / "store_content_optimizer"
        / key
        / "optimizer.sqlite3"
    )
    return Runtime(
        ledger=Ledger(ledger_path),
        gateway=SellerGateway(SellerApiAdapter(repo=selected_repo)),
        evidence=WorkbenchEvidence(selected_repo),
        rule_hashes=policy_rule_hashes(),
    )


def execute_command(
    command: str,
    runtime: Runtime,
    *,
    proposal: dict[str, Any] | None = None,
    force: bool = False,
    problems_only: bool = False,
    product_ids: set[str] | None = None,
) -> dict[str, Any]:
    if command == "audit":
        return {
            "ok": True,
            "status": "audited",
            **audit_catalog(
                runtime.gateway.fetch_catalog(),
                ledger_products=runtime.ledger.product_summary(),
            ),
        }
    if command == "scan":
        counts = scan_store(
            runtime.gateway,
            runtime.ledger,
            runtime.evidence,
            runtime.rule_hashes,
            force=force,
            problems_only=problems_only,
            product_ids=product_ids,
        )
        return {"ok": True, "status": "scanned", "counts": counts}
    if command == "next":
        task = runtime.ledger.next_task()
        if task is None:
            return {"ok": True, "status": "empty"}
        return {"ok": True, "status": "task", "task": task}
    if command == "status":
        products = runtime.ledger.product_summary()
        return {
            "ok": True,
            "status": "summary",
            "counts": runtime.ledger.status_summary(),
            "actions": runtime.ledger.action_summary(),
            "products": products,
            **completion_gate(products),
        }
    if command == "apply":
        if proposal is None:
            raise ValidationError("apply requires a proposal")
        product_id = str(proposal.get("product_id") or "")
        state = runtime.ledger.get_state(product_id)
        if state is None:
            raise ValidationError("proposal product has no queued ledger task")
        task = json.loads(str(state["task_json"]))
        try:
            result = apply_one(runtime.gateway, runtime.ledger, task, proposal)
        except Exception as exc:
            latest = runtime.ledger.get_state(product_id) or {}
            if str(latest.get("status") or "") == "drafting":
                runtime.ledger.mark_status(
                    product_id,
                    "failed",
                    risk_level="high",
                    risk_codes=["apply_failed_before_write"],
                    error=str(exc),
                )
            raise
        return {"ok": True, **result}
    raise ValidationError(f"unknown command: {command}")


def read_proposal(source: str) -> dict[str, Any]:
    if source == "-":
        text = sys.stdin.read()
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("proposal is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError("proposal must be a JSON object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("audit")
    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--force", action="store_true")
    scan_parser.add_argument("--problems-only", action="store_true")
    scan_parser.add_argument("--product-id", action="append", default=[])
    subparsers.add_parser("next")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--proposal", default="-")
    subparsers.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime = build_runtime()
        proposal = (
            read_proposal(str(args.proposal)) if args.command == "apply" else None
        )
        payload = execute_command(
            str(args.command),
            runtime,
            proposal=proposal,
            force=bool(getattr(args, "force", False)),
            problems_only=bool(getattr(args, "problems_only", False)),
            product_ids={
                str(value) for value in getattr(args, "product_id", []) if value
            }
            or None,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
        print(str(exc)[:500], file=sys.stderr)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
