# Browser Exhaustion Recovery And Diagnostics ZIP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an exhausted Ozon candidate list from freezing a batch, and let users export a sanitized diagnostic ZIP for the current batch.

**Architecture:** `WorkbenchService` owns seed replacement and contract invalidation. The heartbeat route only translates browser exhaustion into that application action and restarts the existing runner. The browser content script keys its session state by dispatch token so a rebuilt contract resets the same tab without opening duplicates. A focused diagnostics exporter builds an in-memory ZIP from allow-listed runtime data and recursively redacts secrets before the HTTP handler streams it.

**Tech Stack:** Python 3.11, standard-library `zipfile`/`io`, existing `ThreadingHTTPServer`, Edge Manifest V3 JavaScript, unittest/pytest, Node assert tests.

---

### Task 1: Exhausted Seed Recovery

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `browser_extension/ozon_v2_bridge/content.js`
- Modify: `browser_extension/ozon_v2_bridge/manifest.json`
- Test: `tests/test_workbench_skeleton.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] Add a failing service test asserting that an exhausted sampled seed is recorded, replaced without changing active-pool size, and returns the workbench to `seed_selected` with the old contract invalidated.
- [ ] Implement `replace_exhausted_attribute_template_seed(run_id, seed_id, reason)` using existing dedupe policy and repository APIs.
- [ ] Add a failing heartbeat test with `details.seed_id`, then route `*.no_cross_border_candidate` to replacement and restart the same runner only after replacement succeeds.
- [ ] Include `seed_id` in candidate rejection/exhaustion heartbeats and bind session state to `dispatch_token` so a rebuilt contract resets the same browser tab.
- [ ] Bump the extension version once and run Python plus Node regression tests.

### Task 2: Current Batch Diagnostic ZIP

**Files:**
- Create: `src/ozon_v2/services/diagnostics_export_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_diagnostics_export.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] Add a failing exporter test requiring `summary.json`, `run.json`, `events.json`, `runner.json`, `browser_bridge.json`, and `errors.json` in the ZIP.
- [ ] Add secret fixtures containing API keys, cookies, authorization headers, and proxy passwords; assert that neither secret keys nor values occur in extracted files.
- [ ] Implement an allow-listed in-memory ZIP exporter with recursive redaction and no product images or credential files.
- [ ] Add `GET /api/batches/{run_id}/diagnostics.zip` with attachment headers and a stable filename.
- [ ] Add `导出诊断包 (Export Diagnostics)` to the existing diagnostics drawer; disable it when no batch is selected.
- [ ] Download the ZIP from the live server, inspect its entries, scan it for the configured API key, and verify service health.

### Task 3: Runtime And Documentation Verification

**Files:**
- Create: `E:/obsidian仓库/萧机麦仓库/Ozon 工作台项目库 (Workbench Project)/05-操作日志 (Operation Logs)/2026-07-13 候选耗尽恢复与诊断包 (Exhaustion Recovery And Diagnostics).md`

- [ ] Restart the `8765` service with the workspace `src` directory on `PYTHONPATH`.
- [ ] Resume `wb-b0b814ecfe2a` and verify that seed replacement changes the browser dispatch token instead of opening duplicate tabs.
- [ ] Verify that the current run progresses beyond the exhausted `seed-0491` or records a bounded next blocker with explicit evidence.
- [ ] Record changed files, test results, live batch state, extension version, and diagnostic ZIP contents in the E-drive Obsidian project log.
