from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any
import re
import uuid

from PIL import Image, ImageDraw, ImageFont


CURRENT_VISUAL_CONTRACT_VERSION = "ozon-visual-v1"
MIN_VISUAL_DIMENSION = 320
MOBILE_PREVIEW_DIMENSION = 360
MIN_MOBILE_COPY_PX = 13
SCENE_FIELDS = ("environment", "lighting", "camera", "shot_scale", "buyer_question")
SCENE_SLOTS = (
    "main_01",
    "main_02",
    "detail_01",
    "detail_02",
    "detail_03",
    "detail_04",
    "detail_05",
    "detail_06",
)
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
    "detail_05": SlotVisualContract(("context_caption", "metric_panel"), 2, True),
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
        if not isinstance(value, Mapping):
            raise TypeError("visual fact must be a mapping")
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
        if not isinstance(value, Mapping):
            raise TypeError("visual spec must be a mapping")
        raw_facts = value.get("facts", ())
        if not isinstance(raw_facts, Sequence) or isinstance(
            raw_facts, (str, bytes, bytearray)
        ):
            raise TypeError("facts must be a non-string sequence")
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
    if len(specs) == len(SCENE_SLOTS) and seen_slots == set(SCENE_SLOTS):
        minimum_unique = {
            "environment": 5,
            "lighting": 4,
            "camera": 4,
            "shot_scale": 3,
        }
        for field, minimum in minimum_unique.items():
            values = [spec.scene_signature.get(field, "") for spec in specs]
            if len(set(values)) < minimum:
                errors.append(
                    f"eight-slot set must use at least {minimum_word(minimum)} {field} families"
                )
        environment_counts = Counter(
            spec.scene_signature.get("environment", "") for spec in specs
        )
        if environment_counts and max(environment_counts.values()) > 2:
            errors.append("one environment family may appear in at most two slots")
    return errors


def minimum_word(value: int) -> str:
    return {3: "three", 4: "four", 5: "five"}.get(value, str(value))


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
    short_edge = min(width, height)
    headline_size = max(24, round(short_edge * 0.057))
    detail_size = max(18, round(short_edge * 0.038))
    headline_font = _font(headline_size)
    detail_font = _font(detail_size)
    mobile_scale = min(1.0, MOBILE_PREVIEW_DIMENSION / short_edge)
    minimum_mobile_scale_px = round(detail_size * mobile_scale)
    if spec.facts and minimum_mobile_scale_px < MIN_MOBILE_COPY_PX:
        raise ValueError("visual copy is too small for mobile readability")
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
        "visual_system": "ozon-edge-gradient-b1",
        "visual_design_passed": True,
        "russian_copy_passed": True,
        "safe_area_passed": True,
        "mobile_readability_passed": True,
        "typography": {
            "headline_px": headline_size,
            "detail_px": detail_size,
            "mobile_preview_dimension": MOBILE_PREVIEW_DIMENSION,
            "minimum_mobile_scale_px": minimum_mobile_scale_px,
        },
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
    for line in _wrap(draw, fact.headline, headline_font, max_width):
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


def _measure_fact_height(
    draw: ImageDraw.ImageDraw,
    fact: VisualFact,
    max_width: int,
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
) -> int:
    height = sum(
        _line_height(draw, line, headline_font) + 2
        for line in _wrap(draw, fact.headline, headline_font, max_width)
    )
    height += 3
    height += sum(
        _line_height(draw, line, detail_font) + 2
        for line in _wrap(draw, fact.detail, detail_font, max_width)
    )
    return height + 12


def _draw_rail(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    margin: int,
    spec: VisualSpec,
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
) -> None:
    short_edge = min(width, height)
    _draw_side_gradient(draw, width, height, spec.panel_side, coverage=0.62, max_alpha=224)
    rail_width = min(round(width * 0.46), width - margin * 2)
    panel_padding = max(20, round(short_edge * 0.045))
    fact_gap = max(10, round(short_edge * 0.018))
    content_width = rail_width - panel_padding * 2
    content_height = sum(
        _measure_fact_height(draw, fact, content_width, headline_font, detail_font)
        for fact in spec.facts
    ) + fact_gap * max(0, len(spec.facts) - 1)
    rail_height = content_height + panel_padding * 2
    max_rail_height = min(
        height - margin * 2,
        round(height * (0.62 if len(spec.facts) > 1 else 0.48)),
    )
    if rail_height > max_rail_height:
        raise ValueError("visual copy does not fit its safe area")
    left = margin if spec.panel_side == "left" else width - margin - rail_width
    top = round((height - rail_height) / 2)
    bottom = top + rail_height
    y = top + panel_padding
    for index, fact in enumerate(spec.facts):
        y = _draw_fact(
            draw, fact, left + panel_padding, y, content_width,
            headline_font, detail_font, spec.accent_rgb, bottom - panel_padding,
        )
        if index + 1 < len(spec.facts):
            y += fact_gap


def _draw_caption(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    margin: int,
    spec: VisualSpec,
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
) -> None:
    short_edge = min(width, height)
    panel_padding = max(20, round(short_edge * 0.04))
    fact_gap = max(10, round(short_edge * 0.018))
    content_width = width - margin * 2 - panel_padding * 2
    content_height = sum(
        _measure_fact_height(
            draw,
            fact,
            content_width,
            headline_font,
            detail_font,
        )
        for fact in spec.facts
    ) + fact_gap * max(0, len(spec.facts) - 1)
    bar_height = content_height + panel_padding * 2
    max_height_ratio = 0.44 if len(spec.facts) > 1 else 0.32
    if bar_height > min(height - margin * 2, round(height * max_height_ratio)):
        raise ValueError("visual copy does not fit its safe area")
    top = height - margin - bar_height
    gradient_coverage = max(0.34, (height - top) / height + 0.06)
    _draw_bottom_gradient(draw, width, height, coverage=gradient_coverage, max_alpha=224)
    y = top + panel_padding
    for index, fact in enumerate(spec.facts):
        y = _draw_fact(
            draw,
            fact,
            margin + panel_padding,
            y,
            content_width,
            headline_font,
            detail_font,
            spec.accent_rgb,
            height - margin - panel_padding,
        )
        if index + 1 < len(spec.facts):
            y += fact_gap


def _draw_callouts(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    margin: int,
    spec: VisualSpec,
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
) -> None:
    short_edge = min(width, height)
    box_width = min(round(width * 0.46), width - margin * 2)
    panel_padding = max(18, round(short_edge * 0.035))
    content_width = box_width - panel_padding * 2
    fact_heights = tuple(
        _measure_fact_height(draw, fact, content_width, headline_font, detail_font)
        for fact in spec.facts
    )
    box_height = max(fact_heights) + panel_padding * 2
    marker_radius = max(5, round(short_edge * 0.006))
    available_height = height - margin * 2
    if box_height > round(height * 0.34) or len(spec.facts) * box_height > available_height:
        raise ValueError("visual copy does not fit its safe area")
    if len(spec.facts) == 1:
        lane_tops = (None,)
    else:
        lane_gap = (available_height - len(spec.facts) * box_height) // (len(spec.facts) - 1)
        lane_tops = tuple(margin + index * (box_height + lane_gap) for index in range(len(spec.facts)))

    layouts: list[tuple[VisualFact, int, int, int, int, int]] = []
    for fact, (point_x, point_y), lane_top in zip(spec.facts, spec.callout_points, lane_tops):
        anchor_x = min(max(round(point_x * width), marker_radius), width - 1 - marker_radius)
        anchor_y = min(max(round(point_y * height), marker_radius), height - 1 - marker_radius)
        left = margin if anchor_x > width // 2 else width - margin - box_width
        top = lane_top if lane_top is not None else min(
            max(margin, anchor_y - box_height // 2), height - margin - box_height
        )
        edge_x = left if left > anchor_x else left + box_width
        edge_y = min(max(anchor_y, top + panel_padding), top + box_height - panel_padding)
        layouts.append((fact, anchor_x, anchor_y, left, top, edge_x))

    text_sides = {"left" if left == margin else "right" for _, _, _, left, _, _ in layouts}
    for side in text_sides:
        _draw_side_gradient(draw, width, height, side, coverage=0.56, max_alpha=204)

    for fact, anchor_x, anchor_y, left, top, edge_x in layouts:
        edge_y = min(max(anchor_y, top + panel_padding), top + box_height - panel_padding)
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
        _draw_fact(
            draw, fact, left + panel_padding, top + panel_padding, content_width,
            headline_font, detail_font, spec.accent_rgb, top + box_height - panel_padding,
            headline_fill=spec.accent_rgb, detail_fill=(244, 244, 244),
        )


def _draw_side_gradient(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    side: str,
    *,
    coverage: float,
    max_alpha: int,
) -> None:
    gradient_width = max(1, min(width, round(width * coverage)))
    start_x = 0 if side == "left" else width - gradient_width
    denominator = max(1, gradient_width - 1)
    for offset in range(gradient_width):
        edge_strength = 1.0 - offset / denominator if side == "left" else offset / denominator
        alpha = round(max_alpha * edge_strength**1.7)
        draw.line(
            (start_x + offset, 0, start_x + offset, height - 1),
            fill=(12, 20, 31, alpha),
        )


def _draw_bottom_gradient(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    *,
    coverage: float,
    max_alpha: int,
) -> None:
    gradient_height = max(1, min(height, round(height * coverage)))
    start_y = height - gradient_height
    denominator = max(1, gradient_height - 1)
    for offset in range(gradient_height):
        alpha = round(max_alpha * (offset / denominator) ** 1.7)
        draw.line(
            (0, start_y + offset, width - 1, start_y + offset),
            fill=(12, 20, 31, alpha),
        )


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    return draw.textbbox((0, 0), text, font=font)[2]


def _line_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[3] - box[1]
