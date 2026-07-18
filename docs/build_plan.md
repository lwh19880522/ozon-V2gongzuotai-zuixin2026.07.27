# Ozon V2 Build Plan v0.1

This plan follows `ARCHITECTURE.md` and `docs/collection_contract.md`.

The guiding rule is: build structure first, then capability. Do not add
collection behavior before the gates and state contracts exist.

## Current Status

Completed:

- Architecture contract.
- Collection contract.
- Single-SKU collection rule.
- Existing-store dedupe gate.
- Active seed pool gate.
- Used-seed removal rule.

Not built yet:

- Python package scaffold.
- FastMCP server.
- Runtime filesystem layout.
- Seed pool reader/writer.
- Seed query generator for Ozon market search terms.
- Credential assistant for seller store ID and API key.
- Existing store dedupe adapter.
- Collection task contract generator.
- Ingest validation.
- Evidence CSV writer.

## Build Order

### Step 1: Code Skeleton Only

Layer:

- FastMCP, Application, Domain, Adapter skeletons.

Files:

```text
.codex-plugin/plugin.json
.mcp.json
mcp/server.py
mcp/lifespan.py
src/ozon_v2/__init__.py
src/ozon_v2/app/__init__.py
src/ozon_v2/app/context.py
src/ozon_v2/app/result.py
src/ozon_v2/tools/__init__.py
src/ozon_v2/tools/doctor.py
src/ozon_v2/tools/runs.py
src/ozon_v2/tools/ingest.py
src/ozon_v2/resources/__init__.py
src/ozon_v2/resources/run_status.py
src/ozon_v2/resources/artifacts.py
src/ozon_v2/domain/__init__.py
src/ozon_v2/domain/models.py
src/ozon_v2/domain/policies.py
src/ozon_v2/domain/state_machine.py
src/ozon_v2/domain/validators.py
src/ozon_v2/services/__init__.py
src/ozon_v2/services/run_service.py
src/ozon_v2/services/seller_history_service.py
src/ozon_v2/services/collection_contract_service.py
src/ozon_v2/adapters/__init__.py
src/ozon_v2/adapters/fs_repo.py
src/ozon_v2/adapters/seller_api.py
src/ozon_v2/adapters/playwright_mcp_contract.py
tests/
```

This step will not:

- Implement collection.
- Read Ozon or 1688.
- Read old plugin data.
- Generate upload tasks.
- Create runtime run records.

Test:

- Python imports succeed.
- `mcp/server.py` contains only FastMCP registration shape.
- No file imports old plugin modules or old runtime paths.

### Step 2: Domain Models And Policies

Layer:

- Domain.

Files:

```text
src/ozon_v2/domain/models.py
src/ozon_v2/domain/policies.py
src/ozon_v2/domain/validators.py
tests/test_domain_models.py
tests/test_collection_policies.py
```

Build:

- `SeedProduct`
- `SeedSearchQuery`
- `ExistingStoreProduct`
- `DedupeDecision`
- `OzonCandidate`
- `TargetSku`
- `SelectedSkuMedia`
- `SupplierMatch`
- `MatchedSupplierSku`
- `CollectionPair`
- run and pair status enums

Rules to encode:

- Existing store product dedupe is first gate.
- Single SKU only.
- Chinese seed text is product intent, not a direct Ozon search query.
- Ozon collection requires generated Russian-first search terms.
- One sampled seed produces at most one accepted pair.
- Exact same product required.
- No multi-SKU collection requirement.

This step will not:

- Do filesystem IO.
- Generate Playwright tasks.
- Call Seller API.

Test:

- Unit tests for duplicate blocking.
- Unit tests for single-SKU validation.
- Unit tests rejecting missing seed IDs.

### Step 3: Runtime Filesystem Adapter

Layer:

- Adapter.

Files:

```text
src/ozon_v2/adapters/fs_repo.py
tests/test_fs_repo.py
```

Runtime data root:

```text
E:\ozon-V2工作区\OzonOpsV2
```

Build runtime layout:

```text
OzonOpsV2/
  config/
    seed_pool.initial.json
  state/
    seed_pool.active.json
    seed_pool.used.jsonl
    existing_store_dedupe.jsonl
  runs/
    {run_id}/
      run.json
      sampled_seeds.json
      collection_pairs.jsonl
      evidence.csv
      artifacts/
```

Bundled plugin seed assets:

```text
assets/
  seed_pool/
    Ozon_2000精细子类目种子池.txt
    seed_pool.initial.json
    manifest.json
```

This step will not:

- Put business code under runtime data.
- Read old `D:\OzonOps`.
- Share old run IDs.

Test:

- Creates directories idempotently.
- Initializes active seed pool from `assets/seed_pool/seed_pool.initial.json`
  only when missing.
- Does not restore used seeds from initial package.

### Step 4: Seed Sampling Service

Layer:

- Application service with Domain policies.

Files:

```text
src/ozon_v2/services/run_service.py
src/ozon_v2/domain/policies.py
tests/test_seed_sampling.py
```

Build:

- `start_run(target_count)`
- load existing-store dedupe
- filter active seeds
- sample `target_count`
- require or create Ozon market search queries for sampled seeds before Ozon
  collection
- write run record and sampled seed evidence
- fail with `insufficient-seeds` when needed

This step will not:

- Start Ozon collection.
- Remove seeds before run finalization.
- Use free browsing to fill missing count.
- Use raw Chinese seed text as an Ozon search query.

Test:

- Sampling is without replacement.
- Used seeds are excluded.
- Existing-store duplicates are excluded.
- Not enough eligible seeds returns explicit failure.
- Missing Ozon query terms blocks Ozon collection.

### Step 4A: Seed Query Generation Service

Layer:

- Application service with Adapter boundary.

Files:

```text
src/ozon_v2/services/seed_query_service.py
src/ozon_v2/domain/models.py
src/ozon_v2/domain/validators.py
tests/test_seed_query_service.py
```

Build:

- Convert sampled Chinese seed intent into Russian-first Ozon search terms.
- Preserve original Chinese seed text for audit.
- Store query generation method, confidence, and generated terms.
- Return `needs_query_generation` when a query cannot be generated safely.

This step will not:

- Search Ozon.
- Translate all 500 seeds eagerly unless explicitly requested.
- Treat Chinese seed text as a valid Ozon direct query.

Test:

- Chinese seed without generated Ozon query is blocked.
- Generated query stays linked to `seed_id`.
- Ozon query terms are present in the task contract.

### Step 5: Existing Store Dedupe Refresh

Layer:

- Adapter + Application + FastMCP thin tool.

Files:

```text
src/ozon_v2/adapters/seller_api.py
src/ozon_v2/services/seller_history_service.py
src/ozon_v2/tools/dedupe.py
tests/test_existing_store_dedupe.py
```

Build:

- Read existing store products through Ozon Seller API `/v3/product/list`.
- Fetch product details through `/v3/product/info/list`.
- Write `existing_store_dedupe.jsonl` and `existing_store_dedupe.meta.json`.
- Register `ozon_v2_refresh_existing_store_dedupe`.
- Normalized identity keys for blocking duplicates.

This step will not:

- Print or log raw API keys.
- Guess duplicates from weak evidence as accepted matches.

Test:

- Existing product blocks matching seeds and candidates.
- Uncertain duplicate evidence returns manual review.
- Refresh from adapter writes the dedupe refresh marker.

### Step 5A: Credential Assistant

Layer:

- Application + Adapter + FastMCP thin tools.

Files:

```text
src/ozon_v2/domain/credentials.py
src/ozon_v2/adapters/credential_prompt.py
src/ozon_v2/adapters/credential_prompt_window.py
src/ozon_v2/services/credential_service.py
src/ozon_v2/tools/credentials.py
tests/test_credential_prompt.py
tests/test_credentials.py
```

Build:

- Runtime credential template writer.
- Runtime local credential saver.
- Safe credential status reporter.
- Doctor status fields for credential readiness.
- Doctor must report full store dedupe readiness only when the runtime dedupe
  refresh marker is present, not from credentials alone.
- `start_run` gate that blocks full store-deduped runs when credentials are
  missing.
- Automatic local credential assistant popup when `start_run` detects missing
  credentials.
- Runtime state lock and cooldown so repeated automatic attempts do not open
  duplicate popups.

This step will not:

- Store real credentials in project code.
- Print raw API keys.
- Write API keys to Obsidian.
- Call the real Seller API.

Test:

- Missing credentials report `credentials.missing`.
- Saved credentials return masked API key only.
- Doctor reports readiness without exposing the key.
- `start_run` fails with `run.credentials_missing` when credentials are absent.
- Missing-credential `start_run` requests the credential assistant.
- Repeated popup attempts return `credential_assistant.already_open` or
  `credential_assistant.cooldown` instead of launching duplicate windows.

### Step 6: Collection Task Contract Generator

Layer:

- Application + Adapter contract.

Files:

```text
src/ozon_v2/services/collection_contract_service.py
src/ozon_v2/adapters/playwright_mcp_contract.py
tests/test_collection_contract_service.py
```

Build:

- Generate Ozon task contracts from sampled seeds.
- Include generated `ozon_query_terms_ru` in Ozon task contracts.
- Generate seed attribute-template task contracts immediately after seed
  selection/query generation and before Ozon product collection.
- Attribute-template contracts must capture category candidates, upload attribute
  schema, and draft prefill policy.
- Draft prefill policy allows objective factual fields to match the Ozon
  reference, but requires title, description, rich content, marketing text, SEO
  keywords, and image text to be rewritten.
- Require precise category evidence in Ozon task contracts:
  `category_path`, `leaf_category`, `category_url`, and `category_id`.
- Require detailed Ozon content-score evidence in Ozon task contracts:
  title, description/rich content, attributes, media, price/promo, rating,
  reviews, seller, delivery, and missing-field reasons.
- Generate 1688 exact-match task contracts from accepted Ozon target SKUs.
- Output structured JSON instructions for Codex to execute through Playwright MCP.

This step will not:

- Embed Playwright.
- Run browser automation.
- Parse webpages directly.

Test:

- Contract includes seed IDs.
- Contract includes existing-store dedupe context.
- Contract uses generated Ozon query terms, not raw Chinese seed text.
- Contract requires one target SKU and one matched supplier SKU.

### Step 7: Ingest Validation And Evidence CSV

Layer:

- Application + Domain.

Files:

```text
src/ozon_v2/tools/ingest.py
src/ozon_v2/services/run_service.py
src/ozon_v2/domain/validators.py
tests/test_ingest_collection_output.py
```

Build:

- Validate Playwright MCP output.
- Reject missing seed IDs.
- Reject duplicates.
- Reject non-exact 1688 matches.
- Reject 1688 matches that do not include real domestic shipping fee evidence.
- Reject free-shipping labels as a substitute for domestic shipping fee.
- Write `collection_pairs.jsonl`.
- Write `evidence.csv`.
- Finalize run and move sampled seeds from active pool to used archive.

This step will not:

- Create upload-ready products.
- Generate images.
- Upload anything to Ozon.

Test:

- Valid pair writes evidence.
- Duplicate pair rejected.
- Missing supplier domestic shipping fee rejected.
- Free-shipping label rejected as supplier shipping fee.
- Finalization removes all sampled seeds from active pool.
- Used seed archive is written.

### Step 8: Thin FastMCP Tools

Layer:

- FastMCP.

Files:

```text
mcp/server.py
src/ozon_v2/tools/doctor.py
src/ozon_v2/tools/runs.py
src/ozon_v2/tools/ingest.py
src/ozon_v2/resources/run_status.py
src/ozon_v2/resources/artifacts.py
```

Build tools:

- `ozon_v2_doctor`
- `ozon_v2_credentials_template`
- `ozon_v2_credentials_status`
- `ozon_v2_open_credential_assistant`
- `ozon_v2_save_credentials`
- `ozon_v2_refresh_existing_store_dedupe`
- `ozon_v2_status`
- `ozon_v2_start_run`
- `ozon_v2_next`
- `ozon_v2_ingest_collection_output`

This step will not:

- Put business logic in `mcp/server.py`.
- Let a tool call multiple unrelated services.
- Add legacy compatibility.

Test:

- Tool functions are thin.
- Server imports and registers tools.
- Doctor reports paths, seed pool state, and dedupe readiness.

## First Implementation Step

The next implementation step should be Step 1: Code Skeleton Only.

Before starting Step 1, state:

- Layer: FastMCP + Application + Domain + Adapter skeletons.
- Files to create: the scaffold listed in Step 1.
- Will not do: collection, browser automation, runtime writes, old plugin imports.
- Test: imports and boundary scan.
