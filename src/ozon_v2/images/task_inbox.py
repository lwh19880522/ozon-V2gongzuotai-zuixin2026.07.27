from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ImageTaskInboxError(RuntimeError):
    pass


CLAIM_LOCK_STALE_SECONDS = 120
IN_PROGRESS_STALE_SECONDS = 6 * 60 * 60


class ImageTaskInbox:
    def __init__(self, runtime_root: str | Path) -> None:
        self.root = Path(runtime_root) / "image_tasks"

    def directory(self, status: str) -> Path:
        if status not in {
            "pending",
            "in_progress",
            "grid_ready",
            "awaiting_product",
            "completed",
            "failed",
        }:
            raise ImageTaskInboxError(f"Unsupported image task status: {status}")
        path = self.root / status
        path.mkdir(parents=True, exist_ok=True)
        return path

    def status(self) -> dict[str, Any]:
        counts = {
            status: len(list(self.directory(status).glob("*.json")))
            for status in (
                "pending",
                "in_progress",
                "grid_ready",
                "awaiting_product",
                "completed",
                "failed",
            )
        }
        return {"root": str(self.root.resolve()), "counts": counts}

    def claim_next(self) -> dict[str, Any] | None:
        pending = self.directory("pending")
        in_progress = self.directory("in_progress")
        grid_ready = self.directory("grid_ready")
        lock_path = self.root / ".claim.lock"
        lock_fd = _acquire_claim_lock(lock_path)
        if lock_fd is None:
            return None
        try:
            self._recover_stale_in_progress(in_progress)
            if any(in_progress.glob("*.json")):
                return None
            candidates: list[tuple[Path, dict[str, Any]]] = []
            for lifecycle in (pending, grid_ready):
                for source in lifecycle.glob("*.json"):
                    try:
                        payload = _read_json(source)
                        _validate_package_contract(payload, source.stem)
                        expected_status = (
                            "grid_ready"
                            if lifecycle.name == "grid_ready"
                            else "pending"
                        )
                        if payload.get("status") != expected_status:
                            raise ImageTaskInboxError(
                                f"Package {source.stem} is not {expected_status}: "
                                f"{payload.get('status')}"
                            )
                    except ImageTaskInboxError as exc:
                        self._quarantine(source, exc)
                        continue
                    candidates.append((source, payload))
            if not candidates:
                return None
            _, oldest_payload = min(
                candidates,
                key=lambda item: (
                    str(
                        item[1].get("batch_created_at")
                        or item[1].get("created_at")
                        or ""
                    ),
                    str(item[1].get("created_at") or ""),
                    item[0].name,
                ),
            )
            active_run_id = str(oldest_payload.get("run_id") or "")
            active_candidates = [
                item
                for item in candidates
                if str(item[1].get("run_id") or "") == active_run_id
            ]
            generation_candidates = [
                item
                for item in active_candidates
                if item[0].parent.name == "pending"
                and str(item[1].get("resume_mode") or "") != "upload_only"
            ]
            crop_candidates = [
                item
                for item in active_candidates
                if item[0].parent.name == "grid_ready"
            ]
            upload_candidates = [
                item
                for item in active_candidates
                if item[0].parent.name == "pending"
                and str(item[1].get("resume_mode") or "") == "upload_only"
            ]
            if crop_candidates:
                phase = "grid_crop"
                phase_candidates = crop_candidates
            elif generation_candidates:
                phase = "grid_generation"
                phase_candidates = generation_candidates
            else:
                phase = "upload_only"
                phase_candidates = upload_candidates
            source, payload = min(
                phase_candidates,
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
            expected_status = "grid_ready" if phase == "grid_crop" else "pending"
            if payload.get("status") != expected_status:
                self._quarantine(
                    target,
                    ImageTaskInboxError(
                        f"Package {source.stem} is not {expected_status}: "
                        f"{payload.get('status')}"
                    ),
                )
                return None
            payload["status"] = "in_progress"
            payload["assignment"] = {
                "executor": "ozon-product-media-generator",
                "execution_mode": "single_thread",
                "phase": phase,
                "claimed_at": _utc_now(),
            }
            _write_json(target, payload)
            return {**payload, "package_path": str(target.resolve())}
        finally:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)

    def _recover_stale_in_progress(self, in_progress: Path) -> None:
        now = time.time()
        for source in in_progress.glob("*.json"):
            try:
                payload = _read_json(source)
                _validate_package_contract(payload, source.stem)
                assignment = payload.get("assignment") or {}
                claimed_at = _iso_epoch(assignment.get("claimed_at"))
                if claimed_at is not None and now - claimed_at < IN_PROGRESS_STALE_SECONDS:
                    continue
                phase = str(assignment.get("phase") or "")
                if phase not in {"grid_generation", "grid_crop", "upload_only"}:
                    raise ImageTaskInboxError(
                        f"Package {source.stem} has no recoverable assignment phase."
                    )
            except ImageTaskInboxError as exc:
                self._quarantine(source, exc)
                continue
            return_status = "grid_ready" if phase == "grid_crop" else "pending"
            payload["status"] = return_status
            payload.pop("assignment", None)
            payload["last_release"] = {
                "reason": "stale_in_progress_recovered",
                "released_at": _utc_now(),
            }
            target = self.directory(return_status) / source.name
            source.replace(target)
            _write_json(target, payload)

    def _quarantine(self, source: Path, error: Exception) -> Path:
        failed = self.directory("failed")
        target = failed / source.name
        if target.exists():
            target = failed / f"{source.stem}-{int(time.time() * 1000)}{source.suffix}"
        try:
            payload = _read_json(source)
        except ImageTaskInboxError:
            source.replace(target)
        else:
            payload["status"] = "failed"
            payload["failed_at"] = _utc_now()
            payload["failure"] = {
                "code": "image_task.invalid_package",
                "message": str(error),
            }
            source.replace(target)
            _write_json(target, payload)
        target.with_suffix(target.suffix + ".error.txt").write_text(
            str(error) + "\n",
            encoding="utf-8",
        )
        return target

    def stage_grid(
        self,
        package_id: str,
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        payload, source = self._claimed_package(package_id)
        self._require_phase(payload, package_id, "grid_generation")
        required = (
            "raw_grid_path",
            "raw_grid_sha256",
            "white_anchor_path",
            "white_anchor_sha256",
        )
        normalized = {field: str(checkpoint.get(field) or "").strip() for field in required}
        for field in ("raw_grid_path", "white_anchor_path"):
            if not normalized[field] or not Path(normalized[field]).is_absolute():
                raise ImageTaskInboxError(
                    f"Grid checkpoint {field} must be an absolute path."
                )
        for field in ("raw_grid_sha256", "white_anchor_sha256"):
            if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized[field]):
                raise ImageTaskInboxError(
                    f"Grid checkpoint {field} must be a SHA-256 digest."
                )
            normalized[field] = normalized[field].lower()
        normalized["staged_at"] = _utc_now()
        payload["status"] = "grid_ready"
        payload["grid_checkpoint"] = normalized
        payload.pop("assignment", None)
        target = self.directory("grid_ready") / source.name
        _write_json(target, payload)
        source.unlink()
        return {**payload, "package_path": str(target.resolve())}

    def stage_media(
        self,
        package_id: str,
        generated_media: dict[str, Any],
    ) -> dict[str, Any]:
        payload, source = self._claimed_package(package_id)
        self._require_phase(payload, package_id, "grid_crop")
        images = generated_media.get("images")
        if not isinstance(images, list) or len(images) != 8:
            raise ImageTaskInboxError(
                "Product media staging requires exactly eight validated images."
            )
        slot_ids: set[str] = set()
        normalized_images: list[dict[str, str]] = []
        for item in images:
            if not isinstance(item, dict):
                raise ImageTaskInboxError(
                    "Each staged product image must be a slot/path object."
                )
            slot_id = str(item.get("slot_id") or "").strip()
            path = str(item.get("path") or "").strip()
            if not slot_id or not path or slot_id in slot_ids:
                raise ImageTaskInboxError(
                    "Staged product images require eight unique slot IDs and paths."
                )
            slot_ids.add(slot_id)
            normalized_images.append({"slot_id": slot_id, "path": path})
        video = str(generated_media.get("video") or "").strip()
        video_cover = str(generated_media.get("video_cover") or "").strip()
        if not video or not video_cover:
            raise ImageTaskInboxError(
                "Product media staging requires a slideshow video and video cover."
            )
        payload["status"] = "pending"
        payload["resume_mode"] = "upload_only"
        payload["media_ready_at"] = _utc_now()
        payload["generated_media"] = {
            "images": normalized_images,
            "video": video,
            "video_cover": video_cover,
        }
        payload.pop("assignment", None)
        target = self.directory("pending") / source.name
        _write_json(target, payload)
        source.unlink()
        return {**payload, "package_path": str(target.resolve())}

    def complete(
        self,
        package_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        payload, source = self._claimed_package(package_id)
        self._require_phase(payload, package_id, "upload_only")
        payload["status"] = "completed"
        payload["completed_at"] = _utc_now()
        payload["result"] = dict(receipt)
        target = self.directory("completed") / source.name
        _write_json(target, payload)
        source.unlink()
        return {**payload, "package_path": str(target.resolve())}

    def await_product(
        self,
        package_id: str,
        generated_media: dict[str, Any],
    ) -> dict[str, Any]:
        payload, source = self._claimed_package(package_id)
        self._require_phase(payload, package_id, "upload_only")
        target_info = payload.get("store_target") or {}
        if int(target_info.get("product_id") or 0) > 0:
            raise ImageTaskInboxError(
                f"Package {package_id} is already bound to an Ozon product."
            )
        images = generated_media.get("images")
        if not isinstance(images, list) or len(images) != 8:
            raise ImageTaskInboxError(
                "Deferred upload requires exactly eight generated images."
            )
        payload["status"] = "awaiting_product"
        payload["awaiting_product_since"] = _utc_now()
        payload["generated_media"] = dict(generated_media)
        payload.pop("assignment", None)
        target = self.directory("awaiting_product") / source.name
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
        phase = str((payload.get("assignment") or {}).get("phase") or "")
        return_status = "grid_ready" if phase == "grid_crop" else "pending"
        payload["status"] = return_status
        payload["last_release"] = {
            "executor": "ozon-product-media-generator",
            "released_at": _utc_now(),
            "reason": str(reason or "").strip() or "released",
        }
        payload.pop("assignment", None)
        target = self.directory(return_status) / source.name
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

    @staticmethod
    def _require_phase(
        payload: dict[str, Any],
        package_id: str,
        *allowed: str,
    ) -> None:
        phase = str((payload.get("assignment") or {}).get("phase") or "")
        if phase not in allowed:
            raise ImageTaskInboxError(
                f"Package {package_id} phase {phase!r} is not allowed for this action; "
                f"expected one of {', '.join(allowed)}."
            )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImageTaskInboxError(f"Invalid image task package: {path}") from exc
    if not isinstance(payload, dict):
        raise ImageTaskInboxError(f"Invalid image task package object: {path}")
    return payload


def _acquire_claim_lock(lock_path: Path) -> int | None:
    for _attempt in range(2):
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age_seconds = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age_seconds < CLAIM_LOCK_STALE_SECONDS:
                return None
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
        else:
            os.write(
                lock_fd,
                f"pid={os.getpid()} acquired_at={_utc_now()}\n".encode("utf-8"),
            )
            return lock_fd
    return None


def _iso_epoch(value: Any) -> float | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
    schema_version = payload.get("schema_version")
    if schema_version not in {2, 3}:
        raise ImageTaskInboxError(
            f"Package {package_id} must use image task schema_version=2 or 3."
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
    for field in ("seller_import_task_id",):
        try:
            value = int(target.get(field))
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            raise ImageTaskInboxError(
                f"Package {package_id} requires a positive {field}."
            )
    try:
        product_id = int(target.get("product_id") or 0)
    except (TypeError, ValueError):
        product_id = 0
    binding_state = str(target.get("binding_state") or "").strip()
    if product_id <= 0 and not (
        schema_version == 3 and binding_state == "awaiting_ozon_product"
    ):
        raise ImageTaskInboxError(
            f"Package {package_id} requires a positive product_id."
        )
    if product_id > 0 and schema_version == 3 and binding_state != "bound":
        raise ImageTaskInboxError(
            f"Package {package_id} with a product_id must have binding_state='bound'."
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
