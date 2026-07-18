# Extension Version Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Ozon V2 from creating or starting a batch unless the local browser bridge is online and its actually loaded extension version exactly matches the project manifest.

**Architecture:** Keep the existing single bridge status file and local `ThreadingHTTPServer`. Read the required version directly from the extension manifest, enrich the existing bridge status response with fail-closed readiness fields, ignore only stale invalidated old-page heartbeats when a recent compatible background heartbeat exists, and enforce the same readiness at both the workbench UI and `POST /api/batches` boundary.

**Tech Stack:** Python 3.11 standard library, `unittest`, Manifest V3 JavaScript, Node.js assertion tests, existing filesystem runtime repository, PowerShell lifecycle script.

---

## Scope Boundary

This plan implements only `docs/superpowers/specs/2026-07-10-extension-version-gate-design.md`.

It does not change Ozon search queries, Chinese cross-border seller evidence, seed selection, 1688 supplier handling, image generation, upload behavior, or any old plugin. It does not add an extension auto-updater or introduce another bridge state store.

The workspace is not a Git repository. Commit steps are replaced with explicit verification checkpoints. Do not initialize Git without user authorization.

## File Map

- Modify: `browser_extension/ozon_v2_bridge/background.js` - report the loaded version from `chrome.runtime.getManifest()` instead of a copied string.
- Modify: `browser_extension/ozon_v2_bridge/content.js` - report the loaded version from the extension manifest on Ozon heartbeats.
- Modify: `browser_extension/ozon_v2_bridge/workbench_content.js` - report the loaded version from the extension manifest on workbench heartbeats.
- Modify: `tests/test_browser_extension_background.js` - prove that the manifest version, rather than a hardcoded value, reaches heartbeat and tab ownership state.
- Modify: `src/ozon_v2/workbench/local_server.py` - calculate bridge readiness, protect compatible status from invalidated old pages, reject batch creation, and render version state in the workbench.
- Modify: `tests/test_workbench_local_server.py` - cover status fields, heartbeat precedence, expiry, no-side-effect batch rejection, and UI behavior.
- Modify: `E:\obsidian仓库\萧机麦仓库\Ozon V2 日志库 (Log Vault)\02-操作日志 (Operation Logs)\2026-07-10 操作日志 (Operation Log).md` - record the verified implementation result.

### Task 1: Report The Actually Loaded Extension Version

**Files:**
- Modify: `tests/test_browser_extension_background.js`
- Modify: `browser_extension/ozon_v2_bridge/background.js`
- Modify: `browser_extension/ozon_v2_bridge/content.js`
- Modify: `browser_extension/ozon_v2_bridge/workbench_content.js`
- Test: `tests/test_browser_extension_background.js`

- [ ] **Step 1: Write the failing browser test**

In `tests/test_browser_extension_background.js`, add a manifest-version fixture and record heartbeat bodies:

```javascript
const manifestVersion = "9.8.7";
const postedPayloads = [];

const chrome = {
  // Existing storage, tabs, alarms, and action fixtures remain unchanged.
  runtime: {
    getManifest() {
      return { version: manifestVersion };
    },
    onInstalled: event("installed"),
    onStartup: event("startup"),
    onMessage: event("message"),
  },
};
```

Replace the POST branch of the test `fetch` fixture with:

```javascript
if (options.method === "POST") {
  postedPayloads.push(JSON.parse(options.body));
  return { json: async () => ({ ok: true }) };
}
```

After `await listeners.action();`, add:

```javascript
assert.ok(
  postedPayloads.some((payload) => payload.extension_version === manifestVersion),
  "heartbeats must report the version loaded from chrome.runtime.getManifest()",
);
```

Change the final mapped-tab assertion from the literal `"0.1.11"` to:

```javascript
assert.equal(
  stored.openedTasks["wb-new:ozon_attribute_template"].extensionVersion,
  manifestVersion,
);
```

- [ ] **Step 2: Run the browser test and verify RED**

Run:

```powershell
node tests/test_browser_extension_background.js
```

Expected: FAIL because the extension currently reports the copied value `0.1.11` instead of the mocked manifest value `9.8.7`.

- [ ] **Step 3: Replace copied version strings with the manifest value**

In each of the three extension scripts, replace the handwritten version declaration with:

```javascript
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
```

The affected declarations are:

```text
browser_extension/ozon_v2_bridge/background.js
browser_extension/ozon_v2_bridge/content.js
browser_extension/ozon_v2_bridge/workbench_content.js
```

After the scripts use the loaded manifest value, increment `manifest.json` from `0.1.11` to `0.1.12`. This is required so an Edge process still running the pre-gate `0.1.11` code cannot satisfy the new server gate without reloading the extension.

- [ ] **Step 4: Run the browser test and syntax checks and verify GREEN**

Run:

```powershell
node tests/test_browser_extension_background.js
node --check browser_extension/ozon_v2_bridge/background.js
node --check browser_extension/ozon_v2_bridge/content.js
node --check browser_extension/ozon_v2_bridge/workbench_content.js
```

Expected: the behavior test prints `browser background task reuse: OK`; all three syntax checks exit with code `0` and no output.

### Task 2: Calculate Fail-Closed Bridge Readiness And Protect Good Heartbeats

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write failing bridge-readiness tests**

Add these imports to `tests/test_workbench_local_server.py`:

```python
from datetime import datetime, timedelta, timezone
```

Add this helper to `WorkbenchLocalServerTests`:

```python
def required_extension_version(self) -> str:
    manifest_path = self.project_root / "browser_extension" / "ozon_v2_bridge" / "manifest.json"
    return str(json.loads(manifest_path.read_text(encoding="utf-8"))["version"])
```

Add these tests:

```python
def test_bridge_status_reports_loaded_required_and_ready_versions(self) -> None:
    required = self.required_extension_version()
    self.repo.save_browser_bridge_status(
        {
            "source": "background_interval",
            "extension_version": required,
            "stage": "idle",
            "code": "browser_task.none",
        }
    )

    status = self.get_json("/api/browser-bridge/status")["data"]

    self.assertEqual(required, status["loaded_extension_version"])
    self.assertEqual(required, status["required_extension_version"])
    self.assertTrue(status["version_ready"])
    self.assertEqual("ready", status["readiness_code"])

def test_invalidated_old_page_heartbeat_cannot_replace_recent_compatible_background(self) -> None:
    required = self.required_extension_version()
    self.post_json(
        "/api/browser-bridge/heartbeat",
        {
            "source": "background_interval",
            "extension_version": required,
            "stage": "idle",
            "code": "browser_task.none",
            "message": "Background bridge is healthy.",
        },
    )

    ignored = self.post_json(
        "/api/browser-bridge/heartbeat",
        {
            "source": "workbench_content_script",
            "extension_version": "0.1.9",
            "stage": "workbench_poll_failed",
            "code": "browser_bridge.workbench_poll_failed",
            "message": "Extension context invalidated.",
        },
    )
    status = self.get_json("/api/browser-bridge/status")["data"]

    self.assertEqual("browser_bridge.heartbeat_ignored", ignored["code"])
    self.assertEqual("background_interval", status["source"])
    self.assertEqual(required, status["loaded_extension_version"])
    self.assertTrue(status["version_ready"])

def test_compatible_heartbeat_expires_after_twenty_seconds(self) -> None:
    required = self.required_extension_version()
    saved = self.repo.save_browser_bridge_status(
        {
            "source": "background_interval",
            "extension_version": required,
            "stage": "idle",
            "code": "browser_task.none",
        }
    )
    saved["updated_at"] = (datetime.now(timezone.utc) - timedelta(seconds=21)).isoformat()
    self.repo.browser_bridge_status_path.write_text(
        json.dumps(saved, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    status = self.get_json("/api/browser-bridge/status")["data"]

    self.assertFalse(status["online"])
    self.assertFalse(status["version_ready"])
    self.assertEqual("offline", status["readiness_code"])
```

- [ ] **Step 2: Run the targeted tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_bridge_status_reports_loaded_required_and_ready_versions `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_invalidated_old_page_heartbeat_cannot_replace_recent_compatible_background `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_compatible_heartbeat_expires_after_twenty_seconds
```

Expected: FAIL because the status endpoint does not expose version readiness and every heartbeat currently overwrites the previous status.

- [ ] **Step 3: Add minimal manifest and readiness helpers**

In `src/ozon_v2/workbench/local_server.py`, add the constant and module-level helpers after the imports:

```python
BRIDGE_HEARTBEAT_TIMEOUT_SECONDS = 20


def required_extension_version(project_root: Path) -> str | None:
    manifest_path = project_root / "browser_extension" / "ozon_v2_bridge" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = str(manifest.get("version") or "").strip()
    return version or None


def heartbeat_age_seconds(payload: dict[str, Any]) -> float | None:
    updated_at = payload.get("updated_at")
    if not updated_at:
        return None
    try:
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()


def bridge_readiness(payload: dict[str, Any], required_version: str | None) -> dict[str, Any]:
    status = dict(payload)
    age_seconds = heartbeat_age_seconds(status)
    online = age_seconds is not None and age_seconds <= BRIDGE_HEARTBEAT_TIMEOUT_SECONDS
    loaded_version = str(status.get("extension_version") or "").strip() or None
    if not online:
        readiness_code = "offline"
    elif not loaded_version or not required_version:
        readiness_code = "version_unknown"
    elif loaded_version != required_version:
        readiness_code = "version_mismatch"
    else:
        readiness_code = "ready"
    status.update(
        {
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "online": online,
            "loaded_extension_version": loaded_version,
            "required_extension_version": required_version,
            "version_ready": readiness_code == "ready",
            "readiness_code": readiness_code,
        }
    )
    return status
```

Replace `_bridge_status_payload()` with:

```python
def _bridge_status_payload(self, status: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(status or selected_repo.load_browser_bridge_status())
    required = required_extension_version(selected_repo.context.project_root)
    return bridge_readiness(payload, required)
```

- [ ] **Step 4: Ignore only invalidated incompatible page heartbeats when a current compatible heartbeat exists**

Inside the handler class, add:

```python
def _should_ignore_invalidated_heartbeat(self, incoming: dict[str, Any]) -> bool:
    message = str(incoming.get("message") or "")
    source = str(incoming.get("source") or "")
    required = required_extension_version(selected_repo.context.project_root)
    incoming_version = str(incoming.get("extension_version") or "").strip() or None
    current = self._bridge_status_payload()
    return (
        source == "workbench_content_script"
        and "Extension context invalidated" in message
        and incoming_version != required
        and current.get("version_ready") is True
    )
```

At `POST /api/browser-bridge/heartbeat`, construct the existing heartbeat dictionary as `incoming`, then use:

```python
if self._should_ignore_invalidated_heartbeat(incoming):
    saved = selected_repo.load_browser_bridge_status()
    response_code = "browser_bridge.heartbeat_ignored"
    response_message = "Invalidated old-page heartbeat was ignored."
else:
    saved = selected_repo.save_browser_bridge_status(incoming)
    response_code = "browser_bridge.heartbeat"
    response_message = "Browser bridge heartbeat saved."
```

Keep the existing candidate rejection and exhaustion event persistence unchanged. Return `response_code`, `response_message`, and `self._bridge_status_payload(saved)` in the heartbeat response.

- [ ] **Step 5: Run the targeted tests and verify GREEN**

Run the same three-test command from Step 2.

Expected: `Ran 3 tests` followed by `OK`.

### Task 3: Enforce The Version Gate Before Batch Creation

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Make the existing server fixture explicitly bridge-ready**

In `WorkbenchLocalServerTests.setUp()`, immediately after `self.repo = FsRepo(self.context)`, add:

```python
self.repo.save_browser_bridge_status(
    {
        "source": "background_interval",
        "extension_version": self.required_extension_version(),
        "stage": "idle",
        "code": "browser_task.none",
    }
)
```

In `test_browser_bridge_heartbeat_status_is_visible`, add this as the first line so that the test still exercises the no-heartbeat state:

```python
self.repo.browser_bridge_status_path.unlink(missing_ok=True)
```

Also include the current version in that test's heartbeat payload:

```python
"extension_version": self.required_extension_version(),
```

- [ ] **Step 2: Write failing no-side-effect gate tests**

Add this assertion helper to `WorkbenchLocalServerTests`:

```python
def assert_batch_rejected_without_side_effects(self, expected_code: str) -> dict:
    active_before = self.repo.active_seed_path.read_bytes()
    used_before = self.repo.used_seed_path.read_bytes()
    run_dirs_before = sorted(path.name for path in self.repo.runs_dir.iterdir() if path.is_dir())

    result = self.post_json("/api/batches", {"target_count": 1}, ok=False)

    self.assertFalse(result["ok"])
    self.assertEqual(expected_code, result["code"])
    self.assertEqual(active_before, self.repo.active_seed_path.read_bytes())
    self.assertEqual(used_before, self.repo.used_seed_path.read_bytes())
    self.assertEqual(
        run_dirs_before,
        sorted(path.name for path in self.repo.runs_dir.iterdir() if path.is_dir()),
    )
    return result
```

Add these tests:

```python
def test_batch_api_rejects_offline_bridge_without_creating_or_sampling(self) -> None:
    self.repo.browser_bridge_status_path.unlink(missing_ok=True)

    result = self.assert_batch_rejected_without_side_effects("browser_bridge.offline")

    self.assertEqual("offline", result["data"]["browser_bridge"]["readiness_code"])

def test_batch_api_rejects_unknown_or_mismatched_extension_without_side_effects(self) -> None:
    cases = (
        (None, "version_unknown"),
        ("0.1.9", "version_mismatch"),
    )
    for loaded_version, readiness_code in cases:
        with self.subTest(loaded_version=loaded_version):
            self.repo.save_browser_bridge_status(
                {
                    "source": "background_interval",
                    "extension_version": loaded_version,
                    "stage": "idle",
                    "code": "browser_task.none",
                }
            )

            result = self.assert_batch_rejected_without_side_effects("browser_bridge.update_required")

            self.assertEqual(readiness_code, result["data"]["browser_bridge"]["readiness_code"])

def test_batch_api_rejects_expired_compatible_heartbeat(self) -> None:
    status = self.repo.load_browser_bridge_status()
    status["updated_at"] = (datetime.now(timezone.utc) - timedelta(seconds=21)).isoformat()
    self.repo.browser_bridge_status_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    self.assert_batch_rejected_without_side_effects("browser_bridge.offline")
```

- [ ] **Step 3: Run the gate tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_batch_api_rejects_offline_bridge_without_creating_or_sampling `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_batch_api_rejects_unknown_or_mismatched_extension_without_side_effects `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_batch_api_rejects_expired_compatible_heartbeat
```

Expected: FAIL because `POST /api/batches` currently calls `service.start_batch()` without checking bridge readiness.

- [ ] **Step 4: Add the hard gate before `service.start_batch()`**

Replace the beginning of the `/api/batches` branch in `do_POST()` with:

```python
if path == "/api/batches":
    bridge = self._bridge_status_payload()
    if not bridge["online"]:
        self._send_result(
            Result.failure(
                "browser_bridge.offline",
                "Browser bridge is offline; reload the Edge extension and refresh the workbench.",
                data={"browser_bridge": bridge},
            )
        )
        return
    if not bridge["version_ready"]:
        self._send_result(
            Result.failure(
                "browser_bridge.update_required",
                "Browser bridge extension version does not match the project manifest.",
                data={"browser_bridge": bridge},
            )
        )
        return
    result = service.start_batch(int(payload.get("target_count", 1)))
```

Keep the existing successful result, runner start, and failure response code below this block unchanged.

- [ ] **Step 5: Run gate and existing batch tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_batch_api_rejects_offline_bridge_without_creating_or_sampling `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_batch_api_rejects_unknown_or_mismatched_extension_without_side_effects `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_batch_api_rejects_expired_compatible_heartbeat `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_batch_api_creates_batch_and_events `
  tests.test_workbench_local_server.WorkbenchLocalServerTests.test_batch_post_starts_background_runner
```

Expected: `Ran 5 tests` followed by `OK`.

### Task 4: Show Version State And Disable Unsafe Batch Starts

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write the failing workbench HTML test**

Extend `test_home_page_loads` with:

```python
self.assertIn("扩展版本 (Extension Version)", body)
self.assertIn("版本状态 (Version Status)", body)
self.assertIn('id="bridgeVersion"', body)
self.assertIn('id="bridgeVersionStatus"', body)
self.assertIn('id="bridgeVersionWarning"', body)
self.assertIn('id="startBatch" class="primary" disabled', body)
self.assertIn('$("startBatch").disabled = !ready;', body)
self.assertIn("请在 Edge 扩展页面重新加载", body)
```

- [ ] **Step 2: Run the HTML test and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_workbench_local_server.WorkbenchLocalServerTests.test_home_page_loads
```

Expected: FAIL because the workbench does not yet render version state or disable the start button.

- [ ] **Step 3: Add compact version rows and a single warning**

Change the batch start button to:

```html
<button id="startBatch" class="primary" disabled>开始自动执行 (Run Until Blocked)</button>
```

Inside the existing browser bridge section, add:

```html
<div class="kv"><div class="key">扩展版本 (Extension Version)</div><div id="bridgeVersion" class="value">-</div></div>
<div class="kv"><div class="key">版本状态 (Version Status)</div><div id="bridgeVersionStatus" class="value">检查中 (Checking)</div></div>
<p id="bridgeVersionWarning" class="hint error" hidden></p>
```

Do not add a modal, card, updater button, or another polling loop.

- [ ] **Step 4: Drive the UI entirely from the status response**

At the end of `renderBridge(status)`, add:

```javascript
const loaded = data.loaded_extension_version || "未知 (Unknown)";
const required = data.required_extension_version || "未知 (Unknown)";
const ready = data.version_ready === true;
const labels = {
  ready: "已就绪 (Ready)",
  offline: "离线 (Offline)",
  version_mismatch: "需要更新 (Update Required)",
  version_unknown: "版本未知 (Version Unknown)",
};
$("bridgeVersion").textContent = `已加载 ${loaded} / 要求 ${required}`;
$("bridgeVersionStatus").textContent = labels[data.readiness_code] || "不可用 (Unavailable)";
$("bridgeVersionStatus").className = ready ? "value" : "value error";
$("startBatch").disabled = !ready;
$("bridgeVersionWarning").hidden = ready;
$("bridgeVersionWarning").textContent = ready
  ? ""
  : "浏览器扩展未就绪。请在 Edge 扩展页面重新加载，然后刷新工具台 (Reload the Edge extension, then refresh the workbench).";
```

Wrap `startBatch()` so that a readiness change between polling and clicking is shown in raw state instead of becoming an unhandled promise:

```javascript
async function startBatch() {
  try {
    const target = Number($("targetCount").value || 1);
    const result = await api("/api/batches", { method: "POST", body: JSON.stringify({ target_count: target }) });
    state.runId = result.data.run.run_id;
    localStorage.setItem("ozon_v2_workbench_run_id", state.runId);
    startLivePolling();
    await loadRun();
  } catch (error) {
    $("raw").textContent = JSON.stringify(error, null, 2);
    await loadBridgeStatus().catch(() => {});
  }
}
```

- [ ] **Step 5: Run the HTML test and local-server suite and verify GREEN**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_workbench_local_server.WorkbenchLocalServerTests.test_home_page_loads
python -m unittest tests.test_workbench_local_server -v
```

Expected: the targeted test passes; the local-server module finishes with `OK` and no failures.

### Task 5: Full Verification, Runtime Restart, And Canonical Log

**Files:**
- Verify: `tests/`
- Verify: `browser_extension/ozon_v2_bridge/`
- Modify: `E:\obsidian仓库\萧机麦仓库\Ozon V2 日志库 (Log Vault)\02-操作日志 (Operation Logs)\2026-07-10 操作日志 (Operation Log).md`

- [ ] **Step 1: Run the complete verification matrix**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
node tests/test_browser_extension_background.js
node tests/test_browser_seller_evidence.js
node --check browser_extension/ozon_v2_bridge/background.js
node --check browser_extension/ozon_v2_bridge/content.js
node --check browser_extension/ozon_v2_bridge/workbench_content.js
node --check browser_extension/ozon_v2_bridge/seller_evidence.js
```

Expected: the Python suite ends with `OK`; both JavaScript behavior tests print their `OK` messages; every syntax check exits with code `0`.

- [ ] **Step 2: Restart only the Ozon V2 workbench service**

Run:

```powershell
.\scripts\workbench_control.ps1 -Action Stop
.\scripts\workbench_control.ps1 -Action Start
.\scripts\workbench_control.ps1 -Action Status
```

Expected: `STOPPED`, then `STARTED`, then `RUNNING`, with the health URL `http://127.0.0.1:8765/api/health`. Do not stop or modify any old plugin process.

- [ ] **Step 3: Verify live readiness and the fail-closed response**

Run:

```powershell
$status = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/browser-bridge/status'
$status.data | Select-Object online,loaded_extension_version,required_extension_version,version_ready,readiness_code | Format-List
```

Expected after Edge has reloaded the project extension: `online=True`, loaded and required versions equal the manifest version, `version_ready=True`, and `readiness_code=ready`.

If Edge has not reloaded the extension, the expected safe state is `version_ready=False`; the UI remains disabled and `POST /api/batches` returns either `browser_bridge.offline` or `browser_bridge.update_required` without creating a run directory.

- [ ] **Step 4: Append the verified implementation record to the canonical E-drive Obsidian log**

Append this section only after Step 1 and Step 3 have produced evidence:

```markdown
## 扩展版本硬门禁实现 (Extension Version Gate Implementation)

- 版本来源 (Version Source): 服务端要求版本读取 `browser_extension/ozon_v2_bridge/manifest.json`；扩展心跳读取 `chrome.runtime.getManifest().version`，不再复制版本字符串。
- 状态接口 (Status API): 浏览器桥接状态返回已加载版本、要求版本、在线状态、`version_ready` 和明确的 `readiness_code`。
- 服务端门禁 (Server Gate): 桥接离线、版本未知或版本不一致时拒绝新建批次；拒绝过程不创建运行目录、不抽种子、不改变种子池。
- 失效页面保护 (Invalidated Page Protection): 旧页面的 `Extension context invalidated` 心跳不能覆盖 20 秒内兼容版本的后台心跳。
- 工具台反馈 (Workbench Feedback): 页面显示中英结合的版本状态；未就绪时禁用“开始自动执行”并显示 Edge 重新加载提示。
- 范围边界 (Scope Boundary): 未修改 Ozon 采集规则、海外店判断、1688、生图、上传或旧插件。
- 验证 (Verification): Python 全量测试、浏览器后台行为测试、海外店证据测试和四个扩展脚本语法检查全部通过。
```

- [ ] **Step 5: Confirm the workspace stayed bounded**

Run:

```powershell
rg -n "EXTENSION_VERSION = \"[0-9]" browser_extension/ozon_v2_bridge
rg -n "required_extension_version|version_ready|readiness_code|browser_bridge.update_required" src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
```

Expected: the first command returns no copied numeric extension constants; the second command returns matches only in the workbench server and its focused tests. No old plugin path appears in the changed-file list.
