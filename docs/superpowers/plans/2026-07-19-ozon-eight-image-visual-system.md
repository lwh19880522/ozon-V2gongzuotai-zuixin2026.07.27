# Ozon V2 Eight-Image Commerce Visual System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Ozon V2 from generic scene generation and basic copy placement to a verified eight-slot commerce storyboard with deterministic local Russian infographic layouts, without adding mandatory image-generation calls.

**Architecture:** Add one focused `visual_design` module that owns slot roles, evidence-bound Russian copy validation, scene-diversity rules, and Pillow-based local rendering. Keep queue ownership, grid cropping, repair limits, dynamic zero-to-five image subagents, and the sixth reserve slot unchanged. New work uses prompt version `ozon-image-v3`; existing `ozon-image-v2` receipts remain readable so unfinished historical jobs are not corrupted.

**Tech Stack:** Python 3.11, Pillow, dataclasses, SQLite queue receipts, pytest, Markdown skill contracts.

---

## File Map

- Create `src/ozon_v2/images/visual_design.py`: eight-slot visual contracts, Russian fact validation, scene diversity, safe local rendering.
- Create `tests/test_image_visual_design.py`: unit tests for contracts, copy evidence, diversity and renderer output.
- Modify `src/ozon_v2/images/worker.py`: version-aware receipt requirements for `ozon-image-v2` and `ozon-image-v3`.
- Modify `src/ozon_v2/images/queue.py`: validate v3 visual specs against locked supplier evidence and validate the eight-image set before review.
- Modify `scripts/ozon_image_worker.py`: add deterministic `render-visual` command.
- Modify `tests/test_ozon_image_worker.py`: receipt and queue integration coverage.
- Modify `tests/test_image_worker_contract_files.py`: enforce the four-call speed contract and v3 skill wording.
- Modify `skills/ozon-product-media-generator/SKILL.md`: operational v3 workflow and eight-slot storyboard.
- Modify `skills/ozon-product-media-generator/references/prompt-contract.md`: evidence, copy, layout and acceptance contract.
- Modify `skills/ozon-product-media-generator/assets/main-grid-prompt.txt`: distinct `main_01` and `main_02` roles.
- Create `skills/ozon-product-media-generator/assets/detail-grid-a-prompt.txt`: `detail_01` through `detail_03` roles.
- Create `skills/ozon-product-media-generator/assets/detail-grid-b-prompt.txt`: `detail_04` through `detail_06` roles.
- Modify `skills/ozon-product-media-generator/assets/repair-slot-prompt.txt`: preserve the failed slot's storyboard role and copy zone.
- Stop using `skills/ozon-product-media-generator/assets/detail-grid-prompt.txt` from the active skill; leave the file untouched until all active references are removed and tests prove it is unused.

## Task 1: Define the Eight-Slot Visual Contract

**Files:**
- Create: `src/ozon_v2/images/visual_design.py`
- Create: `tests/test_image_visual_design.py`

- [ ] **Step 1: Write failing contract tests**

Create `tests/test_image_visual_design.py` with these first tests:

```python
from __future__ import annotations

from dataclasses import replace

from ozon_v2.images.visual_design import (
    CURRENT_VISUAL_CONTRACT_VERSION,
    VisualFact,
    VisualSpec,
    validate_visual_set,
    validate_visual_spec,
)


EVIDENCE = "a" * 64


def _scene(**overrides: str) -> dict[str, str]:
    value = {
        "environment": "warm_bedside",
        "lighting": "warm_morning",
        "camera": "front_three_quarter",
        "shot_scale": "medium",
        "buyer_question": "where_used",
    }
    value.update(overrides)
    return value


def _fact(headline: str = "ОТКРЫТЫЙ НИЗ", detail: str = "кабель проходит свободно") -> VisualFact:
    return VisualFact(
        headline=headline,
        detail=detail,
        evidence_sha256=EVIDENCE,
        numeric_verified=False,
    )


def test_main_01_is_clean_and_rejects_copy() -> None:
    clean = VisualSpec(
        contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
        slot_id="main_01",
        recipe="clean_hero",
        facts=(),
        scene_signature=_scene(buyer_question="product_recognition"),
    )
    assert validate_visual_spec(clean, {EVIDENCE}) == []
    assert "does not allow copy" in " ".join(
        validate_visual_spec(replace(clean, facts=(_fact(),)), {EVIDENCE})
    )


def test_numeric_copy_requires_verified_numeric_evidence() -> None:
    spec = VisualSpec(
        contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
        slot_id="detail_05",
        recipe="metric_panel",
        facts=(_fact("ДЛЯ ТЕЛЕФОНА 7 ДЮЙМОВ", "проверенный размер"),),
        scene_signature=_scene(buyer_question="size_or_fit"),
    )
    assert "numeric copy requires numeric_verified" in " ".join(
        validate_visual_spec(spec, {EVIDENCE})
    )


def test_copy_must_reference_locked_evidence_and_avoid_promotion() -> None:
    spec = VisualSpec(
        contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
        slot_id="detail_01",
        recipe="context_caption",
        facts=(replace(_fact("ЛУЧШИЙ ХИТ", "скидка 50%"), evidence_sha256="b" * 64),),
        scene_signature=_scene(),
    )
    errors = " ".join(validate_visual_spec(spec, {EVIDENCE}))
    assert "locked evidence" in errors
    assert "forbidden promotional copy" in errors


def test_scene_slots_must_differ_in_at_least_three_dimensions() -> None:
    first = VisualSpec(
        CURRENT_VISUAL_CONTRACT_VERSION,
        "main_01",
        "clean_hero",
        (),
        _scene(buyer_question="product_recognition"),
    )
    second = VisualSpec(
        CURRENT_VISUAL_CONTRACT_VERSION,
        "main_02",
        "integrated_rail",
        (_fact(),),
        _scene(camera="side", buyer_question="value_overview"),
    )
    assert "differ in fewer than three" in " ".join(validate_visual_set((first, second)))
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run:

```powershell
python -m pytest tests/test_image_visual_design.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'ozon_v2.images.visual_design'`.

- [ ] **Step 3: Implement the contract model and validators**

Create `src/ozon_v2/images/visual_design.py` with this public surface and behavior:

```python
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any, Iterable, Mapping


CURRENT_VISUAL_CONTRACT_VERSION = "ozon-visual-v1"
SCENE_FIELDS = ("environment", "lighting", "camera", "shot_scale", "buyer_question")
SCENE_SLOTS = frozenset(("main_01", "main_02", "detail_01", "detail_04", "detail_05", "detail_06"))
FORBIDDEN_COPY = ("лучший", "хит", "топ", "акция", "скидка", "распродажа", "купить", "%", "₽", "http")


@dataclass(frozen=True)
class SlotVisualContract:
    allowed_recipes: tuple[str, ...]
    max_fact_blocks: int
    copy_required: bool


SLOT_VISUAL_CONTRACTS: dict[str, SlotVisualContract] = {
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
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisualFact":
        return cls(
            headline=str(payload.get("headline") or "").strip(),
            detail=str(payload.get("detail") or "").strip(),
            evidence_sha256=str(payload.get("evidence_sha256") or "").strip(),
            numeric_verified=bool(payload.get("numeric_verified")),
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
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisualSpec":
        return cls(
            contract_version=str(payload.get("contract_version") or ""),
            slot_id=str(payload.get("slot_id") or ""),
            recipe=str(payload.get("recipe") or ""),
            facts=tuple(VisualFact.from_dict(item) for item in payload.get("facts") or ()),
            scene_signature={str(k): str(v) for k, v in dict(payload.get("scene_signature") or {}).items()},
            panel_side=str(payload.get("panel_side") or "right"),
            accent_rgb=tuple(int(v) for v in payload.get("accent_rgb") or (239, 177, 156)),
            callout_points=tuple(tuple(float(v) for v in point) for point in payload.get("callout_points") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _words(value: str) -> list[str]:
    return [word for word in re.split(r"\s+", value.strip()) if word]


def validate_visual_spec(spec: VisualSpec, locked_evidence_sha256s: Iterable[str]) -> list[str]:
    errors: list[str] = []
    contract = SLOT_VISUAL_CONTRACTS.get(spec.slot_id)
    locked = set(locked_evidence_sha256s)
    if spec.contract_version != CURRENT_VISUAL_CONTRACT_VERSION:
        errors.append(f"contract_version must be {CURRENT_VISUAL_CONTRACT_VERSION}")
    if contract is None:
        return errors + ["unknown visual slot"]
    if spec.recipe not in contract.allowed_recipes:
        errors.append(f"recipe {spec.recipe} is not allowed for {spec.slot_id}")
    if len(spec.facts) > contract.max_fact_blocks:
        errors.append("too many copy fact blocks")
    if contract.copy_required and not spec.facts:
        errors.append("slot requires verified copy")
    if contract.max_fact_blocks == 0 and spec.facts:
        errors.append("slot does not allow copy")
    if spec.panel_side not in {"left", "right"}:
        errors.append("panel_side must be left or right")
    if len(spec.accent_rgb) != 3 or any(value < 0 or value > 255 for value in spec.accent_rgb):
        errors.append("accent_rgb must contain three byte values")
    missing_scene = [field for field in SCENE_FIELDS if not spec.scene_signature.get(field, "").strip()]
    if missing_scene:
        errors.append("scene_signature is missing: " + ", ".join(missing_scene))
    for fact in spec.facts:
        combined = f"{fact.headline} {fact.detail}".strip()
        if fact.evidence_sha256 not in locked:
            errors.append("copy fact does not reference locked evidence")
        if not re.search(r"[А-Яа-яЁё]", combined):
            errors.append("copy must contain Russian Cyrillic text")
        if len(_words(fact.headline)) > 5 or len(_words(fact.detail)) > 9:
            errors.append("Russian copy exceeds the short-copy limit")
        if any(token in combined.casefold() for token in FORBIDDEN_COPY):
            errors.append("forbidden promotional copy")
        if any(character.isdigit() for character in combined) and not fact.numeric_verified:
            errors.append("numeric copy requires numeric_verified")
    if spec.recipe == "feature_callout" and len(spec.callout_points) < len(spec.facts):
        errors.append("feature_callout requires one point per fact")
    if any(not (0 <= value <= 1) for point in spec.callout_points for value in point):
        errors.append("callout points must use normalized coordinates")
    return errors


def validate_visual_set(specs: Iterable[VisualSpec]) -> list[str]:
    items = tuple(specs)
    errors: list[str] = []
    slot_ids = [item.slot_id for item in items]
    if len(slot_ids) != len(set(slot_ids)):
        errors.append("visual set contains duplicate slot ids")
    questions = [item.scene_signature.get("buyer_question", "").casefold() for item in items]
    if len(questions) != len(set(questions)):
        errors.append("visual set contains duplicate buyer questions")
    scene_items = [item for item in items if item.slot_id in SCENE_SLOTS]
    for first, second in combinations(scene_items, 2):
        difference_count = sum(
            first.scene_signature.get(field) != second.scene_signature.get(field)
            for field in SCENE_FIELDS
        )
        if difference_count < 3:
            errors.append(
                f"scene slots {first.slot_id} and {second.slot_id} differ in fewer than three dimensions"
            )
    return errors
```

- [ ] **Step 4: Run the contract tests**

Run:

```powershell
python -m pytest tests/test_image_visual_design.py -q
```

Expected: all four tests pass.

- [ ] **Step 5: Commit the contract model**

```powershell
git add src/ozon_v2/images/visual_design.py tests/test_image_visual_design.py
git commit -m "feat: define Ozon eight-image visual contract"
```

## Task 2: Add Deterministic Local Russian Rendering

**Files:**
- Modify: `src/ozon_v2/images/visual_design.py`
- Modify: `tests/test_image_visual_design.py`
- Modify: `scripts/ozon_image_worker.py`

- [ ] **Step 1: Write failing renderer and CLI tests**

Append tests that create a `900×1200` fixture and verify that rendering preserves dimensions, changes only through local drawing, and emits validation metadata:

```python
from pathlib import Path

from PIL import Image

from ozon_v2.images.visual_design import render_visual
from scripts.ozon_image_worker import build_parser


def test_integrated_rail_renders_locally_without_changing_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    spec = VisualSpec(
        CURRENT_VISUAL_CONTRACT_VERSION,
        "main_02",
        "integrated_rail",
        (_fact(),),
        _scene(
            environment="cool_office",
            lighting="cool_daylight",
            camera="side",
            shot_scale="wide",
            buyer_question="value_overview",
        ),
    )

    fragment = render_visual(source, output, spec, {EVIDENCE})

    assert Image.open(output).size == (900, 1200)
    assert Image.open(output).getpixel((850, 600)) != (205, 192, 180)
    assert fragment["visual_contract_version"] == CURRENT_VISUAL_CONTRACT_VERSION
    assert fragment["layout_recipe"] == "integrated_rail"
    assert fragment["copy_block_count"] == 1
    assert fragment["visual_design_passed"] is True
    assert fragment["russian_copy_passed"] is True
    assert fragment["safe_area_passed"] is True
    assert fragment["mobile_readability_passed"] is True


def test_render_visual_command_is_available() -> None:
    args = build_parser().parse_args(
        [
            "--db", "queue.sqlite3", "render-visual",
            "--source", "source.png", "--output", "output.png",
            "--spec-json", "visual.json", "--evidence-sha256", EVIDENCE,
        ]
    )
    assert args.command == "render-visual"
    assert args.evidence_sha256 == [EVIDENCE]
```

- [ ] **Step 2: Run the renderer tests and verify failure**

Run:

```powershell
python -m pytest tests/test_image_visual_design.py -q
```

Expected: import or assertion failure because `render_visual` and `render-visual` do not exist.

- [ ] **Step 3: Implement the renderer**

Add to `visual_design.py`:

```python
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ):
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    raise ValueError("no local Cyrillic-capable font is available")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = word if not current else f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "\n".join(lines)


def _draw_fact(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    fact: VisualFact,
    width: int,
    accent: tuple[int, int, int],
    headline_font: ImageFont.FreeTypeFont,
    detail_font: ImageFont.FreeTypeFont,
) -> int:
    x, y = xy
    headline = _wrap(draw, fact.headline.upper(), headline_font, width)
    detail = _wrap(draw, fact.detail, detail_font, width)
    draw.multiline_text((x, y), headline, font=headline_font, fill=accent, spacing=4)
    headline_box = draw.multiline_textbbox((x, y), headline, font=headline_font, spacing=4)
    detail_y = headline_box[3] + 8
    draw.multiline_text((x, detail_y), detail, font=detail_font, fill=(245, 238, 234), spacing=3)
    detail_box = draw.multiline_textbbox((x, detail_y), detail, font=detail_font, spacing=3)
    return detail_box[3]


def render_visual(
    source_path: str | Path,
    output_path: str | Path,
    spec: VisualSpec,
    locked_evidence_sha256s: Iterable[str],
) -> dict[str, Any]:
    errors = validate_visual_spec(spec, locked_evidence_sha256s)
    if errors:
        raise ValueError("invalid visual spec: " + "; ".join(errors))
    source = Path(source_path)
    output = Path(output_path)
    if not source.is_file():
        raise ValueError("visual source image does not exist")
    with Image.open(source) as raw:
        image = raw.convert("RGBA")
    width, height = image.size
    safe = max(12, round(min(width, height) * 0.08))
    accent = tuple(spec.accent_rgb)
    headline_font = _font(max(18, round(height * 0.030)))
    detail_font = _font(max(14, round(height * 0.019)))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if spec.recipe in {"integrated_rail", "metric_panel"}:
        rail_width = round(width * 0.36)
        left = safe if spec.panel_side == "left" else width - safe - rail_width
        right = left + rail_width
        draw.rounded_rectangle((left, safe, right, height - safe), radius=24, fill=(25, 17, 15, 220))
        y = safe + 34
        for fact in spec.facts:
            y = _draw_fact(draw, (left + 24, y), fact, rail_width - 48, accent, headline_font, detail_font) + 28
    elif spec.recipe == "context_caption":
        top = height - safe - round(height * 0.18)
        draw.rounded_rectangle((safe, top, width - safe, height - safe), radius=22, fill=(17, 27, 43, 215))
        _draw_fact(draw, (safe + 24, top + 20), spec.facts[0], width - 2 * safe - 48, accent, headline_font, detail_font)
    elif spec.recipe == "feature_callout":
        box_width = round(width * 0.42)
        for index, fact in enumerate(spec.facts):
            px = round(spec.callout_points[index][0] * width)
            py = round(spec.callout_points[index][1] * height)
            box_left = safe if px > width // 2 else width - safe - box_width
            box_top = safe + index * round(height * 0.18)
            box_bottom = box_top + round(height * 0.14)
            draw.line((px, py, box_left, box_top + 28), fill=accent + (255,), width=max(2, width // 450))
            draw.rounded_rectangle((box_left, box_top, box_left + box_width, box_bottom), radius=20, fill=(255, 255, 255, 235))
            draw.text((box_left + 20, box_top + 18), fact.headline.upper(), font=detail_font, fill=(25, 32, 51))
    elif spec.recipe != "clean_hero":
        raise ValueError("unsupported visual recipe")

    rendered = Image.alpha_composite(image, overlay).convert("RGB")
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered.save(output, format="PNG")
    return {
        "visual_contract_version": spec.contract_version,
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
```

- [ ] **Step 4: Add the CLI command**

Import `VisualSpec` and `render_visual` in `scripts/ozon_image_worker.py`, add this parser block, then handle it without opening the queue:

```python
    render = commands.add_parser("render-visual")
    render.add_argument("--source", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--spec-json", required=True)
    render.add_argument("--evidence-sha256", action="append", required=True)
```

```python
    elif args.command == "render-visual":
        spec = VisualSpec.from_dict(json.loads(Path(args.spec_json).read_text(encoding="utf-8")))
        _print(render_visual(args.source, args.output, spec, set(args.evidence_sha256)))
```

Move `queue = _queue(args)` into only the branches that need SQLite so `render-visual` remains a pure local file operation.

- [ ] **Step 5: Run tests and commit**

```powershell
python -m pytest tests/test_image_visual_design.py -q
git add src/ozon_v2/images/visual_design.py scripts/ozon_image_worker.py tests/test_image_visual_design.py
git commit -m "feat: render verified Russian image layouts locally"
```

Expected: all visual-design tests pass; no image-generation tool is called.

## Task 3: Bind Visual Metadata to Receipts and Final Set Review

**Files:**
- Modify: `src/ozon_v2/images/worker.py`
- Modify: `src/ozon_v2/images/queue.py`
- Modify: `tests/test_ozon_image_worker.py`

- [ ] **Step 1: Write failing v2 compatibility and v3 enforcement tests**

Extend `_slot_receipt` in `tests/test_ozon_image_worker.py` with `prompt_version: str = "ozon-image-v2"` and pass that variable to `SlotResultReceipt.create`. Import `VisualFact`, `VisualSpec`, and `CURRENT_VISUAL_CONTRACT_VERSION`, then add this helper and tests:

```python
def _v3_validation(slot_id: str, evidence_sha256: str, *, repeated_scene: bool = False) -> dict:
    recipes = {
        "main_01": "clean_hero",
        "main_02": "integrated_rail",
        "detail_01": "context_caption",
        "detail_02": "feature_callout",
        "detail_03": "feature_callout",
        "detail_04": "context_caption",
        "detail_05": "context_caption",
        "detail_06": "integrated_rail",
    }
    facts = () if slot_id == "main_01" else (
        VisualFact("ОТКРЫТЫЙ НИЗ", "кабель проходит свободно", evidence_sha256),
    )
    scene = {
        "environment": "same_room" if repeated_scene else f"room_{slot_id}",
        "lighting": "same_light" if repeated_scene else f"light_{slot_id}",
        "camera": "same_camera" if repeated_scene else f"camera_{slot_id}",
        "shot_scale": "same_scale" if repeated_scene else f"scale_{slot_id}",
        "buyer_question": slot_id,
    }
    spec = VisualSpec(
        CURRENT_VISUAL_CONTRACT_VERSION,
        slot_id,
        recipes[slot_id],
        facts,
        scene,
        callout_points=((0.5, 0.5),) if recipes[slot_id] == "feature_callout" else (),
    )
    return {
        "product_truth": True,
        "slot_role": slot_id,
        "slot_role_satisfied": True,
        "role_visually_demonstrated": True,
        "not_plain_or_near_white_product_only": True,
        "distinct_from_accepted_slots": True,
        "copy_not_used_as_visual_evidence": True,
        "visual_contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
        "visual_design_passed": True,
        "russian_copy_passed": True,
        "safe_area_passed": True,
        "mobile_readability_passed": True,
        "visual_spec": spec.to_dict(),
    }


def test_v2_receipt_remains_readable_but_v3_requires_visual_metadata(tmp_path: Path) -> None:
    selection = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=selection, subject_master=_master(tmp_path, selection))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "grid.png"
    output = tmp_path / "slot.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    legacy_receipt = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
        prompt_version="ozon-image-v2",
    )
    assert legacy_receipt.acceptance_contract_errors() == []

    upgraded = replace(legacy_receipt, prompt_version="ozon-image-v3")
    errors = " ".join(upgraded.acceptance_contract_errors())
    assert "visual_design_passed" in errors
    assert "russian_copy_passed" in errors
    assert "safe_area_passed" in errors
    assert "mobile_readability_passed" in errors
    assert "visual_spec is required" in errors


def test_v3_copy_fact_must_reference_locked_supplier_evidence(tmp_path: Path) -> None:
    selection = _receipt()
    master = _master(tmp_path, selection)
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=selection, subject_master=master)
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[1]
    source = tmp_path / "grid.png"
    output = tmp_path / "slot.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    invalid = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
        prompt_version="ozon-image-v3",
        validation=_v3_validation(slot["slot_id"], "f" * 64),
    )
    with pytest.raises(ValueError, match="locked supplier evidence"):
        queue.record_slot_result(invalid)


def test_v3_final_set_rejects_repeated_scene_signatures(tmp_path: Path) -> None:
    selection = _receipt()
    master = _master(tmp_path, selection)
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=selection, subject_master=master)
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    source = tmp_path / "grid.png"
    _solid_grid(source, ["red", "green", "blue"])
    for index, slot in enumerate(queue.list_slots(job["job_id"])):
        output = tmp_path / f"slot-{index}.png"
        Image.new("RGB", (120, 90), "red").save(output)
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=source,
                output_path=output,
                accepted=True,
                prompt_version="ozon-image-v3",
                validation=_v3_validation(
                    slot["slot_id"], master.source_sha256s[0], repeated_scene=True
                ),
            )
        )
    with pytest.raises(ValueError, match="visual set validation"):
        queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])
```

- [ ] **Step 2: Run the focused tests and verify failure**

```powershell
python -m pytest tests/test_ozon_image_worker.py -q
```

Expected: new tests fail because v3 metadata is not required or checked.

- [ ] **Step 3: Make receipt validation version-aware**

In `worker.py` keep the current six base flags for v2, set `CURRENT_PROMPT_VERSION = "ozon-image-v3"`, and add:

```python
LEGACY_PROMPT_VERSION = "ozon-image-v2"
V3_VISUAL_FLAGS = (
    "visual_design_passed",
    "russian_copy_passed",
    "safe_area_passed",
    "mobile_readability_passed",
)
```

Change `acceptance_contract_errors` so it accepts only v2 or v3. For v2, preserve the existing behavior. For v3, require the three new flags, require `validation["visual_spec"]` to be a dictionary, and require `validation["visual_contract_version"] == "ozon-visual-v1"`. Unknown prompt versions remain errors.

- [ ] **Step 4: Validate v3 against the locked job and the full set**

In `queue.record_slot_result`, perform the existing hash and lease checks first, then load:

```python
subject_master = SubjectMasterSelection.from_dict(json.loads(job["subject_master_json"]))
```

For an accepted v3 receipt, parse `VisualSpec.from_dict(receipt.validation["visual_spec"])`, require `spec.slot_id == receipt.slot_id`, and call:

```python
visual_errors = validate_visual_spec(spec, subject_master.source_sha256s)
if visual_errors:
    raise ValueError("accepted slot failed locked supplier evidence validation: " + "; ".join(visual_errors))
```

In `mark_ready_for_review`, collect receipt prompt versions. Reject mixed v2/v3 sets. When all receipts are v3, parse all eight `visual_spec` payloads and call `validate_visual_set`. Raise `ValueError("visual set validation failed: ...")` on any error before releasing the worker lease.

- [ ] **Step 5: Run worker and queue tests**

```powershell
python -m pytest tests/test_ozon_image_worker.py tests/test_image_generation_queue.py -q
```

Expected: all tests pass, including historical v2 receipt coverage.

- [ ] **Step 6: Commit receipt integration**

```powershell
git add src/ozon_v2/images/worker.py src/ozon_v2/images/queue.py tests/test_ozon_image_worker.py
git commit -m "feat: enforce Ozon visual receipts and set diversity"
```

## Task 4: Upgrade the Active Skill and Prompt Assets to v3

**Required sub-skill:** `superpowers:writing-skills`

**Files:**
- Modify: `tests/test_image_worker_contract_files.py`
- Modify: `skills/ozon-product-media-generator/SKILL.md`
- Modify: `skills/ozon-product-media-generator/references/prompt-contract.md`
- Modify: `skills/ozon-product-media-generator/assets/main-grid-prompt.txt`
- Create: `skills/ozon-product-media-generator/assets/detail-grid-a-prompt.txt`
- Create: `skills/ozon-product-media-generator/assets/detail-grid-b-prompt.txt`
- Modify: `skills/ozon-product-media-generator/assets/repair-slot-prompt.txt`

- [ ] **Step 1: Write failing contract-file assertions**

Extend `test_product_media_contract_keeps_white_anchor_out_of_finished_slots` and add a speed/storyboard test:

```python
def test_product_media_v3_has_fixed_storyboard_and_four_call_first_pass() -> None:
    skill_root = ROOT / "skills" / "ozon-product-media-generator"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    main = (skill_root / "assets" / "main-grid-prompt.txt").read_text(encoding="utf-8")
    detail_a = (skill_root / "assets" / "detail-grid-a-prompt.txt").read_text(encoding="utf-8")
    detail_b = (skill_root / "assets" / "detail-grid-b-prompt.txt").read_text(encoding="utf-8")
    repair = (skill_root / "assets" / "repair-slot-prompt.txt").read_text(encoding="utf-8")
    contract = (skill_root / "references" / "prompt-contract.md").read_text(encoding="utf-8")

    assert "ozon-image-v3" in "\n".join((skill, main, detail_a, detail_b, repair, contract))
    assert "exactly four mandatory image-generation calls" in skill
    assert "one white subject" in skill
    assert "one 1x2 main grid" in skill
    assert "two 1x3 supporting grids" in skill
    assert "main_01" in main and "clean hero" in main
    assert "main_02" in main and "integrated information rail" in main
    for slot_id in ("detail_01", "detail_02", "detail_03"):
        assert slot_id in detail_a
    for slot_id in ("detail_04", "detail_05", "detail_06"):
        assert slot_id in detail_b
    assert "render-visual" in skill
    assert "do not call imagegen for typography" in skill
    assert "copy failure repairs only the local text layer" in contract
```

- [ ] **Step 2: Run and verify the new contract test fails**

```powershell
python -m pytest tests/test_image_worker_contract_files.py -q
```

Expected: failure because v3 assets and wording are absent.

- [ ] **Step 3: Update the active worker skill**

Keep the current claim/lease/stop/resume workflow. Replace the generic image sequence with these exact v3 rules:

```markdown
- First pass uses exactly four mandatory image-generation calls: one white subject, one 1x2 main grid, and two 1x3 supporting grids.
- `main_01` is a text-free clean hero in a real evidence-safe scene.
- `main_02` is a different scene with a reserved integrated information rail for at most two verified facts.
- `detail_01` proves primary use context; `detail_02` proves one functional structure; `detail_03` proves a second structure or verified metric.
- `detail_04` uses a materially different second lifestyle scene; `detail_05` proves scale, fit, or a third evidence-safe context; `detail_06` closes one remaining buyer question.
- Run `render-visual` after crop and conservative upscale. Merge its returned validation fragment into the slot receipt.
- Typography, icons, panels, overflow repair, and safe-area repair are local operations; do not call imagegen for typography.
```

Point the skill to separate `detail-grid-a-prompt.txt` and `detail-grid-b-prompt.txt`. Keep `main_01` free of copy, require v3 for new attempts, and preserve v2 receipts only as historical compatibility.

- [ ] **Step 4: Write the four prompt assets and reference contract**

Set `assets/main-grid-prompt.txt` to this complete text:

```text
Create one horizontal 1x2 ecommerce grid for the exact locked product sales unit. Use the generated white-background subject only as an identity and geometry anchor, and use every locked supplier evidence image as final product truth. Panel 1 is main_01: a text-free clean hero in the most attractive evidence-safe real scene, with the complete product dominant, naturally integrated, unobstructed, and immediately recognizable at mobile thumbnail size. Panel 2 is main_02: a materially different evidence-safe scene that proves the product's value overview and leaves one natural dark or uncluttered side zone for a later local integrated information rail containing at most two verified facts. The panels must differ in environment, lighting, camera, shot scale, and buyer question rather than merely changing angle or background color. Preserve exact quantity, set composition, color, silhouette, proportions, structure, parts, accessories, print, and package contents. Do not render text, numbers, logos, badges, watermarks, prices, discounts, extra products, invented parts, duplicate subjects, cutout edges, collage borders, or panel labels. Keep a precise equal vertical split. Use prompt version ozon-image-v3 and visual contract ozon-visual-v1.
```

Create `assets/detail-grid-a-prompt.txt` with:

```text
Create one horizontal 1x3 supporting-image grid for the exact locked product sales unit. Use the generated white-background subject only as an identity and geometry anchor, and use every locked supplier evidence image as final product truth. Panel 1 is detail_01: visibly prove the primary use location or use process in a realistic evidence-safe scene and leave a compact caption zone. Panel 2 is detail_02: visibly prove one primary functional structure with a meaningful close or medium-close view and clean space for one or two local callouts. Panel 3 is detail_03: visibly prove a second structure, detail, material, or verified metric with a different angle and shot scale; if the evidence contains no reliable metric or material, use another visible structural fact instead of inventing one. The three panels must differ materially in environment, lighting, camera, shot scale, and buyer question. Preserve exact quantity, set composition, color, silhouette, proportions, structure, parts, accessories, print, and package contents. Do not render text, numbers, logos, badges, watermarks, prices, discounts, extra products, invented claims, invented parts, duplicate subjects, cutout edges, collage borders, or panel labels. Keep precise equal vertical splits. Use prompt version ozon-image-v3 and visual contract ozon-visual-v1.
```

Create `assets/detail-grid-b-prompt.txt` with:

```text
Create one horizontal 1x3 supporting-image grid for the exact locked product sales unit. Use the generated white-background subject only as an identity and geometry anchor, and use every locked supplier evidence image as final product truth. Panel 1 is detail_04: a second lifestyle scene whose environment, lighting, camera, shot scale, and buyer question materially differ from every accepted image, with one compact local-caption zone. Panel 2 is detail_05: visibly prove scale, fit, compatibility, or small-space value only when the locked evidence supports it; otherwise use a third evidence-safe context or structural fact and never invent a number. Panel 3 is detail_06: close one remaining buyer question with verified packaging, accessories, installation, structure, or a third context; if those facts are absent, use a verified structure or context rather than fabricated content. Preserve exact quantity, set composition, color, silhouette, proportions, structure, parts, accessories, print, and package contents. Do not render text, numbers, logos, badges, watermarks, prices, discounts, extra products, invented claims, invented parts, duplicate subjects, cutout edges, collage borders, or panel labels. Keep precise equal vertical splits. Use prompt version ozon-image-v3 and visual contract ozon-visual-v1.
```

Replace `assets/repair-slot-prompt.txt` with:

```text
Create one replacement ecommerce image for only the explicitly named failed slot and the exact locked product sales unit. Use the generated white-background subject only as an identity and geometry anchor, use every locked supplier evidence image as final product truth, and preserve the failed slot's buyer question and allowed local layout recipe from ozon-visual-v1. Make the pixels visibly prove that one role and make the new environment, lighting, camera, shot scale, and purpose materially distinct from all accepted slots. Preserve exact quantity, set composition, color, silhouette, proportions, structure, parts, accessories, print, and package contents. Reserve only the copy zone required by the slot recipe. Do not repair typography with image generation. Do not render text, numbers, logos, badges, watermarks, prices, discounts, extra products, invented claims, invented parts, duplicate subjects, cutout edges, or a collage. Use prompt version ozon-image-v3 and visual contract ozon-visual-v1.
```

Add this complete v3 section to `references/prompt-contract.md` and remove any conflicting statement that both main images default to no copy:

```markdown
## Visual contract v3

- Prompt version is `ozon-image-v3`; visual contract is `ozon-visual-v1`.
- First pass is exactly four mandatory image-generation calls: one white subject, one 1x2 main grid, and two 1x3 supporting grids.
- `main_01` uses `clean_hero` and has zero copy.
- `main_02` uses `integrated_rail` with at most two verified Russian fact blocks.
- Supporting slots use only the recipe allowed by the eight-slot contract.
- Every Russian fact block cites one locked supplier evidence SHA-256 value.
- Digits are forbidden unless that fact records `numeric_verified=true`.
- The image model never renders Russian copy, numbers, icons, badges, logos, prices or discounts.
- Run the local `render-visual` command after crop and conservative upscale, then merge its returned validation fragment into the slot receipt.
- Copy failure repairs only the local text layer and never consumes an image-generation attempt.
- A scene slot differs from every other scene slot in at least three of environment, lighting, camera, shot scale and buyer question.
- Accepted v3 receipts contain `visual_design_passed`, `russian_copy_passed`, `safe_area_passed`, `mobile_readability_passed`, and the complete `visual_spec`.
```

- [ ] **Step 5: Run skill contract tests and commit**

```powershell
python -m pytest tests/test_image_worker_contract_files.py tests/test_ozon_image_controller_skill.py -q
git add tests/test_image_worker_contract_files.py skills/ozon-product-media-generator
git commit -m "feat: upgrade Ozon image skill to visual contract v3"
```

Expected: all contract tests pass; the dynamic worker pool assertions remain unchanged.

## Task 5: Verify the Complete Change Without Real Image Generation

**Files:**
- Verify only; do not modify production files unless a failing test identifies a scoped defect.

- [ ] **Step 1: Run the focused visual and image-worker suite**

```powershell
python -m pytest tests/test_image_visual_design.py tests/test_ozon_image_worker.py tests/test_image_generation_queue.py tests/test_image_worker_contract_files.py tests/test_ozon_image_controller_skill.py -q
```

Expected: PASS. The run uses only Pillow fixtures, SQLite temporary databases and contract text; it must not invoke `imagegen`.

- [ ] **Step 2: Run related workbench regressions**

```powershell
python -m pytest tests/test_workbench_local_server.py tests/test_workbench_control.py tests/test_workbench_runtime_control.py -q
```

Expected: PASS. Dynamic zero-to-five subagent wording and the sixth reserve position remain intact.

- [ ] **Step 3: Run the full Python suite**

```powershell
python -m pytest -q
```

Expected: all tests pass; no browser, store API, upload, or real image generation is started.

- [ ] **Step 4: Inspect the final diff**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only the planned Ozon V2 image-contract, local-rendering, test and documentation files are changed by this feature.
