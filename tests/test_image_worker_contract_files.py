from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_image_worker_contract_uses_five_regular_workers_and_one_reserved_slot() -> None:
    controller_skill = (ROOT / "skills" / "ozon-image-generation-controller" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    controller_agent = (
        ROOT / "skills" / "ozon-image-generation-controller" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    worker_skill = (ROOT / "skills" / "ozon-product-media-generator" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    plugin_manifest = (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    workbench_service = (ROOT / "src" / "ozon_v2" / "services" / "workbench_service.py").read_text(
        encoding="utf-8"
    )

    for index in range(1, 6):
        worker_id = f"ozon-image-worker-{index:02d}"
        assert worker_id in controller_skill
        assert worker_id in worker_skill

    combined_active_contract = "\n".join(
        (controller_agent, worker_skill, plugin_manifest, workbench_service)
    )
    assert "exactly five reusable image worker tasks" in controller_agent
    assert "one agent slot reserved" in controller_agent
    assert "five-worker image generation queues" in plugin_manifest
    assert "等待 5 个固定 Codex 生图 worker" in workbench_service
    assert "exactly two" not in combined_active_contract
    assert "two-worker" not in combined_active_contract
    assert "等待两个 Codex 生图工作线程" not in combined_active_contract
