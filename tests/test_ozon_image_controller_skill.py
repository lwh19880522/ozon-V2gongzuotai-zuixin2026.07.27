from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skills" / "ozon-product-media-generator" / "SKILL.md"


def test_old_controller_skill_is_removed_and_media_skill_owns_the_full_flow() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert not (ROOT / "skills" / "ozon-image-generation-controller").exists()
    assert "name: ozon-product-media-generator" in text
    assert "single-thread" in text
    assert "one package at a time" in text
    assert "claim-next" in text
    assert "RUN package_id=" in text
    assert "worker-01" not in text
    assert "thread_id" not in text
    assert "pool" not in text.casefold()
    assert "concurrent" not in text.casefold()


def test_dedicated_media_skill_starts_and_stops_automatic_public_gateway() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "media-start" in text
    assert "media-stop" in text
    assert "free anonymous Cloudflare Quick Tunnel" in text
    assert "ask the user" not in text.casefold()
    assert "R2" not in text


def test_dedicated_media_skill_has_bounded_per_product_stop_gates() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "A missing SKU or subject gate blocks only that product" in text
    assert "Never auto-resume a stopped product" in text
    assert "`pending` = 0" in text
    assert "`in_progress` = 0" in text
    assert "directly replaces the product gallery in Ozon" in text
    assert "never returns generated files to the workbench" in text


def test_dedicated_media_skill_preserves_selected_repairs_and_frozen_slots() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "repair_pending" in text
    assert "review_issue_code" in text
    assert "review_note" in text
    assert "only the explicitly selected slots" in text
    assert "Never reopen or regenerate an unselected `accepted` slot" in text


def test_dedicated_media_skill_claims_workbench_task_packages() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "image_tasks/pending" in text
    assert "image_tasks/in_progress" in text
    assert "seller_import_task_id" in text
    assert "product_id" in text
