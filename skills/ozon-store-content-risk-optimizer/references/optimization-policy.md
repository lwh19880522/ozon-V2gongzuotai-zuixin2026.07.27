# Ozon non-media optimization policy

## 1. Source precedence and evidence_insufficient

Use facts in this order: locked supplier SKU and its confirmed structured evidence; current Seller API category, type, dictionary, and attribute values; exact workbench upload preview/submission matched by `offer_id`; existing public product page observations; existing images as read-only consistency evidence.

Never fuzzy-match an offer, title, image filename, or supplier URL. Generated text and generated images are not product-fact evidence. If no exact workbench match or locked supplier SKU exists, set `evidence_insufficient`: improve only language supported by current Seller API facts, leave objective attributes and price unchanged, and do not set FBS stock 10. Do not ask the user to fill routine fields and do not guess.

## 2. Russian title, description, and Rich Content quality

Customer-visible content must be natural Russian, specific to the actual product, internally consistent, and useful for search and purchase decisions.

- Title: lead with the correct product noun; include only proven type, model, size, quantity, power, color, use, or compatibility; avoid keyword stacking and repeated synonyms.
- Description: explain real benefits, intended scenarios, use, specifications, set composition, and cautions without repeating the title or inventing claims.
- Rich Content: use structured text/layout blocks only. Do not include images, video, media URLs, covers, or duplicated filler.
- Reject Chinese, mojibake, 1688, supplier, wholesale, dropshipping, factory, fulfillment, shipping-promise, phone, messenger, and external-link language.
- Reject unsupported superlatives, guarantee, medical, safety, certification, manufacturer, brand, or compatibility claims.

Semantic self-review is mandatory after deterministic validation: the title, description, Rich Content, and free-text attributes must describe the same product fluently and without word salad.

## 3. Evidence-bound attributes, dictionaries, and units

Every attribute ID must exist in the current Seller API category/type schema. Every dictionary field must use an allowed current dictionary value ID. Keep omitted attributes unchanged.

Objective facts require exact evidence: product identity, brand, model, color, material, compatibility, country, quantity, set composition, package composition, dimensions, weight, capacity, power, voltage, warranty, certification, and safety data. A source reference must resolve to the same normalized value. Evidence absence means `keep`, never a guessed `set`.

Numbers must remain attached to the correct object and unit. Do not turn 290 mm into quantity 29, package quantity into product quantity, item dimensions into packaging dimensions, grams into kilograms without exact conversion, watts into volts, or capacity into power. Category ID, type ID, offer ID, barcode, price, VAT, and operational fields are read-only unless exact authorized evidence explicitly covers that field.

Existing images may reveal an identity, color, quantity, or set-composition conflict, but they never authorize dimensions, weight, capacity, power, voltage, warranty, or compliance facts.

## 4. Risk levels and automatic action

- Severe: identity/quantity/set/unit mismatch, unsafe price below a known minimum, unsupported safety or compliance claim, or another fact that can materially mislead the buyer. Fix only with exact evidence; otherwise guarded FBS stock 0.
- High: Seller API and storefront divergence on quantity, set, dimensions, weight, capacity, power, voltage, category, or type; a failed repair or failed read-back also qualifies. Attempt the evidence-supported repair; on failure use guarded FBS stock 0.
- Medium: inaccurate title/description/Rich Content, missing valuable supported attributes, language defects, or content score weakness. Rewrite automatically.
- Low: clarity, ordering, wording, and non-risk completeness improvements. Optimize automatically.

Never archive. Archived products are skipped. Before FBS stock 0 or FBS stock 10, re-read product state, block on active orders, and require exactly one active RFBS warehouse. Already `IN_SALE` products keep their current stock. Price is read-only without reliable cost and minimum-margin evidence. All writes require asynchronous import success, Seller API read-back, media equality, and rollback on failure.

## 5. Non-media content score and success

Use official text/description and other-attributes groups when available. Exclude the media score group completely and renormalize only available non-media group points to 100. Missing images or video neither deduct content score nor block success.

Optimize the title, description, Rich Content, search phrasing, and all valuable evidence-supported attributes toward the best honest non-media result. Do not fill inapplicable fields or invent values to chase points.

Success means: strong evidence-supported non-media content; no unresolved deterministic or semantic risk; Seller API write and read-back confirmed; images and video untouched; non-media score not reduced; and the correct inventory action completed or safely blocked. Success does not mean every field filled or 100 points at any cost.
