from ozon_v2.images.visual_design import (
    CURRENT_VISUAL_CONTRACT_VERSION,
    VisualFact,
    VisualSpec,
    validate_visual_set,
    validate_visual_spec,
)


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


def test_fact_and_spec_from_dict_normalize_values() -> None:
    fact = VisualFact.from_dict(
        {"headline": " ОТКРЫТЫЙ НИЗ ", "detail": " кабель ", "evidence_sha256": " a ", "numeric_verified": 1}
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
