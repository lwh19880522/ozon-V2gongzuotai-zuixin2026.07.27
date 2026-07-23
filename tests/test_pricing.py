from __future__ import annotations

from decimal import Decimal

import pytest

from ozon_v2.domain.pricing import (
    PricingInput,
    PricingPolicy,
    calculate_listing_price,
    round_up_to_dot_90,
)


def _first_product_input(**overrides: object) -> PricingInput:
    values: dict[str, object] = {
        "purchase_price_cny": "9.9",
        "domestic_shipping_cny": "7",
        "package_weight_g": "380",
        "package_length_cm": "28",
        "package_width_cm": "11",
        "package_height_cm": "2.5",
        "target_margin_rate": "0.20",
    }
    values.update(overrides)
    return PricingInput.from_values(**values)


def test_first_product_uses_guoo_standard_and_rounds_up_to_dot_90() -> None:
    quote = calculate_listing_price(
        _first_product_input(),
        PricingPolicy.default(),
        initial_sale_rub="1194",
    )

    assert quote.freight_channel_code == "extra_small_standard"
    assert quote.cross_border_freight_cny == Decimal("16.952")
    assert quote.total_cost_cny == Decimal("35.852")
    assert quote.raw_listing_price_cny.quantize(Decimal("0.000001")) == Decimal(
        "55.156923"
    )
    assert quote.listing_price_cny == Decimal("55.90")
    assert quote.listing_price_rub == Decimal("671")
    assert quote.old_price_rub == Decimal("839")
    assert quote.iterations == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("55.00", "55.90"),
        ("55.90", "55.90"),
        ("55.91", "56.90"),
        ("0.01", "0.90"),
    ],
)
def test_round_up_to_dot_90_never_rounds_price_down(
    value: str, expected: str
) -> None:
    assert round_up_to_dot_90(Decimal(value)) == Decimal(expected)


def test_commission_and_margin_must_leave_positive_denominator() -> None:
    with pytest.raises(ValueError, match="commission"):
        calculate_listing_price(
            _first_product_input(target_margin_rate="0.85"),
            PricingPolicy.default(),
            initial_sale_rub="1000",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purchase_price_cny", "0"),
        ("domestic_shipping_cny", "-0.01"),
        ("package_weight_g", "0"),
        ("package_length_cm", "-1"),
        ("package_width_cm", "0"),
        ("package_height_cm", "0"),
        ("target_margin_rate", "-0.01"),
    ],
)
def test_inputs_reject_invalid_cost_or_package_values(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        _first_product_input(**{field: value})


def test_product_outside_guoo_standard_limits_is_rejected() -> None:
    with pytest.raises(ValueError, match="GUOO land-air standard"):
        calculate_listing_price(
            _first_product_input(package_weight_g="31000"),
            PricingPolicy.default(),
            initial_sale_rub="1000",
        )
