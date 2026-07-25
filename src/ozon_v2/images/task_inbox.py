from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ImageTaskInboxError(RuntimeError):
    pass


class ImageTaskInbox:
    def __init__(self, runtime_root: str | Path) -> None:
        self.root = Path(runtime_root) / "image_tasks"

    def directory(self, status: str) -> Path:
        if status not in {"pending", "in_progress", "completed", "failed"}:
            raise ImageTaskInboxError(f"Unsupported image task status: {status}")
        path = self.root / status
        path.mkdir(parents=True, exist_ok=True)
        return path

    def status(self) -> dict[str, Any]:
        counts = {
            status: len(list(self.directory(status).glob("*.json")))
            for status in ("pending", "in_progress", "completed", "failed")
        }
        return {"root": str(self.root.resolve()), "counts": counts}

    def claim_next(self, worker_id: str) -> dict[str, Any] | None:
        worker = _worker_id(worker_id)
        pending = self.directory("pending")
        in_progress = self.directory("in_progress")
        for source in sorted(pending.glob("*.json"), key=lambda path: path.name):
            target = in_progress / source.name
            try:
                source.replace(target)
            except FileNotFoundError:
                continue
            payload = _read_json(target)
            if payload.get("status") != "pending":
                _write_json(target, payload)
                raise ImageTaskInboxError(
                    f"Package {source.stem} is not pending: {payload.get('status')}"
                )
            payload["status"] = "in_progress"
            payload["assignment"] = {
                "worker_id": worker,
                "claimed_at": _utc_now(),
            }
            _write_json(target, payload)
            return {**payload, "package_path": str(target.resolve())}
        return None

    def complete(
        self,
        package_id: str,
        worker_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        payload, source = self._owned_package(package_id, worker_id)
        payload["status"] = "completed"
        payload["completed_at"] = _utc_now()
        payload["result"] = dict(receipt)
        target = self.directory("completed") / source.name
        _write_json(target, payload)
        source.unlink()
        return {**payload, "package_path": str(target.resolve())}

    def fail(
        self,
        package_id: str,
        worker_id: str,
        reason: str,
    ) -> dict[str, Any]:
        payload, source = self._owned_package(package_id, worker_id)
        payload["status"] = "failed"
        payload["failed_at"] = _utc_now()
        payload["failure"] = {"reason": str(reason or "").strip() or "unknown"}
        target = self.directory("failed") / source.name
        _write_json(target, payload)
        source.unlink()
        return {**payload, "package_path": str(target.resolve())}

    def load_in_progress(self, package_id: str, worker_id: str) -> dict[str, Any]:
        payload, source = self._owned_package(package_id, worker_id)
        return {**payload, "package_path": str(source.resolve())}

    def _owned_package(
        self,
        package_id: str,
        worker_id: str,
    ) -> tuple[dict[str, Any], Path]:
        safe_id = _package_id(package_id)
        worker = _worker_id(worker_id)
        source = self.directory("in_progress") / f"{safe_id}.json"
        if not source.is_file():
            raise ImageTaskInboxError(f"In-progress package is missing: {safe_id}")
        payload = _read_json(source)
        owner = str((payload.get("assignment") or {}).get("worker_id") or "")
        if owner != worker:
            raise ImageTaskInboxError(
                f"Package {safe_id} belongs to {owner or 'no worker'}, not {worker}"
            )
        return payload, source


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImageTaskInboxError(f"Invalid image task package: {path}") from exc
    if not isinstance(payload, dict):
        raise ImageTaskInboxError(f"Invalid image task package object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _package_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        raise ImageTaskInboxError("Invalid image task package ID.")
    return text


def _worker_id(value: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"ozon-image-worker-(0[1-9]|10)", text):
        raise ImageTaskInboxError("Worker must be one of ozon-image-worker-01..10.")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
