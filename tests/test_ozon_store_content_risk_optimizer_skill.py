from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "ozon-store-content-risk-optimizer"


def test_skill_has_only_the_approved_runtime_files() -> None:
    files = {
        path.relative_to(SKILL_ROOT).as_posix()
        for path in SKILL_ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert files == {
        "SKILL.md",
        "agents/openai.yaml",
        "references/optimization-policy.md",
        "scripts/store_content_optimizer.py",
    }


def test_skill_entrypoint_exposes_the_fixed_commands() -> None:
    script = (SKILL_ROOT / "scripts" / "store_content_optimizer.py").read_text(
        encoding="utf-8"
    )
    for command in ("scan", "next", "apply", "status"):
        assert f'add_parser("{command}")' in script
