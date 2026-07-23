from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from ozon_v2.domain.cel_freight import (
    CelRfbsFreightInput,
    available_cel_rfbs_freight,
)

Number = Decimal | int | float | str

_STANDARD_CHANNEL_CODES = {
    "extra_small_standard",
    "budget_standard",
    "small_standard",
    "big_standard",
    "premium_small_standard",
    "premium_big_standard",
}


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal number.") from exc


def _positive(value: Any, field_name: str) -> Decimal:
    parsed = _decimal(value, field_name)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return parsed


def _non_negative(value: Any, field_name: str) -> Decimal:
    parsed = _decimal(value, field_name)
    if parsed < 0:
        raise ValueError(f"{field_name} must be zero or greater.")
    return parsed


@dataclass(frozen=True)
class PricingInput:
    purchase_price_cny: Decimal
    domestic_shipping_cny: Decimal
    package_weight_g: Decimal
    package_length_cm: Decimal
    package_width_cm: Decimal
    package_height_cm: Decimal
    target_margin_rate: Decimal

    @classmethod
    def from_values(
        cls,
        *,
        purchase_price_cny: Any,
        domestic_shipping_cny: Any,
        package_weight_g: Any,
        package_length_cm: Any,
        package_width_cm: Any,
        package_height_cm: Any,
        target_margin_rate: Any,
    ) -> "PricingInput":
        return cls(
            purchase_price_cny=_positive(
                purchase_price_cny, "purchase_price_cny"
            ),
            domestic_shipping_cny=_non_negative(
                domestic_shipping_cny, "domestic_shipping_cny"
            ),
            package_weight_g=_positive(package_weight_g, "package_weight_g"),
            package_length_cm=_positive(package_length_cm, "package_length_cm"),
            package_width_cm=_positive(package_width_cm, "package_width_cm"),
            package_height_cm=_positive(package_height_cm, "package_height_cm"),
            target_margin_rate=_non_negative(
                target_margin_rate, "target_margin_rate"
            ),
        )


@dataclass(frozen=True)
class PricingPolicy:
    commission_rate: Decimal
    packaging_fee_cny: Decimal
    rub_per_cny: Decimal
    old_price_discount_rate: Decimal
    freight_rule_version: str
    logistics_channel: str = "guoo_land_air_standard"
    rounding_policy: str = "round_up_to_dot_90"

    @classmethod
    def default(cls) -> "PricingPolicy":
        return cls(
            commission_rate=Decimal("0.15"),
            packaging_fee_cny=Decimal("2.00"),
            rub_per_cny=Decimal("12"),
            old_price_discount_rate=Decimal("0.80"),
            freight_rule_version="2026-05-20",
        )

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "PricingPolicy":
        return cls(
            commission_rate=_decimal(
                payload.get("commission_rate"), "commission_rate"
            ),
            packaging_fee_cny=_decimal(
                payload.get("packaging_fee_cny"), "packaging_fee_cny"
            ),
            rub_per_cny=_decimal(payload.get("rub_per_cny"), "rub_per_cny"),
            old_price_discount_rate=_decimal(
                payload.get("old_price_discount_rate"),
                "old_price_discount_rate",
            ),
            freight_rule_version=str(
                payload.get("freight_rule_version") or ""
            ).strip(),
            logistics_channel=str(
                payload.get("logistics_channel")
                or "guoo_land_air_standard"
            ).strip(),
            rounding_policy=str(
                payload.get("rounding_policy") or "round_up_to_dot_90"
            ).strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "commission_rate": str(self.commission_rate),
            "packaging_fee_cny": str(self.packaging_fee_cny),
            "rub_per_cny": str(self.rub_per_cny),
            "old_price_discount_rate": str(self.old_price_discount_rate),
            "freight_rule_version": self.freight_rule_version,
            "logistics_channel": self.logistics_channel,
            "rounding_policy": self.rounding_policy,
        }


@dataclass(frozen=True)
class PricingQuote:
    cross_border_freight_cny: Decimal
    total_cost_cny: Decimal
    raw_listing_price_cny: Decimal
    listing_price_cny: Decimal
    listing_price_rub: Decimal
    old_price_rub: Decimal
    freight_channel_code: str
    billing_weight_kg: Decimal
    iterations: int


def round_up_to_dot_90(value: Decimal) -> Decimal:
    whole = value.to_integral_value(rounding=ROUND_FLOOR)
    candidate = whole + Decimal("0.90")
    if candidate < value:
        candidate += Decimal("1.00")
    return candidate.quantize(Decimal("0.00"))


def calculate_listing_price(
    inputs: PricingInput,
    policy: PricingPolicy,
    *,
    initial_sale_rub: Number,
) -> PricingQuote:
    denominator = (
        Decimal("1") - policy.commission_rate - inputs.target_margin_rate
    )
    if denominator <= 0:
        raise ValueError(
            "The commission rate and target margin must leave a positive "
            "listing-price denominator."
        )
    if policy.rub_per_cny <= 0:
        raise ValueError("rub_per_cny must be greater than zero.")
    if not (Decimal("0") < policy.old_price_discount_rate <= Decimal("1")):
        raise ValueError("old_price_discount_rate must be between zero and one.")

    sale_rub = _positive(initial_sale_rub, "initial_sale_rub")
    previous_channel_code: str | None = None

    for iteration in range(1, 6):
        freight_input = CelRfbsFreightInput.from_values(
            sale_rub=sale_rub,
            actual_weight_kg=inputs.package_weight_g / Decimal("1000"),
            length_cm=inputs.package_length_cm,
            width_cm=inputs.package_width_cm,
            height_cm=inputs.package_height_cm,
        )
        standard_quote = next(
            (
                quote
                for quote in available_cel_rfbs_freight(freight_input)
                if quote.channel_code in _STANDARD_CHANNEL_CODES
            ),
            None,
        )
        if (
            standard_quote is None
            or standard_quote.freight_rmb is None
            or standard_quote.billing_weight_kg is None
        ):
            raise ValueError(
                "No GUOO land-air standard quote matches this product."
            )

        total_cost = (
            inputs.purchase_price_cny
            + inputs.domestic_shipping_cny
            + policy.packaging_fee_cny
            + standard_quote.freight_rmb
        )
        raw_listing_price = total_cost / denominator
        listing_price_cny = round_up_to_dot_90(raw_listing_price)
        listing_price_rub = (
            listing_price_cny * policy.rub_per_cny
        ).to_integral_value(rounding=ROUND_CEILING)
        old_price_rub = (
            listing_price_rub / policy.old_price_discount_rate
        ).to_integral_value(rounding=ROUND_CEILING)

        if previous_channel_code == standard_quote.channel_code:
            return PricingQuote(
                cross_border_freight_cny=standard_quote.freight_rmb,
                total_cost_cny=total_cost,
                raw_listing_price_cny=raw_listing_price,
                listing_price_cny=listing_price_cny,
                listing_price_rub=listing_price_rub,
                old_price_rub=old_price_rub,
                freight_channel_code=standard_quote.channel_code,
                billing_weight_kg=standard_quote.billing_weight_kg,
                iterations=iteration,
            )

        previous_channel_code = standard_quote.channel_code
        sale_rub = listing_price_rub

    raise ValueError("GUOO freight and listing price did not converge.")
