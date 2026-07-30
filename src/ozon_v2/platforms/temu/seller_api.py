from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


TEMU_API_URL = "https://openapi-b-global.temu.com/openapi/router"
TEMU_PUBLISH_METHOD = "temu.local.goods.v3.add"
TEMU_STATUS_METHOD = "temu.local.goods.list.retrieve"
TEMU_STATUS_RECOMMENDED_DELAY_SECONDS = 600


class TemuSellerApiError(RuntimeError):
    pass


class ConfirmationRequired(TemuSellerApiError):
    pass


class TemuIdempotencyConflict(TemuSellerApiError):
    pass


@dataclass(frozen=True, repr=False)
class TemuCredentials:
    store_id: str
    app_key: str
    access_token: str

    def __repr__(self) -> str:
        return (
            f"TemuCredentials(store_id={self.store_id!r}, "
            "app_key='***', access_token='***')"
        )


class TemuSignatureProvider(Protocol):
    def sign(self, payload: Mapping[str, Any]) -> str:
        """Return the approved Partner Platform signature for payload."""


def load_temu_credentials(path: str | Path) -> TemuCredentials:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemuSellerApiError(
            "Temu credentials could not be read from local configuration."
        ) from exc
    if not isinstance(raw, dict):
        raise TemuSellerApiError("Temu credentials configuration must be a JSON object.")
    values = {
        field: str(raw.get(field) or "").strip()
        for field in ("store_id", "app_key", "access_token")
    }
    missing = [field for field, value in values.items() if not value]
    if missing:
        raise TemuSellerApiError(
            f"Temu credentials configuration is missing: {', '.join(missing)}."
        )
    return TemuCredentials(**values)


_IDEMPOTENCY_LOCK = threading.RLock()


class TemuIdempotencyRegistry:
    """Reserve merchant identifiers before a live submission is attempted."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def reserve(
        self,
        *,
        store_id: str,
        external_goods_id: str,
        external_sku_ids: list[str],
    ) -> str:
        normalized_store = str(store_id or "").strip()
        normalized_goods = str(external_goods_id or "").strip()
        normalized_skus = sorted(
            {str(value or "").strip() for value in external_sku_ids}
        )
        if (
            not normalized_store
            or not normalized_goods
            or not normalized_skus
            or "" in normalized_skus
        ):
            raise TemuSellerApiError(
                "Store, product, and SKU identifiers are required for idempotency."
            )

        operation_key = derive_idempotency_key(
            store_id=normalized_store,
            external_goods_id=normalized_goods,
            external_sku_ids=normalized_skus,
        )
        with _IDEMPOTENCY_LOCK:
            payload = self._read()
            for entry in payload["entries"]:
                if entry["store_id"] != normalized_store:
                    continue
                if (
                    entry["external_goods_id"] == normalized_goods
                    or set(entry["external_sku_ids"]) & set(normalized_skus)
                ):
                    raise TemuIdempotencyConflict(
                        "Temu external product or SKU identifier is already "
                        "reserved for this store."
                    )
            payload["entries"].append(
                {
                    "operation_key": operation_key,
                    "store_id": normalized_store,
                    "external_goods_id": normalized_goods,
                    "external_sku_ids": normalized_skus,
                }
            )
            self._write(payload)
        return operation_key

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "entries": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TemuSellerApiError(
                "Temu idempotency registry could not be read."
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("entries"), list)
        ):
            raise TemuSellerApiError("Temu idempotency registry has an invalid shape.")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def derive_idempotency_key(
    *,
    store_id: str,
    external_goods_id: str,
    external_sku_ids: list[str],
) -> str:
    canonical = json.dumps(
        {
            "store_id": str(store_id).strip(),
            "external_goods_id": str(external_goods_id).strip(),
            "external_sku_ids": sorted(
                str(value).strip() for value in external_sku_ids
            ),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_product_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise TemuSellerApiError("Temu product request must be an object.")
    product = deepcopy(dict(request))
    _reject_unknown_fields(
        product,
        {"language", "goodsBasic", "attributes", "skuList"},
        "product request",
    )

    goods = _require_mapping(product, "goodsBasic", "product request")
    _reject_unknown_fields(
        goods,
        {
            "externalGoodsId",
            "goodsName",
            "extCatName",
            "goodsDesc",
            "goodsCarouselImage",
            "detailImage",
            "productType",
            "shipmentLimitDay",
        },
        "goodsBasic",
    )
    _required_text(goods, "externalGoodsId", maximum=128, context="goodsBasic")
    _required_text(goods, "goodsName", maximum=500, context="goodsBasic")
    _optional_text(goods, "extCatName", maximum=500, context="goodsBasic")
    _optional_text(goods, "goodsDesc", maximum=10_000, context="goodsBasic")
    _optional_images(goods, "goodsCarouselImage", maximum=10, context="goodsBasic")
    _optional_images(goods, "detailImage", maximum=50, context="goodsBasic")
    if "productType" in goods and goods["productType"] not in (1, 2, 3, 4):
        raise TemuSellerApiError(
            "goodsBasic.productType must be one of 1, 2, 3, or 4."
        )

    attributes = product.get("attributes", [])
    if not isinstance(attributes, list) or len(attributes) > 200:
        raise TemuSellerApiError("attributes must be an array with at most 200 items.")
    for index, attribute in enumerate(attributes):
        context = f"attributes[{index}]"
        if not isinstance(attribute, dict):
            raise TemuSellerApiError(f"{context} must be an object.")
        _reject_unknown_fields(attribute, {"name", "value"}, context)
        _required_text(attribute, "name", maximum=128, context=context)
        values = attribute.get("value")
        if not isinstance(values, list) or len(values) > 1_000:
            raise TemuSellerApiError(
                f"{context}.value must be an array with at most 1000 items."
            )
        for value_index, value in enumerate(values):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise TemuSellerApiError(
                    f"{context}.value[{value_index}] must be non-empty "
                    "and at most 128 characters."
                )

    sku_list = product.get("skuList")
    if not isinstance(sku_list, list) or not sku_list or len(sku_list) > 500:
        raise TemuSellerApiError("skuList must contain between 1 and 500 items.")
    seen_skus: set[str] = set()
    for index, sku in enumerate(sku_list):
        if not isinstance(sku, dict):
            raise TemuSellerApiError(f"skuList[{index}] must be an object.")
        _validate_sku(sku, index)
        external_sku_id = str(sku["externalSkuId"]).strip()
        if external_sku_id in seen_skus:
            raise TemuSellerApiError(
                "externalSkuId values must be unique within a product request."
            )
        seen_skus.add(external_sku_id)
    return product


def _validate_sku(sku: dict[str, Any], index: int) -> None:
    context = f"skuList[{index}]"
    _reject_unknown_fields(
        sku,
        {
            "externalSkuId",
            "images",
            "price",
            "variations",
            "quantity",
            "packageInfo",
            "barCode",
            "references",
        },
        context,
    )
    _required_text(sku, "externalSkuId", maximum=128, context=context)
    images = sku.get("images")
    if not isinstance(images, list) or not images or len(images) > 10:
        raise TemuSellerApiError(
            f"{context}.images must contain between 1 and 10 URLs."
        )
    _validate_image_urls(images, f"{context}.images")

    price = _require_mapping(sku, "price", context)
    _reject_unknown_fields(price, {"basePrice", "listPrice"}, f"{context}.price")
    _validate_money(
        _require_mapping(price, "basePrice", f"{context}.price"),
        f"{context}.price.basePrice",
    )
    if "listPrice" in price:
        _validate_money(
            _require_mapping(price, "listPrice", f"{context}.price"),
            f"{context}.price.listPrice",
        )

    if "quantity" not in sku:
        raise TemuSellerApiError(f"{context}.quantity is required.")

    package = _require_mapping(sku, "packageInfo", context)
    _reject_unknown_fields(
        package,
        {"weight", "length", "width", "height"},
        f"{context}.packageInfo",
    )
    for field in ("weight", "length", "width", "height"):
        value = package.get(field)
        if (
            not isinstance(value, str)
            or not value.strip()
            or not _is_numeric_string(value)
        ):
            raise TemuSellerApiError(
                f"{context}.packageInfo.{field} must be a numeric string "
                "without a unit."
            )

    variations = sku.get("variations")
    if not isinstance(variations, list) or not variations or len(variations) > 5:
        raise TemuSellerApiError(
            f"{context}.variations must contain between 1 and 5 items."
        )
    for variation_index, variation in enumerate(variations):
        variation_context = f"{context}.variations[{variation_index}]"
        if not isinstance(variation, dict):
            raise TemuSellerApiError(f"{variation_context} must be an object.")
        _reject_unknown_fields(variation, {"name", "value"}, variation_context)
        _required_text(variation, "name", maximum=128, context=variation_context)
        _required_text(variation, "value", maximum=128, context=variation_context)


def _validate_money(value: dict[str, Any], context: str) -> None:
    _reject_unknown_fields(value, {"amount", "currency"}, context)
    _required_text(value, "amount", maximum=None, context=context)
    _required_text(value, "currency", maximum=None, context=context)


def _required_text(
    value: Mapping[str, Any],
    field: str,
    *,
    maximum: int | None,
    context: str,
) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise TemuSellerApiError(f"{context}.{field} is required.")
    if maximum is not None and len(raw) > maximum:
        raise TemuSellerApiError(f"{context}.{field} exceeds {maximum} characters.")
    return raw.strip()


def _optional_text(
    value: Mapping[str, Any],
    field: str,
    *,
    maximum: int,
    context: str,
) -> None:
    if field not in value:
        return
    raw = value[field]
    if not isinstance(raw, str) or len(raw) > maximum:
        raise TemuSellerApiError(
            f"{context}.{field} must be a string of at most {maximum} characters."
        )


def _optional_images(
    value: Mapping[str, Any],
    field: str,
    *,
    maximum: int,
    context: str,
) -> None:
    if field not in value:
        return
    images = value[field]
    if not isinstance(images, list) or len(images) > maximum:
        raise TemuSellerApiError(
            f"{context}.{field} must contain at most {maximum} URLs."
        )
    _validate_image_urls(images, f"{context}.{field}")


def _validate_image_urls(images: list[Any], context: str) -> None:
    for index, image in enumerate(images):
        if not isinstance(image, str) or not image.strip().startswith("https://"):
            raise TemuSellerApiError(
                f"{context}[{index}] must be a public HTTPS URL."
            )


def _require_mapping(
    value: Mapping[str, Any],
    field: str,
    context: str,
) -> dict[str, Any]:
    nested = value.get(field)
    if not isinstance(nested, dict):
        raise TemuSellerApiError(
            f"{context}.{field} is required and must be an object."
        )
    return nested


def _reject_unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    context: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise TemuSellerApiError(
            f"{context} contains unsupported fields: {', '.join(unknown)}."
        )


def _is_numeric_string(value: str) -> bool:
    try:
        return float(value) >= 0
    except ValueError:
        return False


class TemuSellerApi:
    def __init__(
        self,
        *,
        credentials_path: str | Path,
        signature_provider: TemuSignatureProvider | None,
        idempotency_registry: TemuIdempotencyRegistry,
        base_url: str = TEMU_API_URL,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.credentials_path = Path(credentials_path)
        self.signature_provider = signature_provider
        self.idempotency_registry = idempotency_registry
        self.base_url = base_url
        self._opener = opener
        self._clock = clock

    def preview_publish(self, request: Mapping[str, Any]) -> dict[str, Any]:
        product = validate_product_request(request)
        goods = product["goodsBasic"]
        sku_ids = [
            str(sku["externalSkuId"]).strip() for sku in product["skuList"]
        ]
        return {
            "method": TEMU_PUBLISH_METHOD,
            "request": product,
            "idempotency_key": derive_idempotency_key(
                store_id="preview",
                external_goods_id=str(goods["externalGoodsId"]).strip(),
                external_sku_ids=sku_ids,
            ),
            "requires_explicit_confirmation": True,
        }

    def publish_product(
        self,
        request: Mapping[str, Any],
        *,
        explicit_confirmation: bool,
    ) -> dict[str, Any]:
        product = validate_product_request(request)
        if not explicit_confirmation:
            raise ConfirmationRequired(
                "Temu live publication requires explicit confirmation."
            )

        credentials = load_temu_credentials(self.credentials_path)
        envelope = self._signed_envelope(
            method=TEMU_PUBLISH_METHOD,
            request=product,
            credentials=credentials,
        )
        goods = product["goodsBasic"]
        sku_ids = [
            str(sku["externalSkuId"]).strip() for sku in product["skuList"]
        ]
        operation_key = self.idempotency_registry.reserve(
            store_id=credentials.store_id,
            external_goods_id=str(goods["externalGoodsId"]).strip(),
            external_sku_ids=sku_ids,
        )
        response = self._post_json(envelope)
        result = response.get("result")
        if not isinstance(result, dict) or not result.get("goodsId"):
            raise TemuSellerApiError(
                "Temu product creation response did not contain goodsId."
            )
        return {
            "goodsId": result["goodsId"],
            "externalGoodsId": result.get("externalGoodsId"),
            "operation_key": operation_key,
            "publication_confirmed": False,
            "status_check_after_seconds": TEMU_STATUS_RECOMMENDED_DELAY_SECONDS,
        }

    def query_product_status(self, goods_id: int | str) -> dict[str, Any]:
        normalized_goods_id = str(goods_id or "").strip()
        if not normalized_goods_id:
            raise TemuSellerApiError("Temu goodsId is required for status query.")
        credentials = load_temu_credentials(self.credentials_path)
        envelope = self._signed_envelope(
            method=TEMU_STATUS_METHOD,
            request={"goodsIdList": [normalized_goods_id]},
            credentials=credentials,
        )
        response = self._post_json(envelope)
        result = response.get("result")
        if not isinstance(result, dict):
            raise TemuSellerApiError(
                "Temu product status response has an invalid shape."
            )
        return result

    def _signed_envelope(
        self,
        *,
        method: str,
        request: dict[str, Any],
        credentials: TemuCredentials,
    ) -> dict[str, Any]:
        if self.signature_provider is None:
            raise TemuSellerApiError(
                "An approved Temu signature provider is required before "
                "any network request."
            )
        envelope: dict[str, Any] = {
            "type": method,
            "app_key": credentials.app_key,
            "access_token": credentials.access_token,
            "timestamp": int(self._clock()),
            "request": request,
        }
        signature = str(
            self.signature_provider.sign(deepcopy(envelope)) or ""
        ).strip()
        if not signature:
            raise TemuSellerApiError(
                "The approved Temu signature provider returned an empty signature."
            )
        envelope["sign"] = signature
        return envelope

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TemuSellerApiError(
                f"Temu Partner API returned HTTP {exc.code}."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TemuSellerApiError("Temu Partner API request failed.") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TemuSellerApiError(
                "Temu Partner API returned invalid JSON."
            ) from exc
        if not isinstance(result, dict):
            raise TemuSellerApiError("Temu Partner API response must be an object.")
        if result.get("success") is not True:
            code = result.get("errorCode")
            message = _redact_api_message(
                str(result.get("errorMsg") or "request rejected"),
                payload,
            )
            raise TemuSellerApiError(
                f"Temu Partner API error {code}: {message[:200]}"
            )
        return result


def _redact_api_message(message: str, payload: Mapping[str, Any]) -> str:
    redacted = message
    for field in ("app_key", "access_token", "sign"):
        value = str(payload.get(field) or "")
        if value:
            redacted = redacted.replace(value, "***")
    return redacted
