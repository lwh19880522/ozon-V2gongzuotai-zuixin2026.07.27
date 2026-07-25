from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


APPROVED_IMAGE_HOSTS = ("1688.com", "alicdn.com", "tbcdn.cn", "alibaba.com")
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _approved_image_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in APPROVED_IMAGE_HOSTS
    ):
        raise ValueError(f"Unapproved locked supplier image URL: {url}")
    return url


def fetch_locked_image(source_url: str, target: Path) -> Path:
    request = Request(
        _approved_image_url(source_url),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
            ),
            "Referer": "https://detail.1688.com/",
        },
    )
    opener = build_opener(ProxyHandler({}))
    target.parent.mkdir(parents=True, exist_ok=True)
    byte_count = 0
    try:
        with opener.open(request, timeout=30) as response, target.open("wb") as output:
            content_type = str(response.headers.get("Content-Type") or "").casefold()
            if content_type and not content_type.startswith("image/"):
                raise ValueError("Locked supplier URL did not return an image.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > MAX_IMAGE_BYTES:
                    raise ValueError("Locked supplier image exceeds 20 MB.")
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if byte_count == 0:
        target.unlink(missing_ok=True)
        raise ValueError("Locked supplier image is empty.")
    return target


def materialize_visual_evidence(
    tasks: dict[str, Any],
    output_dir: Path,
    *,
    fetch_image: Callable[[str, Path], Path] = fetch_locked_image,
) -> dict[str, Any]:
    run_id = str(tasks.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("content-tasks payload is missing run_id")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_items: list[dict[str, Any]] = []
    downloaded_by_url: dict[str, tuple[Path, str]] = {}

    for task in tasks.get("items") or []:
        if not isinstance(task, dict):
            continue
        seed_id = str(task.get("seed_id") or "").strip()
        evidence_index = task.get("evidence_index") or {}
        refs_to_fields: dict[str, list[str]] = {}
        for field in task.get("field_tasks") or []:
            if not isinstance(field, dict) or field.get("status") != "pending":
                continue
            field_key = str(field.get("field_key") or "").strip()
            for reference in field.get("visual_evidence_refs") or []:
                reference = str(reference or "").strip()
                if reference and field_key:
                    refs_to_fields.setdefault(reference, []).append(field_key)
        if not refs_to_fields:
            continue

        images: list[dict[str, Any]] = []
        for reference, field_keys in refs_to_fields.items():
            source_url = _approved_image_url(evidence_index.get(reference))
            if source_url in downloaded_by_url:
                local_path, digest = downloaded_by_url[source_url]
            else:
                suffix = Path(urlparse(source_url).path).suffix.casefold()
                if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                    suffix = ".jpg"
                filename = f"{hashlib.sha256(source_url.encode()).hexdigest()[:24]}{suffix}"
                (output_dir / seed_id).mkdir(parents=True, exist_ok=True)
                local_path = fetch_image(
                    source_url,
                    output_dir / seed_id / filename,
                ).resolve()
                digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
                downloaded_by_url[source_url] = (local_path, digest)
            images.append(
                {
                    "evidence_ref": reference,
                    "source_url": source_url,
                    "local_path": str(local_path),
                    "sha256": digest,
                    "field_keys": sorted(set(field_keys)),
                }
            )
        manifest_items.append(
            {
                "seed_id": seed_id,
                "images": images,
            }
        )

    return {
        "schema_version": 1,
        "run_id": run_id,
        "items": manifest_items,
    }


def _load_content_tasks(base_url: str, run_id: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/batches/{run_id}/content-tasks"
    request = Request(url, headers={"Accept": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok") or not isinstance(payload.get("data"), dict):
        raise RuntimeError(payload.get("message") or "content-tasks request failed")
    return payload["data"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize frozen 1688 image evidence for the Ozon field Skill."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Defaults to runtime/workbench/visual_evidence/<run_id>.",
    )
    args = parser.parse_args()
    output_dir = args.output_dir or (
        Path("runtime") / "workbench" / "visual_evidence" / args.run_id
    )
    manifest = materialize_visual_evidence(
        _load_content_tasks(args.base_url, args.run_id),
        output_dir,
    )
    manifest_path = output_dir.resolve() / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"manifest_path": str(manifest_path), **manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
