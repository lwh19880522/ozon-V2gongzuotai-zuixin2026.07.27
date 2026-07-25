from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_ozon_v2.ps1"


class InstallationContractTests(unittest.TestCase):
    def test_installer_is_portable_idempotent_and_supports_dry_run(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8-sig")

        self.assertIn("[switch]$DryRun", text)
        self.assertIn("[switch]$NoStart", text)
        self.assertIn("[switch]$NoShortcut", text)
        self.assertIn("Split-Path -Parent $PSScriptRoot", text)
        self.assertIn(".venv", text)
        self.assertIn("-m", text)
        self.assertIn("pip", text)
        self.assertIn("install", text)
        self.assertIn("-e", text)
        self.assertIn("create_workbench_shortcut.ps1", text)
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

        self.assertEqual("0.4.0", plugin["version"])
        self.assertEqual("./skills/", plugin["skills"])
        self.assertLessEqual(len(plugin["interface"]["defaultPrompt"]), 3)
        prompts = "\n".join(plugin["interface"]["defaultPrompt"])
        self.assertIn("$ozon-intelligent-field-drafter", prompts)
        self.assertIn("$ozon-image-generation-controller", prompts)

    def test_readme_links_detailed_installation_and_workflow_guides(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("docs/INSTALLATION.md", readme)
        self.assertIn("docs/USER_GUIDE.md", readme)


if __name__ == "__main__":
    unittest.main()
