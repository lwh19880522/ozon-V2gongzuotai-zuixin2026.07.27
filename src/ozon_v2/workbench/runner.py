from __future__ import annotations

import threading
import time
from typing import Any

from ozon_v2.app.result import Result
from ozon_v2.domain.models import utc_now_iso
from ozon_v2.services.workbench_service import WorkbenchService


class WorkbenchBackgroundRunner:
    def __init__(
        self,
        service: WorkbenchService,
        attribute_template_worker: Any | None = None,
        supplier_worker: Any | None = None,
    ) -> None:
        self.service = service
        self.attribute_template_worker = attribute_template_worker
        self.supplier_worker = supplier_worker
        self._lock = threading.Lock()
        self._statuses: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, threading.Thread] = {}

    def start(self, run_id: str, max_steps: int = 20) -> Result:
        if max_steps <= 0:
            return Result.failure("runner.invalid_max_steps", "max_steps must be greater than zero.")
        with self._lock:
            existing = self._statuses.get(run_id)
            if existing and existing.get("running"):
                return Result.success(
                    "runner.already_running",
                    "Background runner is already running for this batch.",
                    {"runner": dict(existing)},
                )
            self._statuses[run_id] = {
                "run_id": run_id,
                "running": True,
                "state": "running",
                "message": "Background runner is running until the next blocking gate.",
                "last_code": None,
                "blocked_reason": None,
                "stop_requested": False,
                "started_at": utc_now_iso(),
                "finished_at": None,
            }
        self.service.repo.append_run_event(
            run_id,
            "runner.started",
            "Background runner started from the workbench.",
            {"max_steps": max_steps},
        )
        thread = threading.Thread(target=self._run, args=(run_id, max_steps), daemon=True)
        with self._lock:
            self._threads[run_id] = thread
        thread.start()
        return Result.success(
            "runner.started",
            "Background runner started.",
            {"runner": self.status(run_id)},
        )

    def status(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            status = self._statuses.get(run_id)
            if not status:
                return {
                    "run_id": run_id,
                    "running": False,
                    "state": "idle",
                    "message": "No background runner has been started for this batch.",
                    "last_code": None,
                    "blocked_reason": None,
                    "stop_requested": False,
                    "started_at": None,
                    "finished_at": None,
                }
            return dict(status)

    def stop(self, run_id: str) -> Result:
        with self._lock:
            current = self._statuses.get(run_id, {"run_id": run_id})
            current.update(
                {
                    "running": False,
                    "state": "stopped",
                    "message": "Background runner was stopped by the user.",
                    "last_code": "runner.stopped",
                    "blocked_reason": "user_stopped",
                    "stop_requested": True,
                    "finished_at": utc_now_iso(),
                }
            )
            self._statuses[run_id] = current
        self.service.repo.append_run_event(
            run_id,
            "runner.stopped",
            "Background runner was stopped by the user.",
            {"blocked_reason": "user_stopped"},
        )
        return Result.success(
            "runner.stopped",
            "Background runner was stopped by the user.",
            {"runner": self.status(run_id)},
        )

    def stop_all_and_wait(self, run_ids: list[str], timeout_seconds: float = 8.0) -> Result:
        unique_run_ids = list(dict.fromkeys(run_ids))
        running_ids = [run_id for run_id in unique_run_ids if self.status(run_id).get("running")]
        for run_id in running_ids:
            self.stop(run_id)
        deadline = time.monotonic() + timeout_seconds
        for run_id in running_ids:
            with self._lock:
                thread = self._threads.get(run_id)
            if thread is not None:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            still_running = [
                run_id
                for run_id in running_ids
                if self._threads.get(run_id) is not None and self._threads[run_id].is_alive()
            ]
        if still_running:
            return Result.failure(
                "runner.clear_wait_timeout",
                "One or more background runners did not stop before the clear timeout.",
                data={"run_ids": still_running},
            )
        return Result.success(
            "runner.clear_ready",
            "Background runners are stopped and the batches can be cleared.",
            {"stopped_run_ids": running_ids},
        )

    def forget(self, run_ids: list[str]) -> None:
        with self._lock:
            for run_id in run_ids:
                self._statuses.pop(run_id, None)
                self._threads.pop(run_id, None)

    def _run(self, run_id: str, max_steps: int) -> None:
        try:
            result = self._run_until_blocked_or_worker_blocked(run_id, max_steps=max_steps)
            if self._stop_requested(run_id):
                return
            blocked_reason = result.data.get("blocked_reason") if isinstance(result.data, dict) else None
            state = "blocked" if result.code == "autopilot.blocked" else "finished"
            if not result.ok:
                state = "failed"
            self._finish(run_id, state, result.code, result.message, blocked_reason)
            self.service.repo.append_run_event(
                run_id,
                f"runner.{state}",
                result.message,
                {"code": result.code, "blocked_reason": blocked_reason},
            )
        except Exception as exc:  # pragma: no cover - defensive boundary for background threads
            if self._stop_requested(run_id):
                return
            message = f"Background runner failed: {exc}"
            self._finish(run_id, "failed", "runner.failed", message, None)
            self.service.repo.append_run_event(
                run_id,
                "runner.failed",
                message,
                {"error": repr(exc)},
            )

    def _run_until_blocked_or_worker_blocked(self, run_id: str, max_steps: int) -> Result:
        remaining_steps = max_steps
        while remaining_steps > 0:
            if self._stop_requested(run_id):
                return Result.success("runner.stopped", "Background runner was stopped by the user.", {"blocked_reason": "user_stopped"})
            result = self.service.run_until_blocked(
                run_id,
                max_steps=remaining_steps,
                should_stop=lambda: self._stop_requested(run_id),
            )
            if self._stop_requested(run_id):
                return Result.success("runner.stopped", "Background runner was stopped by the user.", {"blocked_reason": "user_stopped"})
            blocked_reason = result.data.get("blocked_reason") if isinstance(result.data, dict) else None
            if blocked_reason == "supplier_collection_worker_required":
                if self.supplier_worker is None:
                    return result
                worker_result = self.supplier_worker.collect(run_id)
                if not worker_result.ok:
                    return Result.success(
                        "autopilot.blocked",
                        worker_result.message,
                        {"blocked_reason": worker_result.code, "worker_result": worker_result.to_dict()},
                    )
                payload = worker_result.data.get("payload") if isinstance(worker_result.data, dict) else None
                if not isinstance(payload, dict):
                    return Result.success(
                        "autopilot.blocked",
                        "Supplier browser worker did not return an ingest payload.",
                        {"blocked_reason": "supplier_worker.invalid_payload"},
                    )
                ingest_result = self.service.ingest_supplier_collection_result(run_id, payload)
                if not ingest_result.ok:
                    return Result.success(
                        "autopilot.blocked",
                        ingest_result.message,
                        {"blocked_reason": ingest_result.code, "ingest_result": ingest_result.to_dict()},
                    )
                remaining_steps -= 1
                continue
            return result
        return Result.success(
            "autopilot.blocked",
            "Autopilot stopped because the worker loop step limit was reached.",
            {"blocked_reason": "worker_step_limit_reached"},
        )

    def _stop_requested(self, run_id: str) -> bool:
        with self._lock:
            return bool(self._statuses.get(run_id, {}).get("stop_requested"))

    def _finish(
        self,
        run_id: str,
        state: str,
        last_code: str,
        message: str,
        blocked_reason: str | None,
    ) -> None:
        with self._lock:
            current = self._statuses.get(run_id, {"run_id": run_id})
            current.update(
                {
                    "running": False,
                    "state": state,
                    "message": message,
                    "last_code": last_code,
                    "blocked_reason": blocked_reason,
                    "finished_at": utc_now_iso(),
                }
            )
            self._statuses[run_id] = current
