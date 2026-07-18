from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    project_root: Path
    runtime_root: Path
    seed_asset_dir: Path

    @property
    def initial_seed_json(self) -> Path:
        return self.seed_asset_dir / "seed_pool.initial.json"

    @property
    def seed_manifest(self) -> Path:
        return self.seed_asset_dir / "manifest.json"


@dataclass(frozen=True)
class Config:
    seed_pool_version: str = "seed_pool.refined.2000.v1"
    default_ozon_query_language: str = "ru-RU"
    default_seed_source_language: str = "zh-CN"


@dataclass(frozen=True)
class AppContext:
    paths: Paths
    config: Config

    @property
    def project_root(self) -> Path:
        return self.paths.project_root

    @property
    def runtime_root(self) -> Path:
        return self.paths.runtime_root


def find_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def build_default_context(runtime_root: Path | None = None) -> AppContext:
    project_root = find_project_root()
    resolved_runtime = runtime_root or project_root.parent / "OzonOpsV2"
    return AppContext(
        paths=Paths(
            project_root=project_root,
            runtime_root=resolved_runtime,
            seed_asset_dir=project_root / "assets" / "seed_pool",
        ),
        config=Config(),
    )
