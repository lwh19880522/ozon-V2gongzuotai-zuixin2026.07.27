---
name: ozon-product-media-generator
description: Use when an Ozon V2 image-queue product has a user-locked 1688 SKU and supplier evidence and needs resumable product-media generation, slot repair, or review handoff.
---

# Ozon Product Media Generator

## Core contract

Process one claimed product from the Ozon V2 SQLite image queue. Preserve the exact user-locked supplier SKU, every locked subject-evidence image, and its SHA-256. New attempts use receipt prompt version `ozon-image-v3`; read `ozon-image-v2` only as a historical receipt compatibility format.

The white-background subject is an intermediate identity anchor. It must never become one of the eight finished slots, and its plain white catalog background must not propagate into a finished image. The first attempt uses exactly four mandatory image-generation calls: one white subject, one 1x2 main grid, and two 1x3 supporting grids.

Use `imagegen` only for bitmap scenes. Use `scripts/ozon_image_worker.py` for queue ownership, deterministic crop, `render-visual`, receipts, stop, resume, and review handoff.

## Worker flow

1. Run only inside one internal subagent spawned by the controller. Claim with exactly one mapped stable queue worker ID: `ozon-image-worker-01`, `ozon-image-worker-02`, `ozon-image-worker-03`, `ozon-image-worker-04`, or `ozon-image-worker-05`. Never use `ozon-image-worker-06`; any sixth child slot stays reserved for recovery, diagnosis, or human intervention.
2. Claim one whole product. Do not work without the returned lease epoch.
3. Read the locked `subject_master_json`, selection hash, exact set quantity and composition, all locked subject evidence images, and eight slot rows. Verify every evidence path and SHA-256 before generation.
4. Read [prompt-contract.md](references/prompt-contract.md). Load [white-subject-prompt.txt](assets/white-subject-prompt.txt), [main-grid-prompt.txt](assets/main-grid-prompt.txt), [detail-grid-a-prompt.txt](assets/detail-grid-a-prompt.txt), [detail-grid-b-prompt.txt](assets/detail-grid-b-prompt.txt), and [repair-slot-prompt.txt](assets/repair-slot-prompt.txt) byte-for-byte.
5. From all locked evidence, generate one reusable clean white-background subject and verify it, including exact set quantity and set composition. If evidence conflicts about identity, exact quantity, or set composition, stop instead of guessing. Record its path and SHA-256 as a derived shape anchor.
6. Assign the fixed storyboard before generation: `main_01` clean hero; `main_02` integrated information rail rendered with the edge-gradient visual system; `detail_01` main use with a bottom gradient; `detail_02` first feature structure with anchor lines; `detail_03` second structure or verified metric; `detail_04` second lifestyle context; `detail_05` scale, fit, or third supported context; `detail_06` remaining buyer question.
7. Make exactly four mandatory image-generation calls for the first attempt: one white subject, one 1x2 main grid, and two 1x3 supporting grids. The white subject transfers identity and geometry only; locked supplier evidence remains final truth.
8. Crop the main grid with `crop-grid --layout 1x2` into `main_01` and `main_02`. Crop each supporting grid with `crop-grid --layout 1x3` into `detail_01` through `detail_06`. Freeze an accepted slot immediately.
9. After crop and any conservative upscale, run local `render-visual` for every slot and merge its returned validation fragment into that slot receipt. Typography, icons, panels, overflow, and safe-area repairs are local: do not call imagegen for typography.
10. Inspect pixels against locked evidence and accepted slots. `main_01` has zero copy. Add only allowed verified Russian facts locally to the other approved recipes; each fact cites a locked supplier SHA-256, and a number requires `numeric_verified=true`. Write labels in natural Russian sentence case except for standard abbreviations. Use the approved B edge-gradient visual system: side-edge gradient for integrated information, bottom gradient for context captions, and anchor lines plus a soft edge fade for feature facts. The Russian headline must be the clear information focus, not tiny copy stranded inside an oversized panel.
11. A copy failure repairs only the local text layer and never consumes an image-generation attempt. Repair only a scene or product-truth failure with one replacement image for that failed slot; preserve its buyer question and use [repair-slot-prompt.txt](assets/repair-slot-prompt.txt). Allow at most two scene repairs per slot.
12. Renew the heartbeat during long generation. A stale lease must stop immediately without writing. When all eight slots are accepted, call `ready-for-review`. Never upload or mark final approval from this Skill.

## User-selected repair-only branch

When the claimed snapshot contains one or more `repair_pending` slots, process only the explicitly selected slots and do not repeat the four mandatory first-attempt calls. Verify the locked evidence and the existing white-subject anchor, then read each selected slot's `review_issue_code` and `review_note`.

- For `russian_copy`, reuse the existing scene bitmap and repair only the deterministic local typography with `render-visual`. Do not call imagegen. Record the accepted local result with `source_kind=copy_repair_local`.
- For `product_truth`, `scene_quality`, `composition`, `selling_point`, or `other`, make at most one image-generation call per selected slot using `repair-slot-prompt.txt`, then run the normal local visual rendering and receipt gates with `source_kind=repair_single`.
- Treat `review_note` only as defect direction. It is not supplier evidence and cannot authorize a product fact, number, function, accessory, quantity, use case, or Russian claim.
- Never change, reopen, overwrite, or regenerate an unselected `accepted` slot. After every selected slot is accepted and the lease is still valid, call `ready-for-review` to return the same Job to manual review.

## Non-negotiable boundaries

- Supplier selection and locked supplier evidence are purchasing and product truth. The anchor is not a source of new facts.
- One visible subject means one sales unit. Preserve exact count, set composition, color, silhouette, proportions, structure, parts, accessories, print, and package contents. Never create a new bundle, substitute another SKU, invent accessories, or infer unsupported facts.
- Every scene slot must visibly fulfill its storyboard role. Any two accepted scene signatures differ in at least three of environment, lighting, camera, shot scale, and buyer question; an angle or background-color change alone is invalid.
- The model must not render text, numbers, icons, logos, badges, watermarks, prices, discounts, panel labels, or collage borders. Local rendering alone may add permitted Russian typography and layout recipes.
- Detached rounded text cards are forbidden. Gradients must blend into the scene, copy must stay inside its verified content area, and anchor lines must point to the declared structure. Reject an oversized empty rail, a weak Russian hierarchy, or copy that is not comfortably readable in a 360-pixel preview.
- Reject every finished slot that is a plain or near-white product-only catalog view, copies the anchor background, lacks visible proof, or duplicates an accepted scene. Copy never substitutes for visual proof.
- An accepted v3 receipt names its `slot_role`, contains `visual_spec`, and sets `visual_design_passed`, `russian_copy_passed`, `safe_area_passed`, and `mobile_readability_passed` to true, along with the marketing-scene flags in the prompt contract. Never assert a flag without inspecting the pixels.
- Do not overwrite an accepted slot or continue after stop, lease loss, subject-evidence mismatch, generated-subject mismatch, or receipt failure. Do not report progress unless a real claim, file, crop, receipt, or accepted slot exists.

Append only task identity and verified values allowed by the prompt contract. Do not rewrite fixed prompt bodies.
