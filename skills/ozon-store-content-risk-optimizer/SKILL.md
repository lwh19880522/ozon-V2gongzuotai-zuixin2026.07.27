---
name: ozon-store-content-risk-optimizer
description: Automatically audit and optimize evidence-supported non-media content for all eligible Ozon store products, verify listing risk, resume incrementally, and guard inventory changes. Use when improving Ozon titles, descriptions, Rich Content, attributes, content score, or listing safety outside the workbench UI.
---

# Ozon Store Content Risk Optimizer

Run this standalone companion skill from the Ozon V2 repository root. It uses a per-store SQLite ledger, processes all unarchived products on the first run, and skips unchanged completed products on later runs. It is automatic: Do not ask the user to approve routine safe optimization.

Read [optimization-policy.md](references/optimization-policy.md) completely before drafting any product. Use the script as the only mutation path:

```powershell
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py scan
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py next
@'
{"base_fingerprint":"task fingerprint","product_id":"task product ID","name":"Russian title","description":"Russian description","rich_content":{},"storefront_observations":[],"attribute_decisions":[],"risk_findings":[]}
'@ | python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py apply --proposal -
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py status
```

## Automatic loop

1. Run `scan` once. It queues new, changed, interrupted, or explicitly rechecked products and preserves the incremental SQLite ledger.
2. Run `next`. Stop normally only when it returns `{"status":"empty"}`.
3. For the returned task, open its exact public `product_url` when accessible. Record only visible high-impact facts in `storefront_observations`; if access fails, use an empty list and never guess.
4. Inspect existing images only when supplied in `read_only_images`, and only for identity, color, quantity, and set-composition consistency. Missing images do not lower the result or block the task.
5. Draft a precise, natural Russian title, description, non-media Rich Content, and evidence-supported attributes. Keep every objective field unchanged when evidence is insufficient.
6. Perform a semantic self-review for fluent Russian, correct product meaning, internal consistency, non-repetition, and absence of supplier language. Rewrite failed fields automatically.
7. Pipe the complete proposal to `apply`. The script performs deterministic validation, asynchronous Seller API confirmation, read-back, rollback, and inventory guards.
8. Continue after safe per-product states such as `completed`, `evidence_insufficient`, `paused`, `pending_risk`, `rolled_back`, or `skipped_archived`.
9. Run `status` at the end and report counts without credentials, API keys, customer data, or posting payloads.

## Hard boundaries

- Do not generate, modify, upload, or delete images or video. Exclude media score completely.
- Never archive a product. Archived products are always skipped unless the user gives a separate explicit reactivation instruction outside this skill.
- Never place Chinese, 1688 data, supplier identity, wholesale, dropshipping, factory-shipping, or unsupported claims in customer-visible content.
- Never guess quantity, set composition, dimensions, weight, capacity, power, voltage, material, compatibility, brand, model, warranty, certification, or country.
- If workbench evidence or a locked supplier SKU is missing, optimize only language supported by current Seller API facts, preserve objective attributes, record `evidence_insufficient`, and do not restore stock.
- For an already `IN_SALE` product, never overwrite inventory.
- After verified safe optimization, an eligible unarchived zero-stock product may receive FBS stock 10.
- An unresolved severe risk, or a failed high-risk repair, uses guarded FBS stock 0 and never archive.
- Check active orders and require exactly one active RFBS warehouse immediately before any stock mutation.
- If write verification fails, rollback the complete original payload. If rollback fails, attempt guarded FBS stock 0 and retain an auditable failure state.

Stop the whole run only for missing credentials, an unavailable Seller API, or a systemic script/database failure. Do not involve the user for ordinary field rewriting, evidence-insufficient skips, or safe per-product risk handling.
