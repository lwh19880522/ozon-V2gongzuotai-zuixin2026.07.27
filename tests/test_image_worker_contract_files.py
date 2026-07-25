from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_image_worker_contract_uses_ten_fixed_visible_tasks() -> None:
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
    fs_repo = (ROOT / "src" / "ozon_v2" / "adapters" / "fs_repo.py").read_text(
        encoding="utf-8"
    )

    for index in range(1, 11):
        worker_id = f"ozon-image-worker-{index:02d}"
        assert worker_id in controller_skill
        assert worker_id in worker_skill

    assert "10 个固定可见任务槽位" in controller_skill
    assert "全局并发上限为 10" in controller_skill
    assert "按槽位顺序复用" in controller_skill

    combined_active_contract = "\n".join(
        (controller_agent, worker_skill, plugin_manifest, fs_repo)
    )
    assert "ten fixed reusable user-visible Codex tasks" in controller_agent
    assert "reuse the registered tasks in slot order" in controller_agent.casefold()
    assert "only when the user explicitly requests a new task" in controller_agent
    assert "fixed pool of ten user-visible tasks" in plugin_manifest
    assert '"image_tasks" / "pending"' in fs_repo
    assert "subagent" not in combined_active_contract
    assert "最多 5" not in combined_active_contract


def test_worker_contract_uses_only_claimed_task_package_assignments() -> None:
    worker_skill = (
        ROOT / "skills" / "ozon-product-media-generator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    worker_agent = (
        ROOT / "skills" / "ozon-product-media-generator" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")

    assert "scripts/ozon_image_task_inbox.py" in worker_skill
    assert "image_tasks/in_progress" in worker_skill
    assert "RUN package_id=" in worker_agent
    assert "claim and process the next product" not in worker_agent


def test_product_media_skill_directly_replaces_ozon_gallery_without_workbench_callback() -> None:
    worker_skill = (
        ROOT / "skills" / "ozon-product-media-generator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    worker_agent = (
        ROOT / "skills" / "ozon-product-media-generator" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")

    assert "image_tasks/in_progress" in worker_skill
    assert "replace_product_pictures" in worker_skill
    assert "all eight ordered public image URLs" in worker_skill
    assert "never return generated files to the workbench" in worker_skill
    assert "direct Ozon gallery replacement" in worker_agent


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
    assert "natural Russian sentence case" in skill
    assert "360-pixel preview" in prompt_contract
    assert "prominent Russian headline" in prompt_contract
    assert "edge-gradient visual system" in skill
    assert "side-edge gradient" in prompt_contract
    assert "bottom gradient" in prompt_contract
    assert "anchor lines" in prompt_contract
    assert "detached rounded text cards" in prompt_contract
    assert "soft side-edge gradient zone" in main_prompt
    assert "bottom-gradient caption zone" in detail_a_prompt
    assert "anchor-line callouts" in detail_a_prompt
    assert "bottom-gradient caption zone" in detail_b_prompt
    assert "edge-gradient visual system" in repair_prompt
    assert "detail_01` and `detail_04` keep one fact block" in skill
    assert "detail_02`, `detail_03`, `detail_05`, and `detail_06` may use two" in prompt_contract
    assert "two verified labels" in detail_a_prompt
    assert "up to two verified labels" in detail_b_prompt
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


def test_product_media_skill_and_grid_prompts_require_three_by_four_finished_images() -> None:
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

    assert "ozon-image-v4" in "\n".join((skill, prompt_contract))
    assert "3:4" in skill
    assert "3:4" in prompt_contract
    assert "3:2" in main_prompt
    assert "each panel is exactly 3:4 portrait" in main_prompt
    for prompt in (detail_a_prompt, detail_b_prompt):
        assert "9:4" in prompt
        assert "each panel is exactly 3:4 portrait" in prompt
    assert "exactly 3:4 portrait" in repair_prompt


def test_product_media_skill_maps_one_primary_ozon_reference_to_every_finished_slot() -> None:
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

    assert "reference_mapping_version=ozon-reference-map-v1" in combined
    assert "primary_ozon_reference" in skill
    assert "one primary Ozon reference per finished slot" in prompt_contract
    assert "main_01` through `detail_06" in prompt_contract
    assert "gallery order" in prompt_contract
    assert "same reference may be bound to at most two slots" in prompt_contract
    assert "one white identity anchor, eight slot-specific Ozon references" in skill

    assert "Ozon reference A maps only to Panel 1" in main_prompt
    assert "Ozon reference B maps only to Panel 2" in main_prompt
    for prompt in (detail_a_prompt, detail_b_prompt):
        assert "Ozon reference A maps only to Panel 1" in prompt
        assert "Ozon reference B maps only to Panel 2" in prompt
        assert "Ozon reference C maps only to Panel 3" in prompt
    for prompt in (main_prompt, detail_a_prompt, detail_b_prompt):
        assert "Do not blend visual directions across panels" in prompt
        assert "Replace the reference product with the locked target product" in prompt
        assert "Do not copy reference branding, text, watermark, price" in prompt

    for receipt_field in (
        "primary_ozon_reference_sha256",
        "reference_slot_index",
        "reference_reused",
        "reference_composition_followed",
        "locked_subject_preserved",
    ):
        assert receipt_field in prompt_contract
    for blueprint_field in (
        "subject_position",
        "subject_scale",
        "camera_family",
        "shot_scale",
        "background_family",
        "lighting_family",
        "negative_space",
        "copy_zone",
    ):
        assert blueprint_field in prompt_contract

    assert "Reuse the failed slot's original primary Ozon reference" in repair_prompt
    assert "unless the user explicitly changes that reference" in repair_prompt
    assert "1x2 main grid" in skill
    assert "two 1x3 supporting grids" in skill
    assert "exactly 3:4 portrait" in combined


def test_product_media_skill_materializes_references_and_checkpoints_every_generation_call() -> None:
    skill_root = ROOT / "skills" / "ozon-product-media-generator"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    prompt_contract = (skill_root / "references" / "prompt-contract.md").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((skill, prompt_contract))

    assert "materialize-references" in combined
    assert "reference_manifest.json" in combined
    assert "512" in combined
    assert "checkpoint-asset" in combined
    assert "checkpoint-status" in combined
    assert "immediately after each image-generation call" in combined
    assert "reuse every hash-verified checkpoint" in combined
    assert "primary_ozon_reference_path" in combined


def test_product_media_handoff_shows_only_final_rendered_slots_with_russian_labels() -> None:
    skill_root = ROOT / "skills" / "ozon-product-media-generator"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert "view_image" in skill
    assert "all eight accepted_path files" in skill
    assert "raw imagegen grids are never the final preview" in skill
    assert "Russian labels" in skill


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
