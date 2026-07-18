# Supplier Review Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move a completed Ozon batch to a dedicated supplier-review page, accept one verified 1688 product link per Ozon product, and start real direct-network supplier collection only after the user clicks the collection command.

**Architecture:** Keep the workbench as the control plane. Persist review input and supplier artifacts inside the existing run directory, use an explicit supplier-review state, and run 1688 collection through a small local Playwright worker with proxy disabled. Image processing remains a later, separate state.

**Tech Stack:** Python 3.11, built-in HTTP server, filesystem JSON artifacts, Node.js, Playwright, existing workbench runner.

---

### Task 1: Supplier review state and persistence

**Files:**
- Modify: `src/ozon_v2/domain/models.py`
- Modify: `src/ozon_v2/domain/state_machine.py`
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Test: `tests/test_workbench_skeleton.py`

- [ ] Write failing tests for automatic `supplier_review`, Ozon evidence rows, 1688 URL validation, and persisted review input.
- [ ] Run the focused tests and verify the expected failures.
- [ ] Implement only the state transition and persistence needed by those tests.
- [ ] Run the focused tests until green.

### Task 2: Supplier review HTTP page

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] Write failing route tests for the review page, review-data API, link submission, and supplier collection command.
- [ ] Run the focused tests and verify the expected failures.
- [ ] Add the compact review page and APIs, including automatic navigation from the workbench.
- [ ] Run the focused tests until green.

### Task 3: Direct 1688 collection worker

**Files:**
- Create: `src/ozon_v2/adapters/supplier_browser_worker.py`
- Create: `scripts/collect_1688_supplier_link.js`
- Modify: `src/ozon_v2/workbench/runner.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_supplier_browser_worker.py`
- Test: `tests/test_workbench_runner.py`

- [ ] Write failing worker and runner tests using deterministic fixture output.
- [ ] Run the focused tests and verify the expected failures.
- [ ] Implement proxy-disabled Playwright collection, validated output ingestion, and transition to `supplier_collected` then `image_processing`.
- [ ] Run all affected tests and perform a browser screenshot check of the review page.
