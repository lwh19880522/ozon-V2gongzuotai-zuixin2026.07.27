---
name: ozon-intelligent-field-drafter
description: Build auditable Russian Ozon V2 Seller API field drafts from collected Ozon evidence, confirmed 1688 product facts, and the complete user-locked supplier SKU. Use for batch field drafting, objective attribute completion, Russian title/description/Rich Content creation, classified evidence gaps, and retrying rejected field decisions before upload.
---

# Ozon Intelligent Field Drafter

## Core contract

Complete one Ozon V2 batch through the workbench field-task API. Use Ozon as a
Russian structure and terminology reference. Use the confirmed 1688 product and
the complete locked supplier SKU as product truth. Translate or normalize a
verified fact without changing its meaning; never copy Chinese customer-facing
text into a Russian Ozon field.

Read [field-policy.md](references/field-policy.md) before deciding objective
fields.

## Workflow

1. Require an Ozon V2 `run_id`.
2. GET `http://127.0.0.1:8765/api/batches/{run_id}/content-tasks`.
3. Process every item with `status=pending`. Do not spawn subagents.
4. For every pending entry in `field_tasks`, inspect its
   `candidate_evidence_refs`, then
   verify the referenced values in `evidence_index`. Read the full evidence when
   candidates are incomplete.
5. Inspect the complete `confirmed_supplier_sku`, including `raw_label`,
   `selected_options`, `set_quantity`, `set_composition`, SKU ID, price, and
   stock. Do not reduce it to the visible option label.
6. Decide every pending field:
   - `creative_rewrite`: create original Russian content from verified facts.
   - `evidence_inference`: translate, normalize, or extract the narrowest value
     supported by exact evidence keys. Cite all supporting keys.
   - `unresolved`: classify a genuinely unavailable fact with one permitted
     `resolution_class`; never use unresolved merely because translation or
     normalization is required.
7. POST one product at a time to
   `http://127.0.0.1:8765/api/batches/{run_id}/content-tasks/complete`. Cover
   every pending field for that product.
8. Correct only rejected fields and resubmit the same product. Preserve accepted
   decisions.
9. Repeat GET until `summary.pending=0` and `summary.pending_fields=0`. This is
   not proof of readiness: inspect `summary.ready`,
   `summary.completed_with_gaps`, and `summary.blocked`.
10. GET `http://127.0.0.1:8765/api/batches/{run_id}/upload` and report, per
    product, total mapped fields, newly filled fields, classified optional gaps,
    required blockers, image blockers, and draft readiness.

## Submission shapes

Objective fact translated from the locked SKU:

```json
{"decision":"filled","value":"Зеленый трехместный диван","evidence_refs":["supplier_selection.supplier_sku.raw_label","supplier_selection.supplier_sku.selected_options.规格"],"reason":"The locked supplier variant was translated into Russian without changing color or capacity."}
```

Exact quantity from the locked SKU:

```json
{"decision":"filled","value":"1","evidence_refs":["supplier_selection.supplier_sku.set_quantity"],"reason":"The locked supplier SKU contains one sales unit."}
```

Unavailable source fact:

```json
{"decision":"unresolved","resolution_class":"source_fact_missing","reason":"Neither collected Ozon evidence nor confirmed 1688 evidence states the warranty.","evidence_refs":[]}
```

Creative fields may be strings, but prefer structured `decision=filled` values
with evidence references.

## Resolution classes

Use exactly one for every unresolved field:

- `source_fact_missing`: no collected source states the fact.
- `supplier_identity_missing`: brand, model, color, or other identity is absent
  from confirmed supplier truth.
- `dictionary_value_missing`: evidence has a fact but no exact permitted Seller
  API dictionary value is available.
- `evidence_conflict`: higher-priority sources conflict and cannot be reconciled.
- `not_applicable`: the field demonstrably does not apply to this product.

## Validation rules

- Submit exact `field_key` values from the current task.
- Cite only exact `evidence_index` keys.
- For `supplier_truth_required`, cite `supplier.*` or `supplier_selection.*`.
- Translate verified Chinese descriptive values into natural Russian for
  customer-facing model, type, color, material, package, and similar fields.
- Preserve codes, quantities, measurements, colors, materials, composition, and
  variant identity during translation.
- Do not translate an identifier into a different identifier and do not invent
  absent certification, customs, warranty, weight, or regulatory facts.
- `completed_with_gaps` is not `ready`; a required unresolved field is
  `blocked`.

## Phase relationship

This Skill and `$ozon-image-generation-controller` are peer executors of the same
Ozon V2 batch. Field drafting must not wait for unrelated image work. Each
product passes its own field and image gates independently.

## Boundaries

- Do not collect new Ozon or 1688 data.
- Do not generate or repair images.
- Do not modify business source code.
- Do not upload.
- Do not publish.
- Do not approve a final listing.
