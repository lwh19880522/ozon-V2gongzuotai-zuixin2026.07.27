# Prompt Contract

## Visual contract v6

New scenes and receipts use `ozon-image-v6` with `ozon-visual-v2`; `ozon-image-v2`, `ozon-image-v3`, `ozon-image-v4`, and `ozon-image-v5` are read-only historical receipt compatibility. A first attempt makes exactly four image-generation calls: one white identity anchor, one 1x2 main grid, and two 1x3 supporting grids. This is the fixed 2+3+3 finished-image structure. Every finished slot is exact 3:4 portrait, so the complete 1x2 grid is 3:2 and each complete 1x3 grid is 9:4. The anchor is identity and geometry only and must never be an accepted finished slot.

The fixed slots are `main_01` core_selling_point; `main_02` value_overview; `detail_01` use_demo; `detail_02` annotated_feature; `detail_03` material_or_mechanism; `detail_04` instructional_or_result; `detail_05` dimension_fit_target_or_scale; and `detail_06` set_contents_or_buyer_question. Before any finished-grid call, load `ozon-commercial-infographic-core-prompt.txt` and prepend it byte-for-byte to the corresponding 1x2 or 1x3 wrapper. The white-anchor call never uses the commercial core. All eight finished panels use `copy_mode=imagegen_integrated` and contain one exact verified 3-7-word Russian headline, one exact verified Russian subtitle of at most 14 words, and 2-4 exact verified Russian functional labels. Every fact cites locked supplier evidence. Any numeric fact requires `numeric_verified=true`. The image model renders that supplied copy as part of the first scene-generation call and renders no other unsupported text, numbers, logos, badges, prices, discounts, URLs, or watermarks. Simple arrows, leader lines, measurement marks, or evidence-safe pictograms are allowed only when the mapped reference's information design uses them and they point to a visually proven feature.

The deterministic crop accepts only near-3:4 source panels, center-crops a small framing deviation to exact 3:4, and rejects square, landscape, or materially wrong panels. After crop and any conservative upscale, preserve exact 3:4 and validate the generated Russian copy, safe area, and mobile readability; never consume a second image-generation call merely to add Russian copy. A copy-only failure repairs the existing bitmap with local deterministic typography and never consumes an image-generation attempt. Scene or product-truth failure alone may use a single-slot image repair, and that repair call includes the exact Russian copy.

The finished layout must use a prominent Russian headline, subordinate subtitle, and 2-4 concise functional labels with an obvious visual hierarchy, but it must not force one repeated label template across the gallery. The reference-layout archetype chooses the information design: editorial selling point, functional infographic, annotated anatomy, instructional steps, dimension or fit diagram, comparison, set-contents layout, or natural lifestyle caption. Plan the text-safe negative space before rendering the subject. The product occupies approximately 55-70 percent of the panel. The headline must be a deliberate composition anchor, contain 3-7 Russian words, and remain at least 7 percent of panel height at the 360-pixel preview; the subtitle contains at most 14 Russian words; supporting copy remains at least 16 pixels and clearly subordinate. A panel contains no more than four principal explanation zones. Match the mapped reference's alignment, density, line rhythm, annotation geometry, and visual balance while replacing its exact words with verified Russian copy. Do not use tiny floating labels, text crossing the subject, generic detached rounded cards, or the same edge-gradient overlay on every slot. Preserve natural Russian sentence case except for standard abbreviations. The receipt must record the actual headline, subtitle, functional labels, projected mobile pixel sizes, `reference_layout_archetype`, and `visual_system`; a hard-coded pass flag without those measurements is invalid.

Each finished panel expresses one core theme. The 2-4 functional labels must all support that theme, remain non-overlapping and mobile-readable, and describe only facts proved by locked evidence. Never add a label merely to fill space.

Any two accepted scene slots differ in at least three of layout archetype, visible proof, environment, lighting, camera, shot scale, and buyer question. An accepted v6 receipt includes its complete `visual_spec`, `copy_mode=imagegen_integrated`, the exact supplied Russian headline, subtitle and functional labels, exact 3:4 output dimensions, `visual_contract_version=ozon-visual-v2`, and these four true pass flags: `visual_design_passed`, `russian_copy_passed`, `safe_area_passed`, and `mobile_readability_passed`.

The full set uses at least six distinct reference-layout archetypes, five environment families, four lighting treatments, four camera/composition families, and three shot scales; one environment family or information template appears no more than twice. A background change alone never satisfies diversity. Pixel-identical, structurally near-duplicate, or visually near-duplicate finished slots fail the set gate regardless of their declared scene signatures.

## Slot-specific Ozon reference mapping

Use `reference_mapping_version=ozon-reference-map-v1`. In `reference_guided` mode use one primary Ozon reference per finished slot. First run `materialize-references`; only `reference_manifest.json` accepted entries may be mapped. A valid reference has been upgraded from an Ozon thumbnail URL when available, decoded locally, has a shorter edge of at least 512 pixels, is not pixel-identical to an earlier reference, contains recognizable product media, and has a legible composition. Classify each valid reference by `reference_layout_archetype`: hero, lifestyle, functional infographic, annotated feature, instructional steps, material close-up, dimension or fit, comparison, or set contents. Map the best matching distinct archetype to each storyboard role instead of blindly following gallery order. Prefer one reference per slot; if valid references are too few, the same reference may be bound to at most two slots and each binding must use a different buyer question. When references are missing or insufficient, use `guidance_mode=ozon_aesthetic_fallback` for uncovered slots. A fallback slot still selects an explicit missing archetype so the gallery remains structurally varied. Missing reference guidance must not stop generation.

One reference-guided slot learns from one primary reference. Reconstruct its layout grammar: subject scale and placement, crop, camera, visual proof, foreground/background relationship, information density, title alignment, annotation geometry, and negative-space distribution. Do not reduce reference learning to a background swap. Do not blend multiple references into one slot. A fallback slot instead uses a self-directed polished Ozon-marketplace composition with a distinct reference-layout archetype, environment, lighting, camera family, shot scale, negative space, and Russian-copy zone. Before image generation, record optional `reference_path` and `reference_sha256`, plus `guidance_mode`, `reference_layout_archetype`, `subject_position`, `subject_scale`, `camera_family`, `shot_scale`, `background_family`, `lighting_family`, `negative_space`, `copy_zone`, `headline_alignment`, and `annotation_style`.

The white-background anchor and locked 1688 supplier evidence control the target product. The locked 1688 SKU and subject evidence are authoritative; Ozon differences and unselected supplier variants are not conflicts. The mapped Ozon reference controls only composition, subject placement and scale, camera, background, lighting, visual movement, negative space, and information hierarchy. Replace the reference product with the locked target product. Never copy reference branding, text, watermark, price, package contents, accessories, functions, quantities, or claims.

Every accepted slot receipt records `guidance_mode`, `reference_layout_archetype`, optional `primary_ozon_reference_path`, optional `primary_ozon_reference_sha256`, `reference_slot_index`, `reference_reused`, `reference_composition_followed`, `reference_layout_followed`, and `locked_subject_preserved`. Reference-guided acceptance reopens the local reference, verifies its dimensions, recomputes its SHA-256, and rejects a result that preserves only the background while losing the reference's subject scale, visual proof, or information structure. Fallback acceptance leaves primary-reference fields null and validates the declared Ozon-aesthetic composition directly. Always set `locked_subject_preserved` only after inspecting pixels against the locked 1688 target evidence.

## Lease, checkpoint, and final-preview contract

Use a lease of at least 1800 seconds for blocking image-generation calls. Heartbeat immediately before every call. Run `checkpoint-asset` immediately after each image-generation call, and crop 1x2 or 1x3 results during that checkpoint step. On any resumed lease, run `checkpoint-status` and reuse every hash-verified checkpoint; never regenerate an already persisted call.

After `ready-for-review`, inspect and display all eight `accepted_path` files with `view_image`. Raw imagegen grids are never the final preview. All eight slots must contain the exact readable Russian headline, subtitle and functional labels integrated during the first scene-generation call. The accepted source hash may equal the crop hash when no copy repair was needed.

## R2 and slideshow publication contract

The public R2 channel is a pre-generation gate. `media-preflight` must succeed
before the first image-generation call; otherwise release the package to
`pending` with `public_media_channel_required` and consume no generation work.
After all eight accepted exact-3:4 slots pass inspection, build one deterministic
3:4 `slideshow.mp4` that scrolls through the eight slots in order and one exact
3:4 `video_cover.jpg` derived locally from `main_01`. Creating the video and
cover never invokes image generation. Publish the eight slots, video, and cover
through the same verified R2 channel. The terminal receipt records all ten
public URLs and hashes, the Ozon picture-import result, and the Ozon video-import
task. Video moderation is asynchronous and is not a reason to regenerate images.

## Allowed dynamic inputs

Append only verified values to a fixed prompt asset:

- Run ID, product ID, supplier offer ID, supplier SKU ID, and slot ID.
- Supplier selection SHA-256 and every locked subject-evidence SHA-256.
- Exact selected options, set quantity, composition, color, dimensions, material, parts, accessories, print, and package contents supported by locked evidence.
- Absolute paths for locked supplier evidence and the derived white-subject anchor path and SHA-256.
- The ordered valid Ozon reference paths and SHA-256 values, the primary reference assigned to each slot, `reference_mapping_version=ozon-reference-map-v1`, and the slot composition blueprint.
- One fixed storyboard buyer question, its allowed layout recipe, one verified 3-7-word Russian headline, one verified Russian subtitle of at most 14 words, and 2-4 verified Russian functional labels with evidence hashes.
- For a user-selected repair only: the selected `slot_id`, `review_issue_code`, and `review_note`. The note describes a defect and is never product evidence.

## User-selected repair behavior

Only `repair_pending` slots may be changed during a repair-only claim. A `russian_copy` request repairs the deterministic local typography with `source_kind=copy_repair_local` and makes no image-generation call. Every other supported issue code may make one `repair_single` image-generation call per selected slot. Do not repeat the four first-attempt calls, and do not modify an unselected accepted slot.

## Truth priority and acceptance

1. User-confirmed supplier SKU receipt.
2. Locked supplier subject evidence images.
3. Accepted supplier facts and gallery images.
4. Generated clean white-background subject as a derived shape anchor only.
5. Ozon references for composition, lighting, and layout only.

Stop only when the locked selected-SKU 1688 evidence itself conflicts about identity, quantity, or composition. Ignore unselected supplier variants and Ozon SKU differences. If stale metadata mistakes an age, size, or model number for quantity while the locked selected-SKU images and option label consistently prove the sales unit, record `locked_metadata_normalization` and follow the 1688 visual evidence. Compare every output against locked evidence for silhouette, count, color, proportions, structure, parts, accessories, print, and set composition. Reject changed or ambiguous products, plain or near-white product-only finished slots, roles without visible proof, or a scene that is not distinct from accepted slots. Russian copy cannot substitute for visual proof.

Every accepted receipt has non-empty `slot_role` and true `product_truth`, `slot_role_satisfied`, `role_visually_demonstrated`, `not_plain_or_near_white_product_only`, `distinct_from_accepted_slots`, `copy_not_used_as_visual_evidence`, `reference_composition_followed`, and `locked_subject_preserved`. It also records `primary_ozon_reference_sha256`, `reference_slot_index`, and `reference_reused`. The deterministic pixel guard and `ready-for-review` recheck output hashes and the full set before handoff.
