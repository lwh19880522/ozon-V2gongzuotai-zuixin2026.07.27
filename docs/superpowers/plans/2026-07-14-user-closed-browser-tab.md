# User-Closed Browser Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop collection when the user closes its assigned browser tab and reopen only after an explicit workbench resume.

**Architecture:** Persist a close marker beside the existing task-to-tab mapping. Serialize tab-removal handling through the existing open-task queue, cancel the matching live server task, suppress the same dispatch token, and clear suppression when a new resume token arrives.

**Tech Stack:** Chrome Extension Manifest V3, JavaScript VM contract tests, Python local workbench API.

---

### Task 1: Lock the browser-close contract

**Files:**
- Modify: `tests/test_browser_extension_background.js`

- [ ] Add a `chrome.tabs.onRemoved` test event and a server task response for the active run.
- [ ] Close the assigned tab and assert that `/runner/stop` is posted.
- [ ] Poll the unchanged task and assert no replacement tab is created.
- [ ] Poll a resumed task with a new dispatch token and assert one replacement tab is created.
- [ ] Run `node tests/test_browser_extension_background.js` and verify the new assertions fail before implementation.

### Task 2: Implement task-scoped close suppression

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/background.js`
- Modify: `browser_extension/ozon_v2_bridge/manifest.json`

- [ ] Serialize tab-removal handling through `openTaskQueue`.
- [ ] Persist `closedByUser`, `closedDispatchToken`, and `closedAt` on the matching task mapping.
- [ ] Stop the matching runnable server task and emit a diagnostic heartbeat.
- [ ] Return without opening while the dispatch token is unchanged.
- [ ] Clear the marker when a new dispatch token arrives.
- [ ] Bump the extension patch version once.

### Task 3: Verify no regressions

**Files:**
- Test: `tests/test_browser_extension_background.js`
- Test: `tests/test_browser_extension_supplier.js`
- Test: `tests/test_browser_workbench_content.js`

- [ ] Run the browser extension test set and require all scripts to pass.
- [ ] Run the relevant Python workbench tests.
- [ ] Reload the unpacked extension once and verify the loaded/required versions match.
- [ ] Confirm an unchanged stopped task remains quiet and an explicit resume reopens once.

