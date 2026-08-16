from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from ozon_v2.adapters.seller_api import SellerApiAdapter


_COMPANY_MARKERS = (
    "有限公司",
    "有限责任公司",
    "公司",
    "商行",
    "经营部",
    "制造厂",
    "加工厂",
    "factory",
    "company",
    " co ltd",
    " inc",
)
_CATEGORY_ATTRIBUTE_MARKERS = (
    "品类",
    "种类",
    "类别",
    "类型",
    "用途",
    "适用",
    "型号",
    "功能",
    "名称",
)
_NON_CATEGORY_ATTRIBUTE_MARKERS = (
    "是否",
    "支持",
    "服务",
    "发票",
    "平台",
    "地区",
    "时间",
    "日期",
    "年份",
    "产地",
    "颜色",
    "品牌",
    "货号",
    "包装",
    "库存",
    "价格",
    "重量",
    "尺寸",
    "物流",
    "运费",
)
_REFERENCE_GENERIC_WORD_STEMS = (
    "аксессуар",
    "accessor",
)
_MOLD_PRODUCT_MARKERS = (
    "模具",
    "蛋糕模",
    "mold",
    "mould",
)
_MOLD_CATEGORY_MARKERS = (
    "模具",
    "烤盘",
    "mold",
    "mould",
    "bakeware",
    "форма",
)
_FOOD_CATEGORY_MARKERS = (
    "食品",
    "food",
    "еда",
    "пищев",
    "продукт",
)


@lru_cache(maxsize=50_000)
def _normalized_text(value: str) -> str:
    return "".join(
        re.findall(
            r"[a-zA-Z0-9\u3400-\u9fff\u0400-\u04ff]+",
            value.casefold(),
        )
    )


def _normalized(value: Any) -> str:
    return _normalized_text(str(value or ""))


@lru_cache(maxsize=50_000)
def _token_set(text: str) -> frozenset[str]:
    text = text.casefold()
    result = {
        token[:32]
        for token in re.findall(r"[a-zA-Z0-9\u0400-\u04ff]{2,}", text)
    }
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        if len(segment) <= 8:
            result.add(segment)
        result.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return frozenset(result)


def _tokens(value: Any) -> frozenset[str]:
    return _token_set(str(value or ""))


def _looks_like_company_name(value: Any) -> bool:
    text = str(value or "").casefold().strip()
    return bool(text) and any(marker in text for marker in _COMPANY_MARKERS)


def _reference_specific_words(value: Any) -> tuple[str, ...]:
    return tuple(
        word
        for word in re.findall(
            r"[a-zA-Z0-9\u0400-\u04ff]{4,}", str(value or "").casefold()
        )
        if not any(word.startswith(stem) for stem in _REFERENCE_GENERIC_WORD_STEMS)
    )


def _reference_specific_leaf_match(source: Any, leaf: Any) -> bool:
    source_words = _reference_specific_words(source)
    if not source_words:
        return False
    return any(_lexical_similarity(word, leaf) >= 0.78 for word in source_words)


def _lexical_similarity(source: Any, target: Any) -> float:
    source_text = str(source or "").strip()
    target_text = str(target or "").strip()
    source_key = _normalized(source_text)
    target_key = _normalized(target_text)
    if not source_key or not target_key:
        return 0.0
    if target_key in source_key:
        target_cjk_length = len(re.findall(r"[\u3400-\u9fff]", target_key))
        if target_cjk_length == 1 and len(target_key) == 1:
            return 0.18
        if target_cjk_length == 2 and len(target_key) == 2:
            return 0.9
        return 1.0
    if len(source_key) >= 2 and source_key in target_key:
        source_cjk_length = len(re.findall(r"[\u3400-\u9fff]", source_key))
        if source_cjk_length == 1 and len(source_key) == 1:
            return 0.16
        if source_cjk_length == 2 and len(source_key) == 2:
            return 0.78
        return 0.9

    target_tokens = _tokens(target_text)
    if not target_tokens:
        return 0.0
    source_tokens = _tokens(source_text)
    exact_coverage = len(source_tokens & target_tokens) / len(target_tokens)

    source_words = re.findall(r"[a-zA-Z0-9\u0400-\u04ff]{4,}", source_text.casefold())
    target_words = re.findall(r"[a-zA-Z0-9\u0400-\u04ff]{4,}", target_text.casefold())
    stem_matches = 0
    for target_word in target_words:
        if any(
            min(len(source_word), len(target_word)) >= 6
            and source_word[:6] == target_word[:6]
            for source_word in source_words
        ):
            stem_matches += 1
    stem_coverage = stem_matches / max(1, len(target_words))
    source_cjk = "".join(re.findall(r"[\u3400-\u9fff]", source_text))
    target_cjk = "".join(re.findall(r"[\u3400-\u9fff]", target_text))
    source_bigrams = {
        source_cjk[index : index + 2]
        for index in range(max(0, len(source_cjk) - 1))
    }
    target_bigrams = {
        target_cjk[index : index + 2]
        for index in range(max(0, len(target_cjk) - 1))
    }
    cjk_coverage = (
        len(source_bigrams & target_bigrams) / len(target_bigrams)
        if target_bigrams
        else 0.0
    )
    return min(
        1.0,
        max(exact_coverage, stem_coverage * 0.9, cjk_coverage * 0.92),
    )


@lru_cache(maxsize=20_000)
def _path_parts(path: str) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in re.split(r"\s*/\s*|\s*>\s*", path)
        if part.strip()
    )


def _is_category_attribute(label: Any) -> bool:
    normalized = _normalized(label)
    if not normalized or any(marker in normalized for marker in _NON_CATEGORY_ATTRIBUTE_MARKERS):
        return False
    return any(marker in normalized for marker in _CATEGORY_ATTRIBUTE_MARKERS)


def _dedupe_signals(
    signals: list[tuple[str, str, float]],
) -> list[tuple[str, str, float]]:
    best_by_text: dict[str, tuple[str, str, float]] = {}
    for signal in signals:
        key = _normalized(signal[1])
        previous = best_by_text.get(key)
        if key and (previous is None or signal[2] > previous[2]):
            best_by_text[key] = signal
    return list(best_by_text.values())


def _source_signals(
    supplier_truth: dict[str, Any],
    *,
    seed_query_terms: list[str],
    seed_subject: str | None,
    seed_category_hint: str | None,
    reference_product_type: str | None,
) -> list[tuple[str, str, float]]:
    objective = supplier_truth.get("objective_fields") or {}
    signals: list[tuple[str, str, float]] = []
    subject = str((supplier_truth.get("subject") or {}).get("value") or "").strip()
    if subject and not _looks_like_company_name(subject):
        signals.append(("locked_supplier_subject", subject, 1.0))
    for value in (objective.get("selected_options") or {}).values():
        if str(value or "").strip():
            signals.append(("locked_supplier_sku_option", str(value), 1.0))
    for value in objective.get("set_composition") or []:
        if str(value or "").strip():
            signals.append(("locked_supplier_set_composition", str(value), 0.9))
    for key, value in (objective.get("attributes") or {}).items():
        if _is_category_attribute(key) and str(value or "").strip():
            signals.append(("locked_supplier_attribute", str(value), 0.8))
    if str(seed_subject or "").strip():
        signals.append(("seed_subject", str(seed_subject), 0.6))
    if str(seed_category_hint or "").strip():
        signals.append(("seed_category_hint", str(seed_category_hint), 0.55))
    for value in seed_query_terms:
        if str(value or "").strip():
            signals.append(("seed_query", str(value), 0.9))
    if str(reference_product_type or "").strip():
        signals.append(("ozon_reference_product_type", str(reference_product_type), 0.95))
    return _dedupe_signals(signals)


def _candidate_role_compatibility(
    path: str,
    signals: list[tuple[str, str, float]],
) -> int:
    locked_supplier_text = " ".join(
        value.casefold()
        for source_name, value, _ in signals
        if source_name.startswith("locked_supplier_")
    )
    if not any(marker in locked_supplier_text for marker in _MOLD_PRODUCT_MARKERS):
        return 0
    candidate_path = path.casefold()
    if any(marker in candidate_path for marker in _MOLD_CATEGORY_MARKERS):
        return 1
    if any(marker in candidate_path for marker in _FOOD_CATEGORY_MARKERS):
        return -1
    return 0


def _score_candidate(
    path: str,
    signals: list[tuple[str, str, float]],
) -> tuple[float, list[str], list[str], float, float, float, float]:
    parts = _path_parts(path)
    leaf = parts[-1] if parts else path
    candidate_supports: list[tuple[float, str]] = []
    matched_tokens: set[str] = set()
    path_tokens = _tokens(path)
    for source_name, value, weight in signals:
        leaf_similarity = _lexical_similarity(value, leaf)
        context_similarity = max(
            (_lexical_similarity(value, part) for part in parts[:-1]),
            default=0.0,
        )
        combined_similarity = leaf_similarity * 0.62 + context_similarity * 0.34
        if source_name == "ozon_reference_product_type":
            if _normalized(value) == _normalized(leaf):
                combined_similarity = 1.0
            elif _reference_specific_leaf_match(value, leaf):
                combined_similarity = max(combined_similarity, leaf_similarity)
            else:
                combined_similarity *= 0.2
        if leaf_similarity >= 0.25 and context_similarity >= 0.25:
            combined_similarity += 0.04
        if combined_similarity > 0:
            candidate_supports.append(
                (min(1.0, combined_similarity) * weight, source_name)
            )
        matched_tokens.update(_tokens(value) & path_tokens)

    candidate_supports.sort(reverse=True)
    independent_sources = {
        source_name
        for score, source_name in candidate_supports
        if score >= 0.25
    }
    support_bonus = min(0.06, max(0, len(independent_sources) - 1) * 0.03)
    best_support = candidate_supports[0][0] if candidate_supports else 0.0
    score = min(1.0, best_support + support_bonus)
    supplier_evidence_score = max(
        (
            support
            for support, source_name in candidate_supports
            if source_name.startswith("locked_supplier_")
        ),
        default=0.0,
    )
    category_attribute_evidence_score = max(
        (
            support
            for support, source_name in candidate_supports
            if source_name == "locked_supplier_attribute"
        ),
        default=0.0,
    )
    seed_evidence_score = max(
        (
            support
            for support, source_name in candidate_supports
            if source_name.startswith("seed_")
        ),
        default=0.0,
    )
    reference_evidence_score = max(
        (
            support
            for support, source_name in candidate_supports
            if source_name == "ozon_reference_product_type"
        ),
        default=0.0,
    )
    return (
        round(score, 4),
        sorted(matched_tokens),
        sorted(independent_sources),
        round(supplier_evidence_score, 4),
        round(category_attribute_evidence_score, 4),
        round(seed_evidence_score, 4),
        round(reference_evidence_score, 4),
    )


def _candidate_rank_key(
    candidate: dict[str, Any],
) -> tuple[int, float, float, float, float, float, int, int]:
    return (
        int(candidate.get("role_compatibility") or 0),
        float(candidate.get("category_attribute_evidence_score") or 0.0),
        float(candidate.get("supplier_evidence_score") or 0.0),
        float(candidate.get("reference_evidence_score") or 0.0),
        float(candidate.get("seed_evidence_score") or 0.0),
        float(candidate.get("match_score") or 0.0),
        len(candidate.get("matched_tokens") or []),
        -int(candidate.get("type_id") or 0),
    )


def _is_unique_match(candidates: list[dict[str, Any]]) -> bool:
    if not candidates:
        return False
    best_role_compatibility = int(candidates[0].get("role_compatibility") or 0)
    if best_role_compatibility < 0:
        return False
    best_score = float(candidates[0].get("match_score") or 0.0)
    best_supplier_score = float(
        candidates[0].get("supplier_evidence_score") or 0.0
    )
    second_supplier_score = (
        float(candidates[1].get("supplier_evidence_score") or 0.0)
        if len(candidates) > 1
        else 0.0
    )
    second_role_compatibility = (
        int(candidates[1].get("role_compatibility") or 0)
        if len(candidates) > 1
        else 0
    )
    if best_role_compatibility > second_role_compatibility:
        return best_score >= 0.25 and best_supplier_score >= 0.25
    best_category_score = float(
        candidates[0].get("category_attribute_evidence_score") or 0.0
    )
    if best_category_score >= 0.54:
        second_category_score = (
            float(candidates[1].get("category_attribute_evidence_score") or 0.0)
            if len(candidates) > 1
            else 0.0
        )
        return (
            best_score >= 0.56
            and (
                len(candidates) == 1
                or best_category_score - second_category_score >= 0.045
            )
        )
    best_reference_score = float(
        candidates[0].get("reference_evidence_score") or 0.0
    )
    if best_reference_score >= 0.82:
        second_reference_score = (
            float(candidates[1].get("reference_evidence_score") or 0.0)
            if len(candidates) > 1
            else 0.0
        )
        corroborating_score = max(
            best_supplier_score,
            float(candidates[0].get("seed_evidence_score") or 0.0),
        )
        return (
            best_score >= 0.82
            and corroborating_score >= 0.12
            and (
                len(candidates) == 1
                or best_reference_score - second_reference_score >= 0.12
            )
        )
    return (
        best_supplier_score >= 0.54
        and best_score >= 0.56
        and (
            len(candidates) == 1
            or best_supplier_score - second_supplier_score >= 0.05
        )
    )


class SellerCategoryResolver:
    def __init__(self, adapter: SellerApiAdapter) -> None:
        self.adapter = adapter

    def resolve(
        self,
        supplier_truth: dict[str, Any],
        *,
        seed_query_terms: list[str] | None = None,
        seed_subject: str | None = None,
        seed_category_hint: str | None = None,
        reference_product_type: str | None = None,
    ) -> dict[str, Any]:
        signals = _source_signals(
            supplier_truth,
            seed_query_terms=list(seed_query_terms or []),
            seed_subject=seed_subject,
            seed_category_hint=seed_category_hint,
            reference_product_type=reference_product_type,
        )
        source_tokens = set()
        for _, value, _ in signals:
            source_tokens.update(_tokens(value))

        ranked_by_identity: dict[tuple[int, int], dict[str, Any]] = {}
        chinese_paths: dict[tuple[int, int], str] = {}
        fetch_errors: list[Exception] = []
        for language in ("ZH_HANS", "DEFAULT"):
            try:
                tree = self.adapter.fetch_description_category_tree(language=language)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
                fetch_errors.append(exc)
                continue
            for raw in tree:
                if raw.get("description_category_id") is None or raw.get("type_id") is None:
                    continue
                path = str(raw.get("matched_category_path") or raw.get("name") or "").strip()
                (
                    score,
                    matched,
                    evidence,
                    supplier_evidence_score,
                    category_attribute_evidence_score,
                    seed_evidence_score,
                    reference_evidence_score,
                ) = _score_candidate(path, signals)
                identity = (int(raw["description_category_id"]), int(raw["type_id"]))
                if language == "ZH_HANS" and path:
                    chinese_paths[identity] = path
                candidate = {
                    "description_category_id": int(raw["description_category_id"]),
                    "type_id": int(raw["type_id"]),
                    "matched_category_path": path,
                    "match_score": score,
                    "matched_tokens": matched,
                    "matching_language": language,
                    "match_evidence_sources": evidence,
                    "supplier_evidence_score": supplier_evidence_score,
                    "category_attribute_evidence_score": category_attribute_evidence_score,
                    "seed_evidence_score": seed_evidence_score,
                    "reference_evidence_score": reference_evidence_score,
                    "role_compatibility": _candidate_role_compatibility(path, signals),
                    "display_category_path_zh": chinese_paths.get(identity, ""),
                    "evidence_source": "locked_1688_supplier_truth_and_official_category_tree",
                }
                previous = ranked_by_identity.get(identity)
                if previous is None or _candidate_rank_key(candidate) > _candidate_rank_key(previous):
                    ranked_by_identity[identity] = candidate
                elif language == "ZH_HANS" and previous is not None:
                    previous["display_category_path_zh"] = path
            if language == "ZH_HANS":
                chinese_top = sorted(
                    ranked_by_identity.values(),
                    key=_candidate_rank_key,
                    reverse=True,
                )[:3]
                if _is_unique_match(chinese_top) or any(
                    float(item.get("supplier_evidence_score") or 0.0) >= 0.35
                    for item in chinese_top
                ):
                    break
        if not ranked_by_identity and fetch_errors:
            raise fetch_errors[-1]

        candidates = sorted(
            ranked_by_identity.values(),
            key=_candidate_rank_key,
            reverse=True,
        )
        if str(reference_product_type or "").strip():
            candidates = [
                candidate
                for candidate in candidates
                if max(
                    float(candidate.get("supplier_evidence_score") or 0.0),
                    float(candidate.get("seed_evidence_score") or 0.0),
                    float(candidate.get("reference_evidence_score") or 0.0),
                )
                >= 0.35
            ]
        top = candidates[:3]
        unique = _is_unique_match(top)
        return {
            "status": "resolved" if unique else "category_confirmation_required",
            "confidence": "high" if unique else "ambiguous",
            "source": "locked_1688_supplier_truth",
            "source_tokens": sorted(source_tokens),
            "candidates": top,
            "chosen": dict(top[0]) if unique else None,
        }
