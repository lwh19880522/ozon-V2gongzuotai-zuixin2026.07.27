from __future__ import annotations

import re
from typing import Iterable

from ozon_v2.domain.models import (
    DedupeDecision,
    DedupeDecisionKind,
    ExistingStoreProduct,
    OzonCandidate,
    PairStatus,
    QueryGenerationStatus,
    SeedProduct,
)

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)


def contains_cjk(value: str) -> bool:
    return bool(_CJK_RE.search(value or ""))


def normalize_identity_text(value: str | None) -> str:
    if not value:
        return ""
    return _NON_WORD_RE.sub("", value.casefold())


def seed_identity_key(seed: SeedProduct) -> str:
    return normalize_identity_text(seed.product_clue or seed.title_or_keyword)


def existing_product_keys(products: Iterable[ExistingStoreProduct]) -> dict[str, ExistingStoreProduct]:
    keys: dict[str, ExistingStoreProduct] = {}
    for product in products:
        if product.normalized_identity_key:
            keys[product.normalized_identity_key] = product
        title_key = normalize_identity_text(product.title)
        if title_key:
            keys.setdefault(title_key, product)
    return keys


def decide_seed_existing_product_dedupe(
    seed: SeedProduct,
    existing_products: Iterable[ExistingStoreProduct],
) -> DedupeDecision:
    seed_key = seed_identity_key(seed)
    if not seed_key:
        return DedupeDecision.possible_duplicate("seed has no usable identity key")
    product_map = existing_product_keys(existing_products)
    if seed_key in product_map:
        product = product_map[seed_key]
        return DedupeDecision.duplicate(
            reason="seed maps to existing store product",
            matched_product_id=product.store_product_id,
            evidence={"seed_id": seed.seed_id, "identity_key": seed_key},
        )
    return DedupeDecision.clear("seed not found in existing store products")


def decide_ozon_candidate_dedupe(
    candidate: OzonCandidate,
    existing_products: Iterable[ExistingStoreProduct],
) -> DedupeDecision:
    title_key = normalize_identity_text(candidate.title)
    product_map = existing_product_keys(existing_products)
    if title_key and title_key in product_map:
        product = product_map[title_key]
        return DedupeDecision.duplicate(
            reason="Ozon candidate title matches existing store product",
            matched_product_id=product.store_product_id,
            evidence={"ozon_product_id": candidate.ozon_product_id, "identity_key": title_key},
        )
    return DedupeDecision.clear("Ozon candidate not found in existing store products")


def seed_has_generated_ozon_query(seed: SeedProduct) -> bool:
    if seed.query_generation_status != QueryGenerationStatus.GENERATED:
        return False
    if not seed.ozon_query_terms_ru:
        return False
    return all(term.strip() and not contains_cjk(term) for term in seed.ozon_query_terms_ru)


def generated_query_terms_are_safe(source_text: str, terms: list[str]) -> bool:
    if not terms:
        return False
    source_key = normalize_identity_text(source_text)
    for term in terms:
        if contains_cjk(term):
            return False
        if normalize_identity_text(term) == source_key:
            return False
    return True


def pair_status_for_dedupe_decision(decision: DedupeDecision) -> PairStatus:
    if decision.kind == DedupeDecisionKind.CLEAR:
        return PairStatus.ACCEPTED
    if decision.kind == DedupeDecisionKind.DUPLICATE:
        return PairStatus.REJECTED
    return PairStatus.NEEDS_MANUAL_REVIEW
