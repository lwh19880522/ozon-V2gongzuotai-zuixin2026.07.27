from __future__ import annotations

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import ExistingStoreProduct, SeedProduct
from ozon_v2.services.seller_history_service import SellerHistoryService

from tests.helpers import RuntimeTestCase


class FakeSellerApi:
    def __init__(self, products):
        self.products = products
        self.page_limit = None

    def fetch_existing_products(self, page_limit=None):
        self.page_limit = page_limit
        return self.products


class ExistingStoreDedupeTests(RuntimeTestCase):
    def test_filesystem_backed_stub_blocks_matching_seed(self) -> None:
        repo = FsRepo(self.context)
        service = SellerHistoryService(repo)
        service.replace_for_tests(
            [
                ExistingStoreProduct(
                    store_product_id="store-1",
                    title="收纳盒",
                    normalized_identity_key="收纳盒",
                )
            ]
        )
        seed = SeedProduct(seed_id="seed-0001", title_or_keyword="收纳盒", product_clue="收纳盒")

        result = service.seed_is_blocked(seed)

        self.assertTrue(result.ok)
        self.assertEqual("duplicate", result.data["kind"])
        self.assertEqual("store-1", result.data["matched_product_id"])

    def test_refresh_from_adapter_writes_dedupe_meta(self) -> None:
        repo = FsRepo(self.context)
        products = [
            ExistingStoreProduct(
                store_product_id="store-1",
                title="收纳盒",
                normalized_identity_key="收纳盒",
            )
        ]
        seller_api = FakeSellerApi(products)
        service = SellerHistoryService(repo, seller_api=seller_api)

        result = service.refresh_from_adapter(page_limit=1)

        self.assertTrue(result.ok)
        self.assertEqual("dedupe.refreshed", result.code)
        self.assertEqual(1, result.data["existing_product_count"])
        self.assertEqual(1, seller_api.page_limit)
        self.assertTrue(repo.existing_store_dedupe_status()["ready"])

    def test_refresh_preserves_products_missing_from_latest_api_snapshot(self) -> None:
        repo = FsRepo(self.context)
        repo.replace_existing_products(
            [
                ExistingStoreProduct(
                    store_product_id="historical-1",
                    title="Исторический товар",
                    normalized_identity_key="историческийтовар",
                )
            ]
        )
        seller_api = FakeSellerApi(
            [
                ExistingStoreProduct(
                    store_product_id="current-1",
                    title="Текущий товар",
                    normalized_identity_key="текущийтовар",
                )
            ]
        )

        result = SellerHistoryService(repo, seller_api=seller_api).refresh_from_adapter()

        self.assertTrue(result.ok)
        self.assertEqual(1, result.data["fetched_product_count"])
        self.assertEqual(2, result.data["existing_product_count"])
        self.assertEqual(
            {"historical-1", "current-1"},
            {product.store_product_id for product in repo.load_existing_products()},
        )

    def test_refresh_recovers_accepted_local_upload_before_api_visibility(self) -> None:
        repo = FsRepo(self.context)
        run = repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        repo.save_upload_previews(
            run_id,
            {
                "run_id": run_id,
                "items": {
                    "seed-local": {
                        "seed_id": "seed-local",
                        "seller_api_item": {
                            "offer_id": "OZV2-local-1",
                            "name": "Электронные настольные часы с календарем",
                        },
                    }
                },
            },
        )
        repo.save_upload_submissions(
            run_id,
            {
                "run_id": run_id,
                "items": {
                    "seed-local": {
                        "seed_id": "seed-local",
                        "offer_id": "OZV2-local-1",
                        "status": "accepted_by_ozon",
                        "submitted_at": "2026-08-03T00:00:00+00:00",
                        "seller_api_status": {
                            "items": [
                                {
                                    "offer_id": "OZV2-local-1",
                                    "product_id": 5780000001,
                                    "status": "imported",
                                }
                            ]
                        },
                    }
                },
            },
        )

        result = SellerHistoryService(repo, seller_api=FakeSellerApi([])).refresh_from_adapter()

        self.assertTrue(result.ok)
        self.assertEqual(0, result.data["fetched_product_count"])
        self.assertEqual(1, result.data["local_uploaded_product_count"])
        products = repo.load_existing_products()
        self.assertEqual(1, len(products))
        self.assertEqual("5780000001", products[0].store_product_id)
        self.assertEqual("OZV2-local-1", products[0].offer_id_when_available)
