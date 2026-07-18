from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
from unittest import TestCase

from ozon_v2.app.context import AppContext, Config, Paths
from ozon_v2.domain.credentials import SellerCredentials


class FakeSellerApiAdapter:
    def __init__(self) -> None:
        self.resolve_count = 0
        self.last_category_candidate = None

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
