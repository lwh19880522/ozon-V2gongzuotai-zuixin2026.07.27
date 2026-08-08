from __future__ import annotations

import pytest

from ozon_v2.domain.supplier_truth import (
    build_supplier_truth_profile,
    validate_supplier_truth_profile,
)


def supplier_product() -> dict:
    return {
        "offer_id": "731070963867",
        "supplier_url": "https://detail.1688.com/offer/731070963867.html",
        "title": "皮革护理湿巾 80片",
        "attributes": {"用途": "清洁", "重量": "240g"},
        "images": ["https://img.example/offer.jpg"],
        "domestic_shipping_evidence": {"visible_text": "包邮"},
    }


def selection_receipt() -> dict:
    return {
        "supplier_offer_id": "731070963867",
        "supplier_sku_id": "sku-80",
        "selection_sha256": "abc123",
        "supplier_sku": {
            "supplier_sku_id": "sku-80",
            "selected_options": {"规格": "80片"},
            "set_quantity": 80,
            "set_composition": ["80片"],
            "price": {"currency": "CNY", "amount": "0.70"},
            "stock": {"status": "in_stock"},
            "image_urls": ["https://img.example/sku-80.jpg"],
            "evidence_source": "trusted_sku_map",
        },
    }


def test_build_supplier_truth_profile_uses_locked_1688_sku_only() -> None:
    profile = build_supplier_truth_profile(
        run_id="wb-test",
        seed_id="seed-1",
        slot={"slot_id": "slot-0001", "candidate_revision": 2},
        supplier_product=supplier_product(),
        selection_receipt=selection_receipt(),
    )

    assert profile["supplier_offer_id"] == "731070963867"
    assert profile["supplier_sku_id"] == "sku-80"
    assert profile["subject"]["value"] == "皮革护理湿巾 80片"
    assert profile["selected_sku_images"] == ["https://img.example/sku-80.jpg"]
    assert profile["truth_source"] == "locked_1688_supplier_sku"
    assert validate_supplier_truth_profile(profile) == []


def test_supplier_truth_rejects_missing_offer_identity() -> None:
    product = supplier_product()
    product.pop("offer_id")
    receipt = selection_receipt()
    receipt["supplier_offer_id"] = ""

    with pytest.raises(ValueError, match="supplier_offer_id is required"):
        build_supplier_truth_profile(
            run_id="wb-test",
            seed_id="seed-1",
            slot={"slot_id": "slot-0001", "candidate_revision": 1},
            supplier_product=product,
            selection_receipt=receipt,
        )
