# Ozon Supplier SKU Gate and Image Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect every provable real 1688 SKU, let the user lock exactly one real SKU, and create a resumable two-worker pipeline that returns two main images and six supporting images without changing product truth.

**Architecture:** The existing supplier browser bridge remains the only 1688 page collector. It emits a versioned SKU matrix; the workbench validates it and writes an immutable SKU selection receipt. A subject-master gate then compiles an immutable image contract into a separate SQLite queue. Two pinned Codex tasks claim whole products atomically, execute one 2-panel grid and two 3-panel grids, repair only failed slots, and return receipts for eight fixed slots. The workbench is the state owner and upload remains locked until the user approves all eight slots.

**Tech Stack:** Python 3.11 standard library, dataclasses, sqlite3, JSON artifacts, vanilla JavaScript browser extension, local HTTP workbench, pytest, Node.js assertion tests, Codex Skill markdown.

---

## Task 1: Real Supplier SKU Contract

**Files:**
- Create: `src/ozon_v2/domain/supplier_sku.py`
- Modify: `src/ozon_v2/domain/models.py`
- Create: `tests/test_supplier_sku_contract.py`

- [ ] Write failing tests for complete SKU options, incomplete evidence rejection, stable combination keys, set quantity, and immutable selection hashes.
- [ ] Run `python -m pytest tests/test_supplier_sku_contract.py -q` and confirm failures reference missing supplier SKU contracts.
- [ ] Implement `SupplierSkuOption`, `SupplierSkuSelectionReceipt`, canonical JSON hashing, and strict validators.
- [ ] Run `python -m pytest tests/test_supplier_sku_contract.py -q` and expect all tests to pass.

## Task 2: 1688 Real SKU Matrix Collection

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/supplier_content.js`
- Modify: `tests/test_supplier_content_script.js`
- Modify: `tests/test_browser_extension_supplier.js`

- [ ] Add failing Node fixtures for a product with one-piece and four-piece real SKU combinations, SKU IDs, prices, stock, and SKU-bound images.
- [ ] Run `node tests/test_supplier_content_script.js` and confirm the missing `sku_options` matrix failure.
- [ ] Parse trusted embedded SKU maps first and DOM evidence second; preserve the legacy summary field only for compatibility.
- [ ] Mark DOM-only labels as incomplete instead of inventing Cartesian SKU combinations.
- [ ] Run `node tests/test_supplier_content_script.js` and `node tests/test_browser_extension_supplier.js` and expect both to pass.

## Task 3: Supplier SKU Persistence and User Selection Gate

**Files:**
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/domain/state_machine.py`
- Modify: `tests/test_workbench_skeleton.py`
- Modify: `tests/test_workbench_runner.py`
- Create: `tests/test_supplier_sku_selection_service.py`

- [ ] Write failing tests that ingestion rejects missing or incomplete SKU matrices and that image processing remains blocked until every product has a valid selection receipt.
- [ ] Write failing tests for `confirm match`, receipt persistence, attempted receipt mutation, and `reject and replace` seed blacklisting/refill.
- [ ] Run the focused tests and confirm the expected gate failures.
- [ ] Add repository methods for `supplier_sku_selections.json` and subject-master selections without changing old batch artifacts.
- [ ] Add service operations to confirm one real SKU, invalidate downstream image work on explicit replacement, and resume only when all products are locked.
- [ ] Run `python -m pytest tests/test_supplier_sku_selection_service.py tests/test_workbench_runner.py tests/test_workbench_skeleton.py -q` and expect all focused tests to pass.

## Task 4: Subject Master Gate and SQLite Image Queue

**Files:**
- Create: `src/ozon_v2/images/__init__.py`
- Create: `src/ozon_v2/images/contracts.py`
- Create: `src/ozon_v2/images/queue.py`
- Create: `tests/test_image_generation_queue.py`

- [ ] Write failing tests for exact SKU linkage, four-piece set completeness, file SHA-256 locking, eight slot creation, atomic whole-product claims, leases, stop, resume, and no duplicate jobs.
- [ ] Run `python -m pytest tests/test_image_generation_queue.py -q` and confirm failures.
- [ ] Implement the subject-master contract and SQLite schema for jobs, slots, attempts, worker leases, and heartbeats.
- [ ] Reject image jobs whose subject master does not reference the active SKU selection hash.
- [ ] Run `python -m pytest tests/test_image_generation_queue.py -q` and expect all tests to pass.

## Task 5: Fixed Eight-Slot Image Protocol and Codex Skill

**Files:**
- Create: `.agents/skills/ozon-product-image-generator/SKILL.md`
- Create: `.agents/skills/ozon-product-image-generator/references/prompt-contract.md`
- Create: `scripts/ozon_image_worker.py`
- Create: `tests/test_ozon_image_worker.py`
- Modify: `.codex-plugin/plugin.json`

- [ ] Write failing tests for slot order, one 1x2 crop, two 1x3 crops, accepted-slot freezing, two repair attempts, worker heartbeats, and result receipt validation.
- [ ] Run `python -m pytest tests/test_ozon_image_worker.py -q` and confirm failures.
- [ ] Implement deterministic crop/receipt orchestration while leaving actual bitmap generation to the Codex image tool described by the Skill.
- [ ] Encode truth rules: one locked sales unit, exact count/color/shape/parts, Ozon as style reference only, supplier evidence as product truth.
- [ ] Register the Skill in the plugin manifest and run the plugin audit workflow.
- [ ] Run `python -m pytest tests/test_ozon_image_worker.py -q` and expect all tests to pass.

## Task 6: Workbench SKU Review and Image Review UI

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `tests/test_workbench_local_server.py`

- [ ] Write failing tests for all-SKU cards, Ozon-vs-1688 differences, `确认一致 (Confirm Match)`, `不一致并替换 (Reject and Replace)`, subject-master selection, worker status, eight slot cards, stop/resume, and fixed-height product paging.
- [ ] Run `python -m pytest tests/test_workbench_local_server.py -q` and confirm failures.
- [ ] Add small JSON endpoints for SKU selection, subject-master selection, image queue status, slot receipts, and final user approval.
- [ ] Render all evidence needed for decisions; do not show generated progress until a worker owns the product.
- [ ] Run `python -m pytest tests/test_workbench_local_server.py -q` and expect all tests to pass.

## Task 7: Upload Gate and Diagnostics

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/services/diagnostics_export_service.py`
- Modify: `tests/test_diagnostics_export_service.py`
- Modify: `tests/test_workbench_local_server.py`

- [ ] Write failing tests proving upload rejects missing/unapproved slots and diagnostics include SKU receipts, subject-master hashes, image jobs, slot receipts, worker state, and failures without credentials.
- [ ] Run focused tests and confirm failures.
- [ ] Make upload drafts read only the active approved eight-slot result.
- [ ] Add the new non-secret artifacts to the current-batch diagnostics ZIP.
- [ ] Run focused tests and expect all tests to pass.

## Task 8: Regression, Browser Smoke Test, and Project Records

**Files:**
- Modify: `E:\obsidian仓库\萧机麦仓库\Ozon 工作台项目库 (Workbench Project)\05-操作日志 (Operation Logs)\2026-07-14 供应商SKU门禁与生图执行器 (Supplier SKU Gate and Image Executor).md`

- [ ] Run `python -m pytest -q`.
- [ ] Run all browser-extension Node tests under `tests/test_browser_*.js` and `tests/test_supplier_content_script.js`.
- [ ] Start the local server on an available port and verify supplier SKU selection, subject-master blocking, image queue status, stop/resume, and eight-slot review in Edge at desktop and narrow widths.
- [ ] Run the Codex plugin audit and record any pre-existing unrelated failures separately.
- [ ] Synchronize implementation evidence, exact test output, remaining limits, and runtime URL to the E-drive Obsidian project library.
- [ ] Skip Git commits because the current `.git` entry is not a valid repository; do not initialize or alter repository history.

## Plan Self-Review

- [ ] Every user-visible completion is backed by a persisted SKU, image, or approval receipt.
- [ ] No new state claims completion when the browser collector or Codex worker has not returned data.
- [ ] Existing Ozon collection, supplier-link review, stop behavior, seed blacklist/refill, and diagnostics remain intact.
- [ ] Old plugins are neither imported nor modified.
- [ ] No custom supplier bundle or generated subject-master image is introduced.
