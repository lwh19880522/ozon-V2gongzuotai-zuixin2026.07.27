# Ozon Store Content Risk Optimizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one standalone Codex skill that automatically improves every eligible Ozon product's evidence-supported non-media content, blocks unsafe writes, pauses unresolved severe-risk offers, and safely restores eligible zero-stock offers to FBS stock 10 without repeatedly processing unchanged products.

**Architecture:** Keep all new runtime logic in one skill-owned Python script. The script reads Seller API and existing workbench evidence, stores a per-store SQLite ledger, emits one deterministic drafting task at a time, validates the Codex-produced Russian proposal, writes the complete product payload while preserving media byte-for-byte, verifies the asynchronous import, rolls back unsafe results, and performs guarded FBS stock actions. `SKILL.md` supplies the automatic Codex loop and the reference file supplies the compact content/risk policy; no workbench UI, service, batch controller, media generator, or plugin manifest changes are needed.

**Tech Stack:** Python 3.11 standard library (`argparse`, `hashlib`, `json`, `pathlib`, `re`, `sqlite3`, `time`, `unicodedata`), existing `FsRepo` and `SellerApiAdapter`, Ozon Seller API, pytest, PowerShell installer/doctor.

---

## File map and non-goals

Create:

- `skills/ozon-store-content-risk-optimizer/SKILL.md`
- `skills/ozon-store-content-risk-optimizer/agents/openai.yaml`
- `skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py`
- `skills/ozon-store-content-risk-optimizer/references/optimization-policy.md`
- `tests/test_ozon_store_content_risk_optimizer.py`
- `tests/test_ozon_store_content_risk_optimizer_skill.py`

Modify:

- `scripts/install_codex_skills.ps1`
- `scripts/verify_ozon_v2_install.ps1`
- `tests/test_installation_contract.py`

Do not modify `.codex-plugin/plugin.json`, `src/ozon_v2/workbench/`, the image-generation skills, or any media upload method. Do not add packages, a new service, a second runtime script, or a new database abstraction.

## Fixed runtime contract

The skill invokes the script from the repository root:

```powershell
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py scan
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py next
@'
{"base_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","product_id":"5401900424","name":"Настенный светильник LED 5 Вт","description":"Компактный настенный светильник с питанием от USB.","rich_content":{},"attribute_decisions":[],"risk_findings":[]}
'@ | python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py apply --proposal -
python skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py status
```

Every command writes exactly one UTF-8 JSON object to stdout. Human-readable diagnostics go to stderr. Exit code `0` means the command completed safely, including a deliberate skip or safe pause; exit code `1` means the run itself failed and can be resumed.

The per-store ledger path is:

```text
<FsRepo.runtime_root>/store_content_optimizer/<sha256(client_id)[:16]>/optimizer.sqlite3
```

The API key is never stored, printed, hashed into product state, or included in an exception.

## Task 1: Scaffold the lean standalone skill

**Files:**

- Create: `skills/ozon-store-content-risk-optimizer/SKILL.md`
- Create: `skills/ozon-store-content-risk-optimizer/agents/openai.yaml`
- Create: `skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py`
- Create: `skills/ozon-store-content-risk-optimizer/references/optimization-policy.md`
- Create: `tests/test_ozon_store_content_risk_optimizer_skill.py`

- [ ] **Step 1: Generate the required skill skeleton with the official scaffold script**

Run:

```powershell
python "C:\Users\林伟华\.codex\skills\.system\skill-creator\scripts\init_skill.py" ozon-store-content-risk-optimizer --path skills --resources scripts,references --interface display_name="Ozon Store Content Risk Optimizer" --interface short_description="Optimize Ozon listing content and stop unsafe offers" --interface default_prompt="Use $ozon-store-content-risk-optimizer to automatically optimize eligible Ozon store content, verify risks, preserve media, and resume from its ledger."
```

Expected: the skill directory, `SKILL.md`, and `agents/openai.yaml` are created. Remove only example files created by the scaffold; retain the four-file runtime structure from the design.

- [ ] **Step 2: Write the first failing static contract test**

Create `tests/test_ozon_store_content_risk_optimizer_skill.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "ozon-store-content-risk-optimizer"


def test_skill_has_only_the_approved_runtime_files() -> None:
    files = {
        path.relative_to(SKILL_ROOT).as_posix()
        for path in SKILL_ROOT.rglob("*")
        if path.is_file()
    }
    assert files == {
        "SKILL.md",
        "agents/openai.yaml",
        "references/optimization-policy.md",
        "scripts/store_content_optimizer.py",
    }


def test_skill_entrypoint_exposes_the_fixed_commands() -> None:
    script = (SKILL_ROOT / "scripts" / "store_content_optimizer.py").read_text(
        encoding="utf-8"
    )
    for command in ("scan", "next", "apply", "status"):
        assert f'add_parser("{command}")' in script
```

- [ ] **Step 3: Run the static test and confirm the entrypoint is missing**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer_skill.py -q
```

Expected: `test_skill_entrypoint_exposes_the_fixed_commands` fails because the generated skill has no implemented script.

- [ ] **Step 4: Add the minimal importable CLI entrypoint and empty policy file**

Create `scripts/store_content_optimizer.py` with this exact initial shape:

```python
from __future__ import annotations

import argparse
import json
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan")
    subparsers.add_parser("next")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--proposal", default="-")
    subparsers.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload: dict[str, Any] = {"ok": True, "command": args.command}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `references/optimization-policy.md` containing only the heading `# Ozon non-media optimization policy`; Task 5 replaces it with the approved rules.

- [ ] **Step 5: Re-run the test**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer_skill.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit only the scaffold files and static test**

```powershell
git add skills/ozon-store-content-risk-optimizer tests/test_ozon_store_content_risk_optimizer_skill.py
git commit -m "feat: scaffold Ozon content risk optimizer skill"
```

## Task 2: Implement discovery, evidence linking, scoring, and the incremental ledger

**Files:**

- Create: `tests/test_ozon_store_content_risk_optimizer.py`
- Modify: `skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py`

- [ ] **Step 1: Add failing tests for store isolation, media-free scoring, archived skips, and repeat protection**

Use `importlib.util.spec_from_file_location` to import the skill script. Add these tests with a temporary database and a `FakeGateway` whose `fetch_catalog()` returns dictionaries:

```python
def test_store_key_never_contains_the_client_id(optimizer) -> None:
    key = optimizer.store_key("123456789")
    assert key == optimizer.hashlib.sha256(b"123456789").hexdigest()[:16]
    assert "123456789" not in key


def test_non_media_score_renormalizes_only_available_text_groups(optimizer) -> None:
    groups = {
        "text": {"earned": 35, "maximum": 40},
        "attributes": {"earned": 45, "maximum": 60},
        "media": {"earned": 0, "maximum": 30},
    }
    assert optimizer.non_media_score(groups) == 80


def test_scan_skips_archived_and_does_not_requeue_unchanged_completed(
    optimizer, tmp_path
) -> None:
    gateway = FakeGateway(
        products=[
            product("11", "sale-11", visibility="IN_SALE"),
            product("12", "archived-12", visibility="ARCHIVED"),
        ]
    )
    ledger = optimizer.Ledger(tmp_path / "optimizer.sqlite3")
    first = optimizer.scan_store(gateway, ledger, FakeEvidence(), rule_hashes())
    assert first == {"queued": 1, "skipped_archived": 1, "skipped_unchanged": 0}
    task = ledger.next_task()
    ledger.mark_completed("11", task["source_fingerprint"], "proposal-a")
    second = optimizer.scan_store(gateway, ledger, FakeEvidence(), rule_hashes())
    assert second == {"queued": 0, "skipped_archived": 1, "skipped_unchanged": 1}


def test_changed_content_and_affected_rule_domain_requeue_product(
    optimizer, tmp_path
) -> None:
    gateway = FakeGateway(products=[product("11", "sale-11")])
    ledger = optimizer.Ledger(tmp_path / "optimizer.sqlite3")
    optimizer.scan_store(gateway, ledger, FakeEvidence(), {"language": "a"})
    task = ledger.next_task()
    ledger.mark_completed("11", task["source_fingerprint"], "proposal-a")
    gateway.products[0]["name"] = "Изменённое название"
    assert optimizer.scan_store(gateway, ledger, FakeEvidence(), {"language": "a"})["queued"] == 1
    task = ledger.next_task()
    ledger.mark_completed("11", task["source_fingerprint"], "proposal-b")
    assert optimizer.scan_store(gateway, ledger, FakeEvidence(), {"language": "b"})["queued"] == 1
```

The shared fixture data must include `product_id`, `sku`, `offer_id`, `visibility`, `name`, `description`, `rich_content`, `attributes`, `images`, `primary_image`, `price`, and `stocks`. Do not include credentials in fixtures.

- [ ] **Step 2: Run the focused tests and confirm missing symbols**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer.py -q
```

Expected: failures for missing `store_key`, `non_media_score`, `Ledger`, and `scan_store`.

- [ ] **Step 3: Implement canonical fingerprints and the two-table SQLite ledger**

Add these public functions/classes to the single script:

```python
RULE_VERSION = "1.0.0"
MEDIA_KEYS = frozenset({"images", "primary_image", "images360", "video", "video_cover"})


def store_key(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_fingerprint(product: dict[str, Any]) -> str:
    tracked = {
        key: product.get(key)
        for key in (
            "product_id", "sku", "offer_id", "visibility", "name", "description",
            "rich_content", "attributes", "images", "primary_image", "price", "stocks",
            "description_category_id", "type_id",
        )
    }
    return hashlib.sha256(canonical_json(tracked).encode("utf-8")).hexdigest()


def non_media_score(groups: dict[str, dict[str, float]]) -> int | None:
    included = [value for key, value in groups.items() if key.casefold() != "media"]
    maximum = sum(float(item.get("maximum") or 0) for item in included)
    if maximum <= 0:
        return None
    earned = sum(float(item.get("earned") or 0) for item in included)
    return round(100 * earned / maximum)
```

`Ledger.__init__` must create exactly two tables:

```sql
CREATE TABLE IF NOT EXISTS product_state (
    product_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    rule_hashes_json TEXT NOT NULL,
    task_json TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_level TEXT,
    risk_codes_json TEXT NOT NULL DEFAULT '[]',
    proposal_fingerprint TEXT,
    original_snapshot_json TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, action, payload_hash)
);
```

Supported statuses are `queued`, `drafting`, `applying`, `completed`, `pending_risk`, `evidence_insufficient`, `paused`, and `failed`. Every transition commits immediately. `next_task()` atomically changes the oldest `queued` row to `drafting` and returns its saved task JSON; on startup, `scan_store()` converts stale `drafting`/`applying` rows back to `queued` after it first compares the live source fingerprint.

- [ ] **Step 4: Implement compact workbench evidence linking**

Add `WorkbenchEvidence(FsRepo)` in the same script. It must:

1. iterate `repo.list_runs()` newest first;
2. read `load_upload_submissions(run_id)` and `load_upload_previews(run_id)`;
3. match exact normalized `offer_id` only;
4. attach `load_supplier_sku_selections(run_id)`, `load_required_attribute_evidence(run_id)`, and `load_pricing_evidence(run_id)` only from that same run;
5. mark `locked_supplier_sku=True` only when the matched run contains a confirmed locked SKU selection;
6. return `evidence_insufficient=True` when no exact match or no locked SKU exists.

Never fuzzy-match titles, SKUs, supplier URLs, or image filenames.

- [ ] **Step 5: Implement the read-only Seller gateway and scan rules**

`SellerGateway` wraps the existing adapter and uses its `_post_json` only for endpoints not already public on the adapter. The read path is:

```text
/v3/product/list                 visibility=ALL, paginated
/v3/product/info/list            chunks of at most 100 product IDs
/v4/product/info/attributes      chunks of at most 100 product IDs
```

Normalize the responses into the fixture shape. Preserve the raw `seller_api_item` needed for a complete later import. `scan_store()` must:

- skip `ARCHIVED` before task creation;
- queue all unarchived products on a new ledger;
- skip `completed` products only when both source fingerprint and relevant rule hashes are unchanged;
- keep an unchanged `pending_risk` or `evidence_insufficient` row out of the generation queue;
- requeue changed, interrupted, or explicitly forced products;
- persist only safe credential-free product/evidence data;
- record the current non-media score but never use missing media as a failure.

- [ ] **Step 6: Run the focused tests**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer.py -q
```

Expected: all discovery/ledger/scoring tests pass.

- [ ] **Step 7: Commit the discovery slice**

```powershell
git add skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py tests/test_ozon_store_content_risk_optimizer.py
git commit -m "feat: add incremental Ozon optimizer ledger"
```

## Task 3: Add deterministic content, evidence, and risk gates

**Files:**

- Modify: `tests/test_ozon_store_content_risk_optimizer.py`
- Modify: `skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py`

- [ ] **Step 1: Add failing validation tests**

Add table-driven tests that prove:

```python
@pytest.mark.parametrize(
    "bad_text",
    [
        "Настенный светильник 墙灯",
        "Товар 1688 от поставщика",
        "Опт, дропшиппинг и доставка от фабрики",
        "лампа лампа лампа лампа LED LED LED",
        "РЎРІРµС‚РёР»СЊРЅРёРє",
    ],
)
def test_customer_text_gate_rejects_forbidden_or_broken_text(optimizer, bad_text) -> None:
    proposal = valid_proposal()
    proposal["name"] = bad_text
    with pytest.raises(optimizer.ValidationError):
        optimizer.validate_proposal(task(), proposal)


def test_objective_attribute_requires_exact_evidence(optimizer) -> None:
    proposal = valid_proposal()
    proposal["attribute_decisions"] = [
        {"id": 6318, "decision": "set", "values": [{"value": "29"}], "evidence_refs": ["seller.attributes.6318"]}
    ]
    with pytest.raises(optimizer.ValidationError, match="objective evidence mismatch"):
        optimizer.validate_proposal(task(objective_value="1"), proposal)


def test_missing_workbench_evidence_keeps_objective_fields_and_blocks_stock(optimizer) -> None:
    proposal = valid_proposal(attribute_decisions=[])
    result = optimizer.validate_proposal(task(evidence_insufficient=True), proposal)
    assert result["evidence_insufficient"] is True
    assert result["allow_inventory_restore"] is False


def test_merge_preserves_every_media_value_exactly(optimizer) -> None:
    original = task()["seller_api_item"]
    merged = optimizer.merge_proposal(task(), valid_proposal())
    for key in optimizer.MEDIA_KEYS:
        assert merged.get(key) == original.get(key)


def test_rich_content_must_be_valid_non_media_json(optimizer) -> None:
    proposal = valid_proposal()
    proposal["rich_content"] = {"content": [{"widgetName": "raShowcase", "blocks": []}]}
    optimizer.validate_proposal(task(), proposal)
    proposal["rich_content"] = {"content": [{"widgetName": "video", "url": "https://example.test/v.mp4"}]}
    with pytest.raises(optimizer.ValidationError, match="media"):
        optimizer.validate_proposal(task(), proposal)
```

Also add cases for dictionary IDs outside the current category schema, numeric unit/object swaps, unsupported compliance claims, changed category/type IDs, price below known cost/minimum margin, and Seller API/storefront disagreement on quantity or dimensions.

- [ ] **Step 2: Run the new tests and confirm they fail**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer.py -q
```

Expected: failures for missing `ValidationError`, `validate_proposal`, `merge_proposal`, and risk classifiers.

- [ ] **Step 3: Implement the exact proposal schema and base-fingerprint guard**

`next` emits one task containing:

```json
{
  "product_id": "5401900424",
  "sku": "4995527952",
  "offer_id": "offer-id",
  "source_fingerprint": "sha256",
  "visibility": "IN_SALE",
  "seller_api_item": {},
  "current": {"name": "", "description": "", "rich_content": {}, "attributes": []},
  "attribute_schema": [],
  "objective_evidence": {},
  "read_only_images": [],
  "product_url": "https://www.ozon.ru/product/5401900424/",
  "storefront_facts": {},
  "pricing_evidence": {},
  "evidence_insufficient": false,
  "non_media_score": 0
}
```

`apply` accepts exactly:

```json
{
  "base_fingerprint": "sha256",
  "product_id": "5401900424",
  "name": "Russian title",
  "description": "Russian description",
  "rich_content": {},
  "storefront_observations": [
    {"field_key": "quantity", "value": "1", "source_url": "https://www.ozon.ru/product/5401900424/"}
  ],
  "attribute_decisions": [
    {"id": 6318, "decision": "keep", "values": [{"value": "1"}], "evidence_refs": ["seller.attributes.6318"]}
  ],
  "risk_findings": [
    {"code": "quantity_consistent", "level": "low", "resolution": "verified", "evidence_refs": ["seller.attributes.6318"]}
  ]
}
```

Reject unknown top-level keys, a product ID mismatch, or a stale `base_fingerprint` before any write. Each storefront observation must come from the task's exact `product_url`; an inaccessible storefront is recorded as unavailable and must never be replaced with a guessed value. Compare observed high-impact fields with current Seller API facts before classifying quantity, set, dimension, weight, capacity, power, and voltage divergence.

- [ ] **Step 4: Implement deterministic text and Rich Content validation**

Apply the gate to title, description, Rich Content text, and every free-text attribute:

- reject Unicode CJK ranges and common mojibake sequences;
- require at least one Cyrillic letter in each non-empty customer-visible text field;
- reject case-insensitive supplier phrases from the policy in Chinese, Russian, and English;
- reject four repeated identical content tokens or keyword repetition above 30% of content tokens;
- reject URLs, phone numbers, messenger handles, shipping promises, unsupported superlatives, guarantees, certifications, medical/safety claims, and invented brand/manufacturer statements;
- parse Rich Content as an object, allow only text/layout blocks, and reject keys or widget names containing `image`, `video`, `media`, `url`, or `cover`.

This deterministic gate does not decide writing quality by itself. `SKILL.md` must additionally require a Codex semantic self-review for natural Russian, product meaning, internal consistency, and non-redundancy before calling `apply`.

- [ ] **Step 5: Implement evidence-safe attribute merging and risk severity**

Use these rules:

- an attribute ID must exist in the current category/type schema;
- a dictionary value must use an allowed current dictionary ID;
- `decision=set` on quantity, set composition, dimensions, weight, capacity, power, voltage, material, compatibility, color, brand, model, warranty, certification, or country requires exact `evidence_refs` whose normalized value equals the proposal;
- with `evidence_insufficient=True`, all objective attributes, category, type, price, and stock remain unchanged;
- preserve attributes omitted from the proposal;
- preserve category/type, offer ID, barcode, price, VAT, dimensions, weight, images, video, and other operational fields unless the task contains explicit exact evidence authorizing the specific non-media change;
- quantity/set/unit/object conflicts and unsupported safety/compliance claims are `severe`;
- Seller API/storefront high-impact disagreement is `high` and requires read-back before sale can continue;
- language/content score defects are `medium` or `low` and are rewritten;
- price is read-only unless reliable cost and minimum-margin evidence exists; detected loss is `severe`, but missing cost evidence causes no price mutation.

- [ ] **Step 6: Re-run the focused tests**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer.py -q
```

Expected: all validation, merge, and risk tests pass.

- [ ] **Step 7: Commit the gate slice**

```powershell
git add skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py tests/test_ozon_store_content_risk_optimizer.py
git commit -m "feat: validate Ozon content and product risk"
```

## Task 4: Implement verified writes, rollback, and guarded FBS stock actions

**Files:**

- Modify: `tests/test_ozon_store_content_risk_optimizer.py`
- Modify: `skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py`

- [ ] **Step 1: Add failing fake-API state-machine tests**

Extend `FakeGateway` to record import and stock requests and simulate read-back. Cover:

```python
def test_in_sale_product_is_optimized_without_stock_overwrite(optimizer, runtime) -> None:
    gateway = FakeGateway(products=[product("11", "sale-11", visibility="IN_SALE", stock=7)])
    result = optimizer.apply_one(gateway, runtime.ledger, task(visibility="IN_SALE"), valid_proposal())
    assert result["status"] == "completed"
    assert gateway.stock_requests == []


def test_safe_ready_to_supply_product_gets_stock_ten_after_verified_import(optimizer, runtime) -> None:
    gateway = FakeGateway(products=[product("11", "sale-11", visibility="READY_TO_SUPPLY", stock=0)])
    result = optimizer.apply_one(gateway, runtime.ledger, task(visibility="READY_TO_SUPPLY"), valid_proposal())
    assert result["status"] == "completed"
    assert gateway.stock_requests == [{"offer_id": "sale-11", "product_id": 11, "stock": 10, "warehouse_id": 501}]


def test_archived_product_is_never_imported_or_restocked(optimizer, runtime) -> None:
    gateway = FakeGateway(products=[product("11", "sale-11", visibility="ARCHIVED", stock=0)])
    result = optimizer.apply_one(gateway, runtime.ledger, task(), valid_proposal())
    assert result["status"] == "skipped_archived"
    assert gateway.import_requests == []
    assert gateway.stock_requests == []


def test_unresolved_severe_risk_sets_stock_zero_without_archiving(optimizer, runtime) -> None:
    gateway = FakeGateway(products=[product("11", "sale-11", visibility="IN_SALE", stock=7)])
    result = optimizer.apply_one(gateway, runtime.ledger, task(), severe_proposal())
    assert result["status"] == "paused"
    assert gateway.stock_requests[0]["stock"] == 0
    assert gateway.archive_requests == []


def test_order_race_blocks_every_stock_mutation(optimizer, runtime) -> None:
    gateway = FakeGateway(products=[product("11", "sale-11", visibility="READY_TO_SUPPLY", stock=0)], active_orders={"sale-11"})
    result = optimizer.apply_one(gateway, runtime.ledger, task(), valid_proposal())
    assert result["status"] == "pending_risk"
    assert gateway.stock_requests == []


def test_failed_readback_rolls_back_original_payload(optimizer, runtime) -> None:
    gateway = FakeGateway(readback_is_bad=True)
    result = optimizer.apply_one(gateway, runtime.ledger, task(), valid_proposal())
    assert result["status"] == "rolled_back"
    assert gateway.import_requests[-1] == [task()["seller_api_item"]]
```

Add cases for import rejection, asynchronous timeout, rate-limit retry, rollback failure followed by stock 0, missing/multiple active RFBS warehouses, evidence-insufficient no-restock, duplicate action hash, and a new order appearing between import verification and stock mutation.

- [ ] **Step 2: Run tests and confirm missing action logic**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer.py -q
```

Expected: action-state tests fail because `apply_one` and gateway mutation methods do not exist.

- [ ] **Step 3: Add the minimal Seller gateway mutation methods**

Implement:

```python
def import_product(self, item: dict[str, Any]) -> int:
    return int(self.adapter.import_products([item])["task_id"])

def import_status(self, task_id: int) -> dict[str, Any]:
    return self.adapter.get_product_import_info(task_id)

def list_rfbs_warehouses(self) -> list[dict[str, Any]]:
    payload = self.adapter._post_json("/v2/warehouse/list", {})
    return [
        item for item in payload.get("warehouses", [])
        if item.get("status") == "created"
        and item.get("is_rfbs") is True
        and item.get("warehouse_type") == "rfbs"
    ]

def set_stock(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return self.adapter._post_json("/v2/products/stocks", {"stocks": rows})
```

Use `/v3/posting/fbs/list` and `/v2/posting/fbo/list` with a bounded recent date window to collect non-cancelled, non-delivered active offer IDs before every stock mutation. Never log posting payloads or customer data; retain only the boolean `active_order_present` and affected offer ID.

- [ ] **Step 4: Implement the import/verify/rollback state machine**

`apply_one()` must perform this order:

1. re-fetch the product and reject archived/stale source state;
2. validate the proposal and persist the original complete seller payload;
3. if unresolved severe risk exists, skip import and enter the guarded stock-zero path;
4. submit exactly one complete item through `/v3/product/import`;
5. poll `/v1/product/import/info` with bounded exponential delays `1, 2, 4, 8, 15, 15` seconds;
6. treat only a product-level successful terminal status as accepted;
7. re-fetch and validate every requested non-media field plus media equality;
8. compare post-write non-media score when the platform supplies the group data; a decrease is a rollback condition;
9. on failure, import the saved original payload and verify it;
10. if rollback fails, attempt guarded stock 0 and record `paused` or `failed`;
11. write one idempotent `action_log` row for each import, rollback, or stock payload hash.

HTTP 429 and transient network failures use `Retry-After` when available or bounded delays; validation errors and other 4xx responses do not retry.

- [ ] **Step 5: Add the inventory decision matrix**

After verified content write and a fresh product/order check:

| Current state | Evidence | Risk | Action |
|---|---|---|---|
| `IN_SALE` | any | safe | keep current stock unchanged |
| `READY_TO_SUPPLY` or another unarchived zero-stock state | sufficient | safe | set FBS stock to 10 |
| any unarchived state | any | unresolved severe, or high-risk repair failed | set FBS stock to 0 |
| any | insufficient | any | do not set stock 10 |
| `ARCHIVED` | any | any | no import, no stock action, no restore |
| any | any | active order or ambiguous warehouse | no stock mutation; persist pending reason |

Select the warehouse only when exactly one active RFBS warehouse is returned. Never hardcode a warehouse ID. Recheck active orders immediately before `set_stock`, even when an earlier preflight was clear.

- [ ] **Step 6: Run action tests**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer.py -q
```

Expected: every fake-API state-machine test passes and no test calls an archive or media endpoint.

- [ ] **Step 7: Commit the write/rollback slice**

```powershell
git add skills/ozon-store-content-risk-optimizer/scripts/store_content_optimizer.py tests/test_ozon_store_content_risk_optimizer.py
git commit -m "feat: apply and verify safe Ozon listing updates"
```

## Task 5: Finish the automatic Codex workflow and compact policy

**Files:**

- Modify: `skills/ozon-store-content-risk-optimizer/SKILL.md`
- Modify: `skills/ozon-store-content-risk-optimizer/agents/openai.yaml`
- Modify: `skills/ozon-store-content-risk-optimizer/references/optimization-policy.md`
- Modify: `tests/test_ozon_store_content_risk_optimizer_skill.py`

- [ ] **Step 1: Expand the failing skill contract test**

Add assertions that `SKILL.md` and the policy contain all required behavior:

```python
def test_skill_is_automatic_incremental_non_media_and_risk_first() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    policy = (SKILL_ROOT / "references" / "optimization-policy.md").read_text(encoding="utf-8")
    for phrase in (
        "scan", "next", "apply", "status", "automatic", "SQLite ledger",
        "all unarchived products", "unchanged completed products", "natural Russian",
        "semantic self-review", "evidence_insufficient", "FBS stock 10", "FBS stock 0",
        "never archive", "archived products", "active orders", "rollback",
    ):
        assert phrase.casefold() in text.casefold()
    for phrase in (
        "title", "description", "Rich Content", "attributes", "content score",
        "media score", "Chinese", "1688", "supplier", "quantity", "set composition",
        "dimensions", "weight", "capacity", "power", "voltage", "dictionary",
        "price", "compliance", "storefront divergence",
    ):
        assert phrase.casefold() in policy.casefold()
    assert "Do not generate, modify, upload, or delete images or video" in text
    assert "Do not ask the user to approve routine safe optimization" in text
    forbidden_marker = "TO" + "DO"
    assert forbidden_marker not in text
    assert forbidden_marker not in policy
```

- [ ] **Step 2: Run the skill contract and confirm it fails**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer_skill.py -q
```

Expected: the new policy/workflow assertions fail against the scaffold text.

- [ ] **Step 3: Write a concise `SKILL.md` automatic loop**

The final `SKILL.md` must have valid YAML frontmatter with only `name` and `description`, then instruct Codex to:

1. run `scan` once;
2. run `next` until it returns `{"status":"empty"}`;
3. open the task's exact public `product_url` when accessible and collect only visible high-impact facts; record the storefront as unavailable if access fails instead of guessing;
4. inspect only already-existing product images when provided, solely for identity/color/quantity/set consistency;
5. draft natural Russian title, description, Rich Content, and evidence-supported attributes;
6. perform a semantic self-review for meaning, fluency, factual consistency, repetition, and prohibited supplier language;
7. automatically rewrite its own failed fields without asking the user;
8. pipe the proposal into `apply` and continue regardless of safe per-product skip/pause;
9. stop only for missing credentials, an unavailable Seller API, or a systemic script/database failure;
10. report final counts from `status` without exposing credentials or customer/order data.

State these hard boundaries explicitly: no UI changes, no image/video generation or mutation, no media score, no archiving, no routine approval request, no guessing objective facts, no stock 10 with insufficient evidence, and no stock overwrite for already `IN_SALE` products.

- [ ] **Step 4: Write the compact optimization/risk policy**

Keep the policy under 180 lines. Use five sections only:

1. source precedence and `evidence_insufficient` behavior;
2. Russian title/description/Rich Content quality;
3. evidence-bound attribute and unit rules;
4. severe/high/medium/low risk matrix;
5. non-media content score and success definition.

The success definition is: the best evidence-supported non-media content, no deterministic or semantic risk failure, Seller API read-back confirmed, media untouched, and the correct inventory action completed or safely blocked. It is not “every field filled” and not “100 points at any cost.”

- [ ] **Step 5: Finalize `agents/openai.yaml`**

Use:

```yaml
interface:
  display_name: "Ozon Store Content Risk Optimizer"
  short_description: "Optimize listing content and stop unsafe offers"
  default_prompt: "Use $ozon-store-content-risk-optimizer to automatically audit and optimize eligible Ozon store products, preserve media, verify every write, and resume from its incremental ledger."
```

- [ ] **Step 6: Validate the skill package and run its tests**

Run:

```powershell
python "C:\Users\林伟华\.codex\skills\.system\skill-creator\scripts\quick_validate.py" skills/ozon-store-content-risk-optimizer
python -m pytest tests/test_ozon_store_content_risk_optimizer_skill.py tests/test_ozon_store_content_risk_optimizer.py -q
```

Expected: validator reports a valid skill and all optimizer tests pass.

- [ ] **Step 7: Commit the finished skill instructions**

```powershell
git add skills/ozon-store-content-risk-optimizer tests/test_ozon_store_content_risk_optimizer_skill.py
git commit -m "docs: define automatic Ozon optimization workflow"
```

## Task 6: Install, diagnose, and verify without touching the workbench UI

**Files:**

- Modify: `scripts/install_codex_skills.ps1`
- Modify: `scripts/verify_ozon_v2_install.ps1`
- Modify: `tests/test_installation_contract.py`

- [ ] **Step 1: Make installation tests expect three active skills**

Rename `test_codex_skill_installer_copies_the_two_active_skills_to_an_isolated_root` to `test_codex_skill_installer_copies_the_three_active_skills_to_an_isolated_root`. Add `ozon-store-content-risk-optimizer` to every active-skill tuple used by installer and doctor tests, and add:

```python
self.assertIn("ozon-store-content-risk-optimizer", text)
```

to the doctor contract test. Do not add a default prompt or plugin-manifest assertion for this skill because discovery already uses `.codex-plugin/plugin.json`'s `"skills": "./skills/"` directory entry.

- [ ] **Step 2: Run the installation tests and confirm they fail**

Run:

```powershell
python -m pytest tests/test_installation_contract.py -q
```

Expected: installer and doctor assertions fail because their `$skillNames` arrays still contain two skills.

- [ ] **Step 3: Add the skill to the installer and doctor arrays only**

Update both PowerShell arrays to:

```powershell
$skillNames = @(
    'ozon-product-media-generator',
    'ozon-intelligent-field-drafter',
    'ozon-store-content-risk-optimizer'
)
```

Do not change retired-skill cleanup, workbench startup, health checks, shortcuts, or the plugin manifest.

- [ ] **Step 4: Run the focused installation tests**

Run:

```powershell
python -m pytest tests/test_installation_contract.py -q
```

Expected: all installation contract tests pass.

- [ ] **Step 5: Run the complete bounded verification suite**

Run:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer.py tests/test_ozon_store_content_risk_optimizer_skill.py tests/test_installation_contract.py tests/test_ozon_field_drafter_skill.py -q
python "C:\Users\林伟华\.codex\skills\.system\skill-creator\scripts\quick_validate.py" skills/ozon-store-content-risk-optimizer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install_codex_skills.ps1 -DryRun
git diff --check
```

Expected: pytest passes, skill validation succeeds, dry run reports `COUNT=3`, and `git diff --check` prints nothing.

- [ ] **Step 6: Perform a no-write CLI smoke test against an isolated runtime**

Use a temporary fake gateway fixture through pytest; do not point the implementation at the live store yet. Verify:

```powershell
python -m pytest tests/test_ozon_store_content_risk_optimizer.py -q -k "scan or status or unchanged or archived"
```

Expected: selected tests pass; no Seller API write method is called.

- [ ] **Step 7: Review the exact diff boundary**

Run:

```powershell
git diff --name-only HEAD
```

Expected: only the nine files in this plan's Create/Modify lists are shown. If unrelated pre-existing files appear, leave them unstaged and do not alter them.

- [ ] **Step 8: Commit the installation integration**

```powershell
git add scripts/install_codex_skills.ps1 scripts/verify_ozon_v2_install.ps1 tests/test_installation_contract.py
git commit -m "chore: install Ozon content risk optimizer skill"
```

## Final acceptance gate before any live run

- [ ] All tests and `quick_validate.py` pass.
- [ ] The installed skill tree exactly matches the repository skill tree.
- [ ] No code path calls archive, picture import, video attachment, or media replacement.
- [ ] A repeated unchanged scan produces zero imports and zero stock writes.
- [ ] An archived product produces zero imports and zero stock writes.
- [ ] An `IN_SALE` product never has its stock overwritten after optimization.
- [ ] A safe, evidence-sufficient, unarchived zero-stock product receives stock 10 only after successful import and read-back.
- [ ] An unresolved severe-risk product receives guarded stock 0 and is not archived.
- [ ] Missing workbench/locked-SKU evidence leaves objective fields unchanged and blocks stock 10.
- [ ] Media absence neither deducts score nor blocks content success; existing media remains byte-for-byte unchanged.
- [ ] The ledger contains no API key, raw credentials, customer data, or posting payloads.
- [ ] The first live store execution begins with `scan` and a human-readable `status` snapshot, then uses the automatic loop; it does not add anything to the workbench interface.
