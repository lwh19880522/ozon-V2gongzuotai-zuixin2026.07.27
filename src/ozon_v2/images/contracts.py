from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ozon_v2.domain.supplier_sku import SupplierSkuSelectionReceipt, stable_sha256


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SubjectMasterSelection:
    run_id: str
    product_id: str
    supplier_sku_id: str
    selection_sha256: str
    source_path: str
    source_image_url: str
    source_sha256: str
    source_paths: tuple[str, ...]
    source_image_urls: tuple[str, ...]
    source_sha256s: tuple[str, ...]
    set_quantity: int
    set_composition: tuple[str, ...]
    visible_subject_quantity: int
    white_background_confirmed: bool
    white_background_generation_required: bool
    confirmed_by: str
    confirmed_at: str
    subject_master_sha256: str

    @classmethod
    def create(
        cls,
        *,
        receipt: SupplierSkuSelectionReceipt,
        visible_subject_quantity: int,
        confirmed_at: str,
        source_path: str | Path | None = None,
        source_image_url: str | None = None,
        source_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
        source_image_urls: list[str] | tuple[str, ...] | None = None,
        white_background_confirmed: bool = False,
    ) -> "SubjectMasterSelection":
        if not receipt.verify_hash():
            raise ValueError("active supplier SKU selection receipt is invalid")
        if visible_subject_quantity != receipt.supplier_sku.set_quantity:
            raise ValueError("visible subject quantity must match the selected supplier SKU")

        raw_paths = list(source_paths or ([] if source_path is None else [source_path]))
        raw_urls = list(source_image_urls or ([] if not source_image_url else [source_image_url]))
        if not raw_paths or not raw_urls:
            raise ValueError("at least one supplier subject evidence image is required")
        if len(raw_paths) != len(raw_urls):
            raise ValueError("subject evidence paths and URLs must have the same length")

        resolved_paths: list[str] = []
        source_hashes: list[str] = []
        normalized_urls: list[str] = []
        for raw_path, raw_url in zip(raw_paths, raw_urls, strict=True):
            path = Path(raw_path).resolve()
            if not path.is_file():
                raise ValueError("subject evidence file does not exist")
            url = str(raw_url or "").strip()
            if not url:
                raise ValueError("subject evidence URL is required")
            resolved_paths.append(str(path))
            source_hashes.append(_file_sha256(path))
            normalized_urls.append(url)

        core = {
            "run_id": receipt.run_id,
            "product_id": receipt.product_id,
            "supplier_sku_id": receipt.supplier_sku_id,
            "selection_sha256": receipt.selection_sha256,
            "source_path": resolved_paths[0],
            "source_image_url": normalized_urls[0],
            "source_sha256": source_hashes[0],
            "source_paths": tuple(resolved_paths),
            "source_image_urls": tuple(normalized_urls),
            "source_sha256s": tuple(source_hashes),
            "set_quantity": receipt.supplier_sku.set_quantity,
            "set_composition": tuple(receipt.supplier_sku.set_composition),
            "visible_subject_quantity": visible_subject_quantity,
            "white_background_confirmed": bool(white_background_confirmed),
            "white_background_generation_required": not bool(white_background_confirmed),
            "confirmed_by": "user",
            "confirmed_at": confirmed_at,
        }
        return cls(**core, subject_master_sha256=stable_sha256(core))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SubjectMasterSelection":
        normalized = dict(payload)
        source_paths = normalized.get("source_paths") or [normalized.get("source_path")]
        source_urls = normalized.get("source_image_urls") or [normalized.get("source_image_url")]
        source_hashes = normalized.get("source_sha256s") or [normalized.get("source_sha256")]
        normalized["source_paths"] = tuple(str(value) for value in source_paths if value)
        normalized["source_image_urls"] = tuple(str(value) for value in source_urls if value)
        normalized["source_sha256s"] = tuple(str(value) for value in source_hashes if value)
        if not normalized["source_paths"] or not normalized["source_image_urls"]:
            raise ValueError("subject evidence is missing")
        if len(normalized["source_paths"]) != len(normalized["source_image_urls"]):
            raise ValueError("subject evidence paths and URLs must have the same length")
        if len(normalized["source_paths"]) != len(normalized["source_sha256s"]):
            raise ValueError("subject evidence paths and hashes must have the same length")
        normalized["source_path"] = str(normalized.get("source_path") or normalized["source_paths"][0])
        normalized["source_image_url"] = str(
            normalized.get("source_image_url") or normalized["source_image_urls"][0]
        )
        normalized["source_sha256"] = str(
            normalized.get("source_sha256") or normalized["source_sha256s"][0]
        )
        normalized["set_composition"] = tuple(
            str(value) for value in normalized.get("set_composition") or []
        )
        normalized.setdefault(
            "white_background_generation_required",
            not bool(normalized.get("white_background_confirmed")),
        )
        return cls(**normalized)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_paths"] = list(self.source_paths)
        payload["source_image_urls"] = list(self.source_image_urls)
        payload["source_sha256s"] = list(self.source_sha256s)
        payload["set_composition"] = list(self.set_composition)
        return payload

    def verify_file(self) -> bool:
        if not self.source_paths or len(self.source_paths) != len(self.source_sha256s):
            return False
        return all(
            Path(path).is_file() and _file_sha256(Path(path)) == expected_sha256
            for path, expected_sha256 in zip(self.source_paths, self.source_sha256s, strict=True)
        )

    def verify_selection(self, receipt: SupplierSkuSelectionReceipt) -> bool:
        return all(
            (
                receipt.verify_hash(),
                self.run_id == receipt.run_id,
                self.product_id == receipt.product_id,
                self.supplier_sku_id == receipt.supplier_sku_id,
                self.selection_sha256 == receipt.selection_sha256,
                self.set_quantity == receipt.supplier_sku.set_quantity,
                self.visible_subject_quantity == receipt.supplier_sku.set_quantity,
                self.set_composition == tuple(receipt.supplier_sku.set_composition),
                bool(self.source_paths),
                len(self.source_paths) == len(self.source_image_urls),
                len(self.source_paths) == len(self.source_sha256s),
            )
        )

