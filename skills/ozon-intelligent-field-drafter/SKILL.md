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
   The workbench has already applied pre-resolved store defaults and direct
   deterministic mappings. Do not spend model decisions recreating fixed
   country, approved no-brand, seller-code, pricing/package, or locked-SKU
   quantity values. It also fixes
   `workflow.defaults.disable_product_grouping=Нет` and may use
   `ozon.category_path.leaf` as a low-priority type fallback when no more
   specific verified type exists.
4. For every pending entry in `field_tasks`, inspect its
   `candidate_evidence_refs`, then
   verify the referenced values in `evidence_index`. Read the full evidence when
   candidates are incomplete. The collected structured Ozon attributes remain
   active evidence for terminology and objective non-identity facts whenever
   the locked 1688 SKU or supplier attributes do not contradict them.
5. When any pending field has `visual_inference_supported=true`, run:

   ```powershell
   python skills/ozon-intelligent-field-drafter/scripts/materialize_visual_evidence.py --run-id <run_id>
   ```

   Read the returned manifest and call `view_image` on every `local_path` for
   that product. Do not decide a visual-supported field from URL strings,
   thumbnails, filenames, or prior generated images.
6. Inspect every listed `visual_evidence_refs` entry through its materialized
   locked supplier SKU images.
   Visual inference is limited to color, customer-facing color name, factory-pack count,
   and set/instrument count when the fact is directly visible and consistent.
   Before deciding any field, identify the primary product subject of the
   locked SKU. Classify accessories, packaging, backgrounds, text overlays,
   decorations, and reference variants separately; they do not change a
   primary-subject fact.
   Follow the field's `visual_target_scope`: `primary_product` for color,
   `factory_packaging` for factory-pack count, and `complete_set` for set/item
   count.
   Cite every inspected image key and attach a field-specific `visual_analysis`
   receipt with a structured `subject_analysis` whether the decision is filled
   or unresolved; generated images are never product-fact evidence.
7. Inspect the complete `confirmed_supplier_sku`, including `raw_label`,
   `selected_options`, `set_quantity`, `set_composition`, SKU ID, price, and
   stock. Do not reduce it to the visible option label.
8. Decide every pending field:
   - `creative_rewrite`: create original Russian content from verified facts.
   - `evidence_inference`: translate, normalize, or extract the narrowest value
     supported by exact evidence keys. Cite all supporting keys.
   - `unresolved`: classify a genuinely unavailable fact with one permitted
     `resolution_class`; never use unresolved merely because translation or
     normalization is required.
   - A field whose `mapping_method` is
     `customer_facing_normalization_required` already has a verified supplier
     fact. Preserve the product meaning, translate it to natural Russian, and
     remove only unrelated supplier fulfillment or promotion text. Terms such
     as `现货当天发`, `包邮`, `一件代发`, warehouse promises, or wholesale
     advertising must never enter the Ozon customer-facing value.
9. POST one product at a time to
   `http://127.0.0.1:8765/api/batches/{run_id}/content-tasks/complete`. Cover
   every pending field for that product.
10. Correct only rejected fields and resubmit the same product. Preserve accepted
   decisions.
11. Repeat GET until `summary.pending=0` and `summary.pending_fields=0`. This is
   not proof of readiness: inspect `summary.ready`,
   `summary.completed_with_gaps`, and `summary.blocked`.
12. GET `http://127.0.0.1:8765/api/batches/{run_id}/upload` and report, per
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

Visual fact after actual image inspection:

```json
{
  "decision": "filled",
  "value": "Серебристый",
  "evidence_refs": ["supplier_selection.supplier_sku.image_urls.0"],
  "reason": "На оригинальном изображении locked SKU виден серебристый металлический корпус.",
  "visual_analysis": {
    "result": "observed",
    "confidence": "high",
    "field_finding": "Корпус и рабочие части имеют серебристый металлический цвет.",
    "inspected_refs": ["supplier_selection.supplier_sku.image_urls.0"],
    "observations": [
      {
        "evidence_ref": "supplier_selection.supplier_sku.image_urls.0",
        "finding": "Полное изображение SKU показывает серебристый металлический инструмент."
      }
    ],
    "subject_analysis": {
      "primary_subject": "Ручной пробойник",
      "target_scope": "primary_product",
      "basis_refs": ["supplier_selection.supplier_sku.image_urls.0"],
      "excluded_elements": [
        {
          "element": "Люверсы рядом с инструментом",
          "role": "accessory",
          "colors": ["Золотистый"]
        }
      ],
      "subject_state": "single_color",
      "subject_colors": ["Серебристый"],
      "normalized_value": "Серебристый"
    }
  }
}
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
- For a visual-supported field, inspect the original locked 1688 images and
  cite every exact `visual_evidence_refs` entry in `visual_analysis`. A generic
  product photo is not enough unless it visibly proves the requested fact.
- Every visual decision must include `subject_analysis`. Identify the primary
  product subject first, follow `visual_target_scope`, and list excluded
  accessories, packaging, backgrounds, text overlays, decorations, and
  reference variants. These roles are universal and must not be replaced with
  product-specific exceptions.
- For color fields use `single_color`, `multi_color`, `variant_conflict`, or
  `not_visible`. Visible accessory colors never create `variant_conflict`.
  `variant_conflict` is reserved for incompatible primary-subject variants
  within the frozen locked-SKU evidence.
- `field_finding` and `reason` must answer the current field. Never reuse a
  factory-package explanation for color, set quantity, or another field.
- A filled visual decision requires `visual_analysis.result=observed`. An
  unresolved visual decision requires `not_visible`, `ambiguous`, or `conflict`.
- For `supplier_truth_required`, cite `supplier.*` or `supplier_selection.*`.
- Translate verified Chinese descriptive values into natural Russian for
  customer-facing model, type, color, material, package, and similar fields.
- Supplier fulfillment and sales promises are not product attributes. Remove
  those phrases during customer-facing normalization while retaining the
  exact locked-SKU evidence reference.
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
