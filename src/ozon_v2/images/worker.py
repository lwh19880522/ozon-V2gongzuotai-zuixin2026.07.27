from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from ozon_v2.domain.supplier_sku import stable_sha256


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def crop_grid(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    layout: str,
    basename: str,
) -> list[Path]:
    panel_count = {"1x2": 2, "1x3": 3}.get(layout)
    if panel_count is None:
        raise ValueError("layout must be 1x2 or 1x3")
    source = Path(source_path)
    if not source.is_file():
        raise ValueError("grid source file does not exist")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    with Image.open(source) as image:
        if image.width < panel_count or image.height < 1:
            raise ValueError("grid image is too small for the requested layout")
        for index in range(panel_count):
            left = image.width * index // panel_count
            right = image.width * (index + 1) // panel_count
            panel = image.crop((left, 0, right, image.height))
            output = destination / f"{basename}-{index + 1:02d}.png"
            panel.save(output, format="PNG")
            outputs.append(output)
    return outputs


@dataclass(frozen=True)
class SlotResultReceipt:
    job_id: str
    slot_id: str
    worker_id: str
    lease_epoch: int
    selection_sha256: str
    subject_master_sha256: str
    prompt_version: str
    source_kind: str
    source_path: str
    source_sha256: str
    output_path: str
    output_sha256: str
    accepted: bool
    validation: dict[str, Any]
    created_at: str
    receipt_sha256: str

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        slot_id: str,
        worker_id: str,
        lease_epoch: int,
        selection_sha256: str,
        subject_master_sha256: str,
        prompt_version: str,
        source_kind: str,
        source_path: str | Path,
        output_path: str | Path,
        accepted: bool,
        validation: dict[str, Any],
        created_at: str,
    ) -> "SlotResultReceipt":
        source = Path(source_path).resolve()
        output = Path(output_path).resolve()
        if not source.is_file() or not output.is_file():
            raise ValueError("slot source and output files are required")
        if not worker_id.strip() or lease_epoch < 1:
            raise ValueError("an active worker lease is required")
        core = {
            "job_id": job_id,
            "slot_id": slot_id,
            "worker_id": worker_id,
            "lease_epoch": lease_epoch,
            "selection_sha256": selection_sha256,
            "subject_master_sha256": subject_master_sha256,
            "prompt_version": prompt_version,
            "source_kind": source_kind,
            "source_path": str(source),
            "source_sha256": file_sha256(source),
            "output_path": str(output),
            "output_sha256": file_sha256(output),
            "accepted": accepted,
            "validation": dict(validation),
            "created_at": created_at,
        }
        return cls(**core, receipt_sha256=stable_sha256(core))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SlotResultReceipt":
        return cls(**payload)

    def hash_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("receipt_sha256")
        return payload

    def verify(self) -> bool:
        source = Path(self.source_path)
        output = Path(self.output_path)
        return all(
            (
                self.receipt_sha256 == stable_sha256(self.hash_payload()),
                source.is_file(),
                output.is_file(),
                source.is_file() and file_sha256(source) == self.source_sha256,
                output.is_file() and file_sha256(output) == self.output_sha256,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
