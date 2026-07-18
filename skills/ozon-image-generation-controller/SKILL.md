---
name: ozon-image-generation-controller
description: Coordinate exactly five reusable Codex image worker tasks for one Ozon V2 batch while reserving one agent slot for recovery, diagnosis, or human intervention.
---

# Ozon Image Generation Controller

## Scope

Use this controller only after the user supplies an Ozon V2 `run_id`.
Read queued products from that batch. The controller supervises workers; it does not generate images itself.

## Worker Contract

1. Create exactly five regular worker tasks:
   - `ozon-image-worker-01`
   - `ozon-image-worker-02`
   - `ozon-image-worker-03`
   - `ozon-image-worker-04`
   - `ozon-image-worker-05`
2. Each worker must read and follow `skills/ozon-product-media-generator/SKILL.md`.
3. Reuse the same five worker tasks for every queued product.
4. Never create a sixth regular image worker. Keep one agent slot reserved for failure recovery, diagnosis, or human intervention.
5. Dispatch the next queued product to whichever existing worker becomes idle.

## Stop Gates

Stop dispatching when the queue is empty, a product requires manual review, a blocking gate is reached, or the user stops the batch.

## Boundaries

- Never upload.
- Never modify business source code.
- Never bypass truth, SKU, subject, image-quality, or review gates.
- Never mark a product complete until its accepted outputs are written back to the current batch.
