from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class RunStatus(str, Enum):
    CREATED = "created"
    NEEDS_QUERY_GENERATION = "needs_query_generation"
    READY_FOR_OZON_COLLECTION = "ready_for_ozon_collection"
    INGESTED = "ingested"
    FINALIZED = "finalized"
    FAILED = "failed"


class PairStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


class QueryGenerationStatus(str, Enum):
    NEEDS_GENERATION = "needs_generation"
    GENERATED = "generated"
    NEEDS_QUERY_GENERATION = "needs_query_generation"


class DedupeDecisionKind(str, Enum):
    CLEAR = "clear"
    DUPLICATE = "duplicate"
    POSSIBLE_DUPLICATE = "possible_duplicate"


class WorkbenchState(str, Enum):
    CREATED = "created"
    NEEDS_CREDENTIALS = "needs_credentials"
    DEDUPING_STORE = "deduping_store"
    STORE_DEDUPED = "store_deduped"
    SEED_SELECTED = "seed_selected"
    ATTRIBUTE_TEMPLATE_COLLECTING = "attribute_template_collecting"
    ATTRIBUTE_TEMPLATE_COLLECTED = "attribute_template_collected"
    OZON_COLLECTING = "ozon_collecting"
    OZON_COLLECTED = "ozon_collected"
    SUPPLIER_REVIEW = "supplier_review"
    SUPPLIER_COLLECTING = "supplier_collecting"
    SUPPLIER_SEARCHING = "supplier_searching"
    SUPPLIER_COLLECTED = "supplier_collected"
    SAME_PRODUCT_REVIEW = "same_product_review"
    AI_FILLING = "ai_filling"
    IMAGE_PROCESSING = "image_processing"
    DRAFT_BUILDING = "draft_building"
    DRAFT_READY = "draft_ready"
    PUBLISH_WAITING_CONFIRMATION = "publish_waiting_confirmation"
    PUBLISH_SUBMITTED = "publish_submitted"
    DONE = "done"
    NEEDS_SLIDER = "needs_slider"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_BLOCKED = "failed_blocked"


class WorkbenchAction(str, Enum):
    CHECK_CREDENTIALS = "check_credentials"
    START_DEDUPE = "start_dedupe"
    SAVE_CREDENTIALS = "save_credentials"
    MARK_STORE_DEDUPED = "mark_store_deduped"
    SELECT_SEEDS = "select_seeds"
    GENERATE_OZON_QUERIES = "generate_ozon_queries"
    START_ATTRIBUTE_TEMPLATE_COLLECTION = "start_attribute_template_collection"
    MARK_ATTRIBUTE_TEMPLATE_COLLECTED = "mark_attribute_template_collected"
    START_OZON_COLLECTION = "start_ozon_collection"
    MARK_OZON_COLLECTED = "mark_ozon_collected"
    OPEN_SUPPLIER_REVIEW = "open_supplier_review"
    START_SUPPLIER_COLLECTION = "start_supplier_collection"
    START_SUPPLIER_SEARCH = "start_supplier_search"
    MARK_SUPPLIER_COLLECTED = "mark_supplier_collected"
    START_SAME_PRODUCT_REVIEW = "start_same_product_review"
    APPROVE_SAME_PRODUCT = "approve_same_product"
    START_AI_FILL = "start_ai_fill"
    START_IMAGE_PROCESSING = "start_image_processing"
    BUILD_DRAFT = "build_draft"
    MARK_DRAFT_READY = "mark_draft_ready"
    REQUEST_PUBLISH = "request_publish"
    MARK_PUBLISH_SUBMITTED = "mark_publish_submitted"
    MARK_DONE = "mark_done"
    MARK_NEEDS_MANUAL_REVIEW = "mark_needs_manual_review"
    MARK_FAILED_RETRYABLE = "mark_failed_retryable"
    MARK_FAILED_BLOCKED = "mark_failed_blocked"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    return value


@dataclass
class SeedProduct:
    seed_id: str
    title_or_keyword: str
    product_clue: str
    source_language: str = "zh-CN"
    category_hint: str | None = None
    ozon_query_terms_ru: list[str] = field(default_factory=list)
    auxiliary_query_terms_en: list[str] = field(default_factory=list)
    ozon_query_language: str = "ru-RU"
    query_generation_status: QueryGenerationStatus = QueryGenerationStatus.NEEDS_GENERATION
    query_generation_method: str | None = None
    query_generation_confidence: str | None = None
    image_reference_when_available: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SeedProduct":
        data = dict(payload)
        status = data.get("query_generation_status", QueryGenerationStatus.NEEDS_GENERATION)
        data["query_generation_status"] = QueryGenerationStatus(status)
        data.setdefault("product_clue", data.get("title_or_keyword", ""))
        data.setdefault("ozon_query_terms_ru", [])
        data.setdefault("auxiliary_query_terms_en", [])
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class SeedSearchQuery:
    seed_id: str
    source_text_zh: str
    ozon_query_terms_ru: list[str]
    auxiliary_query_terms_en: list[str] = field(default_factory=list)
    negative_terms: list[str] = field(default_factory=list)
    query_generation_method: str = "manual_mapping"
    query_generation_confidence: str = "medium"
    generated_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SeedSearchQuery":
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class ExistingStoreProduct:
    store_product_id: str
    title: str
    normalized_identity_key: str
    offer_id_when_available: str | None = None
    brand: str | None = None
    category_path: str | None = None
    main_image_reference: str | None = None
    selected_sku_or_options_when_available: dict[str, Any] = field(default_factory=dict)
    product_url_when_available: str | None = None
    source_captured_at: str = field(default_factory=utc_now_iso)
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExistingStoreProduct":
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class DedupeDecision:
    kind: DedupeDecisionKind
    reason: str
    matched_product_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def clear(cls, reason: str = "no dedupe match") -> "DedupeDecision":
        return cls(kind=DedupeDecisionKind.CLEAR, reason=reason)

    @classmethod
    def duplicate(cls, reason: str, matched_product_id: str, evidence: dict[str, Any] | None = None) -> "DedupeDecision":
        return cls(
            kind=DedupeDecisionKind.DUPLICATE,
            reason=reason,
            matched_product_id=matched_product_id,
            evidence=evidence or {},
        )

    @classmethod
    def possible_duplicate(cls, reason: str, evidence: dict[str, Any] | None = None) -> "DedupeDecision":
        return cls(kind=DedupeDecisionKind.POSSIBLE_DUPLICATE, reason=reason, evidence=evidence or {})

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class RunEvent:
    run_id: str
    event_type: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunEvent":
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class TargetSku:
    sku_id: str
    selected_options: dict[str, str]
    price: str | None = None
    availability: str | None = None
    image_reference: str | None = None
    seller_sku_or_offer_id_when_visible: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TargetSku":
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class SelectedSkuMedia:
    main_gallery_images: list[str] = field(default_factory=list)
    selected_sku_images: list[str] = field(default_factory=list)
    detail_page_images: list[str] = field(default_factory=list)
    selected_option_label_when_visible: str | None = None
    image_role: str = "unknown"
    style_notes: dict[str, Any] = field(default_factory=dict)
    source_url_or_reference: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "SelectedSkuMedia":
        return cls(**(payload or {}))

    def image_count(self) -> int:
        return len(self.main_gallery_images) + len(self.selected_sku_images) + len(self.detail_page_images)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class OzonCandidate:
    seed_id: str
    seed_title_or_keyword: str
    seed_source_language: str
    ozon_query_terms_ru: list[str]
    ozon_product_id: str
    ozon_url: str
    title: str
    seller_name: str
    target_sku: TargetSku
    slot_id: str | None = None
    candidate_revision: int = 1
    selected_sku_media: SelectedSkuMedia = field(default_factory=SelectedSkuMedia)
    brand: str | None = None
    seller_url: str | None = None
    seller_evidence: dict[str, Any] = field(default_factory=dict)
    category_path: str | None = None
    category_url: str | None = None
    category_id: str | None = None
    category: str | None = None
    subcategory: str | None = None
    leaf_category: str | None = None
    price: str | None = None
    currency: str | None = None
    rating: str | None = None
    review_count: int | None = None
    sales_or_popularity_signal: str | None = None
    delivery_origin: str | None = None
    delivery_time: str | None = None
    fulfillment_label: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    domestic_seller_decision: dict[str, Any] = field(default_factory=dict)
    hot_product_evidence: dict[str, Any] = field(default_factory=dict)
    content_score_evidence: dict[str, Any] = field(default_factory=dict)
    source_captured_at: str = field(default_factory=utc_now_iso)
    collector_notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OzonCandidate":
        data = dict(payload)
        data["target_sku"] = TargetSku.from_dict(data["target_sku"])
        data["selected_sku_media"] = SelectedSkuMedia.from_dict(data.get("selected_sku_media"))
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class MatchedSupplierSku:
    sku_id: str
    selected_options: dict[str, str]
    price: str | None = None
    availability: str | None = None
    image_reference: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MatchedSupplierSku":
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class SupplierMatch:
    supplier_product_id: str
    supplier_url: str
    title: str
    shop_name: str
    matched_supplier_sku: MatchedSupplierSku
    exact_match_decision: dict[str, Any]
    selected_sku_media: SelectedSkuMedia = field(default_factory=SelectedSkuMedia)
    shop_url: str | None = None
    company_name: str | None = None
    price_range: str | None = None
    currency: str | None = None
    moq: str | None = None
    stock_or_availability: str | None = None
    shipping_origin: str | None = None
    domestic_shipping_fee: str | None = None
    domestic_shipping_destination: str | None = None
    domestic_shipping_evidence: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    match_evidence: dict[str, Any] = field(default_factory=dict)
    source_captured_at: str = field(default_factory=utc_now_iso)
    collector_notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SupplierMatch":
        data = dict(payload)
        data["matched_supplier_sku"] = MatchedSupplierSku.from_dict(data["matched_supplier_sku"])
        data["selected_sku_media"] = SelectedSkuMedia.from_dict(data.get("selected_sku_media"))
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass
class CollectionPair:
    pair_id: str
    seed_product: SeedProduct
    ozon_candidate: OzonCandidate
    supplier_match: SupplierMatch
    final_decision: PairStatus
    reasons: list[str]
    evidence_csv_row_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CollectionPair":
        data = dict(payload)
        data["seed_product"] = SeedProduct.from_dict(data["seed_product"])
        data["ozon_candidate"] = OzonCandidate.from_dict(data["ozon_candidate"])
        data["supplier_match"] = SupplierMatch.from_dict(data["supplier_match"])
        data["final_decision"] = PairStatus(data["final_decision"])
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)
