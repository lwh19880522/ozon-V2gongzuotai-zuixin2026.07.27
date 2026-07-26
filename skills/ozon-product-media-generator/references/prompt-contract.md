# Prompt Contract

## Visual contract v8

New scenes and receipts use `ozon-image-v8` with `ozon-visual-v2`. Versions
v2-v7 are historical compatibility only. A first attempt makes exactly four
image-generation calls: one white identity anchor, one 1x2 main grid, and two
1x3 supporting grids. This is the fixed 2+3+3 finished-image structure. Every
finished slot is exact 3:4 portrait; the whole 1x2 grid is 3:2 and each whole
1x3 grid is 9:4. The white anchor is identity only and is never a finished slot.

After the anchor is verified, every finished-grid and scene-repair call passes
the exact one-item array:

`referenced_image_paths=[<same frozen white anchor path>]`

That file is always Reference Image 1, the only image input, and the only
product-identity source. Reference Image 2 is forbidden. Do not attach an Ozon
image, supplier evidence image, unselected supplier variant, previous generated
scene, conversation image, or any other product image. Never use
`num_last_images_to_include`. The prompt must bind Reference Image 1 to
immutable identity and require scene simplification rather than any change to
silhouette, proportions, geometry, quantity, parts, structure, sole, heel, toe,
seams, openings, controls, print, color, material appearance, or selected
variant.

The task package uses `schema_version=2` and declares
`reference_count=1`, `additional_image_references_allowed=false`,
`composition_source=fixed_skill_prompt_only`, and
`product_identity_source=white_anchor_only`. It intentionally contains no Ozon
reference-image list.

White-anchor provenance has two distinct layers:

- Locked supplier subject evidence images are the factual inputs used only to
  derive the clean anchor.
- Generated clean white-background subject is the frozen identity image reused
  as Reference Image 1 for every finished-grid and scene-repair call.

## Fixed prompt and storyboard

Before a finished-grid or scene-repair call, prepend
`ozon-commercial-infographic-core-prompt.txt` byte-for-byte to the matching
wrapper. The white-anchor call never uses the commercial core.

The fixed slots are:

- `main_01`: core_selling_point
- `main_02`: value_overview
- `detail_01`: use_demo
- `detail_02`: annotated_feature
- `detail_03`: material_or_mechanism
- `detail_04`: instructional_or_result
- `detail_05`: dimension_fit_target_or_scale
- `detail_06`: set_contents_or_buyer_question

Each slot has one prompt-only composition blueprint with
`reference_mapping_version=none`,
`guidance_mode=fixed_prompt_white_anchor`, one supported `layout_archetype`,
`core_theme`, `subject_position`, `subject_scale`, `camera_family`,
`shot_scale`, `background_family`, `lighting_family`, `negative_space`,
`copy_zone`, `headline_alignment`, and `annotation_style`. Primary Ozon
reference fields and reference-slot fields are null; `reference_reused=false`.
The fixed prompt, blueprint, and verified facts control all composition.

Across eight slots use at least six layout archetypes, five environment
families, four lighting treatments, four camera/composition families, and three
shot scales. One environment family or information template may appear at most
twice. Any two accepted slots differ in at least three of layout archetype,
visible proof, environment, lighting, camera, shot scale, and buyer question.
A background or angle change alone is not diversity.

## Russian copy

All eight finished panels use `copy_mode=imagegen_integrated` and contain:

- one exact verified Russian headline of 3-7 words;
- one exact verified Russian subtitle of at most 14 words;
- 2-4 exact verified Russian functional labels.

Every fact cites locked supplier evidence. Every numeric fact has
`numeric_verified=true` and copies the verified value exactly. The model renders
the supplied copy during the first scene-generation call and renders no other
unsupported text, number, logo, badge, price, discount, URL, or watermark.
Never consume a second scene-generation call merely to add Russian copy.

The product occupies about 55-70 percent of a panel. The headline is a clear
composition anchor and at least 7 percent of panel height in the 360-pixel
preview; supporting copy remains at least 16 pixels. A panel has at most four
explanation zones. Text must not cross the product or safe area. Use natural
Russian sentence case except for standard abbreviations.

## Receipt contract

An accepted v8 receipt records:

- non-empty `slot_role`;
- exact 3:4 output and `visual_contract_version=ozon-visual-v2`;
- complete `visual_spec`;
- true `product_truth`, `slot_role_satisfied`,
  `role_visually_demonstrated`, `not_plain_or_near_white_product_only`,
  `distinct_from_accepted_slots`, `copy_not_used_as_visual_evidence`,
  `visual_design_passed`, `russian_copy_passed`, `safe_area_passed`,
  `mobile_readability_passed`, and `locked_subject_preserved`;
- `copy_mode=imagegen_integrated` and `russian_copy_integrated=true`;
- exact headline, subtitle, and functional labels;
- `reference_mapping_version=none`;
- `guidance_mode=fixed_prompt_white_anchor`;
- a supported `layout_archetype`;
- `image_reference_count=1`;
- `additional_image_references_attached=false`;
- empty `primary_ozon_reference_path`,
  `primary_ozon_reference_sha256`, and `reference_slot_index`;
- `reference_reused=false`;
- `identity_anchor_path`, `identity_anchor_sha256`,
  `identity_anchor_reference_index=1`, `identity_anchor_attached=true`,
  `identity_anchor_reused=true`, and
  `product_identity_source=white_anchor_only`.

Do not assert `reference_composition_followed` or
`reference_layout_followed`; no Ozon reference exists. `ready-for-review`
rejects the set unless all eight receipts use the same live anchor path and
SHA-256.

## Lease, checkpoint, repair, and preview

Use a lease of at least 1800 seconds. Heartbeat immediately before every
blocking generation call. Immediately checkpoint each result and crop grids
atomically. On resume, reuse all hash-verified checkpoints.

A copy-only failure repairs the existing bitmap locally. A scene or
product-truth repair may generate one exact 3:4 replacement for the selected
slot, again using only the same frozen white anchor. Never change the anchor,
append another image, or modify an unselected accepted slot.

After all slots pass, inspect all eight `accepted_path` files with `view_image`.
Raw grids are never the final preview.

## Allowed dynamic inputs

Append only:

- run, package, product, offer, supplier SKU, and slot identifiers;
- locked selection, subject-evidence, and anchor paths and SHA-256 values;
- exact verified selected options, quantity, set composition, color,
  dimensions, material, parts, accessories, print, and package contents;
- the exact one-item `referenced_image_paths` array containing the frozen
  anchor;
- the fixed storyboard buyer question, prompt-only blueprint, exact Russian
  headline, subtitle, and 2-4 functional labels with evidence hashes;
- for selected repair only, `slot_id`, `review_issue_code`, and `review_note`.

The review note is defect direction, never product evidence.

## Truth priority and acceptance

1. User-confirmed selected 1688 SKU receipt.
2. Locked supplier subject evidence images.
3. Accepted supplier facts and gallery images.
4. The verified clean white anchor as a derived shape reference only.

Stop only when locked selected-SKU evidence itself conflicts about identity,
quantity, or composition. Reject a result that changes or obscures the frozen
anchor subject, invents a product fact, uses a plain white catalog treatment,
lacks visual proof for its role, or duplicates an accepted scene. Russian copy
never substitutes for visual proof.
