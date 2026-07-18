from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from ozon_v2.adapters.seller_api import (
    SellerApiAdapter,
    SellerApiError,
    _category_match_score,
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
