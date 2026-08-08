from __future__ import annotations

import math
import re
from typing import Any, Iterable


_RUSSIAN_STOPWORDS = {
    "без",
    "в",
    "во",
    "для",
    "до",
    "из",
    "или",
    "к",
    "на",
    "над",
    "от",
    "по",
    "под",
    "при",
    "с",
    "со",
    "у",
}

_SEARCH_NOISE = {
    "доставка",
    "заказать",
    "китай",
    "купить",
    "озон",
    "продажа",
    "товар",
    "цена",
}
_SEARCH_NOISE_STEMS = {token[:4] for token in _SEARCH_NOISE}
_STEM_ALIAS_GROUPS = (
    frozenset({"мешк", "паке"}),
)


def _tokens(value: Any) -> list[str]:
    normalized = str(value or "").lower().replace("ё", "е")
    return re.findall(r"[0-9a-zа-я]+", normalized, flags=re.IGNORECASE)


def normalized_content_stems(value: Any) -> list[str]:
    stems: list[str] = []
    for token in _tokens(value):
        if token in _RUSSIAN_STOPWORDS or len(token) < 4:
            continue
        stem = token[:4]
        if stem in _SEARCH_NOISE_STEMS:
            continue
        if stem not in stems:
            stems.append(stem)
    return stems


def build_seed_subject_contract(
    *,
    seed_id: str,
    source_text_zh: str,
    queries_ru: Iterable[str],
) -> dict[str, Any]:
    queries = [str(query or "").strip() for query in queries_ru if str(query or "").strip()]
    if not queries:
        raise ValueError("at least one Russian seed query is required")
    primary_query = queries[0]
    required_stems = normalized_content_stems(primary_query)
    if not required_stems:
        raise ValueError("Russian seed query contains no usable subject terms")
    minimum_ratio = 0.70
    return {
        "contract_version": 1,
        "seed_id": str(seed_id),
        "source_text_zh": str(source_text_zh or "").strip(),
        "primary_query_ru": primary_query,
        "required_stems": required_stems,
        "minimum_matches": max(1, math.ceil(len(required_stems) * minimum_ratio)),
        "minimum_match_ratio": minimum_ratio,
    }


def evaluate_subject_text(contract: dict[str, Any], value: Any) -> dict[str, Any]:
    required = list(dict.fromkeys(str(stem) for stem in contract.get("required_stems", []) if stem))
    product_stems = set(normalized_content_stems(value))
    def matches(stem: str) -> bool:
        if stem in product_stems:
            return True
        aliases = next((group for group in _STEM_ALIAS_GROUPS if stem in group), frozenset())
        return any(alias in product_stems for alias in aliases)

    matched = [stem for stem in required if matches(stem)]
    missing = [stem for stem in required if not matches(stem)]
    ratio = len(matched) / len(required) if required else 0.0
    minimum_matches = int(contract.get("minimum_matches") or 1)
    minimum_ratio = float(contract.get("minimum_match_ratio") or 0.70)
    return {
        "accepted": bool(required) and len(matched) >= minimum_matches and ratio >= minimum_ratio,
        "required_stems": required,
        "matched_stems": matched,
        "missing_stems": missing,
        "match_ratio": round(ratio, 4),
        "minimum_matches": minimum_matches,
        "minimum_match_ratio": minimum_ratio,
    }
