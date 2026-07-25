from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import ExistingStoreProduct
from ozon_v2.domain.policies import normalize_identity_text


class SellerApiError(RuntimeError):
    pass


class SellerApiAdapter:
    def __init__(self, repo: FsRepo | None = None, base_url: str = "https://api-seller.ozon.ru") -> None:
        self.repo = repo or FsRepo()
        self.base_url = base_url.rstrip("/")

    def fetch_existing_products(self, page_limit: int | None = None) -> list[ExistingStoreProduct]:
        credentials = self.repo.load_credentials()
        if not credentials:
            raise SellerApiError("Seller credentials are missing.")

        product_refs = self._fetch_product_refs(page_limit=page_limit)
        if not product_refs:
            return []

        info_by_id = self._fetch_product_info([int(item["product_id"]) for item in product_refs])
        products: list[ExistingStoreProduct] = []
        for ref in product_refs:
            product_id = str(ref["product_id"])
            info = info_by_id.get(product_id, {})
            title = str(info.get("name") or ref.get("offer_id") or product_id)
            category_id = info.get("description_category_id") or info.get("category_id")
            primary_image = _first_string(info.get("primary_image")) or _first_string(info.get("images"))
            products.append(
                ExistingStoreProduct(
                    store_product_id=product_id,
                    offer_id_when_available=str(ref.get("offer_id") or info.get("offer_id") or ""),
                    title=title,
                    brand=None,
                    category_path=f"description_category_id:{category_id}" if category_id else None,
                    main_image_reference=primary_image,
                    selected_sku_or_options_when_available={"sku": ref.get("sku")} if ref.get("sku") else {},
                    product_url_when_available=f"https://www.ozon.ru/product/{product_id}/",
                    normalized_identity_key=normalize_identity_text(title),
                    notes="refreshed from Ozon Seller API",
                )
            )
        return products

    def resolve_attribute_template(self, category_candidate: dict[str, Any]) -> dict[str, Any]:
        match = self._match_description_category(category_candidate)
        attributes = self._fetch_description_category_attributes(
            int(match["description_category_id"]),
            int(match["type_id"]),
        )
        schema = [_normalize_upload_attribute(item) for item in attributes]
        schema = [item for item in schema if item["attribute_id"]]
        if not schema:
            raise SellerApiError("Seller category attribute template did not return upload attributes.")
        return {
            "source": "ozon_seller_api_description_category_attribute",
            "description_category_id": match["description_category_id"],
            "type_id": match["type_id"],
            "matched_category_path": match["matched_category_path"],
            "match_confidence": match["match_confidence"],
            "match_score": match["match_score"],
            "upload_attribute_schema": schema,
        }

    def _match_description_category(self, category_candidate: dict[str, Any]) -> dict[str, Any]:
        target_path = str(category_candidate.get("category_path") or "")
        target_leaf = str(category_candidate.get("leaf_category") or "")
        target_url = str(category_candidate.get("category_url") or category_candidate.get("category_id") or "")
        target_title = str(category_candidate.get("product_title") or "")
        target_product_type = str(category_candidate.get("product_type") or "")
        target_key = _normalize_category_text(" ".join([target_path, target_leaf, target_url]))
        if not target_key:
            raise SellerApiError("Public Ozon category evidence is missing.")

        tree = self._fetch_description_category_tree()
        matches: list[dict[str, Any]] = []
        for node in _iter_description_category_nodes(tree):
            if not node.get("description_category_id") or node.get("type_id") is None:
                continue
            assessment = assess_category_template_match(
                category_path=target_path,
                leaf_category=target_leaf,
                matched_category_path=str(node["matched_category_path"]),
                product_title=target_title,
                product_type=target_product_type,
                category_url=target_url,
            )
            score = assessment["match_score"]
            if assessment["credible"] and score >= 35:
                item = dict(node)
                item["match_score"] = score
                item["match_confidence"] = "high" if score >= 80 else "medium" if score >= 35 else "low"
                item["match_evidence"] = assessment
                matches.append(item)
        if not matches:
            raise SellerApiError("Could not match public Ozon category to Seller description category template.")
        matches.sort(key=lambda item: item["match_score"], reverse=True)
        return matches[0]

    def _fetch_description_category_tree(self) -> list[dict[str, Any]]:
        payload = self._post_json("/v1/description-category/tree", {"language": "DEFAULT"})
        result = payload.get("result", [])
        if not isinstance(result, list):
            raise SellerApiError("Seller category tree response has invalid shape.")
        return result

    def _fetch_description_category_attributes(
        self,
        description_category_id: int,
        type_id: int,
    ) -> list[dict[str, Any]]:
        payload = self._post_json(
            "/v1/description-category/attribute",
            {
                "description_category_id": description_category_id,
                "type_id": type_id,
                "language": "DEFAULT",
            },
        )
        result = payload.get("result", [])
        if isinstance(result, dict):
            result = result.get("attributes", [])
        if not isinstance(result, list):
            raise SellerApiError("Seller attribute template response has invalid shape.")
        return result

    def resolve_attribute_dictionary_value(
        self,
        *,
        description_category_id: int,
        type_id: int,
        attribute_id: int,
        value: str,
    ) -> dict[str, Any]:
        words = str(value or "").strip().split()
        candidates = [
            " ".join(words[:length])
            for length in range(len(words), 0, -1)
        ]
        seen: set[str] = set()
        for candidate in candidates:
            target = _normalize_category_text(candidate)
            if not target or target in seen:
                continue
            seen.add(target)
            payload = self._post_json(
                "/v1/description-category/attribute/values/search",
                {
                    "attribute_id": attribute_id,
                    "description_category_id": description_category_id,
                    "type_id": type_id,
                    "value": candidate,
                    "limit": 100,
                },
            )
            result = payload.get("result", [])
            if isinstance(result, dict):
                result = result.get("values") or result.get("items") or []
            if not isinstance(result, list):
                raise SellerApiError(
                    "Seller attribute dictionary response has invalid shape."
                )
            exact_matches = []
            for item in result:
                if not isinstance(item, dict):
                    continue
                visible_value = str(
                    item.get("value") or item.get("name") or ""
                ).strip()
                value_id = (
                    item.get("id")
                    or item.get("value_id")
                    or item.get("dictionary_value_id")
                )
                if (
                    value_id is None
                    or _normalize_category_text(visible_value) != target
                ):
                    continue
                exact_matches.append(
                    {
                        "dictionary_value_id": value_id,
                        "value": visible_value,
                    }
                )
            unique_matches = {
                str(match["dictionary_value_id"]): match
                for match in exact_matches
            }
            if len(unique_matches) == 1:
                return next(iter(unique_matches.values()))
            if len(unique_matches) > 1:
                break
        raise SellerApiError(
            f"Could not resolve one exact dictionary value for attribute {attribute_id}: {value}"
        )

    def import_products(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            raise SellerApiError("At least one product is required for Seller API import.")
        if len(items) > 100:
            raise SellerApiError("Seller API product import accepts at most 100 products.")
        payload = self._post_json("/v3/product/import", {"items": items})
        result = payload.get("result", {})
        if not isinstance(result, dict):
            result = {}
        task_id = result.get("task_id") or payload.get("task_id")
        if task_id is None:
            raise SellerApiError("Seller API product import did not return a task_id.")
        return {"task_id": task_id}

    def get_product_import_info(self, task_id: int) -> dict[str, Any]:
        payload = self._post_json("/v1/product/import/info", {"task_id": task_id})
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            raise SellerApiError("Seller API product import status has invalid shape.")
        return result

    def replace_product_pictures(
        self,
        *,
        product_id: int,
        images: list[str],
    ) -> dict[str, Any]:
        if int(product_id) <= 0:
            raise SellerApiError("A positive Ozon product_id is required.")
        normalized_images = [str(url or "").strip() for url in images]
        if not normalized_images:
            raise SellerApiError("At least one product picture is required.")
        if any(not url.startswith("https://") for url in normalized_images):
            raise SellerApiError("Every product picture must use a public HTTPS URL.")
        payload = self._post_json(
            "/v1/product/pictures/import",
            {
                "product_id": int(product_id),
                "images": normalized_images,
                "images360": [],
            },
        )
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            raise SellerApiError("Seller API picture import returned an invalid result.")
        return result

    def get_seller_currency_code(self) -> str:
        payload = self._post_json("/v1/seller/info", {})
        result = payload.get("result", payload)
        company = result.get("company") if isinstance(result, dict) else None
        currency = (
            str(company.get("currency") or "").strip().upper()
            if isinstance(company, dict)
            else ""
        )
        if currency not in {"CNY", "RUB", "USD", "EUR"}:
            raise SellerApiError(
                "Seller API profile did not return a supported contract currency."
            )
        return currency

    def _fetch_product_refs(self, page_limit: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        last_id = ""
        page = 0
        while True:
            page += 1
            payload = self._post_json(
                "/v3/product/list",
                {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000},
            )
            result = payload.get("result", {})
            page_items = result.get("items", [])
            items.extend(page_items)
            last_id = result.get("last_id") or ""
            if not last_id:
                break
            if page_limit is not None and page >= page_limit:
                break
        return items

    def _fetch_product_info(self, product_ids: list[int]) -> dict[str, dict[str, Any]]:
        info_by_id: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(product_ids, 100):
            payload = self._post_json("/v3/product/info/list", {"product_id": chunk})
            for item in payload.get("items", []):
                product_id = str(item.get("id") or item.get("product_id") or "")
                if product_id:
                    info_by_id[product_id] = item
        return info_by_id

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        credentials = self.repo.load_credentials()
        if not credentials:
            raise SellerApiError("Seller credentials are missing.")
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Client-Id": credentials.client_id,
                "Api-Key": credentials.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise SellerApiError(f"Ozon Seller API returned HTTP {exc.code}: {body[:300]}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 0:
                    continue
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else str(exc)
                raise SellerApiError(f"Ozon Seller API request failed: {reason}") from exc
        raise SellerApiError("Ozon Seller API request failed after retry.")


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _first_string(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        first = value[0]
        return first if isinstance(first, str) else None
    return None


def _iter_description_category_nodes(
    items: list[dict[str, Any]],
    path: tuple[str, ...] = (),
    description_category_id: int | None = None,
):
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("disabled"):
            continue
        name = str(
            item.get("type_name")
            or item.get("category_name")
            or item.get("name")
            or item.get("title")
            or ""
        ).strip()
        current_path = path + ((name,) if name else ())
        current_description_category_id = item.get("description_category_id")
        if current_description_category_id is None:
            current_description_category_id = description_category_id
        type_id = item.get("type_id")
        if current_description_category_id is not None and type_id is not None:
            yield {
                "description_category_id": current_description_category_id,
                "type_id": type_id,
                "matched_category_path": " / ".join(current_path),
            }
        children = item.get("children") or item.get("categories") or item.get("items") or []
        if isinstance(children, list) and children:
            yield from _iter_description_category_nodes(
                children,
                current_path,
                current_description_category_id,
            )


def _category_match_score(target_key: str, target_leaf_key: str, node_key: str) -> int:
    score = 0
    if target_leaf_key and target_leaf_key in node_key:
        score += 70
    if node_key and node_key in target_key:
        score += 50
    target_tokens = _token_keys(target_key)
    target_leaf_tokens = _token_keys(target_leaf_key)
    node_tokens = _token_keys(node_key)
    if target_leaf_tokens and node_tokens:
        matched_leaf_tokens = sum(
            1 for token in target_leaf_tokens if any(_category_tokens_match(token, node) for node in node_tokens)
        )
        score += int(70 * matched_leaf_tokens / len(target_leaf_tokens))
    if target_tokens and node_tokens:
        matched_node_tokens = sum(
            1 for token in node_tokens if any(_category_tokens_match(token, target) for target in target_tokens)
        )
        score += int(30 * matched_node_tokens / len(node_tokens))
    return score


def _token_keys(value: str) -> set[str]:
    return {token for token in value.split() if len(token) >= 3}


def _category_tokens_match(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter = min(len(left), len(right))
    if shorter < 5:
        return False
    common_prefix = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        common_prefix += 1
    return common_prefix >= shorter - 1


def _category_title_match_score(title_key: str, node_leaf_key: str) -> int:
    if not title_key or not node_leaf_key:
        return 0
    if f" {node_leaf_key} " in f" {title_key} ":
        return 100
    title_tokens = _token_keys(title_key)
    leaf_tokens = _token_keys(node_leaf_key)
    if not title_tokens or not leaf_tokens:
        return 0
    matched = sum(
        1 for leaf_token in leaf_tokens if any(_category_tokens_match(leaf_token, title) for title in title_tokens)
    )
    return int(80 * matched / len(leaf_tokens))


_CATEGORY_CONTEXT_STOPWORDS = {
    "для",
    "товар",
    "товары",
    "изделия",
    "аксессуары",
    "принадлежности",
}


def assess_category_template_match(
    *,
    category_path: str,
    matched_category_path: str,
    leaf_category: str = "",
    product_title: str = "",
    product_type: str = "",
    category_url: str = "",
) -> dict[str, Any]:
    target_key = _normalize_category_text(" ".join([category_path, leaf_category, category_url]))
    target_leaf_key = _normalize_category_text(leaf_category)
    node_key = _normalize_category_text(matched_category_path)
    node_leaf = str(matched_category_path).rsplit("/", 1)[-1]
    node_leaf_key = _normalize_category_text(node_leaf)
    category_score = _category_match_score(target_key, target_leaf_key, node_key)
    title_score = _category_title_match_score(_normalize_category_text(product_title), node_leaf_key)
    type_score = _category_title_match_score(_normalize_category_text(product_type), node_leaf_key)

    target_parts = [part.strip() for part in category_path.split("/") if part.strip()]
    node_parts = [part.strip() for part in matched_category_path.split("/") if part.strip()]
    root_compatible = _category_roots_match(
        target_parts[0] if target_parts else "",
        node_parts[0] if node_parts else "",
    )
    context_overlap = _category_context_overlap(
        " ".join(target_parts[:-1] or target_parts),
        " ".join(node_parts[:-1]),
    )
    has_hierarchy = len(target_parts) > 1 and len(node_parts) > 1
    cross_domain = has_hierarchy and not root_compatible
    credible = not cross_domain or type_score > 0 or (title_score > 0 and context_overlap > 0)
    reason = "category_evidence_aligned" if credible else "cross_domain_category_mismatch"
    return {
        "credible": credible,
        "reason": reason,
        "match_score": category_score + max(title_score, type_score),
        "category_score": category_score,
        "title_score": title_score,
        "product_type_score": type_score,
        "root_compatible": root_compatible,
        "context_overlap": context_overlap,
    }


def _category_roots_match(left: str, right: str) -> bool:
    left_tokens = _context_tokens(_normalize_category_text(left))
    right_tokens = _context_tokens(_normalize_category_text(right))
    return bool(
        left_tokens
        and right_tokens
        and any(_category_tokens_match(a, b) for a in left_tokens for b in right_tokens)
    )


def _category_context_overlap(left: str, right: str) -> int:
    left_tokens = _context_tokens(_normalize_category_text(left))
    right_tokens = _context_tokens(_normalize_category_text(right))
    return sum(
        1
        for token in right_tokens
        if any(_category_tokens_match(token, target) for target in left_tokens)
    )


def _context_tokens(value: str) -> set[str]:
    return {
        token
        for token in _token_keys(value)
        if token not in _CATEGORY_CONTEXT_STOPWORDS
    }


def _normalize_category_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(part for part in re.split(r"[^\w]+", value.casefold()) if part)


def _normalize_upload_attribute(item: dict[str, Any]) -> dict[str, Any]:
    dictionary_id = item.get("dictionary_id") or item.get("value_dictionary_id")
    return {
        "attribute_id": str(item.get("id") or item.get("attribute_id") or "").strip(),
        "attribute_label": str(item.get("name") or item.get("attribute_name") or "").strip(),
        "attribute_type": str(item.get("type") or ("dictionary" if dictionary_id else "string")).strip(),
        "is_required": bool(item.get("is_required")),
        "allowed_values": [],
        "dictionary_id": dictionary_id,
        "unit": item.get("unit"),
        "group": item.get("group_name") or item.get("group_id"),
        "max_value_count": item.get("max_value_count"),
        "is_collection": bool(item.get("is_collection")),
        "schema_source": "ozon_seller_api_description_category_attribute",
        "example_value_when_visible": None,
    }
