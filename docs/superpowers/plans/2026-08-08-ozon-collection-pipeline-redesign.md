# Ozon Collection Pipeline Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current Ozon-template gate with a fast subject-locked Ozon reference collection, then resolve the official Seller API category and field template from the locked 1688 SKU, using per-item confirmation only when the official type is genuinely ambiguous.

**Architecture:** Ozon collection becomes a single browser stage that validates a structured seed subject contract and saves market-reference evidence. Supplier review then locks one 1688 SKU and materializes a supplier-truth profile. A server-side category resolver maps that truth profile to `description_category_id + type_id`, downloads and caches the official Seller API template, and creates a per-item confirmation record only for ambiguous mappings. Slot identity and browser task identity prevent old candidates or tabs from writing into the active batch.

**Tech Stack:** Python 3.11, pytest, stdlib HTTP server, Ozon Seller API, Edge/Chrome extension JavaScript, Node-based JavaScript tests, JSON filesystem repository.

---

## Task 1: Add a strict seed subject contract

**Files:**
- Create: `src/ozon_v2/domain/seed_subject.py`
- Modify: `src/ozon_v2/services/collection_contract_service.py`
- Test: `tests/test_seed_subject.py`
- Test: `tests/test_collection_contract_service.py`

- [ ] Write failing tests proving a generic writing board does not satisfy the handball score-recording seed and proving Russian morphology is compared by normalized stems.

```python
def test_handball_score_record_seed_rejects_generic_writing_board() -> None:
    contract = build_seed_subject_contract(
        seed_id="seed-5000-0001",
        source_text_zh="手球计分记录夹",
        queries_ru=["планшет для записи счета гандбола"],
    )
    result = evaluate_subject_text(contract, "Обычная доска для письма")
    assert result["accepted"] is False
    assert "гандбол" in result["missing_stems"]
```

- [ ] Run the focused tests and confirm they fail because the new module and contract fields do not exist.

Run: `python -m pytest tests/test_seed_subject.py tests/test_collection_contract_service.py -q`

Expected: FAIL with missing `ozon_v2.domain.seed_subject` or missing `seed_subject_contract`.

- [ ] Implement Russian token normalization, real Russian stopwords, required-stem extraction, and a 70% minimum match ratio with no two-generic-stem shortcut.

```python
def build_seed_subject_contract(*, seed_id: str, source_text_zh: str, queries_ru: list[str]) -> dict:
    primary_query = next(query for query in queries_ru if query.strip())
    stems = normalized_content_stems(primary_query)
    minimum_matches = max(1, math.ceil(len(stems) * 0.70))
    return {
        "seed_id": seed_id,
        "source_text_zh": source_text_zh,
        "primary_query_ru": primary_query,
        "required_stems": stems,
        "minimum_matches": minimum_matches,
        "minimum_match_ratio": 0.70,
    }
```

- [ ] Add `seed_subject_contract` to every Ozon collection seed and mark Ozon category/type/attributes as `market_reference`.

- [ ] Run the focused tests and commit.

Run: `python -m pytest tests/test_seed_subject.py tests/test_collection_contract_service.py -q`

Expected: PASS.

Commit: `git commit -m "feat: add strict seed subject contracts"`

## Task 2: Enforce the subject contract before and after browser collection

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/product_evidence.js`
- Modify: `browser_extension/ozon_v2_bridge/content.js`
- Modify: `src/ozon_v2/domain/validators.py`
- Test: `tests/test_browser_product_evidence.js`
- Test: `tests/test_collection_policies.py`

- [ ] Add failing JavaScript tests for subject acceptance and rejection, including missing identity qualifiers and generic-title false positives.

```javascript
assert.equal(
  evidence.evaluateSeedSubject(
    contract,
    { title: "Обычная доска для письма", type: "Доска", attributes: {} }
  ).accepted,
  false,
);
```

- [ ] Add failing Python validation tests proving a browser payload without matching `subject_match_evidence` is rejected at ingest.

- [ ] Replace `matchesQueryIntent` with deterministic contract evaluation over title, product type, and core attributes. Save the exact matched and missing stems in `subject_match_evidence`.

- [ ] Limit one seed to at most three detail candidates and stop opening detail pages for search-card candidates that fail the subject prefilter.

- [ ] Re-evaluate the same evidence in Python during ingest so stale or malformed extension results cannot pass by setting `accepted=true` alone.

- [ ] Run browser and Python tests and commit.

Run: `node tests/test_browser_product_evidence.js`

Run: `python -m pytest tests/test_collection_policies.py -q`

Expected: both PASS.

Commit: `git commit -m "fix: enforce Ozon subject identity at collection"`

## Task 3: Remove the Ozon attribute-template browser gate

**Files:**
- Modify: `src/ozon_v2/domain/state_machine.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/runner.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `browser_extension/ozon_v2_bridge/background_v2.js`
- Modify: `browser_extension/ozon_v2_bridge/content.js`
- Test: `tests/test_state_machine.py`
- Test: `tests/test_workbench_skeleton.py`
- Test: `tests/test_browser_extension_background.js`

- [ ] Add failing state-machine tests requiring `OZON_COLLECTED -> SUPPLIER_REVIEW` without `ATTRIBUTE_TEMPLATE_COLLECTING`.

```python
assert machine.transition(
    WorkbenchState.OZON_COLLECTED,
    WorkbenchAction.OPEN_SUPPLIER_REVIEW,
) == WorkbenchState.SUPPLIER_REVIEW
```

- [ ] Add a regression test proving Seller API template failure cannot retire, blacklist, replace, or recollect a locked Ozon product.

- [ ] Change the active transition and runner path so a complete Ozon result immediately prepares supplier review.

- [ ] Remove active `ozon_attribute_template` task dispatch, polling, extension navigation, and the `attribute_template_worker_required` fallback. Keep old JSON readers only where historical upload records still need to be displayed.

- [ ] Make an old active batch in `attribute_template_collecting` return an explicit restart-required result instead of silently entering the new pipeline.

- [ ] Run the focused regression suite and commit.

Run: `python -m pytest tests/test_state_machine.py tests/test_workbench_skeleton.py -q`

Run: `node tests/test_browser_extension_background.js`

Expected: PASS and no new active browser task has type `ozon_attribute_template`.

Commit: `git commit -m "refactor: remove Ozon template collection gate"`

## Task 4: Make collection and replacement slot-scoped

**Files:**
- Modify: `src/ozon_v2/services/collection_contract_service.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `browser_extension/ozon_v2_bridge/background_v2.js`
- Modify: `browser_extension/ozon_v2_bridge/content.js`
- Test: `tests/test_collection_contract_service.py`
- Test: `tests/test_browser_collection_progress.js`
- Test: `tests/test_browser_extension_background.js`
- Test: `tests/test_workbench_skeleton.py`

- [ ] Add failing tests requiring every browser task and result to bind:

```text
run_id + slot_id + candidate_revision + task_type + dispatch_token
```

- [ ] Add tests proving a replacement contract contains only the pending slot and that completed slots are supplied only as immutable server-side checkpoints.

- [ ] Persist `slot_id` and `candidate_revision` in the active seed record. Increment the revision only when that slot is replaced.

- [ ] Validate task identity before browser wait, navigation, and submit; close or abandon stale task tabs when any identity field changes.

- [ ] Reject stale result writes server-side without mutating active run state.

- [ ] Run focused tests and commit.

Run: `python -m pytest tests/test_collection_contract_service.py tests/test_workbench_skeleton.py -q`

Run: `node tests/test_browser_collection_progress.js && node tests/test_browser_extension_background.js`

Expected: PASS; stale token/revision cases are rejected and completed slots stay unchanged.

Commit: `git commit -m "fix: isolate Ozon collection by slot revision"`

## Task 5: Materialize locked 1688 supplier truth

**Files:**
- Create: `src/ozon_v2/domain/supplier_truth.py`
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Test: `tests/test_supplier_truth.py`
- Test: `tests/test_workbench_skeleton.py`

- [ ] Add failing tests that build one truth profile per locked supplier SKU and reject profiles missing offer ID, selected SKU identity, or evidence sources.

```python
profile = build_supplier_truth_profile(slot, supplier_result)
assert profile["supplier_offer_id"] == "731070963867"
assert profile["selected_sku_id"]
assert profile["subject"]["value"]
assert profile["evidence_sources"]
```

- [ ] Implement `supplier_truth_profile` extraction from the selected 1688 offer and SKU only. Include objective fields, selected SKU images, and field-level evidence pointers.

- [ ] Save all profiles to `supplier_truth_profiles.json` after supplier lock and expose repository load/save methods.

- [ ] Ensure Ozon market-reference values never overwrite supplier truth; conflicting Ozon values remain readable only as copy/style references.

- [ ] Run focused tests and commit.

Run: `python -m pytest tests/test_supplier_truth.py tests/test_workbench_skeleton.py -q`

Expected: PASS.

Commit: `git commit -m "feat: persist locked 1688 supplier truth"`

## Task 6: Resolve official Seller API categories from supplier truth

**Files:**
- Create: `src/ozon_v2/services/seller_category_resolver.py`
- Modify: `src/ozon_v2/adapters/seller_api.py`
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Test: `tests/test_seller_category_resolver.py`
- Test: `tests/test_seller_api.py`

- [ ] Add failing tests for unique automatic resolution, ambiguous top candidates, template cache hits, and Seller API outages preserving locked Ozon/1688 evidence.

- [ ] Expose adapter methods that fetch the official category tree and fetch a template by an already chosen `description_category_id + type_id`. Remove public-Ozon-title fuzzy matching from the active path.

```python
def fetch_attribute_template(self, description_category_id: int, type_id: int) -> dict:
    return {
        "description_category_id": description_category_id,
        "type_id": type_id,
        "attributes": self.fetch_description_category_attributes(
            description_category_id=description_category_id,
            type_id=type_id,
        ),
    }
```

- [ ] Implement `SellerCategoryResolver` over the supplier truth subject, intended use, attributes, and the Russian subject expression produced from those 1688 facts. Seed queries may be a translation hint, but Ozon product category/type/title must not contribute to the score.

- [ ] Require a minimum confidence and a positive margin over the second candidate. Otherwise return the top two or three official candidates as `category_confirmation_required`.

- [ ] Cache templates by `description_category_id:type_id` in `seller_template_cache.json`.

- [ ] Run focused tests and commit.

Run: `python -m pytest tests/test_seller_category_resolver.py tests/test_seller_api.py -q`

Expected: PASS; no resolver call accepts an Ozon product payload.

Commit: `git commit -m "feat: resolve Seller categories from supplier truth"`

## Task 7: Add per-item category confirmation

**Files:**
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`
- Test: `tests/test_workbench_skeleton.py`

- [ ] Add failing API tests for listing unresolved items and confirming one official type.

```text
GET  /api/runs/{run_id}/category-confirmations
POST /api/runs/{run_id}/category-confirmations/{slot_id}
```

- [ ] Persist `category_resolutions.json` with `slot_id`, `candidate_revision`, status, candidates, chosen IDs, and template status.

- [ ] Render confirmation cards only for ambiguous items. Show the locked 1688 image/title/SKU, two or three official candidates, match evidence, category search, and a confirm button.

- [ ] On confirmation, validate the slot revision, save the official IDs, fetch/cache the template, and continue the item without recollecting Ozon or 1688.

- [ ] Ensure automatically resolved items never appear in the confirmation list and one unresolved item does not erase or recompute resolved items.

- [ ] Run focused tests and commit.

Run: `python -m pytest tests/test_workbench_local_server.py tests/test_workbench_skeleton.py -q`

Expected: PASS.

Commit: `git commit -m "feat: add per-item Seller category confirmation"`

## Task 8: Route field drafting to the new official templates

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `skills/ozon-intelligent-field-drafter/SKILL.md`
- Modify: `skills/ozon-intelligent-field-drafter/references/field-policy.md`
- Test: `tests/test_attribute_mapping_service.py`
- Test: `tests/test_workbench_skeleton.py`

- [ ] Add failing tests proving field drafts read the locked supplier truth plus the resolved Seller template and treat Ozon fields as non-authoritative reference.

- [ ] Change draft materialization to read `supplier_truth_profiles.json`, `category_resolutions.json`, and the cached Seller template instead of an Ozon-derived `attribute_template_result.json`.

- [ ] Preserve the existing rule that trustworthy collected facts auto-fill required attributes; only evidence-exhausted required fields go to the user.

- [ ] Update the field-drafter skill instructions to the same truth-source boundary so later runs cannot restore the old behavior.

- [ ] Run focused tests and commit.

Run: `python -m pytest tests/test_attribute_mapping_service.py tests/test_workbench_skeleton.py -q`

Expected: PASS.

Commit: `git commit -m "refactor: draft fields from supplier truth templates"`

## Task 9: Migration, full regression, and runtime smoke test

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `README.md`
- Test: `tests/test_workbench_skeleton.py`
- Test: all Python and JavaScript tests under `tests/`

- [ ] Add a startup migration that marks old active `attribute_template_collecting` batches as restart-required and never imports their browser task into a new batch.

- [ ] Add regression coverage for all acceptance cases in the approved specification, especially: 10/10 Ozon collection advances once, template failure does not recollect, China origin/manufacture is accepted, and replacement touches one slot only.

- [ ] Run the complete Python suite.

Run: `python -m pytest -q`

Expected: PASS with zero failures.

- [ ] Run every browser-extension test.

Run:

```powershell
Get-ChildItem tests -Filter 'test_browser_*.js' | ForEach-Object { node $_.FullName }
```

Expected: every script exits 0.

- [ ] Start the local workbench against a temporary runtime root, create a one-item batch, and verify the visible sequence is Ozon collection -> supplier review -> supplier lock -> automatic category or one-item confirmation; no Ozon template browser stage appears.

- [ ] Review `git diff --check`, `git status --short`, and the staged diff so unrelated user changes are not included.

- [ ] Commit the integration and documentation changes.

Commit: `git commit -m "feat: complete supplier-truth collection pipeline"`

- [ ] Push the finished branch only after all verification commands pass.
