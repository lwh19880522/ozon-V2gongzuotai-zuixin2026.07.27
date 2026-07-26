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

    def test_dictionary_value_resolution_retries_a_leading_exact_phrase(self) -> None:
        adapter = SellerApiAdapter()
        calls = []

        def fake_post(path, payload):
            calls.append(payload["value"])
            if payload["value"] == "Серебристый":
                return {"result": [{"id": 61610, "value": "серебристый"}]}
            return {"result": []}

        adapter._post_json = fake_post

        resolved = adapter.resolve_attribute_dictionary_value(
            description_category_id=17028653,
            type_id=92121,
            attribute_id=10096,
            value="Серебристый с черными элементами",
        )

        self.assertEqual(61610, resolved["dictionary_value_id"])
        self.assertEqual("серебристый", resolved["value"])
        self.assertEqual(
            [
                "Серебристый с черными элементами",
                "Серебристый с черными",
                "Серебристый с",
                "Серебристый",
            ],
            calls,
        )

    def test_import_products_submits_one_explicit_batch_and_returns_task_id(self) -> None:
        adapter = SellerApiAdapter()
        calls = []
        adapter._post_json = lambda path, payload: (
            calls.append((path, payload))
            or {"result": {"task_id": 321}}
        )
        item = {"offer_id": "OZV2-ONE", "name": "Тестовый товар"}

        submitted = adapter.import_products([item])

        self.assertEqual({"task_id": 321}, submitted)
        self.assertEqual(
            [("/v3/product/import", {"items": [item]})],
            calls,
        )

    def test_import_products_rejects_empty_or_oversized_batches_before_network(self) -> None:
        adapter = SellerApiAdapter()
        adapter._post_json = lambda path, payload: self.fail("network must not be called")

        with self.assertRaises(SellerApiError):
            adapter.import_products([])
        with self.assertRaises(SellerApiError):
            adapter.import_products([{"offer_id": str(index)} for index in range(101)])

    def test_product_import_info_returns_normalized_result(self) -> None:
        adapter = SellerApiAdapter()
        adapter._post_json = lambda path, payload: {
            "result": {"items": [{"offer_id": "OZV2-ONE", "status": "imported"}]}
        }

        status = adapter.get_product_import_info(321)

        self.assertEqual(
            {"items": [{"offer_id": "OZV2-ONE", "status": "imported"}]},
            status,
        )

    def test_replace_product_pictures_submits_complete_ordered_gallery(self) -> None:
        adapter = SellerApiAdapter()
        calls = []
        adapter._post_json = lambda path, payload: (
            calls.append((path, payload))
            or {"result": {"pictures": [{"url": url} for url in payload["images"]]}}
        )
        images = [
            f"https://media.example/ozon-v2/main-{index:02d}.jpg"
            for index in range(1, 9)
        ]

        result = adapter.replace_product_pictures(product_id=123456, images=images)

        self.assertEqual(
            [
                (
                    "/v1/product/pictures/import",
                    {"product_id": 123456, "images": images, "images360": []},
                )
            ],
            calls,
        )
        self.assertEqual(8, len(result["pictures"]))

    def test_replace_product_pictures_rejects_non_https_or_empty_gallery(self) -> None:
        adapter = SellerApiAdapter()
        adapter._post_json = lambda path, payload: self.fail("network must not be called")

        with self.assertRaises(SellerApiError):
            adapter.replace_product_pictures(product_id=123456, images=[])
        with self.assertRaises(SellerApiError):
            adapter.replace_product_pictures(
                product_id=123456,
                images=["http://media.example/image.jpg"],
            )

    def test_attach_product_video_assets_reimports_same_offer_with_video_fields(
        self,
    ) -> None:
        adapter = SellerApiAdapter()
        calls = []
        adapter._post_json = lambda path, payload: (
            calls.append((path, payload))
            or {"result": {"task_id": 9090}}
        )
        gallery = [
            f"https://media.example/slot-{index:02d}.jpg"
            for index in range(1, 9)
        ]

        result = adapter.attach_product_video_assets(
            seller_api_item={
                "offer_id": "OZV2-ONE",
                "attributes": [
                    {"complex_id": 0, "id": 10, "values": [{"value": "existing"}]}
                ],
                "images": ["https://old.example/one.jpg"],
                "primary_image": "https://old.example/one.jpg",
            },
            video_url="https://media.example/slideshow.mp4",
            video_cover_url="https://media.example/video-cover.jpg",
            video_template_fields=[
                {"attribute_id": "21845", "kind": "video_cover_url"},
                {"attribute_id": "21846", "kind": "video_url"},
                {"attribute_id": "21847", "kind": "video_title"},
            ],
            image_urls=gallery,
        )

        self.assertEqual("/v3/product/import", calls[0][0])
        item = calls[0][1]["items"][0]
        self.assertEqual("OZV2-ONE", item["offer_id"])
        self.assertEqual(gallery, item["images"])
        self.assertEqual(gallery[0], item["primary_image"])
        values = {
            attribute["id"]: attribute["values"][0]["value"]
            for attribute in item["attributes"]
        }
        self.assertEqual("https://media.example/slideshow.mp4", values[21845])
        self.assertEqual("https://media.example/slideshow.mp4", values[21846])
        self.assertEqual("Видео о товаре", values[21847])
        self.assertEqual(9090, result["task_id"])

    def test_seller_contract_currency_is_read_from_seller_profile(self) -> None:
        adapter = SellerApiAdapter()
        calls = []
        adapter._post_json = lambda path, payload: (
            calls.append((path, payload))
            or {"result": {"company": {"currency": "CNY"}}}
        )

        currency = adapter.get_seller_currency_code()

        self.assertEqual("CNY", currency)
        self.assertEqual([("/v1/seller/info", {})], calls)

    def test_seller_contract_currency_rejects_missing_profile_value(self) -> None:
        adapter = SellerApiAdapter()
        adapter._post_json = lambda path, payload: {"result": {"company": {}}}

        with self.assertRaisesRegex(SellerApiError, "currency"):
            adapter.get_seller_currency_code()
