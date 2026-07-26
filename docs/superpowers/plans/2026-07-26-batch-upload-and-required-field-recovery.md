# Ozon V2 Batch Upload And Required Field Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a recoverable per-product batch upload flow with category self-healing and inline user evidence for missing required attributes.

**Architecture:** Keep the existing per-product preview/confirm contract, add seed-scoped draft preparation and a batch coordinator that heals mismatched templates, validates products concurrently, and submits independent Seller API tasks with bounded concurrency. Persist manual required-field facts separately and feed them into the existing attribute mapper. Make the image controller's empty fixed-task registry a one-time automatic bootstrap instead of a magic-phrase blocker.

**Tech Stack:** Python 3.11, `ThreadingHTTPServer`, vanilla browser JavaScript, JSON runtime repository, `unittest`/pytest.

---

### Task 1: Prove seed-scoped validation

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/services/workbench_service.py`

- [x] Add a failing test with two ready products and dictionary-call tracking; previewing one seed must resolve dictionaries only for that seed.
- [x] Run the focused test and verify the second seed is currently resolved.
- [x] Add a seed filter to draft construction and make `preview_product_upload` use it.
- [x] Re-run the focused test and verify it passes.

### Task 2: Prove category self-healing

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/services/workbench_service.py`

- [x] Add failing tests for cross-domain and required-not-applicable template mismatches.
- [x] Run the tests and verify upload preparation currently remains blocked.
- [x] Add per-product automatic template refresh before preview and batch validation.
- [x] Re-run the tests and verify each item uses its credible replacement template.

### Task 3: Add user-confirmed required-field evidence

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/attribute_mapping_service.py`
- Modify: `src/ozon_v2/services/workbench_service.py`

- [x] Add failing tests that reject optional/unknown fields and accept only missing required fields from the current template.
- [x] Add repository methods for `required_attribute_evidence.json`.
- [x] Add user-confirmed required evidence to `map_template_attributes` with `user_confirmed_required_attribute` provenance.
- [x] Add `save_required_attribute_evidence` validation and persistence.
- [x] Re-run focused tests and verify the required gate becomes ready after valid input.

### Task 4: Add bounded batch prepare and submit

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [x] Add service and route coverage for explicit confirmation, bounded parallel validation/submission, and per-item isolation.
- [x] Implement one batch endpoint that prepares eligible products, skips gaps, submits independent one-item imports with a maximum of four workers, and persists merged results.
- [x] Ensure every accepted item creates exactly one image task package.
- [x] Re-run focused tests.

### Task 5: Add upload workspace controls

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [x] Add failing HTML assertions for the batch action and required-field editor.
- [x] Render missing required inputs only for required fields, with one save action per product.
- [x] Add the batch button, explicit confirmation, request dispatch, result rendering, and workspace refresh.
- [x] Re-run local-server tests.

### Task 6: Remove the image Skill bootstrap blocker

**Files:**
- Modify: `skills/ozon-image-generation-controller/SKILL.md`
- Modify: `skills/ozon-image-generation-controller/agents/openai.yaml`
- Modify: `tests/test_ozon_image_controller_skill.py`

- [x] Add contract assertions for one-time fixed-task-pool bootstrap.
- [x] Treat direct start/continue/process intent as bootstrap authorization when the registry is empty.
- [x] Reuse registered fixed tasks after initialization and require explicit replacement authorization only for an unavailable registered task.
- [x] Synchronize the current-user installed Skill copy.

### Task 7: Verify and deliver

**Files:**
- Modify only if a verification failure exposes a regression.

- [ ] Run focused upload, category, mapping, and route tests.
- [ ] Run `python -m pytest -q`.
- [ ] Run the strict installation Doctor.
- [ ] Restart the local workbench and verify `/api/health`.
- [ ] Confirm no real Seller API upload occurred during testing.
- [ ] Commit all source, tests, specs, and plans; push `main` and verify the remote hash.
