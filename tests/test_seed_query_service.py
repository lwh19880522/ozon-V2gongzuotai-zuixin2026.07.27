from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase

from ozon_v2.domain.models import SeedProduct, SeedSearchQuery
from ozon_v2.domain.policies import generated_query_terms_are_safe
from ozon_v2.services.seed_query_service import SeedQueryService


class SeedQueryServiceTests(TestCase):
    def test_bundled_seed_pool_is_refined_2000_package(self) -> None:
        seed_path = Path(__file__).resolve().parents[1] / "assets" / "seed_pool" / "seed_pool.initial.json"
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
        seeds = [SeedProduct.from_dict(item) for item in payload["seeds"]]

        self.assertEqual(2000, payload["seed_count"])
        self.assertEqual(2000, len(seeds))
        self.assertEqual(2000, len({seed.seed_id for seed in seeds}))
        self.assertEqual(2000, len({seed.title_or_keyword for seed in seeds}))
        self.assertEqual(2000, len({seed.ozon_query_terms_ru[0].casefold() for seed in seeds}))
        self.assertTrue(all(seed.category_hint for seed in seeds))
        self.assertTrue(
            all(
                generated_query_terms_are_safe(seed.title_or_keyword, seed.ozon_query_terms_ru)
                for seed in seeds
            )
        )

    def test_bundled_seed_pool_has_complete_russian_query_coverage(self) -> None:
        seed_path = Path(__file__).resolve().parents[1] / "assets" / "seed_pool" / "seed_pool.initial.json"
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
        service = SeedQueryService()

        missing = []
        for item in payload["seeds"]:
            seed = SeedProduct.from_dict(item)
            result = service.generate_for_seed(seed)
            if not result.ok:
                missing.append({"seed_id": seed.seed_id, "title": seed.title_or_keyword})

        self.assertEqual([], missing)

    def test_common_chinese_seed_auto_generates_russian_query(self) -> None:
        seed = SeedProduct(seed_id="seed-0001", title_or_keyword="收纳盒", product_clue="收纳盒")

        result = SeedQueryService().generate_for_seed(seed)

        self.assertTrue(result.ok)
        self.assertEqual("query.generated", result.code)
        self.assertEqual(["органайзер для хранения", "контейнер для хранения"], result.data["ozon_query_terms_ru"])
        self.assertEqual("local_keyword_generator", result.data["query_generation_method"])

    def test_unknown_seed_still_needs_query_generation(self) -> None:
        seed = SeedProduct(seed_id="seed-unknown", title_or_keyword="神秘新品", product_clue="神秘新品")

        result = SeedQueryService().generate_for_seed(seed)

        self.assertFalse(result.ok)
        self.assertEqual("query.needs_generation", result.code)

    def test_summer_quilt_auto_generates_current_batch_query(self) -> None:
        seed = SeedProduct(seed_id="seed-0107", title_or_keyword="夏凉被", product_clue="夏凉被")

        result = SeedQueryService().generate_for_seed(seed)

        self.assertTrue(result.ok)
        self.assertEqual(["летнее одеяло", "легкое одеяло"], result.data["ozon_query_terms_ru"])

    def test_roll_up_pencil_case_uses_precise_product_query(self) -> None:
        seed = SeedProduct(seed_id="seed-0283", title_or_keyword="卷笔袋", product_clue="卷笔袋")

        result = SeedQueryService().generate_for_seed(seed)

        self.assertTrue(result.ok)
        self.assertEqual(["пенал-скрутка для карандашей"], result.data["ozon_query_terms_ru"])

    def test_current_shoe_related_seeds_use_distinct_precise_queries(self) -> None:
        cases = {
            "收纳鞋袋": ["сумка-органайзер для хранения обуви"],
            "鞋袋": ["мешок для хранения обуви"],
            "鞋跟贴": ["накладки на пятку для обуви"],
        }

        for index, (title, expected) in enumerate(cases.items(), start=1):
            with self.subTest(title=title):
                seed = SeedProduct(seed_id=f"seed-shoe-{index}", title_or_keyword=title, product_clue=title)
                result = SeedQueryService().generate_for_seed(seed)

                self.assertTrue(result.ok)
                self.assertEqual(expected, result.data["ozon_query_terms_ru"])
                self.assertEqual("local_keyword_generator", result.data["query_generation_method"])

    def test_mapping_generates_russian_first_query(self) -> None:
        seed = SeedProduct(seed_id="seed-0001", title_or_keyword="收纳盒", product_clue="收纳盒")
        service = SeedQueryService({"收纳盒": ["органайзер для хранения"]})

        result = service.generate_for_seed(seed)

        self.assertTrue(result.ok)
        self.assertEqual(["органайзер для хранения"], result.data["ozon_query_terms_ru"])

    def test_apply_query_keeps_seed_id_linkage(self) -> None:
        seed = SeedProduct(seed_id="seed-0001", title_or_keyword="收纳盒", product_clue="收纳盒")
        query = SeedSearchQuery(
            seed_id="seed-0001",
            source_text_zh="收纳盒",
            ozon_query_terms_ru=["органайзер для хранения"],
        )

        updated = SeedQueryService().apply_query_to_seed(seed, query)

        self.assertEqual("seed-0001", updated.seed_id)
        self.assertEqual(["органайзер для хранения"], updated.ozon_query_terms_ru)
        self.assertEqual("generated", updated.query_generation_status.value)
