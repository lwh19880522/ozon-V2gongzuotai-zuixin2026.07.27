# 1688 Managed Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dedicated multi-channel 1688 selection window where users confirm exact products and trigger collection directly from the detail page.

**Architecture:** Add a `supplier_selection` browser task while the batch is in `supplier_review`. The extension owns one dedicated Edge window with one bound tab per item, while the backend stores per-item captures and reuses the existing supplier collection validator to finalize the batch.

**Tech Stack:** Python standard-library local server, filesystem repository, Manifest V3 Edge extension, vanilla JavaScript, unittest and Node VM tests.

---

### Task 1: Supplier selection task contract

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] Add a failing test asserting `supplier_review` returns `browser_task.supplier_selection_ready` with one channel per review item.
- [ ] Run `python -m unittest tests.test_workbench_local_server.WorkbenchLocalServerTests.test_browser_task_endpoint_returns_supplier_selection_channels -v` and verify it fails on the missing task code.
- [ ] Build the contract from persisted review and Ozon evidence, including `channel_index`, `seed_id`, `ozon_product_id`, title and reference image.
- [ ] Re-run the targeted test and verify it passes.

### Task 2: Per-channel capture and finalization

**Files:**
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] Add failing tests for one-channel persistence, wrong-channel rejection and automatic finalization after the final channel.
- [ ] Run the three targeted tests and verify failures are caused by the missing capture API.
- [ ] Persist a draft keyed by `seed_id`; validate the current review state, detail URL and channel binding.
- [ ] On the final capture, save verified links, call the existing supplier collection start contract, and pass the accumulated products through the existing complete-result validator.
- [ ] Run the targeted tests and verify they pass.

### Task 3: Dedicated Edge window and channel bindings

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/manifest.json`
- Modify: `browser_extension/ozon_v2_bridge/background.js`
- Modify: `browser_extension/ozon_v2_bridge/workbench_content.js`
- Test: `tests/test_browser_extension_supplier.js`
- Test: `tests/test_browser_extension_background.js`

- [ ] Add a failing Node test asserting one dedicated window, N tabs, no ordinary-tab reuse and stable tab-to-channel bindings.
- [ ] Add a failing close test asserting closing a managed tab/window stops the run and suppresses reopen for the same dispatch token.
- [ ] Run both Node tests and verify expected failures.
- [ ] Add the `windows` permission, dedicated-window state, channel lookup messaging and close suppression.
- [ ] Re-run both Node tests and verify they pass.

### Task 4: In-page collection panel

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/supplier_content.js`
- Test: `tests/test_supplier_content_script.js`

- [ ] Add a failing test proving no data is submitted before the user clicks the bound detail-page button.
- [ ] Add a failing test proving the clicked page submits only its bound `seed_id` and full public supplier fields.
- [ ] Run `node tests/test_supplier_content_script.js` and verify both failures.
- [ ] Reuse existing public-field collectors behind an explicit fixed panel state machine.
- [ ] Add best-effort reference-image upload and visible retry/error states without claiming success when the page rejects the upload.
- [ ] Re-run the script test and verify it passes.

### Task 5: Workbench visibility and regression verification

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`
- Create: `E:/obsidian仓库/萧机麦仓库/Ozon 工作台项目库 (Workbench Project)/05-操作日志 (Operation Logs)/2026-07-15 1688专用选品采集 (Managed Supplier Selection).md`

- [ ] Show per-channel statuses in supplier review while retaining the URL fallback and “找不到供应商” action.
- [ ] Run targeted Python and Node tests.
- [ ] Run the full project test suite and record exact pass/fail counts.
- [ ] Restart the local workbench service and verify `http://127.0.0.1:8765/` responds.
- [ ] Record architecture, changed files, tests and extension reload requirement in the Obsidian project library.
