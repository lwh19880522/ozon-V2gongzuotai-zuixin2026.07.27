# Prompt Contract

Prompt and receipt contract version: `ozon-image-v2`.

## Allowed dynamic inputs

Append only these verified values to a fixed prompt asset:

- Run ID, product ID, supplier offer ID, and supplier SKU ID.
- Supplier selection SHA-256 and every locked subject-evidence SHA-256.
- Exact selected options, set quantity, composition, color, dimensions, material, parts, and accessories present in accepted evidence.
- Absolute paths for all locked supplier subject evidence images.
- Generated clean white-background subject path and SHA-256 after evidence verification.
- Approved Ozon style-reference paths.
- One slot role and its verified selling-point facts.
- A request to reserve a clean copy zone for supporting images.

## Truth priority

1. User-confirmed supplier SKU receipt.
2. Locked supplier subject evidence images.
3. Accepted supplier facts and supplier gallery images.
4. Generated clean white-background subject as a derived shape anchor only.
5. Ozon evidence for composition, lighting, and layout only.

When sources conflict, stop the product. Never blend conflicting facts.

## Russian copy

The model must not render copy. After crop, use one short Russian headline and up to two short Russian facts drawn only from accepted evidence. Main slots default to no copy. Unsupported dimensions, performance, durability, material, count, or accessories are forbidden.

## Acceptance

Compare every output first with the Locked supplier subject evidence images, then with the Generated clean white-background subject. Verify silhouette, exact count, color, proportions, structure, visible parts, accessories, and set composition. Reject any changed or ambiguous subject. A visually attractive image cannot override product truth.

The generated white-background subject is an intermediate identity anchor only. Reject a finished slot that copies its plain or near-white product-only catalog treatment, shows only another angle, or does not visibly fulfill its assigned commercial role. Russian copy cannot substitute for visual proof.

Every accepted receipt must contain a non-empty `slot_role` and these boolean fields set to true after actual pixel inspection:

- `product_truth`
- `slot_role_satisfied`
- `role_visually_demonstrated`
- `not_plain_or_near_white_product_only`
- `distinct_from_accepted_slots`
- `copy_not_used_as_visual_evidence`

The queue also applies a deterministic pixel guard to reject the washed-out high-luminance, low-color, near-white anchor treatment even when a worker incorrectly self-reports a pass. `ready-for-review` re-verifies every frozen file hash and requires eight distinct roles before handoff. A headline, role label, or `accepted=true` alone is never sufficient.
