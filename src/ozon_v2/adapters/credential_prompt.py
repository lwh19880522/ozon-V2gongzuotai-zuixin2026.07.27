from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import utc_now_iso

DEFAULT_CREDENTIAL_ASSISTANT_COOLDOWN_SECONDS = 60 * 60


class ProcessHandle(Protocol):
    pid: int


PopenFactory = Callable[..., ProcessHandle]
ProcessAliveChecker = Callable[[int], bool]


@dataclass(frozen=True)
class CredentialAssistantLaunch:
    code: str
    message: str
    state: str
    state_path: str
    credentials_path: str
    template_path: str
    pid: int | None = None
    opened_at: str | None = None
    cooldown_until: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "state": self.state,
            "state_path": self.state_path,
            "credentials_path": self.credentials_path,
            "template_path": self.template_path,
            "pid": self.pid,
            "opened_at": self.opened_at,
            "cooldown_until": self.cooldown_until,
            "error": self.error,
        }


class CredentialPromptLauncher:
    def __init__(
        self,
        repo: FsRepo | None = None,
        *,
        popen_factory: PopenFactory | None = None,
        process_is_alive: ProcessAliveChecker | None = None,
        cooldown_seconds: int = DEFAULT_CREDENTIAL_ASSISTANT_COOLDOWN_SECONDS,
    ) -> None:
        self.repo = repo or FsRepo()
        self.popen_factory = popen_factory or subprocess.Popen
        self.process_is_alive = process_is_alive or is_process_alive
        self.cooldown_seconds = cooldown_seconds

    def open_or_focus(self, *, force: bool = False) -> CredentialAssistantLaunch:
        self.repo.initialize_runtime()
        credential_status = self.repo.credential_status().to_dict()
        base = {
            "state_path": str(self.repo.credential_assistant_state_path),
            "credentials_path": credential_status["credentials_path"],
            "template_path": credential_status["template_path"],
        }
        if credential_status["configured"]:
            return CredentialAssistantLaunch(
                code="credential_assistant.not_needed",
                message="Seller credentials are already configured.",
                state="not_needed",
                **base,
            )

        state = self._load_state()
        pid = _safe_int(state.get("pid"))
        opened_at = state.get("opened_at") if isinstance(state.get("opened_at"), str) else None
        if not force and pid and self.process_is_alive(pid):
            return CredentialAssistantLaunch(
                code="credential_assistant.already_open",
                message="Credential assistant is already open; not opening another popup.",
                state="already_open",
                pid=pid,
                opened_at=opened_at,
                **base,
            )

        cooldown_until = self._cooldown_until(state)
        if not force and cooldown_until:
            return CredentialAssistantLaunch(
                code="credential_assistant.cooldown",
                message="Credential assistant was opened recently; not opening a repeated popup.",
                state="cooldown",
                pid=pid,
                opened_at=opened_at,
                cooldown_until=cooldown_until,
                **base,
            )

        try:
            process = self._launch_window()
        except Exception as exc:  # pragma: no cover - defensive around local OS process launch.
            error_state = {
                "state": "launch_failed",
                "error": str(exc),
                "opened_at": utc_now_iso(),
            }
            self._write_state(error_state)
            return CredentialAssistantLaunch(
                code="credential_assistant.launch_failed",
                message="Credential assistant popup could not be opened.",
                state="launch_failed",
                error=str(exc),
                opened_at=error_state["opened_at"],
                **base,
            )

        opened_at = utc_now_iso()
        launch_state = {
            "state": "open",
            "pid": process.pid,
            "opened_at": opened_at,
            "credentials_path": credential_status["credentials_path"],
            "template_path": credential_status["template_path"],
            "cooldown_seconds": self.cooldown_seconds,
        }
        self._write_state(launch_state)
        return CredentialAssistantLaunch(
            code="credential_assistant.opened",
            message="Credential assistant popup opened. It will not be opened again while active.",
            state="opened",
            pid=process.pid,
            opened_at=opened_at,
            **base,
        )

    def _launch_window(self) -> ProcessHandle:
        script_path = Path(__file__).with_name("credential_prompt_window.py")
        env = os.environ.copy()
        src_path = str(self.repo.context.project_root / "src")
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_path if not existing_pythonpath else src_path + os.pathsep + existing_pythonpath
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if _python_executable().name.lower() != "pythonw.exe":
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return self.popen_factory(
            [str(_python_executable()), str(script_path), "--runtime-root", str(self.repo.runtime_root)],
            cwd=str(self.repo.context.project_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )

    def _load_state(self) -> dict[str, Any]:
        path = self.repo.credential_assistant_state_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        path = self.repo.credential_assistant_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _cooldown_until(self, state: dict[str, Any]) -> str | None:
        if self.cooldown_seconds <= 0:
            return None
        opened_at = state.get("opened_at")
        if not isinstance(opened_at, str):
            return None
        try:
            opened = datetime.fromisoformat(opened_at)
        except ValueError:
            return None
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        until = opened + timedelta(seconds=self.cooldown_seconds)
        now = datetime.now(timezone.utc)
        if now < until:
            return until.isoformat()
        return None


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _is_windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_windows_process_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _python_executable() -> Path:
    executable = Path(sys.executable)
    if os.name == "nt" and executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
    return executable


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
