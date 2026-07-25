from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "ozon-image-generation-controller"
    / "SKILL.md"
)


def test_image_controller_skill_uses_ten_fixed_visible_tasks() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "name: ozon-image-generation-controller" in text
    assert "skills/ozon-product-media-generator/SKILL.md" in text
    for index in range(1, 11):
        assert f"ozon-image-worker-{index:02d}" in text
    assert "ten fixed user-visible Codex work tasks" in text
    assert "global across every Ozon V2 batch" in text
    assert "persisted opaque `thread_id`" in text
    assert "Reuse the same ten tasks in slot order" in text
    assert "one product at a time" in text
    assert "at most ten products concurrently" in text
    assert "Return repair and continuation work to the product's `preferred_slot`" in text
    assert "only when the user explicitly requests a new task" in text
    assert "internal subagent" not in text
    assert "five regular worker" not in text


def test_image_controller_skill_has_bounded_stop_gates() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "Continue dispatching other eligible `pending` packages" in text
    assert "A missing SKU or subject gate blocks only that product" in text
    assert "A `stopped` product does not stop other eligible products" in text
    assert "Never auto-resume a stopped product" in text
    assert "no eligible `pending` packages remain" in text
    assert "manual review" not in text.casefold()
    assert "user stops" in text
    assert "directly replaces the product gallery in Ozon" in text
    assert "never returns generated files to the workbench" in text
    assert "Never modify business source code" in text


def test_image_controller_dispatches_user_selected_repairs_without_reopening_frozen_slots() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "repair_pending" in text
    assert "review_issue_code" in text
    assert "review_note" in text
    assert "only the explicitly selected slots" in text
    assert "Never reopen or regenerate an unselected `accepted` slot" in text


def test_image_controller_claims_fixed_task_packages_outside_the_workbench() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "image_tasks/pending" in text
    assert "image_tasks/in_progress" in text
    assert "seller_import_task_id" in text
    assert "product_id" in text
    assert "fixed task-package inbox" in text


def test_image_controller_uses_the_lightweight_dispatcher_until_every_job_is_drained() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "scripts/ozon_image_task_inbox.py" in text
    assert "claim-next" in text
    assert "RUN package_id=" in text
    assert "Do not stop after the first ten products" in text
    assert "`pending` = 0" in text
    assert "`in_progress` = 0" in text
