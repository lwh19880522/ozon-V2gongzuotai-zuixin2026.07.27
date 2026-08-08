from __future__ import annotations

from ozon_v2.domain.seed_subject import (
    build_seed_subject_contract,
    evaluate_subject_text,
)


def test_handball_score_record_seed_rejects_generic_writing_board() -> None:
    contract = build_seed_subject_contract(
        seed_id="seed-5000-0001",
        source_text_zh="手球计分记录夹",
        queries_ru=["планшет для записи счета гандбола"],
    )

    result = evaluate_subject_text(
        contract,
        "Обычная доска для письма и заметок",
    )

    assert result["accepted"] is False
    assert "ганд" in result["missing_stems"]


def test_subject_contract_accepts_russian_morphology_when_identity_is_preserved() -> None:
    contract = build_seed_subject_contract(
        seed_id="seed-5000-0001",
        source_text_zh="手球计分记录夹",
        queries_ru=["планшет для записи счета гандбола"],
    )

    result = evaluate_subject_text(
        contract,
        "Гандбольный планшет для ведения счёта",
    )

    assert result["accepted"] is True
    assert result["match_ratio"] >= 0.70
    assert set(result["matched_stems"]) >= {"ганд", "план", "счет"}


def test_subject_contract_accepts_close_market_title_with_two_of_three_core_terms() -> None:
    contract = build_seed_subject_contract(
        seed_id="seed-5000-0003",
        source_text_zh="汽车遮阳帘",
        queries_ru=["автомобильный солнцезащитный козырек"],
    )

    result = evaluate_subject_text(
        contract,
        "Солнцезащитная шторка на лобовое стекло автомобиля",
    )

    assert result["accepted"] is True
    assert set(result["matched_stems"]) >= {"авто", "солн"}


def test_subject_contract_removes_only_real_search_noise() -> None:
    contract = build_seed_subject_contract(
        seed_id="seed-5000-0002",
        source_text_zh="厨房收纳架",
        queries_ru=["купить кухонную полку из Китая цена"],
    )

    assert "кухо" in contract["required_stems"]
    assert "полк" in contract["required_stems"]
    assert "купи" not in contract["required_stems"]
    assert "кита" not in contract["required_stems"]
    assert "цена" not in contract["required_stems"]
