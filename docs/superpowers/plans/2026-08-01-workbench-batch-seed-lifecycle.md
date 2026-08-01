# Workbench Batch and Seed Lifecycle Plan

**Goal:** Prevent old/new batch cross-contamination and permanently exclude every sampled or supplier-rejected seed across batch clearing and seed-pool upgrades.

**Architecture:** `FsRepo` owns stable seed identities and batch-directory classification. `WorkbenchService` consumes sampled and replacement seeds idempotently. The local HTTP service exposes only valid current-schema tasks, sanitizes stale browser references, and clears all recognized new/legacy batch directories only after archiving sampled seed usage.

- [x] Add failing tests for stable seed identity and idempotent used/blacklist ledgers.
- [x] Consume normal sampled seeds and replacement seeds immediately.
- [x] Permanently blacklist user-confirmed no-supplier seeds by stable identity and Ozon product ID.
- [x] Reconcile legacy `seed-NNNN` and current `seed-5000-NNNN` identities during runtime initialization.
- [x] Ignore legacy or malformed runs in active browser-task selection.
- [x] Degrade deleted-task requests to `browser_task.none` and sanitize stale heartbeats.
- [x] Make Clear All Batches archive sampled usage and remove recognized `wb-*` and `run-*` directories while preserving authorization, dedupe, and seed ledgers.
- [x] Run isolated and live-workspace regression suites.
- [x] Restart the live workbench and verify health, runtime, task, and bridge APIs.

Verification completed on 2026-08-01:

- Lifecycle tests: 36 passed.
- Other module tests: 363 passed.
- Local workbench service tests: 162 passed.
- Live runtime: service healthy, bridge endpoint returns structured status, current task readable, and 5 historical identities excluded from the 5000-seed active pool. The extension was not sending a heartbeat during the final check, so its status correctly reported offline.
