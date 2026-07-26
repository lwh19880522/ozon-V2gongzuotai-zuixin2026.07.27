from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozon_v2.images.task_inbox import ImageTaskInbox, ImageTaskInboxError


def write_package(runtime_root: Path, package_id: str) -> Path:
    path = runtime_root / "image_tasks" / "pending" / f"{package_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ozon_product_image_generation_and_upload",
                "package_id": package_id,
                "status": "pending",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_image_task_inbox_claims_oldest_package_and_moves_it_atomically(
    tmp_path: Path,
) -> None:
    write_package(tmp_path, "ozon-image-002")
    write_package(tmp_path, "ozon-image-001")
    inbox = ImageTaskInbox(tmp_path)

    claimed = inbox.claim_next("ozon-image-worker-01")

    assert claimed is not None
    assert claimed["package_id"] == "ozon-image-001"
    assert claimed["status"] == "in_progress"
    assert claimed["assignment"]["worker_id"] == "ozon-image-worker-01"
    assert not (tmp_path / "image_tasks" / "pending" / "ozon-image-001.json").exists()
    assert (tmp_path / "image_tasks" / "in_progress" / "ozon-image-001.json").is_file()


def test_image_task_inbox_completion_is_owned_and_never_returns_to_workbench(
    tmp_path: Path,
) -> None:
    write_package(tmp_path, "ozon-image-001")
    inbox = ImageTaskInbox(tmp_path)
    inbox.claim_next("ozon-image-worker-01")

    with pytest.raises(ImageTaskInboxError):
        inbox.complete(
            "ozon-image-001",
            "ozon-image-worker-02",
            {"ozon_picture_import": "accepted"},
        )

    completed = inbox.complete(
        "ozon-image-001",
        "ozon-image-worker-01",
        {"ozon_picture_import": "accepted"},
    )

    assert completed["status"] == "completed"
    assert completed["result"]["ozon_picture_import"] == "accepted"
    assert (tmp_path / "image_tasks" / "completed" / "ozon-image-001.json").is_file()
    assert inbox.status()["counts"] == {
        "pending": 0,
        "in_progress": 0,
        "completed": 1,
        "failed": 0,
    }


def test_image_task_inbox_can_release_owned_package_for_missing_r2_preflight(
    tmp_path: Path,
) -> None:
    write_package(tmp_path, "ozon-image-001")
    inbox = ImageTaskInbox(tmp_path)
    inbox.claim_next("ozon-image-worker-01")

    released = inbox.release(
        "ozon-image-001",
        "ozon-image-worker-01",
        "public_media_channel_required",
    )

    assert released["status"] == "pending"
    assert released["last_release"]["reason"] == "public_media_channel_required"
    assert "assignment" not in released
    assert (tmp_path / "image_tasks" / "pending" / "ozon-image-001.json").is_file()
    assert not (
        tmp_path / "image_tasks" / "in_progress" / "ozon-image-001.json"
    ).exists()
