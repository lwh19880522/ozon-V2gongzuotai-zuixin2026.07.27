from pathlib import Path
import shutil
import subprocess
import sys


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


def test_skill_entrypoint_runs_directly_from_repository_root() -> None:
    script = SKILL_ROOT / "scripts" / "store_content_optimizer.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    for command in ("scan", "next", "apply", "status"):
        assert command in completed.stdout


def test_installed_skill_script_discovers_repository_from_working_directory(
    tmp_path: Path,
) -> None:
    installed = (
        tmp_path
        / ".codex"
        / "skills"
        / "ozon-store-content-risk-optimizer"
        / "scripts"
    )
    installed.mkdir(parents=True)
    script = installed / "store_content_optimizer.py"
    shutil.copy2(
        SKILL_ROOT / "scripts" / "store_content_optimizer.py",
        script,
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_skill_is_automatic_incremental_non_media_and_risk_first() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    policy = (SKILL_ROOT / "references" / "optimization-policy.md").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "scan",
        "next",
        "apply",
        "status",
        "automatic",
        "SQLite ledger",
        "all unarchived products",
        "unchanged completed products",
        "natural Russian",
        "semantic self-review",
        "evidence_insufficient",
        "FBS stock 10",
        "FBS stock 0",
        "never archive",
        "archived products",
        "active orders",
        "rollback",
    ):
        assert phrase.casefold() in text.casefold()
    for phrase in (
        "title",
        "description",
        "Rich Content",
        "attributes",
        "content score",
        "media score",
        "Chinese",
        "1688",
        "supplier",
        "quantity",
        "set composition",
        "dimensions",
        "weight",
        "capacity",
        "power",
        "voltage",
        "dictionary",
        "price",
        "compliance",
        "storefront divergence",
    ):
        assert phrase.casefold() in policy.casefold()
    assert "Do not generate, modify, upload, or delete images or video" in text
    assert "Do not ask the user to approve routine safe optimization" in text
    forbidden_marker = "TO" + "DO"
    assert forbidden_marker not in text
    assert forbidden_marker not in policy
