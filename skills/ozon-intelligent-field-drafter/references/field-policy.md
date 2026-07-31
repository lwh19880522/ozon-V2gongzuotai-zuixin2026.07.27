# Field Decision Policy

## Source precedence

Use the most product-specific verified source:

1. Pre-resolved `workflow_defaults` explicitly marked as store-fixed policy.
   In this China-direct store workflow, country of manufacture is always `Китай`;
   the workbench resolves it before the field Skill runs. These
   defaults are operational policy, not inferred measurements.
   The one-product-per-card workflow also resolves
   `workflow.defaults.disable_product_grouping` to `Нет`; the field Skill must
   not recreate a grouping decision.
2. `confirmed_supplier_sku` and `supplier_selection.*`: exact locked variant,
   `raw_label`, `selected_options`, `set_quantity`, `set_composition`, SKU ID,
   price, stock, and image facts.
3. `supplier_attributes` and `supplier.attributes.*`: 1688 product-level facts
   that do not conflict with the locked SKU.
4. Facts consistent across supplier evidence and `ozon_attributes`.
5. `ozon_attributes` and Ozon content evidence: Russian terminology, structure,
   and non-identity reference facts when supplier truth does not contradict them.
   The structured Ozon attributes remain active evidence; they are not display-only
   data and must be checked before declaring a non-identity source fact missing.
6. Ozon prose: creative reference unless a structured supplier fact confirms it.

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
- Do not turn a title keyword or an ambiguous image impression into a product
  fact.

Never infer safety certification, warranty, EAC/marking codes, partner identity,
factory-pack count, customs values, or weight from category norms.

Marking-code and similar compliance decisions are not Skill inference. When the
Seller API declares one as a Boolean field, the workbench must request one
explicit boolean confirmation and carry that typed value into the upload draft.
The Skill must not derive true or false from category norms, missing evidence,
images, or unrelated attributes.

## Visual evidence

Use only original locked supplier images exposed through `visual_evidence_refs`.
Generated images are never product-fact evidence.

- Materialize the frozen URLs with the field Skill's
  `scripts/materialize_visual_evidence.py`, then use `view_image` on every local
  image before submitting a decision.
- `supplier_selection.supplier_sku.image_urls.*` is exact locked-SKU evidence.
- `supplier.images.*` may corroborate the SKU only when
  `supplier_visual_evidence.page_single_sku=true` and the evidence source is
  `single_sku_detail_page`.
- Direct visual inference is limited to color, customer-facing color name,
  factory-pack count, and set/instrument count.
- Target scope is field-specific and category-independent:
  - `primary_product` for product color and customer-facing color name.
  - `factory_packaging` for factory-pack count.
  - `complete_set` for set or instrument count.
- Identify the primary product subject before deciding the field. Record
  accessories, packaging, backgrounds, text overlays, decorations, and
  reference variants in `subject_analysis.excluded_elements`.
- Visible accessories do not create a product-color conflict. Judge the color
  of the primary product subject; accessory, fastener, cable, gift, packaging,
  background, and overlay colors are excluded unless the target field
  explicitly describes them.
- For color, classify the subject as `single_color`, `multi_color`,
  `variant_conflict`, or `not_visible`. A visible `single_color` or
  `multi_color` subject is an observed fact. Use `variant_conflict` only when
  frozen evidence shows incompatible primary-subject variants and does not
  identify which one is the locked SKU.
- Require a clear, consistent visual fact. A single item photo does not prove a
  factory-pack count; packaging text or a clearly complete counted set must.
- Normalize a visible color to an allowed Seller API dictionary value when the
  task supplies one. If no exact value is available, use
  `dictionary_value_missing`.
- Do not infer dimensions, weight, warranty, certification, safety, customs,
  marking, or compatibility from images.
- Cite an inspected visual reference even when the final decision remains
  unresolved, so a stale text-only gap is not silently accepted again.
- Attach one structured `visual_analysis` receipt per field. Its
  `field_finding` must describe that field, and its per-image observations must
  cover every reference exposed for the field. Its `subject_analysis` must
  identify the primary subject, target scope, basis references, and excluded
  elements.

## Russian normalization

Translate descriptive Chinese supplier facts into natural Russian when the
target field is customer-facing. Keep the original evidence reference and do not
alter model capacity, color, material, dimensions, quantity, or composition.
Codes and identifiers may remain unchanged. A Chinese descriptive string is not
a valid finished Russian model, type, color, material, or package value.

For `customer-facing normalization`, remove unrelated 1688 seller fulfillment
and promotion phrases such as `现货当天发`, `当天发`, `包邮`, `一件代发`,
`厂家直销`, and wholesale advertising. These phrases are not product facts and
must never be translated into the Ozon attribute. Preserve the actual variant
fact before the phrase and cite the locked supplier evidence.

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

Process required fields before optional and creative fields. Exhaust exact
locked-SKU evidence, supplier attributes, structured Ozon attributes, allowed
dictionary values, permitted visual evidence, translation, and normalization
before declaring a required fact missing.

Required fields receive no permission to guess. If the fact remains
unavailable, classify it as `unresolved`. The workbench may expose it to the
user only after that decision appears in `manual_required_fields`; a merely
pending entry in `missing_required_fields` is still Skill work and must not
become a user input.

After the user confirms an exact manual required value, save only that field,
reload the field tasks, and continue until the product is upload-ready or a new
specific blocker is returned. Never leave a product at a generic required-field
blocker without either an evidence-backed value or an exact manual handoff.

## Conflict handling

- Prefer the locked SKU over product-level 1688 attributes.
- Prefer supplier truth over reference Ozon identity.
- Do not average, merge, or select the convenient value.
- Use `evidence_conflict` when frozen evidence cannot settle the conflict.
