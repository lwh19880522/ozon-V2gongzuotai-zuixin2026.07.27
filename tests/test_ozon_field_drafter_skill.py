from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "ozon-intelligent-field-drafter"


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
    assert "evidence_refs" in text
    assert "summary.pending_fields=0" in text
    assert "1688" in text
    assert "Ozon" in text
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
    assert "Never infer" in policy
    assert "Required fields" in policy


def test_plugin_versions_workbench_and_both_skill_phases_as_one_product() -> None:
    plugin = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert plugin["version"] == "0.3.0"
    assert "field-drafting" in plugin["keywords"]
    description = plugin["interface"]["longDescription"]
    prompts = "\n".join(plugin["interface"]["defaultPrompt"])
    assert "field drafting" in description.casefold()
    assert "image generation" in description.casefold()
    assert "$ozon-intelligent-field-drafter" in prompts
    assert "$ozon-image-generation-controller" in prompts

