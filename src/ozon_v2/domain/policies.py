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
_IDENTITY_STOPWORDS = {
    "buy",
    "купить",
    "заказать",
}


def contains_cjk(value: str) -> bool:
    return bool(_CJK_RE.search(value or ""))


def normalize_identity_text(value: str | None) -> str:
    if not value:
        return ""
    return _NON_WORD_RE.sub("", value.casefold())


def seed_identity_key(seed: SeedProduct) -> str:
    return normalize_identity_text(seed.title_or_keyword or seed.product_clue)


def existing_product_keys(products: Iterable[ExistingStoreProduct]) -> dict[str, ExistingStoreProduct]:
    keys: dict[str, ExistingStoreProduct] = {}
    for product in products:
        for value in (
            product.normalized_identity_key,
            product.title,
            product.source_ozon_title,
        ):
            identity_key = normalize_identity_text(value)
            if identity_key:
                keys.setdefault(identity_key, product)
    return keys


def _generated_query_identity_keys(seed: SeedProduct) -> list[str]:
    keys: list[str] = []
    for query in seed.ozon_query_terms_ru:
        words = [
            word
            for word in re.findall(r"[0-9a-zа-яё]+", query.casefold(), flags=re.UNICODE)
            if word not in _IDENTITY_STOPWORDS
        ]
        key = normalize_identity_text(" ".join(words))
        if len(key) >= 8 and key not in keys:
            keys.append(key)
    return keys


def _strict_identity_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 12 and shorter in longer


def decide_seed_existing_product_dedupe(
    seed: SeedProduct,
    existing_products: Iterable[ExistingStoreProduct],
) -> DedupeDecision:
    existing_products = list(existing_products)
    seed_key = seed_identity_key(seed)
    if not seed_key:
        return DedupeDecision.possible_duplicate("seed has no usable identity key")
    for product in existing_products:
        subject_key = normalize_identity_text(product.seed_subject_identity_key)
        if _strict_identity_match(seed_key, subject_key):
            return DedupeDecision.duplicate(
                reason="seed subject matches an uploaded store product lineage",
                matched_product_id=product.store_product_id,
                evidence={
                    "seed_id": seed.seed_id,
                    "identity_key": seed_key,
                    "matched_by": "seed_subject_identity",
                },
            )
    product_map = existing_product_keys(existing_products)
    if seed_key in product_map:
        product = product_map[seed_key]
        return DedupeDecision.duplicate(
            reason="seed maps to existing store product",
            matched_product_id=product.store_product_id,
            evidence={"seed_id": seed.seed_id, "identity_key": seed_key},
        )
    query_keys = _generated_query_identity_keys(seed)
    for product in existing_products:
        product_keys = {
            normalize_identity_text(product.normalized_identity_key),
            normalize_identity_text(product.title),
            normalize_identity_text(product.source_ozon_title),
        }
        for query_key in query_keys:
            if any(_strict_identity_match(query_key, product_key) for product_key in product_keys):
                return DedupeDecision.duplicate(
                    reason="generated Ozon query maps to existing store product",
                    matched_product_id=product.store_product_id,
                    evidence={
                        "seed_id": seed.seed_id,
                        "identity_key": query_key,
                        "matched_by": "generated_query",
                    },
                )
    return DedupeDecision.clear("seed not found in existing store products")


def decide_ozon_candidate_dedupe(
    candidate: OzonCandidate,
    existing_products: Iterable[ExistingStoreProduct],
) -> DedupeDecision:
    existing_products = list(existing_products)
    candidate_product_id = str(candidate.ozon_product_id or "").strip().casefold()
    for product in existing_products:
        store_product_id = str(product.store_product_id or "").strip().casefold()
        source_product_id = str(product.source_ozon_product_id or "").strip().casefold()
        if candidate_product_id and candidate_product_id in {
            store_product_id,
            source_product_id,
        }:
            matched_by = (
                "source_ozon_product_id"
                if candidate_product_id == source_product_id and source_product_id
                else "ozon_product_id"
            )
            return DedupeDecision.duplicate(
                reason="Ozon candidate product id matches existing store product",
                matched_product_id=product.store_product_id,
                evidence={
                    "ozon_product_id": candidate.ozon_product_id,
                    "matched_by": matched_by,
                },
            )
    title_key = normalize_identity_text(candidate.title)
    product_map = existing_product_keys(existing_products)
    if title_key and title_key in product_map:
        product = product_map[title_key]
        return DedupeDecision.duplicate(
            reason="Ozon candidate title matches existing store product",
            matched_product_id=product.store_product_id,
            evidence={"ozon_product_id": candidate.ozon_product_id, "identity_key": title_key},
        )
    if title_key:
        for product in existing_products:
            product_keys = {
                normalize_identity_text(product.normalized_identity_key),
                normalize_identity_text(product.title),
                normalize_identity_text(product.source_ozon_title),
            }
            if any(
                _strict_identity_match(title_key, product_key)
                for product_key in product_keys
            ):
                return DedupeDecision.duplicate(
                    reason="Ozon candidate title maps to existing store product",
                    matched_product_id=product.store_product_id,
                    evidence={
                        "ozon_product_id": candidate.ozon_product_id,
                        "identity_key": title_key,
                        "matched_by": "strict_title_containment",
                    },
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
