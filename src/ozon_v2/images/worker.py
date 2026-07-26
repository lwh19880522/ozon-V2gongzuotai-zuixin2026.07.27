from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from ozon_v2.domain.supplier_sku import stable_sha256
from ozon_v2.images.visual_design import (
    CURRENT_VISUAL_CONTRACT_VERSION,
    HISTORICAL_VISUAL_CONTRACT_VERSION,
)


LEGACY_PROMPT_VERSION = "ozon-image-v2"
VISUAL_PROMPT_VERSION = "ozon-image-v3"
PREVIOUS_PROMPT_VERSION = "ozon-image-v4"
REFERENCE_LAYOUT_PROMPT_VERSION = "ozon-image-v5"
INTEGRATED_COPY_PROMPT_VERSION = "ozon-image-v6"
IDENTITY_ANCHOR_PROMPT_VERSION = "ozon-image-v7"
CURRENT_PROMPT_VERSION = "ozon-image-v8"
SUPPORTED_PROMPT_VERSIONS = frozenset(
    {
        LEGACY_PROMPT_VERSION,
        VISUAL_PROMPT_VERSION,
        PREVIOUS_PROMPT_VERSION,
        REFERENCE_LAYOUT_PROMPT_VERSION,
        INTEGRATED_COPY_PROMPT_VERSION,
        IDENTITY_ANCHOR_PROMPT_VERSION,
        CURRENT_PROMPT_VERSION,
    }
)
VISUAL_PROMPT_VERSIONS = frozenset(
    {
        VISUAL_PROMPT_VERSION,
        PREVIOUS_PROMPT_VERSION,
        REFERENCE_LAYOUT_PROMPT_VERSION,
        INTEGRATED_COPY_PROMPT_VERSION,
        IDENTITY_ANCHOR_PROMPT_VERSION,
        CURRENT_PROMPT_VERSION,
    }
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
V5_REFERENCE_MAPPING_TRUE_FLAGS = (
    *REFERENCE_MAPPING_TRUE_FLAGS,
    "reference_layout_followed",
)
REFERENCE_LAYOUT_ARCHETYPES = frozenset(
    {
        "hero",
        "lifestyle",
        "functional_infographic",
        "annotated_feature",
        "instructional_steps",
        "material_closeup",
        "dimension_fit",
        "comparison",
        "set_contents",
    }
)
REFERENCE_PROMPT_VERSIONS = frozenset(
    {
        PREVIOUS_PROMPT_VERSION,
        REFERENCE_LAYOUT_PROMPT_VERSION,
        INTEGRATED_COPY_PROMPT_VERSION,
        IDENTITY_ANCHOR_PROMPT_VERSION,
    }
)
ADAPTIVE_REFERENCE_PROMPT_VERSIONS = frozenset(
    {
        REFERENCE_LAYOUT_PROMPT_VERSION,
        INTEGRATED_COPY_PROMPT_VERSION,
        IDENTITY_ANCHOR_PROMPT_VERSION,
    }
)
INTEGRATED_COPY_PROMPT_VERSIONS = frozenset(
    {*ADAPTIVE_REFERENCE_PROMPT_VERSIONS, CURRENT_PROMPT_VERSION}
)
CURRENT_VISUAL_PROMPT_VERSIONS = frozenset(
    {
        INTEGRATED_COPY_PROMPT_VERSION,
        IDENTITY_ANCHOR_PROMPT_VERSION,
        CURRENT_PROMPT_VERSION,
    }
)
IDENTITY_ANCHOR_PROMPT_VERSIONS = frozenset(
    {IDENTITY_ANCHOR_PROMPT_VERSION, CURRENT_PROMPT_VERSION}
)
FINISHED_SCENE_PROMPT_VERSIONS = frozenset(
    {*REFERENCE_PROMPT_VERSIONS, CURRENT_PROMPT_VERSION}
)
V5_GUIDANCE_MODES = frozenset(
    {"reference_guided", "ozon_aesthetic_fallback"}
)
PROMPT_ONLY_GUIDANCE_MODE = "fixed_prompt_white_anchor"
PROMPT_ONLY_MAPPING_VERSION = "none"
MINIMUM_OZON_REFERENCE_EDGE = 512
_RUSSIAN_WORD = re.compile(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)?")
_HAN_CHARACTER = re.compile(r"[\u3400-\u9fff]")


def _validate_integrated_copy_payload(
    validation: Mapping[str, Any], errors: list[str]
) -> None:
    headline = str(validation.get("russian_headline") or "").strip()
    subtitle = str(validation.get("russian_subtitle") or "").strip()
    labels = validation.get("russian_functional_labels")

    headline_words = _RUSSIAN_WORD.findall(headline)
    if not 3 <= len(headline_words) <= 7:
        errors.append("russian_headline must contain 3 to 7 Russian words")
    if _HAN_CHARACTER.search(headline):
        errors.append("russian_headline must not contain Chinese text")

    subtitle_words = _RUSSIAN_WORD.findall(subtitle)
    if not subtitle_words or len(subtitle_words) > 14:
        errors.append("russian_subtitle must contain at most 14 Russian words")
    if _HAN_CHARACTER.search(subtitle):
        errors.append("russian_subtitle must not contain Chinese text")

    if (
        not isinstance(labels, (list, tuple))
        or isinstance(labels, (str, bytes, bytearray))
        or not 2 <= len(labels) <= 4
    ):
        errors.append("russian_functional_labels must contain 2 to 4 labels")
        return
    normalized_labels = [str(label or "").strip() for label in labels]
    if any(
        not _RUSSIAN_WORD.search(label) or _HAN_CHARACTER.search(label)
        for label in normalized_labels
    ):
        errors.append("each functional label must contain readable Russian text")
    if len({label.casefold() for label in normalized_labels}) != len(
        normalized_labels
    ):
        errors.append("russian_functional_labels must not repeat")


def _validate_identity_anchor_payload(
    validation: Mapping[str, Any], errors: list[str]
) -> None:
    anchor_path_value = str(validation.get("identity_anchor_path") or "").strip()
    anchor_sha256 = str(validation.get("identity_anchor_sha256") or "").lower()
    if not anchor_path_value:
        errors.append("identity_anchor_path is required")
    else:
        anchor_path = Path(anchor_path_value)
        if not anchor_path.is_file():
            errors.append("identity anchor file does not exist")
        elif file_sha256(anchor_path) != anchor_sha256:
            errors.append("identity anchor SHA-256 does not match")
    if (
        len(anchor_sha256) != 64
        or any(character not in "0123456789abcdef" for character in anchor_sha256)
    ):
        errors.append("identity_anchor_sha256 must be a lowercase SHA-256 digest")
    if validation.get("identity_anchor_reference_index") != 1:
        errors.append("identity_anchor_reference_index must be 1")
    if validation.get("identity_anchor_attached") is not True:
        errors.append("identity_anchor_attached must be true")
    if validation.get("identity_anchor_reused") is not True:
        errors.append("identity_anchor_reused must be true")
    if validation.get("product_identity_source") != "white_anchor_only":
        errors.append("product_identity_source must be white_anchor_only")


def _validate_prompt_only_anchor_payload(
    validation: Mapping[str, Any], errors: list[str]
) -> None:
    if validation.get("reference_mapping_version") != PROMPT_ONLY_MAPPING_VERSION:
        errors.append("reference_mapping_version must be none")
    if validation.get("guidance_mode") != PROMPT_ONLY_GUIDANCE_MODE:
        errors.append(
            "guidance_mode must be fixed_prompt_white_anchor"
        )
    if validation.get("image_reference_count") != 1:
        errors.append("image_reference_count must be 1")
    if validation.get("additional_image_references_attached") is not False:
        errors.append("additional_image_references_attached must be false")
    for key in (
        "primary_ozon_reference_path",
        "primary_ozon_reference_sha256",
        "reference_slot_index",
    ):
        if validation.get(key) not in (None, ""):
            errors.append(f"{key} must be empty for fixed_prompt_white_anchor")
    if validation.get("reference_reused") not in (False, None):
        errors.append(
            "reference_reused must be false for fixed_prompt_white_anchor"
        )
    for key in (
        "reference_composition_followed",
        "reference_layout_followed",
    ):
        if validation.get(key) not in (False, None):
            errors.append(f"{key} must not be asserted without an Ozon reference")
    if validation.get("locked_subject_preserved") is not True:
        errors.append("locked_subject_preserved must be true")
    layout_archetype = (
        str(validation.get("layout_archetype") or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if layout_archetype not in REFERENCE_LAYOUT_ARCHETYPES:
        errors.append("layout_archetype must be a supported layout archetype")


def _validate_current_visual_payload(
    validation: Mapping[str, Any],
    output_sha256: str,
    errors: list[str],
) -> None:
    visual_spec = validation.get("visual_spec")
    facts = visual_spec.get("facts", []) if isinstance(visual_spec, dict) else []
    russian_text = " ".join(
        str(fact.get(key, ""))
        for fact in facts
        if isinstance(fact, dict)
        for key in ("headline", "detail")
    )
    if not re.search(r"[\u0400-\u04ff]", russian_text):
        errors.append("finished slot must contain verified Russian labels")
    output_digest = str(validation.get("visual_output_sha256") or "").lower()
    if output_digest != output_sha256:
        errors.append("visual_output_sha256 must match the accepted output")
    if validation.get("copy_mode") != "imagegen_integrated":
        errors.append("copy_mode must be imagegen_integrated")
    if validation.get("russian_copy_integrated") is not True:
        errors.append("russian_copy_integrated must be true")
    _validate_integrated_copy_payload(validation, errors)


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
            expected_visual_contract = (
                CURRENT_VISUAL_CONTRACT_VERSION
                if self.prompt_version in CURRENT_VISUAL_PROMPT_VERSIONS
                else HISTORICAL_VISUAL_CONTRACT_VERSION
            )
            if self.validation.get("visual_contract_version") != expected_visual_contract:
                errors.append(
                    f"visual_contract_version must be {expected_visual_contract}"
                )
        if (
            isinstance(self.prompt_version, str)
            and self.prompt_version in REFERENCE_PROMPT_VERSIONS
        ):
            if (
                self.validation.get("reference_mapping_version")
                != REFERENCE_MAPPING_VERSION
            ):
                errors.append(
                    f"reference_mapping_version must be {REFERENCE_MAPPING_VERSION}"
                )
            guidance_mode = (
                str(self.validation.get("guidance_mode") or "").strip()
                if self.prompt_version in ADAPTIVE_REFERENCE_PROMPT_VERSIONS
                else "reference_guided"
            )
            if (
                self.prompt_version in ADAPTIVE_REFERENCE_PROMPT_VERSIONS
                and guidance_mode not in V5_GUIDANCE_MODES
            ):
                errors.append(
                    "guidance_mode must be reference_guided or "
                    "ozon_aesthetic_fallback"
                )
            reference_guided = guidance_mode == "reference_guided"
            if reference_guided:
                reference_sha256 = str(
                    self.validation.get("primary_ozon_reference_sha256") or ""
                ).lower()
                if (
                    len(reference_sha256) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in reference_sha256
                    )
                ):
                    errors.append(
                        "primary_ozon_reference_sha256 must be a lowercase "
                        "SHA-256 digest"
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
                            errors.append(
                                "primary Ozon reference SHA-256 does not match"
                            )
                        try:
                            with Image.open(reference_path) as reference_image:
                                if (
                                    min(reference_image.size)
                                    < MINIMUM_OZON_REFERENCE_EDGE
                                ):
                                    errors.append(
                                        "primary Ozon reference edge must be at "
                                        "least 512 pixels"
                                    )
                        except OSError:
                            errors.append(
                                "primary Ozon reference pixels are unavailable"
                            )
                reference_slot_index = self.validation.get(
                    "reference_slot_index"
                )
                if (
                    not isinstance(reference_slot_index, int)
                    or isinstance(reference_slot_index, bool)
                    or reference_slot_index < 1
                ):
                    errors.append(
                        "reference_slot_index must be a positive integer"
                    )
                if not isinstance(self.validation.get("reference_reused"), bool):
                    errors.append("reference_reused must be a boolean")
            elif self.prompt_version in ADAPTIVE_REFERENCE_PROMPT_VERSIONS:
                for key in (
                    "primary_ozon_reference_path",
                    "primary_ozon_reference_sha256",
                ):
                    if self.validation.get(key) not in (None, ""):
                        errors.append(
                            f"{key} must be empty for ozon_aesthetic_fallback"
                        )
                if self.validation.get("reference_reused") not in (False, None):
                    errors.append(
                        "reference_reused must be false for "
                        "ozon_aesthetic_fallback"
                    )
            required_reference_flags = (
                V5_REFERENCE_MAPPING_TRUE_FLAGS
                if (
                    self.prompt_version in ADAPTIVE_REFERENCE_PROMPT_VERSIONS
                    and reference_guided
                )
                else REFERENCE_MAPPING_TRUE_FLAGS
            )
            if (
                self.prompt_version in ADAPTIVE_REFERENCE_PROMPT_VERSIONS
                and not reference_guided
            ):
                required_reference_flags = ("locked_subject_preserved",)
            for key in required_reference_flags:
                if self.validation.get(key) is not True:
                    errors.append(f"{key} must be true")
            if self.prompt_version in ADAPTIVE_REFERENCE_PROMPT_VERSIONS:
                reference_layout_archetype = str(
                    self.validation.get("reference_layout_archetype") or ""
                ).strip().lower().replace("-", "_").replace(" ", "_")
                if reference_layout_archetype not in REFERENCE_LAYOUT_ARCHETYPES:
                    errors.append(
                        "reference_layout_archetype must be a supported "
                        "reference-layout archetype"
                    )
            visual_spec = self.validation.get("visual_spec")
            facts = visual_spec.get("facts", []) if isinstance(visual_spec, dict) else []
            slot_requires_integrated_copy = (
                self.prompt_version in CURRENT_VISUAL_PROMPT_VERSIONS
                or (
                    self.prompt_version == REFERENCE_LAYOUT_PROMPT_VERSION
                    and self.slot_id != "main_01"
                )
            )
            if slot_requires_integrated_copy:
                russian_text = " ".join(
                    str(fact.get(key, ""))
                    for fact in facts
                    if isinstance(fact, dict)
                    for key in ("headline", "detail")
                )
                if not re.search(r"[\u0400-\u04ff]", russian_text):
                    errors.append("finished slot must contain verified Russian labels")
                output_digest = str(
                    self.validation.get("visual_output_sha256") or ""
                ).lower()
                if output_digest != self.output_sha256:
                    errors.append("visual_output_sha256 must match the accepted output")
                if self.prompt_version in INTEGRATED_COPY_PROMPT_VERSIONS:
                    if self.validation.get("copy_mode") != "imagegen_integrated":
                        errors.append("copy_mode must be imagegen_integrated")
                    if self.validation.get("russian_copy_integrated") is not True:
                        errors.append("russian_copy_integrated must be true")
                else:
                    source_digest = str(
                        self.validation.get("visual_source_sha256") or ""
                    ).lower()
                    if len(source_digest) != 64 or source_digest == output_digest:
                        errors.append(
                            "supporting slot must persist a locally rendered "
                            "Russian label layer"
                        )
                if self.prompt_version in CURRENT_VISUAL_PROMPT_VERSIONS:
                    _validate_integrated_copy_payload(self.validation, errors)
            if self.prompt_version in IDENTITY_ANCHOR_PROMPT_VERSIONS:
                _validate_identity_anchor_payload(self.validation, errors)
            if self.prompt_version == CURRENT_PROMPT_VERSION:
                _validate_prompt_only_anchor_payload(self.validation, errors)
        if self.prompt_version == CURRENT_PROMPT_VERSION:
            _validate_current_visual_payload(
                self.validation,
                self.output_sha256,
                errors,
            )
            _validate_identity_anchor_payload(self.validation, errors)
            _validate_prompt_only_anchor_payload(self.validation, errors)
        if (
            isinstance(self.prompt_version, str)
            and self.prompt_version in FINISHED_SCENE_PROMPT_VERSIONS
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


def validate_identity_anchor_consistency(
    receipts: tuple[SlotResultReceipt, ...],
) -> list[str]:
    current_receipts = tuple(
        receipt
        for receipt in receipts
        if receipt.prompt_version in IDENTITY_ANCHOR_PROMPT_VERSIONS
    )
    if not current_receipts:
        return []
    anchor_paths = {
        str(Path(str(receipt.validation.get("identity_anchor_path") or "")).resolve())
        for receipt in current_receipts
    }
    anchor_hashes = {
        str(receipt.validation.get("identity_anchor_sha256") or "").lower()
        for receipt in current_receipts
    }
    errors: list[str] = []
    if len(anchor_paths) != 1:
        errors.append("all accepted slots must reuse the same identity anchor path")
    if len(anchor_hashes) != 1:
        errors.append("all accepted slots must reuse the same identity anchor SHA-256")
    return errors


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


def validate_reference_layout_diversity(
    receipts: tuple[SlotResultReceipt, ...],
    *,
    minimum_unique_archetypes: int = 6,
    maximum_reference_reuse: int = 2,
) -> list[str]:
    """Reject galleries that repeat layouts or over-reuse a historical reference."""
    current = tuple(
        receipt
        for receipt in receipts
        if (
            receipt.prompt_version
            in {*ADAPTIVE_REFERENCE_PROMPT_VERSIONS, CURRENT_PROMPT_VERSION}
            and receipt.accepted
        )
    )
    if not current or len(current) != len(receipts) or len(current) < 8:
        return []
    errors: list[str] = []
    archetypes = [
        str(
            receipt.validation.get(
                "layout_archetype"
                if receipt.prompt_version == CURRENT_PROMPT_VERSION
                else "reference_layout_archetype"
            )
            or ""
        )
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
        for receipt in current
    ]
    if len(set(archetypes)) < minimum_unique_archetypes:
        errors.append(
            "eight-slot set must use at least "
            f"{minimum_unique_archetypes} layout archetypes"
        )
    reference_counts = Counter(
        str(receipt.validation.get("primary_ozon_reference_sha256") or "").lower()
        for receipt in current
        if receipt.validation.get("guidance_mode") == "reference_guided"
        and str(receipt.validation.get("primary_ozon_reference_sha256") or "").strip()
    )
    if any(count > maximum_reference_reuse for count in reference_counts.values()):
        errors.append(
            "one primary Ozon reference may be bound to at most "
            f"{maximum_reference_reuse} finished slots"
        )
    return errors
