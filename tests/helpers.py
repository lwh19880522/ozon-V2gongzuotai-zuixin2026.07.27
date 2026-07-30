from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
from unittest import TestCase

from ozon_v2.app.context import AppContext, Config, Paths
from ozon_v2.domain.credentials import SellerCredentials
from ozon_v2.platforms.temu.seller_api import TemuSellerApiError


class FakeSellerApiAdapter:
    def __init__(self) -> None:
        self.resolve_count = 0
        self.last_category_candidate = None
        self.imported_items: list[dict] = []
        self.import_task_id = 7001
        self.seller_currency_code = "CNY"

    def resolve_attribute_template(self, category_candidate: dict) -> dict:
        self.resolve_count += 1
        self.last_category_candidate = dict(category_candidate)
        return {
            "source": "ozon_seller_api_description_category_attribute",
            "description_category_id": 17000001,
            "type_id": 970001,
            "matched_category_path": category_candidate.get("category_path") or "Дом и сад / Текстиль / Одеяла",
            "match_confidence": "high",
            "match_score": 100,
            "upload_attribute_schema": [
                {
                    "attribute_id": "85",
                    "attribute_label": "Цвет",
                    "attribute_type": "dictionary",
                    "is_required": True,
                    "allowed_values": [],
                    "dictionary_id": 100,
                    "unit": None,
                    "group": "Основные",
                    "schema_source": "ozon_seller_api_description_category_attribute",
                    "example_value_when_visible": None,
                }
            ],
        }

    def resolve_attribute_dictionary_value(
        self,
        *,
        description_category_id: int,
        type_id: int,
        attribute_id: int,
        value: str,
    ) -> dict:
        return {
            "dictionary_value_id": type_id if attribute_id == 8229 else 501,
            "value": value,
        }

    def import_products(self, items: list[dict]) -> dict:
        self.imported_items.extend(items)
        return {"task_id": self.import_task_id}

    def get_product_import_info(self, task_id: int) -> dict:
        return {
            "task_id": task_id,
            "items": [
                {"status": "imported", "offer_id": item.get("offer_id")}
                for item in self.imported_items
            ],
        }

    def get_seller_currency_code(self) -> str:
        return self.seller_currency_code


class FakeTemuSellerApi:
    def __init__(self) -> None:
        self.previewed_requests: list[dict] = []
        self.published_requests: list[dict] = []
        self.status_queries: list[str] = []
        self.publish_error: TemuSellerApiError | None = None
        self.status_error: TemuSellerApiError | None = None
        self.goods_id = "608573962731830"
        self.status_payload: dict = {
            "goodsList": [
                {
                    "goodsId": self.goods_id,
                    "status": "published",
                }
            ]
        }

    def preview_publish(self, request: dict) -> dict:
        saved = json.loads(json.dumps(request, ensure_ascii=False))
        self.previewed_requests.append(saved)
        return {
            "method": "temu.local.goods.v3.add",
            "request": saved,
            "idempotency_key": "fake-preview-operation-key",
            "requires_explicit_confirmation": True,
        }

    def publish_product(
        self,
        request: dict,
        *,
        explicit_confirmation: bool,
    ) -> dict:
        if explicit_confirmation is not True:
            raise TemuSellerApiError(
                "Temu live publication requires explicit confirmation."
            )
        if self.publish_error is not None:
            raise self.publish_error
        saved = json.loads(json.dumps(request, ensure_ascii=False))
        self.published_requests.append(saved)
        return {
            "goodsId": self.goods_id,
            "externalGoodsId": saved["goodsBasic"]["externalGoodsId"],
            "operation_key": "fake-live-operation-key",
            "publication_confirmed": False,
            "status_check_after_seconds": 600,
        }

    def query_product_status(self, goods_id: int | str) -> dict:
        self.status_queries.append(str(goods_id))
        if self.status_error is not None:
            raise self.status_error
        return json.loads(json.dumps(self.status_payload, ensure_ascii=False))


class FakePublicMediaPublisher:
    def __init__(self) -> None:
        self.published_products: list[dict] = []
        self.failure: Exception | None = None

    def publish_product(
        self,
        *,
        run_id: str,
        seed_id: str,
        source_files: list[dict],
        settings: dict,
    ) -> dict:
        if self.failure is not None:
            raise self.failure
        record = {
            "run_id": run_id,
            "seed_id": seed_id,
            "source_files": source_files,
            "settings": dict(settings),
        }
        self.published_products.append(record)
        base_url = str(settings["base_url"]).rstrip("/")
        urls = [
            f"{base_url}/ozon-v2/{run_id}/{seed_id}/{item['slot_id']}.png"
            for item in source_files
        ]
        return {
            "urls": urls,
            "items": [
                {
                    "slot_id": item["slot_id"],
                    "source_path": item["path"],
                    "public_url": url,
                }
                for item, url in zip(source_files, urls, strict=True)
            ],
        }


class RuntimeTestCase(TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ozon-v2-test-"))
        self.project_root = Path(__file__).resolve().parents[1]
        self.context = AppContext(
            paths=Paths(
                project_root=self.project_root,
                runtime_root=self.tmpdir / "OzonOpsV2",
                seed_asset_dir=self.project_root / "assets" / "seed_pool",
            ),
            config=Config(),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def save_test_credentials(self, repo) -> None:
        repo.save_credentials(SellerCredentials(client_id="test-store-id", api_key="test-secret-api-key-1234"))
        repo.replace_existing_products([])
