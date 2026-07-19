from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from ozon_v2.domain.supplier_sku import stable_sha256
from ozon_v2.images.visual_design import CURRENT_VISUAL_CONTRACT_VERSION


LEGACY_PROMPT_VERSION = "ozon-image-v2"
CURRENT_PROMPT_VERSION = "ozon-image-v3"
REQUIRED_ACCEPTANCE_FLAGS = (
    "product_truth",
    "slot_role_satisfied",
    "role_visually_demonstrated",
    "not_plain_or_near_white_product_only",
    "distinct_from_accepted_slots",
    "copy_not_used_as_visual_evidence",
)
V3_VISUAL_FLAGS = (
    "visual_design_passed",
    "russian_copy_passed",
    "safe_area_passed",
    "mobile_readability_passed",
)


def looks_plain_or_near_white_product_only(path: str | Path) -> bool:
    """Reject the washed-out white-anchor treatment seen in finished marketing slots."""
    with Image.open(path) as source:
        sample = source.convert("RGB")
        sample.thumbnail((96, 96))
        if hasattr(sample, "get_flattened_data"):
            pixels = list(sample.get_flattened_data())
        else:  # Pillow < 12 compatibility
            pixels = list(sample.getdata())
    if not pixels:
        return True
    luminance: list[float] = []
    near_white = 0
    total_chroma = 0.0
    for red, green, blue in pixels:
        luminance.append(0.2126 * red + 0.7152 * green + 0.0722 * blue)
        chroma = max(red, green, blue) - min(red, green, blue)
        total_chroma += chroma
        if min(red, green, blue) >= 225 and chroma <= 22:
            near_white += 1
    mean_luminance = sum(luminance) / len(luminance)
    variance = sum((value - mean_luminance) ** 2 for value in luminance) / len(luminance)
    luminance_deviation = variance**0.5
    near_white_ratio = near_white / len(pixels)
    mean_chroma = total_chroma / len(pixels)
    whole_image_is_washed_out = (
        mean_luminance >= 215
        and mean_chroma <= 10
        and (near_white_ratio >= 0.60 or luminance_deviation <= 18)
    )
    width, height = sample.size
    frame_width = max(1, min(width, height) // 10)
    frame_pixels = [
        pixel
        for index, pixel in enumerate(pixels)
        if (
            index % width < frame_width
            or index % width >= width - frame_width
            or index // width < frame_width
            or index // width >= height - frame_width
        )
    ]
    frame_near_white = 0
    frame_chroma = 0.0
    frame_luminance = 0.0
    for red, green, blue in frame_pixels:
        chroma = max(red, green, blue) - min(red, green, blue)
        frame_chroma += chroma
        frame_luminance += 0.2126 * red + 0.7152 * green + 0.0722 * blue
        if min(red, green, blue) >= 220 and chroma <= 28:
            frame_near_white += 1
    frame_is_plain_near_white = (
        frame_near_white / len(frame_pixels) >= 0.70
        and frame_chroma / len(frame_pixels) <= 12
        and frame_luminance / len(frame_pixels) >= 215
    )
    return whole_image_is_washed_out or frame_is_plain_near_white


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

    def acceptance_contract_errors(self) -> list[str]:
        errors: list[str] = []
        if self.prompt_version not in {LEGACY_PROMPT_VERSION, CURRENT_PROMPT_VERSION}:
            errors.append(
                f"prompt_version must be {LEGACY_PROMPT_VERSION} or {CURRENT_PROMPT_VERSION}"
            )
        if not self.accepted:
            return errors
        if not str(self.validation.get("slot_role") or "").strip():
            errors.append("slot_role is required")
        for key in REQUIRED_ACCEPTANCE_FLAGS:
            if self.validation.get(key) is not True:
                errors.append(f"{key} must be true")
        if self.prompt_version == CURRENT_PROMPT_VERSION:
            for key in V3_VISUAL_FLAGS:
                if self.validation.get(key) is not True:
                    errors.append(f"{key} must be true")
            if not isinstance(self.validation.get("visual_spec"), dict):
                errors.append("visual_spec is required")
            if self.validation.get("visual_contract_version") != CURRENT_VISUAL_CONTRACT_VERSION:
                errors.append(
                    f"visual_contract_version must be {CURRENT_VISUAL_CONTRACT_VERSION}"
                )
        try:
            if looks_plain_or_near_white_product_only(self.output_path):
                errors.append("output contains near-white product-only pixels")
        except OSError:
            errors.append("output pixels are unavailable")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
