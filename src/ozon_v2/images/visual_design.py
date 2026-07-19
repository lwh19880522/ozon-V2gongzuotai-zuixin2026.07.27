from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping
import re


CURRENT_VISUAL_CONTRACT_VERSION = "ozon-visual-v1"
SCENE_FIELDS = ("environment", "lighting", "camera", "shot_scale", "buyer_question")
SCENE_SLOTS = ("main_01", "main_02", "detail_01", "detail_04", "detail_05", "detail_06")
FORBIDDEN_PROMOTIONAL_COPY = (
    "лучший",
    "хит",
    "топ",
    "акция",
    "скидка",
    "распродажа",
    "купить",
    "%",
    "₽",
    "http",
)
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@dataclass(frozen=True)
class SlotVisualContract:
    allowed_recipes: tuple[str, ...]
    max_fact_blocks: int
    copy_required: bool


SLOT_VISUAL_CONTRACTS = {
    "main_01": SlotVisualContract(("clean_hero",), 0, False),
    "main_02": SlotVisualContract(("integrated_rail",), 2, True),
    "detail_01": SlotVisualContract(("context_caption",), 1, True),
    "detail_02": SlotVisualContract(("feature_callout",), 2, True),
    "detail_03": SlotVisualContract(("feature_callout", "metric_panel"), 2, True),
    "detail_04": SlotVisualContract(("context_caption",), 1, True),
    "detail_05": SlotVisualContract(("context_caption", "metric_panel"), 1, True),
    "detail_06": SlotVisualContract(("integrated_rail", "context_caption"), 2, True),
}


@dataclass(frozen=True)
class VisualFact:
    headline: str
    detail: str
    evidence_sha256: str
    numeric_verified: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VisualFact:
        return cls(
            headline=str(value.get("headline", "")).strip(),
            detail=str(value.get("detail", "")).strip(),
            evidence_sha256=str(value.get("evidence_sha256", "")).strip(),
            numeric_verified=bool(value.get("numeric_verified", False)),
        )


@dataclass(frozen=True)
class VisualSpec:
    contract_version: str
    slot_id: str
    recipe: str
    facts: tuple[VisualFact, ...]
    scene_signature: dict[str, str]
    panel_side: str = "right"
    accent_rgb: tuple[int, int, int] = (239, 177, 156)
    callout_points: tuple[tuple[float, float], ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VisualSpec:
        raw_facts = value.get("facts", ())
        raw_scene = value.get("scene_signature", {})
        return cls(
            contract_version=str(value.get("contract_version", "")).strip(),
            slot_id=str(value.get("slot_id", "")).strip(),
            recipe=str(value.get("recipe", "")).strip(),
            facts=tuple(
                fact if isinstance(fact, VisualFact) else VisualFact.from_dict(fact)
                for fact in raw_facts
            ),
            scene_signature={str(key).strip(): str(item).strip() for key, item in raw_scene.items()},
            panel_side=str(value.get("panel_side", "right")).strip(),
            accent_rgb=tuple(int(component) for component in value.get("accent_rgb", (239, 177, 156))),
            callout_points=tuple(
                tuple(float(component) for component in point)
                for point in value.get("callout_points", ())
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_visual_spec(
    spec: VisualSpec, locked_evidence_sha256s: set[str]
) -> list[str]:
    errors: list[str] = []
    if spec.contract_version != CURRENT_VISUAL_CONTRACT_VERSION:
        errors.append(f"contract_version must be {CURRENT_VISUAL_CONTRACT_VERSION}")

    contract = SLOT_VISUAL_CONTRACTS.get(spec.slot_id)
    if contract is None:
        errors.append("unknown visual slot")
    else:
        if spec.recipe not in contract.allowed_recipes:
            errors.append(f"recipe {spec.recipe} is not allowed for slot {spec.slot_id}")
        if len(spec.facts) > contract.max_fact_blocks:
            errors.append("too many copy fact blocks")
        if contract.copy_required and not spec.facts:
            errors.append("slot requires verified copy")
        if spec.slot_id == "main_01" and spec.facts:
            errors.append("slot does not allow copy")

    if spec.panel_side not in {"left", "right"}:
        errors.append("panel_side must be left or right")
    if not _is_byte_rgb(spec.accent_rgb):
        errors.append("accent_rgb must contain three byte values")
    for field in SCENE_FIELDS:
        if not spec.scene_signature.get(field, "").strip():
            errors.append(f"scene_signature is missing {field}")

    for fact in spec.facts:
        _validate_fact(fact, locked_evidence_sha256s, errors)

    if spec.recipe == "feature_callout" and len(spec.callout_points) != len(spec.facts):
        errors.append("feature_callout requires one point per fact")
    if any(not _is_normalized_point(point) for point in spec.callout_points):
        errors.append("callout points must use normalized coordinates")
    return errors


def _is_byte_rgb(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 3
        and all(isinstance(component, int) and not isinstance(component, bool) and 0 <= component <= 255 for component in value)
    )


def _validate_fact(
    fact: VisualFact, locked_evidence_sha256s: set[str], errors: list[str]
) -> None:
    copy = f"{fact.headline} {fact.detail}".strip()
    if fact.evidence_sha256 not in locked_evidence_sha256s:
        errors.append("copy fact does not reference locked evidence")
    if not _CYRILLIC.search(copy):
        errors.append("copy must contain Russian Cyrillic text")
    if len(fact.headline.split()) > 5 or len(fact.detail.split()) > 9:
        errors.append("Russian copy exceeds the short-copy limit")
    if any(token in copy.casefold() for token in FORBIDDEN_PROMOTIONAL_COPY):
        errors.append("forbidden promotional copy")
    if any(character.isdigit() for character in copy) and not fact.numeric_verified:
        errors.append("numeric copy requires numeric_verified")


def _is_normalized_point(point: object) -> bool:
    if not isinstance(point, tuple) or len(point) != 2:
        return False
    return all(
        isinstance(component, (int, float))
        and not isinstance(component, bool)
        and 0 <= component <= 1
        for component in point
    )


def validate_visual_set(specs: tuple[VisualSpec, ...]) -> list[str]:
    errors: list[str] = []
    seen_slots: set[str] = set()
    seen_questions: set[str] = set()
    for spec in specs:
        if spec.slot_id in seen_slots:
            errors.append(f"duplicate visual slot {spec.slot_id}")
        seen_slots.add(spec.slot_id)
        question = spec.scene_signature.get("buyer_question", "")
        if question in seen_questions:
            errors.append(f"duplicate buyer_question {question}")
        seen_questions.add(question)

    for index, left in enumerate(specs):
        for right in specs[index + 1 :]:
            if left.slot_id in SCENE_SLOTS and right.slot_id in SCENE_SLOTS:
                differences = sum(
                    left.scene_signature.get(field) != right.scene_signature.get(field)
                    for field in SCENE_FIELDS
                )
                if differences < 3:
                    errors.append(
                        f"scene slots {left.slot_id} and {right.slot_id} differ in fewer than three dimensions"
                    )
    return errors
