from __future__ import annotations

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.app.result import Result


def get_artifacts_resource(run_id: str) -> dict:
    repo = FsRepo()
    run_dir = repo.run_dir(run_id)
    artifacts = {
        "run_json": str(run_dir / "run.json"),
        "sampled_seeds_json": str(run_dir / "sampled_seeds.json"),
        "collection_pairs_jsonl": str(run_dir / "collection_pairs.jsonl"),
        "evidence_csv": str(run_dir / "evidence.csv"),
        "artifacts_dir": str(run_dir / "artifacts"),
    }
    return Result.success("artifacts.ready", "Run artifact paths.", artifacts).to_dict()

