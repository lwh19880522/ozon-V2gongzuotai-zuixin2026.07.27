---
name: ozon-image-generation-controller
description: Use when one Ozon V2 batch needs concurrent image generation through a capacity-aware pool of zero to five internal Codex subagents.
---

# Ozon Image Generation Controller

## Scope

Use this controller only after the user supplies an Ozon V2 `run_id`.
Read queued products from that batch. The controller supervises subagents; it does not generate images itself.

## Worker Contract

1. Before each dispatch cycle, inspect the existing image-subagent pool, unassigned queued whole-product jobs, and free runtime capacity. Spawn only `min(5 - active image subagents, unassigned queued products, currently free internal subagent slots)` additional subagents; currently free slots do not include already-running image subagents.
2. Spawn up to five internal subagents with the `spawn_agent` tool. They must be children of the current controller task, not user-visible Codex tasks or threads. Use the first available identities from this fixed mapping:
   - `ozon_image_worker_01` -> `ozon-image-worker-01`
   - `ozon_image_worker_02` -> `ozon-image-worker-02`
   - `ozon_image_worker_03` -> `ozon-image-worker-03`
   - `ozon_image_worker_04` -> `ozon-image-worker-04`
   - `ozon_image_worker_05` -> `ozon-image-worker-05`
3. Each subagent must read and follow `skills/ozon-product-media-generator/SKILL.md` and claim only with its mapped queue worker ID.
4. Reuse every successfully spawned subagent for later queued products. Send later products to idle subagents with `followup_task`; do not replace them with new tasks.
5. Never use `create_thread`, `fork_thread`, or any API that creates user-visible Codex tasks. Do not create user-visible Codex tasks or threads for image workers.
6. Never create a sixth regular image worker. When the runtime exposes a sixth child slot, keep it reserved for failure recovery, diagnosis, or human intervention.
7. Continue with every subagent that spawned successfully; fewer than five available slots is normal, not a batch failure. If the active image-subagent pool is empty and zero internal subagent slots are available, do not claim image work; report that the controller is waiting for capacity. Existing workers continue even when no new slot is free. Never fall back to `create_thread` or user-visible tasks.
8. Recompute demand and free capacity each dispatch cycle. Do not shrink or stop existing workers merely because no new slot is free. Grow the pool only when unassigned products and free internal slots require it, then dispatch the next product to whichever existing subagent becomes idle.

## User-Selected Repair Dispatch

When a queued product contains `repair_pending` slots, dispatch it through the same whole-product lease, but require the worker to read each selected slot's `review_issue_code` and `review_note`. The worker processes only the explicitly selected slots. Never reopen or regenerate an unselected `accepted` slot, and never repeat the first-attempt grid workflow for a repair-only claim.

## Stop Gates

Treat every product state independently. A product waiting for manual review is a per-product waiting state, not a batch-wide stop gate. Continue dispatching other eligible `pending` or `repair_pending` products while any remain.

A missing SKU or subject gate blocks only that product. Report the blocked product and its required user action, then continue every other eligible queued product. A `stopped` product does not stop other eligible products. Report its persisted stop reason and recovery action. Never auto-resume a stopped product because the stop may have been requested by the user.

An already accepted slot is frozen evidence. If its receipt hash, source/output file hashes, and locked `visual_spec` verify, a console-rendering or display-metadata anomaly is not a product stop gate. Preserve that accepted slot and continue every remaining `pending` slot. Stop the product only when the frozen receipt or file actually fails cryptographic verification and the failure cannot be isolated to one non-accepted slot.

Stop dispatching only when no eligible `pending` or `repair_pending` products remain, a system-wide queue or evidence store failure prevents all remaining work, or the user stops the batch. When stopping, report separate counts for manual review, stopped, missing SKU/subject, failed, and completed products.

## Boundaries

- Never upload.
- Never modify business source code.
- Never bypass truth, SKU, subject, image-quality, or review gates.
- Never mark a product complete until its accepted outputs are written back to the current batch.
