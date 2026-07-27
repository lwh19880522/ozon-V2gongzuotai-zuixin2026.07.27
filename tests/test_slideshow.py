from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from ozon_v2.images.slideshow import build_product_slideshow


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is unavailable")
def test_build_product_slideshow_creates_video_and_cover_from_eight_slots(
    tmp_path: Path,
) -> None:
    images: list[Path] = []
    for index in range(8):
        path = tmp_path / f"slot-{index + 1:02d}.png"
        Image.new(
            "RGB",
            (90, 120),
            (20 + index * 20, 60 + index * 10, 130),
        ).save(path)
        images.append(path)

    result = build_product_slideshow(
        images,
        output_dir=tmp_path / "media",
        width=180,
        height=240,
        fps=12,
        hold_seconds=0.25,
        transition_seconds=0.25,
    )

    video_path = Path(result["video_path"])
    cover_path = Path(result["cover_path"])
    assert video_path.is_file() and video_path.stat().st_size > 0
    assert cover_path.is_file()
    with Image.open(cover_path) as cover:
        assert cover.size == (180, 240)
    assert result["aspect_ratio"] == "3:4"
    assert result["source_image_count"] == 8
    assert result["frame_count"] > 0
    assert result["video_sha256"]
    assert result["cover_sha256"]
