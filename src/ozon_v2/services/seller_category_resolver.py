from __future__ import annotations

import re
from typing import Any

from ozon_v2.adapters.seller_api import SellerApiAdapter


def _tokens(value: Any) -> set[str]:
    values = re.findall(
        r"[a-zA-Z0-9\u3400-\u9fff\u0400-\u04ff]{2,}",
        str(value or "").casefold(),
    )
    return {token[:24] for token in values}


class SellerCategoryResolver:
    def __init__(self, adapter: SellerApiAdapter) -> None:
        self.adapter = adapter

    def resolve(
        self,
        supplier_truth: dict[str, Any],
        *,
        seed_query_terms: list[str] | None = None,
    ) -> dict[str, Any]:
        source_parts = [
            (supplier_truth.get("subject") or {}).get("value"),
            " ".join(str(value) for value in (seed_query_terms or [])),
        ]
        objective = supplier_truth.get("objective_fields") or {}
        source_parts.extend((objective.get("attributes") or {}).keys())
        source_parts.extend((objective.get("attributes") or {}).values())
        source_parts.extend((objective.get("selected_options") or {}).keys())
        source_parts.extend((objective.get("selected_options") or {}).values())
        source_tokens = _tokens(" ".join(str(value or "") for value in source_parts))

        candidates: list[dict[str, Any]] = []
        for raw in self.adapter.fetch_description_category_tree():
            if raw.get("description_category_id") is None or raw.get("type_id") is None:
                continue
            path = str(raw.get("matched_category_path") or raw.get("name") or "").strip()
            candidate_tokens = _tokens(path)
            matched = sorted(source_tokens & candidate_tokens)
            score = len(matched) / max(1, min(len(source_tokens), len(candidate_tokens)))
            candidates.append(
                {
                    "description_category_id": int(raw["description_category_id"]),
                    "type_id": int(raw["type_id"]),
                    "matched_category_path": path,
                    "match_score": round(score, 4),
                    "matched_tokens": matched,
                    "evidence_source": "locked_1688_supplier_truth_and_seed_query",
                }
            )
        candidates.sort(
            key=lambda item: (
                item["match_score"],
                len(item["matched_tokens"]),
                -item["type_id"],
            ),
            reverse=True,
        )
        top = candidates[:3]
        best_score = top[0]["match_score"] if top else 0.0
        second_score = top[1]["match_score"] if len(top) > 1 else 0.0
        unique = bool(top) and best_score >= 0.55 and (
            len(top) == 1 or best_score - second_score >= 0.15
        )
        return {
            "status": "resolved" if unique else "category_confirmation_required",
            "confidence": "high" if unique else "ambiguous",
            "source": "locked_1688_supplier_truth",
            "source_tokens": sorted(source_tokens),
            "candidates": top,
            "chosen": dict(top[0]) if unique else None,
        }
