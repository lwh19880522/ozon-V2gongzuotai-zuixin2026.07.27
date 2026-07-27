from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/lwh19880522/ozon-V2gongzuotai-zuixin2026.07.27"

ROOT_FILES = (
    ".gitattributes",
    ".gitignore",
    ".mcp.json",
    "ARCHITECTURE.md",
    "README.md",
    "pyproject.toml",
    "安装并启动 Ozon V2.cmd",
    "启动 Ozon V2.cmd",
)
ROOT_DIRECTORIES = (
    ".codex-plugin",
    "assets",
    "browser_extension",
    "mcp",
    "scripts",
    "skills",
    "src",
    "tests",
)
DOCUMENT_FILES = (
    "docs/INSTALLATION.md",
    "docs/RELEASE_AND_PRIVACY.md",
    "docs/USER_GUIDE.md",
    "docs/collection_contract.md",
    "docs/workbench_architecture.md",
)
IGNORED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".tmp",
    ".tmp-npm-cache",
    ".worker-output",
}
IGNORED_SUFFIXES = {
    ".pyc",
    ".pyo",
}
TEXT_SUFFIXES = {
    "",
    ".cmd",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_FILE_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
}
FORBIDDEN_FILE_SUFFIXES = {
    ".db",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
}


def _is_release_file(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(
        part in IGNORED_PARTS or part.endswith(".egg-info")
        for part in relative.parts
    ):
        return False
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return False
    return path.is_file()


def release_sources() -> list[Path]:
    sources: list[Path] = []
    for relative in ROOT_FILES + DOCUMENT_FILES:
        source = ROOT / relative
        if not source.is_file():
            raise FileNotFoundError(f"required release file is missing: {relative}")
        sources.append(source)
    for relative in ROOT_DIRECTORIES:
        directory = ROOT / relative
        if not directory.is_dir():
            raise FileNotFoundError(
                f"required release directory is missing: {relative}"
            )
        sources.extend(path for path in directory.rglob("*") if _is_release_file(path))
    return sorted(set(sources), key=lambda item: item.as_posix().lower())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _privacy_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    return (
        (
            "machine-specific Windows user path",
            re.compile(
                r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!<用户名>)(?!<username>)[^<>:\"|?*\r\n\\/]+[\\/]+"
            ),
        ),
        (
            "messaging-app temporary path",
            re.compile(re.escape("xwechat_" + "files"), re.IGNORECASE),
        ),
        (
            "messaging-app runtime temp path",
            re.compile(re.escape("RW" + "Temp"), re.IGNORECASE),
        ),
        (
            "GitHub access token",
            re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        ),
        (
            "private key material",
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        ),
        (
            "real Cloudflare public media hostname",
            re.compile(
                r"(?i)\b(?:https?://)?[a-z0-9-]{6,}[.](?:workers[.]dev|r2[.]dev)\b"
            ),
        ),
        (
            "embedded access credential",
            re.compile(
                r"(?im)^\s*(?:api[_-]?key|access[_-]?key|client[_-]?secret|auth[_-]?token)\s*[:=]\s*[\"']?[A-Za-z0-9/+_-]{16,}"
            ),
        ),
    )


def privacy_findings(root: Path) -> list[str]:
    findings: list[str] = []
    patterns = _privacy_patterns()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        lower_name = path.name.lower()
        lower_suffix = path.suffix.lower()
        if (
            lower_name in FORBIDDEN_FILE_NAMES
            or lower_suffix in FORBIDDEN_FILE_SUFFIXES
        ):
            findings.append(f"{relative}: forbidden secret/runtime file")
            continue
        if lower_suffix not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in patterns:
            if pattern.search(content):
                findings.append(f"{relative}: {label}")
    return findings


def scan_or_fail(root: Path) -> None:
    findings = privacy_findings(root)
    if findings:
        print("PRIVACY_SCAN_FAILED")
        for finding in findings:
            print(f"- {finding}")
        raise SystemExit(1)
    print(f"PRIVACY_SCAN_PASSED files={sum(1 for path in root.rglob('*') if path.is_file())}")


def build_release(destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f"destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    for source in release_sources():
        relative = source.relative_to(ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    copied_files = sorted(
        (
            path
            for path in destination.rglob("*")
            if path.is_file() and path.name != "RELEASE_MANIFEST.json"
        ),
        key=lambda item: item.as_posix().lower(),
    )
    manifest = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "release_type": "clean-installable-source",
        "file_count": len(copied_files),
        "files": {
            path.relative_to(destination).as_posix(): sha256(path)
            for path in copied_files
        },
    }
    (destination / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    scan_or_fail(destination)
    print(f"RELEASE_BUILT destination={destination} files={len(copied_files)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or scan the privacy-clean Ozon V2 release."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--destination", type=Path)
    mode.add_argument("--scan-only", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.scan_only is not None:
        root = args.scan_only.resolve()
        if not root.is_dir():
            raise SystemExit(f"scan root does not exist: {root}")
        scan_or_fail(root)
        return 0
    build_release(args.destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
