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
