---
name: ozon-intelligent-field-drafter
description: Fill or explicitly leave unresolved every pending Ozon V2 Seller API template field from collected Ozon evidence, confirmed 1688 supplier facts, and the user-locked supplier SKU. Use for batch field drafting, Russian title/description/Rich Content creation, objective attribute inference, evidence-backed completion, and retrying rejected field-task submissions before upload.
---

# Ozon Intelligent Field Drafter

## Core contract

Complete one Ozon V2 batch through the workbench field-task API. Treat the Ozon
listing as a structure and writing reference. Treat the confirmed 1688 SKU and
supplier evidence as supplier truth. Fill a field only when its value is supported
by collected evidence; otherwise return an explicit `unresolved` decision.

Read [field-policy.md](references/field-policy.md) before deciding any objective
field.

## Workflow

1. Require an Ozon V2 `run_id`.
2. GET `http://127.0.0.1:8765/api/batches/{run_id}/content-tasks`.
3. Process every item with `status=pending`. Do not spawn subagents.
4. For each item, read all `field_tasks` with `status=pending`, the complete
   `evidence_index`, the full Ozon evidence, supplier attributes, and confirmed
   supplier SKU.
5. Decide every pending field:
   - `creative_rewrite`: write original Russian content from verified facts.
     Preserve useful Ozon structure and information hierarchy without copying its
     wording.
   - `evidence_inference`: derive the narrowest value supported by exact entries
     in `evidence_index`. Cite every supporting key in `evidence_refs`.
   - If the evidence cannot prove a value, return `unresolved` with a concrete
     missing-fact reason. Never guess to increase completion counts.
6. POST one product at a time to
   `http://127.0.0.1:8765/api/batches/{run_id}/content-tasks/complete`.
   The `fields` object must cover every pending field for that product.
7. If the server returns validation errors, correct only the rejected product and
   resubmit it. Preserve all accepted decisions.
8. Repeat the GET after each pass until `summary.pending=0` and
   `summary.pending_fields=0`.
9. Read the upload workspace once more and report filled fields, unresolved
   fields, required-field blockers, and products ready for draft construction.

## Submission shapes

Use this shape for an objective field:

```json
{"decision":"filled","value":"1","evidence_refs":["supplier_selection.supplier_sku.selected_options.数量"],"reason":"The locked supplier SKU contains one sales unit."}
```

Use this shape when a fact is not provable:

```json
{"decision":"unresolved","reason":"Neither collected Ozon nor confirmed 1688 evidence states the warranty.","evidence_refs":[]}
```

Creative fields may be submitted as strings, but prefer the structured
`"decision":"filled"` shape with relevant `evidence_refs` so the audit trail
remains complete.

## Validation rules

- Submit only exact `field_key` values from the current task.
- Use only exact keys present in `evidence_index` as `evidence_refs`.
- Keep numbers, units, quantity, color, material, model, brand, package contents,
  compatibility, certification, and warranty inside verified evidence.
- For identity fields marked `supplier_truth_required`, cite at least one
  `supplier.*` or `supplier_selection.*` key.
- Write Russian creative content that passes the server's language, originality,
  length, hashtag, and Rich Content JSON checks.
- Treat an accepted `unresolved` decision as a completed field decision, not as a
  fabricated value. A required unresolved field remains an upload blocker.

## Phase relationship

This Skill and `$ozon-image-generation-controller` are peer executors of the same
Ozon V2 workbench product. Field drafting must not wait for unrelated image work,
and image work must not change field decisions. The workbench combines both
outputs at each product's upload gate so ready products can advance independently.

## Boundaries

- Do not collect new Ozon or 1688 data.
- Do not generate or repair images.
- Do not modify business source code.
- Do not upload.
- Do not publish.
- Do not approve a final listing.
