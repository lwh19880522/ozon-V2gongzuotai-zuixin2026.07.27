from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.app.result import Result


def _tail(value: str | bytes | None, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[-limit:]
    return str(value)[-limit:]


class SupplierBrowserWorker:
    def __init__(
        self,
        repo: FsRepo,
        node_executable: str = "node",
        timeout_seconds: int = 300,
        subprocess_runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.repo = repo
        self.node_executable = node_executable
        self.timeout_seconds = timeout_seconds
        self.subprocess_runner = subprocess_runner

    def collect(self, run_id: str) -> Result:
        try:
            contract = self.repo.load_supplier_collection_contract(run_id)
        except FileNotFoundError:
            return Result.failure("supplier_worker.missing_contract", "Supplier collection contract is missing.")
        network = contract.get("network") or {}
        if network.get("proxy_disabled") is not True:
            return Result.failure(
                "supplier_worker.direct_network_required",
                "1688 supplier collection requires direct networking with proxy disabled.",
            )
        items = contract.get("items") if isinstance(contract.get("items"), list) else []
        if not items:
            return Result.failure("supplier_worker.no_items", "Supplier collection contract has no product links.")

        worker_dir = self.repo.run_dir(run_id) / "artifacts" / "supplier_worker"
        worker_dir.mkdir(parents=True, exist_ok=True)
        input_path = worker_dir / "input.json"
        output_path = worker_dir / "output.json"
        input_payload = {
            "run_id": run_id,
            "network": {"mode": "direct", "proxy_disabled": True},
            "artifacts_dir": str(worker_dir),
            "items": items,
        }
        input_path.write_text(json.dumps(input_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        script_path = self.repo.context.project_root / "scripts" / "collect_1688_supplier_link.js"
        self.repo.append_run_event(
            run_id,
            "supplier_collection.worker_started",
            "Direct-network 1688 supplier browser worker started.",
            {
                "item_count": len(items),
                "proxy_disabled": True,
                "input_path": str(input_path),
                "output_path": str(output_path),
            },
        )
        try:
            completed = self.subprocess_runner(
                [self.node_executable, str(script_path), str(input_path), str(output_path)],
                cwd=str(self.repo.context.project_root),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return Result.failure(
                "supplier_worker.timeout",
                "1688 supplier browser worker timed out.",
                data={"timeout_seconds": self.timeout_seconds, "output_path": str(output_path)},
            )
        if not output_path.exists():
            return Result.failure(
                "supplier_worker.missing_output",
                "1688 supplier browser worker finished without an output artifact.",
                data={"stdout": _tail(completed.stdout), "stderr": _tail(completed.stderr)},
            )
        output = json.loads(output_path.read_text(encoding="utf-8"))
        if completed.returncode != 0 or output.get("ok") is not True:
            return Result.failure(
                "supplier_worker.failed",
                str(output.get("message") or "1688 supplier browser worker failed."),
                errors=[str(item) for item in output.get("errors", [])],
                data={
                    "output_path": str(output_path),
                    "stdout": _tail(completed.stdout),
                    "stderr": _tail(completed.stderr),
                },
            )
        payload = output.get("payload")
        if not isinstance(payload, dict):
            return Result.failure(
                "supplier_worker.invalid_output",
                "1688 supplier browser worker output is missing its payload.",
                data={"output_path": str(output_path)},
            )
        self.repo.append_run_event(
            run_id,
            "supplier_collection.worker_collected",
            "1688 supplier browser worker collected public supplier data.",
            {"item_count": len(payload.get("supplier_products", [])), "output_path": str(output_path)},
        )
        return Result.success(
            "supplier_worker.collected",
            "1688 supplier browser worker collected public supplier data.",
            {"payload": payload, "input_path": str(input_path), "output_path": str(output_path)},
        )
