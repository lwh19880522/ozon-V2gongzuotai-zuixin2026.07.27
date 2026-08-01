# Prompt Contract

## Visual contract v9

New scenes and receipts use `ozon-image-v9` with `ozon-visual-v2`; v2-v8 are
historical receipt compatibility only. A first attempt makes exactly two image-generation calls:
one white identity anchor and one 4x2 eight-panel gallery grid. The whole grid
is exact 3:2, and every equal crop is exact 3:4
portrait. The white anchor is identity only and is never a finished slot.

After the anchor is verified, the gallery call and every selected scene-repair
call pass exactly:

`referenced_image_paths=[<same frozen white anchor path>]`

That file is Reference Image 1, the only image input and the only
product-identity source. Reference Image 2 is forbidden. Do not attach an Ozon
image, supplier evidence image, unselected supplier variant, previous scene,
conversation image or any other image. Never use
`num_last_images_to_include`.

Bind Reference Image 1 to immutable identity. Every panel must preserve its
silhouette, proportions, geometry, count, sales unit, set composition,
structure, parts, openings, seams, controls, print, markings, material
appearance, color and selected variant. If a scene requires a changed subject,
simplify the scene. Never redesign the subject from text.

The task package declares `generation_mode=single_thread_8_grid`,
`grid_layout=4x2`, `public_media=auto_quick_tunnel`, `reference_count=1`,
`additional_image_references_allowed=false`,
`composition_source=fixed_skill_prompt_only`, and
`product_identity_source=white_anchor_only`. It contains no Ozon reference
image list.

## Fixed storyboard and grid mapping

Prepend `ozon-commercial-infographic-core-prompt.txt` byte-for-byte to
`gallery-8-grid-prompt.txt` and selected scene-repair prompts. The white-anchor
call never uses the commercial core.

Crop the 4x2 grid in row-major order:

1. `main_01`: core_selling_point
2. `main_02`: value_overview
3. `detail_01`: use_demo
4. `detail_02`: annotated_feature
5. `detail_03`: material_or_mechanism
6. `detail_04`: instructional_or_result
7. `detail_05`: dimension_fit_target_or_scale
8. `detail_06`: set_contents_or_buyer_question

Each slot keeps one prompt-only blueprint with
`reference_mapping_version=none`,
`guidance_mode=fixed_prompt_white_anchor`, one supported `layout_archetype`,
`core_theme`, `subject_position`, `subject_scale`, `camera_family`,
`shot_scale`, `background_family`, `lighting_family`, `negative_space`,
`copy_zone`, `headline_alignment`, and `annotation_style`. Ozon reference and
reference-slot fields are null; `reference_reused=false`.

Across eight slots use at least six layout archetypes, five environment
families, four lighting treatments, four camera/composition families and three
shot scales. One environment family or information template may appear at most
twice. Any two slots differ in at least three of layout archetype, visible
proof, environment, lighting, camera, shot scale and buyer question.

## Copy and truth

Each panel contains one exact verified Russian headline of 3-7 words, one exact
verified subtitle of at most 14 words and 2-4 exact verified functional labels.
Every fact cites locked supplier evidence; every number copies its verified
value exactly. Text never substitutes for visible proof.

The product occupies about 55-70 percent of a panel. Keep all text and product
pixels within the panel safe area. No content may cross a grid boundary. Render
no unsupported text, number, logo, badge, price, discount, URL or watermark.

## Receipt and acceptance

An accepted v9 receipt records exact 3:4 output, `ozon-visual-v2`, complete
`visual_spec`, true `product_truth`, `slot_role_satisfied`,
`role_visually_demonstrated`, `not_plain_or_near_white_product_only`,
`distinct_from_accepted_slots`, `copy_not_used_as_visual_evidence`,
`visual_design_passed`, `russian_copy_passed`, `safe_area_passed`,
`mobile_readability_passed` and `locked_subject_preserved`.

It also records `image_reference_count=1`,
`additional_image_references_attached=false`, empty Ozon reference fields,
`identity_anchor_path`, `identity_anchor_sha256`,
`identity_anchor_reference_index=1`, `identity_anchor_attached=true`,
`identity_anchor_reused=true`, and
`product_identity_source=white_anchor_only`. Ready-for-review rejects the set
unless all eight receipts use the same live anchor path and SHA-256.

## Checkpoint, crop and repair

Heartbeat before each blocking generation call. Checkpoint the anchor and raw
grid immediately and reuse every hash-verified checkpoint. Crop the raw grid
with `crop-grid --layout 4x2`; never stretch, reorder or use the raw grid as a
final image. Inspect all eight `accepted_path` files with `view_image`.

A copy-only defect uses local deterministic rendering. A selected scene or
product-truth repair may generate one exact 3:4 replacement with the same sole
frozen white anchor. Never change the anchor, append another reference or edit
an unselected accepted slot.

## Truth priority

1. User-confirmed selected 1688 SKU receipt.
2. Locked supplier subject evidence images for the exact selected SKU.
3. Accepted supplier facts and gallery evidence.
4. Generated clean white-background subject, verified and frozen as the
   derived immutable identity reference.

Reject a result that changes or obscures the frozen subject, invents a fact,
uses a plain white catalog treatment for a finished slot, lacks visual proof or
duplicates an accepted composition.
