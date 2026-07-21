from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ozon_v2.domain.supplier_sku import SupplierSkuOption, SupplierSkuSelectionReceipt
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.queue import ImageGenerationQueue


def selection_receipt(
    product_id: str = "ozon-1",
    sku_id: str = "sku-pink-4",
) -> SupplierSkuSelectionReceipt:
    sku = SupplierSkuOption(
        supplier_sku_id=sku_id,
        combination_key="pink>set-of-4",
        raw_label="pink / set x4",
        selected_options={"color": "pink", "quantity": "set of 4"},
        set_quantity=4,
        set_composition=["set x4"],
        price={"currency": "CNY", "amount": "12.80"},
        stock={"status": "in_stock", "quantity": 88},
        image_urls=["https://cbu01.alicdn.com/img/ibank/sku-pink.jpg"],
        evidence_source="embedded_sku_map",
        complete=True,
    )
    return SupplierSkuSelectionReceipt.confirmed(
        run_id="wb-image",
        product_id=product_id,
        supplier_offer_id="offer-100",
        supplier_sku=sku,
        ozon_target_sku={"sku_id": "ozon-sku", "selected_options": {"quantity": "4"}},
        differences=[],
        confirmed_at="2026-07-14T00:00:00+00:00",
    )


def subject_master(tmp_path: Path, receipt: SupplierSkuSelectionReceipt) -> SubjectMasterSelection:
    source = tmp_path / f"{receipt.product_id}-subject.png"
    source.write_bytes(b"locked-subject-master")
    return SubjectMasterSelection.create(
        receipt=receipt,
        source_path=source,
        source_image_url=receipt.supplier_sku.image_urls[0],
        visible_subject_quantity=4,
        white_background_confirmed=True,
        confirmed_at="2026-07-14T00:01:00+00:00",
    )


def test_subject_master_locks_exact_file_and_supplier_selection(tmp_path: Path) -> None:
    receipt = selection_receipt()
    master = subject_master(tmp_path, receipt)

    assert master.selection_sha256 == receipt.selection_sha256
    assert master.supplier_sku_id == receipt.supplier_sku_id
    assert master.visible_subject_quantity == receipt.supplier_sku.set_quantity
    assert master.verify_file()
    assert master.verify_selection(receipt)


def test_subject_evidence_accepts_multiple_non_white_supplier_images(tmp_path: Path) -> None:
    receipt = selection_receipt()
    sku_source = tmp_path / "sku-source.png"
    gallery_source = tmp_path / "gallery-source.png"
    sku_source.write_bytes(b"sku-bound-evidence")
    gallery_source.write_bytes(b"supplier-gallery-evidence")
    gallery_url = "https://cbu01.alicdn.com/img/ibank/supplier-gallery.jpg"

    master = SubjectMasterSelection.create(
        receipt=receipt,
        source_paths=[sku_source, gallery_source],
        source_image_urls=[receipt.supplier_sku.image_urls[0], gallery_url],
        visible_subject_quantity=4,
        white_background_confirmed=False,
        confirmed_at="2026-07-16T00:01:00+00:00",
    )

    assert master.source_path == str(sku_source.resolve())
    assert master.source_image_url == receipt.supplier_sku.image_urls[0]
    assert master.source_sha256 == master.source_sha256s[0]
    assert master.source_paths == (str(sku_source.resolve()), str(gallery_source.resolve()))
    assert master.source_image_urls == (receipt.supplier_sku.image_urls[0], gallery_url)
    assert len(master.source_sha256s) == 2
    assert master.set_composition == ("set x4",)
    assert master.white_background_confirmed is False
    assert master.white_background_generation_required is True
    assert master.verify_file()
    assert master.verify_selection(receipt)
    assert SubjectMasterSelection.from_dict(master.to_dict()) == master


def test_four_piece_selection_rejects_single_subject_master(tmp_path: Path) -> None:
    receipt = selection_receipt()
    source = tmp_path / "single.png"
    source.write_bytes(b"single-item")

    with pytest.raises(ValueError, match="visible subject quantity"):
        SubjectMasterSelection.create(
            receipt=receipt,
            source_path=source,
            source_image_url=receipt.supplier_sku.image_urls[0],
            visible_subject_quantity=1,
            white_background_confirmed=True,
            confirmed_at="2026-07-14T00:01:00+00:00",
        )


def test_queue_creates_exact_eight_slots_and_reuses_duplicate_job(tmp_path: Path) -> None:
    receipt = selection_receipt()
    master = subject_master(tmp_path, receipt)
    queue = ImageGenerationQueue(tmp_path / "image_jobs.sqlite3")

    first = queue.enqueue(receipt=receipt, subject_master=master)
    second = queue.enqueue(receipt=receipt, subject_master=master)

    assert first["job_id"] == second["job_id"]
    assert queue.job_count() == 1
    assert [slot["slot_id"] for slot in queue.list_slots(first["job_id"])] == [
        "main_01",
        "main_02",
        "detail_01",
        "detail_02",
        "detail_03",
        "detail_04",
        "detail_05",
        "detail_06",
    ]
    assert [slot["source_grid"] for slot in queue.list_slots(first["job_id"])] == [
        "main_1x2",
        "main_1x2",
        "detail_a_1x3",
        "detail_a_1x3",
        "detail_a_1x3",
        "detail_b_1x3",
        "detail_b_1x3",
        "detail_b_1x3",
    ]


def test_two_workers_claim_distinct_whole_products_atomically(tmp_path: Path) -> None:
    queue = ImageGenerationQueue(tmp_path / "image_jobs.sqlite3")
    first_receipt = selection_receipt("ozon-1", "sku-pink-4")
    second_receipt = selection_receipt("ozon-2", "sku-blue-4")
    queue.enqueue(receipt=first_receipt, subject_master=subject_master(tmp_path, first_receipt))
    queue.enqueue(receipt=second_receipt, subject_master=subject_master(tmp_path, second_receipt))

    worker_a = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    worker_b = queue.claim_next("ozon-image-worker-02", now_epoch=100, lease_seconds=30)

    assert worker_a is not None
    assert worker_b is not None
    assert worker_a["job_id"] != worker_b["job_id"]
    assert queue.claim_next("ozon-image-worker-03", now_epoch=100, lease_seconds=30) is None


def test_only_five_regular_workers_can_claim_and_sixth_slot_stays_reserved(
    tmp_path: Path,
) -> None:
    queue = ImageGenerationQueue(tmp_path / "image_jobs.sqlite3")
    jobs = []
    for index in range(1, 7):
        receipt = selection_receipt(f"ozon-{index}", f"sku-{index}")
        jobs.append(
            queue.enqueue(
                receipt=receipt,
                subject_master=subject_master(tmp_path, receipt),
            )
        )

    claimed = [
        queue.claim_next(
            f"ozon-image-worker-{index:02d}",
            now_epoch=100,
            lease_seconds=30,
        )
        for index in range(1, 6)
    ]

    assert all(job is not None for job in claimed)
    assert len({job["job_id"] for job in claimed if job is not None}) == 5
    with pytest.raises(ValueError, match="approved regular image worker"):
        queue.claim_next("ozon-image-worker-06", now_epoch=100, lease_seconds=30)
    assert queue.get_job(jobs[-1]["job_id"])["status"] == "pending"

def test_expired_lease_can_be_reclaimed_and_stop_resume_is_explicit(tmp_path: Path) -> None:
    receipt = selection_receipt()
    queue = ImageGenerationQueue(tmp_path / "image_jobs.sqlite3")
    job = queue.enqueue(receipt=receipt, subject_master=subject_master(tmp_path, receipt))
    claimed = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=10)
    assert claimed and claimed["job_id"] == job["job_id"]

    reclaimed = queue.claim_next("ozon-image-worker-02", now_epoch=111, lease_seconds=10)
    assert reclaimed and reclaimed["job_id"] == job["job_id"]
    assert reclaimed["worker_id"] == "ozon-image-worker-02"

    queue.stop(
        job["job_id"],
        reason="用户在工具台手动停止生图",
        stopped_by="workbench_user",
    )
    stopped = queue.get_job(job["job_id"])
    assert stopped["status"] == "stopped"
    assert stopped["stop_reason"] == "用户在工具台手动停止生图"
    assert stopped["stopped_by"] == "workbench_user"
    assert stopped["stopped_at"] is not None
    assert queue.claim_next("ozon-image-worker-03", now_epoch=200, lease_seconds=10) is None
    queue.resume(job["job_id"])
    assert queue.claim_next("ozon-image-worker-03", now_epoch=200, lease_seconds=10)["job_id"] == job["job_id"]


def test_queue_rejects_subject_master_for_different_selection(tmp_path: Path) -> None:
    receipt = selection_receipt()
    master = subject_master(tmp_path, receipt)
    mismatched = replace(master, selection_sha256="0" * 64)
    queue = ImageGenerationQueue(tmp_path / "image_jobs.sqlite3")

    with pytest.raises(ValueError, match="active supplier SKU selection"):
        queue.enqueue(receipt=receipt, subject_master=mismatched)
