from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from typing import Callable

Number = Decimal | int | float | str


def _decimal(value: Number) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _roundup(value: Decimal, places: int) -> Decimal:
    quantum = Decimal("1").scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_UP)


@dataclass(frozen=True)
class CelRfbsFreightInput:
    sale_rub: Decimal
    actual_weight_kg: Decimal
    length_cm: Decimal
    width_cm: Decimal
    height_cm: Decimal

    @classmethod
    def from_values(
        cls,
        *,
        sale_rub: Number,
        actual_weight_kg: Number,
        length_cm: Number,
        width_cm: Number,
        height_cm: Number,
    ) -> "CelRfbsFreightInput":
        return cls(
            sale_rub=_decimal(sale_rub),
            actual_weight_kg=_decimal(actual_weight_kg),
            length_cm=_decimal(length_cm),
            width_cm=_decimal(width_cm),
            height_cm=_decimal(height_cm),
        )

    @property
    def dimension_sum_cm(self) -> Decimal:
        return self.length_cm + self.width_cm + self.height_cm

    def each_side_at_most(self, limit_cm: Number) -> bool:
        limit = _decimal(limit_cm)
        return self.length_cm <= limit and self.width_cm <= limit and self.height_cm <= limit


@dataclass(frozen=True)
class CelRfbsFreightQuote:
    channel_code: str
    channel_name: str
    product_type: str
    available: bool
    freight_rmb: Decimal | None
    billing_weight_kg: Decimal | None
    volumetric_weight_kg: Decimal | None = None


@dataclass(frozen=True)
class _Rule:
    channel_code: str
    channel_name: str
    product_type: str
    rate_per_kg: Decimal
    fixed_fee: Decimal
    is_eligible: Callable[[CelRfbsFreightInput, Decimal | None], bool]
    billing_weight: Callable[[CelRfbsFreightInput], Decimal]
    volumetric_weight: Callable[[CelRfbsFreightInput], Decimal | None] = lambda _input: None


def _extra_small(input_: CelRfbsFreightInput, _billing_weight: Decimal | None = None) -> bool:
    return (
        input_.sale_rub <= Decimal("1500")
        and input_.actual_weight_kg <= Decimal("0.5")
        and input_.dimension_sum_cm <= Decimal("90")
        and input_.each_side_at_most("60")
    )


def _budget(input_: CelRfbsFreightInput, _billing_weight: Decimal | None = None) -> bool:
    return (
        input_.sale_rub <= Decimal("1500")
        and input_.actual_weight_kg > Decimal("0.5")
        and input_.actual_weight_kg <= Decimal("30")
        and input_.dimension_sum_cm <= Decimal("150")
        and input_.each_side_at_most("60")
    )


def _small(input_: CelRfbsFreightInput, _billing_weight: Decimal | None = None) -> bool:
    return (
        input_.sale_rub > Decimal("1500")
        and input_.sale_rub <= Decimal("7000")
        and input_.actual_weight_kg <= Decimal("2")
        and input_.dimension_sum_cm <= Decimal("150")
        and input_.each_side_at_most("60")
    )


def _big(input_: CelRfbsFreightInput, billing_weight: Decimal | None = None) -> bool:
    return (
        input_.sale_rub > Decimal("1500")
        and input_.sale_rub <= Decimal("7000")
        and input_.actual_weight_kg > Decimal("2")
        and input_.actual_weight_kg <= Decimal("30")
        and input_.dimension_sum_cm <= Decimal("310")
        and input_.each_side_at_most("150")
        and billing_weight is not None
        and billing_weight <= Decimal("31")
    )


def _premium_small(input_: CelRfbsFreightInput, _billing_weight: Decimal | None = None) -> bool:
    return (
        input_.sale_rub >= Decimal("7001")
        and input_.sale_rub <= Decimal("250000")
        and input_.actual_weight_kg <= Decimal("5")
        and input_.dimension_sum_cm <= Decimal("250")
        and input_.each_side_at_most("150")
    )


def _premium_big(input_: CelRfbsFreightInput, billing_weight: Decimal | None = None) -> bool:
    return (
        input_.sale_rub >= Decimal("7001")
        and input_.sale_rub <= Decimal("250000")
        and input_.actual_weight_kg > Decimal("5")
        and input_.actual_weight_kg <= Decimal("30")
        and input_.dimension_sum_cm <= Decimal("310")
        and input_.each_side_at_most("150")
        and billing_weight is not None
        and billing_weight <= Decimal("80")
    )


def _hk(input_: CelRfbsFreightInput, _billing_weight: Decimal | None = None) -> bool:
    return (
        input_.sale_rub <= Decimal("500000")
        and input_.actual_weight_kg <= Decimal("25")
        and input_.dimension_sum_cm <= Decimal("310")
        and input_.each_side_at_most("150")
    )


def _actual_weight(input_: CelRfbsFreightInput) -> Decimal:
    return input_.actual_weight_kg


def _volumetric_weight_12000(input_: CelRfbsFreightInput) -> Decimal:
    return input_.length_cm * input_.width_cm * input_.height_cm / Decimal("12000")


def _max_actual_or_volumetric_12000(input_: CelRfbsFreightInput) -> Decimal:
    return max(_volumetric_weight_12000(input_), input_.actual_weight_kg)


def _hk_volumetric_weight(input_: CelRfbsFreightInput) -> Decimal | None:
    if input_.dimension_sum_cm <= Decimal("60"):
        return None
    return _roundup(input_.length_cm * input_.width_cm * input_.height_cm / Decimal("6000"), 1)


def _hk_billing_weight(input_: CelRfbsFreightInput) -> Decimal:
    volumetric = _hk_volumetric_weight(input_)
    raw_weight = max(volumetric, input_.actual_weight_kg) if volumetric is not None else input_.actual_weight_kg
    return _roundup(raw_weight, 1)


_RULES: tuple[_Rule, ...] = (
    _Rule("extra_small_express", "CEL Express Extra Small", "Extra Small", Decimal("46.8"), Decimal("3.12"), _extra_small, _actual_weight),
    _Rule("extra_small_standard", "CEL Standard Extra Small", "Extra Small", Decimal("36.4"), Decimal("3.12"), _extra_small, _actual_weight),
    _Rule("extra_small_economy", "CEL Economy Extra Small", "Extra Small", Decimal("26"), Decimal("3.12"), _extra_small, _actual_weight),
    _Rule("budget_express", "CEL Express Budget", "Budget", Decimal("34.32"), Decimal("23.92"), _budget, _actual_weight),
    _Rule("budget_standard", "CEL Standard Budget", "Budget", Decimal("26"), Decimal("23.92"), _budget, _actual_weight),
    _Rule("budget_economy", "CEL Economy Budget", "Budget", Decimal("17.68"), Decimal("23.92"), _budget, _actual_weight),
    _Rule("small_express", "CEL Express Small", "Small", Decimal("46.8"), Decimal("16.64"), _small, _actual_weight),
    _Rule("small_standard", "CEL Standard Small", "Small", Decimal("36.4"), Decimal("16.64"), _small, _actual_weight),
    _Rule("small_economy", "CEL Economy Small", "Small", Decimal("26"), Decimal("16.64"), _small, _actual_weight),
    _Rule("big_standard", "CEL Standard Big", "Big", Decimal("26"), Decimal("37.44"), _big, _max_actual_or_volumetric_12000, _volumetric_weight_12000),
    _Rule("big_economy", "CEL Economy Big", "Big", Decimal("17.68"), Decimal("37.44"), _big, _max_actual_or_volumetric_12000, _volumetric_weight_12000),
    _Rule("premium_small_express", "CEL Express Premium Small", "Premium Small", Decimal("46.8"), Decimal("22.88"), _premium_small, _actual_weight),
    _Rule("premium_small_standard", "CEL Standard Premium Small", "Premium Small", Decimal("36.4"), Decimal("22.88"), _premium_small, _actual_weight),
    _Rule("premium_small_economy", "CEL Economy Premium Small", "Premium Small", Decimal("26"), Decimal("22.88"), _premium_small, _actual_weight),
    _Rule("premium_big_standard", "CEL Standard Premium Big", "Premium Big", Decimal("29.12"), Decimal("64.48"), _premium_big, _max_actual_or_volumetric_12000, _volumetric_weight_12000),
    _Rule("premium_big_economy", "CEL Economy Premium Big", "Premium Big", Decimal("23.92"), Decimal("64.48"), _premium_big, _max_actual_or_volumetric_12000, _volumetric_weight_12000),
    _Rule("hk_express", "CEL Express HK", "HK", Decimal("96"), Decimal("19"), _hk, _hk_billing_weight, _hk_volumetric_weight),
)


def calculate_cel_rfbs_freight(input_: CelRfbsFreightInput) -> list[CelRfbsFreightQuote]:
    quotes: list[CelRfbsFreightQuote] = []
    for rule in _RULES:
        billing_weight = rule.billing_weight(input_)
        volumetric_weight = rule.volumetric_weight(input_)
        if not rule.is_eligible(input_, billing_weight):
            quotes.append(
                CelRfbsFreightQuote(
                    channel_code=rule.channel_code,
                    channel_name=rule.channel_name,
                    product_type=rule.product_type,
                    available=False,
                    freight_rmb=None,
                    billing_weight_kg=None,
                    volumetric_weight_kg=volumetric_weight,
                )
            )
            continue
        quotes.append(
            CelRfbsFreightQuote(
                channel_code=rule.channel_code,
                channel_name=rule.channel_name,
                product_type=rule.product_type,
                available=True,
                freight_rmb=billing_weight * rule.rate_per_kg + rule.fixed_fee,
                billing_weight_kg=billing_weight,
                volumetric_weight_kg=volumetric_weight,
            )
        )
    return quotes


def available_cel_rfbs_freight(input_: CelRfbsFreightInput) -> list[CelRfbsFreightQuote]:
    return [quote for quote in calculate_cel_rfbs_freight(input_) if quote.available]
