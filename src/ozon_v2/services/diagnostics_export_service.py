from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import utc_now_iso


SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "proxy_password",
    "secret",
    "token",
)
ERROR_EVENT_PARTS = ("error", "failed", "timeout", "blocked", "rejected", "exhausted")


@dataclass(frozen=True)
class DiagnosticArchive:
    filename: str
    content: bytes


class DiagnosticsExportService:
    def __init__(self, repo: FsRepo | None = None) -> None:
        self.repo = repo or FsRepo()

    def build_current_batch_zip(
        self,
        run_id: str,
        runner_status: dict[str, Any],
        browser_bridge: dict[str, Any],
    ) -> DiagnosticArchive:
        run = self.repo.load_run(run_id)
        events = [event.to_dict() for event in self.repo.load_run_events(run_id)]
        errors = [
            event
            for event in events
            if any(part in str(event.get("event_type") or "").lower() for part in ERROR_EVENT_PARTS)
        ]
        run_dir = self.repo.run_dir(run_id)
        files = [
            {
                "path": path.relative_to(run_dir).as_posix(),
                "size_bytes": path.stat().st_size,
            }
            for path in sorted(run_dir.rglob("*"))
            if path.is_file()
        ]
        entries: dict[str, Any] = {
            "run.json": run,
            "events.json": events,
            "runner.json": runner_status,
            "browser_bridge.json": browser_bridge,
            "errors.json": errors,
            "files.json": files,
        }
        summary = {
            "schema_version": 1,
            "scope": "current_batch",
            "run_id": run_id,
            "generated_at": utc_now_iso(),
            "run_status": run.get("status"),
            "runner_state": runner_status.get("state"),
            "browser_stage": browser_bridge.get("stage") or browser_bridge.get("code"),
            "event_count": len(events),
            "error_event_count": len(errors),
            "included_files": ["summary.json", *entries.keys()],
            "privacy": "Credential files and product media are excluded; sensitive values are redacted.",
        }
        known_secrets = self._known_secrets()
        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as bundle:
            for name, payload in {"summary.json": summary, **entries}.items():
                safe_payload = self._redact(payload, known_secrets=known_secrets)
                bundle.writestr(name, json.dumps(safe_payload, ensure_ascii=False, indent=2))
        return DiagnosticArchive(
            filename=f"ozon-v2-diagnostics-{run_id}.zip",
            content=buffer.getvalue(),
        )

    def _known_secrets(self) -> tuple[str, ...]:
        credentials = self.repo.load_credentials()
        values = [str(credentials.api_key)] if credentials and credentials.api_key else []
        return tuple(value for value in values if len(value) >= 4)

    def _redact(self, value: Any, known_secrets: tuple[str, ...], key: str = "") -> Any:
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if normalized_key and any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): self._redact(item_value, known_secrets, str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item, known_secrets) for item in value]
        if isinstance(value, tuple):
            return [self._redact(item, known_secrets) for item in value]
        if not isinstance(value, str):
            return value
        redacted = value
        for secret in known_secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        redacted = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", redacted)
        redacted = re.sub(
            r"(?i)\b(api[-_ ]?key|authorization|cookie|password|proxy[-_ ]?password|secret|token)\b\s*[:=]\s*[^\s,;]+",
            lambda match: f"{match.group(1)}=[REDACTED]",
            redacted,
        )
        redacted = re.sub(r"(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@", redacted)
        return redacted
