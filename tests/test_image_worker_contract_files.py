from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _worker_skill_root() -> Path:
    return ROOT / "skills" / "ozon-product-media-generator"


def _worker_contract_files() -> dict[str, str]:
    root = _worker_skill_root()
    paths = {
        "skill": root / "SKILL.md",
        "agent": root / "agents" / "openai.yaml",
        "contract": root / "references" / "prompt-contract.md",
        "core": root / "assets" / "ozon-commercial-infographic-core-prompt.txt",
        "main": root / "assets" / "main-grid-prompt.txt",
        "detail_a": root / "assets" / "detail-grid-a-prompt.txt",
        "detail_b": root / "assets" / "detail-grid-b-prompt.txt",
        "repair": root / "assets" / "repair-slot-prompt.txt",
        "white": root / "assets" / "white-subject-prompt.txt",
    }
    return {
        name: path.read_text(encoding="utf-8")
        for name, path in paths.items()
    }


def test_active_image_worker_contract_uses_ten_fixed_visible_tasks() -> None:
    controller_skill = (
        ROOT / "skills" / "ozon-image-generation-controller" / "SKILL.md"
    ).read_text(encoding="utf-8")
    controller_agent = (
        ROOT / "skills" / "ozon-image-generation-controller" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    worker_skill = _worker_contract_files()["skill"]
    plugin_manifest = (ROOT / ".codex-plugin" / "plugin.json").read_text(
        encoding="utf-8"
    )
    fs_repo = (ROOT / "src" / "ozon_v2" / "adapters" / "fs_repo.py").read_text(
        encoding="utf-8"
    )

    for index in range(1, 11):
        worker_id = f"ozon-image-worker-{index:02d}"
        assert worker_id in controller_skill
    assert "`ozon-image-worker-01` through `ozon-image-worker-10`" in worker_skill

    assert "10 个固定可见任务槽位" in controller_skill
    assert "全局并发上限为 10" in controller_skill
    assert "按槽位顺序复用" in controller_skill
    assert "ten fixed reusable user-visible Codex tasks" in controller_agent
    assert "reuse the registered tasks in slot order" in controller_agent.casefold()
    assert "only when the user explicitly requests a new task" in controller_agent
    assert "fixed pool of ten user-visible tasks" in plugin_manifest
    assert '"image_tasks" / "pending"' in fs_repo
    assert "subagent" not in "\n".join(
        (controller_agent, worker_skill, plugin_manifest, fs_repo)
    )


def test_worker_contract_uses_only_claimed_task_package_assignments() -> None:
    files = _worker_contract_files()

    assert "scripts/ozon_image_task_inbox.py" in files["skill"]
    assert "image_tasks/in_progress" in files["skill"]
    assert "schema_version=2" in files["skill"]
    assert "RUN package_id=" in files["agent"]
    assert "claim and process the next product" not in files["agent"]


def test_product_media_directly_uploads_without_workbench_callback() -> None:
    files = _worker_contract_files()
    inbox_script = (ROOT / "scripts" / "ozon_image_task_inbox.py").read_text(
        encoding="utf-8"
    )

    assert "replace_product_pictures" in inbox_script
    assert "eight images" in files["skill"]
    assert "Never return generated files to the workbench" in files["skill"]
    assert "replace the Ozon gallery" in files["agent"]


def test_product_media_requires_r2_and_uploads_slideshow_media() -> None:
    files = _worker_contract_files()
    inbox_script = (ROOT / "scripts" / "ozon_image_task_inbox.py").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((*files.values(), inbox_script))

    assert "media-preflight" in combined
    assert "Before any image-generation call" in files["skill"]
    assert "public R2 base URL" in files["skill"]
    assert "build-slideshow" in combined
    assert "slideshow.mp4" in combined
    assert "video_cover.jpg" in combined
    assert "--video" in inbox_script
    assert "--video-cover" in inbox_script


def test_product_media_v8_uses_one_frozen_white_anchor_as_the_only_image_input() -> None:
    files = _worker_contract_files()
    combined = "\n".join(files.values())

    assert "ozon-image-v8" in combined
    assert "referenced_image_paths=[<same verified white anchor path>]" in files["skill"]
    assert "referenced_image_paths=[<same frozen white anchor path>]" in files["contract"]
    assert "Reference Image 1, the only image input" in combined
    assert "Reference Image 2 is forbidden" in files["contract"]
    assert "Do not attach Reference Image 2" in files["skill"]
    assert "Do not fetch or materialize Ozon gallery images" in files["skill"]
    assert "Ozon reference images are not inputs" in files["skill"]
    assert "never attach Ozon images or any second image" in files["agent"]
    assert "reference_mapping_version=none" in combined
    assert "guidance_mode=fixed_prompt_white_anchor" in combined
    assert "`num_last_images_to_include`" in files["skill"]
    assert "Do not" in files["skill"]

    for name in ("main", "detail_a", "detail_b", "repair"):
        prompt = files[name]
        assert "Reference Image 1" in prompt
        assert "an Ozon image" in prompt
        assert "Reference Image 2" in prompt
    for name in ("main", "detail_a", "detail_b"):
        assert "complete image input list must contain exactly one item" in files[name]
    assert "only image input allowed" in files["repair"]


def test_workbench_package_contract_does_not_send_ozon_images_to_generation() -> None:
    service = (
        ROOT / "src" / "ozon_v2" / "services" / "workbench_service.py"
    ).read_text(encoding="utf-8")
    package_builder = service[
        service.index("def _emit_image_task_package") :
        service.index("def submit_product_upload")
    ]

    assert '"schema_version": 2' in package_builder
    assert '"reference_count": 1' in package_builder
    assert '"additional_image_references_allowed": False' in package_builder
    assert '"composition_source": "fixed_skill_prompt_only"' in package_builder
    assert '"ozon_reference_images"' not in package_builder


def test_product_media_keeps_white_anchor_out_of_finished_slots() -> None:
    files = _worker_contract_files()
    combined = "\n".join(files.values())

    assert "persistent and immutable" in files["skill"]
    assert "never a finished slot" in files["contract"]
    assert "plain or near-white product-only" in files["main"]
    assert "plain or near-white product-only" in files["detail_a"]
    assert "plain or near-white product-only" in files["detail_b"]
    assert "plain or near-white product-only" in files["repair"]
    assert "slot_role_satisfied" in files["contract"]
    assert "role_visually_demonstrated" in files["contract"]
    assert "not_plain_or_near_white_product_only" in files["contract"]
    assert "distinct_from_accepted_slots" in files["contract"]
    assert "copy_not_used_as_visual_evidence" in files["contract"]
    assert "locked_subject_preserved" in combined


def test_product_media_uses_fixed_two_three_three_storyboard() -> None:
    files = _worker_contract_files()

    assert "fixed 2+3+3 structure" in files["skill"]
    assert "one horizontal 1x2 grid" in files["skill"]
    assert files["skill"].count("one horizontal 1x3 grid") == 2
    assert "main_01" in files["main"] and "main_02" in files["main"]
    assert all(
        slot in files["detail_a"]
        for slot in ("detail_01", "detail_02", "detail_03")
    )
    assert all(
        slot in files["detail_b"]
        for slot in ("detail_04", "detail_05", "detail_06")
    )
    assert "3:2" in files["main"]
    assert "exactly 3:4 portrait" in files["main"]
    assert "9:4" in files["detail_a"]
    assert "9:4" in files["detail_b"]
    assert "exactly 3:4 portrait" in files["repair"]


def test_product_media_uses_complete_fixed_commercial_prompt() -> None:
    files = _worker_contract_files()
    core = files["core"]

    assert "顶级俄罗斯电商视觉总监" in core
    assert "白底主体锚点" in core
    assert "唯一允许传入的图片" in core
    assert "不得传入参考图片 2 或任何 Ozon 图片" in core
    assert "产品是绝对视觉中心，占画面约 55%—70%" in core
    assert "3—7个俄语单词" in core
    assert "不超过14个俄语单词" in core
    assert "2—4个功能标签" in core
    assert "最多保留四个主要说明区域" in core
    assert "伪俄文" in core
    assert "虚构参数" in core
    assert "不得先生成无字图再单独消耗一次生图来贴字" in core
    assert "产品是否与白底主体锚点和锁定 1688 SKU 属于同一型号" in core


def test_product_media_generates_verified_russian_copy_in_first_pass() -> None:
    files = _worker_contract_files()
    prompts = [files[name] for name in ("main", "detail_a", "detail_b", "repair")]
    combined = "\n".join(prompts)

    assert "copy_mode=imagegen_integrated" in combined
    assert "Render the supplied exact Russian labels as part of this first generation" in combined
    assert "Do not generate an unlabelled scene for later relabelling" in combined
    assert "Never consume a second scene-generation call merely to add Russian copy" in files["contract"]


def test_product_media_checkpoints_repairs_and_previews_final_slots() -> None:
    files = _worker_contract_files()
    combined = "\n".join(files.values())

    assert "checkpoint" in combined
    assert "Reuse every hash-verified checkpoint" in files["skill"]
    assert "repair_pending" in files["skill"]
    assert "review_issue_code" in combined
    assert "review_note" in combined
    assert "one replacement generation" in files["skill"]
    assert "per selected slot" in files["skill"]
    assert "same sole white anchor" in files["skill"]
    assert "all eight `accepted_path` files with `view_image`" in files["contract"]
    assert "inspect all eight accepted files with" in files["skill"]
