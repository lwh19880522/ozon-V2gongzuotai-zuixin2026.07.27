# Ozon Browser Navigation And Collection Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every managed 1688 supplier channel in its original tab, restore collection controls after any navigation, and show structured Chinese Ozon collection progress and run-event presentation in the workbench.

**Architecture:** The supplier content script owns normal same-tab detail navigation and an idempotent panel reconciler; the background worker owns tab-history back/fallback navigation and explicit refresh messages. Ozon content heartbeats expose structured live counts, while `WorkbenchService` merges those counts with final result files and replacement/failure events. The home page renders the normalized progress and maps stable machine events to Chinese without changing stored events or APIs.

**Tech Stack:** Chrome/Edge Manifest V3 JavaScript, Node `vm` contract tests, Python 3, `unittest`/`pytest`, local HTML/CSS/JavaScript workbench.

---

## File map

- `browser_extension/ozon_v2_bridge/supplier_content.js`: same-tab detail click capture, idempotent floating panel, drag position, lifecycle reconciliation, back-button request.
- `browser_extension/ozon_v2_bridge/background.js`: refresh an already-loaded supplier script, immediate refresh after popup adoption, managed-tab back/fallback navigation.
- `browser_extension/ozon_v2_bridge/content.js`: emit structured Ozon collection progress on collection heartbeats.
- `browser_extension/ozon_v2_bridge/manifest.json`: bump the extension version after behavior changes.
- `src/ozon_v2/services/workbench_service.py`: normalize live/final Ozon progress and count replacements/final failures.
- `src/ozon_v2/workbench/local_server.py`: record replacement/final-failure events and render the Chinese progress card and Chinese event presentation.
- `tests/test_browser_extension_node_contracts.py`: make all extension Node contracts part of standard `pytest`.
- `tests/test_supplier_same_tab_navigation.js`: same-tab and modifier-key contract.
- `tests/test_supplier_panel_lifecycle.js`: panel restoration, single-instance, return and drag-position contract.
- `tests/test_browser_extension_managed_supplier_round.js`: immediate refresh/adoption and tab back/fallback contract.
- `tests/test_browser_collection_progress.js`: structured progress arithmetic and heartbeat payload contract.
- `tests/test_workbench_local_server.py`: service/API/UI progress and Chinese event presentation contracts.

---

### Task 1: Put Extension Contracts In Pytest And Fix Same-Tab Detail Navigation

**Files:**
- Create: `tests/test_browser_extension_node_contracts.py`
- Modify: `tests/test_supplier_same_tab_navigation.js`
- Modify: `tests/test_browser_extension_managed_supplier_round.js`
- Modify: `browser_extension/ozon_v2_bridge/supplier_content.js`
- Modify: `browser_extension/ozon_v2_bridge/background.js`

- [x] **Step 1: Add a failing pytest wrapper for every existing Node contract**

Create `tests/test_browser_extension_node_contracts.py`:

```python
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


NODE_CONTRACTS = (
    "test_browser_content_recovery.js",
    "test_browser_content_timeout_recovery.js",
    "test_browser_extension_background.js",
    "test_browser_extension_managed_supplier_round.js",
    "test_browser_extension_supplier.js",
    "test_browser_product_evidence.js",
    "test_browser_seller_evidence.js",
    "test_browser_workbench_content.js",
    "test_supplier_content_script.js",
    "test_supplier_same_tab_navigation.js",
    "test_supplier_selection_content_script.js",
    "test_supplier_selection_image_upload.js",
    "test_supplier_selection_without_sku_matrix.js",
)


@pytest.mark.parametrize("script_name", NODE_CONTRACTS)
def test_browser_extension_node_contract(script_name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser-extension contracts")
    script = Path(__file__).with_name(script_name)
    completed = subprocess.run(
        [node, str(script)],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
```

- [x] **Step 2: Run the wrapper and verify the existing production defect is RED**

Run:

```powershell
python -m pytest tests/test_browser_extension_node_contracts.py -q
```

Expected: only `test_supplier_same_tab_navigation.js` fails with `a managed 1688 page must capture detail-link clicks for same-tab navigation`; the other Node contracts pass.

- [x] **Step 3: Extend the Node contract for allowed and native browser clicks**

Add a helper to `tests/test_supplier_same_tab_navigation.js` and assert ordinary detail clicks are captured while explicit new-tab and non-detail clicks are untouched:

```javascript
function dispatchClick({ href, button = 0, ctrlKey = false, shiftKey = false }) {
  let prevented = false;
  let stopped = false;
  const anchor = {
    href,
    getAttribute(name) { return name === "href" ? href : null; },
    closest(selector) { return selector === "a[href]" ? this : null; },
  };
  clickCapture({
    target: { closest() { return anchor; } },
    button,
    ctrlKey,
    metaKey: false,
    shiftKey,
    altKey: false,
    defaultPrevented: false,
    preventDefault() { prevented = true; },
    stopImmediatePropagation() { stopped = true; },
  });
  return { prevented, stopped };
}

const normal = dispatchClick({ href: detailUrl });
assert.equal(assignedUrl, detailUrl);
assert.deepEqual(normal, { prevented: true, stopped: true });

assignedUrl = "";
assert.deepEqual(dispatchClick({ href: detailUrl, ctrlKey: true }), { prevented: false, stopped: false });
assert.equal(assignedUrl, "");
assert.deepEqual(dispatchClick({ href: detailUrl, button: 1 }), { prevented: false, stopped: false });
assert.deepEqual(dispatchClick({ href: "https://www.1688.com/" }), { prevented: false, stopped: false });
```

The content-script contract must also assert that Ctrl/Shift/Alt/Meta or middle-button pointer-down on a valid detail link sends `ozon_v2_supplier_native_new_tab_intent` with the target URL but never prevents the native event.

Extend `tests/test_browser_extension_managed_supplier_round.js` with a RED backend contract: send that intent from a currently bound opener tab, create one same-window child tab with its `openerTabId`, and assert `handleSupplierTabCreated()` returns `{ adopted: false, nativeIntent: true }`; the channel `tabId` and original tab remain unchanged. A child tab without a recorded intent continues through the existing popup compatibility adoption path.

- [x] **Step 4: Implement the smallest managed same-tab click capture**

Add these functions inside the existing supplier content-script closure:

```javascript
let managedNavigationInstalled = false;

function managedDetailUrl(event) {
  if (!event || event.defaultPrevented || event.button !== 0) return "";
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return "";
  const anchor = event.target && event.target.closest
    ? event.target.closest("a[href]")
    : null;
  if (!anchor) return "";
  try {
    const target = new URL(anchor.href || anchor.getAttribute("href"), location.href);
    if (!/(^|\.)1688\.com$/i.test(target.hostname)) return "";
    if (!/^\/offer\/\d+\.html$/i.test(target.pathname)) return "";
    return target.href;
  } catch (_) {
    return "";
  }
}

function installManagedSameTabNavigation() {
  if (managedNavigationInstalled) return;
  managedNavigationInstalled = true;
  document.addEventListener("pointerdown", (event) => {
    const detailUrl = detailUrlFromEvent(event);
    const explicitNewTab = event.button === 1
      || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey;
    if (detailUrl && explicitNewTab) {
      chrome.runtime.sendMessage({
        type: "ozon_v2_supplier_native_new_tab_intent",
        url: detailUrl,
      }).catch(() => null);
    }
  }, true);
  document.addEventListener("click", (event) => {
    const detailUrl = managedDetailUrl(event);
    if (!detailUrl) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    location.assign(detailUrl);
  }, true);
}
```

Factor anchor/host/path validation into `detailUrlFromEvent(event)` so both handlers use the same allowlist. Call `installManagedSameTabNavigation()` only after `initializeManagedChannel()` has obtained a real binding. Do not install it on unrelated 1688 tabs.

In `background.js`, keep a short-lived in-memory native-intent map keyed by the verified bound sender tab ID. The runtime message handler must reject unbound senders. Before popup adoption, `handleSupplierTabCreated()` waits only a small bounded turn, consumes at most one unexpired intent for its `openerTabId`, and skips adoption when it matches. Do not persist the intent and do not close or rebind either tab on the skip path. This preserves explicit native new-tab behavior while leaving the existing no-intent popup fallback available.

- [x] **Step 5: Run the Node gate and supplier-focused Python contracts**

Run:

```powershell
python -m pytest tests/test_browser_extension_node_contracts.py tests/test_workbench_local_server.py -q -k "node_contract or managed_supplier or extension_manifest_registers_1688"
```

Expected: all selected tests pass; no browser starts.

- [x] **Step 6: Commit Task 1**

```powershell
git add tests/test_browser_extension_node_contracts.py tests/test_supplier_same_tab_navigation.js tests/test_browser_extension_managed_supplier_round.js browser_extension/ozon_v2_bridge/supplier_content.js browser_extension/ozon_v2_bridge/background.js
git commit -m "fix: keep managed supplier navigation in one tab"
```

---

### Task 2: Reconcile The Supplier Panel And Add Managed Back Navigation

**Files:**
- Create: `tests/test_supplier_panel_lifecycle.js`
- Modify: `tests/test_browser_extension_node_contracts.py`
- Modify: `tests/test_browser_extension_managed_supplier_round.js`
- Modify: `browser_extension/ozon_v2_bridge/supplier_content.js`
- Modify: `browser_extension/ozon_v2_bridge/background.js`
- Modify: `browser_extension/ozon_v2_bridge/manifest.json`

- [x] **Step 1: Add a RED panel lifecycle Node test**

Create `tests/test_supplier_panel_lifecycle.js` with a small DOM/runtime mock. The assertions must be concrete:

```javascript
assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1);

document.getElementById("ozon-v2-supplier-panel").remove();
listeners.popstate();
await tick();
assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "popstate must restore one panel");

document.getElementById("ozon-v2-supplier-panel").remove();
listeners.pageshow({ persisted: true });
await tick();
assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "pageshow must restore one panel");

replaceDocumentBody();
mutationCallback();
await tick();
assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "whole-body replacement must restore one panel");

await sendRuntimeMessage({ type: "ozon_v2_supplier_channel_refresh" });
assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "refresh must remain idempotent");

const backButton = document.getElementById("ozon-v2-supplier-back");
await backButton.onclick();
assert.ok(runtimeMessages.some((item) => item.type === "ozon_v2_supplier_channel_back"));
```

Mock `MutationObserver` and assert removal schedules one reconciliation, not multiple panels. Mock `chrome.storage.local` and assert a stored `{ left, top }` position is clamped inside the viewport before application.

Seed `sessionStorage` with the current channel's `ozon_v2_reference_prepared_<run_id>_<seed_id>` marker, click the managed back button, and assert the marker is cleared before `ozon_v2_supplier_channel_back` is sent. This guarantees that a history fallback to the 1688 home page prepares the assigned reference image again instead of trusting stale page-local state.

Then make the mocked location an 1688 home URL, fire `popstate`/refresh in the same document, and assert `prepareReferenceImage(binding)` is invoked exactly once through reconciliation. This covers BFCache/history return where the content script is not reinitialized.

Make the runtime mock return `{ ok: false }` for one back attempt and assert the panel remains bound while its status becomes `返回失败，请重试`. A failed back must not remove the panel or clear the channel binding.

Add `"test_supplier_panel_lifecycle.js"` to `NODE_CONTRACTS`.

- [x] **Step 2: Add RED background assertions for refresh and back/fallback**

Extend `tests/test_browser_extension_managed_supplier_round.js`:

```javascript
const sentMessages = [];
const backCalls = [];

chrome.tabs.sendMessage = async (tabId, message) => {
  sentMessages.push({ tabId, message });
  return message.type === "ozon_v2_content_ping"
    ? { ok: true, bridge: "supplier", panel_present: false }
    : { ok: true };
};
chrome.tabs.goBack = async (tabId) => { backCalls.push(tabId); };

await context.handleSupplierTabCreated(detailTab);
assert.ok(sentMessages.some((item) => (
  item.tabId === detailTab.id && item.message.type === "ozon_v2_supplier_channel_refresh"
)));

const back = await sendMessage({ type: "ozon_v2_supplier_channel_back" }, detailTab);
assert.equal(back.ok, true);
assert.deepEqual(backCalls, [detailTab.id]);
```

Add a second channel where `goBack` throws, then assert `chrome.tabs.update(tabId, {url: "https://www.1688.com/"})` is used on the same tab and no tab is created or removed.

Add an orphan `detail.1688.com` tab in the managed window without `openerTabId`. Call `handleSupplierTabCreated()` and assert no channel `tabId` changes and no bound tab is removed. The background worker must never guess a channel from tab order or window position.

Ask `ozon_v2_get_supplier_channel` from that orphan sender and assert the response has no binding but includes only structured `tab_id`, `window_id`, `opener_tab_id` and `url` diagnostics. Also call `handleSupplierTabUpdated()` with `changeInfo.url` for a bound tab and assert a supplier refresh is sent after the navigation lifecycle event.

- [x] **Step 3: Run the lifecycle tests and confirm RED**

Run:

```powershell
node tests/test_supplier_panel_lifecycle.js
node tests/test_browser_extension_managed_supplier_round.js
```

Expected: missing reconciliation/refresh/back behavior causes failures.

- [x] **Step 4: Replace one-shot panel creation with an idempotent reconciler**

Keep the current panel styling but split creation from state refresh:

```javascript
let activeManagedBinding = null;
let reconcileTimer = null;

function scheduleManagedPanelReconcile(delayMs = 50) {
  clearTimeout(reconcileTimer);
  reconcileTimer = setTimeout(() => {
    if (activeManagedBinding) reconcileManagedPanel(activeManagedBinding);
  }, delayMs);
}

async function reconcileManagedPanel(binding = activeManagedBinding) {
  const resolved = binding || await currentSupplierChannelBinding();
  if (!resolved) return null;
  activeManagedBinding = resolved;
  let panel = document.getElementById("ozon-v2-supplier-panel");
  if (!panel) panel = createManagedPanel(resolved);
  panel.dataset.channelIndex = String(resolved.channel_index);
  panel.querySelector("[data-role='channel-title']").textContent =
    `通道 ${Number(resolved.channel_index) + 1} · ${resolved.ozon_title || resolved.seed_id}`;
  const detailReady = is1688DetailPage();
  panel.querySelector("#ozon-v2-collect-current-product").disabled = !detailReady;
  panel.querySelector("#ozon-v2-supplier-status").textContent = detailReady
    ? "当前商品可以采集"
    : "请选择同款商品并进入详情页";
  restoreManagedPanelPosition(panel);
  if (is1688HomePage()) await prepareReferenceImageOnce(resolved);
  return panel;
}
```

`currentSupplierChannelBinding()` returns the binding portion of the structured lookup response. `prepareReferenceImageOnce()` reuses one in-flight promise per binding so pageshow, mutation and refresh cannot upload the reference twice concurrently; the existing session marker remains the completed-state guard.

`createManagedPanel()` must add `id="ozon-v2-supplier-back"`, preserve the existing collect/reject handlers, add pointer drag handlers, and save `{left, top}` under `supplierPanelPosition` in `chrome.storage.local`. `restoreManagedPanelPosition()` clamps left/top to `0..innerWidth-panel.offsetWidth` and `0..innerHeight-panel.offsetHeight`. The async back handler must clear the current channel's reference-prepared session marker before messaging the background worker, so a home fallback re-runs the existing `prepareReferenceImage(binding)` path. If the response is not successful, keep the panel/binding and set the status text to `返回失败，请重试`.

- [x] **Step 5: Install bounded lifecycle recovery**

After binding is obtained:

```javascript
reconcileManagedPanel(binding);
installManagedSameTabNavigation();
addEventListener("pageshow", () => scheduleManagedPanelReconcile());
addEventListener("popstate", () => scheduleManagedPanelReconcile());

const observer = new MutationObserver(() => {
  if (!document.getElementById("ozon-v2-supplier-panel")) {
    scheduleManagedPanelReconcile(100);
  }
});
observer.observe(document.documentElement, { childList: true, subtree: true });
```

Observing `document.documentElement` keeps the observer alive when an SPA replaces the entire body. Wrap `history.pushState` and `history.replaceState` once and dispatch a private URL-change event; that event also schedules reconciliation. Extend the supplier runtime listener:

```javascript
if (message.type === "ozon_v2_supplier_channel_refresh") {
  scheduleManagedPanelReconcile(0);
  sendResponse({ ok: true, panel_present: !!document.getElementById("ozon-v2-supplier-panel") });
  return false;
}
```

The `ozon_v2_content_ping` response must include `binding_present` and `panel_present`.

- [x] **Step 6: Refresh loaded scripts and implement managed back in background**

Change `ensureContentScript()` so a successful supplier ping sends refresh before returning:

```javascript
const response = await chrome.tabs.sendMessage(tab.id, { type: "ozon_v2_content_ping" });
if (response && response.ok) {
  if (isManagedSupplierTask(task)) {
    await chrome.tabs.sendMessage(tab.id, { type: "ozon_v2_supplier_channel_refresh" });
  }
  return true;
}
```

Factor the existing inline task reconstruction from `handleSupplierTabUpdated()` into `managedSupplierTaskForChannel(channel)`. At the end of successful `handleSupplierTabCreated()`, call `chrome.tabs.get(tab.id)` to obtain the current adopted tab, build `const task = managedSupplierTaskForChannel(match.channel)`, and call `ensureContentScript(adoptedTab, task)` immediately. Let `handleSupplierTabUpdated()` react to either `changeInfo.url` or `status === "complete"` and use the same helper, so no undefined `task` variable or stale pre-navigation tab object is possible.

Extend the existing supplier-channel lookup response to return safe sender diagnostics when no binding exists:

```javascript
{
  ok: true,
  binding: null,
  diagnostics: {
    tab_id: sender.tab.id,
    window_id: sender.tab.windowId,
    opener_tab_id: sender.tab.openerTabId || null,
    url: sender.tab.url || "",
  },
}
```

`initializeManagedChannel()` must retain the last diagnostics returned during its bounded binding wait and include them in the existing `supplier_channel_binding_missing` heartbeat. Do not include page text, cookies, query contents or any other sensitive data.

Add the message handler:

```javascript
if (message.type === "ozon_v2_supplier_channel_back") {
  const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
  supplierChannelForTab(tabId)
    .then(async (binding) => {
      if (!binding) return { ok: false, code: "supplier_selection.channel_missing" };
      try {
        await chrome.tabs.goBack(tabId);
        return { ok: true, mode: "history" };
      } catch (_) {
        await chrome.tabs.update(tabId, { url: "https://www.1688.com/", active: true });
        return { ok: true, mode: "home" };
      }
    })
    .then((result) => sendResponse(result))
    .catch((error) => sendResponse({ ok: false, error: String(error) }));
  return true;
}
```

For a content script with no binding, preserve the existing `supplier_channel_binding_missing` heartbeat and attach the safe sender diagnostics above. For an orphan tab with no opener, the background worker must leave all channel bindings unchanged; never infer a channel from tab order or window position. After `goBack` or the home fallback causes a URL/load update, `handleSupplierTabUpdated()` performs the required content-script ensure/refresh on the same channel tab.

- [x] **Step 7: Bump and verify the extension contract**

Change `manifest.json` version from `0.1.51` to `0.1.52`.

Run:

```powershell
python -m pytest tests/test_browser_extension_node_contracts.py tests/test_workbench_local_server.py -q -k "node_contract or browser_bridge or extension"
```

Expected: all selected tests pass.

- [x] **Step 8: Commit Task 2**

```powershell
git add browser_extension/ozon_v2_bridge/background.js browser_extension/ozon_v2_bridge/supplier_content.js browser_extension/ozon_v2_bridge/manifest.json tests/test_browser_extension_node_contracts.py tests/test_supplier_panel_lifecycle.js tests/test_browser_extension_managed_supplier_round.js
git commit -m "fix: restore supplier controls across navigation"
```

---

### Task 3: Emit And Normalize Structured Ozon Collection Progress

**Files:**
- Create: `tests/test_browser_collection_progress.js`
- Modify: `tests/test_browser_extension_node_contracts.py`
- Modify: `browser_extension/ozon_v2_bridge/content.js`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `tests/test_workbench_local_server.py`

- [x] **Step 1: Add RED Node arithmetic and heartbeat contracts**

Create `tests/test_browser_collection_progress.js`. Load `content.js` in the existing VM style and assert the exposed top-level helper:

```javascript
assert.deepEqual(
  JSON.parse(JSON.stringify(context.collectionProgress({ candidates: [{}, {}] }, 5))),
  {
    total_count: 5,
    processed_count: 2,
    success_count: 2,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 3,
  },
);
assert.deepEqual(
  JSON.parse(JSON.stringify(context.collectionProgress(
    { candidates: [{}, {}, {}, {}, {}, {}] },
    3,
  ))),
  {
    total_count: 3,
    processed_count: 3,
    success_count: 3,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 0,
  },
);
```

The browser owns only live total/success counts, so the helper must always emit live `failure_count: 0` and `replacement_count: 0`; server-side outcome events supply those two fields later.

Use the real VM entry points, not manually invented state fields, for four heartbeat contracts:

1. run a reusable-snapshot path through `runOzonCollection()` and inspect `snapshot_reused`;
2. run a normal accepted-detail path through `runOzonCollection()` and inspect the next-seed or submit heartbeat;
3. call the real `requireChineseCrossBorderDetail()` exhaustion path for `task_type: "ozon_collection"` with `totalCount`, inspecting both `candidate_rejected` and `no_cross_border_candidate`;
4. run the real `submitOzonCollection()` path and inspect both `submitting` and `submitted`.

Every captured heartbeat must contain the same six non-negative fields, with processed clamped to total. The attribute-template form of `requireChineseCrossBorderDetail()` must not receive collection progress. Add the script to `NODE_CONTRACTS`.

- [x] **Step 2: Add RED Python service progress tests**

In `tests/test_workbench_local_server.py`, create a target-5 batch, save a matching live bridge status, append replacement/final-failure events, and assert the batch API returns normalized progress:

```python
self.repo.save_browser_bridge_status(
    {
        "source": "ozon_content_script",
        "run_id": run_id,
        "task_type": "ozon_collection",
        "details": {
            "collection_progress": {
                "total_count": 5,
                "processed_count": 2,
                "success_count": 2,
                "failure_count": 0,
                "replacement_count": 0,
                "pending_count": 3,
            }
        },
    }
)
self.repo.append_run_event(run_id, "browser_candidate.replaced", "replaced", {"seed_id": "old-1"})
result = self.get_json(f"/api/batches/{run_id}")
progress = result["data"]["progress"]["ozon_collection_progress"]
self.assertEqual(
    {
        "total_count": 5,
        "processed_count": 2,
        "success_count": 2,
        "failure_count": 0,
        "replacement_count": 1,
        "pending_count": 3,
    },
    progress,
)
```

Add separate tests proving a different `run_id` heartbeat is ignored, a same-run non-`ozon_collection` heartbeat is ignored, final `ozon_collection_result.json` overrides live success, duplicate replacement events for one seed count once, a replaced seed wins over any stale failed event for the same seed, and `processed_count` is clamped to total.

Append an attribute-template exhaustion/replacement event for the same batch and assert it does not change `ozon_collection_progress`. Outcome aggregation is scoped by both `seed_id` and `task_type`.

Extend the existing exhausted-seed heartbeat test with two route-level RED cases:

- post the same stale `.no_cross_border_candidate` heartbeat twice after the first replacement succeeds; assert exactly one `browser_candidate.replaced`, no `browser_candidate.failed`, and no second runner resume;
- construct a batch with no eligible replacement and assert exactly one `browser_candidate.failed` whose source result code is `workbench.exhausted_seed_no_replacement`.

Failures such as `workbench.exhausted_seed_invalid_state` and `workbench.exhausted_seed_not_sampled` are stale/technical heartbeats, not final product failures.

- [x] **Step 3: Run both new contracts and verify RED**

Run:

```powershell
node tests/test_browser_collection_progress.js
python -m pytest tests/test_workbench_local_server.py -q -k "ozon_collection_progress"
```

Expected: missing helper and missing `ozon_collection_progress` failures.

- [x] **Step 4: Implement the pure browser progress helper and attach it to Ozon heartbeats**

Add to `content.js`:

```javascript
function collectionProgress(state, totalCount) {
  const total = Math.max(0, Number.parseInt(totalCount, 10) || 0);
  const success = Math.max(0, Array.isArray(state.candidates) ? state.candidates.length : 0);
  const processed = Math.min(total, success);
  return {
    total_count: total,
    processed_count: processed,
    success_count: Math.min(success, total),
    failure_count: 0,
    replacement_count: 0,
    pending_count: Math.max(total - processed, 0),
  };
}

function collectionProgressDetails(state, totalCount, details = {}) {
  return {
    ...details,
    collection_progress: collectionProgress(state, totalCount),
  };
}
```

Pass `seeds.length` from `runOzonCollection()` into every Ozon `running` and `snapshot_reused` heartbeat. Add an optional final `totalCount = null` parameter to `requireChineseCrossBorderDetail()`; every Ozon call passes `seeds.length`, while attribute-template calls keep it null. The helper wraps `candidate_rejected` and `no_cross_border_candidate` details only when `taskType === "ozon_collection"` and `totalCount !== null`.

Change `submitOzonCollection(runId, ingestUrl, state, totalCount)` to send `state.candidates` and attach progress to both final heartbeats; update both Ozon call sites to pass the real state and `seeds.length`. Use `collectionProgressDetails()` on Ozon `running`, `snapshot_reused`, `candidate_rejected`, `no_cross_border_candidate`, `submitting` and `submitted`. Do not attach collection progress to supplier or attribute-template heartbeats.

- [x] **Step 5: Record replacement/final-failure outcomes as structured events**

In the `.no_cross_border_candidate` heartbeat route, after `replace_exhausted_attribute_template_seed()`:

```python
task_type = str(payload.get("task_type") or "")
if replacement.ok:
    selected_repo.append_run_event(
        run_id,
        "browser_candidate.replaced",
        "The exhausted Ozon seed was replaced.",
        {
            "seed_id": rejected_seed_id,
            "task_type": task_type,
            "replacement": replacement.data,
        },
    )
elif (
    task_type == "ozon_collection"
    and replacement.code == "workbench.exhausted_seed_no_replacement"
):
    selected_repo.append_run_event(
        run_id,
        "browser_candidate.failed",
        "The exhausted Ozon seed could not be replaced.",
        {
            "seed_id": rejected_seed_id,
            "task_type": task_type,
            "result_code": replacement.code,
        },
    )
```

Before calling replacement, find any existing `browser_candidate.replaced` or `browser_candidate.failed` event for the same direct `data.seed_id` and `data.task_type`. If one exists, treat the incoming heartbeat as stale/idempotent: keep the diagnostic heartbeat response but do not replace again, append an opposite outcome, or resume the runner. Append at most one outcome event per seed and task type.

Keep the existing `browser_candidate.exhausted` event for diagnostics. Never turn candidate-level rejection into seed failure. Only the exact `workbench.exhausted_seed_no_replacement` result is a final failure; invalid state or a no-longer-sampled seed remains diagnostic and does not affect product failure counts.

- [x] **Step 6: Normalize progress in WorkbenchService**

Add `_ozon_collection_progress(run, seeds)` and call it from `_run_progress()`:

```python
def _ozon_collection_progress(self, run: dict, seeds: list) -> dict[str, int]:
    total = len(seeds)
    bridge = self.repo.load_browser_bridge_status()
    live = {}
    if (
        str(bridge.get("run_id") or "") == str(run["run_id"])
        and bridge.get("task_type") == "ozon_collection"
    ):
        details = bridge.get("details") if isinstance(bridge.get("details"), dict) else {}
        candidate = details.get("collection_progress")
        if isinstance(candidate, dict):
            live = candidate

    events = self.repo.load_run_events(run["run_id"])
    replaced = {
        str(event.data.get("seed_id") or "")
        for event in events
        if (
            event.event_type == "browser_candidate.replaced"
            and event.data.get("task_type") == "ozon_collection"
        )
    } - {""}
    failed = ({
        str(event.data.get("seed_id") or "")
        for event in events
        if (
            event.event_type == "browser_candidate.failed"
            and event.data.get("task_type") == "ozon_collection"
        )
    } - {""}) - replaced

    final_path = self.repo.run_dir(run["run_id"]) / "ozon_collection_result.json"
    if final_path.exists():
        success = len(self.repo.load_ozon_collection_result(run["run_id"]).get("ozon_candidates", []))
    else:
        success = max(0, int(live.get("success_count", 0) or 0))
    failure = min(len(failed), max(total - success, 0))
    processed = min(total, success + failure)
    return {
        "total_count": total,
        "processed_count": processed,
        "success_count": min(success, total),
        "failure_count": failure,
        "replacement_count": len(replaced),
        "pending_count": max(total - processed, 0),
    }
```

`_run_progress()` must expose this under `ozon_collection_progress`. Keep existing seed/query fields unchanged.

- [x] **Step 7: Run the focused progress regression**

Run:

```powershell
python -m pytest tests/test_browser_extension_node_contracts.py tests/test_workbench_local_server.py -q -k "node_contract or ozon_collection_progress or exhausted_ozon"
```

Expected: all selected tests pass.

- [x] **Step 8: Commit Task 3**

```powershell
git add browser_extension/ozon_v2_bridge/content.js src/ozon_v2/services/workbench_service.py src/ozon_v2/workbench/local_server.py tests/test_browser_collection_progress.js tests/test_browser_extension_node_contracts.py tests/test_workbench_local_server.py
git commit -m "feat: expose structured Ozon collection progress"
```

---

### Task 4: Render The Chinese Progress Card And Chinese Run Events

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Add RED home-page structure and presentation contracts**

Extend `test_home_page_loads()`:

```python
self.assertIn('id="ozonCollectionProgress"', body)
self.assertIn('id="ozonProgressBar"', body)
self.assertIn('role="progressbar"', body)
self.assertIn('aria-valuemin="0"', body)
self.assertIn('id="ozonProcessed"', body)
self.assertIn('id="ozonSucceeded"', body)
self.assertIn('id="ozonFailed"', body)
self.assertIn('id="ozonReplaced"', body)
self.assertIn('id="ozonPending"', body)
self.assertIn("等待采集数据", body)
self.assertIn("function eventPresentation(event)", body)
self.assertIn("采集已入库", body)
self.assertIn("后台执行器已启动", body)
self.assertIn("系统事件（查看原始信息）", body)
self.assertIn("event.event_type", body)
self.assertIn("textContent", body)
```

Add a test proving `GET /api/batches/{run_id}/events` still returns the original English machine event unchanged after the UI mapping is added.

- [ ] **Step 2: Run the home-page test and verify RED**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py::WorkbenchLocalServerTests::test_home_page_loads -q
```

Expected: missing progress IDs and `eventPresentation` assertions fail.

- [ ] **Step 3: Add the progress card above Run Events**

Add compact CSS and this semantic structure before the event section:

```html
<section id="ozonCollectionProgress" class="collection-progress-card" aria-label="Ozon 采集进度">
  <div class="section-head">
    <div><h2>Ozon 采集进度</h2><p id="ozonProcessed" class="hint">已处理 0 / 0</p></div>
  </div>
  <div id="ozonProgressBar" class="collection-progress-track" role="progressbar"
       aria-valuemin="0" aria-valuemax="0" aria-valuenow="0">
    <span id="ozonProgressSuccess" class="collection-progress-success"></span>
    <span id="ozonProgressFailure" class="collection-progress-failure"></span>
  </div>
  <div class="collection-progress-metrics">
    <span>成功 <strong id="ozonSucceeded">0</strong></span>
    <span>最终失败 <strong id="ozonFailed">0</strong></span>
    <span>已替换 <strong id="ozonReplaced">0</strong></span>
    <span>待处理 <strong id="ozonPending">0</strong></span>
  </div>
</section>
```

Render only normalized numbers from `data.progress.ozon_collection_progress`:

```javascript
function renderOzonCollectionProgress(progress) {
  const hasProgress = !!progress;
  const value = progress || {};
  const total = Math.max(0, Number(value.total_count) || 0);
  const processed = Math.min(total, Math.max(0, Number(value.processed_count) || 0));
  const success = Math.min(total, Math.max(0, Number(value.success_count) || 0));
  const failure = Math.min(total - success, Math.max(0, Number(value.failure_count) || 0));
  $("ozonProcessed").textContent = hasProgress
    ? `已处理 ${processed} / ${total}`
    : "等待采集数据";
  $("ozonSucceeded").textContent = String(success);
  $("ozonFailed").textContent = String(failure);
  $("ozonReplaced").textContent = String(Math.max(0, Number(value.replacement_count) || 0));
  $("ozonPending").textContent = String(Math.max(total - processed, 0));
  const bar = $("ozonProgressBar");
  bar.setAttribute("aria-valuemax", String(total));
  bar.setAttribute("aria-valuenow", String(processed));
  $("ozonProgressSuccess").style.width = `${total ? success / total * 100 : 0}%`;
  $("ozonProgressFailure").style.width = `${total ? failure / total * 100 : 0}%`;
}
```

Call it from `render()` after existing seed/query progress.

- [ ] **Step 4: Present known events in Chinese without mutating the event API**

Add a stable mapping and a safe fallback:

```javascript
const EVENT_PRESENTATIONS = {
  "ozon_collection.ingested": ["采集已入库", "Ozon 采集结果已接收，采集门禁已完成。"],
  "runner.started": ["后台执行器已启动", "后台执行器已从工作台启动。"],
  "runner.stopped": ["后台执行器已停止", "当前后台任务已停止。"],
  "runner.blocked": ["后台执行器等待处理", "后台执行器已到达需要处理的门禁。"],
  "autopilot.started": ["自动运行已启动", "系统将自动运行到下一个阻塞门禁。"],
  "autopilot.blocked": ["自动运行等待处理", "自动运行已停在需要用户处理的门禁。"],
  "supplier_selection.product_captured": ["供应商商品已采集", "一个用户确认的 1688 商品已回传工作台。"],
  "browser_task.cancelled": ["浏览器任务已取消", "当前浏览器任务已由用户停止。"],
  "browser_candidate.rejected": ["候选商品未通过", "当前候选不符合采集要求，正在检查其他结果。"],
  "browser_candidate.exhausted": ["当前种子已耗尽", "当前种子没有找到合格候选，正在尝试补位。"],
  "browser_candidate.replaced": ["种子已替换", "失败种子已完成补位，批次继续运行。"],
  "browser_candidate.failed": ["种子最终失败", "失败种子无法补位，需要人工处理。"],
};

function eventPresentation(event) {
  const known = EVENT_PRESENTATIONS[event.event_type];
  return known
    ? { title: known[0], message: known[1], raw: event.message || "" }
    : {
        title: "系统事件（查看原始信息）",
        message: "该事件尚无中文模板，原始信息保留用于诊断。",
        raw: event.message || "",
      };
}
```

Replace row `innerHTML` with explicit elements and `textContent`:

```javascript
function textCell(row, value, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value || "";
  row.appendChild(cell);
  return cell;
}

events.forEach((event) => {
  const row = document.createElement("tr");
  textCell(row, event.created_at || "", "event-time");
  const typeCell = textCell(row, eventPresentation(event).title, "event-type");
  const rawType = document.createElement("small");
  rawType.textContent = event.event_type || "";
  typeCell.appendChild(rawType);
  const presentation = eventPresentation(event);
  const messageCell = textCell(row, presentation.message, "event-message");
  if (presentation.raw) {
    const raw = document.createElement("small");
    raw.textContent = presentation.raw;
    messageCell.appendChild(raw);
  }
  $("events").appendChild(row);
});
```

- [ ] **Step 5: Verify generated JavaScript and focused workbench tests**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -q
$env:PYTHONPATH='src'
@'
import re
from ozon_v2.workbench.local_server import build_home_html
html = build_home_html()
print('\n'.join(re.findall(r'<script>(.*?)</script>', html, flags=re.S)))
'@ | python - | node --check -
```

Expected: all workbench tests pass and Node syntax check exits 0.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
git commit -m "feat: show Chinese Ozon collection progress"
```

---

### Task 5: Integrated Regression, Review And Project Record

**Files:**
- Modify only if verification finds a defect: files already named above
- Create outside repository: `E:\obsidian仓库\萧机麦仓库\Ozon 工作台项目库 (Workbench Project)\2026-07-20 Ozon 浏览器通道与中文采集进度.md`

- [ ] **Step 1: Run every browser-extension Node contract through pytest**

```powershell
python -m pytest tests/test_browser_extension_node_contracts.py -q
```

Expected: every Node contract passes, including same-tab navigation, panel lifecycle and collection progress.

- [ ] **Step 2: Run focused browser/workbench regression**

```powershell
python -m pytest tests/test_workbench_local_server.py tests/test_supplier_browser_worker.py tests/test_browser_extension_node_contracts.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run the complete repository test suite**

```powershell
python -m pytest -q
```

Expected: zero failures. The test count increases from the 296-test baseline because Node contracts are now part of pytest.

- [ ] **Step 4: Run static checks**

```powershell
git diff --check
node --check browser_extension/ozon_v2_bridge/background.js
node --check browser_extension/ozon_v2_bridge/supplier_content.js
node --check browser_extension/ozon_v2_bridge/content.js
```

Expected: all commands exit 0.

- [ ] **Step 5: Request independent code review**

Review the complete feature range from `0d379e2` to `HEAD`. The reviewer must check channel isolation, no-opener safety, panel singleton recovery, back fallback, progress arithmetic, run-id isolation, original event API preservation, DOM text safety, and absence of real collection/upload side effects. Fix every Critical or Important issue and repeat Steps 1–4.

- [ ] **Step 6: Write and verify the Obsidian record**

Use Chinese first with English in parentheses. Record:

- the same-tab navigation and back behavior;
- automatic panel restoration paths;
- success/final-failure/replacement/pending definitions;
- Chinese event presentation with original machine codes preserved;
- exact test counts from Steps 1–3;
- confirmation that validation did not trigger real collection, image generation, upload or final approval.

Create the note with `apply_patch`, then read the exact file back from the E-drive vault.

- [ ] **Step 7: Confirm final branch state**

```powershell
git status --short
git log --oneline -6
```

Expected: the feature worktree is clean and all implementation commits are visible on `codex/ozon-browser-progress-ui`.

Do not merge, push or delete the worktree without a separate user choice.
