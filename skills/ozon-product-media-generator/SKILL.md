---
name: ozon-product-media-generator
description: Use when a claimed Ozon V2 post-product task package needs resumable product-media generation and direct Ozon gallery replacement.
---

# Ozon Product Media Generator

## Core contract

Process one owned package from `image_tasks/in_progress`. Preserve the exact
user-locked supplier SKU, every locked subject-evidence image, and its SHA-256.
The workbench has already created the Ozon product with one locked 1688 original;
this Skill generates the final gallery and replaces that temporary gallery
directly in Ozon. New attempts use receipt prompt version `ozon-image-v4`; read
`ozon-image-v2` and `ozon-image-v3` only as historical receipt compatibility
formats.

The white-background subject is an intermediate identity anchor. It must never become one of the eight finished slots, and its plain white catalog background must not propagate into a finished image. The first attempt uses exactly four mandatory image-generation calls: one white subject, one 1x2 main grid, and two 1x3 supporting grids.

Use `reference_mapping_version=ozon-reference-map-v1`. Before generating finished scenes, bind one `primary_ozon_reference` to every slot from `main_01` through `detail_06`. The complete first-attempt visual bundle is one white identity anchor, eight slot-specific Ozon references, and the locked supplier evidence used for truth validation. The white anchor fixes the target product; each Ozon reference supplies only that slot's composition, camera, background, lighting, negative space, and information hierarchy.

Use `imagegen` only for bitmap scenes. Use `scripts/ozon_image_worker.py` for
deterministic crop, `render-visual`, and slot receipts. Use
`scripts/ozon_image_task_inbox.py` for package ownership and the final public
media plus direct Ozon gallery replacement.

## Worker flow

1. Run only inside one of the controller's ten fixed user-visible Codex work tasks: `ozon-image-worker-01`, `ozon-image-worker-02`, `ozon-image-worker-03`, `ozon-image-worker-04`, `ozon-image-worker-05`, `ozon-image-worker-06`, `ozon-image-worker-07`, `ozon-image-worker-08`, `ozon-image-worker-09`, and `ozon-image-worker-10`. Accept only `RUN package_id=<id> worker_id=<id>`. One task processes one package at a time. Do not create another task; after completion, remain available for reuse.
2. Load exactly that package from `image_tasks/in_progress` with `scripts/ozon_image_task_inbox.py`. Verify that `assignment.worker_id`, `package_id`, `status=in_progress`, `generation_contract.slot_count=8`, `aspect_ratio=3:4`, `direct_ozon_upload=true`, and `return_to_workbench=false` all match. If any value differs, make no package state change and report the mismatch.
3. Read the package's locked `subject_master`, selection hash, exact set quantity and composition, all locked subject evidence images, and ordered Ozon references. Verify every local evidence path and SHA-256 before generation. Run `materialize-references` before using any Ozon gallery URL: upgrade thumbnail URLs, decode the pixels, reject any image whose shorter edge is below 512 pixels, pixel-deduplicate it, and use only the ordered local paths and hashes recorded in `reference_manifest.json`. Stop before image generation when that manifest is not ready.
4. Read [prompt-contract.md](references/prompt-contract.md). Load [white-subject-prompt.txt](assets/white-subject-prompt.txt), [main-grid-prompt.txt](assets/main-grid-prompt.txt), [detail-grid-a-prompt.txt](assets/detail-grid-a-prompt.txt), [detail-grid-b-prompt.txt](assets/detail-grid-b-prompt.txt), and [repair-slot-prompt.txt](assets/repair-slot-prompt.txt) byte-for-byte.
5. From all locked evidence, generate one reusable clean white-background subject and verify it, including exact set quantity and set composition. If evidence conflicts about identity, exact quantity, or set composition, stop instead of guessing. Record its path and SHA-256 as a derived shape anchor.
6. Read Ozon references only from `reference_manifest.json` without changing gallery order. Exclude a reference only when the materializer or pixel inspection proves that it cannot load, is pixel-identical to an earlier reference, is not product media, or has no legible composition. Map the first eight valid references in order to `main_01`, `main_02`, then `detail_01` through `detail_06`. When fewer than eight valid references exist, choose the closest role-compatible reference for each remaining slot; the same reference may be bound to at most two slots and must set `reference_reused=true`.
7. Before generation, write one composition blueprint per slot with `reference_path`, `reference_sha256`, `subject_position`, `subject_scale`, `camera_family`, `shot_scale`, `background_family`, `lighting_family`, `negative_space`, and `copy_zone`. Assign the fixed storyboard: `main_01` clean hero; `main_02` integrated information rail rendered with the edge-gradient visual system; `detail_01` main use with a bottom gradient; `detail_02` first feature structure with anchor lines; `detail_03` second structure or verified metric; `detail_04` second lifestyle context; `detail_05` scale, fit, or third supported context; `detail_06` remaining buyer question.
8. Make exactly four mandatory image-generation calls for the first attempt: one white subject, one 1x2 main grid, and two 1x3 supporting grids. Every finished slot is exactly 3:4 portrait. Therefore request the whole 1x2 grid at 3:2 and each whole 1x3 grid at 9:4; never request a square grid or square panel. Pass the white anchor plus each panel's mapped Ozon reference in panel order. Label the mapping explicitly so references never bleed across panels. The white subject transfers identity and geometry only; locked supplier evidence remains final truth. Send a 30-minute heartbeat immediately before every blocking image-generation call. Run `checkpoint-asset` immediately after each image-generation call; for grids this command must crop the panels at once and atomically record their paths, dimensions, and hashes.
9. Crop the main grid with `crop-grid --layout 1x2` into `main_01` and `main_02`. Crop each supporting grid with `crop-grid --layout 1x3` into `detail_01` through `detail_06`. The deterministic crop rejects panels outside the near-3:4 input tolerance and normalizes only a small framing deviation to exact 3:4. Freeze an accepted slot immediately.
10. After crop and any conservative upscale, run local `render-visual` for every slot and merge its returned validation fragment into that slot receipt. Typography, icons, panels, overflow, and safe-area repairs are local: do not call imagegen for typography.
11. Inspect pixels against locked evidence, the slot's primary Ozon reference, and accepted slots. Confirm the locked product remains exact while the scene visibly follows the mapped reference's composition blueprint. Reject an output that copies the reference product, brand, text, watermark, price, unsupported accessory, or unsupported function. `main_01` has zero copy. Add only allowed verified Russian facts locally to the other approved recipes; each fact cites a locked supplier SHA-256, and a number requires `numeric_verified=true`. `detail_01` and `detail_04` keep one fact block. `detail_02`, `detail_03`, `detail_05`, and `detail_06` may use two only when the pixels visibly support two distinct, non-overlapping verified facts; otherwise keep one. Write labels in natural Russian sentence case except for standard abbreviations. Use the approved B edge-gradient visual system: side-edge gradient for integrated information, bottom gradient for context captions, and anchor lines plus a soft edge fade for feature facts. The Russian headline must be the clear information focus, not tiny copy stranded inside an oversized panel.
12. A copy failure repairs only the local text layer and never consumes an image-generation attempt. Repair only a scene or product-truth failure with one replacement image for that failed slot; preserve its buyer question and original `primary_ozon_reference`, and use [repair-slot-prompt.txt](assets/repair-slot-prompt.txt). Change the reference only when the user explicitly requests it. Allow at most two scene repairs per slot.
13. At claim or resume, run `checkpoint-status` and reuse every hash-verified checkpoint instead of making a duplicate image-generation call. Renew the package heartbeat before each long generation call and after each returned result. A stale ownership record must stop immediately without writing. When all eight slots are accepted, use `view_image` on all eight accepted_path files. raw imagegen grids are never the final preview. Confirm that every supporting/detail accepted image visibly contains its locally rendered Russian labels; `main_01` remains the intentional zero-copy hero.
14. Invoke `scripts/ozon_image_task_inbox.py ... upload-gallery` with the eight accepted local files in slot order. The command validates exact 3:4 pixels, publishes the files to the configured public media channel, resolves the package's `seller_import_task_id` to `product_id` when needed, and calls `SellerApiAdapter.replace_product_pictures` with all eight ordered public image URLs. Mark the package complete only after Ozon accepts that gallery. This is direct Ozon gallery replacement: never return generated files to the workbench and never wait for a workbench callback.

## User-selected repair-only branch

When a user explicitly requeues a failed package with one or more
`repair_pending` slots, process only the explicitly selected slots and do not repeat the four mandatory first-attempt calls. Verify the locked evidence and
the existing white-subject anchor, then read each selected slot's
`review_issue_code` and `review_note`.

- For `russian_copy`, reuse the existing scene bitmap and repair only the deterministic local typography with `render-visual`. Do not call imagegen. Record the accepted local result with `source_kind=copy_repair_local`.
- For `product_truth`, `scene_quality`, `composition`, `selling_point`, or `other`, make at most one image-generation call per selected slot using `repair-slot-prompt.txt`, then run the normal local visual rendering and receipt gates with `source_kind=repair_single`.
- Treat `review_note` only as defect direction. It is not supplier evidence and cannot authorize a product fact, number, function, accessory, quantity, use case, or Russian claim.
- Never change, reopen, overwrite, or regenerate an unselected `accepted` slot. After every selected slot is accepted and the lease is still valid, call `ready-for-review` to return the same Job to manual review.

## Non-negotiable boundaries

- Supplier selection and locked supplier evidence are purchasing and product truth. The anchor is not a source of new facts.
- Ozon references are slot-specific composition blueprints only. Never use their product identity, text, brand, price, package contents, accessories, or claims as target-product evidence, and never blend visual directions across slots.
- One visible subject means one sales unit. Preserve exact count, set composition, color, silhouette, proportions, structure, parts, accessories, print, and package contents. Never create a new bundle, substitute another SKU, invent accessories, or infer unsupported facts.
- Every scene slot must visibly fulfill its storyboard role. Any two accepted scene signatures differ in at least three of environment, lighting, camera, shot scale, and buyer question; an angle or background-color change alone is invalid.
- Across all eight slots use at least five evidence-safe environment families, four lighting treatments, four camera/composition families, and three shot scales. One environment family may appear at most twice. The final queue gate rejects pixel-identical and visually near-duplicate outputs even when their written scene labels differ.
- Use this commercial storyboard: `main_01` clean identity hero; `main_02` real-use value overview; `detail_01` use result; `detail_02` mechanism or key structure; `detail_03` material/build detail; `detail_04` a second evidence-safe use context; `detail_05` scale, fit, or compatibility; `detail_06` set contents, care, storage, or another evidence-backed buyer question. If evidence cannot support a listed role, substitute a different provable buyer question rather than inventing a claim.
- The model must not render text, numbers, icons, logos, badges, watermarks, prices, discounts, panel labels, or collage borders. Local rendering alone may add permitted Russian typography and layout recipes.
- Every accepted finished bitmap is exact 3:4 portrait at the pixel level. A square, landscape, or other aspect ratio is a hard failure and must never reach review, public media, or Ozon upload.
- Detached rounded text cards are forbidden. Gradients must blend into the scene, copy must stay inside its verified content area, and anchor lines must point to the declared structure. Reject an oversized empty rail, a weak Russian hierarchy, or copy that is not comfortably readable in a 360-pixel preview.
- Reject every finished slot that is a plain or near-white product-only catalog view, copies the anchor background, lacks visible proof, or duplicates an accepted scene. Copy never substitutes for visual proof.
- An accepted v4 receipt names its `slot_role`, contains `visual_spec`, proves an exact 3:4 output, records `reference_mapping_version=ozon-reference-map-v1`, `primary_ozon_reference_path`, `primary_ozon_reference_sha256`, `reference_slot_index`, `reference_reused`, `reference_composition_followed`, and `locked_subject_preserved`, and sets `visual_design_passed`, `russian_copy_passed`, `safe_area_passed`, and `mobile_readability_passed` to true, along with the marketing-scene flags in the prompt contract. The reference path must be a decoded local file of at least 512 pixels on its shorter edge and its live hash must equal the receipt hash. Never assert a flag without inspecting the pixels.
- Do not overwrite an accepted slot or continue after stop, lease loss, subject-evidence mismatch, generated-subject mismatch, or receipt failure. Do not report progress unless a real claim, file, crop, receipt, or accepted slot exists.
- Upload only the complete ordered eight-image gallery for the package's resolved
  Ozon product. Never create or edit product fields, price, inventory, or final
  approval from this Skill.
- Store generation, publication, and Ozon picture-import receipts under
  `image_tasks`; never return generated files to the workbench.

Append only task identity and verified values allowed by the prompt contract. Do not rewrite fixed prompt bodies.
