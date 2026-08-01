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
        "gallery": root / "assets" / "gallery-8-grid-prompt.txt",
        "repair": root / "assets" / "repair-slot-prompt.txt",
        "white": root / "assets" / "white-subject-prompt.txt",
    }
    return {name: path.read_text(encoding="utf-8") for name, path in paths.items()}


def test_active_image_worker_contract_is_strictly_single_threaded() -> None:
    worker_skill = _worker_contract_files()["skill"]
    worker_agent = _worker_contract_files()["agent"]
    plugin_manifest = (ROOT / ".codex-plugin" / "plugin.json").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((worker_skill, worker_agent, plugin_manifest))

    assert "single-thread" in combined
    assert "one package at a time" in combined
    assert not (ROOT / "skills" / "ozon-image-generation-controller").exists()
    assert "ozon-image-worker-01" not in combined
    assert "thread_id" not in combined
    assert "fixed pool" not in combined.casefold()
    assert "concurrent image generation" not in combined.casefold()
    assert "subagent" not in combined.casefold()


def test_worker_contract_uses_only_claimed_task_package_assignments() -> None:
    files = _worker_contract_files()

    assert "scripts/ozon_image_task_inbox.py" in files["skill"]
    assert "image_tasks/in_progress" in files["skill"]
    assert "schema_version=2" in files["skill"]
    assert "RUN package_id=" in files["agent"]


def test_product_media_automatically_starts_free_public_gateway() -> None:
    files = _worker_contract_files()
    inbox_script = (ROOT / "scripts" / "ozon_image_task_inbox.py").read_text(
        encoding="utf-8"
    )
    public_media = (ROOT / "src" / "ozon_v2" / "adapters" / "public_media.py").read_text(
        encoding="utf-8"
    )
    workbench = (ROOT / "src" / "ozon_v2" / "services" / "workbench_service.py").read_text(
        encoding="utf-8"
    )
    fs_repo = (ROOT / "src" / "ozon_v2" / "adapters" / "fs_repo.py").read_text(
        encoding="utf-8"
    )
    local_server = (ROOT / "src" / "ozon_v2" / "workbench" / "local_server.py").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((*files.values(), inbox_script, public_media, workbench, fs_repo, local_server))

    assert "media-start" in combined
    assert "media-stop" in combined
    assert "CloudflareQuickTunnelMediaPublisher" in inbox_script
    assert "trycloudflare.com" in public_media
    assert "user-configured" not in combined.casefold()
    assert " R2" not in combined
    assert "r2_" not in combined.casefold()
    assert "configure_public_media_base_url" not in combined
    assert "public_media_settings" not in combined
    assert "/api/settings/public-media" not in combined
    assert "build-slideshow" in combined
    assert "slideshow.mp4" in combined
    assert "video_cover.jpg" in combined


def test_product_media_uses_one_frozen_white_anchor_as_only_image_input() -> None:
    files = _worker_contract_files()
    combined = "\n".join(files.values())

    assert "referenced_image_paths=[<same verified white anchor path>]" in files["skill"]
    assert "referenced_image_paths=[<same frozen white anchor path>]" in files["contract"]
    assert "Reference Image 1, the only image input" in combined
    assert "Reference Image 2 is forbidden" in files["contract"]
    assert "Do not attach Reference Image 2" in files["skill"]
    assert "Do not fetch or materialize Ozon gallery images" in files["skill"]
    assert "reference_mapping_version=none" in combined
    assert "guidance_mode=fixed_prompt_white_anchor" in combined
    assert "`num_last_images_to_include`" in files["skill"]
    assert "complete image input list must contain exactly one item" in files["gallery"]
    assert "only image input allowed" in files["repair"]


def test_product_media_generates_one_four_by_two_grid_then_crops_eight_slots() -> None:
    files = _worker_contract_files()
    gallery = files["gallery"]

    assert "exactly two image-generation calls" in files["skill"]
    assert "one 4x2 grid" in files["skill"]
    assert "complete canvas aspect ratio of exactly 3:2" in gallery
    assert "four columns and two rows" in gallery
    for slot in (
        "main_01",
        "main_02",
        "detail_01",
        "detail_02",
        "detail_03",
        "detail_04",
        "detail_05",
        "detail_06",
    ):
        assert slot in gallery
    assert "crop-grid --layout 4x2" in files["skill"]
    assert "exactly 3:4 portrait" in files["repair"]


def test_workbench_package_contract_declares_single_grid_and_auto_gateway() -> None:
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
    assert '"generation_mode": "single_thread_8_grid"' in package_builder
    assert '"grid_layout": "4x2"' in package_builder
    assert '"public_media": "auto_quick_tunnel"' in package_builder
    assert "r2_preflight_required" not in package_builder
    assert '"ozon_reference_images"' not in package_builder


def test_product_media_keeps_identity_and_truth_gates_unchanged() -> None:
    files = _worker_contract_files()
    combined = "\n".join(files.values())

    assert "persistent and immutable" in files["skill"]
    assert "never a finished slot" in files["contract"]
    assert "locked_subject_preserved" in combined
    assert "product_truth" in files["contract"]
    assert "same live anchor path and SHA-256" in files["contract"]
    assert "If a scene cannot be made without changing Reference Image 1, simplify the scene" in files["gallery"]
    assert "Never change the anchor" in files["skill"]


def test_product_media_checkpoints_repairs_and_previews_final_slots() -> None:
    files = _worker_contract_files()
    combined = "\n".join(files.values())

    assert "checkpoint" in combined
    assert "Reuse every hash-verified checkpoint" in files["skill"]
    assert "repair_pending" in files["skill"]
    assert "review_issue_code" in combined
    assert "review_note" in combined
    assert "same frozen white anchor" in files["repair"]
    assert "all eight `accepted_path` files with `view_image`" in files["contract"]
