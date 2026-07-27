---
name: ozon-product-media-generator
description: Use when a claimed Ozon V2 post-product task package needs resumable product-media generation, R2 publication, slideshow video creation, and direct Ozon media upload.
---

# Ozon Product Media Generator

## Core contract

Process one owned package from `image_tasks/in_progress`. Preserve the exact
user-locked 1688 SKU, selected quantity, set composition, subject evidence, and
SHA-256 values. The workbench has already created the Ozon product with one
locked 1688 original. This Skill generates eight final images, a scrolling
slideshow video and cover, publishes them through the configured R2 channel,
and uploads that media directly to the same Ozon product.

New attempts use `ozon-image-v8`; versions v2-v7 are historical receipt
compatibility only. The first attempt uses the fixed 2+3+3 structure and exactly
four mandatory image-generation calls:

1. one verified white-background subject anchor;
2. one horizontal 1x2 grid for `main_01` and `main_02`;
3. one horizontal 1x3 grid for `detail_01` through `detail_03`;
4. one horizontal 1x3 grid for `detail_04` through `detail_06`.

The white anchor is persistent and immutable. After it is verified, every
finished-grid call and every scene-repair call must use the exact one-item array
`referenced_image_paths=[<same verified white anchor path>]`. That file is
Reference Image 1, the only image input, and the only product-identity source.
Do not attach Reference Image 2, Ozon product images, supplier evidence images,
unselected variants, previous generated scenes, or conversation images. Do not
use `num_last_images_to_include`.

Use `reference_mapping_version=none` and
`guidance_mode=fixed_prompt_white_anchor`. The fixed commercial prompt and
eight slot blueprints provide all composition, camera, scene, lighting,
negative-space, information-hierarchy, and Russian-copy direction. Use
`imagegen` only for bitmap scenes. Use `scripts/ozon_image_worker.py` for crop,
local copy repair, checkpoints, and slot receipts. Use
`scripts/ozon_image_task_inbox.py` for ownership, R2 publication, slideshow,
and direct Ozon media upload.

## Worker flow

1. Run only inside one of the controller's ten fixed reusable tasks
   `ozon-image-worker-01` through `ozon-image-worker-10`. Accept only
   `RUN package_id=<id> worker_id=<id>`. One task processes one package at a
   time and remains available for reuse. Never create another task.
2. Load exactly that package from `image_tasks/in_progress`. Require
   `schema_version=2`, an active matching assignment, `status=in_progress`,
   `slot_count=8`, `aspect_ratio=3:4`, `direct_ozon_upload=true`,
   `return_to_workbench=false`, `r2_preflight_required=true`,
   `video.required=true`, and `video_cover.required=true`.
3. Require the package identity contract to contain:
   `required=true`, `source=generated_white_anchor`, `reference_index=1`,
   `reference_count=1`, `additional_image_references_allowed=false`,
   `reuse_for_all_finished_calls=true`, `reuse_for_repairs=true`,
   `product_identity_source=white_anchor_only`, and
   `composition_source=fixed_skill_prompt_only`. A mismatch makes no package
   state change.
4. Before any image-generation call, run
   `scripts/ozon_image_task_inbox.py --runtime-root <runtime> media-preflight`.
   The user must provide and save the public R2 base URL. Never invent it. If
   preflight fails, release the package with
   `public_media_channel_required`; generate and upload nothing.
5. Verify the package's locked `subject_master`, selection hash, exact sales
   unit, exact set quantity and composition, every local 1688 evidence path,
   and every SHA-256. The package intentionally contains no Ozon
   reference-image list. Do not fetch or materialize Ozon gallery images.
6. Read [prompt-contract.md](references/prompt-contract.md). Load
   [white-subject-prompt.txt](assets/white-subject-prompt.txt),
   [ozon-commercial-infographic-core-prompt.txt](assets/ozon-commercial-infographic-core-prompt.txt),
   [main-grid-prompt.txt](assets/main-grid-prompt.txt),
   [detail-grid-a-prompt.txt](assets/detail-grid-a-prompt.txt),
   [detail-grid-b-prompt.txt](assets/detail-grid-b-prompt.txt), and
   [repair-slot-prompt.txt](assets/repair-slot-prompt.txt) byte-for-byte.
   Prepend the shared core byte-for-byte to finished-grid and scene-repair
   wrappers, never to the white-anchor call.
7. Use all locked subject evidence images to generate one reusable clean white-background subject
   from the selected 1688 SKU. Preserve the exact set quantity and set composition.
   If its hash-verified checkpoint exists, reuse
   it. Stop only when the locked selected-SKU evidence itself is missing or
   internally contradictory. Freeze the anchor path and SHA-256 for the whole
   package.
8. Write one prompt-only blueprint per slot with
   `guidance_mode=fixed_prompt_white_anchor`,
   `reference_mapping_version=none`, a supported `layout_archetype`,
   `core_theme`, `subject_position`, `subject_scale`, `camera_family`,
   `shot_scale`, `background_family`, `lighting_family`, `negative_space`,
   `copy_zone`, `headline_alignment`, `annotation_style`,
   `copy_mode=imagegen_integrated`, one verified 3-7-word Russian headline,
   one verified Russian subtitle of at most 14 words, and 2-4 verified Russian
   functional labels. Ozon reference fields and reference-slot fields are
   null; `reference_reused=false`.
9. Use the fixed storyboard: `main_01` core selling point; `main_02` real-use
   value; `detail_01` real-use demonstration; `detail_02` feature or mechanism;
   `detail_03` material or structure; `detail_04` instruction or result;
   `detail_05` dimension, fit, target user, or scale; `detail_06` set contents,
   care, storage, or another proved buyer question. Use at least six layout
   archetypes across the gallery; changing only background or angle is invalid.
10. Make the four mandatory calls. Request the 1x2 grid at 3:2 and each 1x3
    grid at 9:4 so every cropped panel is 3:4. Every finished call uses only
    `referenced_image_paths=[<same frozen anchor>]`. Include each panel's exact
    verified Russian copy in the first scene-generation prompt. Never consume a
    second image-generation call merely to add labels. If a scene would change
    the anchor shape, simplify the scene.
11. Heartbeat immediately before each blocking generation call. Immediately
    checkpoint every result; crop grids atomically with `crop-grid --layout
    1x2` or `1x3`. Reuse every hash-verified checkpoint on resume.
12. Inspect every cropped panel against locked 1688 truth, the frozen anchor,
    its prompt-only blueprint, and accepted slots. Reject changed identity,
    silhouette, geometry, quantity, parts, color, material appearance,
    selected variant, unsupported facts, unreadable Russian copy, wrong 3:4
    ratio, plain white catalog treatment, or duplicate scene structure.
13. A copy-only defect uses local deterministic `render-visual`; it never
    consumes another scene-generation call. A scene or product-truth repair
    may regenerate only the failed slot, again using the exact one-item anchor
    array. Never change the anchor or attach a second image. Allow at most two
    scene repairs per slot.
14. Each accepted v8 receipt records the exact 3:4 output, `ozon-visual-v2`,
    integrated Russian copy, `reference_mapping_version=none`,
    `guidance_mode=fixed_prompt_white_anchor`, supported `layout_archetype`,
    `image_reference_count=1`,
    `additional_image_references_attached=false`, empty Ozon reference fields,
    `reference_reused=false`, `locked_subject_preserved=true`, the frozen
    anchor path and hash, `identity_anchor_reference_index=1`,
    `identity_anchor_attached=true`, `identity_anchor_reused=true`, and
    `product_identity_source=white_anchor_only`. All eight receipts use the
    same anchor path and hash.
15. After all eight slots pass, inspect all eight accepted files with
    `view_image`. Build `slideshow.mp4` in slot order and `video_cover.jpg`
    locally from `main_01`; this makes no image-generation call.
16. Run `upload-gallery` with the eight images, slideshow, and cover. It
    rechecks R2, publishes the ten media files, replaces the Ozon gallery, and
    submits video fields. Complete the package only after both Ozon submissions
    are accepted. Never return generated files to the workbench.

## User-selected repair-only branch

Only explicitly selected `repair_pending` slots may change. A `russian_copy`
request uses local typography only. `product_truth`, `scene_quality`,
`composition`, `selling_point`, or `other` may make one replacement generation
per selected slot with the same sole white anchor. A review note is defect
direction, never product evidence. Never reopen an unselected accepted slot.

## Non-negotiable boundaries

- Locked selected 1688 SKU evidence is purchasing and product truth. The white
  anchor is a derived identity image, not a source of new facts.
- Ozon reference images are not inputs. Do not fetch, inspect, materialize, or
  attach them.
- Preserve exact sales unit, count, set composition, color, silhouette,
  proportions, structure, parts, accessories, print, and package contents.
- All finished images are exact 3:4 portrait, visually distinct, commercial
  scenes or infographics with first-pass natural Russian copy.
- Do not overwrite an accepted slot or continue after lease loss, evidence
  mismatch, anchor mismatch, or receipt failure.
- Upload only the complete eight-image gallery, slideshow video, and cover for
  the resolved Ozon product. Never edit price, inventory, business attributes,
  or final approval.

Append only task identity and verified values allowed by the prompt contract.
Do not rewrite fixed prompt bodies.
