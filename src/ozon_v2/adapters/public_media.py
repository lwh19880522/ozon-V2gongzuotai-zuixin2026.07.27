from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class PublicMediaError(RuntimeError):
    pass


CommandRunner = Callable[[list[str], int], dict[str, Any]]
PublicProbe = Callable[[str, int], None]


class CloudflareR2MediaPublisher:
    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        public_probe: PublicProbe | None = None,
        wrangler_command: list[str] | None = None,
    ) -> None:
        self.command_runner = command_runner or _run_command
        self.public_probe = public_probe or _probe_public_media
        self.wrangler_command = wrangler_command or _default_wrangler_command()

    def preflight(self, settings: dict[str, Any]) -> dict[str, Any]:
        base_url = _https_base_url(settings.get("base_url"))
        bucket = str(settings.get("r2_bucket") or "").strip()
        prefix = _safe_segment(settings.get("object_prefix") or "ozon-v2")
        if not bucket:
            raise PublicMediaError("Cloudflare R2 bucket is not configured.")
        timeout_seconds = int(settings.get("preflight_timeout_seconds") or 120)
        result = self.command_runner(
            [*self.wrangler_command, "r2", "bucket", "list", "--json"],
            timeout_seconds,
        )
        used_text_fallback = _is_unknown_json_argument(result)
        if used_text_fallback:
            result = self.command_runner(
                [*self.wrangler_command, "r2", "bucket", "list"],
                timeout_seconds,
            )
        if int(result.get("returncode", 1)) != 0:
            detail = str(result.get("stderr") or result.get("stdout") or "").strip()
            raise PublicMediaError(
                "Cloudflare R2 channel preflight failed: "
                + (detail or "Wrangler could not list accessible buckets.")
            )
        output = str(result.get("stdout") or "")
        if used_text_fallback and bucket not in output:
            raise PublicMediaError(
                f"Cloudflare R2 bucket is not accessible: {bucket}"
            )
        return {
            "ready": True,
            "provider": "cloudflare_r2_direct",
            "base_url": base_url,
            "bucket": bucket,
            "object_prefix": prefix,
        }

    def publish_product(
        self,
        *,
        run_id: str,
        seed_id: str,
        source_files: list[dict[str, Any]],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        preflight = self.preflight(settings)
        base_url = str(preflight["base_url"])
        bucket = str(preflight["bucket"])
        prefix = str(preflight["object_prefix"])
        if not source_files:
            raise PublicMediaError("No reviewed local media files are available to publish.")

        uploads: list[dict[str, Any]] = []
        for index, item in enumerate(source_files, start=1):
            source = Path(str(item.get("path") or "")).expanduser()
            if not source.is_file():
                raise PublicMediaError(f"Reviewed media file is missing: {source}")
            slot_id = _safe_segment(item.get("slot_id") or f"image_{index:02d}")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
            suffix = source.suffix.lower() or ".jpg"
            key = "/".join(
                (
                    prefix,
                    _safe_segment(run_id),
                    _safe_segment(seed_id),
                    f"{slot_id}-{digest}{suffix}",
                )
            )
            content_type = mimetypes.guess_type(source.name)[0] or "image/jpeg"
            command = [
                *self.wrangler_command,
                "r2",
                "object",
                "put",
                f"{bucket}/{key}",
                "--file",
                str(source),
                "--content-type",
                content_type,
                "--cache-control",
                "public, max-age=31536000, immutable",
                "--remote",
            ]
            result = self.command_runner(
                command,
                int(settings.get("upload_timeout_seconds") or 1200),
            )
            if int(result.get("returncode", 1)) != 0:
                detail = str(result.get("stderr") or result.get("stdout") or "").strip()
                raise PublicMediaError(
                    f"Cloudflare R2 upload failed for {slot_id}: {detail or 'unknown error'}"
                )
            public_url = f"{base_url}/{'/'.join(quote(part, safe='') for part in key.split('/'))}"
            self.public_probe(
                public_url,
                int(settings.get("public_probe_timeout_seconds") or 30),
            )
            uploads.append(
                {
                    "slot_id": slot_id,
                    "source_path": str(source.resolve()),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "bucket": bucket,
                    "object_key": key,
                    "public_url": public_url,
                    "content_type": content_type,
                }
            )
        return {
            "provider": "cloudflare_r2_direct",
            "bucket": bucket,
            "base_url": base_url,
            "urls": [item["public_url"] for item in uploads],
            "items": uploads,
        }


def _is_unknown_json_argument(result: dict[str, Any]) -> bool:
    if int(result.get("returncode", 1)) == 0:
        return False
    detail = " ".join(
        str(result.get(key) or "") for key in ("stderr", "stdout")
    ).casefold()
    return "unknown argument" in detail and "json" in detail


def _default_wrangler_command() -> list[str]:
    wrangler = shutil.which("wrangler")
    if wrangler:
        return [wrangler]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "wrangler"]
    npx_cmd = shutil.which("npx.cmd")
    if npx_cmd:
        return [npx_cmd, "--yes", "wrangler"]
    return ["wrangler"]


def _run_command(command: list[str], timeout_seconds: int) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs = {
            "startupinfo": startupinfo,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, timeout_seconds),
            check=False,
            **kwargs,
        )
    except FileNotFoundError as exc:
        raise PublicMediaError(
            "Cloudflare Wrangler is unavailable. Install Wrangler or make npx available."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PublicMediaError("Cloudflare R2 upload timed out.") from exc
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
    }


def _probe_public_media(url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = Request(url, method="GET", headers={"User-Agent": "OzonV2/1.0"})
            with urlopen(request, timeout=min(10, max(1, timeout_seconds))) as response:
                content_type = str(response.headers.get("Content-Type") or "").casefold()
                if 200 <= int(response.status) < 300 and content_type.startswith(
                    ("image/", "video/")
                ):
                    response.read(1)
                    return
                last_error = f"HTTP {response.status}, content-type={content_type or 'missing'}"
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise PublicMediaError(f"Public media verification failed: {last_error}")


def _https_base_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if text.startswith("http://"):
        text = "https://" + text.removeprefix("http://")
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PublicMediaError("A public HTTPS media base URL is required.")
    return text


def _safe_segment(value: Any) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in str(value or "").strip()
    ).strip("-")
    return cleaned or "item"
