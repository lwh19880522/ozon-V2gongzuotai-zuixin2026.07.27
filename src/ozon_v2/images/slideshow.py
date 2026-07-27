from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


class SlideshowError(RuntimeError):
    pass


def build_product_slideshow(
    image_paths: list[str | Path],
    *,
    output_dir: str | Path,
    width: int = 720,
    height: int = 960,
    fps: int = 24,
    hold_seconds: float = 1.25,
    transition_seconds: float = 0.45,
    ffmpeg_executable: str | None = None,
) -> dict[str, Any]:
    sources = [Path(value).resolve() for value in image_paths]
    if len(sources) != 8:
        raise SlideshowError("Exactly eight accepted images are required for video.")
    if any(not path.is_file() for path in sources):
        raise SlideshowError("One or more accepted slideshow images are missing.")
    if width <= 0 or height <= 0 or width * 4 != height * 3:
        raise SlideshowError("Slideshow output must use an exact 3:4 aspect ratio.")
    if fps <= 0 or hold_seconds <= 0 or transition_seconds <= 0:
        raise SlideshowError("Slideshow timing values must be positive.")

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    video_path = output / "slideshow.mp4"
    video_temp = output / "slideshow.mp4.tmp"
    cover_path = output / "video_cover.jpg"
    frames = [_fit_rgb(path, (width, height)) for path in sources]
    frames[0].save(
        cover_path,
        format="JPEG",
        quality=92,
        optimize=True,
        progressive=True,
    )

    ffmpeg = ffmpeg_executable or _ffmpeg_executable()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(video_temp),
    ]
    process = _start_encoder(command)
    hold_frames = max(1, round(hold_seconds * fps))
    transition_frames = max(1, round(transition_seconds * fps))
    frame_count = 0
    try:
        assert process.stdin is not None
        for index, current in enumerate(frames):
            for _ in range(hold_frames):
                process.stdin.write(current.tobytes())
                frame_count += 1
            if index == len(frames) - 1:
                continue
            following = frames[index + 1]
            for step in range(1, transition_frames + 1):
                offset = round(width * step / transition_frames)
                transition = Image.new("RGB", (width, height))
                transition.paste(current, (-offset, 0))
                transition.paste(following, (width - offset, 0))
                process.stdin.write(transition.tobytes())
                frame_count += 1
        process.stdin.close()
        stderr = (process.stderr.read() if process.stderr is not None else b"")
        return_code = process.wait()
    except (BrokenPipeError, OSError) as exc:
        process.kill()
        stderr = (process.stderr.read() if process.stderr is not None else b"")
        raise SlideshowError(
            f"FFmpeg slideshow encoding failed: {_decode(stderr) or exc}"
        ) from exc
    finally:
        for frame in frames:
            frame.close()

    if return_code != 0 or not video_temp.is_file() or video_temp.stat().st_size <= 0:
        raise SlideshowError(
            f"FFmpeg slideshow encoding failed: {_decode(stderr) or return_code}"
        )
    video_temp.replace(video_path)
    return {
        "video_path": str(video_path),
        "cover_path": str(cover_path),
        "source_image_count": len(sources),
        "aspect_ratio": "3:4",
        "width": width,
        "height": height,
        "fps": fps,
        "frame_count": frame_count,
        "duration_seconds": round(frame_count / fps, 3),
        "video_sha256": _sha256(video_path),
        "cover_sha256": _sha256(cover_path),
    }


def _fit_rgb(path: Path, size: tuple[int, int]) -> Image.Image:
    try:
        with Image.open(path) as source:
            return ImageOps.fit(
                source.convert("RGB"),
                size,
                method=Image.Resampling.LANCZOS,
            )
    except (OSError, ValueError) as exc:
        raise SlideshowError(f"Invalid accepted slideshow image: {path}") from exc


def _ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise SlideshowError(
            "FFmpeg is unavailable. Install ffmpeg or the imageio-ffmpeg package."
        ) from exc
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def _start_encoder(command: list[str]) -> subprocess.Popen[bytes]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs = {
            "startupinfo": startupinfo,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **kwargs,
        )
    except OSError as exc:
        raise SlideshowError(f"Could not start FFmpeg: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()[-2000:]
