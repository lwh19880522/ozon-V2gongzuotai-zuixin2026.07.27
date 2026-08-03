from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.app.result import Result


def _tail(value: str | bytes | None, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[-limit:]
    return str(value)[-limit:]


class AttributeTemplateBrowserWorker:
    def __init__(
        self,
        repo: FsRepo,
        node_executable: str = "node",
        timeout_seconds: int = 240,
    ) -> None:
        self.repo = repo
        self.node_executable = node_executable
        self.timeout_seconds = timeout_seconds

    def collect(self, run_id: str) -> Result:
        try:
            contract = self.repo.load_attribute_template_contract(run_id)
        except FileNotFoundError:
            contract = {}
        payload = contract.get("payload") if isinstance(contract, dict) else None
        seeds = payload.get("seeds", []) if isinstance(payload, dict) else []
        if not seeds:
            return Result.failure(
                "attribute_template_worker.no_seeds",
                "Attribute template worker cannot run because sampled seeds are missing.",
            )
        worker_dir = self.repo.run_dir(run_id) / "artifacts" / "attribute_template_worker"
        worker_dir.mkdir(parents=True, exist_ok=True)
        visitor_profile_dir = self.repo.context.runtime_root / "state" / "ozon_visitor_profile"
        visitor_profile_dir.mkdir(parents=True, exist_ok=True)
        input_path = worker_dir / "input.json"
        output_path = worker_dir / "output.json"
        input_payload = {
            "run_id": run_id,
            "artifacts_dir": str(worker_dir),
            "visitor_profile_dir": str(visitor_profile_dir),
            "seeds": seeds,
        }
        input_path.write_text(json.dumps(input_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        script_path = self.repo.context.project_root / "scripts" / "ozon_attribute_template_worker.js"
        self.repo.append_run_event(
            run_id,
            "attribute_template.worker_started",
            "Attribute template browser worker started.",
            {
                "worker": "local_playwright_browser",
                "input_path": str(input_path),
                "output_path": str(output_path),
                "visitor_profile_dir": str(visitor_profile_dir),
                "access_identity": "visitor_profile",
                "ozon_proxy_required": True,
            },
        )
        try:
            completed = subprocess.run(
                [self.node_executable, str(script_path), str(input_path), str(output_path)],
                cwd=str(self.repo.context.project_root),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.repo.append_run_event(
                run_id,
                "attribute_template.worker_failed",
                "Attribute template browser worker timed out.",
                {"timeout_seconds": self.timeout_seconds, "stdout": exc.stdout or "", "stderr": exc.stderr or ""},
            )
            return Result.failure(
                "attribute_template_worker.timeout",
                "Attribute template browser worker timed out.",
                data={"input_path": str(input_path), "output_path": str(output_path)},
            )
        if not output_path.exists():
            self.repo.append_run_event(
                run_id,
                "attribute_template.worker_failed",
                "Attribute template browser worker finished without output.",
                {"stdout": _tail(completed.stdout), "stderr": _tail(completed.stderr)},
            )
            return Result.failure(
                "attribute_template_worker.missing_output",
                "Attribute template browser worker finished without output.",
                data={"input_path": str(input_path), "output_path": str(output_path)},
            )
        output = json.loads(output_path.read_text(encoding="utf-8"))
        if output.get("ok") is not True:
            self.repo.append_run_event(
                run_id,
                "attribute_template.worker_failed",
                str(output.get("message") or "Attribute template browser worker failed."),
                {
                    "output_path": str(output_path),
                    "errors": output.get("errors", []),
                    "returncode": completed.returncode,
                    "stdout": _tail(completed.stdout),
                    "stderr": _tail(completed.stderr),
                },
            )
            return Result.failure(
                "attribute_template_worker.failed",
                str(output.get("message") or "Attribute template browser worker failed."),
                errors=[str(item) for item in output.get("errors", [])],
                data={"input_path": str(input_path), "output_path": str(output_path), "worker_output": output},
            )
        if completed.returncode != 0:
            self.repo.append_run_event(
                run_id,
                "attribute_template.worker_failed",
                "Attribute template browser worker exited with an error.",
                {"returncode": completed.returncode, "stdout": _tail(completed.stdout), "stderr": _tail(completed.stderr)},
            )
            return Result.failure(
                "attribute_template_worker.failed",
                "Attribute template browser worker exited with an error.",
                data={
                    "input_path": str(input_path),
                    "output_path": str(output_path),
                    "stdout": completed.stdout or "",
                    "stderr": completed.stderr or "",
                },
            )
        payload = output.get("payload")
        if not isinstance(payload, dict):
            return Result.failure(
                "attribute_template_worker.invalid_output",
                "Attribute template browser worker output is missing payload.",
                data={"output_path": str(output_path), "worker_output": output},
            )
        self.repo.append_run_event(
            run_id,
            "attribute_template.worker_collected",
            "Attribute template browser worker collected a template payload.",
            {"output_path": str(output_path), "seed_count": len(payload.get("seed_templates", []))},
        )
        return Result.success(
            "attribute_template_worker.collected",
            "Attribute template browser worker collected a template payload.",
            {"payload": payload, "input_path": str(input_path), "output_path": str(output_path)},
        )
