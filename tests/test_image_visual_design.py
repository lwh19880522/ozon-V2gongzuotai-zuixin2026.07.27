import os
import sys
from pathlib import Path

import pytest
from PIL import Image

from ozon_v2.images.visual_design import (
    CURRENT_VISUAL_CONTRACT_VERSION,
    VisualFact,
    VisualSpec,
    render_visual,
    validate_visual_set,
    validate_visual_spec,
)
from ozon_v2.images.worker import file_sha256


LOCKED_HASH = "a" * 64


def _scene(**overrides: str) -> dict[str, str]:
    scene = {
        "environment": "warm_bedside",
        "lighting": "warm_morning",
        "camera": "front_three_quarter",
        "shot_scale": "medium",
        "buyer_question": "where_used",
    }
    scene.update(overrides)
    return scene


def _fact(**overrides: object) -> VisualFact:
    values = {
        "headline": "ОТКРЫТЫЙ НИЗ",
        "detail": "кабель проходит свободно",
        "evidence_sha256": LOCKED_HASH,
    }
    values.update(overrides)
    return VisualFact(**values)


def _spec(slot_id: str, recipe: str, **overrides: object) -> VisualSpec:
    values = {
        "contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
        "slot_id": slot_id,
        "recipe": recipe,
        "facts": (),
        "scene_signature": _scene(),
    }
    values.update(overrides)
    return VisualSpec(**values)


def test_main_01_clean_hero_allows_no_copy_but_rejects_facts() -> None:
    clean = _spec("main_01", "clean_hero")
    copied = _spec("main_01", "clean_hero", facts=(_fact(),))

    assert validate_visual_spec(clean, {LOCKED_HASH}) == []
    assert "slot does not allow copy" in validate_visual_spec(copied, {LOCKED_HASH})


def test_detail_05_numeric_copy_requires_verified_number() -> None:
    spec = _spec(
        "detail_05",
        "metric_panel",
        facts=(_fact(detail="кабель проходит свободно 7 см"),),
    )

    assert "numeric copy requires numeric_verified" in validate_visual_spec(spec, {LOCKED_HASH})


@pytest.mark.parametrize(
    "slot_id,recipe,callout_points",
    [
        ("detail_02", "feature_callout", ((0.2, 0.3), (0.8, 0.7))),
        ("detail_03", "feature_callout", ((0.2, 0.3), (0.8, 0.7))),
        ("detail_05", "metric_panel", ()),
        ("detail_06", "context_caption", ()),
    ],
)
def test_selected_detail_slots_allow_two_verified_fact_blocks(
    slot_id: str,
    recipe: str,
    callout_points: tuple[tuple[float, float], ...],
) -> None:
    spec = _spec(
        slot_id,
        recipe,
        facts=(
            _fact(headline="Первая особенность"),
            _fact(headline="Вторая особенность"),
        ),
        callout_points=callout_points,
    )

    assert validate_visual_spec(spec, {LOCKED_HASH}) == []


def test_detail_01_reports_unlocked_and_promotional_copy_together() -> None:
    spec = _spec(
        "detail_01",
        "context_caption",
        facts=(_fact(detail="скидка 50%", evidence_sha256="b" * 64),),
    )

    errors = validate_visual_spec(spec, {LOCKED_HASH})

    assert "copy fact does not reference locked evidence" in errors
    assert "forbidden promotional copy" in errors


def test_scene_slots_need_three_dimensions_of_difference() -> None:
    main_01 = _spec("main_01", "clean_hero")
    main_02 = _spec(
        "main_02",
        "integrated_rail",
        facts=(_fact(),),
        scene_signature=_scene(camera="side", buyer_question="how_used"),
    )

    assert any(
        "differ in fewer than three" in error
        for error in validate_visual_set((main_01, main_02))
    )


def test_scene_diversity_covers_detail_02_and_detail_03_in_a_full_eight_slot_set() -> None:
    recipes = {
        "main_01": "clean_hero",
        "main_02": "integrated_rail",
        "detail_01": "context_caption",
        "detail_02": "feature_callout",
        "detail_03": "metric_panel",
        "detail_04": "context_caption",
        "detail_05": "metric_panel",
        "detail_06": "integrated_rail",
    }
    scene_signatures = {
        slot_id: _scene(
            environment=f"{slot_id}_environment",
            lighting=f"{slot_id}_lighting",
            camera=f"{slot_id}_camera",
            shot_scale=f"{slot_id}_shot_scale",
            buyer_question=f"{slot_id}_buyer_question",
        )
        for slot_id in recipes
    }
    scene_signatures["detail_03"] = _scene(
        environment="detail_02_environment",
        lighting="detail_02_lighting",
        camera="detail_02_camera",
        shot_scale="detail_02_shot_scale",
        buyer_question="detail_03_buyer_question",
    )
    specs = tuple(
        _spec(slot_id, recipe, scene_signature=scene_signatures[slot_id])
        for slot_id, recipe in recipes.items()
    )

    assert validate_visual_set(specs) == [
        "scene slots detail_02 and detail_03 differ in fewer than three dimensions"
    ]


def test_full_eight_slot_set_rejects_one_reused_environment_family() -> None:
    recipes = {
        "main_01": "clean_hero",
        "main_02": "integrated_rail",
        "detail_01": "context_caption",
        "detail_02": "feature_callout",
        "detail_03": "metric_panel",
        "detail_04": "context_caption",
        "detail_05": "metric_panel",
        "detail_06": "integrated_rail",
    }
    specs = tuple(
        _spec(
            slot_id,
            recipe,
            scene_signature=_scene(
                environment="same_indoor_room",
                lighting=f"lighting_{slot_id}",
                camera=f"camera_{slot_id}",
                shot_scale=f"shot_scale_{slot_id}",
                buyer_question=f"question_{slot_id}",
            ),
        )
        for slot_id, recipe in recipes.items()
    )

    assert "eight-slot set must use at least five environment families" in validate_visual_set(specs)


def test_slot_contract_rejects_version_slot_recipe_copy_limit_and_missing_copy() -> None:
    invalid = _spec("unknown", "clean_hero", contract_version="old")
    wrong_recipe = _spec("detail_01", "clean_hero", facts=(_fact(),))
    too_many = _spec("main_02", "integrated_rail", facts=(_fact(), _fact(), _fact()))
    missing_copy = _spec("main_02", "integrated_rail")

    assert "contract_version must be ozon-visual-v1" in validate_visual_spec(invalid, {LOCKED_HASH})
    assert "unknown visual slot" in validate_visual_spec(invalid, {LOCKED_HASH})
    assert "is not allowed" in " ".join(validate_visual_spec(wrong_recipe, {LOCKED_HASH}))
    assert "too many copy fact blocks" in validate_visual_spec(too_many, {LOCKED_HASH})
    assert "slot requires verified copy" in validate_visual_spec(missing_copy, {LOCKED_HASH})


def test_copy_requires_cyrillic_short_non_promotional_text() -> None:
    latin = _spec("detail_01", "context_caption", facts=(_fact(headline="OPEN", detail="cable"),))
    verbose = _spec(
        "detail_01",
        "context_caption",
        facts=(_fact(detail="один два три четыре пять шесть семь восемь девять десять"),),
    )
    promotion = _spec("detail_01", "context_caption", facts=(_fact(headline="ЛУЧШИЙ ВЫБОР"),))

    assert "copy must contain Russian Cyrillic text" in validate_visual_spec(latin, {LOCKED_HASH})
    assert "Russian copy exceeds the short-copy limit" in validate_visual_spec(verbose, {LOCKED_HASH})
    assert "forbidden promotional copy" in validate_visual_spec(promotion, {LOCKED_HASH})


def test_layout_and_callouts_are_bounded() -> None:
    bad_layout = _spec(
        "detail_01",
        "context_caption",
        facts=(_fact(),),
        panel_side="top",
        accent_rgb=(256, 1),
        scene_signature=_scene(environment=""),
    )
    missing_points = _spec("detail_02", "feature_callout", facts=(_fact(),))
    bad_points = _spec(
        "detail_02",
        "feature_callout",
        facts=(_fact(),),
        callout_points=((1.1, 0.5),),
    )

    errors = validate_visual_spec(bad_layout, {LOCKED_HASH})
    assert "panel_side must be left or right" in errors
    assert "accent_rgb must contain three byte values" in errors
    assert "scene_signature is missing environment" in errors
    assert "feature_callout requires one point per fact" in validate_visual_spec(missing_points, {LOCKED_HASH})
    assert "callout points must use normalized coordinates" in validate_visual_spec(bad_points, {LOCKED_HASH})


def test_feature_callout_rejects_extra_points_for_one_fact() -> None:
    spec = _spec(
        "detail_02",
        "feature_callout",
        facts=(_fact(),),
        callout_points=((0.2, 0.8), (0.8, 0.2)),
    )

    assert "feature_callout requires one point per fact" in validate_visual_spec(spec, {LOCKED_HASH})


def test_visual_spec_copies_scene_signature_from_caller() -> None:
    scene = _scene()
    spec = _spec("main_01", "clean_hero", scene_signature=scene)

    scene["environment"] = "kitchen"

    assert spec.scene_signature["environment"] == "warm_bedside"


def test_visual_spec_scene_signature_is_read_only() -> None:
    spec = _spec("main_01", "clean_hero")

    with pytest.raises(TypeError):
        spec.scene_signature["environment"] = "kitchen"


def test_scene_whitespace_cannot_create_fake_dimension_difference() -> None:
    first = _spec("main_01", "clean_hero")
    second = _spec(
        "main_02",
        "integrated_rail",
        facts=(_fact(),),
        scene_signature={key: f" {value} " for key, value in _scene().items()},
    )

    assert any(
        "differ in fewer than three" in error
        for error in validate_visual_set((first, second))
    )


def test_fact_and_spec_from_dict_normalize_values() -> None:
    fact = VisualFact.from_dict(
        {"headline": " ОТКРЫТЫЙ НИЗ ", "detail": " кабель ", "evidence_sha256": " a ", "numeric_verified": True}
    )
    spec = VisualSpec.from_dict(
        {
            "contract_version": " ozon-visual-v1 ",
            "slot_id": " detail_02 ",
            "recipe": " feature_callout ",
            "facts": [fact],
            "scene_signature": _scene(),
            "accent_rgb": [1, 2, 3],
            "callout_points": [[0.2, 0.8]],
        }
    )

    assert fact == VisualFact("ОТКРЫТЫЙ НИЗ", "кабель", "a", True)
    assert spec.facts == (fact,)
    assert spec.accent_rgb == (1, 2, 3)
    assert spec.callout_points == ((0.2, 0.8),)
    assert spec.to_dict()["slot_id"] == "detail_02"


@pytest.mark.parametrize(
    "payload, message",
    [
        ([], "visual spec must be a mapping"),
        (
            {
                "contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
                "slot_id": "main_01",
                "recipe": "clean_hero",
                "facts": [None],
                "scene_signature": _scene(),
            },
            "visual fact must be a mapping",
        ),
        (
            {
                "contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
                "slot_id": "main_01",
                "recipe": "clean_hero",
                "facts": "not-a-sequence",
                "scene_signature": _scene(),
            },
            "facts must be a non-string sequence",
        ),
    ],
)
def test_visual_spec_from_dict_rejects_non_json_object_shapes(
    payload: object, message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        VisualSpec.from_dict(payload)


def test_visual_set_rejects_duplicate_slots_and_buyer_questions() -> None:
    first = _spec("main_01", "clean_hero")
    duplicate_slot = _spec(
        "main_01",
        "clean_hero",
        scene_signature=_scene(
            environment="kitchen",
            lighting="cool_day",
            camera="side",
            shot_scale="wide",
            buyer_question="how_clean",
        ),
    )
    duplicate_question = _spec(
        "detail_01",
        "context_caption",
        facts=(_fact(),),
        scene_signature=_scene(
            environment="kitchen",
            lighting="cool_day",
            camera="side",
            shot_scale="wide",
        ),
    )

    errors = validate_visual_set((first, duplicate_slot, duplicate_question))

    assert "duplicate visual slot main_01" in errors
    assert "duplicate buyer_question where_used" in errors


@pytest.mark.parametrize("value", ["false", 1])
def test_fact_from_dict_rejects_non_boolean_numeric_verified(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        VisualFact.from_dict(
            {
                "headline": "ОТКРЫТЫЙ НИЗ",
                "detail": "кабель проходит свободно",
                "evidence_sha256": LOCKED_HASH,
                "numeric_verified": value,
            }
        )


def test_from_dict_normalizes_none_scene_value_to_empty() -> None:
    spec = VisualSpec.from_dict(
        {
            "contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
            "slot_id": "main_01",
            "recipe": "clean_hero",
            "facts": (),
            "scene_signature": _scene(environment=None),
        }
    )

    assert "scene_signature is missing environment" in validate_visual_spec(spec, {LOCKED_HASH})


def test_from_dict_rejects_non_string_copy_fields() -> None:
    with pytest.raises((TypeError, ValueError)):
        VisualFact.from_dict(
            {"headline": 4, "detail": "кабель", "evidence_sha256": LOCKED_HASH}
        )


def test_from_dict_rejects_coercible_rgb_components() -> None:
    with pytest.raises((TypeError, ValueError)):
        VisualSpec.from_dict(
            {
                "contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
                "slot_id": "main_01",
                "recipe": "clean_hero",
                "facts": (),
                "scene_signature": _scene(),
                "accent_rgb": [True, 2.9, "3"],
            }
        )


@pytest.mark.parametrize("point", [[True, "0.5"], [0.5, float("inf")]])
def test_from_dict_rejects_coercible_or_nonfinite_points(point: list[object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        VisualSpec.from_dict(
            {
                "contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
                "slot_id": "main_01",
                "recipe": "clean_hero",
                "facts": (),
                "scene_signature": _scene(),
                "callout_points": [point],
            }
        )


def test_numeric_copy_needs_literal_true_verification() -> None:
    spec = _spec(
        "detail_05",
        "metric_panel",
        facts=(_fact(detail="кабель проходит свободно 7 см", numeric_verified=1),),
    )

    assert "numeric copy requires numeric_verified" in validate_visual_spec(spec, {LOCKED_HASH})


def test_promotional_word_boundaries_do_not_reject_stopor() -> None:
    spec = _spec(
        "detail_01",
        "context_caption",
        facts=(_fact(headline="НАДЁЖНЫЙ СТОПОР"),),
    )

    assert validate_visual_spec(spec, {LOCKED_HASH}) == []


def test_standalone_top_is_forbidden_promotional_copy() -> None:
    spec = _spec(
        "detail_01",
        "context_caption",
        facts=(_fact(headline="ТОП ВЫБОР"),),
    )

    assert "forbidden promotional copy" in validate_visual_spec(spec, {LOCKED_HASH})


def _render_spec(recipe: str = "integrated_rail", **overrides: object) -> VisualSpec:
    values = {
        "contract_version": CURRENT_VISUAL_CONTRACT_VERSION,
        "slot_id": "main_02",
        "recipe": recipe,
        "facts": (
            VisualFact(
                headline="\u041a\u0420\u0415\u041f\u041b\u0415\u041d\u0418\u0415",
                detail="\u041a\u0430\u0431\u0435\u043b\u044c \u043f\u0440\u043e\u0445\u043e\u0434\u0438\u0442 \u0441\u0432\u043e\u0431\u043e\u0434\u043d\u043e",
                evidence_sha256=LOCKED_HASH,
            ),
        ),
        "scene_signature": _scene(
            environment="kitchen_rail",
            lighting="cool_day",
            camera="side_profile",
            shot_scale="close",
            buyer_question="cable_routing",
        ),
    }
    values.update(overrides)
    return VisualSpec(**values)


def test_render_visual_renders_verified_russian_integrated_rail(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "nested" / "output.png"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)

    receipt = render_visual(source, output, _render_spec(), {LOCKED_HASH})

    assert Image.open(output).size == (900, 1200)
    assert Image.open(output).getpixel((800, 600)) != (205, 192, 180)
    assert receipt["visual_contract_version"] == CURRENT_VISUAL_CONTRACT_VERSION
    assert receipt["layout_recipe"] == "integrated_rail"
    assert receipt["copy_block_count"] == 1
    assert receipt["visual_design_passed"] is True
    assert receipt["russian_copy_passed"] is True
    assert receipt["safe_area_passed"] is True
    assert receipt["mobile_readability_passed"] is True
    assert receipt["visual_system"] == "ozon-edge-gradient-b1"
    assert receipt["visual_source_sha256"] == file_sha256(source)
    assert receipt["visual_output_sha256"] == file_sha256(output)
    assert receipt["visual_source_sha256"] != receipt["visual_output_sha256"]


def test_integrated_rail_uses_mobile_prominent_russian_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (1024, 1024), (205, 192, 180)).save(source)
    captured_sizes: list[tuple[int, int]] = []
    original_draw_rail = visual_design._draw_rail

    def capture_draw_rail(
        draw: object,
        width: int,
        height: int,
        margin: int,
        spec: VisualSpec,
        headline_font: object,
        detail_font: object,
    ) -> None:
        captured_sizes.append((headline_font.size, detail_font.size))
        original_draw_rail(
            draw,
            width,
            height,
            margin,
            spec,
            headline_font,
            detail_font,
        )

    monkeypatch.setattr(visual_design, "_draw_rail", capture_draw_rail)

    receipt = render_visual(source, output, _render_spec(), {LOCKED_HASH})

    assert captured_sizes == [(58, 39)]
    assert receipt["typography"]["headline_px"] == 58
    assert receipt["typography"]["detail_px"] == 39
    assert receipt["typography"]["minimum_mobile_scale_px"] >= 13


def test_integrated_rail_uses_a_soft_edge_gradient_instead_of_a_card(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    source_rgb = (205, 192, 180)
    Image.new("RGB", (1024, 1024), source_rgb).save(source)

    render_visual(source, output, _render_spec(panel_side="right"), {LOCKED_HASH})

    rendered = Image.open(output)
    assert rendered.getpixel((1000, 512)) != source_rgb
    assert rendered.getpixel((320, 512)) == source_rgb
    assert sum(rendered.getpixel((1000, 512))) < sum(rendered.getpixel((760, 512)))


@pytest.mark.parametrize(
    "slot_id,recipe,extra",
    [
        ("main_02", "integrated_rail", {}),
        ("detail_01", "context_caption", {}),
        ("detail_02", "feature_callout", {"callout_points": ((0.25, 0.5),)}),
    ],
)
def test_b_system_never_draws_detached_rounded_text_cards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slot_id: str,
    recipe: str,
    extra: dict[str, object],
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / f"{recipe}.png"
    output = tmp_path / f"{recipe}-output.png"
    Image.new("RGB", (1024, 1024), (205, 192, 180)).save(source)

    def reject_rounded_rectangle(*args: object, **kwargs: object) -> None:
        pytest.fail("B system must not draw a detached rounded text card")

    monkeypatch.setattr(
        visual_design.ImageDraw.ImageDraw,
        "rounded_rectangle",
        reject_rounded_rectangle,
    )

    render_visual(
        source,
        output,
        _render_spec(slot_id=slot_id, recipe=recipe, **extra),
        {LOCKED_HASH},
    )


def test_context_caption_uses_a_bottom_gradient_without_darkening_the_top(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "caption.png"
    source_rgb = (205, 192, 180)
    Image.new("RGB", (1024, 1024), source_rgb).save(source)

    render_visual(
        source,
        output,
        _render_spec(slot_id="detail_01", recipe="context_caption"),
        {LOCKED_HASH},
    )

    rendered = Image.open(output)
    assert rendered.getpixel((512, 120)) == source_rgb
    assert sum(rendered.getpixel((512, 1000))) < sum(source_rgb)


def test_detail_06_context_caption_renders_both_verified_fact_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / "source.png"
    output = tmp_path / "caption.png"
    Image.new("RGB", (1024, 1024), (205, 192, 180)).save(source)
    rendered_headlines: list[str] = []

    def capture_fact(
        draw: object,
        fact: VisualFact,
        x: int,
        y: int,
        max_width: int,
        headline_font: object,
        detail_font: object,
        accent_rgb: object,
        bottom: int,
        **fills: object,
    ) -> int:
        rendered_headlines.append(fact.headline)
        return y + 60

    monkeypatch.setattr(visual_design, "_draw_fact", capture_fact)
    spec = _render_spec(
        slot_id="detail_06",
        recipe="context_caption",
        facts=(
            VisualFact("Первая особенность", "Проверенная деталь", LOCKED_HASH),
            VisualFact("Вторая особенность", "Ещё одна деталь", LOCKED_HASH),
        ),
    )

    render_visual(source, output, spec, {LOCKED_HASH})

    assert rendered_headlines == ["Первая особенность", "Вторая особенность"]


def test_russian_headline_keeps_natural_sentence_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (1024, 1024), (205, 192, 180)).save(source)
    wrapped_values: list[str] = []
    original_wrap = visual_design._wrap

    def capture_wrap(draw: object, value: str, font: object, max_width: int) -> list[str]:
        wrapped_values.append(value)
        return original_wrap(draw, value, font, max_width)

    monkeypatch.setattr(visual_design, "_wrap", capture_wrap)
    spec = _render_spec(
        facts=(
            VisualFact(
                headline="Надёжное крепление",
                detail="Кабель проходит свободно",
                evidence_sha256=LOCKED_HASH,
            ),
        )
    )

    render_visual(source, output, spec, {LOCKED_HASH})

    assert "Надёжное крепление" in wrapped_values
    assert "НАДЁЖНОЕ КРЕПЛЕНИЕ" not in wrapped_values


def test_feature_callout_uses_light_copy_on_its_edge_gradient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / "light-source.png"
    output = tmp_path / "callout.png"
    Image.new("RGB", (900, 1200), (250, 250, 250)).save(source)
    captured_fills: list[tuple[object, object]] = []

    def capture_fact(
        draw: object,
        fact: object,
        x: int,
        y: int,
        max_width: int,
        headline_font: object,
        detail_font: object,
        accent_rgb: object,
        bottom: object = None,
        headline_fill: object = None,
        detail_fill: object = None,
    ) -> int:
        captured_fills.append((headline_fill, detail_fill))
        return y

    monkeypatch.setattr(visual_design, "_draw_fact", capture_fact)

    render_visual(
        source,
        output,
        _render_spec(
            slot_id="detail_02",
            recipe="feature_callout",
            callout_points=((0.2, 0.5),),
        ),
        {LOCKED_HASH},
    )

    assert captured_fills == [((239, 177, 156), (244, 244, 244))]


def test_feature_callout_uses_non_overlapping_boxes_for_same_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / "source.png"
    output = tmp_path / "callout.png"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    captured_boxes: list[tuple[str, int, int, int, int]] = []

    def capture_fact(
        draw: object,
        fact: VisualFact,
        x: int,
        y: int,
        max_width: int,
        headline_font: object,
        detail_font: object,
        accent_rgb: object,
        bottom: int,
        **fills: object,
    ) -> int:
        margin = max(12, round(min(900, 1200) * 0.08))
        captured_boxes.append(
            (fact.headline, x - margin // 2, y - margin // 2, x - margin // 2 + max_width + margin, bottom + margin // 2)
        )
        return y

    monkeypatch.setattr(visual_design, "_draw_fact", capture_fact)
    second_fact = VisualFact("УСТАНОВКА", "Надежно держит форму", LOCKED_HASH)

    render_visual(
        source,
        output,
        _render_spec(
            slot_id="detail_02",
            recipe="feature_callout",
            facts=(_render_spec().facts[0], second_fact),
            callout_points=((0.2, 0.5), (0.2, 0.5)),
        ),
        {LOCKED_HASH},
    )

    assert [box[0] for box in captured_boxes] == ["КРЕПЛЕНИЕ", "УСТАНОВКА"]
    first, second = captured_boxes
    assert first[4] <= second[2] or second[4] <= first[2]


@pytest.mark.parametrize("point", [(0.0, 0.0), (1.0, 1.0)])
def test_feature_callout_keeps_corner_marker_inside_canvas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: tuple[float, float]
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / "source.png"
    output = tmp_path / "callout.png"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    captured_ellipses: list[tuple[int, int, int, int]] = []

    def capture_ellipse(
        draw: object, box: tuple[int, int, int, int], **kwargs: object
    ) -> None:
        captured_ellipses.append(box)

    monkeypatch.setattr(visual_design.ImageDraw.ImageDraw, "ellipse", capture_ellipse)

    render_visual(
        source,
        output,
        _render_spec(
            slot_id="detail_02",
            recipe="feature_callout",
            callout_points=(point,),
        ),
        {LOCKED_HASH},
    )

    left, top, right, bottom = captured_ellipses[0]
    assert 0 <= left <= right < 900
    assert 0 <= top <= bottom < 1200


@pytest.mark.parametrize("panel_side", ["left", "right"])
def test_integrated_rail_keeps_copy_content_inside_safe_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, panel_side: str
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    captured: list[tuple[int, int]] = []

    def capture_fact(
        draw: object, fact: object, x: int, y: int, max_width: int, *fonts: object
    ) -> int:
        captured.append((x, max_width))
        return y

    monkeypatch.setattr(visual_design, "_draw_fact", capture_fact)
    render_visual(source, output, _render_spec(panel_side=panel_side), {LOCKED_HASH})

    margin = max(12, round(min(900, 1200) * 0.08))
    assert captured and captured[0][0] >= margin
    assert captured[0][0] + captured[0][1] <= 900 - margin


def test_render_visual_rejects_missing_source_and_invalid_spec_without_output(tmp_path: Path) -> None:
    output = tmp_path / "output.png"
    with pytest.raises(ValueError, match="visual source image does not exist"):
        render_visual(tmp_path / "missing.png", output, _render_spec(), {LOCKED_HASH})
    assert not output.exists()

    source = tmp_path / "source.png"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    invalid = _render_spec(slot_id="main_01", recipe="clean_hero", facts=(_fact(),))
    with pytest.raises(ValueError, match="invalid visual spec"):
        render_visual(source, output, invalid, {LOCKED_HASH})
    assert not output.exists()


def test_render_visual_clean_hero_preserves_pixels_and_size(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    spec = _spec("main_01", "clean_hero")

    render_visual(source, output, spec, {LOCKED_HASH})

    assert Image.open(output).size == (900, 1200)
    assert Image.open(output).getpixel((850, 600)) == (205, 192, 180)


def test_wrap_splits_a_single_overlong_russian_word_by_character() -> None:
    from ozon_v2.images import visual_design

    draw = visual_design.ImageDraw.Draw(Image.new("RGB", (900, 200)))
    font = visual_design._font(24)
    word = "А" * 200

    lines = visual_design._wrap(draw, word, font, 100)

    assert "".join(lines) == word
    assert len(lines) > 1
    assert all(visual_design._text_width(draw, line, font) <= 100 for line in lines)


def test_render_visual_rejects_long_copy_that_cannot_fit_its_safe_area(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    spec = _render_spec(
        slot_id="detail_02",
        recipe="feature_callout",
        callout_points=((0.2, 0.5),),
        facts=(
            VisualFact(
                headline="КРЕПЛЕНИЕ",
                detail="А" * 200,
                evidence_sha256=LOCKED_HASH,
            ),
        )
    )

    with pytest.raises(ValueError, match="visual copy does not fit its safe area"):
        render_visual(source, output, spec, {LOCKED_HASH})

    assert not output.exists()


def test_render_visual_preserves_existing_output_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ozon_v2.images import visual_design

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    known_good = b"known-good-output"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    output.write_bytes(known_good)

    def fail_replace(source_path: object, target_path: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(visual_design, "os", os, raising=False)
    monkeypatch.setattr(visual_design.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        render_visual(source, output, _render_spec(), {LOCKED_HASH})

    assert output.read_bytes() == known_good
    assert list(tmp_path.glob(".output.png.*.tmp")) == []


@pytest.mark.parametrize("size", [(16, 16), (20, 1000)])
def test_render_visual_rejects_sources_too_small_for_safe_typography(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", size, (205, 192, 180)).save(source)

    with pytest.raises(ValueError, match="visual source image is too small for safe typography"):
        render_visual(source, output, _render_spec(), {LOCKED_HASH})

    assert not output.exists()


def test_render_visual_cli_parses_and_avoids_sqlite_for_local_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import ozon_image_worker

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    spec_path = tmp_path / "visual.json"
    Image.new("RGB", (900, 1200), (205, 192, 180)).save(source)
    spec_path.write_text(__import__("json").dumps(_render_spec().to_dict()), encoding="utf-8")
    args = ozon_image_worker.build_parser().parse_args(
        [
            "--db", "queue.sqlite3", "render-visual", "--source", str(source),
            "--output", str(output), "--spec-json", str(spec_path),
            "--evidence-sha256", LOCKED_HASH,
        ]
    )
    assert args.command == "render-visual"
    assert args.evidence_sha256 == [LOCKED_HASH]
    monkeypatch.setattr(ozon_image_worker, "_queue", lambda args: pytest.fail("SQLite must stay closed"))
    monkeypatch.setattr(sys, "argv", [
        "ozon_image_worker.py", "--db", "queue.sqlite3", "render-visual", "--source", str(source),
        "--output", str(output), "--spec-json", str(spec_path), "--evidence-sha256", LOCKED_HASH,
    ])

    assert ozon_image_worker.main() == 0
    assert output.exists()
