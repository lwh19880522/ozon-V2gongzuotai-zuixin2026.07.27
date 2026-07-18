from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SupplierSkuOption:
    supplier_sku_id: str
    combination_key: str
    raw_label: str
    selected_options: dict[str, str]
    set_quantity: int
    set_composition: list[str]
    price: dict[str, Any]
    stock: dict[str, Any]
    image_urls: list[str]
    evidence_source: str
    complete: bool
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SupplierSkuOption":
        return cls(
            supplier_sku_id=str(payload.get("supplier_sku_id") or "").strip(),
            combination_key=str(payload.get("combination_key") or "").strip(),
            raw_label=str(payload.get("raw_label") or "").strip(),
            selected_options={
                str(key).strip(): str(value).strip()
                for key, value in (payload.get("selected_options") or {}).items()
                if str(key).strip() and str(value).strip()
            },
            set_quantity=int(payload.get("set_quantity") or 0),
            set_composition=[str(item).strip() for item in payload.get("set_composition") or [] if str(item).strip()],
            price=dict(payload.get("price") or {}),
            stock=dict(payload.get("stock") or {}),
            image_urls=[str(item).strip() for item in payload.get("image_urls") or [] if str(item).strip()],
            evidence_source=str(payload.get("evidence_source") or "").strip(),
            complete=payload.get("complete") is True,
            evidence=dict(payload.get("evidence") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _documented_composition_quantity(values: list[str]) -> int | None:
    quantities: list[int] = []
    for value in values:
        match = re.search(r"(?:x|×|\*)\s*(\d+)\b", value, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"(\d+)\s*(?:支|件|个|只|套)", value)
        if match:
            quantities.append(int(match.group(1)))
    return sum(quantities) if quantities else None


def validate_supplier_sku_option(option: SupplierSkuOption) -> list[str]:
    errors: list[str] = []
    if not option.supplier_sku_id:
        errors.append("supplier_sku_id is required")
    if not option.combination_key:
        errors.append("combination_key is required")
    if not option.raw_label:
        errors.append("raw_label is required")
    if not option.selected_options:
        errors.append("selected_options are required")
    if option.set_quantity < 1:
        errors.append("set_quantity must be at least 1")
    if not option.set_composition:
        errors.append("set_composition is required")
    documented_quantity = _documented_composition_quantity(option.set_composition)
    if documented_quantity is not None and documented_quantity != option.set_quantity:
        errors.append("set_composition quantity must match set_quantity")
    if option.set_quantity > 1 and documented_quantity is None:
        errors.append("set_composition must document the set quantity")
    if not str(option.price.get("currency") or "").strip() or not str(option.price.get("amount") or "").strip():
        errors.append("price currency and amount are required")
    if not str(option.stock.get("status") or "").strip():
        errors.append("stock status is required")
    if not option.image_urls:
        errors.append("SKU-bound image_urls are required")
    if not option.evidence_source:
        errors.append("evidence_source is required")
    if option.evidence_source == "dom_option_labels" and option.complete:
        errors.append("DOM option labels cannot prove a complete supplier SKU combination")
    if not option.complete:
        errors.append("complete supplier SKU evidence is required")
    return errors


@dataclass(frozen=True)
class SupplierSkuSelectionReceipt:
    run_id: str
    product_id: str
    supplier_offer_id: str
    supplier_sku_id: str
    supplier_sku: SupplierSkuOption
    ozon_target_sku: dict[str, Any]
    differences: list[dict[str, Any] | str]
    decision: str
    selection_sha256: str
    confirmed_by: str
    confirmed_at: str

    @classmethod
    def confirmed(
        cls,
        *,
        run_id: str,
        product_id: str,
        supplier_offer_id: str,
        supplier_sku: SupplierSkuOption,
        ozon_target_sku: dict[str, Any],
        differences: list[dict[str, Any] | str],
        confirmed_at: str,
    ) -> "SupplierSkuSelectionReceipt":
        errors = validate_supplier_sku_option(supplier_sku)
        if errors:
            raise ValueError("; ".join(errors))
        core = {
            "run_id": run_id,
            "product_id": product_id,
            "supplier_offer_id": supplier_offer_id,
            "supplier_sku_id": supplier_sku.supplier_sku_id,
            "supplier_sku": supplier_sku.to_dict(),
            "ozon_target_sku": ozon_target_sku,
            "differences": differences,
            "decision": "confirmed_match",
        }
        return cls(
            run_id=run_id,
            product_id=product_id,
            supplier_offer_id=supplier_offer_id,
            supplier_sku_id=supplier_sku.supplier_sku_id,
            supplier_sku=supplier_sku,
            ozon_target_sku=ozon_target_sku,
            differences=differences,
            decision="confirmed_match",
            selection_sha256=stable_sha256(core),
            confirmed_by="user",
            confirmed_at=confirmed_at,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SupplierSkuSelectionReceipt":
        data = dict(payload)
        data["supplier_sku"] = SupplierSkuOption.from_dict(dict(data.get("supplier_sku") or {}))
        data["ozon_target_sku"] = dict(data.get("ozon_target_sku") or {})
        data["differences"] = list(data.get("differences") or [])
        return cls(**data)

    def hash_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "product_id": self.product_id,
            "supplier_offer_id": self.supplier_offer_id,
            "supplier_sku_id": self.supplier_sku_id,
            "supplier_sku": self.supplier_sku.to_dict(),
            "ozon_target_sku": self.ozon_target_sku,
            "differences": self.differences,
            "decision": self.decision,
        }

    def verify_hash(self) -> bool:
        return self.selection_sha256 == stable_sha256(self.hash_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["supplier_sku"] = self.supplier_sku.to_dict()
        return payload
