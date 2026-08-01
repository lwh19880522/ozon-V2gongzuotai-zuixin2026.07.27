from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozon_v2.images.task_inbox import ImageTaskInbox, ImageTaskInboxError


def write_package(
    runtime_root: Path,
    package_id: str,
    *,
    run_id: str = "wb-oldest",
    created_at: str = "2026-08-01T00:00:00+00:00",
) -> Path:
    path = runtime_root / "image_tasks" / "pending" / f"{package_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "ozon_product_image_generation_and_upload",
                "package_id": package_id,
                "status": "pending",
                "run_id": run_id,
                "seed_id": f"seed-{package_id}",
                "created_at": created_at,
                "store_target": {
                    "seller_import_task_id": 7001,
                    "product_id": 900001,
                },
                "generation_contract": {
                    "generation_mode": "single_thread_8_grid",
                    "grid_layout": "4x2",
                    "public_media": "auto_quick_tunnel",
                    "identity_reference": {
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
                },
                "evidence": {},
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

    claimed = inbox.claim_next()

    assert claimed is not None
    assert claimed["package_id"] == "ozon-image-001"
    assert claimed["status"] == "in_progress"
    assert claimed["assignment"]["executor"] == "ozon-product-media-generator"
    assert claimed["assignment"]["execution_mode"] == "single_thread"
    assert not (tmp_path / "image_tasks" / "pending" / "ozon-image-001.json").exists()
    assert (tmp_path / "image_tasks" / "in_progress" / "ozon-image-001.json").is_file()


def test_image_task_inbox_rejects_ozon_reference_images_before_claim(
    tmp_path: Path,
) -> None:
    package = write_package(tmp_path, "ozon-image-001")
    payload = json.loads(package.read_text(encoding="utf-8"))
    payload["evidence"]["ozon_reference_images"] = ["https://example.test/ozon.jpg"]
    package.write_text(json.dumps(payload), encoding="utf-8")
    inbox = ImageTaskInbox(tmp_path)

    with pytest.raises(
        ImageTaskInboxError,
        match="must not send Ozon images to generation",
    ):
        inbox.claim_next()

    assert package.is_file()
    assert not (
        tmp_path / "image_tasks" / "in_progress" / "ozon-image-001.json"
    ).exists()


def test_image_task_inbox_completion_is_owned_and_never_returns_to_workbench(
    tmp_path: Path,
) -> None:
    write_package(tmp_path, "ozon-image-001")
    inbox = ImageTaskInbox(tmp_path)
    inbox.claim_next()

    completed = inbox.complete(
        "ozon-image-001",
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


def test_image_task_inbox_can_release_package_when_auto_gateway_cannot_start(
    tmp_path: Path,
) -> None:
    write_package(tmp_path, "ozon-image-001")
    inbox = ImageTaskInbox(tmp_path)
    inbox.claim_next()

    released = inbox.release(
        "ozon-image-001",
        "automatic_public_gateway_unavailable",
    )

    assert released["status"] == "pending"
    assert released["last_release"]["reason"] == "automatic_public_gateway_unavailable"
    assert "assignment" not in released
    assert (tmp_path / "image_tasks" / "pending" / "ozon-image-001.json").is_file()
    assert not (
        tmp_path / "image_tasks" / "in_progress" / "ozon-image-001.json"
    ).exists()


def test_image_task_inbox_never_claims_a_second_active_package(
    tmp_path: Path,
) -> None:
    write_package(tmp_path, "ozon-image-001")
    inbox = ImageTaskInbox(tmp_path)
    first = inbox.claim_next()
    write_package(tmp_path, "ozon-image-002")

    second = inbox.claim_next()

    assert first is not None
    assert second is None
    assert (tmp_path / "image_tasks" / "pending" / "ozon-image-002.json").is_file()


def test_image_task_inbox_finishes_the_oldest_batch_before_the_next_batch(
    tmp_path: Path,
) -> None:
    write_package(
        tmp_path,
        "ozon-image-001",
        run_id="wb-new",
        created_at="2026-08-01T02:00:00+00:00",
    )
    write_package(
        tmp_path,
        "ozon-image-003",
        run_id="wb-old",
        created_at="2026-08-01T01:00:00+00:00",
    )
    write_package(
        tmp_path,
        "ozon-image-002",
        run_id="wb-old",
        created_at="2026-08-01T01:01:00+00:00",
    )
    inbox = ImageTaskInbox(tmp_path)

    first = inbox.claim_next()
    inbox.complete(first["package_id"], {"ozon_picture_import": "accepted"})
    second = inbox.claim_next()

    assert first["run_id"] == "wb-old"
    assert second["run_id"] == "wb-old"
    assert (tmp_path / "image_tasks" / "pending" / "ozon-image-001.json").is_file()


def test_image_task_inbox_rejects_package_without_exact_ozon_product_binding(
    tmp_path: Path,
) -> None:
    package = write_package(tmp_path, "ozon-image-001")
    payload = json.loads(package.read_text(encoding="utf-8"))
    payload["store_target"]["product_id"] = None
    package.write_text(json.dumps(payload), encoding="utf-8")
    inbox = ImageTaskInbox(tmp_path)

    with pytest.raises(
        ImageTaskInboxError,
        match="positive product_id",
    ):
        inbox.claim_next()

    assert package.is_file()
