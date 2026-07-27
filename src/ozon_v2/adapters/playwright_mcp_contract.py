from __future__ import annotations

from typing import Any

APPROVED_BROWSER_EXECUTORS = ["microsoft_playwright_mcp", "codex_in_app_browser", "workbench_browser_bridge"]


class PlaywrightMcpContractAdapter:
    def ozon_collection_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract_type": "ozon_collection",
            "executor": "microsoft_playwright_mcp",
            "approved_browser_executors": APPROVED_BROWSER_EXECUTORS,
            "payload": payload,
        }

    def ozon_attribute_template_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract_type": "ozon_attribute_template",
            "executor": "microsoft_playwright_mcp",
            "approved_browser_executors": APPROVED_BROWSER_EXECUTORS,
            "payload": payload,
        }

    def supplier_collection_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract_type": "1688_exact_match_collection",
            "executor": "microsoft_playwright_mcp",
            "approved_browser_executors": APPROVED_BROWSER_EXECUTORS,
            "payload": payload,
        }
