from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from ozon_v2.adapters.seller_api import (
    SellerApiAdapter,
    SellerApiError,
    _category_match_score,
    _category_title_match_score,
    _iter_description_category_nodes,
    _normalize_category_text,
)
from ozon_v2.domain.credentials import SellerCredentials


class FakeCredentialsRepo:
    def load_credentials(self):
        return SellerCredentials(client_id="test-client", api_key="test-api-key")


class FakeHttpResponse:
    def __init__(self, payload: bytes = b"{}", read_error: Exception | None = None) -> None:
        self.payload = payload
        self.read_error = read_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.payload


class SellerCategoryTreeTests(TestCase):
    @patch("ozon_v2.adapters.seller_api.urllib.request.urlopen")
    def test_post_json_retries_once_after_read_timeout(self, urlopen) -> None:
        urlopen.side_effect = [
            FakeHttpResponse(read_error=TimeoutError("The read operation timed out")),
            FakeHttpResponse(payload=b'{"result":{"items":[]}}'),
        ]
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())

        result = adapter._post_json("/v3/product/list", {"limit": 1000})

        self.assertEqual({"result": {"items": []}}, result)
        self.assertEqual(2, urlopen.call_count)

    @patch("ozon_v2.adapters.seller_api.urllib.request.urlopen")
    def test_post_json_wraps_repeated_read_timeout_as_seller_api_error(self, urlopen) -> None:
        urlopen.side_effect = [
            FakeHttpResponse(read_error=TimeoutError("The read operation timed out")),
            FakeHttpResponse(read_error=TimeoutError("The read operation timed out")),
        ]
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())

        with self.assertRaisesRegex(SellerApiError, "timed out"):
            adapter._post_json("/v3/product/list", {"limit": 1000})

        self.assertEqual(2, urlopen.call_count)

    def test_type_node_inherits_description_category_id_from_parent(self) -> None:
        tree = [
            {
                "category_name": "Канцелярские товары",
                "description_category_id": 71328593,
                "children": [
                    {
                        "type_name": "Ценник",
                        "type_id": 970615927,
                        "children": [],
                    }
                ],
            }
        ]

        nodes = list(_iter_description_category_nodes(tree))

        self.assertEqual(1, len(nodes))
        self.assertEqual(71328593, nodes[0]["description_category_id"])
        self.assertEqual(970615927, nodes[0]["type_id"])
        self.assertEqual("Канцелярские товары / Ценник", nodes[0]["matched_category_path"])

    def test_russian_inflection_keeps_category_match_above_medium_threshold(self) -> None:
        target = _normalize_category_text("бумажные бирки")
        leaf = _normalize_category_text("бумажные бирки")
        node = _normalize_category_text("Хобби и творчество / Аксессуары для творчества / Бирка")

        score = _category_match_score(target, leaf, node)

        self.assertGreaterEqual(score, 35)

    def test_category_normalization_preserves_word_boundaries(self) -> None:
        self.assertEqual("бумажные бирки", _normalize_category_text("Бумажные / бирки"))

    def test_title_match_does_not_treat_mak_as_a_word_inside_gamak(self) -> None:
        score = _category_title_match_score(
            _normalize_category_text("Детский гамак для самолета"),
            _normalize_category_text("Мак"),
        )

        self.assertEqual(0, score)

    def test_category_match_rejects_title_only_cross_domain_collision(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Продукты питания",
                "description_category_id": 100,
                "children": [{"type_name": "Мак", "type_id": 101, "children": []}],
            }
        ]

        with self.assertRaises(SellerApiError):
            adapter._match_description_category(
                {
                    "category_path": "Детские товары / Переноски для детей / SUFEITE",
                    "leaf_category": "SUFEITE",
                    "product_title": "Детский гамак для самолета",
                    "product_type": "Гамак детский в самолет",
                }
            )

    def test_category_match_rejects_same_word_with_incompatible_parent_domain(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Продукты питания",
                "description_category_id": 200,
                "children": [
                    {
                        "category_name": "Хлеб и кондитерские изделия",
                        "children": [{"type_name": "Ватрушка", "type_id": 201, "children": []}],
                    }
                ],
            }
        ]

        with self.assertRaises(SellerApiError):
            adapter._match_description_category(
                {
                    "category_path": "Спорт и отдых / Водный спорт / Ватрушки и баллоны / HONGHONG",
                    "leaf_category": "HONGHONG",
                    "product_title": "Буксируемый баллон, ватрушка-диван",
                    "product_type": "Буксируемый водный аттракцион",
                }
            )

    def test_category_match_keeps_specific_title_when_parent_context_supports_it(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Строительство и ремонт",
                "description_category_id": 300,
                "children": [
                    {
                        "category_name": "Инструменты для ремонта",
                        "children": [{"type_name": "Просекатель", "type_id": 301, "children": []}],
                    }
                ],
            }
        ]

        match = adapter._match_description_category(
            {
                "category_path": "Хобби и творчество / Инструменты для рукоделия / pro sewing",
                "leaf_category": "pro sewing",
                "product_title": "Дырокол и просекатель для кожи",
                "product_type": "Инструмент для работы с кожей",
            }
        )

        self.assertEqual(301, match["type_id"])

    def test_product_title_selects_price_tag_instead_of_luggage_tag(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Галантерея и аксессуары",
                "description_category_id": 17027904,
                "children": [{"type_name": "Бирка багажная", "type_id": 94279, "children": []}],
            },
            {
                "category_name": "Канцелярские товары",
                "description_category_id": 71328593,
                "children": [{"type_name": "Ценник", "type_id": 970615927, "children": []}],
            },
        ]

        match = adapter._match_description_category(
            {
                "category_path": "бумажные бирки",
                "leaf_category": "бумажные бирки",
                "product_title": "Ценник крафтовый на веревке, набор 100 шт",
            }
        )

        self.assertEqual(71328593, match["description_category_id"])
        self.assertEqual(970615927, match["type_id"])

    def test_dictionary_value_resolution_requires_exact_normalized_value(self) -> None:
        adapter = SellerApiAdapter()
        adapter._post_json = lambda path, payload: {
            "result": [
                {"id": 501, "value": "Белый"},
                {"id": 502, "value": "Белый матовый"},
            ]
        }

        resolved = adapter.resolve_attribute_dictionary_value(
            description_category_id=10,
            type_id=20,
            attribute_id=85,
            value="белый",
        )

        self.assertEqual(501, resolved["dictionary_value_id"])
        self.assertEqual("Белый", resolved["value"])

    def test_dictionary_value_resolution_does_not_guess_near_match(self) -> None:
        adapter = SellerApiAdapter()
        adapter._post_json = lambda path, payload: {
            "result": [{"id": 502, "value": "Белый матовый"}]
        }

        with self.assertRaises(SellerApiError):
            adapter.resolve_attribute_dictionary_value(
                description_category_id=10,
                type_id=20,
                attribute_id=85,
                value="Белый",
            )
