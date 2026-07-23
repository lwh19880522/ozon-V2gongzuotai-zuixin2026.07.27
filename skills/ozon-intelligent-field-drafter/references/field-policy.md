# Field Decision Policy

## Source precedence

Use the most product-specific verified source:

1. `confirmed_supplier_sku` and `supplier_selection.*`: exact locked variant,
   `raw_label`, `selected_options`, `set_quantity`, `set_composition`, SKU ID,
   price, stock, and image facts.
2. `supplier_attributes` and `supplier.attributes.*`: 1688 product-level facts
   that do not conflict with the locked SKU.
3. Facts consistent across supplier evidence and `ozon_attributes`.
4. `ozon_attributes` and Ozon content evidence: Russian terminology, structure,
   and non-identity reference facts when supplier truth does not contradict them.
5. Ozon prose: creative reference unless a structured supplier fact confirms it.
6. `workflow_defaults`: stable operational values created by the workbench, such
   as a deterministic seller code. Never treat these as product measurements.

Never let lower-priority evidence overwrite higher-priority evidence.

## Identity fields

Brand, model, article, color, variant, package contents, and set quantity identify
the purchased supplier product. Prefer the complete locked SKU. Do not copy a
different Ozon seller's identity. If supplier identity is absent, use
`supplier_identity_missing`.

## Objective fields

Fill dimensions, weight, material, quantity, compatibility, intended use,
country, warranty, certification, and marking only from explicit evidence.

- Preserve the measured number.
- Convert units only by exact arithmetic.
- Preserve whether a number describes an item, set, or package.
- Use `set_quantity` for the locked sales-unit quantity when the Seller API field
  asks for quantity in the unit of measurement.
- Use `set_composition` and `raw_label` to describe package composition after
  faithful Russian translation.
- Do not turn a title keyword or image impression into a product fact.

Never infer safety certification, warranty, EAC/marking codes, partner identity,
factory-pack count, customs values, or weight from category norms.

## Russian normalization

Translate descriptive Chinese supplier facts into natural Russian when the
target field is customer-facing. Keep the original evidence reference and do not
alter model capacity, color, material, dimensions, quantity, or composition.
Codes and identifiers may remain unchanged. A Chinese descriptive string is not
a valid finished Russian model, type, color, material, or package value.

## Dictionary fields

Return the evidence-supported Russian value. Seller API dictionary resolution is
a later gate. If no exact allowed dictionary value can be established, use
`dictionary_value_missing`; do not guess a nearby value.

## Resolution classes

- `source_fact_missing`: source evidence does not contain the fact.
- `supplier_identity_missing`: confirmed supplier identity is absent.
- `dictionary_value_missing`: exact dictionary value is unavailable.
- `evidence_conflict`: verified sources conflict.
- `not_applicable`: evidence proves the field does not apply.

`summary.pending_fields=0` means all decisions were recorded. It is not proof of
readiness. Only `ready` means no unresolved field remains; `completed_with_gaps`
contains optional gaps and `blocked` contains required gaps.

## Creative fields

Use Ozon to learn Russian structure, search language, and information order.
Create original wording from verified supplier facts. Do not copy the source
title, description, hashtags, or Rich Content blocks verbatim.

## Required fields

Required fields receive no permission to guess. Classify the missing fact and
leave the product blocked while other products continue independently.

## Conflict handling

- Prefer the locked SKU over product-level 1688 attributes.
- Prefer supplier truth over reference Ozon identity.
- Do not average, merge, or select the convenient value.
- Use `evidence_conflict` when frozen evidence cannot settle the conflict.
