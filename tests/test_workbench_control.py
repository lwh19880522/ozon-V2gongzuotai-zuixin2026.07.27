from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_SCRIPT = PROJECT_ROOT / "scripts" / "workbench_control.ps1"
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_workbench.py"
LAUNCHER_SCRIPT = PROJECT_ROOT / "scripts" / "launch_workbench.ps1"
RESTART_HELPER_SCRIPT = PROJECT_ROOT / "scripts" / "workbench_restart_helper.ps1"
SHORTCUT_SCRIPT = PROJECT_ROOT / "scripts" / "create_workbench_shortcut.ps1"


def unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class StopFailureHandler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self.send_json(200, {"ok": True, "code": "health.ok"})
            return
        self.send_json(404, {"ok": False})

    def do_POST(self) -> None:
        if self.path == "/api/runtime/stop":
            self.send_json(503, {"ok": False, "code": "stop.unavailable"})
            return
        self.send_json(404, {"ok": False})

    def log_message(self, format: str, *args: object) -> None:
        return


class WorkbenchControlScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_dir = Path(self.temp_dir.name) / "runtime"
        self.state_file = self.runtime_dir / "workbench.json"
        self.port = unused_local_port()

    def tearDown(self) -> None:
        self.run_control("Stop", check=False)
        self.temp_dir.cleanup()

    def run_control(
        self, action: str, *, check: bool = True, timeout: float = 25
    ) -> subprocess.CompletedProcess[str]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(CONTROL_SCRIPT),
            "-Action",
            action,
            "-Port",
            str(self.port),
            "-RuntimeState",
            str(self.runtime_dir),
        ]
        stdout_path = self.runtime_dir / f"test-{action.lower()}-{time.time_ns()}.stdout.log"
        stderr_path = self.runtime_dir / f"test-{action.lower()}-{time.time_ns()}.stderr.log"
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout,
            )
        result = subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout_path.read_bytes().decode("utf-8", errors="replace"),
            stderr_path.read_bytes().decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            self.fail(f"{action} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    def health_ok(self) -> bool:
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=1) as response:
                return json.loads(response.read().decode("utf-8")).get("code") == "health.ok"
        except (OSError, URLError, TimeoutError, json.JSONDecodeError):
            return False

    def wait_until_stopped(self) -> None:
        deadline = time.time() + 8
        while time.time() < deadline:
            if not self.health_ok():
                return
            time.sleep(0.1)
        self.fail("Workbench health endpoint was still online after stop.")

    def test_start_is_idempotent_and_stale_record_does_not_block(self) -> None:
        self.runtime_dir.mkdir(parents=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": self.port,
                    "project_root": "C:/wrong-project",
                    "status": "running",
                }
            ),
            encoding="utf-8",
        )

        first = self.run_control("Start")
        first_state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
        second = self.run_control("Start")
        second_state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))

        self.assertIn("STARTED", first.stdout)
        self.assertIn("ALREADY_RUNNING", second.stdout)
        self.assertEqual(first_state["pid"], second_state["pid"])
        self.assertEqual(self.port, first_state["port"])
        self.assertEqual(str(PROJECT_ROOT.resolve()).lower(), str(first_state["project_root"]).lower())
        self.assertTrue(self.health_ok())

    def test_stop_releases_health_endpoint_and_is_idempotent(self) -> None:
        self.run_control("Start")

        stopped = self.run_control("Stop")
        self.wait_until_stopped()
        stopped_again = self.run_control("Stop")

        self.assertIn("STOPPED", stopped.stdout)
        self.assertIn("NOT_RUNNING", stopped_again.stdout)

    def test_runtime_api_restart_replaces_pid_and_helper_exits(self) -> None:
        self.run_control("Start")
        previous_pid = json.loads(self.state_file.read_text(encoding="utf-8-sig"))["pid"]
        request = Request(
            f"http://127.0.0.1:{self.port}/api/runtime/restart",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            scheduled = json.loads(response.read().decode("utf-8"))

        deadline = time.time() + 30
        restart_status_file = self.runtime_dir / "restart-status.json"
        latest_state = {}
        restart_status = {}
        while time.time() < deadline:
            if self.state_file.exists():
                latest_state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
            if restart_status_file.exists():
                restart_status = json.loads(restart_status_file.read_text(encoding="utf-8-sig"))
            if (
                self.health_ok()
                and latest_state.get("pid") not in {None, previous_pid}
                and restart_status.get("status") == "succeeded"
            ):
                break
            time.sleep(0.2)

        self.assertEqual("runtime.restart_scheduled", scheduled["code"])
        self.assertTrue(self.health_ok())
        self.assertNotEqual(previous_pid, latest_state.get("pid"))
        self.assertEqual("succeeded", restart_status.get("status"))

    def test_launcher_and_shortcut_are_one_safe_entry(self) -> None:
        launcher = LAUNCHER_SCRIPT.read_text(encoding="utf-8-sig")
        shortcut = SHORTCUT_SCRIPT.read_text(encoding="utf-8-sig")
        restart_helper = RESTART_HELPER_SCRIPT.read_text(encoding="utf-8-sig")

        self.assertIn("workbench_control.ps1", launcher)
        self.assertIn("NoOpen", launcher)
        self.assertNotIn("--user-data-dir", launcher)
        self.assertNotIn("--load-extension", launcher)
        self.assertIn("Ozon V2 工具台.lnk", shortcut)
        self.assertIn("launch_workbench.ps1", shortcut)
        self.assertNotIn("startup", shortcut.lower())
        self.assertIn("OpenEdgeAfterRestart", restart_helper)
        self.assertIn("msedge.exe", restart_helper)
        self.assertIn("http://127.0.0.1:$Port/", restart_helper)

    def test_launcher_no_open_starts_healthy_service(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.runtime_dir / "launcher-test.stdout.log"
        stderr_path = self.runtime_dir / "launcher-test.stderr.log"
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER_SCRIPT),
            "-Port",
            str(self.port),
            "-RuntimeState",
            str(self.runtime_dir),
            "-NoOpen",
        ]
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=25,
            )
        stdout = stdout_path.read_bytes().decode("utf-8", errors="replace")
        stderr = stderr_path.read_bytes().decode("utf-8", errors="replace")

        self.assertEqual(0, completed.returncode, msg=f"{stdout}\n{stderr}")
        self.assertIn("WORKBENCH_READY", stdout)
        self.assertTrue(self.health_ok())

    def test_stop_refuses_pid_from_another_process(self) -> None:
        self.runtime_dir.mkdir(parents=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": self.port,
                    "project_root": str(PROJECT_ROOT.resolve()),
                    "status": "running",
                }
            ),
            encoding="utf-8",
        )

        result = self.run_control("Stop", check=False)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("STOP_BLOCKED_UNEXPECTED_PROCESS", result.stdout)
        self.state_file.unlink(missing_ok=True)

    def test_stop_with_stopped_state_does_not_inspect_reused_pid(self) -> None:
        self.runtime_dir.mkdir(parents=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": self.port,
                    "project_root": str(PROJECT_ROOT.resolve()),
                    "started_at": "2026-07-19T00:00:00.0000000Z",
                    "status": "stopped",
                }
            ),
            encoding="utf-8",
        )

        result = self.run_control("Stop", check=False)

        self.assertEqual(0, result.returncode, msg=f"{result.stdout}\n{result.stderr}")
        self.assertIn("NOT_RUNNING", result.stdout)

    def test_stop_with_invalid_state_port_is_not_blocked(self) -> None:
        self.runtime_dir.mkdir(parents=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "pid": 0,
                    "port": "not-a-port",
                    "project_root": str(PROJECT_ROOT.resolve()),
                    "status": "stopped",
                }
            ),
            encoding="utf-8",
        )

        result = self.run_control("Stop", check=False)

        self.assertEqual(0, result.returncode, msg=f"{result.stdout}\n{result.stderr}")
        self.assertIn("NOT_RUNNING", result.stdout)
        self.assertNotIn("Cannot convert value", result.stderr)

    def test_stop_with_invalid_state_pid_normalizes_to_zero(self) -> None:
        self.runtime_dir.mkdir(parents=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "pid": "not-a-pid",
                    "port": self.port,
                    "project_root": str(PROJECT_ROOT.resolve()),
                    "status": "running",
                }
            ),
            encoding="utf-8",
        )

        result = self.run_control("Stop", check=False)
        state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))

        self.assertEqual(0, result.returncode, msg=f"{result.stdout}\n{result.stderr}")
        self.assertIn("NOT_RUNNING", result.stdout)
        self.assertNotIn("Cannot convert value", result.stderr)
        self.assertEqual(0, state["pid"])

    def test_stop_fails_when_health_remains_online_after_stop_api_failure(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", self.port), StopFailureHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            deadline = time.time() + 2
            while time.time() < deadline and not self.health_ok():
                time.sleep(0.05)
            self.assertTrue(self.health_ok())
            self.runtime_dir.mkdir(parents=True)
            self.state_file.write_text(
                json.dumps(
                    {
                        "pid": 0,
                        "port": self.port,
                        "project_root": str(PROJECT_ROOT.resolve()),
                        "status": "stopped",
                        "updated_at": "sentinel",
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_control("Stop", check=False)
            state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))

            self.assertNotEqual(0, result.returncode, msg=f"{result.stdout}\n{result.stderr}")
            self.assertIn("STOP_FAILED HEALTH_STILL_ONLINE", result.stdout)
            self.assertEqual("sentinel", state["updated_at"])
            self.assertTrue(self.health_ok())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_forced_stop_fails_when_health_remains_online(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", self.port), StopFailureHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        dummy = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                str(START_SCRIPT),
                "--port",
                str(self.port),
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        server_thread.start()
        try:
            deadline = time.time() + 2
            while time.time() < deadline and not self.health_ok():
                time.sleep(0.05)
            self.assertTrue(self.health_ok())
            self.runtime_dir.mkdir(parents=True)
            self.state_file.write_text(
                json.dumps(
                    {
                        "pid": dummy.pid,
                        "port": self.port,
                        "project_root": str(PROJECT_ROOT.resolve()),
                        "status": "running",
                        "updated_at": "sentinel",
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_control("Stop", check=False, timeout=60)
            state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))

            self.assertNotEqual(0, result.returncode, msg=f"{result.stdout}\n{result.stderr}")
            self.assertIn("STOP_FAILED HEALTH_STILL_ONLINE", result.stdout)
            self.assertEqual("sentinel", state["updated_at"])
            self.assertEqual("running", state["status"])
            self.assertTrue(self.health_ok())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            if dummy.poll() is None:
                dummy.terminate()
                try:
                    dummy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    dummy.kill()
                    dummy.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()




