---
name: ozon-product-media-generator
description: Use when an Ozon V2 image-queue product has a user-locked real 1688 SKU and one or more subject evidence images and needs a verified white-background subject, main images, supporting images, slot repair, or resumable Codex worker execution.
---

# Ozon Product Media Generator

## Core contract

Process one claimed product from the Ozon V2 SQLite image queue. Preserve the exact user-locked supplier SKU, all locked subject evidence images, and every evidence hash. First generate one reusable clean white-background subject from those images. Then return two main images and six supporting images to eight fixed slots, and stop for user review.

Use `imagegen` for bitmap generation. Use `scripts/ozon_image_worker.py` for queue ownership, deterministic grid cropping, result receipts, stop, resume, and final review handoff.

## Worker flow

1. Run as exactly one stable regular worker ID: `ozon-image-worker-01`, `ozon-image-worker-02`, `ozon-image-worker-03`, `ozon-image-worker-04`, or `ozon-image-worker-05`. Never use `ozon-image-worker-06`; that agent slot is reserved for recovery, diagnosis, or human intervention.
2. Claim one whole product. Do not work without the returned lease epoch.
3. Read the locked `subject_master_json`, selection hash, exact set quantity, exact set composition, all locked subject evidence images, and eight slot rows from the queue snapshot.
4. Read [prompt-contract.md](references/prompt-contract.md). Load [white-subject-prompt.txt](assets/white-subject-prompt.txt) and the matching slot prompt asset byte-for-byte.
5. Verify every locked evidence path and SHA-256. Use all locked subject evidence images to generate one reusable clean white-background subject. If the evidence conflicts about identity, exact quantity, or set composition, stop instead of guessing.
6. Verify the generated white-background subject against the receipt and all evidence images, including exact set quantity and set composition. Record its path and SHA-256 as a derived shape anchor.
7. Generate one horizontal 1x2 main grid with `imagegen`, using the generated white-background subject as the shape anchor and the locked supplier evidence as final product truth. Ozon images may guide composition only.
8. Save the raw grid locally, crop it with `crop-grid --layout 1x2`, validate both panels, and record `main_01` and `main_02` separately.
9. Generate two horizontal 1x3 supporting grids with the same verified subject anchor and evidence set. Crop each with `crop-grid --layout 1x3`, then record `detail_01` through `detail_06`.
10. Add only verified Russian copy after crop. Do not ask the image model to render text.
11. Freeze every accepted slot immediately. Repair only a failed slot with a single-image generation; allow at most two repairs per slot.
12. Renew the heartbeat during long generation. A stale lease must stop immediately without writing.
13. When all eight slots are accepted, call `ready-for-review`. Never upload or mark final approval from this Skill.

## Non-negotiable boundaries

- The supplier selection receipt is purchasing and product truth.
- Every locked subject evidence image must come from the confirmed supplier product and its SHA-256 must not change.
- The generated clean white-background subject is derived from evidence. It is a reusable shape anchor, not a new source of product facts.
- One visible subject means one sales unit. A four-piece SKU must show the same four-piece set in every image.
- Preserve exact count, color, shape, proportions, structure, parts, accessories, print, and set contents.
- Do not create a new bundle, substitute another SKU, invent accessories, or infer unsupported facts.
- Do not use an Ozon image as the product-shape anchor.
- Main images default to no text. Supporting copy must be verified Russian and added locally after crop.
- Do not overwrite an accepted slot or continue after stop, lease loss, subject-evidence mismatch, generated-subject mismatch, or receipt failure.
- Do not report progress unless a real queue claim, file, crop, receipt, or accepted slot exists.

## Fixed assets

- White-background subject: [white-subject-prompt.txt](assets/white-subject-prompt.txt)
- Main 1x2 grid: [main-grid-prompt.txt](assets/main-grid-prompt.txt)
- Supporting 1x3 grid: [detail-grid-prompt.txt](assets/detail-grid-prompt.txt)
- Single-slot repair: [repair-slot-prompt.txt](assets/repair-slot-prompt.txt)

Append only the task identity and verified facts allowed by the prompt contract. Do not rewrite the fixed bodies.
