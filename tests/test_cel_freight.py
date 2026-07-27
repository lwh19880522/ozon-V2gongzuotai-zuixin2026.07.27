from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from ozon_v2.domain.cel_freight import CelRfbsFreightInput, available_cel_rfbs_freight, calculate_cel_rfbs_freight


def _available_by_code(input_: CelRfbsFreightInput) -> dict[str, Decimal]:
    return {
        quote.channel_code: quote.freight_rmb
        for quote in available_cel_rfbs_freight(input_)
        if quote.freight_rmb is not None
    }


class CelFreightTests(TestCase):
    def test_matches_cel_sheet_default_extra_small_and_hk_values(self) -> None:
        input_ = CelRfbsFreightInput.from_values(
            sale_rub=1500,
            actual_weight_kg="0.1",
            length_cm=10,
            width_cm=10,
            height_cm=10,
        )

        quotes = _available_by_code(input_)

        self.assertEqual(Decimal("7.80"), quotes["extra_small_express"])
        self.assertEqual(Decimal("6.76"), quotes["extra_small_standard"])
        self.assertEqual(Decimal("5.72"), quotes["extra_small_economy"])
        self.assertEqual(Decimal("28.6"), quotes["hk_express"])

    def test_budget_uses_actual_weight_like_l4_l11_o4_formula(self) -> None:
        input_ = CelRfbsFreightInput.from_values(
            sale_rub=1500,
            actual_weight_kg=1,
            length_cm=10,
            width_cm=10,
            height_cm=10,
        )

        quotes = _available_by_code(input_)

        self.assertEqual(Decimal("58.24"), quotes["budget_express"])
        self.assertEqual(Decimal("49.92"), quotes["budget_standard"])
        self.assertEqual(Decimal("41.60"), quotes["budget_economy"])

    def test_small_uses_actual_weight(self) -> None:
        input_ = CelRfbsFreightInput.from_values(
            sale_rub=2000,
            actual_weight_kg=2,
            length_cm=10,
            width_cm=10,
            height_cm=10,
        )

        quotes = _available_by_code(input_)

        self.assertEqual(Decimal("110.24"), quotes["small_express"])
        self.assertEqual(Decimal("89.44"), quotes["small_standard"])
        self.assertEqual(Decimal("68.64"), quotes["small_economy"])

    def test_big_uses_max_of_actual_and_volumetric_weight(self) -> None:
        input_ = CelRfbsFreightInput.from_values(
            sale_rub=2000,
            actual_weight_kg=3,
            length_cm=60,
            width_cm=50,
            height_cm=40,
        )

        quotes = calculate_cel_rfbs_freight(input_)
        big_standard = next(quote for quote in quotes if quote.channel_code == "big_standard")
        big_economy = next(quote for quote in quotes if quote.channel_code == "big_economy")

        self.assertTrue(big_standard.available)
        self.assertEqual(Decimal("10"), big_standard.volumetric_weight_kg)
        self.assertEqual(Decimal("10"), big_standard.billing_weight_kg)
        self.assertEqual(Decimal("297.44"), big_standard.freight_rmb)
        self.assertEqual(Decimal("214.24"), big_economy.freight_rmb)

    def test_big_is_unavailable_when_billing_weight_exceeds_limit(self) -> None:
        input_ = CelRfbsFreightInput.from_values(
            sale_rub=2000,
            actual_weight_kg=3,
            length_cm=150,
            width_cm=80,
            height_cm=80,
        )

        quotes = calculate_cel_rfbs_freight(input_)
        big_standard = next(quote for quote in quotes if quote.channel_code == "big_standard")

        self.assertFalse(big_standard.available)
        self.assertEqual(Decimal("80"), big_standard.volumetric_weight_kg)

    def test_premium_big_allows_billing_weight_at_80kg_limit(self) -> None:
        input_ = CelRfbsFreightInput.from_values(
            sale_rub=10000,
            actual_weight_kg=6,
            length_cm=150,
            width_cm=80,
            height_cm=80,
        )

        quotes = _available_by_code(input_)

        self.assertEqual(Decimal("2394.08"), quotes["premium_big_standard"])
        self.assertEqual(Decimal("1978.08"), quotes["premium_big_economy"])

    def test_hk_rounds_volumetric_and_billing_weight_up_to_one_decimal(self) -> None:
        input_ = CelRfbsFreightInput.from_values(
            sale_rub=1500,
            actual_weight_kg="0.11",
            length_cm=30,
            width_cm=30,
            height_cm=1,
        )

        quotes = calculate_cel_rfbs_freight(input_)
        hk = next(quote for quote in quotes if quote.channel_code == "hk_express")

        self.assertTrue(hk.available)
        self.assertEqual(Decimal("0.2"), hk.volumetric_weight_kg)
        self.assertEqual(Decimal("0.2"), hk.billing_weight_kg)
        self.assertEqual(Decimal("38.2"), hk.freight_rmb)
