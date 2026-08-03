from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


def _health_ok(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("ok") is True
    except (OSError, URLError, json.JSONDecodeError):
        return False


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restart Ozon V2 after the current server exits")
    parser.add_argument("--wait-for-pid", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--runtime-state", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--open-edge-after-restart", action="store_true")
    args = parser.parse_args()
    status_path = args.runtime_state / "restart-status.json"
    base = {"port": args.port, "previous_pid": args.wait_for_pid}
    _write_status(status_path, {**base, "status": "waiting_for_exit", "attempt": 0})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _health_ok(args.port):
        time.sleep(0.15)
    if _health_ok(args.port):
        _write_status(status_path, {**base, "status": "failed", "attempt": 0, "details": "Previous workbench did not release its health endpoint."})
        return 1

    launcher = args.project_root / "scripts" / "workbench_launcher.py"
    for attempt in range(1, 3):
        command = [
            sys.executable,
            str(launcher),
            "--no-open",
            "--port",
            str(args.port),
            "--runtime-state",
            str(args.runtime_state),
        ]
        completed = subprocess.run(command, cwd=args.project_root, timeout=20, check=False)
        if completed.returncode == 0:
            if args.open_edge_after_restart:
                webbrowser.open(f"http://127.0.0.1:{args.port}/", new=1)
            _write_status(status_path, {**base, "status": "succeeded", "attempt": attempt})
            return 0
        _write_status(status_path, {**base, "status": "retrying", "attempt": attempt})
        time.sleep(0.5)
    _write_status(status_path, {**base, "status": "failed", "attempt": 2, "details": "Restart failed after one retry."})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
