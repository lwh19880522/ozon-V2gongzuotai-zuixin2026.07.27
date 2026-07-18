from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ozon_v2.workbench.runtime_control import WorkbenchRuntimeController


class FakeServer:
    def __init__(self) -> None:
        self.shutdown_called = threading.Event()

    def shutdown(self) -> None:
        self.shutdown_called.set()


class WorkbenchRuntimeControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name) / "project"
        (self.project_root / "scripts").mkdir(parents=True)
        (self.project_root / "scripts" / "workbench_restart_helper.ps1").write_text("# helper", encoding="utf-8")
        self.runtime_dir = self.project_root / "runtime" / "workbench"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_mark_running_and_stopped_update_runtime_state(self) -> None:
        controller = WorkbenchRuntimeController(self.project_root, self.runtime_dir, port=8765)

        controller.mark_running(4321)
        running = json.loads((self.runtime_dir / "workbench.json").read_text(encoding="utf-8"))
        controller.mark_stopped()
        stopped = json.loads((self.runtime_dir / "workbench.json").read_text(encoding="utf-8"))

        self.assertEqual("running", running["status"])
        self.assertEqual(4321, running["pid"])
        self.assertEqual(8765, running["port"])
        self.assertEqual(str(self.project_root), running["project_root"])
        self.assertEqual("stopped", stopped["status"])
        self.assertEqual(4321, stopped["pid"])

    def test_restart_spawns_one_helper_and_schedules_shutdown(self) -> None:
        controller = WorkbenchRuntimeController(self.project_root, self.runtime_dir, port=8765)
        controller.mark_running(4321)
        server = FakeServer()

        with patch("ozon_v2.workbench.runtime_control.subprocess.Popen") as popen:
            first = controller.schedule("restart", server)
            second = controller.schedule("restart", server)

        self.assertTrue(server.shutdown_called.wait(timeout=1))
        self.assertEqual("runtime.restart_scheduled", first["code"])
        self.assertEqual("runtime.action_in_progress", second["code"])
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn(str(self.project_root / "scripts" / "workbench_restart_helper.ps1"), command)
        self.assertIn("4321", command)
        self.assertIn("8765", command)
        state = json.loads((self.runtime_dir / "workbench.json").read_text(encoding="utf-8"))
        self.assertEqual("restarting", state["status"])

    def test_stop_schedules_shutdown_without_spawning_helper(self) -> None:
        controller = WorkbenchRuntimeController(self.project_root, self.runtime_dir, port=8765)
        controller.mark_running(4321)
        server = FakeServer()

        with patch("ozon_v2.workbench.runtime_control.subprocess.Popen") as popen:
            result = controller.schedule("stop", server)

        self.assertTrue(server.shutdown_called.wait(timeout=1))
        self.assertEqual("runtime.stop_scheduled", result["code"])
        popen.assert_not_called()
        state = json.loads((self.runtime_dir / "workbench.json").read_text(encoding="utf-8"))
        self.assertEqual("stopping", state["status"])

    def test_restart_helper_is_bounded_and_never_opens_browser(self) -> None:
        helper_path = Path(__file__).resolve().parents[1] / "scripts" / "workbench_restart_helper.ps1"

        helper = helper_path.read_text(encoding="utf-8-sig")

        self.assertIn("$attempt -le 2", helper)
        self.assertIn("workbench_control.ps1", helper)
        self.assertIn("restart-status.json", helper)
        self.assertNotIn("msedge", helper.lower())
        self.assertNotIn("start-process http", helper.lower())

    def test_unknown_action_is_rejected(self) -> None:
        controller = WorkbenchRuntimeController(self.project_root, self.runtime_dir, port=8765)

        with self.assertRaises(ValueError):
            controller.schedule("launch", FakeServer())


if __name__ == "__main__":
    unittest.main()



