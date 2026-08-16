---
name: ozon-store-content-risk-optimizer
description: Automatically audit and optimize evidence-supported non-media content for all eligible Ozon store products, verify listing risk, resume incrementally, and guard inventory changes. Use when improving Ozon titles, descriptions, Rich Content, attributes, content score, or listing safety outside the workbench UI.
---

# Ozon Store Content Risk Optimizer

Run this standalone companion skill from the Ozon V2 repository root. It uses a per-store SQLite ledger, processes all unarchived products on the first run, and skips unchanged completed products on later runs. It is automatic: Do not ask the user to approve routine safe optimization.

Read [optimization-policy.md](references/optimization-policy.md) completely before drafting any product. Use the script as the only mutation path:

```powershell
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py audit
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py scan
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py scan --problems-only
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py scan --problems-only --product-id PRODUCT_ID
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py next
@'
{"base_fingerprint":"task fingerprint","product_id":"task product ID","name":"Russian title","description":"Russian description","rich_content":{},"storefront_observations":[],"attribute_decisions":[],"risk_findings":[]}
'@ | python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py apply --proposal -
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py status
```

## Automatic loop

1. Run `audit` first. It is read-only and lists every catalog product, normalized sale state, Seller validation risks, missing required attributes, and the official `/v1/product/rating-by-sku` score groups. The script excludes `media` and renormalizes only `text` plus `other_attributes`.
2. Run `scan` for a new store. For a previously processed store, run `scan --problems-only`; it requeues low/unavailable scores, current Seller errors, preserved semantic risks, and changed locked evidence without repeating unchanged problem-free products. Seller errors, status changes, schema changes, score changes, and exact locked evidence are part of the fingerprint.
3. Run `next`. Stop normally only when it returns `{"status":"empty"}`.
4. For the returned task, open its exact public `product_url` when accessible. Record only visible high-impact facts in `storefront_observations`; if access fails, use an empty list and never guess.
5. Draft a precise, natural Russian title, description, text-only Rich Content, and every valuable evidence-supported attribute. Keep every objective field unchanged when evidence is insufficient. An existing Seller title or description may support a missing value only when the proposed value occurs there exactly. Dictionary values are resolved exactly through the current Seller API before validation.
6. Perform a semantic self-review for fluent Russian, correct product meaning, internal consistency, non-repetition, correct numeric separators, and absence of supplier or internal workflow language such as `locked-вариант`. Rewrite failed fields automatically.
7. Pipe the complete proposal to `apply`. The script performs deterministic validation, asynchronous Seller API confirmation, read-back, rollback, risk gating, and inventory guards.
   If a title-only Seller API task is accepted but exact title read-back is unchanged, keep `write_rejected_no_change`; do not touch stock. Use the exact Seller edit page `/app/products/{product_id}/edit/general-info`, change only `名称`, submit review, and require exact Seller API read-back before continuing. If the visible title is already a fully duplicated string, the CLI may use the sanitized import path for that exact recovery only; it must still verify exact read-back and rollback on failure.
8. Continue after per-product terminal states. Only `completed` is verified success. `score_unverified`, `score_below_target`, `evidence_insufficient`, `pending_risk`, `paused`, and `rolled_back` are explicit non-success outcomes and must never be reported as optimized.
9. Run `audit` and `status` at the end. Report every product state plus action/result counts without credentials, API keys, customer data, or posting payloads. Never say the run is complete unless `status.run_complete` is `true`; otherwise report the exact unresolved count and products.

## Hard boundaries

- Do not generate, modify, upload, or delete images or video. Do not inspect media as product evidence. Exclude media and media score completely.
- Never archive a product. Archived products are always skipped unless the user gives a separate explicit reactivation instruction outside this skill.
- Never place Chinese, 1688 data, supplier identity, internal workflow terms such as `locked`, wholesale, dropshipping, factory-shipping, or unsupported claims in customer-visible content.
- Never guess quantity, set composition, dimensions, weight, capacity, power, voltage, material, compatibility, brand, model, warranty, certification, or country.
- Reject ambiguous numeric text such as a lost decimal, multiplication, range, minus, or degree separator. Never introduce a customer-visible number absent from current content or exact objective evidence.
- Treat Seller API errors, failed validation, `is_created=false`, missing required attributes, and failed statuses as deterministic risks. A successful HTTP/import response never clears them by itself; current Seller read-back must clear them.
- Automatically repair meaning-preserving syntax warnings before risk pausing. In particular, normalize spaces inside existing hashtags to underscores and verify Seller read-back; keep unrelated dimensions, weight, quantity, category, or type risks open instead of skipping the tag repair.
- Treat exact `workbench_user` package dimensions and weight as authoritative evidence; do not recollect them from the supplier. Revalidate their Ozon density before every repair. For `is_created=false`, combine corrected syntax and valid confirmed package fields in one full product import instead of using attribute-only update. If the confirmed values themselves fail density validation, pause with the exact contradiction and never alter or guess them.
- Preserve unresolved semantic risks across rescans. A category/type mismatch cannot be cleared by rewriting text or filling attributes from the wrong schema.
- Use only tasks returned by `next` and proposals submitted through the CLI `apply` command. Never modify queued task evidence in memory or invoke imported mutation helpers; the integrity fingerprint must reject either shortcut.
- If workbench evidence or a locked supplier SKU is missing, optimize only language supported by current Seller API facts, preserve objective attributes, record `evidence_insufficient`, and do not restore stock.
- For an already `IN_SALE` product, never overwrite inventory.
- After verified safe optimization with an available non-media score at or above the policy target, an eligible unarchived zero-stock product may receive FBS stock 10.
- If the official non-media score is unavailable, use `score_unverified`; do not claim completion and do not restore stock.
- An unresolved severe risk, or a failed high-risk repair, uses guarded FBS stock 0 and never archive.
- Check active orders and require exactly one active RFBS warehouse immediately before any stock mutation.
- If write verification fails, rollback the complete original payload. If rollback fails, attempt guarded FBS stock 0 and retain an auditable failure state.

Stop the whole run only for missing credentials, an unavailable Seller API, or a systemic script/database failure. Do not involve the user for ordinary field rewriting, evidence-insufficient skips, or safe per-product risk handling.
