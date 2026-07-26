from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from ozon_v2.domain.supplier_sku import (
    SupplierSkuOption,
    SupplierSkuSelectionReceipt,
    stable_sha256,
)
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.queue import ImageGenerationQueue
from ozon_v2.images.worker import (
    CURRENT_PROMPT_VERSION,
    SlotResultReceipt,
    crop_grid,
    file_sha256,
    validate_reference_layout_diversity,
)
from ozon_v2.images.visual_design import (
    CURRENT_VISUAL_CONTRACT_VERSION,
    HISTORICAL_VISUAL_CONTRACT_VERSION,
    VisualFact,
    VisualSpec,
)


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
    prompt_version: str = "ozon-image-v2",
) -> SlotResultReceipt:
    normalized_validation = dict(
        validation
        or {
            "product_truth": accepted,
            "slot_role": slot["slot_id"],
            "slot_role_satisfied": accepted,
            "role_visually_demonstrated": accepted,
            "not_plain_or_near_white_product_only": accepted,
            "distinct_from_accepted_slots": accepted,
            "copy_not_used_as_visual_evidence": accepted,
        }
    )
    if accepted and prompt_version == CURRENT_PROMPT_VERSION:
        normalized_validation.setdefault("visual_source_sha256", file_sha256(source_path))
        normalized_validation.setdefault("visual_output_sha256", file_sha256(output_path))
    return SlotResultReceipt.create(
        job_id=job["job_id"],
        slot_id=slot["slot_id"],
        worker_id=job["worker_id"],
        lease_epoch=job["lease_epoch"],
        selection_sha256=job["selection_sha256"],
        subject_master_sha256=job["subject_master_sha256"],
        prompt_version=prompt_version,
        source_kind=source_kind or slot["source_grid"],
        source_path=source_path,
        output_path=output_path,
        accepted=accepted,
        validation=normalized_validation,
        created_at="2026-07-14T01:02:00+00:00",
    )


def _v3_validation(
    slot_id: str,
    evidence_sha256: str,
    *,
    repeated_scene: bool = False,
    primary_ozon_reference_path: Path | None = None,
    visual_contract_version: str = HISTORICAL_VISUAL_CONTRACT_VERSION,
) -> dict:
    is_current_visual_contract = (
        visual_contract_version == CURRENT_VISUAL_CONTRACT_VERSION
    )
    recipes = {
        "main_01": "integrated_rail" if is_current_visual_contract else "clean_hero",
        "main_02": "integrated_rail",
        "detail_01": "context_caption",
        "detail_02": "feature_callout",
        "detail_03": "feature_callout",
        "detail_04": "context_caption",
        "detail_05": "context_caption",
        "detail_06": "integrated_rail",
    }

    fact = VisualFact(
        headline="Надёжная фиксация кабеля",
        detail="\u041a\u0430\u0431\u0435\u043b\u044c \u043f\u0440\u043e\u0445\u043e\u0434\u0438\u0442 \u0441\u0432\u043e\u0431\u043e\u0434\u043d\u043e",
        evidence_sha256=evidence_sha256,
    )
    scene_suffix = "same" if repeated_scene else slot_id
    spec = VisualSpec(
        contract_version=visual_contract_version,
        slot_id=slot_id,
        recipe=recipes[slot_id],
        facts=()
        if slot_id == "main_01" and not is_current_visual_contract
        else (fact,),
        scene_signature={
            "environment": f"environment_{scene_suffix}",
            "lighting": f"lighting_{scene_suffix}",
            "camera": f"camera_{scene_suffix}",
            "shot_scale": f"shot_scale_{scene_suffix}",
            "buyer_question": slot_id,
        },
        callout_points=((0.5, 0.5),)
        if slot_id in {"detail_02", "detail_03"}
        else (),
    )
    archetypes = {
        "main_01": "hero",
        "main_02": "functional_infographic",
        "detail_01": "lifestyle",
        "detail_02": "annotated_feature",
        "detail_03": "material_closeup",
        "detail_04": "instructional_steps",
        "detail_05": "dimension_fit",
        "detail_06": "set_contents",
    }
    validation = {
        "product_truth": True,
        "slot_role": slot_id,
        "slot_role_satisfied": True,
        "role_visually_demonstrated": True,
        "not_plain_or_near_white_product_only": True,
        "distinct_from_accepted_slots": True,
        "copy_not_used_as_visual_evidence": True,
        "visual_design_passed": True,
        "russian_copy_passed": True,
        "safe_area_passed": True,
        "mobile_readability_passed": True,
        "visual_contract_version": visual_contract_version,
        "visual_spec": spec.to_dict(),
        "reference_mapping_version": "ozon-reference-map-v1",
        "guidance_mode": "reference_guided",
        "primary_ozon_reference_sha256": "e" * 64,
        "reference_slot_index": 1,
        "reference_reused": False,
        "reference_composition_followed": True,
        "reference_layout_archetype": archetypes[slot_id],
        "reference_layout_followed": True,
        "locked_subject_preserved": True,
        "copy_mode": "imagegen_integrated",
        "russian_copy_integrated": True,
        "russian_headline": "Надёжная фиксация кабеля",
        "russian_subtitle": "Кабель проходит свободно и остаётся на месте",
        "russian_functional_labels": [
            "Точная фиксация",
            "Аккуратная укладка",
        ],
    }
    if primary_ozon_reference_path is not None:
        validation["primary_ozon_reference_path"] = str(
            primary_ozon_reference_path.resolve()
        )
        validation["primary_ozon_reference_sha256"] = file_sha256(
            primary_ozon_reference_path
        )
    return validation


def _rehash_receipt(receipt: SlotResultReceipt, **changes: object) -> SlotResultReceipt:
    changed = replace(receipt, **changes)
    return replace(changed, receipt_sha256=stable_sha256(changed.hash_payload()))


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
    _solid_grid(main_grid, ["red", "green"], panel_size=(90, 120))
    _solid_grid(detail_grid, ["red", "green", "blue"], panel_size=(90, 120))

    main = crop_grid(main_grid, tmp_path / "main", layout="1x2", basename="main")
    detail = crop_grid(detail_grid, tmp_path / "detail", layout="1x3", basename="detail")

    assert len(main) == 2
    assert len(detail) == 3
    assert [Image.open(path).size for path in main] == [(90, 120), (90, 120)]
    assert [Image.open(path).getpixel((45, 60)) for path in detail] == [
        (255, 0, 0),
        (0, 128, 0),
        (0, 0, 255),
    ]


def test_crop_protocol_rejects_square_panels_instead_of_shipping_them(tmp_path: Path) -> None:
    grid = tmp_path / "square-main-grid.png"
    _solid_grid(grid, ["red", "green"], panel_size=(120, 120))

    with pytest.raises(ValueError, match="3:4"):
        crop_grid(grid, tmp_path / "main", layout="1x2", basename="main")


def test_crop_protocol_normalizes_near_three_by_four_panels_exactly(tmp_path: Path) -> None:
    grid = tmp_path / "near-three-by-four-grid.png"
    _solid_grid(grid, ["red", "green"], panel_size=(91, 120))

    outputs = crop_grid(grid, tmp_path / "main", layout="1x2", basename="main")

    assert [Image.open(path).size for path in outputs] == [(90, 120), (90, 120)]
    assert all(
        Image.open(path).width * 4 == Image.open(path).height * 3
        for path in outputs
    )


def test_v4_receipt_rejects_non_three_by_four_finished_slot(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "source.png"
    output = tmp_path / "square-output.png"
    _solid_grid(source, ["red", "green"], panel_size=(90, 120))
    Image.new("RGB", (800, 800), "red").save(output)
    result = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
        prompt_version="ozon-image-v4",
        validation=_v3_validation(slot["slot_id"], "f" * 64),
    )

    assert any("3:4" in error for error in result.acceptance_contract_errors())


def test_v4_receipt_requires_slot_specific_ozon_reference_mapping(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    _solid_grid(source, ["red", "green"], panel_size=(90, 120))
    Image.new("RGB", (90, 120), "red").save(output)
    validation = _v3_validation(
        slot["slot_id"],
        "f" * 64,
        visual_contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
    )
    validation.pop("primary_ozon_reference_sha256")
    result = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
        prompt_version=CURRENT_PROMPT_VERSION,
        validation=validation,
    )

    assert any(
        "primary_ozon_reference_sha256" in error
        for error in result.acceptance_contract_errors()
    )


def test_v4_receipt_rejects_unverified_or_low_resolution_reference(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    reference = tmp_path / "reference.png"
    _solid_grid(source, ["red", "green"], panel_size=(90, 120))
    Image.new("RGB", (90, 120), "red").save(output)
    Image.new("RGB", (50, 50), "blue").save(reference)
    validation = _v3_validation(
        slot["slot_id"],
        "f" * 64,
        primary_ozon_reference_path=reference,
        visual_contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
    )
    validation["primary_ozon_reference_sha256"] = "0" * 64
    result = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
        prompt_version=CURRENT_PROMPT_VERSION,
        validation=validation,
    )

    errors = result.acceptance_contract_errors()

    assert any("reference SHA-256 does not match" in error for error in errors)
    assert any("at least 512 pixels" in error for error in errors)


def test_v5_fallback_accepts_a_declared_layout_without_an_ozon_reference(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "fallback-source.png"
    output = tmp_path / "fallback-output.png"
    _solid_grid(source, ["red", "green"], panel_size=(90, 120))
    Image.new("RGB", (90, 120), "red").save(output)
    validation = _v3_validation(
        slot["slot_id"],
        "f" * 64,
        visual_contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
    )
    validation.update(
        {
            "guidance_mode": "ozon_aesthetic_fallback",
            "reference_reused": False,
        }
    )
    validation.pop("primary_ozon_reference_sha256")
    result = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
        prompt_version=CURRENT_PROMPT_VERSION,
        validation=validation,
    )

    assert result.acceptance_contract_errors() == []


def test_v5_gallery_rejects_background_swap_layouts_and_reference_overuse(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "layout-source.png"
    output = tmp_path / "layout-output.png"
    _solid_grid(source, ["red", "green"], panel_size=(90, 120))
    Image.new("RGB", (90, 120), "red").save(output)
    base = _slot_receipt(
        job=job,
        slot=slot,
        source_path=source,
        output_path=output,
        accepted=True,
        prompt_version=CURRENT_PROMPT_VERSION,
        validation=_v3_validation(
            slot["slot_id"],
            "f" * 64,
            visual_contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
        ),
    )
    repeated = tuple(
        replace(
            base,
            slot_id=f"slot_{index}",
            validation={
                **base.validation,
                "reference_layout_archetype": "lifestyle",
                "primary_ozon_reference_sha256": "a" * 64,
            },
        )
        for index in range(8)
    )

    errors = validate_reference_layout_diversity(repeated)

    assert any("at least 6" in error for error in errors)
    assert any("at most 2" in error for error in errors)


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


def test_v2_accepted_receipt_remains_valid_but_v3_requires_visual_metadata(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    legacy = _slot_receipt(
        job=job, slot=slot, source_path=source, output_path=output, accepted=True
    )

    assert legacy.acceptance_contract_errors() == []
    errors = replace(legacy, prompt_version="ozon-image-v3").acceptance_contract_errors()
    for field in (
        "visual_design_passed",
        "russian_copy_passed",
        "safe_area_passed",
        "mobile_readability_passed",
        "visual_spec is required",
    ):
        assert any(field in error for error in errors)


def test_v3_accepted_slot_rejects_unlocked_supplier_evidence(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    master = _master(tmp_path, receipt)
    queue.enqueue(receipt=receipt, subject_master=master)
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[1]
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)

    with pytest.raises(ValueError, match="locked supplier evidence"):
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=source,
                output_path=output,
                accepted=True,
                prompt_version="ozon-image-v3",
                validation=_v3_validation(slot["slot_id"], "f" * 64),
            )
        )


def _record_eight_v3_slots(
    tmp_path: Path,
    *,
    repeated_scene: bool = False,
    mixed_v2: bool = False,
) -> tuple[ImageGenerationQueue, dict]:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    master = _master(tmp_path, receipt)
    queue.enqueue(receipt=receipt, subject_master=master)
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    source = tmp_path / "source.png"
    _solid_grid(source, ["red", "green", "blue"])
    colors = [
        "red",
        "green",
        "blue",
        "orange",
        "purple",
        "cyan",
        "yellow",
        "brown",
    ]
    for index, slot in enumerate(queue.list_slots(job["job_id"])):
        output = tmp_path / f"output-{slot['slot_id']}.png"
        Image.new("RGB", (120, 90), colors[index]).save(output)
        prompt_version = "ozon-image-v2" if mixed_v2 and index == 0 else "ozon-image-v3"
        validation = None if prompt_version == "ozon-image-v2" else _v3_validation(
            slot["slot_id"], master.source_sha256, repeated_scene=repeated_scene
        )
        queue.record_slot_result(
            _slot_receipt(
                job=job,
                slot=slot,
                source_path=source,
                output_path=output,
                accepted=True,
                prompt_version=prompt_version,
                validation=validation,
            )
        )
    return queue, job


def _force_manual_review_job(queue: ImageGenerationQueue, job_id: str) -> None:
    with queue._connect() as connection:
        connection.execute(
            """
            UPDATE image_jobs
            SET status = 'manual_review_required', worker_id = NULL,
                lease_expires = NULL, heartbeat_at = NULL
            WHERE job_id = ?
            """,
            (job_id,),
        )


def _record_user_requested_v3_repair(
    tmp_path: Path,
    queue: ImageGenerationQueue,
    claimed: dict,
    *,
    slot_id: str = "main_02",
) -> dict:
    queue.request_repairs(
        claimed["job_id"],
        [{"slot_id": slot_id, "issue_code": "scene_quality", "note": "场景不真实"}],
        now_epoch=500,
    )
    repair_job = queue.claim_next(
        "ozon-image-worker-02", now_epoch=501, lease_seconds=30
    )
    assert repair_job and repair_job["job_id"] == claimed["job_id"]
    repair_slot = next(
        slot for slot in queue.list_slots(claimed["job_id"])
        if slot["slot_id"] == slot_id
    )
    master = SubjectMasterSelection.from_dict(
        json.loads(repair_job["subject_master_json"])
    )
    source = tmp_path / f"{slot_id}-repair-source.png"
    output = tmp_path / f"{slot_id}-repair-output.png"
    reference = tmp_path / f"{slot_id}-ozon-reference.png"
    _solid_grid(source, ["red", "green"], panel_size=(90, 120))
    Image.new("RGB", (90, 120), "black").save(output)
    Image.new("RGB", (600, 800), "navy").save(reference)
    queue.record_slot_result(
        _slot_receipt(
            job=repair_job,
            slot=repair_slot,
            source_path=source,
            output_path=output,
            accepted=True,
            source_kind="repair_single",
            prompt_version=CURRENT_PROMPT_VERSION,
            validation=_v3_validation(
                slot_id,
                master.source_sha256,
                primary_ozon_reference_path=reference,
                visual_contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
            ),
        )
    )
    return repair_job


def test_ready_for_review_rejects_mixed_v2_and_v3_receipts(tmp_path: Path) -> None:
    queue, job = _record_eight_v3_slots(tmp_path, mixed_v2=True)

    with pytest.raises(ValueError, match="mixed prompt versions are not allowed"):
        queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])
    assert queue.get_job(job["job_id"])["status"] == "in_progress"


def test_user_requested_v3_repair_can_coexist_with_frozen_legacy_slots(
    tmp_path: Path,
) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path, mixed_v2=True)
    _force_manual_review_job(queue, claimed["job_id"])
    repair_job = _record_user_requested_v3_repair(tmp_path, queue, claimed)

    reviewed = queue.mark_ready_for_review(
        repair_job["job_id"], repair_job["worker_id"], repair_job["lease_epoch"]
    )

    assert reviewed["status"] == "manual_review_required"
    frozen = queue.list_slots(claimed["job_id"])[0]
    assert json.loads(frozen["receipt_json"])["prompt_version"] == "ozon-image-v2"
    assert frozen["review_requested_at"] is None


def test_user_requested_v3_repair_can_preserve_historical_v1_receipt(
    tmp_path: Path,
) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path, mixed_v2=True)
    first_slot = queue.list_slots(claimed["job_id"])[0]
    historical = _rehash_receipt(
        SlotResultReceipt.from_dict(json.loads(first_slot["receipt_json"])),
        prompt_version="ozon-image-v1",
    )
    with queue._connect() as connection:
        connection.execute(
            "UPDATE image_slots SET receipt_json = ? WHERE job_id = ? AND slot_id = ?",
            (
                json.dumps(historical.to_dict(), ensure_ascii=False, sort_keys=True),
                claimed["job_id"],
                first_slot["slot_id"],
            ),
        )
    _force_manual_review_job(queue, claimed["job_id"])
    repair_job = _record_user_requested_v3_repair(tmp_path, queue, claimed)

    reviewed = queue.mark_ready_for_review(
        repair_job["job_id"], repair_job["worker_id"], repair_job["lease_epoch"]
    )

    assert reviewed["status"] == "manual_review_required"
    frozen = queue.list_slots(claimed["job_id"])[0]
    assert json.loads(frozen["receipt_json"])["prompt_version"] == "ozon-image-v1"
    assert frozen["review_requested_at"] is None


def test_ready_for_review_rejects_repeated_v3_scenes(tmp_path: Path) -> None:
    queue, job = _record_eight_v3_slots(tmp_path, repeated_scene=True)

    with pytest.raises(ValueError, match="visual set validation failed"):
        queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])
    assert queue.get_job(job["job_id"])["status"] == "in_progress"


def test_v3_spec_slot_mismatch_and_unknown_prompt_version_are_rejected(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    master = _master(tmp_path, receipt)
    queue.enqueue(receipt=receipt, subject_master=master)
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[1]
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    bad_validation = _v3_validation(slot["slot_id"], master.source_sha256)
    bad_validation["visual_spec"]["slot_id"] = "detail_01"

    with pytest.raises(ValueError, match="slot_id"):
        queue.record_slot_result(
            _slot_receipt(
                job=job, slot=slot, source_path=source, output_path=output,
                accepted=True, prompt_version="ozon-image-v3", validation=bad_validation,
            )
        )
    with pytest.raises(ValueError, match="prompt_version"):
        queue.record_slot_result(
            _slot_receipt(
                job=job, slot=slot, source_path=source, output_path=output,
                accepted=True, prompt_version="unknown", validation=_v3_validation(
                    slot["slot_id"], master.source_sha256
                ),
            )
        )


def test_all_valid_v3_slots_can_be_ready_for_review(tmp_path: Path) -> None:
    queue, job = _record_eight_v3_slots(tmp_path)

    reviewed = queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])

    assert reviewed["status"] == "manual_review_required"


def test_manual_review_approval_rechecks_receipts_and_completes_job(tmp_path: Path) -> None:
    queue, job = _record_eight_v3_slots(tmp_path)
    queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])

    completed = queue.approve_review(job["job_id"])

    assert completed["status"] == "completed"
    assert queue.get_job(job["job_id"])["status"] == "completed"


def test_ready_for_review_rejects_pixel_identical_outputs_even_with_distinct_scene_labels(
    tmp_path: Path,
) -> None:
    queue, job = _record_eight_v3_slots(tmp_path)
    slots = queue.list_slots(job["job_id"])
    first = slots[0]
    second = slots[1]
    first_receipt = SlotResultReceipt.from_dict(json.loads(first["receipt_json"]))
    second_receipt = SlotResultReceipt.from_dict(json.loads(second["receipt_json"]))
    duplicate = _rehash_receipt(
        second_receipt,
        output_path=first_receipt.output_path,
        output_sha256=first_receipt.output_sha256,
    )
    with queue._connect() as connection:
        connection.execute(
            "UPDATE image_slots SET accepted_path = ?, receipt_json = ? "
            "WHERE job_id = ? AND slot_id = ?",
            (
                duplicate.output_path,
                json.dumps(duplicate.to_dict(), ensure_ascii=False, sort_keys=True),
                job["job_id"],
                second["slot_id"],
            ),
        )

    with pytest.raises(ValueError, match="pixel-identical"):
        queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])


def test_user_review_requeues_only_selected_slots_and_preserves_old_outputs(
    tmp_path: Path,
) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path)
    queue.mark_ready_for_review(
        claimed["job_id"], claimed["worker_id"], claimed["lease_epoch"]
    )
    before = {
        slot["slot_id"]: slot for slot in queue.list_slots(claimed["job_id"])
    }

    result = queue.request_repairs(
        claimed["job_id"],
        [
            {
                "slot_id": "main_02",
                "issue_code": "russian_copy",
                "note": "俄文太小",
            },
            {
                "slot_id": "detail_03",
                "issue_code": "selling_point",
                "note": "用途不清楚",
            },
        ],
        now_epoch=500,
    )

    after = {
        slot["slot_id"]: slot for slot in queue.list_slots(claimed["job_id"])
    }
    assert result["job"]["status"] == "pending"
    assert result["job"]["worker_id"] is None
    assert result["job"]["lease_expires"] is None
    assert result["job"]["heartbeat_at"] is None
    assert {
        slot_id
        for slot_id, slot in after.items()
        if slot["status"] == "repair_pending"
    } == {"main_02", "detail_03"}
    assert after["main_02"]["accepted_path"] == before["main_02"]["accepted_path"]
    assert after["main_02"]["receipt_json"] == before["main_02"]["receipt_json"]
    assert after["main_02"]["review_issue_code"] == "russian_copy"
    assert after["main_02"]["review_note"] == "俄文太小"
    assert after["main_02"]["review_requested_at"] == 500
    assert after["detail_03"]["review_issue_code"] == "selling_point"
    assert after["detail_03"]["review_note"] == "用途不清楚"
    for slot_id in set(before) - {"main_02", "detail_03"}:
        assert after[slot_id] == before[slot_id]


def test_user_requested_repair_rejects_legacy_receipt_without_state_change(
    tmp_path: Path,
) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path)
    queue.mark_ready_for_review(
        claimed["job_id"], claimed["worker_id"], claimed["lease_epoch"]
    )
    queue.request_repairs(
        claimed["job_id"],
        [{"slot_id": "main_02", "issue_code": "scene_quality", "note": ""}],
        now_epoch=500,
    )
    repair_job = queue.claim_next(
        "ozon-image-worker-02", now_epoch=501, lease_seconds=30
    )
    assert repair_job
    repair_slot = next(
        slot
        for slot in queue.list_slots(claimed["job_id"])
        if slot["slot_id"] == "main_02"
    )
    source = tmp_path / "legacy-repair-source.png"
    output = tmp_path / "legacy-repair-output.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    legacy_repair = _slot_receipt(
        job=repair_job,
        slot=repair_slot,
        source_path=source,
        output_path=output,
        accepted=True,
        source_kind="repair_single",
        prompt_version="ozon-image-v2",
    )
    before = queue.snapshot(claimed["job_id"])

    with pytest.raises(
        ValueError,
        match=f"user-selected repair.*{CURRENT_PROMPT_VERSION}",
    ):
        queue.record_slot_result(legacy_repair)

    assert queue.snapshot(claimed["job_id"]) == before


def test_rejected_user_requested_repair_preserves_previous_accepted_artifact(
    tmp_path: Path,
) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path)
    queue.mark_ready_for_review(
        claimed["job_id"], claimed["worker_id"], claimed["lease_epoch"]
    )
    before_request = next(
        slot
        for slot in queue.list_slots(claimed["job_id"])
        if slot["slot_id"] == "detail_03"
    )
    queue.request_repairs(
        claimed["job_id"],
        [{"slot_id": "detail_03", "issue_code": "scene_quality", "note": ""}],
        now_epoch=500,
    )
    repair_job = queue.claim_next(
        "ozon-image-worker-02", now_epoch=501, lease_seconds=30
    )
    assert repair_job
    repair_slot = next(
        slot
        for slot in queue.list_slots(claimed["job_id"])
        if slot["slot_id"] == "detail_03"
    )
    master = SubjectMasterSelection.from_dict(
        json.loads(repair_job["subject_master_json"])
    )
    source = tmp_path / "rejected-repair-source.png"
    output = tmp_path / "rejected-repair-output.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    rejected = _slot_receipt(
        job=repair_job,
        slot=repair_slot,
        source_path=source,
        output_path=output,
        accepted=False,
        source_kind="repair_single",
        prompt_version=CURRENT_PROMPT_VERSION,
        validation=_v3_validation(
            "detail_03",
            master.source_sha256,
            visual_contract_version=CURRENT_VISUAL_CONTRACT_VERSION,
        ),
    )

    updated = queue.record_slot_result(rejected)

    assert updated["status"] == "repair_pending"
    assert updated["attempt_count"] == before_request["attempt_count"] + 1
    assert updated["repair_count"] == before_request["repair_count"] + 1
    assert updated["accepted_path"] == before_request["accepted_path"]
    assert updated["receipt_json"] == before_request["receipt_json"]
    attempt = queue.list_attempts(claimed["job_id"])[-1]
    assert attempt["accepted"] == 0
    assert json.loads(attempt["receipt_json"])["receipt_sha256"] == rejected.receipt_sha256


@pytest.mark.parametrize(
    ("repairs", "expected_code"),
    [
        ([], "image_job.repair_selection_invalid"),
        (
            [
                {"slot_id": "main_01", "issue_code": "composition", "note": ""},
                {"slot_id": "main_01", "issue_code": "scene_quality", "note": ""},
            ],
            "image_job.repair_selection_invalid",
        ),
        (
            [{"slot_id": "main_01", "issue_code": "unknown", "note": ""}],
            "image_job.repair_feedback_invalid",
        ),
        (
            [{"slot_id": "main_01", "issue_code": "other", "note": "   "}],
            "image_job.repair_feedback_invalid",
        ),
        (
            [{"slot_id": "main_01", "issue_code": "composition", "note": "x" * 501}],
            "image_job.repair_feedback_invalid",
        ),
        (
            [{"slot_id": "missing", "issue_code": "composition", "note": ""}],
            "image_job.repair_slot_invalid",
        ),
    ],
)
def test_repair_request_validation_is_atomic(
    tmp_path: Path,
    repairs: list[dict[str, str]],
    expected_code: str,
) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path)
    queue.mark_ready_for_review(
        claimed["job_id"], claimed["worker_id"], claimed["lease_epoch"]
    )
    before = queue.snapshot(claimed["job_id"])

    with pytest.raises(ValueError) as caught:
        queue.request_repairs(claimed["job_id"], repairs, now_epoch=500)

    assert getattr(caught.value, "code", None) == expected_code
    assert queue.snapshot(claimed["job_id"]) == before


def test_repair_request_rejects_non_reviewable_job_without_side_effects(
    tmp_path: Path,
) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path)
    before = queue.snapshot(claimed["job_id"])

    with pytest.raises(ValueError) as caught:
        queue.request_repairs(
            claimed["job_id"],
            [{"slot_id": "main_01", "issue_code": "composition", "note": ""}],
            now_epoch=500,
        )

    assert getattr(caught.value, "code", None) == "image_job.repair_not_reviewable"
    assert queue.snapshot(claimed["job_id"]) == before


def test_repair_request_rejects_exhausted_slot_without_side_effects(
    tmp_path: Path,
) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path)
    queue.mark_ready_for_review(
        claimed["job_id"], claimed["worker_id"], claimed["lease_epoch"]
    )
    with queue._connect() as connection:
        connection.execute(
            "UPDATE image_slots SET repair_count = 2 WHERE job_id = ? AND slot_id = ?",
            (claimed["job_id"], "detail_01"),
        )
    before = queue.snapshot(claimed["job_id"])

    with pytest.raises(ValueError) as caught:
        queue.request_repairs(
            claimed["job_id"],
            [{"slot_id": "detail_01", "issue_code": "scene_quality", "note": ""}],
            now_epoch=500,
        )

    assert getattr(caught.value, "code", None) == "image_job.repair_limit_reached"
    assert queue.snapshot(claimed["job_id"]) == before


def test_receipt_payload_parser_and_acceptance_reject_non_mapping_validation_and_prompt_shape(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    slot = queue.list_slots(job["job_id"])[0]
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    valid = _slot_receipt(
        job=job, slot=slot, source_path=source, output_path=output, accepted=True
    )
    null_validation = _rehash_receipt(valid, validation=None)
    list_prompt_version = _rehash_receipt(valid, prompt_version=[])

    for malformed, message in (
        (null_validation, "validation must be a mapping"),
        (list_prompt_version, "prompt_version must be a string"),
    ):
        with pytest.raises(TypeError, match=message):
            SlotResultReceipt.from_dict(malformed.to_dict())
        assert any(message in error for error in malformed.acceptance_contract_errors())

    baseline = queue.snapshot(job["job_id"])
    with pytest.raises(ValueError, match="validation must be a mapping"):
        queue.record_slot_result(null_validation)
    assert queue.snapshot(job["job_id"]) == baseline
    with pytest.raises(ValueError, match="prompt_version must be a string"):
        queue.record_slot_result(list_prompt_version)
    assert queue.snapshot(job["job_id"]) == baseline


def test_finalization_rejects_persisted_null_validation_without_state_change(tmp_path: Path) -> None:
    receipt = _receipt()
    queue = ImageGenerationQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(receipt=receipt, subject_master=_master(tmp_path, receipt))
    job = queue.claim_next("ozon-image-worker-01", now_epoch=100, lease_seconds=30)
    assert job
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    _solid_grid(source, ["red", "green", "blue"])
    Image.new("RGB", (120, 90), "red").save(output)
    receipts: list[SlotResultReceipt] = []
    for slot in queue.list_slots(job["job_id"]):
        result = _slot_receipt(
            job=job, slot=slot, source_path=source, output_path=output, accepted=True
        )
        queue.record_slot_result(result)
        receipts.append(result)
    malformed = _rehash_receipt(receipts[0], validation=None)
    with queue._connect() as connection:
        connection.execute(
            "UPDATE image_slots SET receipt_json = ? WHERE job_id = ? AND slot_id = ?",
            (json.dumps(malformed.to_dict()), job["job_id"], malformed.slot_id),
        )
    baseline = queue.snapshot(job["job_id"])

    with pytest.raises(ValueError, match="validation must be a mapping"):
        queue.mark_ready_for_review(job["job_id"], job["worker_id"], job["lease_epoch"])

    assert queue.snapshot(job["job_id"]) == baseline
