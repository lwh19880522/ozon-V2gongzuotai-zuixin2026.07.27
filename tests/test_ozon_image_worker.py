from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from ozon_v2.domain.supplier_sku import SupplierSkuOption, SupplierSkuSelectionReceipt
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.queue import ImageGenerationQueue
from ozon_v2.images.worker import SlotResultReceipt, crop_grid


def _receipt(product_id: str = "ozon-image-product") -> SupplierSkuSelectionReceipt:
    sku = SupplierSkuOption(
        supplier_sku_id="supplier-set-4",
        combination_key="blue>set-of-4",
        raw_label="blue / set x4",
        selected_options={"color": "blue", "quantity": "set of 4"},
        set_quantity=4,
        set_composition=["set x4"],
        price={"currency": "CNY", "amount": "15.80"},
        stock={"status": "in_stock", "quantity": 50},
        image_urls=["https://cbu01.alicdn.com/img/ibank/set-4.jpg"],
        evidence_source="embedded_sku_map",
        complete=True,
    )
    return SupplierSkuSelectionReceipt.confirmed(
        run_id="wb-image-worker",
        product_id=product_id,
        supplier_offer_id="offer-200",
        supplier_sku=sku,
        ozon_target_sku={"sku_id": "ozon-set", "selected_options": {"quantity": "4"}},
        differences=[],
        confirmed_at="2026-07-14T01:00:00+00:00",
    )


def _master(tmp_path: Path, receipt: SupplierSkuSelectionReceipt) -> SubjectMasterSelection:
    path = tmp_path / f"{receipt.product_id}-master.png"
    Image.new("RGB", (400, 400), "white").save(path)
    return SubjectMasterSelection.create(
        receipt=receipt,
        source_path=path,
        source_image_url=receipt.supplier_sku.image_urls[0],
        visible_subject_quantity=4,
        white_background_confirmed=True,
        confirmed_at="2026-07-14T01:01:00+00:00",
    )


def _solid_grid(path: Path, colors: list[str], panel_size: tuple[int, int] = (120, 90)) -> None:
    width, height = panel_size
    grid = Image.new("RGB", (width * len(colors), height))
    for index, color in enumerate(colors):
        grid.paste(Image.new("RGB", panel_size, color), (index * width, 0))
    grid.save(path)


def _slot_receipt(
    *,
    job: dict,
    slot: dict,
    source_path: Path,
    output_path: Path,
    accepted: bool,
    source_kind: str | None = None,
    validation: dict | None = None,
) -> SlotResultReceipt:
    return SlotResultReceipt.create(
        job_id=job["job_id"],
        slot_id=slot["slot_id"],
        worker_id=job["worker_id"],
        lease_epoch=job["lease_epoch"],
        selection_sha256=job["selection_sha256"],
        subject_master_sha256=job["subject_master_sha256"],
        prompt_version="ozon-image-v2",
        source_kind=source_kind or slot["source_grid"],
        source_path=source_path,
        output_path=output_path,
        accepted=accepted,
        validation=validation
        or {
            "product_truth": accepted,
            "slot_role": slot["slot_id"],
            "slot_role_satisfied": accepted,
            "role_visually_demonstrated": accepted,
            "not_plain_or_near_white_product_only": accepted,
            "distinct_from_accepted_slots": accepted,
            "copy_not_used_as_visual_evidence": accepted,
        },
        created_at="2026-07-14T01:02:00+00:00",
    )


def test_accepted_slot_rejects_white_anchor_style_without_visual_role_evidence(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "main-grid.png"
    output = tmp_path / "main-01.png"
    _solid_grid(source, ["white", "white"])
    Image.new("RGB", (120, 90), "white").save(output)

    weak_receipt = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
        validation={"product_truth": True, "layout": True},
    )

    with pytest.raises(ValueError, match="marketing-scene validation"):
        queue.record_slot_result(weak_receipt)


def test_accepted_slot_rejects_near_white_pixels_even_when_worker_self_reports_pass(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "main-grid.png"
    output = tmp_path / "main-01.png"
    _solid_grid(source, ["white", "white"])
    Image.new("RGB", (120, 90), (242, 242, 240)).save(output)
    self_reported_pass = {
        "product_truth": True,
        "slot_role": "installed_use",
        "slot_role_satisfied": True,
        "role_visually_demonstrated": True,
        "not_plain_or_near_white_product_only": True,
        "distinct_from_accepted_slots": True,
        "copy_not_used_as_visual_evidence": True,
    }

    with pytest.raises(ValueError, match="near-white product-only pixels"):
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=source,
                output_path=output,
                accepted=True,
                validation=self_reported_pass,
            )
        )


def test_accepted_slot_rejects_dark_product_on_plain_white_catalog_background(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "main-grid.png"
    output = tmp_path / "main-01.png"
    _solid_grid(source, ["white", "white"])
    catalog = Image.new("RGB", (120, 90), "white")
    catalog.paste((25, 25, 25), (18, 8, 102, 82))
    catalog.save(output)

    with pytest.raises(ValueError, match="near-white product-only pixels"):
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=source,
                output_path=output,
                accepted=True,
                validation={
                    "product_truth": True,
                    "slot_role": "installed_use",
                    "slot_role_satisfied": True,
                    "role_visually_demonstrated": True,
                    "not_plain_or_near_white_product_only": True,
                    "distinct_from_accepted_slots": True,
                    "copy_not_used_as_visual_evidence": True,
                },
            )
        )


def test_crop_protocol_splits_one_by_two_and_one_by_three_without_overlap(tmp_path: Path) -> None:
    main_grid = tmp_path / "main-grid.png"
    detail_grid = tmp_path / "detail-grid.png"
    _solid_grid(main_grid, ["red", "green"])
    _solid_grid(detail_grid, ["red", "green", "blue"])

    main = crop_grid(main_grid, tmp_path / "main", layout="1x2", basename="main")
    detail = crop_grid(detail_grid, tmp_path / "detail", layout="1x3", basename="detail")

    assert len(main) == 2
    assert len(detail) == 3
    assert [Image.open(path).size for path in main] == [(120, 90), (120, 90)]
    assert [Image.open(path).getpixel((60, 45)) for path in detail] == [
        (255, 0, 0),
        (0, 128, 0),
        (0, 0, 255),
    ]


def test_accepted_slot_is_frozen_and_receipt_tampering_is_detected(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queued = queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job and job["job_id"] == queued["job_id"]
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "main-grid.png"
    output = tmp_path / "main-01.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    result = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
    )

    queue.record_slot_result(result)
    assert queue.list_slots(job["job_id"])[0]["status"] == "accepted"
    assert not replace(result, output_sha256="0" * 64).verify()
    with pytest.raises(ValueError, match="accepted slot is frozen"):
        queue.record_slot_result(result)


def test_only_two_single_slot_repairs_are_allowed(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[2]
    source = tmp_path / "detail-grid.png"
    output = tmp_path / "detail-01.png"
    _solid_grid(source, ["red", "green", "blue"])
    Image.new("RGB", (120, 90), "red").save(output)

    queue.record_slot_result(
        _slot_receipt(
            job=job,
            slot=slot,
            source_path=source,
            output_path=output,
            accepted=False,
        )
    )
    for _ in range(2):
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=output,
                output_path=output,
                accepted=False,
                source_kind="repair_single",
            )
        )

    current = queue.list_slots(job["job_id"])[2]
    assert current["repair_count"] == 2
    assert current["status"] == "manual_review_required"
    with pytest.raises(ValueError, match="repair limit"):
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=output,
                output_path=output,
                accepted=True,
                source_kind="repair_single",
            )
        )


def test_worker_heartbeat_uses_fencing_epoch_and_rejects_stale_writer(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    first = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=10)
    assert first and first["lease_epoch"] == 1
    extended = queue.heartbeat(
        first["job_id"],
        "ozon-image-worker-01",
        first["lease_epoch"],
        now_epoch=105,
        lease_seconds=20,
    )
    assert extended["lease_expires"] == 125

    second = queue.claim_next("ozon-image-worker-02", now_epoch=126, lease_seconds=10)
    assert second and second["lease_epoch"] == 2
    with pytest.raises(ValueError, match="stale worker lease"):
        queue.heartbeat(
            first["job_id"],
            "ozon-image-worker-01",
            first["lease_epoch"],
            now_epoch=127,
            lease_seconds=10,
        )


def test_all_eight_slots_are_required_before_manual_review(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    source = tmp_path / "all-slots-source.png"
    output = tmp_path / "all-slots-output.png"
    _solid_grid(source, ["red", "green", "blue"])
    Image.new("RGB", (120, 90), "red").save(output)

    with pytest.raises(ValueError, match="all eight image slots"):
        queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])

    for slot in queue.list_slots(job["job_id"]):
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=source,
                output_path=output,
                accepted=True,
            )
        )
    reviewed = queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])
    assert reviewed["status"] == "manual_review_required"
    assert reviewed["worker_id"] is None


def test_ready_for_review_rechecks_unique_roles_and_frozen_output_files(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    source = tmp_path / "all-slots-source.png"
    _solid_grid(source, ["red", "green", "blue"])

    outputs: list[Path] = []
    for index, slot in enumerate(queue.list_slots(job["job_id"])):
        output = tmp_path / f"slot-{index}.png"
        Image.new("RGB", (120, 90), "red").save(output)
        outputs.append(output)
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=source,
                output_path=output,
                accepted=True,
                validation={
                    "product_truth": True,
                    "slot_role": "same_role",
                    "slot_role_satisfied": True,
                    "role_visually_demonstrated": True,
                    "not_plain_or_near_white_product_only": True,
                    "distinct_from_accepted_slots": True,
                    "copy_not_used_as_visual_evidence": True,
                },
            )
        )

    with pytest.raises(ValueError, match="slot roles must be distinct"):
        queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])

    Image.new("RGB", (120, 90), "blue").save(outputs[0])
    with pytest.raises(ValueError, match="receipt failed final verification"):
        queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])
