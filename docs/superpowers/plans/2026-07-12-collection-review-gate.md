# Collection Review Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ozon and 1688 collection evidence auditable before image processing and prevent non-product 1688 images from entering the image workspace.

**Architecture:** Extend the existing supplier review API/page into one combined collection review surface. Keep the current state machine and use its existing `start_image_processing` action as the explicit approval transition; change autopilot so `supplier_collected` is a blocking review state. Tighten the browser collector at the source.

**Tech Stack:** Python 3, local HTTP workbench, unittest/pytest, Chromium extension JavaScript, Node-based collector tests.

---

### Task 1: Lock the review gate with service tests

**Files:**
- Modify: `tests/test_workbench_service.py`
- Modify: `tests/test_workbench_runner.py`
- Modify: `src/ozon_v2/services/workbench_service.py`

- [ ] Add a failing test asserting `run_until_blocked()` leaves a completed supplier collection in `supplier_collected` with blocked reason `collection_review_required`.
- [ ] Run the focused tests and verify the old automatic `image_processing` transition fails the assertion.
- [ ] Replace the automatic transition in the `SUPPLIER_COLLECTED` branch with `_autopilot_blocked(..., "collection_review_required", ...)`.
- [ ] Run the focused tests and verify they pass.

### Task 2: Return complete collection evidence from the review API

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/services/workbench_service.py`

- [ ] Add a failing API test that expects `ozon_product`, `supplier_product`, `ozon_completeness`, `supplier_completeness`, and `can_approve` for each review item.
- [ ] Run the test and confirm the new fields are absent.
- [ ] Merge persisted Ozon candidates and supplier products by `seed_id`; compute required-field completeness without changing stored artifacts.
- [ ] Run the API test and verify all evidence and missing fields are returned.

### Task 3: Add explicit approval and remove automatic redirect

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [ ] Add failing page/API tests requiring `确认采集结果 (Approve Collection)`, no automatic redirect from `supplier_collected`, and an approval POST that dispatches `start_image_processing`.
- [ ] Run the tests and confirm failure against current behavior.
- [ ] Add the approval endpoint and button, enable it only when `can_approve` is true, and render a clear error when fields are missing.
- [ ] Remove the automatic supplier-review-to-images redirect; navigate only after successful approval.
- [ ] Run focused page/API tests and verify they pass.

### Task 4: Render Ozon and 1688 evidence for user audit

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [ ] Add failing HTML assertions for Ozon category, seller, SKU, delivery, attributes, source images, supplier title, price, SKU, attributes, freight, and filtered images.
- [ ] Run the test and confirm the current summary-only page fails.
- [ ] Render compact side-by-side evidence sections with expandable attributes and stable thumbnail grids; show collected/missing status explicitly.
- [ ] Run focused HTML tests and verify they pass.

### Task 5: Filter supplier images and correct the supplier title

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/supplier_content.js`
- Modify: `browser_extension/ozon_v2_bridge/manifest.json`
- Modify: `tests/browser_extension_supplier_content.test.js`

- [ ] Add failing Node tests with a fixture containing SVG icons, tiny UI images, duplicate `_sum` thumbnails, product gallery images, and a company-name element separate from the product title.
- [ ] Run the Node tests and verify the broad `document.querySelectorAll("img")` collector fails.
- [ ] Restrict collection to product evidence regions, reject SVG/icon/logo/small assets, canonicalize thumbnail URLs, deduplicate, and prioritize product title selectors/meta evidence.
- [ ] Bump the extension version once after all browser changes are complete.
- [ ] Run all browser-extension tests and verify they pass.

### Task 6: Regression, current-run audit, and project record

**Files:**
- Modify: `E:/obsidian仓库/萧机麦仓库/Ozon 工作台项目库 (Workbench Project)/02-操作日志 (Operation Logs)/2026-07-12 采集审核门与图片过滤 (Collection Review Gate).md`

- [ ] Run focused service, runner, local-server, and Node tests.
- [ ] Run the broader relevant Python suite and record any pre-existing unrelated failures separately.
- [ ] Restart the workbench and verify health at `http://127.0.0.1:8765/`.
- [ ] Open the current batch review route and verify existing Ozon/1688 evidence is visible without deleting artifacts.
- [ ] Record the root cause, changed files, test evidence, extension version, and any required one-time reload in Obsidian.

