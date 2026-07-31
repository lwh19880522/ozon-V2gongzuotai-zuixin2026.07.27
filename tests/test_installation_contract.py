from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_ozon_v2.ps1"
SKILL_INSTALLER = ROOT / "scripts" / "install_codex_skills.ps1"
DOCTOR = ROOT / "scripts" / "verify_ozon_v2_install.ps1"
DOUBLE_CLICK_INSTALLER = ROOT / "安装并启动 Ozon V2.cmd"
DOUBLE_CLICK_LAUNCHER = ROOT / "启动 Ozon V2.cmd"


class InstallationContractTests(unittest.TestCase):
    def test_installer_is_portable_idempotent_and_supports_dry_run(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8-sig")

        self.assertIn("[switch]$DryRun", text)
        self.assertIn("[switch]$NoStart", text)
        self.assertIn("[switch]$NoShortcut", text)
        self.assertIn("[switch]$NoOpen", text)
        self.assertIn("[string]$ShortcutPath", text)
        self.assertIn("Split-Path -Parent $PSScriptRoot", text)
        self.assertIn(".venv", text)
        self.assertIn("-m", text)
        self.assertIn("pip", text)
        self.assertIn("install", text)
        self.assertIn("-e", text)
        self.assertIn("install_codex_skills.ps1", text)
        self.assertIn("create_workbench_shortcut.ps1", text)
        self.assertIn("launch_workbench.ps1", text)
        self.assertIn("verify_ozon_v2_install.ps1", text)
        self.assertIn("@('-ShortcutPath', $ShortcutPath)", text)
        self.assertNotIn("E:\\ozon-V2", text)
        self.assertNotIn("C:\\Users\\", text)

        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INSTALLER),
                "-DryRun",
                "-NoStart",
                "-NoShortcut",
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
        )

        self.assertEqual(
            0,
            completed.returncode,
            msg=f"{completed.stdout}\n{completed.stderr}",
        )
        self.assertIn("INSTALL_DRY_RUN_OK", completed.stdout)

    def test_repository_has_real_double_click_install_and_launch_entries(self) -> None:
        install_text = DOUBLE_CLICK_INSTALLER.read_text(encoding="utf-8-sig")
        launch_text = DOUBLE_CLICK_LAUNCHER.read_text(encoding="utf-8-sig")

        self.assertIn("scripts\\install_ozon_v2.ps1", install_text)
        self.assertIn("INSTALL_FAILED", install_text)
        self.assertIn("scripts\\launch_workbench.ps1", launch_text)
        self.assertIn("scripts\\install_ozon_v2.ps1", launch_text)
        self.assertIn(".venv\\Scripts\\python.exe", launch_text)

    def test_codex_skill_installer_copies_all_three_skills_to_an_isolated_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_root = Path(temp_dir) / "skills"
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SKILL_INSTALLER),
                    "-SkillTargetRoot",
                    str(target_root),
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
            )

            self.assertEqual(
                0,
                completed.returncode,
                msg=f"{completed.stdout}\n{completed.stderr}",
            )
            for skill_name in (
                "ozon-product-media-generator",
                "ozon-intelligent-field-drafter",
                "ozon-image-generation-controller",
            ):
                source = ROOT / "skills" / skill_name
                installed = target_root / skill_name
                self.assertTrue((installed / "SKILL.md").is_file())
                source_files = sorted(
                    path.relative_to(source) for path in source.rglob("*") if path.is_file()
                )
                installed_files = sorted(
                    path.relative_to(installed)
                    for path in installed.rglob("*")
                    if path.is_file()
                )
                self.assertEqual(source_files, installed_files)
                for relative_path in source_files:
                    self.assertEqual(
                        (source / relative_path).read_bytes(),
                        (installed / relative_path).read_bytes(),
                    )

    def test_doctor_requires_real_runtime_shortcut_skills_and_health(self) -> None:
        text = DOCTOR.read_text(encoding="utf-8-sig")

        self.assertIn(".venv\\Scripts\\python.exe", text)
        self.assertIn("[char]0x5DE5", text)
        self.assertIn("[char]0x5177", text)
        self.assertIn("[char]0x53F0", text)
        self.assertIn("api/health", text)
        self.assertIn("ozon-product-media-generator", text)
        self.assertIn("ozon-intelligent-field-drafter", text)
        self.assertIn("ozon-image-generation-controller", text)
        self.assertIn("Get-FileHash", text)
        self.assertIn("DOCTOR_PASSED", text)
        self.assertIn("DOCTOR_FAILED", text)

    def test_doctor_rejects_a_skills_only_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            skill_root = temp_root / ".codex" / "skills"
            skill_root.mkdir(parents=True)
            for skill_name in (
                "ozon-product-media-generator",
                "ozon-intelligent-field-drafter",
                "ozon-image-generation-controller",
            ):
                source = ROOT / "skills" / skill_name
                target = skill_root / skill_name
                target.mkdir()
                (target / "SKILL.md").write_bytes((source / "SKILL.md").read_bytes())

            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(DOCTOR),
                    "-ProjectRoot",
                    str(temp_root / "missing-workbench"),
                    "-SkillTargetRoot",
                    str(skill_root),
                    "-ShortcutPath",
                    str(temp_root / "missing.lnk"),
                    "-HealthUrl",
                    "http://127.0.0.1:9/api/health",
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("DOCTOR_FAILED", completed.stdout)
            self.assertNotIn("DOCTOR_PASSED", completed.stdout)

    def test_runtime_dependencies_and_mcp_use_the_installed_venv(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        control = (ROOT / "scripts" / "workbench_control.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("Pillow", pyproject)
        self.assertIn("fastmcp", pyproject)
        self.assertEqual(
            ".venv/Scripts/python.exe",
            mcp["mcpServers"]["ozon-v2"]["command"],
        )
        self.assertIn(".venv\\Scripts\\python.exe", control)

    def test_plugin_exposes_both_skills_as_one_versioned_product(self) -> None:
        plugin = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        self.assertEqual("0.4.2", plugin["version"])
        self.assertEqual("./skills/", plugin["skills"])
        self.assertLessEqual(len(plugin["interface"]["defaultPrompt"]), 3)
        prompts = "\n".join(plugin["interface"]["defaultPrompt"])
        self.assertIn("只安装 Skill 不算完成", prompts)
        self.assertIn("api/health", prompts)
        self.assertIn("$ozon-intelligent-field-drafter", prompts)
        self.assertIn("$ozon-image-generation-controller", prompts)

    def test_readme_links_detailed_installation_and_workflow_guides(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("docs/INSTALLATION.md", readme)
        self.assertIn("docs/USER_GUIDE.md", readme)
        self.assertIn("安装并启动 Ozon V2.cmd", readme)
        self.assertIn("只安装 Skill", readme)


if __name__ == "__main__":
    unittest.main()
