# Ozon/1688 Collection Review Gate Design

## Goal

Before image processing starts, show the persisted Ozon and 1688 collection evidence side by side and require an explicit user approval. Supplier media must contain product images only.

## Scope

- Reuse the existing supplier review route as the combined collection review page.
- Show every persisted public Ozon field and every persisted public 1688 field, including missing-field indicators.
- Stop autopilot at `supplier_collected` until the user approves the result.
- Filter 1688 media at collection time so SVG UI assets, small icons, duplicate thumbnails, logos, and unrelated page images are excluded.
- Correct supplier title extraction so the product title is not replaced by the company name.
- Keep existing run artifacts; do not delete or rewrite historical results silently.

## Data Flow

1. Ozon browser collection is validated and persisted.
2. The collection review page displays the complete Ozon evidence while the user supplies the verified 1688 URL.
3. The 1688 browser bridge collects and persists the supplier evidence.
4. The state becomes `supplier_collected`; autopilot stops with `collection_review_required`.
5. The same review page displays Ozon and 1688 evidence, image galleries, and completeness indicators.
6. The user clicks `确认采集结果 (Approve Collection)`.
7. The existing `start_image_processing` action advances the batch to image processing.

## Review Page

Each product row contains:

- Ozon: product ID, title, seller, category path, selected SKU, price, rating/review count, attributes, delivery and Chinese cross-border evidence, description evidence, and source images.
- 1688: offer ID, product title, supplier, price, SKU evidence, attributes, domestic freight evidence, source URL, and filtered product images.
- Completeness: explicit collected/missing status for required fields.
- Controls: supplier link entry during `supplier_review`; collection progress during `supplier_collecting`; approval during `supplier_collected`.

The page must not redirect to image processing automatically.

## Image Acceptance

The 1688 collector accepts images from product-gallery, SKU, or detail-content regions and the product Open Graph image. It rejects SVG files, known UI/icon/logo assets, images whose intrinsic size is below the product-image threshold, and thumbnail duplicates when a full-size form of the same image exists.

## Error Handling

- Missing required fields stay visible and block approval.
- The API rejects approval unless supplier collection is complete and all required evidence is present.
- Existing batches already in image processing remain readable from the review route; their artifacts are not deleted.
- A recollection produces a new preserved result only through an explicit user action in a future change; it is not part of this fix.

## Verification

- JavaScript collector tests prove icons and thumbnails are rejected while product images remain.
- Service tests prove autopilot stops at `supplier_collected`.
- API/page tests prove full Ozon and supplier evidence is returned and approval advances to `image_processing`.
- Existing Ozon collection, supplier collection, runner, and workbench tests remain green.

