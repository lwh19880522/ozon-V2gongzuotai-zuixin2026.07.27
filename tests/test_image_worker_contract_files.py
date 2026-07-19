from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_image_worker_contract_uses_dynamic_pool_up_to_five_workers() -> None:
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
    assert "up to five reusable image subagents" in controller_agent
    assert "spawn_agent" in controller_agent
    assert "Never use create_thread" in controller_agent
    assert "available capacity" in controller_agent
    assert "sixth child slot reserved" in controller_agent
    assert "dynamic pool of up to five subagents" in plugin_manifest
    assert "等待最多 5 个动态 Codex 生图子智能体" in workbench_service
    assert "worker tasks" not in controller_agent
    assert "exactly five" not in combined_active_contract
    assert "等待 5 个固定 Codex 生图子智能体" not in workbench_service
    assert "exactly two" not in combined_active_contract
    assert "two-worker" not in combined_active_contract
    assert "等待两个 Codex 生图工作线程" not in combined_active_contract


def test_product_media_contract_keeps_white_anchor_out_of_finished_slots() -> None:
    skill_root = ROOT / "skills" / "ozon-product-media-generator"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    prompt_contract = (skill_root / "references" / "prompt-contract.md").read_text(
        encoding="utf-8"
    )
    main_prompt = (skill_root / "assets" / "main-grid-prompt.txt").read_text(
        encoding="utf-8"
    )
    detail_a_prompt = (skill_root / "assets" / "detail-grid-a-prompt.txt").read_text(
        encoding="utf-8"
    )
    detail_b_prompt = (skill_root / "assets" / "detail-grid-b-prompt.txt").read_text(
        encoding="utf-8"
    )
    repair_prompt = (skill_root / "assets" / "repair-slot-prompt.txt").read_text(
        encoding="utf-8"
    )
    combined = "\n".join(
        (skill, prompt_contract, main_prompt, detail_a_prompt, detail_b_prompt, repair_prompt)
    )

    assert "intermediate identity anchor" in skill
    assert "must never become one of the eight finished slots" in skill
    assert "plain or near-white product-only" in main_prompt
    assert "plain or near-white product-only" in detail_a_prompt
    assert "plain or near-white product-only" in detail_b_prompt
    assert "plain or near-white product-only" in repair_prompt
    assert "Russian copy cannot substitute for visual proof" in prompt_contract
    assert "slot_role_satisfied" in prompt_contract
    assert "role_visually_demonstrated" in prompt_contract
    assert "not_plain_or_near_white_product_only" in prompt_contract
    assert "distinct_from_accepted_slots" in prompt_contract
    assert "copy_not_used_as_visual_evidence" in prompt_contract
    assert "ozon-image-v3" in combined


def test_product_media_skill_uses_visual_contract_v3_storyboard_and_local_copy_repairs() -> None:
    skill_root = ROOT / "skills" / "ozon-product-media-generator"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    prompt_contract = (skill_root / "references" / "prompt-contract.md").read_text(
        encoding="utf-8"
    )
    main_prompt = (skill_root / "assets" / "main-grid-prompt.txt").read_text(encoding="utf-8")
    detail_a_prompt = (skill_root / "assets" / "detail-grid-a-prompt.txt").read_text(
        encoding="utf-8"
    )
    detail_b_prompt = (skill_root / "assets" / "detail-grid-b-prompt.txt").read_text(
        encoding="utf-8"
    )
    repair_prompt = (skill_root / "assets" / "repair-slot-prompt.txt").read_text(
        encoding="utf-8"
    )
    combined = "\n".join(
        (skill, prompt_contract, main_prompt, detail_a_prompt, detail_b_prompt, repair_prompt)
    )

    assert "ozon-image-v3" in combined
    assert "exactly four mandatory image-generation calls" in skill
    assert "one white subject" in skill
    assert "one 1x2 main grid" in skill
    assert "two 1x3 supporting grids" in skill
    assert "render-visual" in skill
    assert "do not call imagegen for typography" in skill
    assert "detail-grid-a-prompt.txt" in skill
    assert "detail-grid-b-prompt.txt" in skill
    assert "detail-grid-prompt.txt" not in skill
    assert "main_01" in main_prompt and "clean hero" in main_prompt
    assert "main_02" in main_prompt and "integrated information rail" in main_prompt
    assert all(slot in detail_a_prompt for slot in ("detail_01", "detail_02", "detail_03"))
    assert all(slot in detail_b_prompt for slot in ("detail_04", "detail_05", "detail_06"))
    assert "copy failure repairs only the local text layer" in prompt_contract
    for prompt in (main_prompt, detail_a_prompt, detail_b_prompt, repair_prompt):
        assert "icons" in prompt
    assert "invented claims" in main_prompt
    assert "second structure or verified metric" in detail_a_prompt
    assert "detail, material" not in detail_a_prompt
    diversity_contract = (
        "differ in at least three of environment, lighting, camera, shot scale, and buyer question"
    )
    assert diversity_contract in main_prompt
    assert diversity_contract in detail_a_prompt
    assert diversity_contract in detail_b_prompt


def test_product_media_skill_uses_structured_user_feedback_for_selected_repairs() -> None:
    skill_root = ROOT / "skills" / "ozon-product-media-generator"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    prompt_contract = (skill_root / "references" / "prompt-contract.md").read_text(
        encoding="utf-8"
    )
    repair_prompt = (skill_root / "assets" / "repair-slot-prompt.txt").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((skill, prompt_contract, repair_prompt))

    assert "repair_pending" in skill
    assert "review_issue_code" in combined
    assert "review_note" in combined
    assert "only the explicitly selected slots" in skill
    assert "do not repeat the four mandatory first-attempt calls" in skill
    assert "russian_copy" in skill
    assert "copy_repair_local" in skill
    assert "must not be treated as product evidence" in repair_prompt
    assert "one image-generation call per selected slot" in skill
