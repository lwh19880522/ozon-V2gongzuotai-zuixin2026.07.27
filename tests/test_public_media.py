from __future__ import annotations

from pathlib import Path

import pytest

from ozon_v2.adapters.public_media import (
    CloudflareR2MediaPublisher,
    PublicMediaError,
)


def test_cloudflare_publisher_preflight_requires_public_r2_channel() -> None:
    commands: list[list[str]] = []
    publisher = CloudflareR2MediaPublisher(
        command_runner=lambda command, timeout_seconds: (
            commands.append(command)
            or {
                "returncode": 0,
                "stdout": '[{"name":"ozon-media"}]',
                "stderr": "",
            }
        ),
        public_probe=lambda url, timeout_seconds: None,
        wrangler_command=["wrangler"],
    )

    result = publisher.preflight(
        {
            "base_url": "https://media.example.test",
            "r2_bucket": "ozon-media",
            "object_prefix": "ozon-v2",
        }
    )

    assert result["ready"] is True
    assert result["base_url"] == "https://media.example.test"
    assert result["bucket"] == "ozon-media"
    assert commands == [["wrangler", "r2", "bucket", "list", "--json"]]


def test_cloudflare_publisher_preflight_rejects_missing_public_url() -> None:
    publisher = CloudflareR2MediaPublisher(
        command_runner=lambda command, timeout_seconds: {
            "returncode": 0,
            "stdout": "[]",
            "stderr": "",
        },
        wrangler_command=["wrangler"],
    )

    with pytest.raises(PublicMediaError, match="public HTTPS"):
        publisher.preflight({"r2_bucket": "ozon-media"})


def test_cloudflare_publisher_uploads_every_reviewed_image_and_probes_public_urls(
    tmp_path: Path,
) -> None:
    first = tmp_path / "main 01.png"
    second = tmp_path / "detail_01.jpg"
    first.write_bytes(b"first-reviewed-image")
    second.write_bytes(b"second-reviewed-image")
    commands: list[list[str]] = []
    probed: list[str] = []

    publisher = CloudflareR2MediaPublisher(
        command_runner=lambda command, timeout_seconds: (
            commands.append(command)
            or {"returncode": 0, "stdout": "uploaded", "stderr": ""}
        ),
        public_probe=lambda url, timeout_seconds: probed.append(url) or None,
        wrangler_command=["npx.cmd", "--yes", "wrangler"],
    )

    result = publisher.publish_product(
        run_id="wb-one",
        seed_id="seed-one",
        source_files=[
            {"slot_id": "main_01", "path": str(first)},
            {"slot_id": "detail_01", "path": str(second)},
        ],
        settings={
            "base_url": "https://media.example.test",
            "r2_bucket": "yandex-media",
            "object_prefix": "ozon-v2",
        },
    )

    assert len(result["urls"]) == 2
    assert probed == result["urls"]
    assert all(url.startswith("https://media.example.test/ozon-v2/wb-one/seed-one/") for url in result["urls"])
    assert commands[0] == [
        "npx.cmd",
        "--yes",
        "wrangler",
        "r2",
        "bucket",
        "list",
        "--json",
    ]
    assert commands[1][:5] == [
        "npx.cmd",
        "--yes",
        "wrangler",
        "r2",
        "object",
    ]
    assert commands[1][5] == "put"
    assert commands[1][6].startswith("yandex-media/ozon-v2/wb-one/seed-one/main_01-")
    assert "--remote" in commands[1]
    assert "--content-type" in commands[1]
    assert result["items"][0]["content_type"] == "image/png"


def test_cloudflare_publisher_stops_before_ozon_when_public_probe_fails(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.png"
    source.write_bytes(b"reviewed-image")
    publisher = CloudflareR2MediaPublisher(
        command_runner=lambda command, timeout_seconds: {
            "returncode": 0,
            "stdout": "uploaded",
            "stderr": "",
        },
        public_probe=lambda url, timeout_seconds: (_ for _ in ()).throw(
            PublicMediaError("public image returned HTTP 404")
        ),
        wrangler_command=["wrangler"],
    )

    with pytest.raises(PublicMediaError, match="HTTP 404"):
        publisher.publish_product(
            run_id="wb-one",
            seed_id="seed-one",
            source_files=[{"slot_id": "main_01", "path": str(source)}],
            settings={
                "base_url": "https://media.example.test",
                "r2_bucket": "yandex-media",
                "object_prefix": "ozon-v2",
            },
        )
