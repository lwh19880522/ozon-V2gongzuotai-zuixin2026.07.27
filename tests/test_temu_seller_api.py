from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path
from unittest import TestCase

from ozon_v2.platforms.temu.seller_api import (
    ConfirmationRequired,
    TEMU_PUBLISH_METHOD,
    TEMU_STATUS_METHOD,
    TemuIdempotencyConflict,
    TemuIdempotencyRegistry,
    TemuSellerApi,
    TemuSellerApiError,
    load_temu_credentials,
    validate_product_request,
)


def valid_product() -> dict:
    return {
        "goodsBasic": {
            "externalGoodsId": "TEMU-GOODS-001",
            "goodsName": "Travel organizer",
            "extCatName": "Home / Storage",
            "goodsCarouselImage": ["https://media.example/main.jpg"],
            "productType": 1,
        },
        "attributes": [{"name": "Material", "value": ["Polyester"]}],
        "skuList": [
            {
                "externalSkuId": "TEMU-SKU-001",
                "images": ["https://media.example/sku.jpg"],
                "price": {
                    "basePrice": {"amount": "1000", "currency": "JPY"},
                    "listPrice": {"amount": "2000", "currency": "JPY"},
                },
                "variations": [{"name": "Color", "value": "Blue"}],
                "quantity": 100,
                "packageInfo": {
                    "weight": "22",
                    "length": "33",
                    "width": "44",
                    "height": "55",
                },
            }
        ],
    }


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class RecordingOpener:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.responses.pop(0))


class RecordingSigner:
    def __init__(self) -> None:
        self.payloads = []

    def sign(self, payload):
        self.payloads.append(payload)
        return "approved-signature"


class TemuSellerApiTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.credentials_path = self.root / "temu_credentials.local.json"
        self.credentials_path.write_text(
            json.dumps(
                {
                    "store_id": "store-001",
                    "app_key": "app-key-secret",
                    "access_token": "access-token-secret",
                }
            ),
            encoding="utf-8",
        )
        self.registry = TemuIdempotencyRegistry(
            self.root / "temu_idempotency.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_client(self, *, signer=None, opener=None) -> TemuSellerApi:
        return TemuSellerApi(
            credentials_path=self.credentials_path,
            signature_provider=signer,
            idempotency_registry=self.registry,
            opener=opener or self.fail_if_network,
            clock=lambda: 1_700_000_000,
        )

    def fail_if_network(self, request, timeout):
        self.fail("network must not be called")

    def test_credentials_are_loaded_without_secret_repr(self) -> None:
        credentials = load_temu_credentials(self.credentials_path)
        self.assertEqual("store-001", credentials.store_id)
        self.assertNotIn("app-key-secret", repr(credentials))
        self.assertNotIn("access-token-secret", repr(credentials))

    def test_validation_preserves_official_category_and_attributes(self) -> None:
        request = valid_product()
        normalized = validate_product_request(request)
        self.assertEqual(
            "Home / Storage",
            normalized["goodsBasic"]["extCatName"],
        )
        self.assertEqual(
            [{"name": "Material", "value": ["Polyester"]}],
            normalized["attributes"],
        )
        self.assertIsNot(request, normalized)

    def test_validation_rejects_missing_and_unsupported_fields(self) -> None:
        missing = valid_product()
        del missing["skuList"][0]["packageInfo"]["weight"]
        with self.assertRaisesRegex(TemuSellerApiError, "weight"):
            validate_product_request(missing)

        unsupported = valid_product()
        unsupported["categoryId"] = 123
        with self.assertRaisesRegex(TemuSellerApiError, "unsupported fields"):
            validate_product_request(unsupported)

    def test_preview_is_secret_free_and_offline(self) -> None:
        self.credentials_path.unlink()
        preview = self.build_client().preview_publish(valid_product())
        serialized = json.dumps(preview)
        self.assertEqual(TEMU_PUBLISH_METHOD, preview["method"])
        self.assertTrue(preview["requires_explicit_confirmation"])
        self.assertNotIn("app_key", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertEqual(64, len(preview["idempotency_key"]))

    def test_publish_requires_confirmation_and_approved_signer(self) -> None:
        client = self.build_client(signer=RecordingSigner())
        with self.assertRaises(ConfirmationRequired):
            client.publish_product(
                valid_product(),
                explicit_confirmation=False,
            )
        self.assertFalse((self.root / "temu_idempotency.json").exists())

        client = self.build_client(signer=None)
        with self.assertRaisesRegex(TemuSellerApiError, "signature provider"):
            client.publish_product(
                valid_product(),
                explicit_confirmation=True,
            )
        self.assertFalse((self.root / "temu_idempotency.json").exists())

    def test_publish_reserves_ids_before_network(self) -> None:
        signer = RecordingSigner()
        registry_path = self.root / "temu_idempotency.json"

        class ReservationCheckingOpener(RecordingOpener):
            def __call__(opener_self, request, timeout):
                self.assertTrue(registry_path.exists())
                return super().__call__(request, timeout)

        opener = ReservationCheckingOpener(
            [
                {
                    "success": True,
                    "result": {
                        "goodsId": 608573962731830,
                        "externalGoodsId": "TEMU-GOODS-001",
                    },
                }
            ]
        )
        client = self.build_client(signer=signer, opener=opener)
        result = client.publish_product(
            valid_product(),
            explicit_confirmation=True,
        )

        self.assertEqual(608573962731830, result["goodsId"])
        self.assertFalse(result["publication_confirmed"])
        self.assertEqual(600, result["status_check_after_seconds"])
        self.assertEqual(TEMU_PUBLISH_METHOD, signer.payloads[0]["type"])
        sent = json.loads(opener.requests[0][0].data.decode("utf-8"))
        self.assertEqual("approved-signature", sent["sign"])
        self.assertEqual(valid_product(), sent["request"])

        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "TEMU-GOODS-001",
            registry["entries"][0]["external_goods_id"],
        )
        self.assertNotIn("app-key-secret", json.dumps(registry))

    def test_duplicate_identifiers_are_blocked(self) -> None:
        opener = RecordingOpener(
            [
                {
                    "success": True,
                    "result": {
                        "goodsId": 608573962731830,
                        "externalGoodsId": "TEMU-GOODS-001",
                    },
                }
            ]
        )
        client = self.build_client(
            signer=RecordingSigner(),
            opener=opener,
        )
        client.publish_product(valid_product(), explicit_confirmation=True)
        with self.assertRaises(TemuIdempotencyConflict):
            client.publish_product(valid_product(), explicit_confirmation=True)
        self.assertEqual(1, len(opener.requests))

    def test_status_query_uses_official_goods_id_list_field(self) -> None:
        signer = RecordingSigner()
        opener = RecordingOpener(
            [
                {
                    "success": True,
                    "result": {
                        "goodsList": [
                            {
                                "goodsId": "608573962731830",
                                "goodsSearchType": "INCOMPLETE",
                            }
                        ]
                    },
                }
            ]
        )
        client = self.build_client(signer=signer, opener=opener)
        status = client.query_product_status(608573962731830)

        self.assertEqual("608573962731830", status["goodsList"][0]["goodsId"])
        self.assertEqual(TEMU_STATUS_METHOD, signer.payloads[0]["type"])
        self.assertEqual(
            {"goodsIdList": ["608573962731830"]},
            signer.payloads[0]["request"],
        )

    def test_http_and_api_errors_do_not_echo_secrets(self) -> None:
        def rejected(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "unauthorized",
                {},
                None,
            )

        client = self.build_client(
            signer=RecordingSigner(),
            opener=rejected,
        )
        with self.assertRaisesRegex(TemuSellerApiError, "HTTP 401"):
            client.query_product_status(123)

        opener = RecordingOpener(
            [
                {
                    "success": False,
                    "errorCode": 400001,
                    "errorMsg": (
                        "invalid access-token-secret, app-key-secret, "
                        "approved-signature"
                    ),
                }
            ]
        )
        client = self.build_client(
            signer=RecordingSigner(),
            opener=opener,
        )
        with self.assertRaisesRegex(TemuSellerApiError, "400001") as caught:
            client.query_product_status(123)
        message = str(caught.exception)
        self.assertNotIn("access-token-secret", message)
        self.assertNotIn("app-key-secret", message)
        self.assertNotIn("approved-signature", message)
