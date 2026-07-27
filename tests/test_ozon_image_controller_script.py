from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import ozon_image_worker
from ozon_v2.domain.supplier_sku import SupplierSkuOption, SupplierSkuSelectionReceipt
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.queue import ImageGenerationQueue


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_SCRIPT = ROOT / "scripts" / "ozon_image_controller.py"
WORKER_SCRIPT = ROOT / "scripts" / "ozon_image_worker.py"


def test_worker_default_lease_covers_blocking_image_generation_calls() -> None:
    parser = ozon_image_worker.build_parser()

    claim = parser.parse_args(
        ["--db", "queue.sqlite3", "claim", "--worker", "ozon-image-worker-01"]
    )
    assigned = parser.parse_args(
        [
            "--db",
            "queue.sqlite3",
            "claim-assigned",
            "--worker",
            "ozon-image-worker-01",
            "--job",
            "img-job",
            "--instruction-id",
            "assign-job",
        ]
    )
    heartbeat = parser.parse_args(
        [
            "--db",
            "queue.sqlite3",
            "heartbeat",
            "--job",
            "img-job",
            "--worker",
            "ozon-image-worker-01",
            "--epoch",
            "1",
        ]
    )

    assert claim.lease_seconds >= 1800
    assert assigned.lease_seconds >= 1800
    assert heartbeat.lease_seconds >= 1800


def _queued_product(tmp_path: Path) -> tuple[SupplierSkuSelectionReceipt, SubjectMasterSelection]:
    sku = SupplierSkuOption(
        supplier_sku_id="sku-script",
        combination_key="black>single",
        raw_label="black / single",
        selected_options={"color": "black"},
        set_quantity=1,
        set_composition=["1 piece"],
        price={"currency": "CNY", "amount": "10.00"},
        stock={"status": "in_stock", "quantity": 10},
        image_urls=["https://cbu01.alicdn.com/img/ibank/sku-script.jpg"],
        evidence_source="embedded_sku_map",
        complete=True,
    )
    receipt = SupplierSkuSelectionReceipt.confirmed(
        run_id="wb-image",
        product_id="ozon-script",
        supplier_offer_id="offer-script",
        supplier_sku=sku,
        ozon_target_sku={"sku_id": "ozon-script-sku"},
        differences=[],
        confirmed_at="2026-07-25T00:00:00+00:00",
    )
    source = tmp_path / "script-subject.png"
    source.write_bytes(b"script-subject")
    master = SubjectMasterSelection.create(
        receipt=receipt,
        source_path=source,
        source_image_url=sku.image_urls[0],
        visible_subject_quantity=1,
        white_background_confirmed=True,
        confirmed_at="2026-07-25T00:01:00+00:00",
    )
    return receipt, master


def test_controller_script_registers_a_fixed_task_and_emits_a_short_assignment(
    tmp_path: Path,
) -> None:
    assert CONTROLLER_SCRIPT.is_file(), "the lightweight controller script is required"
    db_path = tmp_path / "image_jobs.sqlite3"
    queue = ImageGenerationQueue(db_path)
    receipt, master = _queued_product(tmp_path)
    job = queue.enqueue(
        receipt=receipt,
        subject_master=master,
    )

    registered = subprocess.run(
        [
            sys.executable,
            str(CONTROLLER_SCRIPT),
            "--db",
            str(db_path),
            "register",
            "--worker",
            "ozon-image-worker-01",
            "--thread-id",
            "thread-visible-01",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(registered.stdout)["worker_id"] == "ozon-image-worker-01"

    dispatched = subprocess.run(
        [
            sys.executable,
            str(CONTROLLER_SCRIPT),
            "--db",
            str(db_path),
            "dispatch",
            "--run",
            "wb-image",
            "--limit",
            "10",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(dispatched.stdout)

    assert payload["summary"]["assigned"] == 1
    assert payload["assignments"][0]["job_id"] == job["job_id"]
    assert payload["assignments"][0]["thread_id"] == "thread-visible-01"
    assert payload["assignments"][0]["command"].startswith("RUN instruction_id=")


def test_worker_script_claims_only_the_controller_assignment(tmp_path: Path) -> None:
    db_path = tmp_path / "image_jobs.sqlite3"
    queue = ImageGenerationQueue(db_path)
    receipt, master = _queued_product(tmp_path)
    queue.enqueue(receipt=receipt, subject_master=master)
    queue.register_worker_slot("ozon-image-worker-01", "thread-visible-01")
    assignment = queue.dispatch_assignments(run_id="wb-image", limit=1)[0]

    claimed = subprocess.run(
        [
            sys.executable,
            str(WORKER_SCRIPT),
            "--db",
            str(db_path),
            "claim-assigned",
            "--worker",
            assignment["worker_id"],
            "--job",
            assignment["job_id"],
            "--instruction-id",
            assignment["instruction_id"],
            "--lease-seconds",
            "30",
        ],
        capture_output=True,
        text=True,
    )

    assert claimed.returncode == 0, claimed.stderr
    payload = json.loads(claimed.stdout)
    assert payload["job"]["job_id"] == assignment["job_id"]
    assert payload["job"]["worker_id"] == assignment["worker_id"]
