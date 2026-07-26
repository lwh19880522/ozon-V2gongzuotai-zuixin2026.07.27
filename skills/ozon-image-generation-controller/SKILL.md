---
name: ozon-image-generation-controller
description: Use when an Ozon V2 batch needs concurrent image generation through ten fixed reusable user-visible Codex work tasks.
---

# Ozon Image Generation Controller

## Scope

Use this controller for the fixed post-product image-task inbox. The workbench
ends after product creation and writes one secret-free package to
`OzonOpsV2/image_tasks/pending`. The controller schedules those packages; it
never generates images itself, and it never returns generated files to the
workbench.

## Fixed visible task pool

1. The image pool contains ten fixed user-visible Codex work tasks and is global across every Ozon V2 batch. The controller task is not counted in this limit.
2. The stable slots are `ozon-image-worker-01`, `ozon-image-worker-02`, `ozon-image-worker-03`, `ozon-image-worker-04`, `ozon-image-worker-05`, `ozon-image-worker-06`, `ozon-image-worker-07`, `ozon-image-worker-08`, `ozon-image-worker-09`, and `ozon-image-worker-10`. These are the 10 个固定可见任务槽位 and queue lease identities.
3. Each slot maps to one persisted opaque `thread_id`. Never derive or invent a task ID from its slot number, display name, or URL.
4. Reuse the same ten tasks in slot order for new products，按槽位顺序复用. A task finishes product A, becomes idle, then receives product B. Each task handles one product at a time, and the pool handles at most ten products concurrently. 全局并发上限为 10.
5. Never use hidden child agents. Never create a product-specific task. Never grow the pool above ten.
6. The initial empty registry is a one-time pool bootstrap, not a batch-time
   replacement. When the registered pool is empty and the user directly asks
   to start, continue, or process Ozon image tasks, that request authorizes
   creation of exactly the missing fixed tasks through
   `ozon-image-worker-10`. Do not ask the user to repeat a magic confirmation phrase such as “create ten tasks”; create and register the fixed pool once,
   then dispatch the queued packages. If initialization was interrupted before
   all ten task IDs were durably registered, resume only the missing slot
   numbers.
7. After the pool has been initialized, a normal batch start, continuation,
   retry, repair, or controller restart must reuse the registered tasks and
   must not silently create or replace tasks. Replacing an unavailable
   registered task is allowed only when the user explicitly requests a new task.
8. If a registered task is unavailable, mark only that slot unavailable and continue with the other slots. Report the exact slot that needs the user's explicit replacement instruction.

## Fixed task-package inbox

The intake source is the runtime-wide fixed task-package inbox:
`image_tasks/pending`. A claimed package moves atomically to
`image_tasks/in_progress`; terminal receipts live under
`image_tasks/completed` or `image_tasks/failed`. The package's
`seller_import_task_id` and `offer_id` identify the newly created product. The
worker resolves `product_id` from the Seller import status before final gallery
replacement when it is not already stored in the package.

## Dispatch contract

1. Read all eligible whole-product `pending` packages across every Ozon V2 batch.
2. Reconcile the persisted fixed-task registry before dispatch. Do not recreate tasks after a controller restart.
3. Assign packages to idle slots in ascending slot order. Use the corresponding stable worker ID while that task owns the package.
4. Claim with `scripts/ozon_image_task_inbox.py --runtime-root <runtime> claim-next --worker <worker_id>`. Send the registered task only the short command returned from the claim: `RUN package_id=<id> worker_id=<id>`.
5. The visible task follows `skills/ozon-product-media-generator/SKILL.md`, reads only its owned package from `image_tasks/in_progress`, loads the fixed commercial-infographic core plus the 2+3+3 grid wrappers from that Skill, generates and validates eight 3:4 images, publishes them, and directly replaces the product gallery in Ozon. The controller must never paste or reconstruct the long image prompt inside a worker command.
6. When the package reaches `completed` or `failed`, release the fixed slot and immediately claim the next package. Continue dispatching other eligible `pending` packages. A failed package does not block other products.
7. Return repair and continuation work to the product's `preferred_slot`. If that task is busy, queue it for that same task rather than creating another task.
8. Package claiming and completion are idempotent by `package_id`; repeated controller scans must not start duplicate generation.

## Scripted dispatch loop

Use the filesystem inbox and the lightweight
`scripts/ozon_image_task_inbox.py`; do not add a daemon, another database, or
per-product controller tasks.

0. Before claiming any package, require the user to provide the public R2 HTTPS
   channel address. If it is supplied but not saved, POST
   `{"base_url":"https://..."}` to the local workbench
   `/api/settings/public-media` endpoint, then run
   `scripts/ozon_image_task_inbox.py --runtime-root <runtime> media-preflight`.
   If the address is absent, the bucket is inaccessible, or public preflight
   fails, claim nothing and ask the user to configure it. Do not start image
   generation.
1. Read the fixed-task registry before claiming packages. On a registered pool,
   keep every existing task. On an empty or interrupted first-install pool,
   apply the one-time pool bootstrap above, persist each returned opaque
   `thread_id` immediately, and continue without another user round trip.
   Replacing a different registered `thread_id` still requires the user's
   explicit replacement instruction.
2. Read `status`, then run `claim-next` once for each idle registered worker and send its returned `RUN package_id=... worker_id=...` command to the persisted `thread_id`.
3. Wait for any active package to reach `completed` or `failed`. Immediately run `claim-next` for that newly idle slot; do not wait for all ten active tasks to finish.
4. Do not stop after the first ten products. Ten is the global concurrency limit, not the batch size. A batch of 30 or 100 products keeps recycling the same ten tasks until the queue is drained.
5. End the controller only when `pending` = 0 and `in_progress` = 0, or when the user explicitly stops the batch or a system-wide failure prevents every remaining product from running.

## User-selected repair dispatch

When a queued product contains `repair_pending` slots, dispatch it to its original fixed visible task. Require the task to read every selected slot's `review_issue_code` and `review_note`, and process only the explicitly selected slots. Never reopen or regenerate an unselected `accepted` slot.

## Stop gates

Treat every product independently. A package waiting for an explicit user repair
instruction is a per-product waiting state, not a batch-wide stop gate. Continue
dispatching other eligible `pending` packages while any remain.

A missing SKU or subject gate blocks only that product. Report the blocked product and its required user action, then continue every other eligible queued product. A `stopped` product does not stop other eligible products. Report its persisted stop reason and recovery action. Never auto-resume a stopped product because the stop may have been requested by the user.

An accepted slot is frozen evidence. If its receipt hash, source/output hashes, and locked `visual_spec` verify, a display-metadata anomaly is not a product stop gate. Preserve that slot and continue the remaining `pending` slots. Stop the product only when the frozen receipt or file fails verification and the failure cannot be isolated.

Stop dispatching only when no eligible `pending` packages remain and no package is in progress, a system-wide inbox or evidence-store failure prevents all remaining work, or the user stops the batch. Report separate counts for stopped, missing SKU/subject, failed, and completed products.

## Boundaries

- Upload only the eight validated generated images for the package's configured
  Ozon store and product. This is a complete gallery replacement after product
  creation, not product creation or final business approval.
- The controller never returns generated files to the workbench and never
  modifies a workbench batch artifact after the package has been emitted.
- Never modify business source code while executing an image batch.
- Never bypass truth, SKU, subject, image-quality, or review gates.
- Never mark a package complete until Ozon accepts the gallery-replacement
  request and the task-package receipt is durable.
