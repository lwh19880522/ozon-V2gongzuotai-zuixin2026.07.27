from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ozon_v2.domain.supplier_sku import SupplierSkuSelectionReceipt, stable_sha256
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.visual_design import VisualSpec, validate_visual_set, validate_visual_spec
from ozon_v2.images.worker import CURRENT_PROMPT_VERSION, LEGACY_PROMPT_VERSION, SlotResultReceipt


REGULAR_IMAGE_WORKER_IDS = tuple(
    f"ozon-image-worker-{index:02d}" for index in range(1, 6)
)
REPAIR_ISSUE_CODES = frozenset(
    {
        "product_truth",
        "scene_quality",
        "composition",
        "selling_point",
        "russian_copy",
        "other",
    }
)
MAX_REVIEW_NOTE_LENGTH = 500
HISTORICAL_PROMPT_VERSIONS = frozenset(
    {"ozon-image-v1", LEGACY_PROMPT_VERSION}
)

SLOT_DEFINITIONS = (
    ("main_01", "main", "main_1x2", 1),
    ("main_02", "main", "main_1x2", 2),
    ("detail_01", "detail", "detail_a_1x3", 1),
    ("detail_02", "detail", "detail_a_1x3", 2),
    ("detail_03", "detail", "detail_a_1x3", 3),
    ("detail_04", "detail", "detail_b_1x3", 1),
    ("detail_05", "detail", "detail_b_1x3", 2),
    ("detail_06", "detail", "detail_b_1x3", 3),
)


class ImageRepairRequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ImageGenerationQueue:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS image_jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    supplier_sku_id TEXT NOT NULL,
                    selection_sha256 TEXT NOT NULL,
                    subject_master_sha256 TEXT NOT NULL,
                    subject_master_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    lease_expires REAL,
                    heartbeat_at REAL,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(run_id, product_id, selection_sha256, subject_master_sha256)
                );

                CREATE TABLE IF NOT EXISTS image_slots (
                    job_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    source_grid TEXT NOT NULL,
                    panel_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    repair_count INTEGER NOT NULL DEFAULT 0,
                    accepted_path TEXT,
                    receipt_json TEXT,
                    PRIMARY KEY(job_id, slot_id),
                    FOREIGN KEY(job_id) REFERENCES image_jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS image_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    lease_epoch INTEGER NOT NULL,
                    source_kind TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id, slot_id) REFERENCES image_slots(job_id, slot_id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_lease_epoch_column(connection)
            self._ensure_repair_count_column(connection)
            self._ensure_review_feedback_columns(connection)

    @staticmethod
    def _ensure_lease_epoch_column(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(image_jobs)")
        }
        if "lease_epoch" not in columns:
            connection.execute(
                "ALTER TABLE image_jobs "
                "ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _ensure_repair_count_column(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(image_slots)")
        }
        if "repair_count" not in columns:
            connection.execute(
                "ALTER TABLE image_slots "
                "ADD COLUMN repair_count INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _ensure_review_feedback_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(image_slots)")
        }
        definitions = {
            "review_issue_code": "TEXT",
            "review_note": "TEXT",
            "review_requested_at": "REAL",
        }
        for name, declaration in definitions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE image_slots ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def enqueue(
        self,
        *,
        receipt: SupplierSkuSelectionReceipt,
        subject_master: SubjectMasterSelection,
    ) -> dict[str, Any]:
        if not subject_master.verify_selection(receipt):
            raise ValueError("subject master does not match the active supplier SKU selection")
        if not subject_master.verify_file():
            raise ValueError("subject master file hash does not match the locked file")

        identity = {
            "run_id": receipt.run_id,
            "product_id": receipt.product_id,
            "selection_sha256": receipt.selection_sha256,
            "subject_master_sha256": subject_master.subject_master_sha256,
        }
        job_id = f"img-{stable_sha256(identity)[:20]}"
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO image_jobs (
                    job_id, run_id, product_id, supplier_sku_id,
                    selection_sha256, subject_master_sha256,
                    subject_master_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    job_id,
                    receipt.run_id,
                    receipt.product_id,
                    receipt.supplier_sku_id,
                    receipt.selection_sha256,
                    subject_master.subject_master_sha256,
                    json.dumps(subject_master.to_dict(), ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            for ordinal, (slot_id, role, source_grid, panel_index) in enumerate(SLOT_DEFINITIONS, start=1):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO image_slots (
                        job_id, slot_id, ordinal, role, source_grid,
                        panel_index, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (job_id, slot_id, ordinal, role, source_grid, panel_index),
                )
            row = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        return dict(row)

    def job_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM image_jobs").fetchone()
        return int(row["count"])

    def list_slots(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM image_slots WHERE job_id = ? ORDER BY ordinal",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._row(row)

    def list_run_jobs(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM image_jobs WHERE run_id = ? ORDER BY created_at, job_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_attempts(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM image_attempts WHERE job_id = ? ORDER BY attempt_id",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError("unknown image job")
        return {
            "job": job,
            "slots": self.list_slots(job_id),
            "attempts": self.list_attempts(job_id),
        }

    def request_repairs(
        self,
        job_id: str,
        repairs: list[dict[str, Any]],
        *,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(repairs, list) or not repairs:
            raise ImageRepairRequestError(
                "image_job.repair_selection_invalid",
                "At least one image slot must be selected for repair.",
            )

        normalized: list[dict[str, str]] = []
        seen_slots: set[str] = set()
        for repair in repairs:
            if not isinstance(repair, dict):
                raise ImageRepairRequestError(
                    "image_job.repair_selection_invalid",
                    "Every image repair selection must be an object.",
                )
            slot_id = str(repair.get("slot_id") or "").strip()
            if not slot_id or slot_id in seen_slots:
                raise ImageRepairRequestError(
                    "image_job.repair_selection_invalid",
                    "Image repair slots must be present and unique.",
                )
            issue_code = str(repair.get("issue_code") or "").strip()
            note_value = repair.get("note", "")
            if not isinstance(note_value, str):
                raise ImageRepairRequestError(
                    "image_job.repair_feedback_invalid",
                    "Image repair notes must be text.",
                )
            note = note_value.strip()
            if issue_code not in REPAIR_ISSUE_CODES:
                raise ImageRepairRequestError(
                    "image_job.repair_feedback_invalid",
                    "Image repair issue_code is not supported.",
                )
            if len(note) > MAX_REVIEW_NOTE_LENGTH:
                raise ImageRepairRequestError(
                    "image_job.repair_feedback_invalid",
                    f"Image repair notes must not exceed {MAX_REVIEW_NOTE_LENGTH} characters.",
                )
            if issue_code == "other" and not note:
                raise ImageRepairRequestError(
                    "image_job.repair_feedback_invalid",
                    "A note is required when the image repair issue is other.",
                )
            seen_slots.add(slot_id)
            normalized.append(
                {"slot_id": slot_id, "issue_code": issue_code, "note": note}
            )

        now = time.time() if now_epoch is None else float(now_epoch)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                job is None
                or job["status"] != "manual_review_required"
                or job["worker_id"] is not None
                or job["lease_expires"] is not None
            ):
                connection.rollback()
                raise ImageRepairRequestError(
                    "image_job.repair_not_reviewable",
                    "Image repairs can only be requested at the manual review gate.",
                )

            placeholders = ",".join("?" for _ in normalized)
            slot_ids = [repair["slot_id"] for repair in normalized]
            rows = connection.execute(
                f"""
                SELECT * FROM image_slots
                WHERE job_id = ? AND slot_id IN ({placeholders})
                """,
                (job_id, *slot_ids),
            ).fetchall()
            slots_by_id = {str(row["slot_id"]): row for row in rows}
            if set(slots_by_id) != set(slot_ids) or any(
                slots_by_id[slot_id]["status"] != "accepted" for slot_id in slot_ids
            ):
                connection.rollback()
                raise ImageRepairRequestError(
                    "image_job.repair_slot_invalid",
                    "Every selected image slot must exist and be accepted.",
                )
            if any(int(slots_by_id[slot_id]["repair_count"]) >= 2 for slot_id in slot_ids):
                connection.rollback()
                raise ImageRepairRequestError(
                    "image_job.repair_limit_reached",
                    "A selected image slot has reached the repair limit.",
                )

            for repair in normalized:
                connection.execute(
                    """
                    UPDATE image_slots
                    SET status = 'repair_pending', review_issue_code = ?,
                        review_note = ?, review_requested_at = ?
                    WHERE job_id = ? AND slot_id = ?
                    """,
                    (
                        repair["issue_code"],
                        repair["note"],
                        now,
                        job_id,
                        repair["slot_id"],
                    ),
                )
            connection.execute(
                """
                UPDATE image_jobs
                SET status = 'pending', worker_id = NULL, lease_expires = NULL,
                    heartbeat_at = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            updated_job = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            updated_slots = connection.execute(
                f"""
                SELECT * FROM image_slots
                WHERE job_id = ? AND slot_id IN ({placeholders})
                ORDER BY ordinal
                """,
                (job_id, *slot_ids),
            ).fetchall()
            connection.commit()
        return {
            "job": dict(updated_job),
            "slots": [dict(slot) for slot in updated_slots],
        }

    def claim_next(
        self,
        worker_id: str,
        *,
        now_epoch: float | None = None,
        lease_seconds: float = 120,
    ) -> dict[str, Any] | None:
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        if worker_id not in REGULAR_IMAGE_WORKER_IDS:
            raise ValueError("worker_id must identify an approved regular image worker")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time() if now_epoch is None else float(now_epoch)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM image_jobs
                WHERE status = 'pending'
                   OR (status = 'in_progress' AND lease_expires < ?)
                ORDER BY created_at, job_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE image_jobs
                SET status = 'in_progress', worker_id = ?,
                    lease_expires = ?, heartbeat_at = ?, updated_at = ?,
                    lease_epoch = lease_epoch + 1
                WHERE job_id = ?
                """,
                (worker_id, now + lease_seconds, now, now, row["job_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()
            connection.commit()
        return self._row(claimed)

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        *,
        now_epoch: float | None = None,
        lease_seconds: float = 120,
    ) -> dict[str, Any]:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time() if now_epoch is None else float(now_epoch)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "in_progress"
                or row["worker_id"] != worker_id
                or row["lease_epoch"] != lease_epoch
                or row["lease_expires"] is None
                or row["lease_expires"] < now
            ):
                connection.rollback()
                raise ValueError("stale worker lease cannot be renewed")
            connection.execute(
                """
                UPDATE image_jobs
                SET lease_expires = ?, heartbeat_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (now + lease_seconds, now, now, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        return dict(updated)

    def record_slot_result(self, receipt: SlotResultReceipt) -> dict[str, Any]:
        if not receipt.verify():
            raise ValueError("slot result receipt failed validation")
        acceptance_errors = receipt.acceptance_contract_errors()
        if acceptance_errors:
            raise ValueError(
                "accepted slot failed marketing-scene validation: "
                + "; ".join(acceptance_errors)
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (receipt.job_id,),
            ).fetchone()
            if (
                job is None
                or job["status"] != "in_progress"
                or job["worker_id"] != receipt.worker_id
                or job["lease_epoch"] != receipt.lease_epoch
            ):
                connection.rollback()
                raise ValueError("stale worker lease cannot write image results")
            if (
                job["selection_sha256"] != receipt.selection_sha256
                or job["subject_master_sha256"] != receipt.subject_master_sha256
            ):
                connection.rollback()
                raise ValueError("slot result does not match the active image contract")
            slot = connection.execute(
                "SELECT * FROM image_slots WHERE job_id = ? AND slot_id = ?",
                (receipt.job_id, receipt.slot_id),
            ).fetchone()
            if slot is None:
                connection.rollback()
                raise ValueError("unknown image slot")
            if slot["status"] == "accepted":
                connection.rollback()
                raise ValueError("accepted slot is frozen")

            if receipt.accepted and receipt.prompt_version == CURRENT_PROMPT_VERSION:
                try:
                    subject_master = SubjectMasterSelection.from_dict(
                        json.loads(job["subject_master_json"])
                    )
                    spec = VisualSpec.from_dict(receipt.validation["visual_spec"])
                    errors = []
                    if spec.slot_id != receipt.slot_id:
                        errors.append("visual_spec.slot_id does not match receipt.slot_id")
                    errors.extend(
                        validate_visual_spec(spec, set(subject_master.source_sha256s))
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    connection.rollback()
                    raise ValueError(
                        "accepted slot failed locked supplier evidence validation: "
                        f"invalid visual_spec ({error})"
                    ) from None
                if errors:
                    connection.rollback()
                    raise ValueError(
                        "accepted slot failed locked supplier evidence validation: "
                        + "; ".join(errors)
                    )

            is_repair = receipt.source_kind == "repair_single"
            repair_count = int(slot["repair_count"])
            if is_repair and repair_count >= 2:
                connection.rollback()
                raise ValueError("single-slot repair limit has been reached")
            next_repair_count = repair_count + (1 if is_repair else 0)
            if receipt.accepted:
                next_status = "accepted"
            elif next_repair_count >= 2:
                next_status = "manual_review_required"
            else:
                next_status = "repair_pending"

            payload = json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True)
            connection.execute(
                """
                INSERT INTO image_attempts (
                    job_id, slot_id, worker_id, lease_epoch, source_kind,
                    accepted, receipt_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.job_id,
                    receipt.slot_id,
                    receipt.worker_id,
                    receipt.lease_epoch,
                    receipt.source_kind,
                    1 if receipt.accepted else 0,
                    payload,
                    receipt.created_at,
                ),
            )
            connection.execute(
                """
                UPDATE image_slots
                SET status = ?, attempt_count = attempt_count + 1,
                    repair_count = ?, accepted_path = ?, receipt_json = ?
                WHERE job_id = ? AND slot_id = ?
                """,
                (
                    next_status,
                    next_repair_count,
                    receipt.output_path if receipt.accepted else None,
                    payload,
                    receipt.job_id,
                    receipt.slot_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM image_slots WHERE job_id = ? AND slot_id = ?",
                (receipt.job_id, receipt.slot_id),
            ).fetchone()
            connection.commit()
        return dict(updated)

    def mark_ready_for_review(self, job_id: str, worker_id: str, lease_epoch: int) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                job is None
                or job["status"] != "in_progress"
                or job["worker_id"] != worker_id
                or job["lease_epoch"] != lease_epoch
            ):
                connection.rollback()
                raise ValueError("stale worker lease cannot finalize image results")
            remaining = connection.execute(
                """
                SELECT COUNT(*) AS count FROM image_slots
                WHERE job_id = ? AND status != 'accepted'
                """,
                (job_id,),
            ).fetchone()
            if int(remaining["count"]) != 0:
                connection.rollback()
                raise ValueError("all eight image slots must be accepted before review")
            accepted_receipts = connection.execute(
                """
                SELECT slot_id, receipt_json, review_requested_at
                FROM image_slots WHERE job_id = ? ORDER BY rowid
                """,
                (job_id,),
            ).fetchall()
            migration_requested = any(
                stored["review_requested_at"] is not None
                for stored in accepted_receipts
            )
            slot_roles: list[str] = []
            receipts: list[SlotResultReceipt] = []
            for stored in accepted_receipts:
                try:
                    receipt = SlotResultReceipt.from_dict(json.loads(stored["receipt_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    connection.rollback()
                    raise ValueError(
                        f"accepted slot receipt is missing or invalid: {error}"
                    ) from None
                if not receipt.verify():
                    connection.rollback()
                    raise ValueError("accepted slot receipt failed final verification")
                if (
                    receipt.selection_sha256 != job["selection_sha256"]
                    or receipt.subject_master_sha256 != job["subject_master_sha256"]
                ):
                    connection.rollback()
                    raise ValueError(
                        "accepted slot receipt does not match the active image contract"
                    )
                preserved_historical = (
                    migration_requested
                    and receipt.prompt_version in HISTORICAL_PROMPT_VERSIONS
                    and stored["review_requested_at"] is None
                )
                acceptance_errors = receipt.acceptance_contract_errors()
                if acceptance_errors and not (
                    preserved_historical
                    and receipt.prompt_version == "ozon-image-v1"
                ):
                    connection.rollback()
                    raise ValueError(
                        "accepted slot failed marketing-scene validation: "
                        + "; ".join(acceptance_errors)
                    )
                slot_roles.append(
                    str(
                        stored["slot_id"]
                        if receipt.prompt_version == "ozon-image-v1"
                        else receipt.validation["slot_role"]
                    )
                    .strip()
                    .casefold()
                )
                receipts.append(receipt)
            if len(set(slot_roles)) != len(slot_roles):
                connection.rollback()
                raise ValueError("all eight accepted slot roles must be distinct")
            prompt_versions = {receipt.prompt_version for receipt in receipts}
            migration_allowed = (
                migration_requested
                and CURRENT_PROMPT_VERSION in prompt_versions
                and prompt_versions
                <= HISTORICAL_PROMPT_VERSIONS | {CURRENT_PROMPT_VERSION}
                and all(
                    receipt.prompt_version == CURRENT_PROMPT_VERSION
                    or stored["review_requested_at"] is None
                    for stored, receipt in zip(
                        accepted_receipts, receipts, strict=True
                    )
                )
            )
            if len(prompt_versions) != 1 and not migration_allowed:
                connection.rollback()
                raise ValueError("mixed prompt versions are not allowed")
            if prompt_versions == {"ozon-image-v1"}:
                connection.rollback()
                raise ValueError("accepted slot receipt has an unknown prompt version")
            if prompt_versions == {CURRENT_PROMPT_VERSION} or migration_allowed:
                specs: list[VisualSpec] = []
                errors: list[str] = []
                for stored, receipt in zip(accepted_receipts, receipts, strict=True):
                    if receipt.prompt_version != CURRENT_PROMPT_VERSION:
                        continue
                    try:
                        spec = VisualSpec.from_dict(receipt.validation["visual_spec"])
                    except (KeyError, TypeError, ValueError) as error:
                        errors.append(f"invalid visual_spec ({error})")
                        continue
                    if receipt.slot_id != stored["slot_id"] or spec.slot_id != receipt.slot_id:
                        errors.append("visual_spec.slot_id does not match receipt.slot_id")
                    specs.append(spec)
                if not errors:
                    errors.extend(validate_visual_set(tuple(specs)))
                if errors:
                    connection.rollback()
                    raise ValueError("visual set validation failed: " + "; ".join(errors))
            elif prompt_versions != {LEGACY_PROMPT_VERSION}:
                connection.rollback()
                raise ValueError("accepted slot receipt has an unknown prompt version")
            connection.execute(
                """
                UPDATE image_jobs
                SET status = 'manual_review_required', worker_id = NULL,
                    lease_expires = NULL, heartbeat_at = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        return dict(updated)

    def stop(self, job_id: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE image_jobs
                SET status = 'stopped', worker_id = NULL, lease_expires = NULL,
                    heartbeat_at = NULL, updated_at = ?
                WHERE job_id = ? AND status NOT IN ('completed', 'failed')
                """,
                (now, job_id),
            )

    def resume(self, job_id: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE image_jobs
                SET status = 'pending', worker_id = NULL, lease_expires = NULL,
                    heartbeat_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'stopped'
                """,
                (now, job_id),
            )
