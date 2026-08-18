from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import platform
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen


class PublicMediaError(RuntimeError):
    pass


PublicProbe = Callable[[str, int], None]
GatewayStarter = Callable[[Path], dict[str, Any]]

GATEWAY_DIRNAME = "public_media_gateway"
STATE_FILENAME = "state.json"
STOP_FILENAME = "stop.requested"
TUNNEL_HOST_SUFFIX = ".trycloudflare.com"
TUNNEL_URL_PATTERN = "https://"
CLOUDFLARED_VERSION = "2026.7.2"
CLOUDFLARED_SHA256 = {
    "cloudflared-windows-amd64.exe": "cdb5d4432f6ae1595654a692a51308b69d2bf7af961f5578d9391837cf072df9",
    "cloudflared-linux-amd64": "ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd",
    "cloudflared-linux-arm64": "405df476437e027fc6d18729a5a77155c0a33a6082aeee60a799a688f3052e66",
}


class CloudflareQuickTunnelMediaPublisher:
    """Publish reviewed media through an automatically managed Quick Tunnel."""

    def __init__(
        self,
        runtime_root: str | Path,
        *,
        gateway_starter: GatewayStarter | None = None,
        public_probe: PublicProbe | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.gateway_starter = gateway_starter or ensure_quick_tunnel_gateway
        self.public_probe = public_probe or _probe_public_media

    def preflight(self) -> dict[str, Any]:
        state = dict(self.gateway_starter(self.runtime_root))
        base_url = _quick_tunnel_base_url(state.get("base_url"))
        route_token = _route_token(state.get("route_token"))
        media_root = Path(str(state.get("media_root") or "")).resolve()
        if not media_root.is_dir():
            raise PublicMediaError("Automatic public media directory is unavailable.")
        return {
            **state,
            "ready": True,
            "provider": "cloudflare_quick_tunnel",
            "base_url": base_url,
            "route_token": route_token,
            "media_root": str(media_root),
        }

    def publish_product(
        self,
        *,
        run_id: str,
        seed_id: str,
        source_files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        gateway = self.preflight()
        if not source_files:
            raise PublicMediaError("No reviewed local media files are available to publish.")
        media_root = Path(gateway["media_root"])
        base_url = str(gateway["base_url"])
        route_token = str(gateway["route_token"])
        product_directory = media_root.joinpath(
            "ozon-v2",
            _safe_segment(run_id),
            _safe_segment(seed_id),
        )
        shutil.rmtree(product_directory, ignore_errors=True)
        product_directory.mkdir(parents=True, exist_ok=True)

        published: list[dict[str, Any]] = []
        for index, item in enumerate(source_files, start=1):
            source = Path(str(item.get("path") or "")).expanduser().resolve()
            if not source.is_file():
                raise PublicMediaError(f"Reviewed media file is missing: {source}")
            slot_id = _safe_segment(item.get("slot_id") or f"image_{index:02d}")
            digest = _file_sha256(source)
            suffix = source.suffix.lower() or ".jpg"
            relative_parts = (
                "ozon-v2",
                _safe_segment(run_id),
                _safe_segment(seed_id),
                f"{slot_id}-{digest[:12]}{suffix}",
            )
            destination = media_root.joinpath(*relative_parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.is_file() or _file_sha256(destination) != digest:
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                shutil.copyfile(source, temporary)
                temporary.replace(destination)
            encoded_path = "/".join(
                quote(part, safe="") for part in (route_token, *relative_parts)
            )
            public_url = f"{base_url}/{encoded_path}"
            self.public_probe(public_url, 30)
            published.append(
                {
                    "slot_id": slot_id,
                    "source_path": str(source),
                    "published_path": str(destination),
                    "sha256": digest,
                    "public_url": public_url,
                    "content_type": mimetypes.guess_type(source.name)[0]
                    or "application/octet-stream",
                }
            )
        return {
            "provider": "cloudflare_quick_tunnel",
            "base_url": base_url,
            "urls": [item["public_url"] for item in published],
            "items": published,
            "gateway_pid": gateway.get("manager_pid"),
        }


def ensure_quick_tunnel_gateway(runtime_root: str | Path) -> dict[str, Any]:
    root = Path(runtime_root).resolve()
    gateway_dir = root / GATEWAY_DIRNAME
    gateway_dir.mkdir(parents=True, exist_ok=True)
    state_path = gateway_dir / STATE_FILENAME
    state = _load_gateway_state(state_path)
    if _state_is_live(state):
        return state

    state_path.unlink(missing_ok=True)
    (gateway_dir / STOP_FILENAME).unlink(missing_ok=True)
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "ozon_public_media_gateway.py"
    if not script_path.is_file():
        raise PublicMediaError(f"Automatic public gateway launcher is missing: {script_path}")
    log_path = gateway_dir / "gateway.log"
    log_handle = log_path.open("ab", buffering=0)
    command = [
        sys.executable,
        str(script_path),
        "--runtime-root",
        str(root),
        "serve",
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "cwd": str(Path(__file__).resolve().parents[3]),
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise PublicMediaError("Automatic public gateway could not be started.") from exc
    finally:
        log_handle.close()

    deadline = time.monotonic() + 240
    last_detail = ""
    while time.monotonic() < deadline:
        state = _load_gateway_state(state_path)
        if state and _state_is_live(state):
            return state
        if process.poll() is not None:
            last_detail = _tail_text(log_path)
            break
        time.sleep(0.5)
    if process.poll() is None:
        process.terminate()
    raise PublicMediaError(
        "Automatic free public gateway did not become ready. "
        + (last_detail or "See public_media_gateway/gateway.log.")
    )


def stop_quick_tunnel_gateway(runtime_root: str | Path) -> dict[str, Any]:
    root = Path(runtime_root).resolve()
    gateway_dir = root / GATEWAY_DIRNAME
    state = _load_gateway_state(gateway_dir / STATE_FILENAME)
    if not state:
        return {"stopped": True, "was_running": False}
    (gateway_dir / STOP_FILENAME).write_text("stop\n", encoding="utf-8")
    manager_pid = int(state.get("manager_pid") or 0)
    deadline = time.monotonic() + 15
    while manager_pid > 0 and _pid_is_alive(manager_pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    return {
        "stopped": not _pid_is_alive(manager_pid),
        "was_running": True,
        "manager_pid": manager_pid,
    }


def serve_quick_tunnel_gateway(runtime_root: str | Path) -> None:
    root = Path(runtime_root).resolve()
    gateway_dir = root / GATEWAY_DIRNAME
    media_root = gateway_dir / "files"
    state_path = gateway_dir / STATE_FILENAME
    stop_path = gateway_dir / STOP_FILENAME
    shutil.rmtree(media_root, ignore_errors=True)
    media_root.mkdir(parents=True, exist_ok=True)
    state_path.unlink(missing_ok=True)
    stop_path.unlink(missing_ok=True)
    route_token = secrets.token_urlsafe(24).replace("-", "_")
    port = _free_local_port()
    handler = _media_handler(media_root, route_token)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    tunnel: subprocess.Popen[str] | None = None
    try:
        tunnel_command = _tunnel_command(root, port)
        tunnel = subprocess.Popen(
            tunnel_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(gateway_dir),
            **_hidden_process_kwargs(),
        )
        base_url = _wait_for_tunnel_url(tunnel, 90)
        health_url = f"{base_url}/healthz"
        _probe_gateway_health(health_url, 90)
        state = {
            "ready": True,
            "provider": "cloudflare_quick_tunnel",
            "base_url": base_url,
            "route_token": route_token,
            "media_root": str(media_root.resolve()),
            "port": port,
            "manager_pid": os.getpid(),
            "tunnel_pid": tunnel.pid,
            "started_at_epoch": time.time(),
        }
        _write_json_atomic(state_path, state)
        while tunnel.poll() is None and not stop_path.exists():
            time.sleep(0.5)
    finally:
        state_path.unlink(missing_ok=True)
        stop_path.unlink(missing_ok=True)
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tunnel.kill()
        server.shutdown()
        server.server_close()
        shutil.rmtree(media_root, ignore_errors=True)


def _tunnel_command(runtime_root: Path, port: int) -> list[str]:
    configured = str(os.environ.get("OZON_V2_CLOUDFLARED") or "").strip()
    if configured and Path(configured).is_file():
        executable = str(Path(configured).resolve())
    else:
        executable = shutil.which("cloudflared") or str(
            _download_cloudflared(runtime_root)
        )
    return [
        executable,
        "tunnel",
        "--no-autoupdate",
        "--url",
        f"http://127.0.0.1:{port}",
        "--protocol",
        "http2",
    ]


def _download_cloudflared(runtime_root: Path) -> Path:
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
    if system == "windows":
        asset = "cloudflared-windows-amd64.exe"
    elif system == "linux":
        asset = f"cloudflared-linux-{arch}"
    elif system == "darwin":
        asset = f"cloudflared-darwin-{arch}.tgz"
    else:
        raise PublicMediaError(f"Unsupported operating system for automatic tunnel: {system}")
    if asset.endswith(".tgz"):
        raise PublicMediaError(
            "Automatic cloudflared bootstrap currently supports Windows and Linux."
        )
    destination = runtime_root / GATEWAY_DIRNAME / "tools" / (
        "cloudflared.exe" if system == "windows" else "cloudflared"
    )
    expected_sha256 = CLOUDFLARED_SHA256.get(asset)
    if not expected_sha256:
        raise PublicMediaError(f"No pinned cloudflared checksum is available for {asset}.")
    if destination.is_file():
        if destination.stat().st_size > 1_000_000 and _file_sha256(destination) == expected_sha256:
            return destination
        destination.unlink(missing_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    url = (
        "https://github.com/cloudflare/cloudflared/releases/download/"
        f"{CLOUDFLARED_VERSION}/{asset}"
    )
    request = Request(url, headers={"User-Agent": "OzonV2/1.0"})
    try:
        with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (HTTPError, URLError, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise PublicMediaError("cloudflared could not be downloaded automatically.") from exc
    if temporary.stat().st_size <= 1_000_000:
        temporary.unlink(missing_ok=True)
        raise PublicMediaError("Downloaded cloudflared binary is unexpectedly small.")
    if _file_sha256(temporary) != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise PublicMediaError("Downloaded cloudflared binary failed SHA-256 verification.")
    temporary.replace(destination)
    if os.name != "nt":
        destination.chmod(0o755)
    return destination


def _media_handler(media_root: Path, route_token: str) -> type[BaseHTTPRequestHandler]:
    root = media_root.resolve()
    prefix = f"/{route_token}/"

    class MediaHandler(BaseHTTPRequestHandler):
        server_version = "OzonV2PublicMedia/1.0"

        def do_GET(self) -> None:  # noqa: N802
            self._serve(head_only=False)

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(head_only=True)

        def do_POST(self) -> None:  # noqa: N802
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

        def do_PUT(self) -> None:  # noqa: N802
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _serve(self, *, head_only: bool) -> None:
            request_path = unquote(urlparse(self.path).path)
            if request_path == "/healthz":
                payload = b"ok\n"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if not head_only:
                    self.wfile.write(payload)
                return
            if not request_path.startswith(prefix):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            relative = Path(request_path[len(prefix) :])
            if relative.is_absolute() or ".." in relative.parts:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not candidate.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(candidate.stat().st_size))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            if not head_only:
                with candidate.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile)

    return MediaHandler


def _wait_for_tunnel_url(process: subprocess.Popen[str], timeout_seconds: int) -> str:
    if process.stdout is None:
        raise PublicMediaError("Automatic tunnel output is unavailable.")
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        for token in line.replace("\x1b", " ").split():
            cleaned = token.strip("|[](){}<>.,;\"'")
            if cleaned.startswith(TUNNEL_URL_PATTERN):
                try:
                    return _quick_tunnel_base_url(cleaned)
                except PublicMediaError:
                    continue
    raise PublicMediaError("Cloudflare Quick Tunnel did not return a public URL.")


def _state_is_live(state: dict[str, Any]) -> bool:
    if not state:
        return False
    try:
        base_url = _quick_tunnel_base_url(state.get("base_url"))
        _route_token(state.get("route_token"))
        media_root = Path(str(state.get("media_root") or "")).resolve()
        manager_pid = int(state.get("manager_pid") or 0)
    except (PublicMediaError, OSError, TypeError, ValueError):
        return False
    if not media_root.is_dir() or not _pid_is_alive(manager_pid):
        return False
    try:
        _probe_gateway_health(f"{base_url}/healthz", 5)
    except PublicMediaError:
        return False
    return True


def _load_gateway_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_pid_is_alive(pid: int) -> bool:
    """Probe a Windows process without sending a signal to it."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(get_exit_code(handle, ctypes.byref(exit_code))) and (
                exit_code.value == 259  # STILL_ACTIVE
            )
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _hidden_process_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _probe_gateway_health(url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = Request(url, method="GET", headers={"User-Agent": "OzonV2/1.0"})
            with urlopen(request, timeout=min(5, max(1, timeout_seconds))) as response:
                if int(response.status) == 200 and response.read(3) == b"ok\n":
                    return
                last_error = f"HTTP {response.status}"
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise PublicMediaError(f"Automatic public gateway verification failed: {last_error}")


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


def _quick_tunnel_base_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    hostname = str(parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not hostname.endswith(TUNNEL_HOST_SUFFIX):
        raise PublicMediaError("A Cloudflare Quick Tunnel public HTTPS URL is required.")
    return text


def _route_token(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) < 12 or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in text
    ):
        raise PublicMediaError("Automatic public media route token is invalid.")
    return text


def _safe_segment(value: Any) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in str(value or "").strip()
    ).strip("-")
    return cleaned or "item"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail_text(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip()
    except OSError:
        return ""
