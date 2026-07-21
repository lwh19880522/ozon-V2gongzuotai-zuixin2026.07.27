# Ozon Supplier SKU Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让无规格 1688 商品可以确认唯一 SKU，并把多 SKU 原始矩阵改成以 Ozon 目标为参照、能够理解且不会胡乱默认选择的决策界面。

**Architecture:** 内容脚本修正唯一 SKU 识别；`WorkbenchService` 用一个共享归一化函数兼容新旧采集结果，并让审核、确认和收据校验引用相同候选集合。工作台页面在浏览器端只负责结构化展示、确定性字段比较和用户交互，不写入推测数据。

**Tech Stack:** Python 3、pytest、原生 JavaScript、Node `vm`、Ozon V2 本地工作台。

---

## 文件结构

- `browser_extension/ozon_v2_bridge/supplier_content.js`：识别无真实规格组的页面并生成唯一 SKU 证据。
- `src/ozon_v2/services/workbench_service.py`：归一化唯一 SKU 候选，并统一审核、确认和收据校验的数据源。
- `src/ozon_v2/workbench/local_server.py`：Ozon 目标摘要、结构化 SKU 卡片、确定性推荐和上下文确认按钮。
- `tests/test_supplier_selection_without_sku_matrix.js`：无规格页的浏览器采集合约。
- `tests/test_supplier_sku_selection_service.py`：旧采集结果唯一 SKU 恢复、确认和歧义保护。
- `tests/test_workbench_local_server.py`：审核 API 与工作台页面决策文案合约。

### Task 1: 锁定唯一 SKU 采集与旧数据恢复

**Files:**
- Modify: `tests/test_supplier_selection_without_sku_matrix.js`
- Modify: `tests/test_supplier_sku_selection_service.py`
- Test: `tests/test_supplier_selection_without_sku_matrix.js`
- Test: `tests/test_supplier_sku_selection_service.py`

- [ ] **Step 1: 将无规格页测试改成要求完整唯一候选**

```javascript
assert.equal(capturedPayload.supplier_product.sku_options.length, 1);
const option = capturedPayload.supplier_product.sku_options[0];
assert.equal(option.supplier_sku_id, "123456789012");
assert.equal(option.evidence_source, "single_sku_detail_page");
assert.equal(option.evidence.no_visible_variant_selector, true);
assert.equal(option.complete, true);
```

- [ ] **Step 2: 增加旧空矩阵服务测试**

构造 `sku.evidence=no_visible_variant_selector`、空 `sku_groups`、空 `sku_options`、真实价格和图片，断言 `_collection_review_items` 暴露一个候选，随后 `confirm_supplier_sku` 成功且 `_supplier_sku_selection_status().complete` 为真。

- [ ] **Step 3: 增加歧义保护测试**

构造非空 `sku_groups` 和空 `sku_options`，断言审核候选仍为空，证明系统不会把解析失败的多规格商品伪装成唯一 SKU。

- [ ] **Step 4: 运行测试确认失败**

Run: `node tests/test_supplier_selection_without_sku_matrix.js`

Expected: FAIL，当前内容脚本因宽泛选择器返回空矩阵。

Run: `python -m pytest tests/test_supplier_sku_selection_service.py -q`

Expected: FAIL，当前服务只读取原始 `sku_options`。

### Task 2: 实现共享唯一 SKU 归一化

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/supplier_content.js`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Test: `tests/test_supplier_selection_without_sku_matrix.js`
- Test: `tests/test_supplier_sku_selection_service.py`

- [ ] **Step 1: 让内容脚本只按真实规格组阻止唯一 SKU**

```javascript
function collectSingleSkuOption(groups) {
  if (Array.isArray(groups) && groups.length) return [];
  // 保留 offer、价格、图片等完整证据门禁。
}
```

- [ ] **Step 2: 增加服务端共享归一化函数**

函数返回原始有效选项；只有空矩阵、空规格组、明确无选择器证据和完整页面字段同时成立时，才返回一个 `SupplierSkuOption.to_dict()`。候选必须包含：

```python
{
    "supplier_sku_id": offer_id,
    "combination_key": "页面唯一 SKU",
    "raw_label": "页面唯一 SKU（无需选择规格）",
    "selected_options": {"规格": "页面唯一 SKU"},
    "stock": {"status": "unknown", "quantity": None},
    "evidence_source": "single_sku_detail_page",
    "complete": True,
}
```

- [ ] **Step 3: 统一三个调用点**

`_collection_review_items`、`confirm_supplier_sku` 和 `_supplier_sku_selection_status` 都调用该函数，不再直接遍历 `product.get("sku_options", [])`。

- [ ] **Step 4: 运行唯一 SKU 聚焦测试**

Run: `node tests/test_supplier_selection_without_sku_matrix.js`

Run: `python -m pytest tests/test_supplier_sku_selection_service.py -q`

Expected: 全部 PASS。

### Task 3: 锁定可理解的多 SKU 决策界面

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: 添加页面合约断言**

```python
self.assertIn("Ozon 原商品目标", page)
self.assertIn("1688 可采购规格", page)
self.assertIn("页面只有一个真实 SKU，无需选择规格", page)
self.assertIn("确认页面唯一 SKU", page)
self.assertIn("确认所选 SKU", page)
self.assertIn("系统没有找到可证明的唯一对应项", page)
self.assertIn("function analyzeSupplierSkuOptions", page)
self.assertIn('radio.addEventListener("change", updateSkuLockButton)', page)
```

- [ ] **Step 2: 运行页面测试确认失败**

Run: `python -m pytest tests/test_workbench_local_server.py::WorkbenchLocalServerTests::test_supplier_review_exposes_real_sku_lock_and_subject_master_routes -q`

Expected: FAIL，当前页面只输出原始 SKU 卡片。

### Task 4: 实现目标驱动的 SKU 决策视图

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: 增加决策区样式和两层容器**

在 `skuOptions` 内按顺序渲染 Ozon 目标摘要、模式提示和 1688 候选网格；原始证据详情继续由现有 Evidence 面板承载。

- [ ] **Step 2: 增加字段归类和确定性比较函数**

`skuDecisionFacts()` 将常见俄文、英文和中文键归到“包装数量、尺寸、颜色、套装/型号”；`analyzeSupplierSkuOptions()` 只比较规范化完全相同值和明确数字 token。只有唯一最高分达到门槛时返回 `recommendedSkuId`，否则返回空字符串。

- [ ] **Step 3: 改造候选卡和按钮状态**

唯一 SKU 自动选中并显示“确认页面唯一 SKU”；多 SKU 只在高置信唯一推荐时预选，否则必须由用户点击卡片。每个 radio 的 `change` 事件调用 `updateSkuLockButton()`，未选择时按钮保持禁用。

- [ ] **Step 4: 运行页面测试**

Run: `python -m pytest tests/test_workbench_local_server.py -q`

Expected: 全部 PASS。

### Task 5: 安全回归、提交与交付

**Files:**
- No additional production files.

- [ ] **Step 1: 运行浏览器 Node 合约**

Run: `python -m pytest tests/test_browser_extension_node_contracts.py -q`

Expected: 全部 PASS。

- [ ] **Step 2: 运行完整业务测试**

Run: `python -m pytest -q`

Expected: 全部 PASS，不触发真实业务操作。

- [ ] **Step 3: 检查差异并提交**

Run: `git diff --check`

Expected: 无输出。

```powershell
git add docs/superpowers/specs/2026-07-21-ozon-supplier-sku-decision-design.md docs/superpowers/plans/2026-07-21-ozon-supplier-sku-decision.md browser_extension/ozon_v2_bridge/supplier_content.js src/ozon_v2/services/workbench_service.py src/ozon_v2/workbench/local_server.py tests/test_supplier_selection_without_sku_matrix.js tests/test_supplier_sku_selection_service.py tests/test_workbench_local_server.py
git commit -m "fix: make supplier SKU decisions understandable"
```

- [ ] **Step 4: 合并和推送**

把 `codex/ozon-browser-task-recovery-ui` 快进合并到本地 `main`，在合并后的 `main` 再跑完整测试；随后用目标仓库专用 SSH 路径推送功能分支并确认 PR #3 仍为 OPEN。不得推送远端 `main`。
