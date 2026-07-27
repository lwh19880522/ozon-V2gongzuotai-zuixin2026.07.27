from __future__ import annotations

import json
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "ozon-intelligent-field-drafter"
VISUAL_SCRIPT = SKILL_ROOT / "scripts" / "materialize_visual_evidence.py"


def test_field_drafter_skill_owns_intelligent_field_workflow() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "name: ozon-intelligent-field-drafter" in text
    assert "content-tasks" in text
    assert "field_tasks" in text
    assert "evidence_index" in text
    assert "creative_rewrite" in text
    assert "evidence_inference" in text
    assert '"decision":"filled"' in text
    assert '"decision":"unresolved"' in text
    assert "resolution_class" in text
    assert "source_fact_missing" in text
    assert "evidence_refs" in text
    assert "summary.pending_fields=0" in text
    assert "not proof of readiness" in text
    assert "set_quantity" in text
    assert "set_composition" in text
    assert "locked supplier SKU images" in text
    assert "visual_evidence_refs" in text
    assert "generated images are never product-fact evidence" in text
    assert "color, customer-facing color name, factory-pack count" in text
    assert "materialize_visual_evidence.py" in text
    assert "view_image" in text
    assert "visual_analysis" in text
    assert "subject_analysis" in text
    assert "primary product subject" in text
    assert "accessories, packaging, backgrounds, text overlays" in text
    assert "single_color" in text
    assert "multi_color" in text
    assert "variant_conflict" in text
    assert "field_finding" in text
    assert "customer-facing" in text
    assert "Russian" in text
    assert "1688" in text
    assert "Ozon" in text
    assert "pre-resolved store defaults" in text
    assert "Do not spend model decisions" in text
    assert "collected structured Ozon attributes" in text
    assert "现货当天发" in text
    assert "supplier fulfillment" in text
    assert "workflow.defaults.disable_product_grouping" in text
    assert "ozon.category_path.leaf" in text
    assert "supplier truth" in text
    assert "Do not upload" in text
    assert "Do not publish" in text
    assert "Do not spawn subagents" in text
    assert "TODO" not in text


def test_field_drafter_skill_has_detailed_source_and_validation_policy() -> None:
    policy = (SKILL_ROOT / "references" / "field-policy.md").read_text(
        encoding="utf-8"
    )

    assert "Source precedence" in policy
    assert "confirmed_supplier_sku" in policy
    assert "supplier_attributes" in policy
    assert "ozon_attributes" in policy
    assert "Identity fields" in policy
    assert "Dictionary fields" in policy
    assert "Russian normalization" in policy
    assert "Resolution classes" in policy
    assert "workflow_defaults" in policy
    assert "country of manufacture is always `Китай`" in policy
    assert "structured Ozon attributes remain active evidence" in policy
    assert "现货当天发" in policy
    assert "customer-facing normalization" in policy
    assert "disable_product_grouping" in policy
    assert "Never infer" in policy
    assert "Required fields" in policy
    assert "Visual evidence" in policy
    assert "single_sku_detail_page" in policy
    assert "Target scope" in policy
    assert "primary_product" in policy
    assert "factory_packaging" in policy
    assert "complete_set" in policy
    assert "Visible accessories do not create a product-color conflict" in policy
    assert "Do not infer dimensions, weight, warranty, certification" in policy


def test_field_drafter_materializes_locked_images_for_actual_visual_inspection(
    tmp_path: Path,
) -> None:
    assert VISUAL_SCRIPT.is_file(), "field Skill needs its own visual materializer"
    spec = importlib.util.spec_from_file_location(
        "ozon_field_visual_materializer",
        VISUAL_SCRIPT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image_url = "https://cbu01.alicdn.com/img/ibank/locked-sku.jpg"
    tasks = {
        "run_id": "wb-test",
        "items": [
            {
                "seed_id": "seed-1",
                "field_tasks": [
                    {
                        "field_key": "color",
                        "status": "pending",
                        "visual_evidence_refs": [
                            "supplier_selection.supplier_sku.image_urls.0"
                        ],
                    }
                ],
                "evidence_index": {
                    "supplier_selection.supplier_sku.image_urls.0": image_url
                },
            }
        ],
    }

    def fake_fetch(_url: str, target: Path) -> Path:
        target.write_bytes(b"locked-original-image")
        return target

    manifest = module.materialize_visual_evidence(
        tasks,
        tmp_path,
        fetch_image=fake_fetch,
    )

    assert manifest["run_id"] == "wb-test"
    assert len(manifest["items"]) == 1
    image = manifest["items"][0]["images"][0]
    assert image["evidence_ref"].endswith("image_urls.0")
    assert Path(image["local_path"]).is_file()
    assert image["sha256"]
    assert image["field_keys"] == ["color"]


def test_plugin_versions_workbench_and_both_skill_phases_as_one_product() -> None:
    plugin = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert plugin["version"] == "0.4.1"
    assert "field-drafting" in plugin["keywords"]
    description = plugin["interface"]["longDescription"]
    prompts = "\n".join(plugin["interface"]["defaultPrompt"])
    assert "field drafting" in description.casefold()
    assert "image generation" in description.casefold()
    assert "$ozon-intelligent-field-drafter" in prompts
    assert "$ozon-image-generation-controller" in prompts
