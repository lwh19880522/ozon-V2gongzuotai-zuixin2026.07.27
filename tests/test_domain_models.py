from __future__ import annotations

from unittest import TestCase

from ozon_v2.domain.models import QueryGenerationStatus, SeedProduct
from ozon_v2.domain.validators import validate_seed_ready_for_ozon


class DomainModelTests(TestCase):
    def test_bundled_chinese_seed_requires_generated_ozon_query(self) -> None:
        seed = SeedProduct(seed_id="seed-0001", title_or_keyword="收纳盒", product_clue="收纳盒")

        errors = validate_seed_ready_for_ozon(seed)

        self.assertIn("generated Russian-first Ozon query terms are required before Ozon collection", errors)

    def test_seed_with_russian_query_is_ready(self) -> None:
        seed = SeedProduct(
            seed_id="seed-0001",
            title_or_keyword="收纳盒",
            product_clue="收纳盒",
            ozon_query_terms_ru=["органайзер для хранения"],
            query_generation_status=QueryGenerationStatus.GENERATED,
        )

        self.assertEqual([], validate_seed_ready_for_ozon(seed))

