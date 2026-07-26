from __future__ import annotations

from dataclasses import replace

import pytest

from ozon_v2.domain.supplier_sku import (
    SupplierSkuOption,
    SupplierSkuSelectionReceipt,
    documented_composition_quantity,
    validate_supplier_sku_option,
)


def complete_four_piece_sku() -> SupplierSkuOption:
    return SupplierSkuOption(
        supplier_sku_id="sku-pink-4",
        combination_key="颜色:粉色|数量:4支",
        raw_label="粉色 4支套装",
        selected_options={"颜色": "粉色", "数量": "4支"},
        set_quantity=4,
        set_composition=["粉色修正笔 x4"],
        price={"currency": "CNY", "amount": "12.80"},
        stock={"status": "in_stock", "quantity": 368},
        image_urls=["https://cbu01.alicdn.com/img/sku-pink-4.jpg"],
        evidence_source="1688_public_sku_map",
        complete=True,
    )


def test_complete_real_supplier_sku_is_valid_and_round_trips() -> None:
    sku = complete_four_piece_sku()

    assert validate_supplier_sku_option(sku) == []
    assert SupplierSkuOption.from_dict(sku.to_dict()) == sku


def test_supplier_sku_selection_does_not_require_price() -> None:
    sku = replace(
        complete_four_piece_sku(),
        price={"currency": "CNY", "amount": ""},
    )

    assert validate_supplier_sku_option(sku) == []


def test_dom_button_labels_cannot_claim_complete_real_sku() -> None:
    sku = replace(
        complete_four_piece_sku(),
        supplier_sku_id="",
        evidence_source="dom_option_labels",
        complete=True,
    )

    errors = validate_supplier_sku_option(sku)

    assert "supplier_sku_id is required" in errors
    assert "DOM option labels cannot prove a complete supplier SKU combination" in errors


def test_set_quantity_must_match_documented_composition() -> None:
    sku = replace(complete_four_piece_sku(), set_composition=["粉色修正笔 x1"])

    assert "set_composition quantity must match set_quantity" in validate_supplier_sku_option(sku)


@pytest.mark.parametrize(
    ("composition", "expected"),
    [
        (["清洁剂100ml*2+刷子*1"], 3),
        (["2 флакона и 1 щетка"], 3),
        (["2 bottles + 1 brush"], 3),
        (["комплект x4"], 4),
        (["12-18 месяцев", "Размер 90 x 70 см", "модель 1020"], None),
        (["黑色均码", "适用年龄 12-18 个月", "型号 1020"], None),
    ],
)
def test_documented_composition_quantity_uses_only_explicit_sales_unit_counts(
    composition: list[str],
    expected: int | None,
) -> None:
    assert documented_composition_quantity(composition) == expected


def test_selection_hash_is_stable_across_mapping_order() -> None:
    first = complete_four_piece_sku()
    second = replace(first, selected_options={"数量": "4支", "颜色": "粉色"})

    receipt_a = SupplierSkuSelectionReceipt.confirmed(
        run_id="wb-test",
        product_id="seed-0001",
        supplier_offer_id="offer-100",
        supplier_sku=first,
        ozon_target_sku={"sku_id": "ozon-1", "selected_options": {"数量": "4", "颜色": "粉色"}},
        differences=[],
        confirmed_at="2026-07-14T00:00:00+00:00",
    )
    receipt_b = SupplierSkuSelectionReceipt.confirmed(
        run_id="wb-test",
        product_id="seed-0001",
        supplier_offer_id="offer-100",
        supplier_sku=second,
        ozon_target_sku={"selected_options": {"颜色": "粉色", "数量": "4"}, "sku_id": "ozon-1"},
        differences=[],
        confirmed_at="2026-07-14T00:00:00+00:00",
    )

    assert receipt_a.selection_sha256 == receipt_b.selection_sha256
    assert receipt_a.verify_hash()


def test_selection_receipt_detects_downstream_mutation() -> None:
    receipt = SupplierSkuSelectionReceipt.confirmed(
        run_id="wb-test",
        product_id="seed-0001",
        supplier_offer_id="offer-100",
        supplier_sku=complete_four_piece_sku(),
        ozon_target_sku={"sku_id": "ozon-1", "selected_options": {"数量": "4"}},
        differences=[],
        confirmed_at="2026-07-14T00:00:00+00:00",
    )
    payload = receipt.to_dict()
    payload["supplier_sku"]["set_quantity"] = 1

    changed = SupplierSkuSelectionReceipt.from_dict(payload)

    assert not changed.verify_hash()


def test_incomplete_supplier_sku_cannot_be_confirmed() -> None:
    incomplete = replace(complete_four_piece_sku(), complete=False)

    with pytest.raises(ValueError, match="complete supplier SKU evidence is required"):
        SupplierSkuSelectionReceipt.confirmed(
            run_id="wb-test",
            product_id="seed-0001",
            supplier_offer_id="offer-100",
            supplier_sku=incomplete,
            ozon_target_sku={"sku_id": "ozon-1"},
            differences=[],
            confirmed_at="2026-07-14T00:00:00+00:00",
        )
