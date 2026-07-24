# Ozon Slot Reference Image Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Ozon product media Skill generate two main images and six supporting images by binding every finished slot to one primary Ozon reference while preserving the locked 1688 subject.

**Architecture:** Keep truth ownership in the locked supplier evidence and white-background subject anchor. Add a deterministic slot-to-reference contract to the Skill and prompt assets, then require receipts to prove both subject preservation and reference-composition alignment. The workbench remains an evidence/result surface.

**Tech Stack:** Markdown Skill contracts, fixed text prompt assets, pytest contract tests.

---

### Task 1: Add failing contract tests

**Files:**
- Modify: `tests/test_image_worker_contract_files.py`

- [ ] Add a test requiring eight per-slot primary Ozon references, ordered panel mapping, subject/reference separation, receipt fields, and the unchanged 2+6 exact-3:4 output contract.
- [ ] Run `python -m pytest tests/test_image_worker_contract_files.py -q`.
- [ ] Confirm the new test fails because the active Skill does not yet define the per-slot reference contract.

### Task 2: Upgrade the Skill and prompt contract

**Files:**
- Modify: `skills/ozon-product-media-generator/SKILL.md`
- Modify: `skills/ozon-product-media-generator/references/prompt-contract.md`

- [ ] Define the white anchor as the only identity/geometry source used by imagegen, with locked supplier images remaining final truth.
- [ ] Define deterministic valid-reference filtering and slot mapping.
- [ ] Require a structured composition blueprint and receipt fields for every slot.
- [ ] Preserve the four-call first attempt, exact 3:4 panels, local Russian typography, repair isolation, and no-upload boundary.

### Task 3: Upgrade fixed prompt assets

**Files:**
- Modify: `skills/ozon-product-media-generator/assets/main-grid-prompt.txt`
- Modify: `skills/ozon-product-media-generator/assets/detail-grid-a-prompt.txt`
- Modify: `skills/ozon-product-media-generator/assets/detail-grid-b-prompt.txt`
- Modify: `skills/ozon-product-media-generator/assets/repair-slot-prompt.txt`

- [ ] Label Ozon references by panel and prohibit cross-panel blending.
- [ ] Require the target product to replace the reference product while following composition, camera, background, lighting, negative space, and information hierarchy.
- [ ] Forbid copying reference branding, text, watermarks, prices, accessories, or unsupported functions.
- [ ] Require repair calls to reuse the failed slot's original primary reference unless the user explicitly changes it.

### Task 4: Validate and regress

**Files:**
- Test: `tests/test_image_worker_contract_files.py`

- [ ] Run `python -m pytest tests/test_image_worker_contract_files.py -q`.
- [ ] Run the Skill validator against `skills/ozon-product-media-generator`.
- [ ] Run the relevant Ozon image-worker tests without triggering real image generation, upload, publish, or approval.
- [ ] Inspect the final diff to ensure no workbench scheduling logic changed.
