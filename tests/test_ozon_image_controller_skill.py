from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "ozon-image-generation-controller"
    / "SKILL.md"
)


def test_image_controller_skill_uses_five_workers_and_reserves_sixth_slot() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "name: ozon-image-generation-controller" in text
    assert "skills/ozon-product-media-generator/SKILL.md" in text
    for index in range(1, 6):
        assert f"ozon-image-worker-{index:02d}" in text
    assert "ozon-image-worker-06" not in text
    assert "Create exactly five regular worker tasks" in text
    assert "Reuse the same five worker tasks" in text
    assert "Never create a sixth regular image worker" in text
    assert "Keep one agent slot reserved for failure recovery, diagnosis, or human intervention" in text


def test_image_controller_skill_has_bounded_stop_gates() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "queue is empty" in text
    assert "manual review" in text
    assert "blocking gate" in text
    assert "user stops" in text
    assert "Never upload" in text
    assert "Never modify business source code" in text
