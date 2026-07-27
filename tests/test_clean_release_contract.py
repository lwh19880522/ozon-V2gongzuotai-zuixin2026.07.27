from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NEW_REPOSITORY = "https://github.com/lwh19880522/ozon-V2gongzuotai-zuixin2026.07.27"
OLD_REPOSITORY = (
    "https://github.com/lwh19880522/" + "OZON-gongjutai-2026.07.18"
)
RELEASE_BUILDER = ROOT / "scripts" / "build_clean_release.py"


class CleanReleaseContractTests(unittest.TestCase):
    def test_public_metadata_points_only_to_the_new_private_repository(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        installation = (ROOT / "docs" / "INSTALLATION.md").read_text(
            encoding="utf-8"
        )
        plugin = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        combined = "\n".join((readme, installation, plugin["repository"]))
        self.assertIn(NEW_REPOSITORY, combined)
        self.assertNotIn(OLD_REPOSITORY, combined)
        self.assertEqual(NEW_REPOSITORY, plugin["repository"])

    def test_clean_release_builder_exports_the_complete_installable_product(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            release_root = Path(temp_dir) / "release"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RELEASE_BUILDER),
                    "--destination",
                    str(release_root),
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(
                0,
                completed.returncode,
                msg=f"{completed.stdout}\n{completed.stderr}",
            )
            required_paths = (
                "README.md",
                ".gitattributes",
                "docs/INSTALLATION.md",
                "docs/USER_GUIDE.md",
                "docs/RELEASE_AND_PRIVACY.md",
                "安装并启动 Ozon V2.cmd",
                "启动 Ozon V2.cmd",
                "scripts/install_ozon_v2.ps1",
                "scripts/launch_workbench.ps1",
                "scripts/verify_ozon_v2_install.ps1",
                "browser_extension/ozon_v2_bridge/manifest.json",
                ".codex-plugin/plugin.json",
                ".mcp.json",
                "skills/ozon-intelligent-field-drafter/SKILL.md",
                "skills/ozon-image-generation-controller/SKILL.md",
                "skills/ozon-product-media-generator/SKILL.md",
                "skills/ozon-product-media-generator/assets/ozon-commercial-infographic-core-prompt.txt",
                "assets/seed_pool/seed_pool.initial.json",
                "src/ozon_v2/workbench/local_server.py",
                "tests/test_installation_contract.py",
                "RELEASE_MANIFEST.json",
            )
            for relative_path in required_paths:
                self.assertTrue(
                    (release_root / relative_path).is_file(),
                    msg=f"missing release file: {relative_path}",
                )

            for excluded_path in (
                ".git",
                ".venv",
                "runtime",
                "tmp",
                "work",
                "obsidian",
                "docs/superpowers",
                "src/ozon_v2_ops_controller.egg-info",
            ):
                self.assertFalse(
                    (release_root / excluded_path).exists(),
                    msg=f"private or historical path leaked: {excluded_path}",
                )

            manifest = json.loads(
                (release_root / "RELEASE_MANIFEST.json").read_text(encoding="utf-8")
            )
            self.assertEqual(NEW_REPOSITORY, manifest["repository"])
            self.assertGreater(manifest["file_count"], 100)
            self.assertIn("PRIVACY_SCAN_PASSED", completed.stdout)
            self.assertEqual(
                "* -text\n",
                (release_root / ".gitattributes").read_text(encoding="utf-8"),
            )

    def test_readme_links_installation_workflow_and_privacy_documents(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("docs/INSTALLATION.md", readme)
        self.assertIn("docs/USER_GUIDE.md", readme)
        self.assertIn("docs/RELEASE_AND_PRIVACY.md", readme)
        self.assertIn("安装并启动 Ozon V2.cmd", readme)
        self.assertIn("build_clean_release.py", readme)

    def test_privacy_scan_rejects_a_machine_specific_user_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            private_fragment = "C:" + "\\Users\\" + "private-user\\AppData\\secret.txt"
            (fixture_root / "leak.txt").write_text(
                private_fragment,
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(RELEASE_BUILDER),
                    "--scan-only",
                    str(fixture_root),
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("PRIVACY_SCAN_FAILED", completed.stdout)


if __name__ == "__main__":
    unittest.main()
