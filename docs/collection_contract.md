# Ozon V2 Collection Contract v0.1

This document defines the first collection contract for Ozon V2.

It is a Domain + Application design step. It does not implement browser
automation, MCP tools, runtime data, or legacy plugin compatibility.

## Step Boundary

Layer:

- Domain: existing-store-product dedupe, seed eligibility, domestic seller
  judgment, hot-product candidate rules, single-SKU identity policy,
  selected-SKU media policy, exact-same supplier matching rules, and SKU truth
  policy.
- Application: pre-collection dedupe gate, seed sampling gate, task contract
  shape for Ozon collection, 1688 collection, ingest validation, and evidence
  CSV generation.

Files created or modified in this step:

- `docs/collection_contract.md`

This step does not:

- Create FastMCP tools.
- Start browser automation.
- Scrape Ozon or 1688 pages.
- Create upload tasks, images, or ready pools.
- Import or touch old plugin code.
- Write runtime data.

Completion test:

- The contract states how to identify Chinese domestic Ozon sellers.
- The contract states what Ozon product data must be collected.
- The contract states how 1688 exact-match supplier collection works.
- The contract states single-SKU collection for both Ozon and 1688, selected-SKU
  media collection, and exact-same product matching.
- The contract states that every run must first build a dedupe list from the
  target store's existing products.
- The contract states that each collection run must start from a random sample
  from the seed pool.

## Collection Goal

Phase-one collection has two linked parts:

1. Ozon side: collect hot products from Chinese domestic competitor stores.
2. 1688 side: find the real supplier product that is the same as the selected
   Ozon product.

The result is not an upload-ready listing. The result is a verified collection
evidence package that later steps can use to learn category, subcategory,
attributes, a single matched SKU, selected-SKU image references, and supplier
truth.

Precise Ozon category evidence is mandatory. A collected Ozon candidate is not
valid unless the collector captures the category path, leaf category, category
URL, and category ID.

## Run Gate Order

Every plugin run must follow this order:

0. Confirm seller credentials exist through the credential assistant.
1. Build or refresh the target store existing-product dedupe list.
2. Apply the dedupe list to seed eligibility.
3. Randomly sample eligible seeds from the active seed pool.
4. Generate Ozon market search queries for sampled seeds.
5. Start Ozon collection from the sampled seeds and generated Ozon queries.
6. Match one exact 1688 supplier SKU for each accepted Ozon target SKU.
7. Finalize the run and remove sampled seeds from the active seed pool package.

No later step may skip an earlier gate.

## Credential Assistant Gate

The plugin cannot perform a complete store-level test without the user's target
store credentials.

Before existing-store dedupe runs, the credential assistant must help the user
provide:

```text
seller_credentials:
  client_id
  api_key
```

User-facing wording may call `client_id` the store ID. Internally it is the
Seller API `Client-Id`.

Rules:

- Credentials live only under runtime config:
  `E:\ozon-V2工作区\OzonOpsV2\config\seller_credentials.local.json`.
- A template may be generated at:
  `E:\ozon-V2工作区\OzonOpsV2\config\seller_credentials.template.json`.
- Do not store real credentials in project code, docs, tests, seed assets, or
  Obsidian logs.
- Tool responses and logs must mask `api_key`.
- `doctor` must report whether seller credentials are configured.
- `doctor` must not mark full store dedupe ready from credentials alone. Dedupe
  readiness requires a runtime refresh marker, even when the refreshed store has
  zero existing products.
- `start_run` must detect missing credentials, request the local credential
  assistant popup, and then fail with `run.credentials_missing` without
  continuing to later gates.
- The credential assistant must be single-instance. If the popup is already
  open, later automatic attempts must return `credential_assistant.already_open`
  instead of opening another popup.
- Automatic popup attempts must respect a runtime cooldown so a user who is away
  from the computer does not receive repeated popups.
- Full existing-store dedupe is not ready until credentials are configured.

## Existing Store Product Dedupe Gate

Before any seed sampling or collection starts, the application must identify
products that already exist in the user's target Ozon store.

Existing store products must be added to the dedupe list and must never be
collected again.

This is the first collection gate. It runs before the seed pool gate.

After refreshing this gate, runtime must write:

```text
state/existing_store_dedupe.jsonl
state/existing_store_dedupe.meta.json
```

The meta file proves that the refresh ran. An empty dedupe JSONL without the meta
file is treated as "not refreshed", not as "store has no products".

The refresh uses the target store Seller API credentials and must be exposed as
`ozon_v2_refresh_existing_store_dedupe`. The tool is read-only against Seller
API and writes only runtime dedupe state.

### Existing Product Record Shape

The dedupe list should preserve enough evidence to block repeated collection:

```text
existing_store_product:
  store_product_id
  offer_id_when_available
  title
  brand
  category_path
  main_image_reference
  selected_sku_or_options_when_available
  product_url_when_available
  normalized_identity_key
  source_captured_at
  notes
```

### Dedupe Rules

Rules:

- If a product already exists in the target store, do not collect it.
- If a seed clearly maps to an existing store product, exclude that seed before
  sampling.
- If an Ozon candidate matches an existing store product, reject it before 1688
  matching.
- If a 1688 supplier match reveals the same product as an existing store product,
  reject the pair.
- Do not use an existing store product as a style reference, substitute, or
  fallback candidate.
- Rejected duplicates must be recorded with the dedupe evidence that caused the
  rejection.

The dedupe list is authoritative for this run. If the dedupe evidence is
uncertain, mark the candidate as `needs_manual_review`; do not continue it as an
accepted collection pair.

## Seed Pool Gate

Collection must not start from open-ended browsing.

Before every collection run, the application must sample target seeds from the
active seed pool. The initial seed pool contains 500 seed products prepared by
the user.

For a new user installation, the plugin initializes a runtime active seed pool
from the bundled 500-seed package when no active pool exists yet.

If the batch target is 20 uploads, the run must first sample 20 eligible seed
products from the active pool. Ozon collection then starts from those 20 seed
products.

### Seed Pool Purpose

The seed pool prevents:

- Random, uncontrolled collection.
- Repeatedly collecting the same product.
- Repeatedly uploading the same or near-duplicate product.
- Drifting away from the user's intended product universe.

### Seed Pool Record Shape

Each seed product should be treated as a collection starting point:

```text
seed_product:
  seed_id
  title_or_keyword
  source_language
  product_clue
  category_hint
  ozon_query_terms
  ozon_query_language
  query_generation_status
  image_reference_when_available
  notes
```

Every seed must have a stable `seed_id`.

Bundled initial seed pool asset:

```text
assets/seed_pool/Ozon_2000精细子类目种子池.txt
assets/seed_pool/seed_pool.initial.json
assets/seed_pool/manifest.json
```

The seed pool source language is Chinese. Chinese seed text remains product
identity evidence; Ozon search uses the bundled Russian-first query terms.

### Seed Query Language Rules

The 500 bundled seeds are Chinese product clues. They must not be typed directly
into Ozon search.

Rules:

- Keep the original Chinese seed text as `title_or_keyword` and `product_clue`.
- Before Ozon collection, generate Ozon market search terms for each sampled
  seed.
- Ozon search terms must be Russian-first because Ozon product discovery is
  Russian-market oriented.
- English terms may be included as auxiliary terms when they are common product
  names, but Chinese must not be the direct Ozon query.
- If no usable Russian or market-language query can be generated for a seed,
  mark the seed result as `needs_query_generation` and do not start Ozon
  browsing for that seed.
- The generated query must stay tied to the original `seed_id` so the audit
  trail can show which Chinese seed produced which Ozon query.

Expected generated query shape:

```text
seed_search_query:
  seed_id
  source_text_zh
  ozon_query_terms_ru
  auxiliary_query_terms_en
  negative_terms
  query_generation_method
  query_generation_confidence
  generated_at
```

1688 search can use Chinese terms, but only after the Ozon target SKU is chosen.
The 1688 step must still prove exact sameness with the selected Ozon SKU.

### Sampling Rules

For each run:

- Input `target_count` equals the intended upload count for this batch.
- Randomly sample `target_count` eligible seeds from the active seed pool.
- Sample without replacement within the same run.
- Exclude seeds already sampled in any previous finalized run.
- Exclude seeds already marked uploaded, rejected as duplicate, or currently in
  progress.
- Exclude seeds whose matched Ozon or supplier product has already been uploaded.
- Exclude seeds blocked by the existing-store-product dedupe list.
- If fewer eligible seeds remain than `target_count`, stop with an explicit
  insufficient-seeds result instead of filling the batch with random browsing.
- Store the sampled seed list in the run evidence so the batch can be audited.
- Store generated Ozon search query terms for each sampled seed before Ozon
  browsing starts.

Random sampling must be reproducible for audit: the run evidence should record
the V2 run id, target count, seed pool snapshot/version, random seed value when
available, and sampled `seed_id` list.

### Seed-To-Collection Rule

Each sampled seed should produce at most one accepted single-SKU collection pair:

```text
seed_product -> Ozon target SKU -> matched 1688 supplier SKU
```

If no exact Ozon/1688 match can be proven for that seed, mark that seed result
as `needs_manual_review` or `rejected`. Do not replace it with an unrelated
product unless a later explicit retry step samples a new eligible seed.

### Seed Pool Removal After Run

After a run is finalized, every sampled seed must be removed from the active seed
pool package.

If the batch sampled 20 seeds, those 20 seed products leave the active pool after
the run. They must not be sampled again for later runs.

Removal rules:

- Remove sampled seeds from the runtime active seed pool whether the seed ended
  as accepted, rejected, duplicate, or needs manual review.
- Preserve removed seeds in a used-seed archive for audit.
- Do not delete evidence about removed seeds.
- Do not rehydrate removed seeds from the original bundled seed package on later
  plugin starts.
- The original bundled seed package is only an initialization source for new
  users or empty first-run setup. The runtime active seed pool is the source of
  truth after initialization.

The purpose is to prevent repeated collection attempts from the same seed.

## Single SKU Only

Phase-one collection is single-SKU collection.

Rules:

- Do not collect multiple Ozon specifications.
- Do not collect multiple Ozon SKUs.
- Do not collect multiple 1688 specifications.
- Do not collect multiple 1688 SKUs.
- Select one Ozon target SKU.
- Find one 1688 supplier SKU that is exactly the same product as the Ozon target
  SKU.
- If exact sameness cannot be proven, mark the pair as `needs_manual_review` or
  `rejected`; do not broaden the collection to nearby variants.

## Ozon Domestic Seller Judgment

The collector must identify whether an Ozon product comes from a Chinese
domestic cross-border seller. Judgment must be evidence-based. A candidate
should keep all observed evidence, not just the final boolean.

### Primary Evidence: Delivery Warehouse Address

Check the product detail page delivery section:

- `Из Китая` or `Китай` means the item ships from China and is treated as a
  Chinese cross-border domestic seller signal.
- `Москва`, `СПБ`, or `Россия` means Russian local shipping and is treated as a
  local-store signal, even if the seller may be Chinese-owned.

This is the most accurate buyer-side judgment method.

### Secondary Evidence: Delivery Time

Use delivery promise as a quick auxiliary signal:

- Local-store pattern: today, next day, or about 3-7 days.
- Chinese cross-border pattern: about 15-30 days.
- Month-end or next-month promise such as `конец месяца` or `следующий месяц`
  is also a Chinese cross-border signal.

Delivery time cannot override clear warehouse evidence, but it can support an
uncertain case.

### Ozon Global / Foreign Goods Area

The Ozon home page region/category entry for foreign goods can be used:

- `Мир / Зарубежные товары`
- Filter `Китай`

Products found under this route are treated as Chinese cross-border candidates
unless contradicted by product-detail evidence.

### Store Registration Evidence

From the product page, open the store page and inspect merchant information:

- Chinese city/province/company markers such as Changsha, Shenzhen, Guangzhou,
  Zhejiang, Shandong, Chinese company names, or a unified social credit code
  indicate a Chinese domestic cross-border seller.
- Russian company markers such as `ИП`, `ООО`, Russian tax number, or Russian
  address indicate a local Russian store.

Store registration evidence is a strong supporting signal, but product delivery
warehouse evidence remains the first buyer-side signal.

### Fulfillment Label Evidence

Product logistics labels can support classification:

- `FBS` or `rFBS`: Chinese cross-border seller / domestic source signal.
- `FBO` or `Ozon склад`: Russian local Ozon warehouse signal.

The label is supporting evidence. It must be stored when available.

### Decision Policy

The domestic-seller decision must produce:

```text
is_chinese_domestic_seller: true | false | unknown
confidence: high | medium | low
evidence:
  delivery_origin
  delivery_time
  global_area_route
  store_registration
  fulfillment_label
  notes
```

Rules:

- `high`: delivery origin clearly says China, or multiple independent signals
  point to China.
- `medium`: no clear delivery origin, but delivery time and store registration
  both point to China.
- `low`: only one weak signal points to China.
- `false`: warehouse, registration, and fulfillment evidence point to Russia.
- `unknown`: evidence is missing or contradictory.

Phase one should prefer high-confidence and medium-confidence candidates.
Low-confidence candidates may be recorded but should not be promoted.

## Ozon Hot-Product Candidate Rules

Category is unrestricted. The collector may search across categories, but every
accepted candidate must satisfy:

- Candidate discovery starts from a sampled seed product.
- Chinese domestic seller judgment is `true` with high or medium confidence.
- Product appears to be a hot or competitive product.
- Product has enough visible listing data to learn from.
- Product has one visible SKU or selected option that can be used for
  same-product comparison.
- If the listing exposes multiple specifications or SKU options, choose only one
  target SKU and keep the collection focused on that SKU. Do not collect the
  full option tree.
- Product images are collected only as references for the selected target SKU.
  Do not collect image sets for unrelated SKU options.

Hot-product evidence may include:

- Sales count or sold quantity when visible.
- Review count.
- Rating.
- Cart/order popularity markers.
- Bestseller/popular/ranking badge.
- Strong price competitiveness.
- Repeated presence across similar listings.

The collector must store which hot-product signals were observed. Do not accept
a product only because it is visually interesting.

## Ozon Product Data To Collect

For each accepted Ozon candidate, collect:

```text
seed_id
seed_title_or_keyword
ozon_product_id
ozon_url
title
brand
seller_name
seller_url
seller_evidence
  category_path
  category_url
  category_id
  category
  subcategory
  leaf_category
price
currency
rating
review_count
sales_or_popularity_signal
delivery_origin
delivery_time
fulfillment_label
attributes
content_score_evidence
target_sku
selected_sku_media
source_captured_at
collector_notes
```

### Category And Attribute Learning

Collect category and attribute fields because later upload steps must reuse this
information to reduce repetitive manual work.

Required category data:

- Full category path when visible.
- Main category.
- Subcategory.
- Leaf category or final marketplace category when visible.

Required attribute data:

- Product attribute names as Ozon displays them.
- Product attribute values.
- Which attributes appear required or structurally important.
- Units of measure when visible.
- Attribute grouping if Ozon groups them by section.

The collector must keep original field labels, including Russian labels, because
later mapping should learn from the real marketplace wording.

### Seed Attribute Template Prerequisite

Immediately after seeds are selected and safe Ozon query terms are available,
the workflow must fetch the Ozon category and attribute template for each seed.
This happens before Ozon competitor-product collection.

Purpose:

- Know which Ozon category should be used during upload.
- Know which upload fields are required or optional.
- Know which values require selecting from allowed Ozon dictionaries.
- Prepare a draft prefill plan before batch upload, so later upload does not
  waste time discovering required fields one by one.

Template output must include:

```text
category_candidates
  category_path
  leaf_category
  category_url
  category_id
  confidence
  source_evidence

upload_attribute_schema
  attribute_id
  attribute_label
  attribute_type
  is_required
  allowed_values
  unit
  group
  example_value_when_visible

draft_prefill_plan
  field_key
  source
  prefill_allowed
  rewrite_required
  reason
```

Draft prefill policy:

- Objective factual fields may be prefilled from the Ozon product/template when
  the 1688 supplier proves the same product: dimensions, weight, material, color,
  size, capacity, quantity, package contents, compatibility, model, voltage,
  power, age group, and gender.
- Supplier truth overrides conflicting Ozon attributes.
- Creative fields must not be copied from Ozon: title, description, rich content,
  marketing claims, bullet points, SEO keywords, and image text.
- The upload draft can be prefilled in bulk only after objective fields and
  rewrite-required fields are separated.

### Content Score Optimization Evidence

Ozon collection must be detailed enough to support later content score
optimization. The collector is not only finding a same-product target; it is also
building evidence for future title, attribute, media, pricing, and rich-content
improvement.

Required public-page evidence:

- Raw title and title length.
- Description text and rich-content blocks when visible.
- Attribute table, including original Ozon labels and values.
- Required or structurally important visible attributes.
- Category path, leaf category, category URL, and category ID.
- Brand when visible.
- Current price, currency, old price, discount, or promotion signal when visible.
- Rating and review count.
- Seller name, seller URL, and seller identity evidence.
- Delivery origin, delivery time, and fulfillment label.
- Main gallery images, selected-SKU images, detail images, and image style notes.

Recommended market signals:

- Search result position.
- Visible badges, bestseller labels, sale labels, or ranking labels.
- Sales, order, cart, or popularity markers when visible.
- Price competitiveness compared with nearby similar listings.
- Repeated presence across similar listings.

Optional external analytics signals may be recorded only when a permitted data
source exposes them:

- Monthly sales and monthly revenue.
- Month-over-month trend.
- Advertising cost share.
- Promotion days and discount.
- Paid promotion days.
- Following or competing seller counts.
- Lowest price and highest price.
- Product views and add-to-cart rate.
- Search-category views and add-to-cart rate.
- Display total, display conversion rate, and click share.
- FBS/FBP commission or commission rate.

Missing field policy:

- Do not invent metrics.
- If a public field is not visible, record it under `missing_fields` with the
  reason.
- If an external analytics field is unavailable, record it as unavailable; do
  not block Ozon collection only because external analytics are missing.
- Accepted Ozon candidates must include a `content_score_evidence` object.

### Ozon Single-SKU Identity And Selected-SKU Media

For Ozon collection, SKU data is used only to confirm whether the 1688 product
is the same product. Ozon SKU data is not the source of truth for later listing
SKU replication.

For each Ozon product, collect one target SKU:

```text
target_sku:
  - sku_id
  - selected_options
  - price
  - availability
  - image_reference
  - seller_sku_or_offer_id_when_visible
  - notes
```

If the Ozon listing has multiple colors, sizes, models, capacities, sets, or
other option groups, do not attempt to fully model the SKU tree. Choose one
target SKU and collect media only for that selected SKU:

```text
selected_sku_media:
  - main_gallery_images
  - selected_sku_images
  - detail_page_images
  - selected_option_label_when_visible
  - image_role: main | selected_sku | detail | comparison | lifestyle | unknown
  - style_notes:
      layout
      composition
      background
      props
      text_or_badges
      framing
      color_tone
      aesthetic_quality
  - source_url_or_reference
```

The purpose of Ozon selected-SKU images is future image generation: when 1688
has the exact same SKU/product, generated images should learn and imitate the
Ozon candidate's style, layout, structure, and visual polish while using the
real supplier product as the source product.

Rules:

- Ozon needs only one target SKU for same-product confirmation.
- Do not collect complete Ozon SKU dimensions.
- Do not collect multiple Ozon SKU variants.
- Do not use Ozon SKU count as the final SKU count.
- Do not infer purchasable SKUs from Ozon images.
- Preserve useful images for the selected target SKU only.

## 1688 Exact-Match Supplier Collection

The 1688 side must find the real supplier product that is the same item as the
accepted Ozon candidate.

The supplier product must be a true same-product match. Similar style is not
enough.

### Exact-Match Evidence

A 1688 candidate should be accepted only when evidence supports sameness:

- Same product appearance and function.
- Same distinctive visual details.
- Same model/design where applicable.
- Same selected SKU/specification combination when applicable.
- Same material/specification cues where visible.
- Same package/set contents where applicable.
- Same images or highly matching image set when that is legitimate supplier
  material.

Reject candidates that are only:

- Same category.
- Similar color.
- Similar shape but different design.
- Same keyword but visibly different product.
- Cheaper substitute.
- Higher-spec or lower-spec substitute.

### 1688 Product Data To Collect

For each accepted 1688 supplier match, collect:

```text
supplier_product_id
supplier_url
title
shop_name
shop_url
company_name
price_range
currency
moq
stock_or_availability
shipping_origin
domestic_shipping_fee
domestic_shipping_destination
domestic_shipping_evidence
attributes
matched_supplier_sku
selected_sku_media
match_evidence
source_captured_at
collector_notes
```

### Single-SKU Truth Policy

The real 1688 supplier page is the source of truth for the purchasable SKU, but
phase-one collection only captures the single supplier SKU that matches the
selected Ozon target SKU.

Rules:

- Ozon provides one target SKU for same-product confirmation.
- 1688 provides one matched supplier SKU for supplier truth.
- If the 1688 page exposes multiple specifications or SKU options, choose only
  the one that exactly matches the Ozon target SKU.
- Do not collect the full 1688 SKU tree.
- Do not invent supplier SKUs from Ozon images or visible Ozon variants.
- If 1688 exposes more accurate specification names or values, keep the 1688
  values for the matched single SKU as supplier truth.
- Capture the real domestic freight for the selected supplier page/SKU flow.
  A generic free-shipping, return-shipping, or wholesale service label is not
  enough.
- If the selected supplier flow clearly shows one-piece ordering is supported
  and the concrete origin -> destination delivery line shows `包邮` for the
  selected quantity context, normalize the domestic freight to `¥0`.
  Keep the raw text, for example `1件起批`, `广东汕头 -> 福建泉州`,
  `50件以内`, and `包邮`, as evidence.
- Domestic freight evidence must include the fee, destination, quantity context
  when visible, and the raw page text used as evidence.
- The final evidence must distinguish `ozon_target_sku`, `ozon_selected_sku_media`,
  and `matched_supplier_sku`.

This policy prevents later upload work from promising variants that were not
verified as the exact same supplier SKU.

## Linked Evidence Shape

Every accepted pair must produce a linked collection record:

```text
collection_pair:
  seed_product:
    seed_id
    title_or_keyword
    source_language
    product_clue
    ozon_query_terms_ru
  ozon_candidate:
    identity
    domestic_seller_decision
    hot_product_evidence
    content_score_evidence
    category_and_attributes
    ozon_target_sku
    ozon_selected_sku_media
  supplier_match:
    identity
    exact_match_decision
    matched_supplier_sku
    domestic_shipping_fee
    domestic_shipping_destination
    domestic_shipping_evidence
    supplier_attributes
  final_decision:
    accepted | rejected | needs_manual_review
    reasons
    evidence_csv_row_id
```

## Playwright Task Contract Direction

The controller will not run Playwright directly. It will generate task contracts
for Codex to execute through Microsoft Playwright MCP.

Ozon collection task contract must ask the browser worker to:

1. Receive the sampled seed product list, generated Ozon query terms, and the
   existing-store dedupe context for the run.
2. Require the seed attribute template prerequisite to be completed before
   collecting competitor product details.
3. For each seed, use `ozon_query_terms_ru` or approved market-language terms to
   find Chinese domestic seller candidates using the evidence rules above.
4. Prefer hot products that match the seed intent.
5. Open product detail pages.
6. Reject candidates that match the existing-store dedupe list.
7. Capture seller, category, attributes, one target SKU, selected-SKU images for
   image-generation style learning, and content-score optimization evidence.
8. Return structured evidence, not prose-only notes.

The Ozon browser worker must not directly search the original Chinese
`title_or_keyword` on Ozon.

1688 collection task contract must ask the browser worker to:

1. Search for the exact same product as the chosen Ozon candidate.
2. Use direct network access for 1688; do not route 1688, 1688 image search,
   or Alibaba image/CDN requests through a proxy.
3. Start from the 1688 homepage `https://www.1688.com/`, then enter image
   search from the homepage camera/image-search control. Do not begin from a
   search results page, detail page, direct `air.1688.com` URL, or any other
   intermediate page.
4. Upload the local Ozon product image to 1688 image search, select the actual
   product subject when 1688 offers subject crops, then search. Do not rely on
   remote Ozon image URLs or text search as the primary exact-match method.
5. Compare visible details, the Ozon target SKU, and selected-SKU image
   references.
6. Reject similar-but-not-same candidates.
7. Capture supplier attributes and one matched supplier SKU.
8. Capture the real domestic shipping fee for the selected supplier page/SKU
   flow; do not use generic "free shipping" or wholesale labels as a
   substitute. A concrete one-piece/selected-quantity `包邮` delivery line is
   recorded as `¥0`.
9. Return structured match evidence.

## Ingest Validation Direction

`ozon_v2_ingest_collection_output` must later validate:

- Existing-store dedupe was run before seed sampling.
- A sampled `seed_id` is present for every collection pair.
- The collection pair belongs to the current sampled seed list.
- The seed was eligible after existing-store dedupe filtering.
- Generated Ozon query terms exist before Ozon collection starts.
- Seed attribute template collection is completed before Ozon product collection.
- The Ozon task did not use raw Chinese seed text as the direct Ozon search
  query.
- Accepted candidates do not match the existing-store dedupe list.
- Required identity fields are present.
- Domestic seller decision contains evidence.
- Ozon category and attribute data are present.
- Ozon content-score optimization evidence is present.
- One Ozon target SKU is present.
- Ozon selected-SKU media is present when product images are available.
- Full Ozon SKU variation structure is not required and must not be required.
- 1688 exact-match decision contains evidence.
- One matched 1688 supplier SKU is present.
- Real 1688 domestic shipping fee, destination, and raw evidence are present.
- A generic free-shipping label is rejected as shipping-fee evidence, but a
  concrete one-piece/selected-quantity delivery line showing `包邮` is accepted
  as `¥0`.
- Full 1688 SKU variation structure is not required and must not be required.
- Single-SKU truth policy is applied.
- Rejected or manual-review records include reasons.
- Run finalization removes every sampled seed from the active seed pool and
  writes the used-seed archive.

## Evidence CSV Direction

The evidence CSV must include enough columns for manual auditing:

```text
pair_id
run_id
seed_pool_version
seed_id
seed_title_or_keyword
seed_source_language
ozon_query_terms_ru
query_generation_method
dedupe_checked_at
existing_store_duplicate
existing_store_duplicate_reason
final_decision
ozon_url
ozon_title
ozon_seller_name
domestic_seller_confidence
delivery_origin
delivery_time
fulfillment_label
category_path
hot_product_signals
ozon_target_sku_options
ozon_image_reference_count
ozon_selected_sku_image_reference_count
supplier_url
supplier_title
supplier_shop_name
exact_match_confidence
match_evidence_summary
matched_supplier_sku_options
supplier_domestic_shipping_fee
supplier_domestic_shipping_destination
supplier_domestic_shipping_evidence
single_sku_truth_notes
review_notes
captured_at
```

## Open Questions For Later Steps

These are intentionally not solved in this step:

- The seed eligibility state store location.
- The exact seed query generation implementation.
- The used-seed archive file path and exact file format.
- The existing-store dedupe source adapter and refresh cadence.
- The exact uploaded/deduped history file format.
- The exact random seed generation method.
- Exact numeric threshold for "hot product".
- Which Ozon discovery pages to use first.
- Whether image matching will be manual, browser-assisted, or model-assisted.
- The final CSV filename and runtime directory layout.
- The exact Python model class names.

These must be decided in small follow-up steps under the same architecture
contract.
