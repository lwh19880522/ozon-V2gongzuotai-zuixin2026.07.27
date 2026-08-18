from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import socket
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image

from ozon_v2.images.worker import crop_grid, file_sha256


MINIMUM_REFERENCE_EDGE = 512
DEFAULT_MINIMUM_REFERENCES = 4
MAXIMUM_REFERENCE_BYTES = 20 * 1024 * 1024
MAXIMUM_REFERENCE_PIXELS = 40_000_000
TRUSTED_OZON_IMAGE_HOST_SUFFIXES = (".ozone.ru", ".ozon.ru")


def upgrade_ozon_reference_url(url: str) -> str:
    normalized = str(url or "").strip()
    return normalized.replace("/wc50/", "/wc1000/").replace("/wc100/", "/wc1000/")


def _validate_reference_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    hostname = str(parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme != "https" or not hostname:
        raise ValueError("reference image URL must use HTTPS")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("reference image URL contains unsupported authority data")
    if not any(hostname.endswith(suffix) for suffix in TRUSTED_OZON_IMAGE_HOST_SUFFIXES):
        raise ValueError("reference image host is not an approved Ozon CDN")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError("reference image host could not be resolved") from exc
    if not addresses:
        raise ValueError("reference image host did not resolve to an address")
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address)
        if not address.is_global:
            raise ValueError("reference image host resolves to a non-public address")
    return parsed.geturl()


class _SafeReferenceRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            _validate_reference_url(newurl),
        )


def fetch_reference_bytes(url: str) -> bytes:
    safe_url = _validate_reference_url(url)
    request = urllib.request.Request(
        safe_url,
        headers={"User-Agent": "Mozilla/5.0 OzonV2ReferenceMaterializer/1.0"},
    )
    opener = urllib.request.build_opener(_SafeReferenceRedirectHandler())
    with opener.open(request, timeout=30) as response:
        _validate_reference_url(str(response.geturl() or safe_url))
        content_type = str(response.headers.get("Content-Type") or "").casefold()
        if content_type and not content_type.startswith("image/"):
            raise ValueError("reference response is not an image")
        content_length = str(response.headers.get("Content-Length") or "").strip()
        if content_length and int(content_length) > MAXIMUM_REFERENCE_BYTES:
            raise ValueError("reference image exceeds the download size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, MAXIMUM_REFERENCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAXIMUM_REFERENCE_BYTES:
                raise ValueError("reference image exceeds the download size limit")
        return b"".join(chunks)


def _pixel_digest(image: Image.Image) -> str:
    sample = image.convert("RGB").resize((64, 64))
    pixels = (
        sample.get_flattened_data()
        if hasattr(sample, "get_flattened_data")
        else sample.getdata()
    )
    quantized = bytes(channel // 8 for pixel in pixels for channel in pixel)
    return hashlib.sha256(quantized).hexdigest()


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def materialize_ozon_references(
    urls: Iterable[str],
    output_dir: str | Path,
    *,
    fetch_bytes: Callable[[str], bytes] = fetch_reference_bytes,
    minimum_edge: int = MINIMUM_REFERENCE_EDGE,
    minimum_valid: int = DEFAULT_MINIMUM_REFERENCES,
) -> dict[str, Any]:
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_pixels: set[str] = set()

    for gallery_index, raw_url in enumerate(urls, start=1):
        url = upgrade_ozon_reference_url(raw_url)
        if not url:
            rejected.append({"gallery_index": gallery_index, "reason": "empty_url"})
            continue
        try:
            content = fetch_bytes(url)
            with Image.open(BytesIO(content)) as opened:
                if opened.width * opened.height > MAXIMUM_REFERENCE_PIXELS:
                    raise ValueError("reference image exceeds the pixel limit")
                image = opened.convert("RGB")
                width, height = image.size
                pixel_sha256 = _pixel_digest(image)
                if min(width, height) < minimum_edge:
                    rejected.append(
                        {
                            "gallery_index": gallery_index,
                            "url": url,
                            "reason": "below_minimum_edge",
                            "width": width,
                            "height": height,
                        }
                    )
                    continue
                if pixel_sha256 in seen_pixels:
                    rejected.append(
                        {
                            "gallery_index": gallery_index,
                            "url": url,
                            "reason": "pixel_duplicate",
                        }
                    )
                    continue
                seen_pixels.add(pixel_sha256)
                path = destination / f"ref_{len(accepted) + 1:02d}.png"
                image.save(path, format="PNG")
        except Exception as error:
            rejected.append(
                {
                    "gallery_index": gallery_index,
                    "url": url,
                    "reason": "unreadable",
                    "detail": str(error),
                }
            )
            continue
        accepted.append(
            {
                "gallery_index": gallery_index,
                "url": url,
                "path": str(path),
                "sha256": file_sha256(path),
                "pixel_sha256": pixel_sha256,
                "width": width,
                "height": height,
            }
        )

    reference_guidance_ready = len(accepted) >= minimum_valid
    manifest_path = destination / "reference_manifest.json"
    result = {
        "contract_version": "ozon-reference-materialization-v1",
        "minimum_edge": minimum_edge,
        "minimum_valid": minimum_valid,
        "ready": True,
        "reference_guidance_ready": reference_guidance_ready,
        "generation_mode": (
            "reference_guided"
            if reference_guidance_ready
            else "ozon_aesthetic_fallback"
        ),
        "warning_reason": (
            None
            if reference_guidance_ready
            else "insufficient_distinct_legible_ozon_references"
        ),
        "stop_reason": None,
        "accepted": accepted,
        "rejected": rejected,
        "manifest_path": str(manifest_path),
    }
    _write_json_atomically(manifest_path, result)
    return result


def ozon_gallery_urls_for_product(
    collection_json: str | Path,
    product_id: str,
) -> list[str]:
    payload = json.loads(
        Path(collection_json).read_text(encoding="utf-8"),
        strict=False,
    )
    target = str(product_id)
    matches: list[Mapping[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            identifiers = {
                str(value.get(key))
                for key in (
                    "product_id",
                    "ozon_product_id",
                    "ozon_id",
                    "id",
                    "seed_id",
                )
                if value.get(key) is not None
            }
            if target in identifiers:
                matches.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    if not matches:
        raise ValueError(f"product {target} was not found in the Ozon collection result")

    urls: list[str] = []

    def collect(value: Any, parent_key: str = "") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                collect(child, str(key).lower())
        elif isinstance(value, list):
            for child in value:
                collect(child, parent_key)
        elif isinstance(value, str):
            lowered = value.lower()
            if (
                lowered.startswith(("http://", "https://"))
                and any(token in parent_key for token in ("image", "media", "gallery"))
            ):
                urls.append(value)

    collect(matches[0])
    ordered: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    if not ordered:
        raise ValueError(f"product {target} has no Ozon gallery URLs")
    return ordered


def persist_generation_checkpoint(
    *,
    job_id: str,
    asset_kind: str,
    source_path: str | Path,
    checkpoint_dir: str | Path,
    layout: str | None = None,
    basename: str | None = None,
) -> dict[str, Any]:
    source = Path(source_path).resolve()
    if not source.is_file():
        raise ValueError("generated source file does not exist")
    destination = Path(checkpoint_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    safe_kind = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in asset_kind
    ).strip("_")
    if not safe_kind:
        raise ValueError("asset_kind is required")
    checkpoint_source = destination / f"{safe_kind}{source.suffix.lower() or '.png'}"
    if source != checkpoint_source:
        temporary = checkpoint_source.with_name(
            f".{checkpoint_source.name}.{uuid.uuid4().hex}.tmp"
        )
        shutil.copy2(source, temporary)
        os.replace(temporary, checkpoint_source)

    outputs: list[dict[str, Any]] = []
    if layout:
        for path in crop_grid(
            checkpoint_source,
            destination / f"{safe_kind}_slots",
            layout=layout,
            basename=basename or safe_kind,
        ):
            with Image.open(path) as image:
                outputs.append(
                    {
                        "path": str(path.resolve()),
                        "sha256": file_sha256(path),
                        "width": image.width,
                        "height": image.height,
                    }
                )

    manifest_path = destination / "checkpoint_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("job_id") != job_id:
            raise ValueError("checkpoint directory belongs to another image job")
    else:
        manifest = {
            "contract_version": "ozon-generation-checkpoint-v1",
            "job_id": job_id,
            "assets": [],
        }
    entry = {
        "asset_kind": safe_kind,
        "source_path": str(checkpoint_source),
        "source_sha256": file_sha256(checkpoint_source),
        "layout": layout,
        "outputs": outputs,
    }
    manifest["assets"] = [
        item for item in manifest["assets"] if item.get("asset_kind") != safe_kind
    ] + [entry]
    _write_json_atomically(manifest_path, manifest)
    return entry


def load_generation_checkpoints(
    job_id: str,
    checkpoint_dir: str | Path,
) -> dict[str, Any]:
    manifest_path = Path(checkpoint_dir).resolve() / "checkpoint_manifest.json"
    if not manifest_path.is_file():
        return {
            "contract_version": "ozon-generation-checkpoint-v1",
            "job_id": job_id,
            "assets": [],
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("job_id") != job_id:
        raise ValueError("checkpoint manifest belongs to another image job")
    for asset in manifest.get("assets", []):
        source = Path(asset["source_path"])
        if not source.is_file() or file_sha256(source) != asset["source_sha256"]:
            raise ValueError(f"checkpoint hash mismatch: {source}")
        for output in asset.get("outputs", []):
            path = Path(output["path"])
            if not path.is_file() or file_sha256(path) != output["sha256"]:
                raise ValueError(f"checkpoint hash mismatch: {path}")
    return manifest
