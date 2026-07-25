# Prompt Contract

## Visual contract v4

New scenes and receipts use `ozon-image-v4` with `ozon-visual-v1`; `ozon-image-v2` and `ozon-image-v3` are read-only historical receipt compatibility. A first attempt makes exactly four image-generation calls: one white identity anchor, one 1x2 main grid, and two 1x3 supporting grids. Every finished slot is exact 3:4 portrait, so the complete 1x2 grid is 3:2 and each complete 1x3 grid is 9:4. The anchor is identity and geometry only and must never be an accepted finished slot.

The fixed slots are `main_01` clean_hero with zero copy; `main_02` integrated_rail with at most two verified Russian fact blocks; and supporting slots using only their allowed `ozon-visual-v1` recipes. Every fact cites a locked supplier SHA-256. Any numeric fact requires `numeric_verified=true`. The image model renders no text, numbers, icons, logos, badges, prices, or discounts.

The deterministic crop accepts only near-3:4 source panels, center-crops a small framing deviation to exact 3:4, and rejects square, landscape, or materially wrong panels. After crop and any conservative upscale, preserve exact 3:4, run local `render-visual`, and merge its validation fragment into the slot receipt. A copy failure repairs only the local text layer and never consumes an image-generation attempt. Scene or product-truth failure alone may use a single-slot image repair.

The local layout must use a prominent Russian headline, supporting detail with an obvious visual hierarchy, and the approved B edge-gradient visual system. `integrated_rail` and `metric_panel` use a soft side-edge gradient; `context_caption` uses a bottom gradient; `feature_callout` uses accent anchor lines with a soft edge fade behind its text. Do not use detached rounded text cards. Validate the rendered type at a 360-pixel preview: the smallest Russian copy must remain at least 13 pixels. Preserve natural Russian sentence case except for standard abbreviations. The receipt must record the actual headline, detail, projected mobile pixel sizes, and `visual_system=ozon-edge-gradient-b1`; a hard-coded pass flag without those measurements is invalid.

Copy density is slot-specific: `detail_01` and `detail_04` remain at one verified fact block. `detail_02`, `detail_03`, `detail_05`, and `detail_06` may use two verified fact blocks only when the scene visibly proves both and the local renderer keeps them non-overlapping and mobile-readable. Never add a second label merely to fill space.

Any two accepted scene slots differ in at least three of environment, lighting, camera, shot scale, and buyer question. An accepted v4 receipt includes its complete `visual_spec`, exact 3:4 output dimensions, `visual_contract_version=ozon-visual-v1`, and these four true pass flags: `visual_design_passed`, `russian_copy_passed`, `safe_area_passed`, and `mobile_readability_passed`.

The full set uses at least five environment families, four lighting treatments, four camera/composition families, and three shot scales; one environment family appears no more than twice. Pixel-identical or visually near-duplicate finished slots fail the set gate regardless of their declared scene signatures.

## Slot-specific Ozon reference mapping

Use `reference_mapping_version=ozon-reference-map-v1` and one primary Ozon reference per finished slot. First run `materialize-references`; only `reference_manifest.json` accepted entries may be mapped. A valid reference has been upgraded from an Ozon thumbnail URL when available, decoded locally, has a shorter edge of at least 512 pixels, is not pixel-identical to an earlier reference, contains recognizable product media, and has a legible composition. Bind valid references in gallery order to `main_01` through `detail_06`. With eight or more valid references, every slot uses a different reference. With fewer than eight, select the closest role-compatible reference for each gap; the same reference may be bound to at most two slots and the receipt sets `reference_reused=true`.

One slot learns from one primary reference. Do not blend the composition, camera, lighting, background, or negative-space direction of multiple Ozon references into one slot. Before image generation, record `reference_path`, `reference_sha256`, `subject_position`, `subject_scale`, `camera_family`, `shot_scale`, `background_family`, `lighting_family`, `negative_space`, and `copy_zone`.

The white-background anchor and locked supplier evidence control the target product. The mapped Ozon reference controls only composition, subject placement and scale, camera, background, lighting, visual movement, negative space, and information hierarchy. Replace the reference product with the locked target product. Never copy reference branding, text, watermark, price, package contents, accessories, functions, quantities, or claims.

Every accepted slot receipt records `primary_ozon_reference_path`, `primary_ozon_reference_sha256`, `reference_slot_index`, `reference_reused`, `reference_composition_followed`, and `locked_subject_preserved`. Acceptance reopens the local reference, verifies its dimensions, and recomputes its SHA-256. Set the last two flags only after inspecting the pixels against both the mapped reference and locked target evidence.

## Lease, checkpoint, and final-preview contract

Use a lease of at least 1800 seconds for blocking image-generation calls. Heartbeat immediately before every call. Run `checkpoint-asset` immediately after each image-generation call, and crop 1x2 or 1x3 results during that checkpoint step. On any resumed lease, run `checkpoint-status` and reuse every hash-verified checkpoint; never regenerate an already persisted call.

After `ready-for-review`, inspect and display all eight `accepted_path` files with `view_image`. Raw imagegen grids are never the final preview. `main_01` intentionally contains no copy; every other slot must contain readable Russian labels rendered by the local deterministic typography layer, and the recorded rendered output hash must differ from its pre-typography source hash.

## Allowed dynamic inputs

Append only verified values to a fixed prompt asset:

- Run ID, product ID, supplier offer ID, supplier SKU ID, and slot ID.
- Supplier selection SHA-256 and every locked subject-evidence SHA-256.
- Exact selected options, set quantity, composition, color, dimensions, material, parts, accessories, print, and package contents supported by locked evidence.
- Absolute paths for locked supplier evidence and the derived white-subject anchor path and SHA-256.
- The ordered valid Ozon reference paths and SHA-256 values, the primary reference assigned to each slot, `reference_mapping_version=ozon-reference-map-v1`, and the slot composition blueprint.
- One fixed storyboard buyer question, its allowed layout recipe, and verified Russian facts with evidence hashes.
- For a user-selected repair only: the selected `slot_id`, `review_issue_code`, and `review_note`. The note describes a defect and is never product evidence.

## User-selected repair behavior

Only `repair_pending` slots may be changed during a repair-only claim. A `russian_copy` request repairs the deterministic local typography with `source_kind=copy_repair_local` and makes no image-generation call. Every other supported issue code may make one `repair_single` image-generation call per selected slot. Do not repeat the four first-attempt calls, and do not modify an unselected accepted slot.

## Truth priority and acceptance

1. User-confirmed supplier SKU receipt.
2. Locked supplier subject evidence images.
3. Accepted supplier facts and gallery images.
4. Generated clean white-background subject as a derived shape anchor only.
5. Ozon references for composition, lighting, and layout only.

Stop when identity, quantity, or composition conflicts. Compare every output against locked evidence for silhouette, count, color, proportions, structure, parts, accessories, print, and set composition. Reject changed or ambiguous products, plain or near-white product-only finished slots, roles without visible proof, or a scene that is not distinct from accepted slots. Russian copy cannot substitute for visual proof.

Every accepted receipt has non-empty `slot_role` and true `product_truth`, `slot_role_satisfied`, `role_visually_demonstrated`, `not_plain_or_near_white_product_only`, `distinct_from_accepted_slots`, `copy_not_used_as_visual_evidence`, `reference_composition_followed`, and `locked_subject_preserved`. It also records `primary_ozon_reference_sha256`, `reference_slot_index`, and `reference_reused`. The deterministic pixel guard and `ready-for-review` recheck output hashes and the full set before handoff.
