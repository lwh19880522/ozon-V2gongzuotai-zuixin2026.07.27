from __future__ import annotations

import os
import hashlib
from io import BytesIO
from pathlib import Path

import pytest

from ozon_v2.adapters.public_media import (
    CloudflareQuickTunnelMediaPublisher,
    PublicMediaError,
    _download_cloudflared,
    _pid_is_alive,
    _tunnel_command,
)


def _gateway(tmp_path: Path) -> dict[str, object]:
    media_root = tmp_path / "public-media"
    media_root.mkdir()
    return {
        "ready": True,
        "provider": "cloudflare_quick_tunnel",
        "base_url": "https://quiet-field.trycloudflare.com",
        "route_token": "private-route",
        "media_root": str(media_root),
        "manager_pid": 1234,
    }


def test_quick_tunnel_preflight_starts_automatically_without_user_settings(
    tmp_path: Path,
) -> None:
    starts: list[Path] = []
    publisher = CloudflareQuickTunnelMediaPublisher(
        tmp_path,
        gateway_starter=lambda runtime_root: starts.append(runtime_root)
        or _gateway(tmp_path),
        public_probe=lambda url, timeout_seconds: None,
    )

    result = publisher.preflight()

    assert result["ready"] is True
    assert result["provider"] == "cloudflare_quick_tunnel"
    assert result["base_url"].endswith(".trycloudflare.com")
    assert starts == [tmp_path.resolve()]


def test_quick_tunnel_publisher_copies_media_and_probes_public_urls(
    tmp_path: Path,
) -> None:
    first = tmp_path / "main 01.png"
    second = tmp_path / "detail_01.jpg"
    first.write_bytes(b"first-reviewed-image")
    second.write_bytes(b"second-reviewed-image")
    probed: list[str] = []
    publisher = CloudflareQuickTunnelMediaPublisher(
        tmp_path,
        gateway_starter=lambda runtime_root: _gateway(tmp_path),
        public_probe=lambda url, timeout_seconds: probed.append(url),
    )

    result = publisher.publish_product(
        run_id="wb-one",
        seed_id="seed-one",
        source_files=[
            {"slot_id": "main_01", "path": str(first)},
            {"slot_id": "detail_01", "path": str(second)},
        ],
    )

    assert result["provider"] == "cloudflare_quick_tunnel"
    assert result["base_url"] == "https://quiet-field.trycloudflare.com"
    assert probed == result["urls"]
    assert all(
        url.startswith(
            "https://quiet-field.trycloudflare.com/private-route/ozon-v2/"
            "wb-one/seed-one/"
        )
        for url in result["urls"]
    )
    copied = [Path(item["published_path"]) for item in result["items"]]
    assert [path.read_bytes() for path in copied] == [
        b"first-reviewed-image",
        b"second-reviewed-image",
    ]
    assert result["items"][0]["content_type"] == "image/png"


def test_quick_tunnel_publisher_removes_stale_files_for_same_product(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path)
    stale = Path(str(gateway["media_root"])) / "ozon-v2" / "wb-one" / "seed-one" / "stale.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")
    source = tmp_path / "main.png"
    source.write_bytes(b"reviewed-image")
    publisher = CloudflareQuickTunnelMediaPublisher(
        tmp_path,
        gateway_starter=lambda runtime_root: gateway,
        public_probe=lambda url, timeout_seconds: None,
    )

    publisher.publish_product(
        run_id="wb-one",
        seed_id="seed-one",
        source_files=[{"slot_id": "main_01", "path": str(source)}],
    )

    assert not stale.exists()


def test_quick_tunnel_publisher_stops_before_ozon_when_public_probe_fails(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.png"
    source.write_bytes(b"reviewed-image")
    publisher = CloudflareQuickTunnelMediaPublisher(
        tmp_path,
        gateway_starter=lambda runtime_root: _gateway(tmp_path),
        public_probe=lambda url, timeout_seconds: (_ for _ in ()).throw(
            PublicMediaError("public image returned HTTP 404")
        ),
    )

    with pytest.raises(PublicMediaError, match="HTTP 404"):
        publisher.publish_product(
            run_id="wb-one",
            seed_id="seed-one",
            source_files=[{"slot_id": "main_01", "path": str(source)}],
        )


def test_quick_tunnel_publisher_rejects_untrusted_gateway_state(
    tmp_path: Path,
) -> None:
    state = _gateway(tmp_path)
    state["base_url"] = "http://127.0.0.1:8765"
    publisher = CloudflareQuickTunnelMediaPublisher(
        tmp_path,
        gateway_starter=lambda runtime_root: state,
    )

    with pytest.raises(PublicMediaError, match="public HTTPS"):
        publisher.preflight()


def test_pid_probe_works_for_the_live_manager_process_on_windows() -> None:
    assert _pid_is_alive(os.getpid()) is True


def test_quick_tunnel_uses_http2_for_reliable_windows_networks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "cloudflared.exe"
    executable.write_bytes(b"placeholder")
    monkeypatch.setenv("OZON_V2_CLOUDFLARED", str(executable))

    command = _tunnel_command(tmp_path, 8765)

    assert command[-2:] == ["--protocol", "http2"]


def test_cloudflared_download_uses_pinned_release_and_verifies_sha256(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified-cloudflared" + (b"x" * 1_000_000)
    digest = hashlib.sha256(payload).hexdigest()
    requested_urls: list[str] = []

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        return Response(payload)

    monkeypatch.setattr("ozon_v2.adapters.public_media.platform.system", lambda: "Windows")
    monkeypatch.setattr("ozon_v2.adapters.public_media.platform.machine", lambda: "AMD64")
    monkeypatch.setattr(
        "ozon_v2.adapters.public_media.CLOUDFLARED_SHA256",
        {"cloudflared-windows-amd64.exe": digest},
    )
    monkeypatch.setattr("ozon_v2.adapters.public_media.urlopen", fake_urlopen)

    executable = _download_cloudflared(tmp_path)

    assert executable.read_bytes() == payload
    assert "/releases/download/2026.7.2/" in requested_urls[0]
