from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any


class WorkbenchRuntimeController:
    """Coordinates one-shot stop and restart actions for the local workbench."""

    def __init__(self, project_root: Path, runtime_dir: Path, port: int) -> None:
        self.project_root = Path(project_root).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        self.port = int(port)
        self.state_path = self.runtime_dir / "workbench.json"
        self._lock = threading.Lock()
        self._scheduled_action: str | None = None

    def mark_running(self, pid: int) -> None:
        now = self._utc_now()
        self._write_state(
            {
                "pid": int(pid),
                "port": self.port,
                "project_root": str(self.project_root),
                "started_at": now,
                "updated_at": now,
                "status": "running",
            }
        )

    def mark_stopped(self) -> None:
        current = self._read_state()
        current.update(
            {
                "pid": int(current.get("pid") or os.getpid()),
                "port": self.port,
                "project_root": str(self.project_root),
                "started_at": current.get("started_at") or self._utc_now(),
                "updated_at": self._utc_now(),
                "status": "stopped",
            }
        )
        self._write_state(current)

    def schedule(
        self,
        action: str,
        server: Any,
        *,
        open_edge_after_restart: bool = False,
    ) -> dict[str, Any]:
        if action not in {"stop", "restart"}:
            raise ValueError(f"Unsupported runtime action: {action}")
        with self._lock:
            if self._scheduled_action is not None:
                return {
                    "ok": True,
                    "code": "runtime.action_in_progress",
                    "message": f"Runtime {self._scheduled_action} is already in progress.",
                    "data": {"action": self._scheduled_action},
                    "errors": [],
                }
            self._scheduled_action = action

        current = self._read_state()
        pid = int(current.get("pid") or os.getpid())
        current.update(
            {
                "pid": pid,
                "port": self.port,
                "project_root": str(self.project_root),
                "started_at": current.get("started_at") or self._utc_now(),
                "updated_at": self._utc_now(),
                "status": "restarting" if action == "restart" else "stopping",
            }
        )
        self._write_state(current)
        if action == "restart":
            self._spawn_restart_helper(pid, open_edge_after_restart=open_edge_after_restart)

        threading.Thread(
            target=self._shutdown_after_response,
            args=(server,),
            name=f"ozon-workbench-{action}",
            daemon=True,
        ).start()
        return {
            "ok": True,
            "code": f"runtime.{action}_scheduled",
            "message": f"Runtime {action} scheduled.",
            "data": {"action": action, "pid": pid, "port": self.port},
            "errors": [],
        }

    def _spawn_restart_helper(self, pid: int, *, open_edge_after_restart: bool) -> None:
        helper = self.project_root / "scripts" / "workbench_restart_helper.py"
        command = [
            sys.executable,
            str(helper),
            "--wait-for-pid",
            str(pid),
            "--port",
            str(self.port),
            "--runtime-state",
            str(self.runtime_dir),
            "--project-root",
            str(self.project_root),
        ]
        if open_edge_after_restart:
            command.append("--open-edge-after-restart")
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )

    @staticmethod
    def _shutdown_after_response(server: Any) -> None:
        time.sleep(0.15)
        server.shutdown()

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = self.runtime_dir / f"workbench.{os.getpid()}.{threading.get_ident()}.tmp"
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, self.state_path)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
