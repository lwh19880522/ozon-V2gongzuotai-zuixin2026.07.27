# Ozon V2 Foundation Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the current workbench runner deterministic, cancellable, and safe before adding the supplier-link, image-generation, or publishing modules.

**Architecture:** Keep the existing single-process `ThreadingHTTPServer` and one `WorkbenchBackgroundRunner`. Add cooperative cancellation at the orchestrator boundary, reject supervised actions while the runner is active, isolate the flaky publish-boundary test from auto-start behavior, and add a small Windows lifecycle script for reliable local startup.

**Tech Stack:** Python 3.11 standard library, `unittest`, PowerShell, existing filesystem runtime repository.

---

## Scope Boundary

This plan fixes only OZV2-001, OZV2-002, and OZV2-012 from `docs/issues/2026-07-10-current-issues.md`.

It does not change Seller category matching, add 1688 collection, redesign the workbench views, generate images, or submit products. Those remain separate plans so the foundation repair stays reviewable.

The workspace is not a Git repository. Commit steps are replaced with explicit verification checkpoints and Obsidian operation-log entries. Do not initialize Git without user authorization.

The canonical Obsidian vault is `E:\obsidian仓库\萧机麦仓库`. Write Ozon V2 logs under `Ozon V2 日志库 (Log Vault)` in that vault. The project-local `obsidian/OzonV2-LogVault` directory is a non-canonical historical copy and must not receive new operation logs.

## File Map

- Modify: `src/ozon_v2/services/workbench_service.py` — cooperative stop probe at every automatic transition boundary.
- Modify: `src/ozon_v2/workbench/runner.py` — pass the stop probe and prevent stopped status from being overwritten.
- Modify: `src/ozon_v2/workbench/local_server.py` — inject a runner for tests and reject supervised actions while it is active.
- Modify: `tests/test_workbench_skeleton.py` — prove cancellation prevents the next state transition.
- Modify: `tests/test_workbench_runner.py` — prove a running thread exits without writing a late blocked status.
- Modify: `tests/test_workbench_local_server.py` — isolate the publish-lock test and prove the HTTP busy guard.
- Create: `scripts/workbench_control.ps1` — start, stop, and report workbench health without duplicate processes.
- Modify: `E:\obsidian仓库\萧机麦仓库\Ozon V2 日志库 (Log Vault)\02-操作日志 (Operation Logs)\2026-07-10 操作日志 (Operation Log).md` — record verified foundation repairs.

### Task 1: Add Failing Cancellation And Late-Status Tests

**Files:**
- Modify: `tests/test_workbench_skeleton.py`
- Modify: `tests/test_workbench_runner.py`
- Test: `tests/test_workbench_skeleton.py`
- Test: `tests/test_workbench_runner.py`

- [ ] **Step 1: Write the failing test**

Add this test to `WorkbenchSkeletonTests`:

```python
def test_autopilot_stop_probe_prevents_next_transition(self) -> None:
    repo = FsRepo(self.context)
    service = WorkbenchService(repo, seller_api_adapter=FakeSellerApiAdapter())
    run = repo.create_workbench_batch_record(target_count=1)
    run["status"] = WorkbenchState.STORE_DEDUPED.value
    repo.save_run(run)

    result = service.run_until_blocked(
        run["run_id"],
        max_steps=5,
        should_stop=lambda: True,
    )

    self.assertTrue(result.ok)
    self.assertEqual("autopilot.blocked", result.code)
    self.assertEqual("user_stopped", result.data["blocked_reason"])
    self.assertEqual(
        WorkbenchState.STORE_DEDUPED.value,
        repo.load_run(run["run_id"])["status"],
    )
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_workbench_skeleton.WorkbenchSkeletonTests.test_autopilot_stop_probe_prevents_next_transition
```

Expected: FAIL because `run_until_blocked()` does not accept `should_stop`.

- [ ] **Step 3: Add a deterministic cooperative service fixture**

Add `import threading` to `tests/test_workbench_runner.py`, then add this fixture near the existing fake workers:

```python
class CooperativeBlockingService:
    def __init__(self, repo: FsRepo) -> None:
        self.repo = repo
        self.entered = threading.Event()

    def run_until_blocked(self, run_id, max_steps=20, should_stop=None):
        self.entered.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                return Result.success(
                    "autopilot.blocked",
                    "Stopped by user.",
                    {"blocked_reason": "user_stopped"},
                )
            time.sleep(0.01)
        raise AssertionError("stop probe was not observed")
```

- [ ] **Step 4: Write the late-status regression test**

```python
def test_stop_is_not_overwritten_when_background_thread_exits(self) -> None:
    repo = FsRepo(self.context)
    service = CooperativeBlockingService(repo)
    runner = WorkbenchBackgroundRunner(service)
    run_id = repo.create_workbench_batch_record(target_count=1)["run_id"]

    runner.start(run_id)
    self.assertTrue(service.entered.wait(timeout=2))
    runner.stop(run_id)
    status = self.wait_until_idle(runner, run_id)
    time.sleep(0.05)

    self.assertEqual("stopped", status["state"])
    self.assertEqual("user_stopped", status["blocked_reason"])
    event_types = [event.event_type for event in repo.load_run_events(run_id)]
    stopped_index = event_types.index("runner.stopped")
    self.assertNotIn("runner.blocked", event_types[stopped_index + 1 :])
```

- [ ] **Step 5: Run the runner test and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_workbench_runner.WorkbenchBackgroundRunnerTests.test_stop_is_not_overwritten_when_background_thread_exits
```

Expected: FAIL because the runner does not pass `should_stop` into the service fixture, so the fixture reaches `stop probe was not observed` or the runner records a late terminal status.

### Task 2: Implement Cooperative Cancellation

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/runner.py`
- Test: `tests/test_workbench_skeleton.py`

- [ ] **Step 1: Add the stop-probe type and parameter**

Update the service import and signature:

```python
from collections.abc import Callable

def run_until_blocked(
    self,
    run_id: str,
    max_steps: int = 20,
    should_stop: Callable[[], bool] | None = None,
) -> Result:
```

- [ ] **Step 2: Check cancellation before every automatic step**

At the start of each `for _step in range(max_steps)` iteration, before loading and dispatching the next state:

```python
if should_stop is not None and should_stop():
    return self._autopilot_blocked(
        run_id,
        "user_stopped",
        "Autopilot stopped before the next state transition.",
        history,
    )
```

Do not roll back the last completed transition. Cancellation only prevents the next transition.

- [ ] **Step 3: Pass the runner stop probe**

Change the runner call to:

```python
result = self.service.run_until_blocked(
    run_id,
    max_steps=remaining_steps,
    should_stop=lambda: self._stop_requested(run_id),
)
```

Keep the existing `_stop_requested()` checks immediately before `_finish()` and before terminal runner events. Do not add a second runner state machine.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_workbench_skeleton.WorkbenchSkeletonTests.test_autopilot_stop_probe_prevents_next_transition
python -m unittest tests.test_workbench_runner
```

Expected: both commands PASS.

### Task 3: Reject Supervised Actions While The Runner Is Active

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `tests/test_workbench_local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Make the runner injectable without changing production defaults**

Change the handler factory to:

```python
def create_handler(
    repo: FsRepo | None = None,
    background_runner: WorkbenchBackgroundRunner | None = None,
) -> type[BaseHTTPRequestHandler]:
    selected_repo = repo or FsRepo()
    service = background_runner.service if background_runner is not None else WorkbenchService(selected_repo)
    credential_service = CredentialService(selected_repo)
    runner = background_runner or WorkbenchBackgroundRunner(service)
```

`make_server()` continues calling `create_handler(repo)` so production behavior is unchanged.

- [ ] **Step 2: Add a fake busy runner and failing HTTP test**

Add a minimal fake in the local-server test file:

```python
class FakeBusyRunner:
    def __init__(self, service: WorkbenchService) -> None:
        self.service = service

    def status(self, run_id: str) -> dict:
        return {"run_id": run_id, "running": True, "state": "running"}
```

Start a temporary `ThreadingHTTPServer` with `create_handler(repo, FakeBusyRunner(service))`, then post a valid action and assert:

```python
self.assertFalse(result["ok"])
self.assertEqual("runner.busy", result["code"])
self.assertEqual(original_status, repo.load_run(run_id)["status"])
```

- [ ] **Step 3: Run the busy-boundary test and verify RED**

Expected: FAIL because `/actions` currently calls `service.dispatch()` unconditionally.

- [ ] **Step 4: Add the HTTP guard**

Before dispatching an action:

```python
runner_status = runner.status(parts[2])
if runner_status.get("running"):
    self._send_result(
        Result.failure(
            "runner.busy",
            "Stop or wait for the background runner before using a supervised action.",
        ),
        run_id=parts[2],
    )
    return
```

This guard applies only to supervised `/actions`. Browser result-ingest endpoints remain allowed when their task is pending.

- [ ] **Step 5: Run local-server tests**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_workbench_local_server
```

Expected: PASS.

### Task 4: Remove The Flaky Publish-Test Setup

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Isolate the publish-lock test from batch auto-start**

Replace the first four setup lines in `test_publish_request_is_locked_at_http_boundary` with:

```python
run = self.repo.create_workbench_batch_record(target_count=1)
run_id = run["run_id"]
run["status"] = WorkbenchState.DRAFT_READY.value
self.repo.save_run(run)
```

The test now verifies only the publish HTTP boundary. Auto-start remains covered by `test_batch_post_starts_background_runner`.

- [ ] **Step 2: Run the formerly flaky test 40 times**

Run:

```powershell
$env:PYTHONPATH='src'
$failed=0
1..40 | ForEach-Object {
  python -m unittest tests.test_workbench_local_server.WorkbenchLocalServerTests.test_publish_request_is_locked_at_http_boundary *> $null
  if ($LASTEXITCODE -ne 0) { $failed++ }
}
"TOTAL=40 FAILED=$failed"
```

Expected: `TOTAL=40 FAILED=0`.

### Task 5: Add Reliable Windows Workbench Lifecycle Control

**Files:**
- Create: `scripts/workbench_control.ps1`
- Test: `scripts/workbench_control.ps1`

- [ ] **Step 1: Create a single start/stop/status script**

The script accepts `-Action Start|Stop|Status`, resolves the project root from `$PSScriptRoot`, stores `workbench.pid` under `E:\ozon-V2工作区\OzonOpsV2\state`, and uses `Start-Process -WindowStyle Hidden`.

Required behavior:

```powershell
param(
  [ValidateSet('Start','Stop','Status')]
  [string]$Action = 'Status',
  [int]$Port = 8765
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeState = 'E:\ozon-V2工作区\OzonOpsV2\state'
$PidFile = Join-Path $RuntimeState 'workbench.pid'
$HealthUrl = "http://127.0.0.1:$Port/api/health"
```

For `Start`, first call the health endpoint. If healthy, print `ALREADY_RUNNING` and exit without creating another process. Otherwise launch:

```powershell
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
$process = Start-Process -FilePath 'python' `
  -ArgumentList @('-m','ozon_v2.workbench.local_server','--host','127.0.0.1','--port',"$Port") `
  -WorkingDirectory $ProjectRoot `
  -WindowStyle Hidden `
  -PassThru
```

Poll the health endpoint for at most 10 seconds. Write the PID only after health succeeds. For `Stop`, verify the PID belongs to a running process before `Stop-Process`; remove only the PID file, never runtime evidence.

- [ ] **Step 2: Verify lifecycle behavior**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\workbench_control.ps1 -Action Start
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\workbench_control.ps1 -Action Start
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\workbench_control.ps1 -Action Status
Invoke-RestMethod http://127.0.0.1:8765/api/health
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\workbench_control.ps1 -Action Stop
```

Expected: first start reports `STARTED`, second reports `ALREADY_RUNNING`, status reports `RUNNING`, health returns `health.ok`, and stop reports `STOPPED`.

### Task 6: Full Verification And Documentation

**Files:**
- Modify: `docs/issues/2026-07-10-current-issues.md`
- Modify: `E:\obsidian仓库\萧机麦仓库\Ozon V2 日志库 (Log Vault)\02-操作日志 (Operation Logs)\2026-07-10 操作日志 (Operation Log).md`

- [ ] **Step 1: Run the full Python suite**

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests
```

Expected: all tests PASS with no failures or errors.

- [ ] **Step 2: Run extension syntax checks**

```powershell
node --check browser_extension\ozon_v2_bridge\background.js
node --check browser_extension\ozon_v2_bridge\content.js
node --check browser_extension\ozon_v2_bridge\workbench_content.js
```

Expected: all commands exit 0.

- [ ] **Step 3: Update issue statuses**

Mark OZV2-001, OZV2-002, and OZV2-012 as `已修复 (Resolved)` only after all verification commands pass. Record the repeated-test result and health result in the Obsidian operation log.

- [ ] **Step 4: Stop at the foundation checkpoint**

Do not start Seller category mapping or the 1688 extension in the same change. Review the foundation result first, then create the separate collection-repair plan for OZV2-003, OZV2-005, OZV2-006, and OZV2-007.
