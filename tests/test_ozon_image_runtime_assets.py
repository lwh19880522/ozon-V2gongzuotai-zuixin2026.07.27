from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from ozon_v2.images.runtime_assets import (
    _validate_reference_url,
    load_generation_checkpoints,
    materialize_ozon_references,
    persist_generation_checkpoint,
)


def test_reference_url_validation_rejects_non_https_and_untrusted_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ozon_v2.images.runtime_assets.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    with pytest.raises(ValueError, match="HTTPS"):
        _validate_reference_url("http://ir.ozone.ru/image.png")
    with pytest.raises(ValueError, match="approved Ozon CDN"):
        _validate_reference_url("https://example.com/image.png")


def test_reference_url_validation_rejects_private_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ozon_v2.images.runtime_assets.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )

    with pytest.raises(ValueError, match="non-public"):
        _validate_reference_url("https://ir.ozone.ru/image.png")


def test_materializer_rejects_decompression_bomb_sized_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ozon_v2.images.runtime_assets.MAXIMUM_REFERENCE_PIXELS", 100)
    oversized = _image_bytes((30, 60, 90), size=(11, 10))

    result = materialize_ozon_references(
        ["https://ir.ozone.ru/s3/wc1000/oversized.png"],
        tmp_path / "references",
        fetch_bytes=lambda _: oversized,
    )

    assert result["accepted"] == []
    assert "pixel limit" in result["rejected"][0]["detail"]


def _image_bytes(
    color: tuple[int, int, int],
    *,
    size: tuple[int, int] = (800, 800),
    image_format: str = "PNG",
) -> bytes:
    encoded = BytesIO()
    Image.new("RGB", size, color).save(encoded, format=image_format)
    return encoded.getvalue()


def test_materializer_upgrades_filters_and_pixel_deduplicates_references(
    tmp_path: Path,
) -> None:
    red_png = _image_bytes((180, 20, 20))
    red_jpeg = _image_bytes((180, 20, 20), image_format="JPEG")
    blue = _image_bytes((20, 20, 180))
    tiny = _image_bytes((20, 180, 20), size=(50, 50))
    requested: list[str] = []

    payloads = {
        "https://ir.ozone.ru/s3/wc1000/red.png": red_png,
        "https://ir.ozone.ru/s3/wc1000/red-copy.jpg": red_jpeg,
        "https://ir.ozone.ru/s3/wc1000/blue.png": blue,
        "https://ir.ozone.ru/s3/wc1000/tiny.png": tiny,
    }

    def fetch(url: str) -> bytes:
        requested.append(url)
        return payloads[url]

    result = materialize_ozon_references(
        [
            "https://ir.ozone.ru/s3/wc50/red.png",
            "https://ir.ozone.ru/s3/wc50/red-copy.jpg",
            "https://ir.ozone.ru/s3/wc50/blue.png",
            "https://ir.ozone.ru/s3/wc50/tiny.png",
        ],
        tmp_path / "references",
        fetch_bytes=fetch,
        minimum_valid=2,
    )

    assert all("/wc1000/" in url for url in requested)
    assert result["ready"] is True
    assert len(result["accepted"]) == 2
    assert {entry["width"] for entry in result["accepted"]} == {800}
    assert {entry["height"] for entry in result["accepted"]} == {800}
    assert any(item["reason"] == "pixel_duplicate" for item in result["rejected"])
    assert any(item["reason"] == "below_minimum_edge" for item in result["rejected"])
    assert Path(result["manifest_path"]).is_file()


def test_materializer_falls_back_without_stopping_when_references_are_insufficient(
    tmp_path: Path,
) -> None:
    result = materialize_ozon_references(
        ["https://ir.ozone.ru/s3/wc50/only.png"],
        tmp_path / "references",
        fetch_bytes=lambda _: _image_bytes((40, 80, 120)),
        minimum_valid=4,
    )

    assert result["ready"] is True
    assert result["reference_guidance_ready"] is False
    assert result["generation_mode"] == "ozon_aesthetic_fallback"
    assert result["stop_reason"] is None


def test_generation_checkpoint_is_cropped_immediately_and_hash_verified(
    tmp_path: Path,
) -> None:
    grid = tmp_path / "generated-main-grid.png"
    canvas = Image.new("RGB", (600, 400))
    canvas.paste(Image.new("RGB", (300, 400), "red"), (0, 0))
    canvas.paste(Image.new("RGB", (300, 400), "blue"), (300, 0))
    canvas.save(grid)
    checkpoint_dir = tmp_path / "checkpoints"

    result = persist_generation_checkpoint(
        job_id="img-job",
        asset_kind="main_grid",
        source_path=grid,
        checkpoint_dir=checkpoint_dir,
        layout="1x2",
        basename="main",
    )
    restored = load_generation_checkpoints("img-job", checkpoint_dir)

    assert result["source_sha256"] == restored["assets"][0]["source_sha256"]
    assert len(restored["assets"][0]["outputs"]) == 2
    assert all(Path(item["path"]).is_file() for item in restored["assets"][0]["outputs"])
    assert all(
        Image.open(item["path"]).width * 4 == Image.open(item["path"]).height * 3
        for item in restored["assets"][0]["outputs"]
    )

    Path(restored["assets"][0]["source_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checkpoint hash mismatch"):
        load_generation_checkpoints("img-job", checkpoint_dir)
