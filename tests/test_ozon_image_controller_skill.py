from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "ozon-image-generation-controller"
    / "SKILL.md"
)


def test_image_controller_skill_uses_dynamic_pool_up_to_five_workers() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "name: ozon-image-generation-controller" in text
    assert "skills/ozon-product-media-generator/SKILL.md" in text
    for index in range(1, 6):
        assert f"ozon-image-worker-{index:02d}" in text
    assert "ozon-image-worker-06" not in text
    assert "Spawn up to five internal subagents with the `spawn_agent` tool" in text
    assert "min(5 - active image subagents, unassigned queued products, currently free internal subagent slots)" in text
    assert "Do not shrink or stop existing workers merely because no new slot is free" in text
    assert "Reuse every successfully spawned subagent" in text
    assert "Never use `create_thread`" in text
    assert "Do not create user-visible Codex tasks or threads" in text
    assert "Continue with every subagent that spawned successfully" in text
    assert "If the active image-subagent pool is empty and zero internal subagent slots are available" in text
    assert "If zero internal subagent slots are available, do not claim image work" not in text
    assert "Never fall back to `create_thread`" in text
    assert "Create exactly five regular worker tasks" not in text
    assert "Spawn exactly five internal subagents" not in text
    assert "fewer than five subagent slots are available, stop" not in text
    assert "Never create a sixth regular image worker" in text
    assert "When the runtime exposes a sixth child slot, keep it reserved" in text


def test_image_controller_skill_has_bounded_stop_gates() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "queue is empty" in text
    assert "manual review" in text
    assert "blocking gate" in text
    assert "user stops" in text
    assert "Never upload" in text
    assert "Never modify business source code" in text


def test_image_controller_dispatches_user_selected_repairs_without_reopening_frozen_slots() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "repair_pending" in text
    assert "review_issue_code" in text
    assert "review_note" in text
    assert "only the explicitly selected slots" in text
    assert "Never reopen or regenerate an unselected `accepted` slot" in text
