from __future__ import annotations

from typing import Any


def build_supplier_truth_profile(
    *,
    run_id: str,
    seed_id: str,
    slot: dict[str, Any],
    supplier_product: dict[str, Any],
    selection_receipt: dict[str, Any],
) -> dict[str, Any]:
    selected_sku = dict(selection_receipt.get("supplier_sku") or {})
    offer_id = str(
        supplier_product.get("offer_id")
        or supplier_product.get("supplier_product_id")
        or selection_receipt.get("supplier_offer_id")
        or ""
    ).strip()
    supplier_sku_id = str(
        selection_receipt.get("supplier_sku_id")
        or selected_sku.get("supplier_sku_id")
        or ""
    ).strip()
    title = str(supplier_product.get("title") or "").strip()
    profile = {
        "schema_version": 1,
        "run_id": run_id,
        "seed_id": seed_id,
        "slot_id": str(slot.get("slot_id") or "").strip(),
        "candidate_revision": int(slot.get("candidate_revision") or 1),
        "supplier_offer_id": offer_id,
        "supplier_url": str(supplier_product.get("supplier_url") or "").strip(),
        "supplier_sku_id": supplier_sku_id,
        "subject": {
            "value": title,
            "source": "locked_1688_offer_title",
        },
        "selected_sku": selected_sku,
        "objective_fields": {
            "attributes": dict(supplier_product.get("attributes") or {}),
            "selected_options": dict(selected_sku.get("selected_options") or {}),
            "set_quantity": selected_sku.get("set_quantity"),
            "set_composition": list(selected_sku.get("set_composition") or []),
            "price": dict(selected_sku.get("price") or {}),
            "stock": dict(selected_sku.get("stock") or {}),
            "domestic_shipping_evidence": dict(
                supplier_product.get("domestic_shipping_evidence") or {}
            ),
        },
        "selected_sku_images": list(selected_sku.get("image_urls") or []),
        "offer_images": list(supplier_product.get("images") or []),
        "evidence_sources": [
            {
                "kind": "supplier_offer",
                "offer_id": offer_id,
                "url": str(supplier_product.get("supplier_url") or "").strip(),
            },
            {
                "kind": "selected_supplier_sku",
                "supplier_sku_id": supplier_sku_id,
                "selection_sha256": str(selection_receipt.get("selection_sha256") or ""),
                "source": str(selected_sku.get("evidence_source") or ""),
            },
        ],
        "truth_source": "locked_1688_supplier_sku",
    }
    errors = validate_supplier_truth_profile(profile)
    if errors:
        raise ValueError("; ".join(errors))
    return profile


def validate_supplier_truth_profile(profile: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(profile.get("seed_id") or "").strip():
        errors.append("seed_id is required")
    if not str(profile.get("slot_id") or "").strip():
        errors.append("slot_id is required")
    if int(profile.get("candidate_revision") or 0) < 1:
        errors.append("candidate_revision must be at least 1")
    if not str(profile.get("supplier_offer_id") or "").strip():
        errors.append("supplier_offer_id is required")
    if not str(profile.get("supplier_sku_id") or "").strip():
        errors.append("supplier_sku_id is required")
    if not str((profile.get("subject") or {}).get("value") or "").strip():
        errors.append("supplier subject is required")
    if not list(profile.get("evidence_sources") or []):
        errors.append("evidence_sources are required")
    if not list(profile.get("selected_sku_images") or []):
        errors.append("selected SKU images are required")
    return errors
