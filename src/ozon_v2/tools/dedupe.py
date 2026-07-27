from __future__ import annotations

from ozon_v2.services.seller_history_service import SellerHistoryService


def ozon_v2_refresh_existing_store_dedupe(page_limit: int | None = None) -> dict:
    return SellerHistoryService().refresh_from_adapter(page_limit=page_limit).to_dict()
