from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
import re
import uuid

from PIL import Image, ImageDraw, ImageFont


CURRENT_VISUAL_CONTRACT_VERSION = "ozon-visual-v1"
MIN_VISUAL_DIMENSION = 320
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
_PROMOTIONAL_WORDS = FORBIDDEN_PROMOTIONAL_COPY[:7]
_PROMOTIONAL_MARKERS = FORBIDDEN_PROMOTIONAL_COPY[7:]
_PROMOTIONAL_WORD_PATTERN = re.compile(
    rf"(?<!\w)(?:{'|'.join(_PROMOTIONAL_WORDS)})(?!\w)"
)


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "headline", _string_value(self.headline, "headline"))
        object.__setattr__(self, "detail", _string_value(self.detail, "detail"))
        object.__setattr__(
            self,
            "evidence_sha256",
            _string_value(self.evidence_sha256, "evidence_sha256"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VisualFact:
        numeric_verified = value.get("numeric_verified", False)
        if numeric_verified is None:
            numeric_verified = False
        if type(numeric_verified) is not bool:
            raise ValueError("numeric_verified must be a boolean")
        return cls(
            headline=_string_value(value.get("headline", ""), "headline"),
            detail=_string_value(value.get("detail", ""), "detail"),
            evidence_sha256=_string_value(
                value.get("evidence_sha256", ""), "evidence_sha256"
            ),
            numeric_verified=numeric_verified,
        )


@dataclass(frozen=True)
class VisualSpec:
    contract_version: str
    slot_id: str
    recipe: str
    facts: tuple[VisualFact, ...]
    scene_signature: Mapping[str, str]
    panel_side: str = "right"
    accent_rgb: tuple[int, int, int] = (239, 177, 156)
    callout_points: tuple[tuple[float, float], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_version",
            _string_value(self.contract_version, "contract_version"),
        )
        object.__setattr__(self, "slot_id", _string_value(self.slot_id, "slot_id"))
        object.__setattr__(self, "recipe", _string_value(self.recipe, "recipe"))
        object.__setattr__(self, "panel_side", _string_value(self.panel_side, "panel_side"))
        object.__setattr__(
            self,
            "scene_signature",
            MappingProxyType(
                {
                    _string_value(key, "scene field"): _string_value(
                        item, "scene_signature value"
                    )
                    for key, item in self.scene_signature.items()
                }
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VisualSpec:
        raw_facts = value.get("facts", ())
        raw_scene = value.get("scene_signature", {})
        if raw_scene is None:
            raw_scene = {}
        if not isinstance(raw_scene, Mapping):
            raise TypeError("scene_signature must be a mapping")
        return cls(
            contract_version=_string_value(
                value.get("contract_version", ""), "contract_version"
            ),
            slot_id=_string_value(value.get("slot_id", ""), "slot_id"),
            recipe=_string_value(value.get("recipe", ""), "recipe"),
            facts=tuple(
                fact if isinstance(fact, VisualFact) else VisualFact.from_dict(fact)
                for fact in raw_facts
            ),
            scene_signature={
                _string_value(key, "scene field"): _string_value(
                    item, "scene_signature value"
                )
                for key, item in raw_scene.items()
            },
            panel_side=_string_value(value.get("panel_side", "right"), "panel_side"),
            accent_rgb=_parse_rgb(value.get("accent_rgb", (239, 177, 156))),
            callout_points=tuple(
                _parse_point(point)
                for point in value.get("callout_points", ())
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "slot_id": self.slot_id,
            "recipe": self.recipe,
            "facts": tuple(
                {
                    "headline": fact.headline,
                    "detail": fact.detail,
                    "evidence_sha256": fact.evidence_sha256,
                    "numeric_verified": fact.numeric_verified,
                }
                for fact in self.facts
            ),
            "scene_signature": dict(self.scene_signature),
            "panel_side": self.panel_side,
            "accent_rgb": self.accent_rgb,
            "callout_points": self.callout_points,
        }


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
    folded_copy = copy.casefold()
    if _PROMOTIONAL_WORD_PATTERN.search(folded_copy) or any(
        marker in folded_copy for marker in _PROMOTIONAL_MARKERS
    ):
        errors.append("forbidden promotional copy")
    if any(character.isdigit() for character in copy) and fact.numeric_verified is not True:
        errors.append("numeric copy requires numeric_verified")


def _string_value(value: object, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value.strip()


def _parse_rgb(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("accent_rgb must be a sequence")
    if any(not isinstance(component, int) or isinstance(component, bool) for component in value):
        raise TypeError("accent_rgb components must be integers")
    return tuple(value)


def _parse_point(value: object) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("callout point must be a sequence")
    if any(
        not isinstance(component, (int, float))
        or isinstance(component, bool)
        or not math.isfinite(component)
        for component in value
    ):
        raise TypeError("callout point components must be finite numbers")
    return tuple(float(component) for component in value)


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


def render_visual(
    source_path: str | Path,
    output_path: str | Path,
    spec: VisualSpec,
    locked_evidence_sha256s: set[str],
) -> dict[str, Any]:
    """Render a validated visual contract with local deterministic typography only."""
    errors = validate_visual_spec(spec, locked_evidence_sha256s)
    if errors:
        raise ValueError(f"invalid visual spec: {'; '.join(errors)}")

    source = Path(source_path)
    output = Path(output_path)
    if not source.is_file():
        raise ValueError("visual source image does not exist")

    with Image.open(source) as image:
        base = image.convert("RGBA")
    width, height = base.size
    if min(width, height) < MIN_VISUAL_DIMENSION:
        raise ValueError("visual source image is too small for safe typography")
    safe_margin = max(12, round(min(width, height) * 0.08))
    headline_font = _font(max(18, round(height * 0.030)))
    detail_font = _font(max(14, round(height * 0.019)))
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if spec.recipe in {"integrated_rail", "metric_panel"}:
        _draw_rail(draw, width, height, safe_margin, spec, headline_font, detail_font)
    elif spec.recipe == "context_caption":
        _draw_caption(draw, width, height, safe_margin, spec, headline_font, detail_font)
    elif spec.recipe == "feature_callout":
        _draw_callouts(draw, width, height, safe_margin, spec, headline_font, detail_font)
    elif spec.recipe != "clean_hero":
        raise ValueError(f"unsupported visual recipe: {spec.recipe}")

    rendered = Image.alpha_composite(base, overlay).convert("RGB")
    encoded = BytesIO()
    rendered.save(encoded, format="PNG")
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(output, encoded.getvalue())
    return {
        "visual_contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
        "layout_recipe": spec.recipe,
        "copy_block_count": len(spec.facts),
        "copy_evidence_sha256s": [fact.evidence_sha256 for fact in spec.facts],
        "scene_signature": dict(spec.scene_signature),
        "visual_spec": spec.to_dict(),
        "visual_design_passed": True,
        "russian_copy_passed": True,
        "safe_area_passed": True,
        "mobile_readability_passed": True,
    }


def _write_atomically(output: Path, content: bytes) -> None:
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _font(size: int) -> ImageFont.FreeTypeFont:
    for font_path in (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/arial.ttf")):
        if font_path.is_file():
            return ImageFont.truetype(font_path, size=size)
    raise RuntimeError("no local Cyrillic-capable font is available")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        proposed = word if not current else f"{current} {word}"
        if current and _text_width(draw, proposed, font) > max_width:
            lines.append(current)
            current = ""
        if not current:
            chunks = _split_word(draw, word, font, max_width)
            lines.extend(chunks[:-1])
            current = chunks[-1]
        else:
            current = proposed
    if current:
        lines.append(current)
    return lines


def _split_word(
    draw: ImageDraw.ImageDraw, word: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for character in word:
        proposed = f"{current}{character}"
        if current and _text_width(draw, proposed, font) > max_width:
            chunks.append(current)
            current = character
        else:
            current = proposed
    if current:
        chunks.append(current)
    return chunks


def _draw_fact(
    draw: ImageDraw.ImageDraw,
    fact: VisualFact,
    x: int,
    y: int,
    max_width: int,
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
    accent_rgb: tuple[int, int, int],
    bottom: int,
    headline_fill: tuple[int, int, int] | None = None,
    detail_fill: tuple[int, int, int] | None = None,
) -> int:
    headline_fill = headline_fill or accent_rgb
    detail_fill = detail_fill or (244, 244, 244)
    measured_lines: list[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int], int]] = []
    cursor = y
    for line in _wrap(draw, fact.headline.upper(), headline_font, max_width):
        measured_lines.append((line, headline_font, headline_fill, cursor))
        cursor += _line_height(draw, line, headline_font) + 2
    cursor += 3
    for line in _wrap(draw, fact.detail, detail_font, max_width):
        measured_lines.append((line, detail_font, detail_fill, cursor))
        cursor += _line_height(draw, line, detail_font) + 2

    if cursor + 12 > bottom or any(
        (box := draw.textbbox((x, line_y), line, font=font))[2] - box[0] > max_width
        or box[3] > bottom
        for line, font, _, line_y in measured_lines
    ):
        raise ValueError("visual copy does not fit its safe area")

    for line, font, fill, line_y in measured_lines:
        draw.text((x, line_y), line, font=font, fill=(*fill, 255))
    return cursor + 12


def _draw_rail(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    margin: int,
    spec: VisualSpec,
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
) -> None:
    rail_width = round(width * 0.36)
    left = 0 if spec.panel_side == "left" else width - rail_width
    top, bottom = margin, height - margin
    radius = max(12, round(min(width, height) * 0.025))
    draw.rounded_rectangle((left, top, left + rail_width, bottom), radius=radius, fill=(20, 31, 43, 190))
    y = top + margin
    for fact in spec.facts:
        y = _draw_fact(
            draw, fact, max(margin, left + margin), y, rail_width - margin * 2,
            headline_font, detail_font, spec.accent_rgb, bottom - margin,
        )


def _draw_caption(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    margin: int,
    spec: VisualSpec,
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
) -> None:
    bar_height = round(height * 0.18)
    top = height - margin - bar_height
    radius = max(12, round(min(width, height) * 0.025))
    draw.rounded_rectangle((margin, top, width - margin, height - margin), radius=radius, fill=(16, 41, 69, 205))
    _draw_fact(
        draw, spec.facts[0], margin * 2, top + margin // 2, width - margin * 4,
        headline_font, detail_font, spec.accent_rgb, height - margin - margin // 2,
    )


def _draw_callouts(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    margin: int,
    spec: VisualSpec,
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
) -> None:
    box_width = round(width * 0.42)
    box_height = max(round(height * 0.16), 100)
    marker_radius = 5
    radius = max(12, round(min(width, height) * 0.02))
    available_height = height - margin * 2
    if len(spec.facts) * box_height > available_height:
        raise ValueError("visual callouts do not fit their safe area")
    if len(spec.facts) == 1:
        lane_tops = (None,)
    else:
        lane_gap = (available_height - len(spec.facts) * box_height) // (len(spec.facts) - 1)
        lane_tops = tuple(margin + index * (box_height + lane_gap) for index in range(len(spec.facts)))

    for fact, (point_x, point_y), lane_top in zip(spec.facts, spec.callout_points, lane_tops):
        anchor_x = min(max(round(point_x * width), marker_radius), width - marker_radius)
        anchor_y = min(max(round(point_y * height), marker_radius), height - marker_radius)
        left = margin if anchor_x > width // 2 else width - margin - box_width
        top = lane_top if lane_top is not None else min(
            max(margin, anchor_y - box_height // 2), height - margin - box_height
        )
        edge_x = left if left > anchor_x else left + box_width
        edge_y = min(max(anchor_y, top + radius), top + box_height - radius)
        draw.line((anchor_x, anchor_y, edge_x, edge_y), fill=(*spec.accent_rgb, 255), width=max(2, round(width * 0.004)))
        draw.ellipse(
            (
                anchor_x - marker_radius,
                anchor_y - marker_radius,
                anchor_x + marker_radius,
                anchor_y + marker_radius,
            ),
            fill=(*spec.accent_rgb, 255),
        )
        draw.rounded_rectangle((left, top, left + box_width, top + box_height), radius=radius, fill=(250, 250, 250, 232))
        _draw_fact(
            draw, fact, left + margin // 2, top + margin // 2, box_width - margin,
            headline_font, detail_font, spec.accent_rgb, top + box_height - margin // 2,
            headline_fill=(25, 32, 51), detail_fill=(70, 75, 85),
        )


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    return draw.textbbox((0, 0), text, font=font)[2]


def _line_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[3] - box[1]
