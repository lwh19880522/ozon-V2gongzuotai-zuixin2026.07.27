---
name: ozon-product-media-generator
description: Use when the Ozon V2 workbench has emitted image task packages that must be processed in a single-thread two-phase batch with a frozen white-background identity anchor, all raw 4x2 grids generated before deterministic 3:4 cropping begins, deferred product binding when necessary, an automatically managed free public media gateway, and direct Ozon gallery and video upload after binding.
---

# Ozon Product Media Generator

## Core contract

This is the only Ozon workbench image-generation skill. Run the complete flow
single-threaded in the current task with a hard batch barrier: generate and
checkpoint every raw 4x2 grid in the oldest batch first, then crop and finish
those staged grids one at a time. Never create parallel or delegated generation
tasks. Claim one package at a time, and never crop a grid while that batch still
has a product awaiting raw-grid generation.

Read generation packages from `image_tasks/pending`; staged raw grids live in
`image_tasks/grid_ready`; claimed packages live in `image_tasks/in_progress`.
Use `scripts/ozon_image_task_inbox.py` for claiming, raw-grid staging,
automatic public media, slideshow creation and direct Ozon upload. A normal
package command is `RUN package_id=<id>` and its `assignment.phase` is authoritative.

For a new product, make exactly two image-generation calls:

1. Use all locked subject evidence images to generate one reusable clean white-background subject.
   Preserve the exact set quantity and set composition, then verify and freeze
   it as the identity anchor.
2. Use that frozen anchor as the only image input to generate one 4x2 grid
   containing all eight finished gallery panels.

The white anchor is persistent and immutable. After verification, every
finished-grid and selected scene-repair call must use the exact one-item array
`referenced_image_paths=[<same verified white anchor path>]`. The frozen file
is Reference Image 1, the only product-identity source and the only image input.
Do not attach Reference Image 2, Ozon images, supplier evidence images,
unselected variants, prior scenes, or conversation images. Do not use
`num_last_images_to_include`.

## Sequential batch flow

1. Run `scripts/ozon_image_task_inbox.py --runtime-root <runtime> media-start`.
   It must automatically start a local read-only media server and a free anonymous Cloudflare Quick Tunnel.
   User-supplied buckets, domains, public URLs, accounts, tokens and upload
   channels are not part of this workflow. If automatic startup fails,
   claim nothing; report the local gateway error.
2. Read status and run `claim-next`. Process only the returned package and obey
   `assignment.phase`. One active package at a time remains a hard single-thread
   rule, while the inbox enforces the batch-wide generation/crop barrier.
3. Require `schema_version=3`, `status=in_progress`, `slot_count=8`,
   `aspect_ratio=3:4`, `generation_mode=single_thread_8_grid`,
   `grid_layout=4x2`, `public_media=auto_quick_tunnel`,
   `direct_ozon_upload=true`, `return_to_workbench=false`, and required video
   and cover fields. Require a positive `seller_import_task_id`. The store
   target must be either `binding_state=bound` with a positive `product_id`, or
   `binding_state=awaiting_ozon_product` with no product ID yet. An unbound
   package is still a valid generation task, but it cannot publish until the
   workbench later binds the exact Ozon product.
4. Require the identity contract to declare `required=true`,
   `source=generated_white_anchor`, `reference_index=1`, `reference_count=1`,
   `additional_image_references_allowed=false`,
   `reuse_for_all_finished_calls=true`, `reuse_for_repairs=true`,
   `product_identity_source=white_anchor_only`, and
   `composition_source=fixed_skill_prompt_only`.
5. Verify the locked `subject_master`, selected SKU receipt, selection hash,
   exact sales unit, quantity, set composition, every local evidence file and
   every SHA-256. Do not fetch or materialize Ozon gallery images.
6. Read [prompt-contract.md](references/prompt-contract.md). Load
   [white-subject-prompt.txt](assets/white-subject-prompt.txt),
   [ozon-commercial-infographic-core-prompt.txt](assets/ozon-commercial-infographic-core-prompt.txt),
   [gallery-8-grid-prompt.txt](assets/gallery-8-grid-prompt.txt), and
   [repair-slot-prompt.txt](assets/repair-slot-prompt.txt) byte-for-byte.
   Prepend the shared core to the gallery and scene-repair wrapper, never to
   the white-anchor call.
7. Generate the white anchor from all locked selected-SKU evidence. Preserve
   exact identity, quantity and composition. Reject changed silhouette,
   proportions, geometry, color, material appearance, structure, parts,
   accessories, print, markings or package contents. Freeze its absolute path
   and SHA-256 for the whole package. Reuse every hash-verified checkpoint.
8. Build one prompt-only blueprint for each of `main_01`, `main_02`,
   `detail_01` through `detail_06`. Keep the existing storyboard, evidence-safe
   Russian copy and visual-diversity rules from the prompt contract.
9. Phase A, raw-grid generation: when `assignment.phase=grid_generation`,
   heartbeat, generate or reuse the frozen white anchor, then make one blocking
   gallery call for that product at a 3:2 whole-canvas ratio using only
   `referenced_image_paths=[<frozen anchor>]`. Save the untouched raw grid and
   run `scripts/ozon_image_task_inbox.py --runtime-root <runtime> stage-grid
   --package <id> --grid <absolute-grid-path> --white-anchor <absolute-anchor-path>`.
   This records both absolute paths and SHA-256 values and moves the package to
   `image_tasks/grid_ready`. Do not crop it. Claim the next package and continue
   Phase A until no product in the current oldest batch remains in raw-grid generation.
10. Phase B, batch crop: only after Phase A has staged every eligible product,
    `claim-next` may return `assignment.phase=grid_crop`. Do not crop any grid until every eligible product
    in the current batch has a staged raw 4x2 grid.
    Re-verify the package ID, run ID, seed ID, frozen-anchor path and hash, and
    raw-grid path and hash before running
    `scripts/ozon_image_worker.py crop-grid --layout 4x2`. Map row-major crops
    to the eight fixed slots. Cropping must not stretch a panel; each output
    remains exact 3:4. Finish staged grids one at a time without starting a newer batch.
11. Inspect every crop against the locked 1688 truth and the frozen anchor.
    Reject any panel whose subject identity, silhouette, proportions,
    geometry, quantity, parts, structure, color, material appearance, selected
    variant, accessories, or markings changed. A visually attractive but
    changed product is invalid. Never change the anchor to make a panel pass.
12. Validate each panel's role, distinct composition, safe area, readable
    Russian copy and evidence-backed claims. Every accepted receipt records
    `ozon-image-v9`, the frozen anchor path and hash,
    `identity_anchor_reference_index=1`, `identity_anchor_attached=true`,
    `identity_anchor_reused=true`, `image_reference_count=1`,
    `additional_image_references_attached=false`,
    `locked_subject_preserved=true`, and
    `product_identity_source=white_anchor_only`.
13. A copy-only defect uses deterministic local typography. For a selected
    scene or product-truth repair, regenerate only that failed 3:4 slot with
    the same sole frozen white anchor. Never change the anchor, attach another
    image, or modify an unselected accepted slot. Allow at most two scene
    repairs per slot.
14. Inspect all eight accepted files with `view_image`. Build `slideshow.mp4`
    and `video_cover.jpg` locally. Run `upload-gallery`. For a bound package it
    publishes through the active automatic gateway, replaces the exact Ozon
    product gallery and submits video fields. For an unbound package the same
    command records all generated media and moves the package to
    `image_tasks/awaiting_product` without treating it as failed.
15. Claim the next staged grid from the same oldest batch and repeat Phase B.
    A package
    rebound by the workbench returns to `pending` with `resume_mode=upload_only`;
    reuse its recorded media and call `upload-gallery` without regenerating or
    recropping. The inbox may return `assignment.phase=upload_only` for it.
    End the pass only when `pending` = 0, `grid_ready` = 0 and `in_progress` = 0,
    then run `media-stop`. `awaiting_product` packages may resume later after
    exact Ozon binding.

## Repair and stop gates

For `repair_pending`, process only the explicitly selected slots. Read each
`review_issue_code` and `review_note`. Never reopen or regenerate an unselected `accepted` slot.

A missing SKU or subject gate blocks only that product. Fail or release it with
the exact reason, then continue sequentially with the next eligible package.
Never auto-resume a stopped product. A gateway-wide, inbox-wide or evidence
store failure may stop the batch; a single product failure may not.

## Boundaries

- Locked selected 1688 SKU evidence is product truth; the white anchor is a
  derived immutable identity image and cannot introduce new facts.
- Upload only the eight validated images, slideshow and cover to the resolved
  product. This directly replaces the product gallery in Ozon.
- The skill never returns generated files to the workbench and never edits
  price, inventory, business attributes, source code or final approval.
- Never bypass SKU, subject, identity, truth, image-quality or review gates.
