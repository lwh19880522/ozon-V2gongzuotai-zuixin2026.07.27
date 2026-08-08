from __future__ import annotations

from ozon_v2.services.seller_category_resolver import SellerCategoryResolver


class FakeAdapter:
    def __init__(self, tree: list[dict]) -> None:
        self.tree = tree

    def fetch_description_category_tree(self) -> list[dict]:
        return self.tree


def truth() -> dict:
    return {
        "subject": {"value": "湿巾"},
        "objective_fields": {
            "attributes": {"用途": "汽车清洁"},
            "selected_options": {"规格": "80片"},
        },
    }


def test_resolver_selects_one_official_type_from_supplier_truth() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            [
                {
                    "description_category_id": 10,
                    "type_id": 101,
                    "matched_category_path": "Автотовары / Салфетки влажные автомобильные",
                },
                {
                    "description_category_id": 20,
                    "type_id": 202,
                    "matched_category_path": "Дом / Бумажные полотенца",
                },
            ]
        )
    )

    result = resolver.resolve(truth(), seed_query_terms=["автомобильные влажные салфетки"])

    assert result["status"] == "resolved"
    assert result["chosen"]["description_category_id"] == 10
    assert result["source"] == "locked_1688_supplier_truth"


def test_resolver_returns_confirmation_for_ambiguous_official_types() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            [
                {"description_category_id": 10, "type_id": 101, "matched_category_path": "Дом / Салфетки"},
                {"description_category_id": 11, "type_id": 102, "matched_category_path": "Авто / Салфетки"},
            ]
        )
    )

    result = resolver.resolve(truth(), seed_query_terms=["салфетки"])

    assert result["status"] == "category_confirmation_required"
    assert result["chosen"] is None
    assert len(result["candidates"]) == 2
