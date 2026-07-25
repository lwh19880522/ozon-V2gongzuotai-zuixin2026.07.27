from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from ozon_v2.domain.supplier_sku import stable_sha256
from ozon_v2.images.visual_design import CURRENT_VISUAL_CONTRACT_VERSION


LEGACY_PROMPT_VERSION = "ozon-image-v2"
PREVIOUS_PROMPT_VERSION = "ozon-image-v3"
CURRENT_PROMPT_VERSION = "ozon-image-v4"
SUPPORTED_PROMPT_VERSIONS = frozenset(
    {LEGACY_PROMPT_VERSION, PREVIOUS_PROMPT_VERSION, CURRENT_PROMPT_VERSION}
)
VISUAL_PROMPT_VERSIONS = frozenset(
    {PREVIOUS_PROMPT_VERSION, CURRENT_PROMPT_VERSION}
)
REQUIRED_OUTPUT_ASPECT_RATIO = (3, 4)
MAX_SOURCE_PANEL_ASPECT_RATIO_DEVIATION = 0.05
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
REFERENCE_MAPPING_VERSION = "ozon-reference-map-v1"
REFERENCE_MAPPING_TRUE_FLAGS = (
    "reference_composition_followed",
    "locked_subject_preserved",
)
MINIMUM_OZON_REFERENCE_EDGE = 512


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


def is_exact_three_by_four_image(path: str | Path) -> bool:
    try:
        with Image.open(path) as image:
            return (
                image.width > 0
                and image.height > 0
                and image.width * REQUIRED_OUTPUT_ASPECT_RATIO[1]
                == image.height * REQUIRED_OUTPUT_ASPECT_RATIO[0]
            )
    except (OSError, ValueError):
        return False


def _normalize_panel_to_three_by_four(panel: Image.Image) -> Image.Image:
    ratio = panel.width / panel.height
    required_ratio = (
        REQUIRED_OUTPUT_ASPECT_RATIO[0] / REQUIRED_OUTPUT_ASPECT_RATIO[1]
    )
    if abs(ratio - required_ratio) > MAX_SOURCE_PANEL_ASPECT_RATIO_DEVIATION:
        raise ValueError(
            "Every generated grid panel must be near 3:4 portrait before deterministic crop."
        )
    scale = min(
        panel.width // REQUIRED_OUTPUT_ASPECT_RATIO[0],
        panel.height // REQUIRED_OUTPUT_ASPECT_RATIO[1],
    )
    if scale < 1:
        raise ValueError("Grid panel is too small for an exact 3:4 output.")
    target_width = scale * REQUIRED_OUTPUT_ASPECT_RATIO[0]
    target_height = scale * REQUIRED_OUTPUT_ASPECT_RATIO[1]
    left = (panel.width - target_width) // 2
    top = (panel.height - target_height) // 2
    return panel.crop(
        (left, top, left + target_width, top + target_height)
    )


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
            panel = _normalize_panel_to_three_by_four(panel)
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
        if not isinstance(payload, Mapping):
            raise TypeError("slot result receipt must be a mapping")
        normalized = dict(payload)
        validation = normalized.get("validation", {})
        if not isinstance(validation, Mapping):
            raise TypeError("validation must be a mapping")
        if not isinstance(normalized.get("prompt_version"), str):
            raise TypeError("prompt_version must be a string")
        normalized["validation"] = dict(validation)
        return cls(**normalized)

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
        if not isinstance(self.prompt_version, str):
            errors.append("prompt_version must be a string")
        elif self.prompt_version not in SUPPORTED_PROMPT_VERSIONS:
            errors.append(
                "prompt_version must be one of "
                + ", ".join(sorted(SUPPORTED_PROMPT_VERSIONS))
            )
        if not isinstance(self.validation, Mapping):
            errors.append("validation must be a mapping")
            return errors
        if not self.accepted:
            return errors
        if not str(self.validation.get("slot_role") or "").strip():
            errors.append("slot_role is required")
        for key in REQUIRED_ACCEPTANCE_FLAGS:
            if self.validation.get(key) is not True:
                errors.append(f"{key} must be true")
        if (
            isinstance(self.prompt_version, str)
            and self.prompt_version in VISUAL_PROMPT_VERSIONS
        ):
            for key in V3_VISUAL_FLAGS:
                if self.validation.get(key) is not True:
                    errors.append(f"{key} must be true")
            if not isinstance(self.validation.get("visual_spec"), dict):
                errors.append("visual_spec is required")
            if self.validation.get("visual_contract_version") != CURRENT_VISUAL_CONTRACT_VERSION:
                errors.append(
                    f"visual_contract_version must be {CURRENT_VISUAL_CONTRACT_VERSION}"
                )
        if self.prompt_version == CURRENT_PROMPT_VERSION:
            if (
                self.validation.get("reference_mapping_version")
                != REFERENCE_MAPPING_VERSION
            ):
                errors.append(
                    f"reference_mapping_version must be {REFERENCE_MAPPING_VERSION}"
                )
            reference_sha256 = str(
                self.validation.get("primary_ozon_reference_sha256") or ""
            ).lower()
            if (
                len(reference_sha256) != 64
                or any(character not in "0123456789abcdef" for character in reference_sha256)
            ):
                errors.append(
                    "primary_ozon_reference_sha256 must be a lowercase SHA-256 digest"
                )
            reference_path_value = str(
                self.validation.get("primary_ozon_reference_path") or ""
            ).strip()
            if not reference_path_value:
                errors.append("primary_ozon_reference_path is required")
            else:
                reference_path = Path(reference_path_value)
                if not reference_path.is_file():
                    errors.append("primary Ozon reference file does not exist")
                else:
                    if file_sha256(reference_path) != reference_sha256:
                        errors.append("primary Ozon reference SHA-256 does not match")
                    try:
                        with Image.open(reference_path) as reference_image:
                            if min(reference_image.size) < MINIMUM_OZON_REFERENCE_EDGE:
                                errors.append(
                                    "primary Ozon reference edge must be at least 512 pixels"
                                )
                    except OSError:
                        errors.append("primary Ozon reference pixels are unavailable")
            reference_slot_index = self.validation.get("reference_slot_index")
            if (
                not isinstance(reference_slot_index, int)
                or isinstance(reference_slot_index, bool)
                or reference_slot_index < 1
            ):
                errors.append("reference_slot_index must be a positive integer")
            if not isinstance(self.validation.get("reference_reused"), bool):
                errors.append("reference_reused must be a boolean")
            for key in REFERENCE_MAPPING_TRUE_FLAGS:
                if self.validation.get(key) is not True:
                    errors.append(f"{key} must be true")
            visual_spec = self.validation.get("visual_spec")
            facts = visual_spec.get("facts", []) if isinstance(visual_spec, dict) else []
            if self.slot_id != "main_01":
                russian_text = " ".join(
                    str(fact.get(key, ""))
                    for fact in facts
                    if isinstance(fact, dict)
                    for key in ("headline", "detail")
                )
                if not re.search(r"[\u0400-\u04ff]", russian_text):
                    errors.append("supporting slot must contain verified Russian labels")
                source_digest = str(
                    self.validation.get("visual_source_sha256") or ""
                ).lower()
                output_digest = str(
                    self.validation.get("visual_output_sha256") or ""
                ).lower()
                if output_digest != self.output_sha256:
                    errors.append("visual_output_sha256 must match the accepted output")
                if len(source_digest) != 64 or source_digest == output_digest:
                    errors.append(
                        "supporting slot must persist a locally rendered Russian label layer"
                    )
        if (
            self.prompt_version == CURRENT_PROMPT_VERSION
            and not is_exact_three_by_four_image(self.output_path)
        ):
            errors.append("output must be exactly 3:4 portrait")
        try:
            if looks_plain_or_near_white_product_only(self.output_path):
                errors.append("output contains near-white product-only pixels")
        except OSError:
            errors.append("output pixels are unavailable")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_output_diversity(
    receipts: tuple[SlotResultReceipt, ...],
    *,
    minimum_mean_rgb_delta: float = 6.0,
) -> list[str]:
    """Reject duplicate or visually near-duplicate finished slots."""
    errors: list[str] = []
    previews: dict[str, Image.Image] = {}
    for receipt in receipts:
        with Image.open(receipt.output_path) as source:
            previews[receipt.slot_id] = source.convert("RGB").resize((64, 64))
    for index, left in enumerate(receipts):
        for right in receipts[index + 1 :]:
            if left.output_sha256 == right.output_sha256:
                errors.append(
                    f"output slots {left.slot_id} and {right.slot_id} are pixel-identical"
                )
                continue
            difference = ImageChops.difference(
                previews[left.slot_id], previews[right.slot_id]
            )
            mean_rgb_delta = sum(ImageStat.Stat(difference).mean) / 3
            if mean_rgb_delta < minimum_mean_rgb_delta:
                errors.append(
                    f"output slots {left.slot_id} and {right.slot_id} are visually near-duplicate"
                )
    return errors
