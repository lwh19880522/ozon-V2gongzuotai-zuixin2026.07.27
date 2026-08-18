from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import webbrowser


PROJECT_ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_workbench.py"


def _health_ok(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("ok") is True
    except (OSError, URLError, json.JSONDecodeError):
        return False


def _wait_for_health(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok(port):
            return True
        time.sleep(0.2)
    return False


def _server_python() -> Path:
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        return venv_python
    current = Path(sys.executable).resolve()
    if current.name.casefold() == "pythonw.exe":
        console_python = current.with_name("python.exe")
        if console_python.is_file():
            return console_python
    return current


def _append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")


def _show_error(message: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Ozon V2 工具台", 0x10)
    except (AttributeError, OSError):
        return


def start_workbench(port: int, runtime_state: Path, *, open_browser: bool) -> int:
    runtime_state.mkdir(parents=True, exist_ok=True)
    launcher_log = runtime_state / "launcher.log"
    url = f"http://127.0.0.1:{port}/"
    if not _health_ok(port):
        env = dict(os.environ)
        src = str(PROJECT_ROOT / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        command = [
            str(_server_python()),
            str(START_SCRIPT),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--runtime-dir",
            str(runtime_state),
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        stdout_path = runtime_state / "server.stdout.log"
        stderr_path = runtime_state / "server.stderr.log"
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=creationflags,
                close_fds=True,
            )
        _append_log(launcher_log, f"start.spawned pid={process.pid} port={port}")
        if not _wait_for_health(port, 12):
            _append_log(launcher_log, f"start.failed pid={process.pid} port={port}")
            _show_error(f"工具台启动失败。请查看日志：\n{launcher_log}")
            return 1
    _append_log(launcher_log, f"start.ready port={port}")
    print(f"WORKBENCH_READY {url}", flush=True)
    if open_browser:
        webbrowser.open(url, new=1)
    return 0


def stop_workbench(port: int, runtime_state: Path, timeout: float = 8) -> int:
    if not _health_ok(port):
        return 0
    try:
        auth_token = (runtime_state / "api_auth_token").read_text(encoding="utf-8").strip()
    except OSError:
        return 1
    if len(auth_token) < 32:
        return 1
    request = Request(
        f"http://127.0.0.1:{port}/api/runtime/stop",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Ozon-Workbench-Token": auth_token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=3):
            pass
    except (OSError, URLError):
        return 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _health_ok(port):
            return 0
        time.sleep(0.2)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe Ozon V2 workbench launcher")
    parser.add_argument("--action", choices=("start", "stop", "restart", "status"), default="start")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runtime-state", type=Path)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    runtime_state = (args.runtime_state or PROJECT_ROOT / "runtime" / "workbench").resolve()
    if args.action == "status":
        return 0 if _health_ok(args.port) else 1
    if args.action == "stop":
        return stop_workbench(args.port, runtime_state)
    if args.action == "restart" and stop_workbench(args.port, runtime_state) != 0:
        return 1
    return start_workbench(args.port, runtime_state, open_browser=not args.no_open)


if __name__ == "__main__":
    raise SystemExit(main())
