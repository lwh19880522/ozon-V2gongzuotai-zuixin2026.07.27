from __future__ import annotations

import json
import os
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

    def claim_next(self) -> dict[str, Any] | None:
        pending = self.directory("pending")
        in_progress = self.directory("in_progress")
        lock_path = self.root / ".claim.lock"
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        try:
            if any(in_progress.glob("*.json")):
                return None
            candidates: list[tuple[Path, dict[str, Any]]] = []
            for source in pending.glob("*.json"):
                payload = _read_json(source)
                _validate_package_contract(payload, source.stem)
                candidates.append((source, payload))
            if not candidates:
                return None
            _, oldest_payload = min(
                candidates,
                key=lambda item: (
                    str(item[1].get("created_at") or ""),
                    item[0].name,
                ),
            )
            active_run_id = str(oldest_payload.get("run_id") or "")
            source, payload = min(
                (
                    item
                    for item in candidates
                    if str(item[1].get("run_id") or "") == active_run_id
                ),
                key=lambda item: (
                    str(item[1].get("created_at") or ""),
                    item[0].name,
                ),
            )
            target = in_progress / source.name
            try:
                source.replace(target)
            except FileNotFoundError:
                return None
            if payload.get("status") != "pending":
                _write_json(target, payload)
                raise ImageTaskInboxError(
                    f"Package {source.stem} is not pending: {payload.get('status')}"
                )
            payload["status"] = "in_progress"
            payload["assignment"] = {
                "executor": "ozon-product-media-generator",
                "execution_mode": "single_thread",
                "claimed_at": _utc_now(),
            }
            _write_json(target, payload)
            return {**payload, "package_path": str(target.resolve())}
        finally:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)

    def complete(
        self,
        package_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        payload, source = self._claimed_package(package_id)
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
        reason: str,
    ) -> dict[str, Any]:
        payload, source = self._claimed_package(package_id)
        payload["status"] = "failed"
        payload["failed_at"] = _utc_now()
        payload["failure"] = {"reason": str(reason or "").strip() or "unknown"}
        target = self.directory("failed") / source.name
        _write_json(target, payload)
        source.unlink()
        return {**payload, "package_path": str(target.resolve())}

    def release(
        self,
        package_id: str,
        reason: str,
    ) -> dict[str, Any]:
        payload, source = self._claimed_package(package_id)
        payload["status"] = "pending"
        payload["last_release"] = {
            "executor": "ozon-product-media-generator",
            "released_at": _utc_now(),
            "reason": str(reason or "").strip() or "released",
        }
        payload.pop("assignment", None)
        target = self.directory("pending") / source.name
        _write_json(target, payload)
        source.unlink()
        return {**payload, "package_path": str(target.resolve())}

    def load_in_progress(self, package_id: str) -> dict[str, Any]:
        payload, source = self._claimed_package(package_id)
        _validate_package_contract(payload, source.stem)
        return {**payload, "package_path": str(source.resolve())}

    def _claimed_package(self, package_id: str) -> tuple[dict[str, Any], Path]:
        safe_id = _package_id(package_id)
        source = self.directory("in_progress") / f"{safe_id}.json"
        if not source.is_file():
            raise ImageTaskInboxError(f"In-progress package is missing: {safe_id}")
        payload = _read_json(source)
        assignment = payload.get("assignment") or {}
        if (
            assignment.get("executor") != "ozon-product-media-generator"
            or assignment.get("execution_mode") != "single_thread"
        ):
            raise ImageTaskInboxError(
                f"Package {safe_id} is not owned by the dedicated single-thread skill."
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


def _validate_package_contract(
    payload: dict[str, Any],
    package_id: str,
) -> None:
    if payload.get("schema_version") != 2:
        raise ImageTaskInboxError(
            f"Package {package_id} must use image task schema_version=2."
        )
    if payload.get("kind") != "ozon_product_image_generation_and_upload":
        raise ImageTaskInboxError(f"Package {package_id} has an unsupported kind.")
    if not str(payload.get("run_id") or "").strip():
        raise ImageTaskInboxError(f"Package {package_id} has no run_id.")
    if not str(payload.get("seed_id") or "").strip():
        raise ImageTaskInboxError(f"Package {package_id} has no seed_id.")
    target = payload.get("store_target")
    if not isinstance(target, dict):
        raise ImageTaskInboxError(f"Package {package_id} has no store target.")
    for field in ("seller_import_task_id", "product_id"):
        try:
            value = int(target.get(field))
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            raise ImageTaskInboxError(
                f"Package {package_id} requires a positive {field}."
            )
    contract = payload.get("generation_contract")
    if not isinstance(contract, dict):
        raise ImageTaskInboxError(
            f"Package {package_id} has no generation contract."
        )
    required_generation = {
        "generation_mode": "single_thread_8_grid",
        "grid_layout": "4x2",
        "public_media": "auto_quick_tunnel",
    }
    for field, expected in required_generation.items():
        if contract.get(field) != expected:
            raise ImageTaskInboxError(
                f"Package {package_id} generation_contract.{field} must be "
                f"{expected!r}."
            )
    identity = contract.get("identity_reference")
    if not isinstance(identity, dict):
        raise ImageTaskInboxError(
            f"Package {package_id} has no identity-reference contract."
        )
    required_identity = {
        "required": True,
        "source": "generated_white_anchor",
        "reference_index": 1,
        "reference_count": 1,
        "additional_image_references_allowed": False,
        "reuse_for_all_finished_calls": True,
        "reuse_for_repairs": True,
        "product_identity_source": "white_anchor_only",
        "composition_source": "fixed_skill_prompt_only",
    }
    for field, expected in required_identity.items():
        if identity.get(field) != expected:
            raise ImageTaskInboxError(
                f"Package {package_id} identity_reference.{field} must be "
                f"{expected!r}."
            )
    evidence = payload.get("evidence")
    if isinstance(evidence, dict) and evidence.get("ozon_reference_images"):
        raise ImageTaskInboxError(
            f"Package {package_id} must not send Ozon images to generation."
        )


def _package_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        raise ImageTaskInboxError("Invalid image task package ID.")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
