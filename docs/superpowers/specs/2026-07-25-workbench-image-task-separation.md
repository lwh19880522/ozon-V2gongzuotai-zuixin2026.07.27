# Workbench Image Task Separation Design

## Goal

Separate final image generation and image upload from the Ozon V2 workbench. The
workbench uploads a product with exactly one already locked 1688 subject image,
then emits a durable image-generation-and-upload task package for the image Skill.
Generated images never return to the workbench.

## Confirmed Ozon Constraint

Ozon product import requires at least one image. A product therefore cannot be
created with an empty `images` array. The workbench will use the first URL already
locked in `subject_master.source_image_urls` without downloading, padding,
transcoding, regenerating, or publishing it through Cloudflare R2.

## Workbench Boundary

The workbench remains responsible for:

- category template and required attribute completion;
- original Russian content and pricing evidence;
- locked 1688 SKU and subject evidence;
- explicit product-upload preview and confirmation;
- product submission to Ozon with exactly one locked original image URL;
- durable creation of one image task package after Seller API accepts the product
  submission.

The workbench is no longer responsible for:

- waiting for eight generated images;
- reading accepted image slots into the upload draft;
- displaying or reviewing the final image gallery;
- publishing generated images to Cloudflare R2;
- uploading the final image set to Ozon;
- receiving generated-image paths or image-review results.

## Upload Flow

1. The user locks one real 1688 SKU and subject evidence.
2. The workbench selects `subject_master.source_image_urls[0]`.
3. Upload preview builds the normal Seller API product payload with:
   - `images: [locked_original_url]`;
   - `primary_image: locked_original_url`.
4. The user explicitly confirms the product upload.
5. Ozon returns an import `task_id`.
6. The workbench writes one task package atomically to:
   `OzonOpsV2/image_tasks/pending/<package_id>.json`.
7. Workbench processing for that product is complete. It does not wait for image
   generation or image replacement.

## Task Package Contract

Each package is self-contained and contains no API secrets:

```json
{
  "schema_version": 1,
  "kind": "ozon_product_image_generation_and_upload",
  "package_id": "imgpkg-...",
  "status": "pending",
  "created_at": "...",
  "run_id": "wb-...",
  "seed_id": "seed-...",
  "store_target": {
    "credential_ref": "configured_store",
    "seller_import_task_id": 7001,
    "offer_id": "OZV2-...",
    "product_id": null
  },
  "bootstrap_image": {
    "source": "locked_1688_subject_original",
    "url": "https://...",
    "subject_master_sha256": "..."
  },
  "generation_contract": {
    "slot_count": 8,
    "aspect_ratio": "3:4",
    "direct_ozon_upload": true,
    "replace_complete_gallery": true,
    "return_to_workbench": false
  },
  "evidence": {
    "subject_master": {},
    "locked_supplier_sku": {},
    "ozon_reference_images": []
  }
}
```

The Skill may update or move task-package files after claiming them. The
workbench never rewrites a package already emitted for the same successful
submission.

## Skill Boundary

The image Skill periodically scans the fixed pending directory. It resolves the
Ozon `product_id` from the Seller import task when necessary, generates and
validates all eight 3:4 images, publishes them to the configured public media
channel, and calls Ozon's picture-import endpoint with the complete ordered image
array. That endpoint replaces the temporary original image.

The Skill writes its receipts in the image-task area, not in workbench batch
artifacts. Failed packages remain retryable and do not block other products.

## Compatibility

- Existing image queue data and old batch artifacts remain readable.
- Existing image queue data and file APIs remain readable for diagnostics, but
  the image workspace page is removed; an old `/images` bookmark redirects to
  the upload page.
- Existing product submissions remain idempotent.
- Retrying a failed product submission may create a new package only when the
  Seller import `task_id` changes.

## Verification Boundary

All automated tests use fake Seller API and filesystem adapters. Tests must not
call real product import, R2, image generation, or picture upload.
