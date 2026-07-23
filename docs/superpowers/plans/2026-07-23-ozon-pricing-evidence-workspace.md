# Ozon Pricing Evidence Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the approved master-detail pricing workspace to the Ozon V2 upload page, calculate GUOO land-air freight and Ozon listing prices, persist each product independently, and expose confirmed package measurements as safe downstream attribute evidence.

**Architecture:** Keep pricing mathematics in a new pure domain module that composes the existing `cel_freight` rules. Persist pricing policy and per-batch evidence through `FileSystemRepository`, expose preview/confirm operations through `WorkbenchService`, and render the approved C layout in the existing local-server HTML. Server calculations are authoritative; UI calculations are display-only.

**Tech Stack:** Python 3, `Decimal`, existing `FileSystemRepository`, existing `WorkbenchService`, inline HTML/CSS/JavaScript, `pytest`.

---

## File structure

- Create `src/ozon_v2/domain/pricing.py`: validated inputs, fixed store policy, GUOO standard-channel convergence, cost and price rounding.
- Modify `src/ozon_v2/adapters/fs_repo.py`: load/save `pricing_settings.json` and batch `pricing_evidence.json` with existing atomic writes.
- Modify `src/ozon_v2/services/attribute_mapping_service.py`: consume only explicit package-dimension/weight evidence and never overwrite product dimensions.
- Modify `src/ozon_v2/services/workbench_service.py`: enrich upload items, preview and confirm one product, merge pricing gate per product.
- Modify `src/ozon_v2/workbench/local_server.py`: pricing endpoints and approved C-layout UI.
- Create `tests/test_pricing.py`: isolated pricing-domain tests.
- Modify `tests/test_workbench_local_server.py`: repository, service, endpoint and HTML contract tests.

### Task 1: Pricing domain calculator

**Files:**
- Create: `src/ozon_v2/domain/pricing.py`
- Create: `tests/test_pricing.py`

- [ ] **Step 1: Write failing first-product and validation tests**

```python
from decimal import Decimal

from ozon_v2.domain.pricing import PricingInput, PricingPolicy, calculate_listing_price


def test_first_product_uses_guoo_standard_and_rounds_up_to_dot_90():
    quote = calculate_listing_price(
        PricingInput.from_values(
            purchase_price_cny="9.9",
            domestic_shipping_cny="7",
            package_weight_g="380",
            package_length_cm="28",
            package_width_cm="11",
            package_height_cm="2.5",
            target_margin_rate="0.20",
        ),
        PricingPolicy.default(),
        initial_sale_rub="1194",
    )
    assert quote.freight_channel_code == "extra_small_standard"
    assert quote.cross_border_freight_cny == Decimal("16.952")
    assert quote.total_cost_cny == Decimal("35.852")
    assert quote.listing_price_cny == Decimal("55.90")
    assert quote.listing_price_rub == Decimal("671")


def test_commission_and_margin_must_leave_positive_denominator():
    inputs = PricingInput.from_values(
        purchase_price_cny=10,
        domestic_shipping_cny=0,
        package_weight_g=100,
        package_length_cm=10,
        package_width_cm=10,
        package_height_cm=10,
        target_margin_rate="0.85",
    )
    with pytest.raises(ValueError, match="commission"):
        calculate_listing_price(inputs, PricingPolicy.default(), initial_sale_rub=1000)
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run:

```powershell
python -m pytest tests/test_pricing.py -q
```

Expected: collection fails because `ozon_v2.domain.pricing` does not exist.

- [ ] **Step 3: Implement validated decimal inputs and policy**

```python
@dataclass(frozen=True)
class PricingPolicy:
    commission_rate: Decimal
    packaging_fee_cny: Decimal
    rub_per_cny: Decimal
    old_price_discount_rate: Decimal
    freight_rule_version: str

    @classmethod
    def default(cls) -> "PricingPolicy":
        return cls(
            commission_rate=Decimal("0.15"),
            packaging_fee_cny=Decimal("2.00"),
            rub_per_cny=Decimal("12"),
            old_price_discount_rate=Decimal("0.80"),
            freight_rule_version="2026-05-20",
        )


@dataclass(frozen=True)
class PricingInput:
    purchase_price_cny: Decimal
    domestic_shipping_cny: Decimal
    package_weight_g: Decimal
    package_length_cm: Decimal
    package_width_cm: Decimal
    package_height_cm: Decimal
    target_margin_rate: Decimal
```

Validate purchase price, dimensions and weight as positive; domestic shipping and margin as non-negative; and reject `commission_rate + target_margin_rate >= 1`.

- [ ] **Step 4: Implement standard-lane convergence and rounding**

```python
STANDARD_CODES = {
    "extra_small_standard",
    "budget_standard",
    "small_standard",
    "big_standard",
    "premium_small_standard",
    "premium_big_standard",
}


def round_up_to_dot_90(value: Decimal) -> Decimal:
    whole = value.to_integral_value(rounding=ROUND_FLOOR)
    candidate = whole + Decimal("0.90")
    if candidate < value:
        candidate += Decimal("1.00")
    return candidate.quantize(Decimal("0.00"))


def calculate_listing_price(
    inputs: PricingInput,
    policy: PricingPolicy,
    *,
    initial_sale_rub: Number,
) -> PricingQuote:
    sale_rub = _decimal(initial_sale_rub)
    seen_codes: list[str] = []
    for iteration in range(1, 6):
        freight_input = CelRfbsFreightInput.from_values(
            sale_rub=sale_rub,
            actual_weight_kg=inputs.package_weight_g / Decimal("1000"),
            length_cm=inputs.package_length_cm,
            width_cm=inputs.package_width_cm,
            height_cm=inputs.package_height_cm,
        )
        standard = next(
            (
                quote
                for quote in available_cel_rfbs_freight(freight_input)
                if quote.channel_code in STANDARD_CODES
            ),
            None,
        )
        if standard is None or standard.freight_rmb is None:
            raise ValueError("No GUOO land-air standard quote matches this product.")
        total_cost = (
            inputs.purchase_price_cny
            + inputs.domestic_shipping_cny
            + policy.packaging_fee_cny
            + standard.freight_rmb
        )
        raw_price = total_cost / (
            Decimal("1") - policy.commission_rate - inputs.target_margin_rate
        )
        listing_cny = round_up_to_dot_90(raw_price)
        next_sale_rub = (listing_cny * policy.rub_per_cny).to_integral_value(rounding=ROUND_CEILING)
        if seen_codes and seen_codes[-1] == standard.channel_code:
            old_price_rub = (
                next_sale_rub / policy.old_price_discount_rate
            ).to_integral_value(rounding=ROUND_CEILING)
            return PricingQuote(
                cross_border_freight_cny=standard.freight_rmb,
                total_cost_cny=total_cost,
                raw_listing_price_cny=raw_price,
                listing_price_cny=listing_cny,
                listing_price_rub=next_sale_rub,
                old_price_rub=old_price_rub,
                freight_channel_code=standard.channel_code,
                billing_weight_kg=standard.billing_weight_kg,
                iterations=iteration,
            )
        seen_codes.append(standard.channel_code)
        sale_rub = next_sale_rub
    raise ValueError("GUOO freight and listing price did not converge.")
```

Return all calculation components as `Decimal`, including the final RUB price, old RUB price and iteration count.

- [ ] **Step 5: Run pricing tests**

Run:

```powershell
python -m pytest tests/test_pricing.py tests/test_cel_freight.py -q
```

Expected: all pricing and existing freight tests pass.

- [ ] **Step 6: Commit the pricing domain**

```powershell
git add src/ozon_v2/domain/pricing.py tests/test_pricing.py
git commit -m "feat: add Ozon pricing calculator"
```

### Task 2: Pricing persistence and safe package evidence

**Files:**
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/attribute_mapping_service.py`
- Modify: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write failing persistence and mapping tests**

```python
def test_pricing_evidence_round_trips_without_touching_other_batch_files(self):
    payload = {
        "schema_version": 1,
        "run_id": "wb-test",
        "items": {"seed-1": {"status": "confirmed"}},
    }
    self.repo.save_pricing_evidence("wb-test", payload)
    assert self.repo.load_pricing_evidence("wb-test") == payload


def test_package_weight_maps_only_to_package_weight_field(self):
    result = map_template_attributes(
        [
            {"attribute_id": 1, "attribute_label": "Вес с упаковкой, г", "is_required": False},
            {"attribute_id": 2, "attribute_label": "Вес товара, г", "is_required": False},
        ],
        {},
        pricing_evidence={"package_weight_g": "380"},
    )
    assert result["fields"][0]["value"] == "380"
    assert result["fields"][0]["source"] == "user_confirmed_pricing_evidence"
    assert result["fields"][1]["status"] == "missing_fact"
```

- [ ] **Step 2: Run focused tests and verify missing methods/argument failures**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -k "pricing_evidence or package_weight" -q
```

Expected: failures for missing repository methods and unsupported `pricing_evidence` argument.

- [ ] **Step 3: Add repository methods**

```python
def save_pricing_settings(self, payload: dict[str, Any]) -> Path:
    path = self.config_dir / "pricing_settings.json"
    self._write_json(path, payload)
    return path

def load_pricing_settings(self) -> dict[str, Any]:
    if not (self.config_dir / "pricing_settings.json").exists():
        return PricingPolicy.default().to_dict()
    return self._read_json(self.config_dir / "pricing_settings.json")

def save_pricing_evidence(self, run_id: str, payload: dict[str, Any]) -> Path:
    path = self.run_dir(run_id) / "pricing_evidence.json"
    self._write_json(path, payload)
    return path

def load_pricing_evidence(self, run_id: str) -> dict[str, Any]:
    return self._read_json(self.run_dir(run_id) / "pricing_evidence.json")
```

- [ ] **Step 4: Add package-specific canonical labels**

Extend the existing function with the explicit keyword argument below and add evidence with priority `1` only for:

```python
def map_template_attributes(
    upload_schema: list[dict[str, Any]],
    ozon_candidate: dict[str, Any],
    *,
    supplier_product: dict[str, Any] | None = None,
    supplier_selection: dict[str, Any] | None = None,
    rewritten_content: dict[str, Any] | None = None,
    pricing_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

```python
PACKAGE_EVIDENCE_LABELS = {
    "package_weight_g": ("Вес с упаковкой, г", "package_weight"),
    "package_length_cm": ("Длина упаковки, см", "package_length"),
    "package_width_cm": ("Ширина упаковки, см", "package_width"),
    "package_height_cm": ("Высота упаковки, см", "package_height"),
}
```

Do not alias these canonical labels to generic `weight`, `length`, `width` or `height`.

- [ ] **Step 5: Run focused persistence and mapping tests**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -k "pricing_evidence or package_weight" -q
```

Expected: focused tests pass.

- [ ] **Step 6: Commit persistence and evidence mapping**

```powershell
git add src/ozon_v2/adapters/fs_repo.py src/ozon_v2/services/attribute_mapping_service.py tests/test_workbench_local_server.py
git commit -m "feat: persist Ozon pricing evidence"
```

### Task 3: Workbench service preview, confirmation and per-product gate

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write failing service tests**

```python
def test_pricing_preview_calculates_without_writing(self):
    result = self.service.preview_pricing_evidence(
        self.run_id,
        {
            "seed_id": "seed-1599",
            "purchase_price_cny": "9.9",
            "domestic_shipping_cny": "7",
            "package_weight_g": "380",
            "package_length_cm": "28",
            "package_width_cm": "11",
            "package_height_cm": "2.5",
            "target_margin_rate": "0.20",
        },
    )
    assert result.ok
    assert result.data["calculation"]["listing_price_cny"] == "55.90"
    assert not (self.repo.run_dir(self.run_id) / "pricing_evidence.json").exists()


def test_pricing_confirm_updates_only_current_product_and_gate(self):
    result = self.service.confirm_pricing_evidence(self.run_id, self.first_product_input)
    assert result.ok
    workspace = self.service.upload_workspace(self.run_id).data
    first = next(item for item in workspace["items"] if item["seed_id"] == "seed-1599")
    second = next(item for item in workspace["items"] if item["seed_id"] != "seed-1599")
    assert first["pricing_ready"] is True
    assert "pricing" not in first["blocking_gates"]
    assert second["pricing_ready"] is False
    assert "pricing" in second["blocking_gates"]
```

- [ ] **Step 2: Run focused tests and verify missing service method failures**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -k "pricing_preview or pricing_confirm" -q
```

Expected: failures for missing `preview_pricing_evidence` and `confirm_pricing_evidence`.

- [ ] **Step 3: Add shared pricing context lookup**

Create one private helper that:

- verifies `seed_id` belongs to the batch;
- loads current locked supplier SKU;
- resolves the supplier URL from collected final URL or `offer_id`;
- reads Ozon reference price as initial RUB value;
- reads the fixed `PricingPolicy`;
- parses the seven user input fields.

Return a clear `Result.failure` for missing SKU, invalid input or unavailable GUOO standard route.

- [ ] **Step 4: Implement preview and confirm**

```python
def preview_pricing_evidence(self, run_id: str, payload: dict[str, Any]) -> Result:
    context = self._pricing_context(run_id, payload)
    quote = calculate_listing_price(
        context.inputs,
        context.policy,
        initial_sale_rub=context.initial_sale_rub,
    )
    return Result.success(
        "pricing_evidence.previewed",
        "Pricing preview calculated.",
        self._pricing_response(context, quote),
    )


def confirm_pricing_evidence(self, run_id: str, payload: dict[str, Any]) -> Result:
    preview = self.preview_pricing_evidence(run_id, payload)
    if not preview.ok:
        return preview
    stored = self._load_or_initialize_pricing_evidence(run_id)
    stored["items"][payload["seed_id"]] = {
        "status": "confirmed",
        **preview.data,
        "confirmed_by": "user",
        "confirmed_at": utc_now_iso(),
    }
    self.repo.save_pricing_evidence(run_id, stored)
    return Result.success("pricing_evidence.confirmed", "Pricing evidence confirmed.", preview.data)
```

- [ ] **Step 5: Enrich upload workspace and gate**

For every item returned from `upload_workspace`:

- add `supplier_url`, selected SKU reference price and pricing evidence;
- pass `pricing_evidence=confirmed_inputs` into the existing `map_template_attributes` call;
- append `"pricing"` to `blocking_gates` unless current item is confirmed and current policy snapshot matches;
- add batch counts `pricing_confirmed_count` and `pricing_ready_count`;
- preserve independent `ready_to_build`.

- [ ] **Step 6: Run focused service tests**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -k "pricing" -q
```

Expected: pricing service tests pass.

- [ ] **Step 7: Commit service integration**

```powershell
git add src/ozon_v2/services/workbench_service.py tests/test_workbench_local_server.py
git commit -m "feat: integrate product pricing gates"
```

### Task 4: Local API routes

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write failing endpoint contract tests**

```python
def test_pricing_preview_and_confirm_routes_are_exposed(self):
    source = Path("src/ozon_v2/workbench/local_server.py").read_text(encoding="utf-8")
    assert 'parts[3:] == ["pricing-evidence", "preview"]' in source
    assert 'parts[3:] == ["pricing-evidence", "confirm"]' in source
```

- [ ] **Step 2: Run endpoint tests and verify failure**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -k "pricing_preview_and_confirm_routes" -q
```

Expected: route assertions fail.

- [ ] **Step 3: Add POST route dispatch**

Inside the existing POST handler:

```python
if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["pricing-evidence", "preview"]:
    self._send_result(service.preview_pricing_evidence(parts[2], payload), run_id=parts[2])
    return
if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["pricing-evidence", "confirm"]:
    self._send_result(service.confirm_pricing_evidence(parts[2], payload), run_id=parts[2])
    return
```

- [ ] **Step 4: Run endpoint tests**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -k "pricing_preview_and_confirm_routes" -q
```

Expected: endpoint contract tests pass.

- [ ] **Step 5: Commit routes**

```powershell
git add src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
git commit -m "feat: expose product pricing endpoints"
```

### Task 5: Approved C-layout UI

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Modify: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write failing HTML contract tests**

```python
def test_upload_page_contains_stable_master_detail_pricing_workspace(self):
    html = build_upload_workspace_html("wb-test")
    assert 'id="pricingWorkspace"' in html
    assert 'id="pricingProductList"' in html
    assert 'id="pricingEditor"' in html
    assert "价格与包装证据" in html
    assert "打开 1688 商品页" in html
    assert "采购价（人工确认）" in html
    assert "目标净利润率" in html
    assert "确认并写入本件价格与包装证据" in html
    assert "/pricing-evidence/preview" in html
    assert "/pricing-evidence/confirm" in html
```

- [ ] **Step 2: Run HTML test and verify failure**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -k "stable_master_detail_pricing_workspace" -q
```

Expected: required IDs and labels are absent.

- [ ] **Step 3: Add the full-width master-detail markup and styles**

Insert before `.upload-layout`:

```html
<section id="pricingWorkspace" class="panel pricing-workspace">
  <div class="panel-head">
    <div>
      <h3>价格与包装证据 (Pricing & Package Evidence)</h3>
      <p>每件商品独立保存；确认后的包装数据将进入字段证据。</p>
    </div>
    <span id="pricingProgress" class="pill">0 / 0 已确认</span>
  </div>
  <div class="pricing-master-detail">
    <nav id="pricingProductList" class="pricing-product-list"></nav>
    <div id="pricingEditor" class="pricing-editor"></div>
  </div>
</section>
```

Use the approved C visual hierarchy: persistent left product list, stable right editor, green evidence fields, amber fixed parameters and blue final-price card.

- [ ] **Step 4: Implement stable client state and server preview**

```javascript
const pricingState = {
  selectedSeedId: null,
  drafts: new Map(),
  previewTimer: null,
};

function selectPricingProduct(seedId) {
  capturePricingDraft();
  pricingState.selectedSeedId = seedId;
  renderPricingWorkspace();
}

async function previewPricing(seedId) {
  const payload = pricingPayload(seedId);
  const result = await api(
    `/api/batches/${encodeURIComponent(runId)}/pricing-evidence/preview`,
    {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}
  );
  renderPricingCalculation(result.data);
}
```

Never reset `selectedSeedId` during upload workspace refresh. Keep unsaved values in `pricingState.drafts`.

- [ ] **Step 5: Implement per-product confirmation**

```javascript
async function confirmPricing(seedId) {
  const result = await api(
    `/api/batches/${encodeURIComponent(runId)}/pricing-evidence/confirm`,
    {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(pricingPayload(seedId))}
  );
  updatePricingItem(seedId, result.data);
  renderPricingWorkspace();
  renderCurrentUploadGates();
}
```

Do not navigate, collapse the editor, trigger upload or auto-select another product after confirmation.

- [ ] **Step 6: Run page tests**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py -k "pricing or upload_page" -q
```

Expected: page and service pricing tests pass.

- [ ] **Step 7: Commit the UI**

```powershell
git add src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
git commit -m "feat: add pricing evidence workspace"
```

### Task 6: Regression verification

**Files:**
- Modify only if a regression is found in files already listed above.

- [ ] **Step 1: Run focused pricing and freight suites**

Run:

```powershell
python -m pytest tests/test_pricing.py tests/test_cel_freight.py tests/test_workbench_local_server.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run the full suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass with no real collection, image generation, upload, publish or approval action.

- [ ] **Step 3: Inspect the local page**

Open the local upload page for a fixture or existing batch and verify:

- the pricing workspace appears before field mapping;
- five products remain visible in the left list;
- switching products never auto-collapses the right editor;
- the 1688 URL opens in a new tab;
- the first-product sample returns `¥55.90` and `671 ₽` with the store ratio `1 CNY = 12 RUB`;
- confirmation changes only the current product pricing state;
- upload remains publish-locked.

- [ ] **Step 4: Final repository check**

Run:

```powershell
git status --short
git log -6 --oneline
```

Expected: no unintended files, spreadsheet-analysis scratch data or runtime evidence changes are staged.
