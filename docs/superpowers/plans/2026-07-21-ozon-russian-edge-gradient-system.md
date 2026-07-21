# Ozon V2 Russian Edge-Gradient Visual System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace detached rounded Russian text cards with the approved B edge-gradient system while preserving evidence validation, mobile readability, and the no-real-generation boundary.

**Architecture:** Keep the existing `VisualSpec` recipes and queue contract stable. Change only the deterministic Pillow renderer and the active media skill wording: side recipes render into a soft edge gradient, context copy renders into a bottom gradient, and feature copy uses anchor lines with a soft edge fade. Add receipt metadata so new B-system output is auditable without invalidating historical receipts.

**Tech Stack:** Python 3.11, Pillow, pytest, Markdown skill contracts.

---

### Task 1: Lock the B-system renderer behavior

**Files:**
- Modify: `tests/test_image_visual_design.py`
- Modify: `src/ozon_v2/images/visual_design.py`

- [x] **Step 1: Write failing tests**

Add tests that render uniform fixtures and assert: `integrated_rail` darkens the selected edge with a continuous fade; `context_caption` darkens only the lower region; `feature_callout` draws an accent anchor and does not draw a rounded card; all recipes preserve natural Russian headline case; the receipt reports `visual_system=ozon-edge-gradient-b1`.

- [x] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m pytest tests/test_image_visual_design.py -q
```

Expected: failures show the old rounded-card renderer and forced uppercase behavior.

- [x] **Step 3: Implement the minimal renderer change**

In `src/ozon_v2/images/visual_design.py`:

- add a deterministic RGBA edge-gradient helper and bottom-gradient helper;
- remove `rounded_rectangle` calls from `_draw_rail`, `_draw_caption`, and `_draw_callouts`;
- draw facts directly on the gradient using the existing measured safe areas;
- remove `.upper()` from headline wrapping;
- add `visual_system: "ozon-edge-gradient-b1"` to the render receipt.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_image_visual_design.py -q
```

Expected: all visual-design tests pass.

### Task 2: Update the active Ozon media skill contract

**Files:**
- Modify: `tests/test_image_worker_contract_files.py`
- Modify: `skills/ozon-product-media-generator/SKILL.md`
- Modify: `skills/ozon-product-media-generator/references/prompt-contract.md`
- Modify: `skills/ozon-product-media-generator/assets/main-grid-prompt.txt`
- Modify: `skills/ozon-product-media-generator/assets/detail-grid-a-prompt.txt`
- Modify: `skills/ozon-product-media-generator/assets/detail-grid-b-prompt.txt`
- Modify: `skills/ozon-product-media-generator/assets/repair-slot-prompt.txt`

- [x] **Step 1: Write failing contract assertions**

Require the skill and prompt contract to name the three B-system treatments, forbid detached rounded text cards, preserve natural Russian sentence case, and keep typography local.

- [x] **Step 2: Run the contract test and verify RED**

Run:

```powershell
python -m pytest tests/test_image_worker_contract_files.py -q
```

Expected: failure because the B-system wording is absent.

- [x] **Step 3: Update the fixed skill and prompt text**

Describe only scene-space reservation and local rendering. Keep the existing four mandatory first-pass generation calls, evidence priority, slot roles, repair limits, worker topology, and no-upload boundary unchanged.

- [x] **Step 4: Run the contract tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_image_worker_contract_files.py tests/test_ozon_image_controller_skill.py -q
```

Expected: all contract tests pass without invoking image generation.

### Task 3: Produce an offline visual proof and run regressions

**Files:**
- Create: `.artifacts/ozon-russian-edge-gradient-preview/` (ignored local evidence only)
- Verify: `tests/test_image_visual_design.py`
- Verify: full Python suite

- [x] **Step 1: Render local previews**

Use `render_visual` against existing local scene images when available; otherwise use deterministic Pillow fixtures. Render one example for each B recipe and a 360-pixel contact sheet. Do not call `imagegen`.

- [x] **Step 2: Inspect the produced pixels**

Verify Russian readability, no independent rounded cards, continuous gradient edges, visible callout anchors, and no source overwrite.

- [x] **Step 3: Run focused and full verification**

Run:

```powershell
python -m pytest tests/test_image_visual_design.py tests/test_image_worker_contract_files.py tests/test_ozon_image_worker.py tests/test_image_generation_queue.py -q
python -m pytest -q
git diff --check
git status --short
```

Expected: all tests pass, diff check is clean, and no business action is triggered.

- [x] **Step 4: Commit locally**

```powershell
git add docs/superpowers/specs/2026-07-21-ozon-russian-edge-gradient-system-design.md docs/superpowers/plans/2026-07-21-ozon-russian-edge-gradient-system.md src/ozon_v2/images/visual_design.py tests/test_image_visual_design.py tests/test_image_worker_contract_files.py skills/ozon-product-media-generator
git commit -m "fix: integrate Russian copy into Ozon imagery"
```

Do not push or create a pull request unless the user requests it.
