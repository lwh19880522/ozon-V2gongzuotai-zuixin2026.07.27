# Ozon non-media optimization policy

## 1. Source precedence and evidence_insufficient

Use facts in this order: locked supplier SKU and its confirmed structured evidence; current Seller API category, type, dictionary, attribute values, title, and description; exact workbench upload preview/submission matched by `offer_id`; existing public product page observations. Bind workbench evidence only by the exact chain `offer_id -> seed_id -> confirmed_match selection -> locked supplier SKU -> seed-specific attribute evidence`.

Never fuzzy-match an offer, title, image filename, or supplier URL. Generated text and generated images are not product-fact evidence. If no exact workbench match or locked supplier SKU exists, set `evidence_insufficient`: improve only language supported by current Seller API facts, leave objective attributes and price unchanged, and do not set FBS stock 10. Do not ask the user to fill routine fields and do not guess.

## 2. Russian title, description, and Rich Content quality

Customer-visible content must be natural Russian, specific to the actual product, internally consistent, and useful for search and purchase decisions.

- Title: lead with the correct product noun; include only proven type, model, size, quantity, power, color, use, or compatibility; avoid keyword stacking and repeated synonyms.
- Description: explain real benefits, intended scenarios, use, specifications, set composition, and cautions without repeating the title or inventing claims.
- Rich Content: use structured text/layout blocks only. Do not include images, video, media URLs, covers, or duplicated filler.
- Reject Chinese, mojibake, 1688, supplier, internal workflow terms such as `locked-вариант`, wholesale, dropshipping, factory, fulfillment, shipping-promise, phone, messenger, and external-link language.
- Reject unsupported superlatives, guarantee, medical, safety, certification, manufacturer, brand, or compatibility claims.
- Reject ambiguous number formatting such as `70 90 см`, `6 4 см`, `2 8 игроков`, or an asymmetric range such as `от 50 до +400 C`. Do not introduce any numeric product fact absent from existing customer content or exact objective evidence.

Semantic self-review is mandatory after deterministic validation: the title, description, Rich Content, and free-text attributes must describe the same product fluently and without word salad.

## 3. Evidence-bound attributes, dictionaries, and units

Every attribute ID must exist in the current Seller API category/type schema. Every dictionary field must use an allowed current dictionary value ID. Keep omitted attributes unchanged. Existing Seller title or description can support a missing attribute only when the complete proposed visible value occurs there exactly with token boundaries; do not infer a nearby value, synonym, size class, material, unit, or compatibility.

Required attribute `8229` (`Тип`) is satisfied when Seller returns a nonzero top-level `type_id`; `/v4/product/info/attributes` may legitimately omit a duplicate attribute value. Do not add or rewrite `8229` in that case. Use the matching `type_name` only to audit whether the assigned category/type is semantically consistent with the customer-visible product; never infer or publish a nearby type from the title.

Objective facts require exact evidence: product identity, brand, model, color, material, compatibility, country, quantity, set composition, package composition, dimensions, weight, capacity, power, voltage, warranty, certification, and safety data. A source reference must resolve to the same normalized value. Evidence absence means `keep`, never a guessed `set`.

Exact package dimensions and weight confirmed by `workbench_user` outrank supplier recapture. Convert their units deterministically and revalidate Ozon package density before every write. If those confirmed values are internally invalid, record `invalid_user_confirmed_package_density`, keep stock guarded, and do not change the values merely to pass validation. For a card with `is_created=false`, submit a valid confirmed package correction and deterministic syntax repairs together through the full product import path; attribute-only update is not a valid recovery path.

Numbers must remain attached to the correct object and unit. Do not turn 290 mm into quantity 29, package quantity into product quantity, item dimensions into packaging dimensions, grams into kilograms without exact conversion, watts into volts, or capacity into power. Category ID, type ID, offer ID, barcode, price, VAT, and operational fields are read-only unless exact authorized evidence explicitly covers that field.

Media is outside this skill. Do not inspect it for evidence and do not read or write media fields except to prove byte-for-byte/request-value preservation during Seller API read-back.

## 4. Risk levels and automatic action

- Severe: identity/quantity/set/unit mismatch, unsafe price below a known minimum, unsupported safety or compliance claim, or another fact that can materially mislead the buyer. Fix only with exact evidence; otherwise guarded FBS stock 0.
- High: Seller API and storefront divergence on quantity, set, dimensions, weight, capacity, power, voltage, category, or type; a failed repair or failed read-back also qualifies. Attempt the evidence-supported repair; on failure use guarded FBS stock 0.
- Medium: inaccurate title/description/Rich Content, missing valuable supported attributes, language defects, or content score weakness. Rewrite automatically.
- Low: clarity, ordering, wording, and non-risk completeness improvements. Optimize automatically.

Syntactic Seller warnings that have one deterministic meaning-preserving repair are not severe. Apply the safe repair even when another high or severe product risk remains, then keep the remaining risk and inventory guard active. For `BR_hashtag_validation`, preserve each existing hashtag, replace only internal whitespace with underscores, and require focused Seller read-back. If Seller accepts the task but still returns the original value during the bounded read-back window, keep the accepted repair pending instead of restoring the invalid value. Never let a separate dimensions, weight, quantity, category, or type risk suppress this safe repair.

Never archive. Archived products are skipped. Before FBS stock 0 or FBS stock 10, re-read product state, block on active orders, and require exactly one active RFBS warehouse. Already `IN_SALE` products keep their current stock. Price is read-only without reliable cost and minimum-margin evidence. All writes require asynchronous import success, Seller API read-back, media equality, and rollback on failure.

Seller API item errors and warnings, failed validation, `is_created=false`, failed status flags, and required attributes with no value must be copied into the task and final report. An import task accepted by the API is transport confirmation only; it is not content or risk success. A claimed repair remains unresolved until focused Seller read-back clears the corresponding error and required-field gap.

Persist unresolved semantic risks across every rescan. Do not clear a category/type mismatch by editing customer text or filling the current category's attributes. Keep it high risk until an evidence-supported category/type repair is applied and Seller read-back confirms the new category, type, schema, and absence of validation errors.

## 5. Non-media content score and success

Read official weighted groups from `/v1/product/rating-by-sku`. Exclude the `media` group completely and renormalize only `text` plus `other_attributes` to 100. Preserve each group's conditions and `improve_attributes` as the reason for any proposed change. Missing images or video neither deduct content score nor block field processing.

Optimize the title, description, Rich Content, search phrasing, and all valuable evidence-supported attributes toward the best honest non-media result. Do not fill inapplicable fields or invent values to chase points.

Only `completed` means success. It requires strong evidence-supported non-media content; no unresolved deterministic or semantic risk; no Seller validation error or missing required field; Seller API write and focused read-back confirmation; images and video untouched; and an official non-media score of at least 80 that did not decrease. If the official score is absent, use `score_unverified`; if it remains below 80, use `score_below_target`. Neither state may be reported as optimized or receive stock 10. Success does not mean inventing values to reach 100 points.

The store run is complete only when the final status returns `run_complete: true`. A partial score increase, an accepted API task, an empty queue, or a terminal non-success state is never store completion.

For title-only changes, an accepted attribute-update task with unchanged exact title read-back is `write_rejected_no_change`, not success and not rollback evidence. Do not alter stock. Use the exact Seller product edit page, modify only `名称`, submit review, and require exact Seller API title read-back before clearing the title risk.
