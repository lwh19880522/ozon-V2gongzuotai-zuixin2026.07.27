# Prompt Contract

## Visual contract v3

New scenes and receipts use `ozon-image-v3` with `ozon-visual-v1`; `ozon-image-v2` is read-only historical receipt compatibility. A first attempt makes exactly four image-generation calls: one white identity anchor, one 1x2 main grid, and two 1x3 supporting grids. The anchor is identity and geometry only and must never be an accepted finished slot.

The fixed slots are `main_01` clean_hero with zero copy; `main_02` integrated_rail with at most two verified Russian fact blocks; and supporting slots using only their allowed `ozon-visual-v1` recipes. Every fact cites a locked supplier SHA-256. Any numeric fact requires `numeric_verified=true`. The image model renders no text, numbers, icons, logos, badges, prices, or discounts.

After crop and any conservative upscale, run local `render-visual` and merge its validation fragment into the slot receipt. A copy failure repairs only the local text layer and never consumes an image-generation attempt. Scene or product-truth failure alone may use a single-slot image repair.

Any two accepted scene slots differ in at least three of environment, lighting, camera, shot scale, and buyer question. An accepted v3 receipt includes its complete `visual_spec`, `visual_contract_version=ozon-visual-v1`, and these four true pass flags: `visual_design_passed`, `russian_copy_passed`, `safe_area_passed`, and `mobile_readability_passed`.

## Allowed dynamic inputs

Append only verified values to a fixed prompt asset:

- Run ID, product ID, supplier offer ID, supplier SKU ID, and slot ID.
- Supplier selection SHA-256 and every locked subject-evidence SHA-256.
- Exact selected options, set quantity, composition, color, dimensions, material, parts, accessories, print, and package contents supported by locked evidence.
- Absolute paths for locked supplier evidence and the derived white-subject anchor path and SHA-256.
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

Every accepted receipt has non-empty `slot_role` and true `product_truth`, `slot_role_satisfied`, `role_visually_demonstrated`, `not_plain_or_near_white_product_only`, `distinct_from_accepted_slots`, and `copy_not_used_as_visual_evidence`. The deterministic pixel guard and `ready-for-review` recheck output hashes and the full set before handoff.
