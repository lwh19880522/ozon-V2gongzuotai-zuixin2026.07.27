# Field Decision Policy

## Source precedence

Use the most product-specific verified source:

1. `confirmed_supplier_sku` / `supplier_selection.*`: exact user-locked sales
   unit, variant, quantity, color, model, composition, price, and SKU image facts.
2. `supplier_attributes` / `supplier.attributes.*`: 1688 product-level facts that
   do not conflict with the locked SKU.
3. Facts independently consistent across supplier evidence and
   `ozon_attributes`.
4. `ozon_attributes` and Ozon content evidence: target-market structure,
   terminology, and non-identity reference facts when supplier truth does not
   contradict them.
5. Ozon prose: creative structure only unless the same fact appears in a
   structured evidence entry.

Never let lower-priority evidence overwrite higher-priority evidence.

## Identity fields

Brand, model, article, color, variant, package contents, and set quantity identify
the purchased supplier product. When supplier truth exists, never copy these
values from the reference Ozon competitor. If supplier identity evidence is
missing, mark the field `unresolved`.

## Objective fields

Fill dimensions, weight, material, quantity, compatibility, intended use,
country, warranty, certification, and marking only from explicit evidence.
Normalize formatting without changing meaning:

- Preserve the measured number.
- Convert a unit only when the conversion is exact.
- Preserve whether a number describes one item, one set, or packaging.
- Do not turn a title keyword or image impression into a product fact.

Never infer safety certification, warranty, EAC/marking codes, partner identity,
factory-pack count, customs values, or weight from category norms.

## Dictionary fields

Return the evidence-supported text value. Seller API dictionary resolution is a
later workbench gate. Do not replace a factual value with a guessed dictionary
entry merely to satisfy the API.

## Creative fields

Use Ozon to learn Russian structure, search language, and information order.
Create original wording from verified supplier facts. Do not copy the source
title, description, bullets, hashtags, or Rich Content blocks verbatim.

## Required fields

Required fields receive no special permission to guess. If evidence is missing,
return `unresolved` and name the missing fact. The product remains blocked while
other products may continue.

## Conflict handling

When sources conflict:

- Prefer the confirmed supplier SKU over product-level 1688 attributes.
- Prefer 1688 supplier truth over the reference Ozon listing for identity.
- Do not average, merge, or select the more convenient value.
- Mark unresolved when the conflict cannot be settled from frozen evidence.
