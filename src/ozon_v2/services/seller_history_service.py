from __future__ import annotations

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.seller_api import SellerApiAdapter, SellerApiError
from ozon_v2.app.result import Result
from ozon_v2.domain.models import ExistingStoreProduct
from ozon_v2.domain.policies import decide_seed_existing_product_dedupe


class SellerHistoryService:
    def __init__(self, repo: FsRepo | None = None, seller_api: SellerApiAdapter | None = None) -> None:
        self.repo = repo or FsRepo()
        self.seller_api = seller_api or SellerApiAdapter()

    def refresh_from_adapter(self, page_limit: int | None = None) -> Result:
        try:
            products = self.seller_api.fetch_existing_products(page_limit=page_limit)
        except SellerApiError as exc:
            return Result.failure(
                "dedupe.refresh_failed",
                "Existing store dedupe refresh failed.",
                errors=[str(exc)],
            )
        self.repo.replace_existing_products(products)
        status = self.repo.existing_store_dedupe_status()
        return Result.success(
            "dedupe.refreshed",
            "Existing store dedupe list refreshed.",
            {
                "existing_product_count": len(products),
                "refreshed_at": status["refreshed_at"],
                "meta_path": status["meta_path"],
            },
        )

    def replace_for_tests(self, products: list[ExistingStoreProduct]) -> Result:
        self.repo.replace_existing_products(products)
        return Result.success(
            "dedupe.replaced",
            "Existing store dedupe list replaced.",
            {"existing_product_count": len(products)},
        )

    def seed_is_blocked(self, seed) -> Result:
        decision = decide_seed_existing_product_dedupe(seed, self.repo.load_existing_products())
        return Result.success("dedupe.checked", "Seed dedupe checked.", decision.to_dict())
