# Public Product Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce complete, validated Ozon public product artifacts instead of treating page traversal as collection.

**Architecture:** Add one small browser-side evidence module for deterministic JSON-LD and field normalization, make the content script wait on strong seller evidence plus a complete product snapshot, and strengthen Python ingest validation so incomplete or placeholder data cannot complete a gate.

**Tech Stack:** Manifest V3 JavaScript, Node.js assertion tests, Python 3.11, `unittest`, existing browser bridge and Seller API adapter.

---

The workspace is not a Git repository. Do not initialize Git. Do not modify the old plugin.

### Task 1: Lock The Result Contract With Failing Tests

- [ ] Update `tests/test_browser_seller_evidence.js` so generic seller words are not readiness evidence.
- [ ] Create `tests/test_browser_product_evidence.js` for JSON-LD extraction, real attributes, media filtering and snapshot completeness.
- [ ] Extend `tests/test_collection_policies.py` and `tests/test_workbench_skeleton.py` so placeholder attributes, missing media, seller URL and delivery data fail ingest.
- [ ] Run targeted tests and observe RED for the missing behavior.

### Task 2: Add Focused Browser Product Evidence

- [ ] Create `browser_extension/ozon_v2_bridge/product_evidence.js` with pure normalization, JSON-LD selection and completeness checks.
- [ ] Register it before `content.js` in `manifest.json` and increment the extension version.
- [ ] Run browser evidence tests and syntax checks until GREEN.

### Task 3: Replace Premature Readiness And Placeholder Extraction

- [ ] Make `seller_evidence.hasDecisionEvidence()` require an explicit local or Chinese cross-border signal.
- [ ] Build a product snapshot from JSON-LD plus targeted Ozon widgets and breadcrumb/attribute pairs.
- [ ] Wait until the seller decision is high confidence and core fields are stable; wait the full bounded timeout for `unknown`.
- [ ] Build template and Ozon candidate payloads only from the validated snapshot; remove all fabricated fallback values.
- [ ] Run targeted browser tests and syntax checks.

### Task 4: Enforce Complete Results At The Python Boundary

- [ ] Require precise numeric category ID, true SKU, real attribute values, product media, seller URL, price/rating/reviews and delivery evidence.
- [ ] Require all public content-score keys and nonempty gallery evidence.
- [ ] Verify invalid payloads do not create result files or advance workbench state.
- [ ] Run targeted and full Python suites.

### Task 5: Live Verification

- [ ] Restart only the Ozon V2 workbench and reload extension version.
- [ ] Re-run the mouse-pad seed with no seed removal on failure.
- [ ] Inspect both result artifacts field by field and report actual values, not event counts.
- [ ] Append the verified result or exact blocker to the canonical E-drive Obsidian log.
