from __future__ import annotations

import io
import ssl
import urllib.error
from unittest import TestCase
from unittest.mock import patch

from ozon_v2.adapters.seller_api import (
    SellerApiAdapter,
    SellerApiError,
    SellerApiTransportError,
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
    def test_description_category_tree_forwards_requested_language(self) -> None:
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())
        calls: list[tuple[str, dict]] = []

        def fake_post(path: str, payload: dict) -> dict:
            calls.append((path, payload))
            return {"result": []}

        adapter._post_json = fake_post

        self.assertEqual([], adapter.fetch_description_category_tree(language="ZH_HANS"))
        self.assertEqual(
            [("/v1/description-category/tree", {"language": "ZH_HANS"})],
            calls,
        )

    def test_description_category_tree_is_downloaded_once_per_language(self) -> None:
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())
        calls: list[tuple[str, dict]] = []

        def fake_post(path: str, payload: dict) -> dict:
            calls.append((path, payload))
            return {"result": []}

        adapter._post_json = fake_post

        adapter.fetch_description_category_tree(language="ZH_HANS")
        adapter.fetch_description_category_tree(language="ZH_HANS")
        adapter.fetch_description_category_tree(language="DEFAULT")
        adapter.fetch_description_category_tree(language="DEFAULT")

        self.assertEqual(
            [
                ("/v1/description-category/tree", {"language": "ZH_HANS"}),
                ("/v1/description-category/tree", {"language": "DEFAULT"}),
            ],
            calls,
        )

    def test_product_state_lookup_uses_exact_offer_and_returns_final_errors(
        self,
    ) -> None:
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())
        calls: list[tuple[str, dict]] = []

        def fake_post(path: str, payload: dict) -> dict:
            calls.append((path, payload))
            if path == "/v3/product/list":
                return {
                    "result": {
                        "items": [
                            {
                                "product_id": 5763454848,
                                "offer_id": "OZV2-PLANT-CLIPS",
                                "sku": 0,
                            }
                        ],
                        "last_id": "",
                    }
                }
            if path == "/v3/product/info/list":
                return {
                    "items": [
                        {
                            "id": 5763454848,
                            "offer_id": "OZV2-PLANT-CLIPS",
                            "sku": 0,
                            "statuses": {
                                "validation_status": "pending",
                                "is_created": False,
                            },
                            "errors": [
                                {
                                    "code": "INCORRECT_DENSITY",
                                    "level": "error",
                                }
                            ],
                        }
                    ]
                }
            self.fail(f"unexpected Seller API path: {path}")

        adapter._post_json = fake_post

        state = adapter.get_product_state_by_offer_id("OZV2-PLANT-CLIPS")

        self.assertEqual(5763454848, state["product_id"])
        self.assertIs(state["is_created"], False)
        self.assertEqual("pending", state["validation_status"])
        self.assertEqual("INCORRECT_DENSITY", state["errors"][0]["code"])
        self.assertEqual(
            {
                "filter": {
                    "offer_id": ["OZV2-PLANT-CLIPS"],
                    "visibility": "ALL",
                },
                "last_id": "",
                "limit": 1000,
            },
            calls[0][1],
        )

    @patch("ozon_v2.adapters.seller_api.urllib.request.urlopen")
    def test_post_json_retries_once_after_read_timeout(self, urlopen) -> None:
        urlopen.side_effect = [
            FakeHttpResponse(read_error=TimeoutError("The read operation timed out")),
            FakeHttpResponse(payload=b'{"result":{"items":[]}}'),
        ]
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())

        with patch("ozon_v2.adapters.seller_api.time.sleep"):
            result = adapter._post_json("/v3/product/list", {"limit": 1000})

        self.assertEqual({"result": {"items": []}}, result)
        self.assertEqual(2, urlopen.call_count)

    @patch("ozon_v2.adapters.seller_api.urllib.request.urlopen")
    def test_post_json_wraps_repeated_read_timeout_as_seller_api_error(self, urlopen) -> None:
        urlopen.side_effect = [
            FakeHttpResponse(read_error=TimeoutError("The read operation timed out")),
            FakeHttpResponse(read_error=TimeoutError("The read operation timed out")),
            FakeHttpResponse(read_error=TimeoutError("The read operation timed out")),
            FakeHttpResponse(read_error=TimeoutError("The read operation timed out")),
        ]
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())

        with patch("ozon_v2.adapters.seller_api.time.sleep"):
            with self.assertRaisesRegex(SellerApiError, "timed out"):
                adapter._post_json("/v3/product/list", {"limit": 1000})

        self.assertEqual(4, urlopen.call_count)

    @patch("ozon_v2.adapters.seller_api.urllib.request.urlopen")
    def test_post_json_retries_once_after_ssl_unexpected_eof(self, urlopen) -> None:
        urlopen.side_effect = [
            FakeHttpResponse(
                read_error=ssl.SSLEOFError(
                    8,
                    "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
                )
            ),
            FakeHttpResponse(payload=b'{"result":{"items":[]}}'),
        ]
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())

        with patch("ozon_v2.adapters.seller_api.time.sleep"):
            result = adapter._post_json("/v1/product/import/info", {"task_id": 7001})

        self.assertEqual({"result": {"items": []}}, result)
        self.assertEqual(2, urlopen.call_count)

    @patch("ozon_v2.adapters.seller_api.urllib.request.urlopen")
    def test_post_json_retries_429_with_retry_after_backoff(self, urlopen) -> None:
        urlopen.side_effect = [
            urllib.error.HTTPError(
                "https://api-seller.ozon.ru/v3/product/list",
                429,
                "Too Many Requests",
                {"Retry-After": "2"},
                io.BytesIO(b'{"message":"rate limited"}'),
            ),
            FakeHttpResponse(payload=b'{"result":{"items":[]}}'),
        ]
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())

        with patch("ozon_v2.adapters.seller_api.time.sleep") as sleep:
            result = adapter._post_json("/v3/product/list", {"limit": 1000})

        self.assertEqual({"result": {"items": []}}, result)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(2, urlopen.call_count)

    @patch("ozon_v2.adapters.seller_api.urllib.request.urlopen")
    def test_product_import_does_not_replay_unknown_timeout(self, urlopen) -> None:
        urlopen.return_value = FakeHttpResponse(
            read_error=TimeoutError("The read operation timed out")
        )
        adapter = SellerApiAdapter(repo=FakeCredentialsRepo())

        with self.assertRaisesRegex(SellerApiTransportError, "unknown outcome"):
            adapter.import_products([{"offer_id": "OZV2-ONE"}])

        self.assertEqual(1, urlopen.call_count)

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

    def test_category_match_accepts_public_root_nested_under_seller_root(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Одежда",
                "description_category_id": 400,
                "children": [
                    {
                        "category_name": "Аксессуары",
                        "children": [
                            {
                                "type_name": "Повязка на голову",
                                "type_id": 401,
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ]

        match = adapter._match_description_category(
            {
                "category_path": "Аксессуары / Женские аксессуары / Головные уборы / Банданы и косынки",
                "leaf_category": "Банданы и косынки",
                "product_title": "Повязка на голову 1 шт.",
            }
        )

        self.assertEqual(400, match["description_category_id"])
        self.assertEqual(401, match["type_id"])

    def test_category_match_rejects_cross_domain_type_with_only_stopword_overlap(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Товары для курения",
                "description_category_id": 500,
                "children": [
                    {
                        "type_name": "Аксессуар для курения",
                        "type_id": 501,
                        "children": [],
                    }
                ],
            }
        ]

        with self.assertRaises(SellerApiError):
            adapter._match_description_category(
                {
                    "category_path": "Товары для животных / Для собак / Амуниция / Аксессуары",
                    "leaf_category": "Аксессуары",
                    "product_title": "Салфетка в виде щенка из шенила, серая",
                    "product_type": "Украшение для животных",
                }
            )

    def test_category_match_rejects_parent_only_match_with_wrong_seller_type(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Детские товары",
                "description_category_id": 600,
                "children": [
                    {
                        "category_name": "Обучающие игры",
                        "children": [
                            {
                                "type_name": "Диапроектор",
                                "type_id": 601,
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ]

        with self.assertRaises(SellerApiError):
            adapter._match_description_category(
                {
                    "category_path": "Детские товары / Игрушки и игры / Развивающие игры / Обучающие игры",
                    "leaf_category": "Обучающие игры",
                    "product_title": "Образовательные бумажные карточки для тренировки по вычитанию",
                }
            )

    def test_category_match_accepts_strong_hierarchy_with_partial_leaf_alias(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Animals",
                "description_category_id": 650,
                "children": [
                    {
                        "category_name": "Domestic animals",
                        "children": [
                            {
                                "category_name": "Bird supplies",
                                "children": [
                                    {
                                        "type_name": "Parrot accessories",
                                        "type_id": 651,
                                        "children": [],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        match = adapter._match_description_category(
            {
                "category_path": "Animals / Domestic animals / Bird supplies / Parrot toys",
                "leaf_category": "Parrot toys",
                "product_title": "Set of eleven hanging items for parrots",
            }
        )

        self.assertEqual(650, match["description_category_id"])
        self.assertEqual(651, match["type_id"])

    def test_category_match_rejects_cross_domain_generic_tool_overlap(self) -> None:
        adapter = SellerApiAdapter()
        adapter._fetch_description_category_tree = lambda: [
            {
                "category_name": "Строительство и ремонт",
                "description_category_id": 700,
                "children": [
                    {
                        "category_name": "Оснастка для инструмента",
                        "children": [
                            {
                                "type_name": "Принадлежности для инструментов",
                                "type_id": 701,
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ]

        with self.assertRaises(SellerApiError):
            adapter._match_description_category(
                {
                    "category_path": "Хобби и творчество / Рукоделие / Инструменты и инвентарь / Портновские принадлежности",
                    "leaf_category": "Портновские принадлежности",
                    "product_title": "Инструмент для вышивки крестом, фиксированная клипса для вышивания",
                }
            )

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
        adapter._post_json = lambda path, payload, **kwargs: (
            calls.append((path, payload, kwargs))
            or {"result": {"task_id": 321}}
        )
        item = {"offer_id": "OZV2-ONE", "name": "Тестовый товар"}

        submitted = adapter.import_products([item])

        self.assertEqual({"task_id": 321}, submitted)
        self.assertEqual(
            [
                (
                    "/v3/product/import",
                    {"items": [item]},
                    {"retry_transient": False},
                )
            ],
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
        adapter._post_json = lambda path, payload, **_kwargs: (
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
        adapter._post_json = lambda path, payload, **_kwargs: (
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
        self.assertEqual("https://media.example/video-cover.jpg", values[21845])
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
