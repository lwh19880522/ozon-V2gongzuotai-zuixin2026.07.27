# Ozon Managed Supplier Lane Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复单个受管 1688 标签关闭后五个通道全部失去覆盖采集卡的问题，并锁定明确重启只补缺失通道及供应商字段完整回传。

**Architecture:** 在扩展后台把“单标签关闭”和“整个受管窗口关闭”分成两个状态转换。新增稳定身份对齐逻辑，在新 dispatch 中复用仍存活的通道标签、仅为缺失通道建页；内容脚本和服务端保持现有提交协议，用回归断言锁定属性与 SKU 字段在工作台成功回执前后的完整性。

**Tech Stack:** Chrome/Edge Manifest V3、JavaScript、Node `vm` 合约测试、Python 3、pytest、Ozon V2 本地工作台服务。

---

## 文件结构

- `browser_extension/ozon_v2_bridge/background.js`：受管通道绑定、关闭事件、窗口复用和缺失通道补页。
- `browser_extension/ozon_v2_bridge/manifest.json`：扩展行为变更后的版本号。
- `tests/test_browser_extension_managed_supplier_round.js`：五通道关闭、子页接管和明确重启的浏览器后台合约。
- `tests/test_supplier_selection_content_script.js`：覆盖采集卡和提交字段合约。
- `tests/test_workbench_local_server.py`：工作台保存完整供应商属性/SKU 数据并返回持久化成功的合约。

### Task 1: 锁定单通道关闭不会暂停整批

**Files:**
- Modify: `tests/test_browser_extension_managed_supplier_round.js`
- Test: `tests/test_browser_extension_managed_supplier_round.js`

- [ ] **Step 1: 写入失败测试**

在五通道创建完成后关闭第三个通道，并断言只清空该通道：

```javascript
const closedChannel = entry.channels[2];
const healthyBefore = entry.channels
  .filter((channel) => channel !== closedChannel)
  .map((channel) => channel.tabId);
tabs.delete(closedChannel.tabId);
const stoppedBefore = postedPaths.filter((value) => value.includes("/runner/stop")).length;
await context.handleTaskTabRemoved(closedChannel.tabId);
assert.equal(closedChannel.tabId, null);
assert.deepEqual(
  entry.channels.filter((channel) => channel !== closedChannel).map((channel) => channel.tabId),
  healthyBefore,
);
assert.equal(entry.launchState, "incomplete");
assert.equal(postedPaths.filter((value) => value.includes("/runner/stop")).length, stoppedBefore);
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node tests/test_browser_extension_managed_supplier_round.js`

Expected: FAIL，现有实现会把五个 `tabId` 全部设为 `null`，并发送 `/runner/stop`。

- [ ] **Step 3: 提交测试检查点**

```powershell
git add tests/test_browser_extension_managed_supplier_round.js
git commit -m "test: lock isolated managed supplier lane closure"
```

### Task 2: 实现单通道隔离关闭

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/background.js:145-191`
- Test: `tests/test_browser_extension_managed_supplier_round.js`

- [ ] **Step 1: 在通用整批停止逻辑前处理受管通道**

在 `handleTaskTabRemoved` 中找到被关闭通道；受管任务只更新这个通道并提前继续：

```javascript
const closedChannel = Array.isArray(entry.channels)
  ? entry.channels.find((channel) => channel.tabId === tabId)
  : null;
if (closedChannel) {
  closedChannel.tabId = null;
  closedChannel.closedAt = closedAt;
  if (!SUPPLIER_TERMINAL_STATES.has(closedChannel.state)) closedChannel.state = "waiting_user";
  entry.launchState = "incomplete";
  entry.lastUpdatedAt = closedAt;
  openedTasks[key] = entry;
  await saveOpenedTasks(openedTasks);
  continue;
}
```

不得修改 `handleManagedSupplierWindowRemoved` 的整窗暂停语义。

- [ ] **Step 2: 运行聚焦测试确认通过**

Run: `node tests/test_browser_extension_managed_supplier_round.js`

Expected: PASS，并保留现有子标签接管、整窗关闭和全通道终态断言。

- [ ] **Step 3: 提交实现**

```powershell
git add browser_extension/ozon_v2_bridge/background.js tests/test_browser_extension_managed_supplier_round.js
git commit -m "fix: isolate managed supplier lane closures"
```

### Task 3: 锁定明确重启只补缺失通道

**Files:**
- Modify: `tests/test_browser_extension_managed_supplier_round.js`
- Test: `tests/test_browser_extension_managed_supplier_round.js`

- [ ] **Step 1: 写入失败测试**

在 Task 1 的单通道关闭后，先用同 token 调用 `performOpenTask`，断言不创建标签；再用新 token 调用，断言只创建一个标签并保留其他稳定绑定：

```javascript
const tabCountAfterClose = tabs.size;
await context.performOpenTask(task, "managed_round_poll", { allowCreate: true });
assert.equal(tabs.size, tabCountAfterClose, "same dispatch must not reopen a user-closed lane");
const restarted = supplierTask(5, "managed-token-restart");
await context.performOpenTask(restarted, "managed_round_restart", { allowCreate: true });
assert.equal(tabs.size, tabCountAfterClose + 1);
assert.deepEqual(
  entry.channels.filter((channel) => channel.channel_index !== 2).map((channel) => channel.tabId),
  healthyBefore,
);
assert.equal(updatedTabs.length, 0, "healthy lanes must not be navigated during restart");
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node tests/test_browser_extension_managed_supplier_round.js`

Expected: FAIL，现有实现按窗口标签顺序重映射，且不会只创建缺失页。

### Task 4: 用稳定身份对齐并补开缺失通道

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/background.js:416-445`
- Modify: `browser_extension/ozon_v2_bridge/background.js:486-528`
- Test: `tests/test_browser_extension_managed_supplier_round.js`

- [ ] **Step 1: 增加稳定身份和存活标签映射**

```javascript
function sameSupplierChannel(channel, item, fallbackIndex) {
  return channel
    && channel.channel_index === (Number.isInteger(item.channel_index) ? item.channel_index : fallbackIndex)
    && String(channel.seed_id || "") === String(item.seed_id || "")
    && String(channel.ozon_product_id || "") === String(item.ozon_product_id || "");
}

function liveTabForPreviousChannel(previous, tabsById) {
  return previous && Number.isInteger(previous.tabId) ? tabsById.get(previous.tabId) || null : null;
}
```

- [ ] **Step 2: 改造现有窗口分支**

为每个 task item 找稳定身份相同的旧通道并复用其存活标签。同 dispatch 如果存在缺失通道，只保存 `incomplete` 并返回等待；新 dispatch 则在 `previous.windowId` 内为每个缺失通道执行一次：

```javascript
const tab = await chrome.tabs.create({
  windowId: previous.windowId,
  url: "https://www.1688.com/",
  active: false,
});
```

随后用按 item 对齐的 tabs 构造 `managedSupplierChannels`。不得调用 `chrome.tabs.update` 导航健康标签，也不得用 `existingWindowTabs[index]` 猜通道。

- [ ] **Step 3: 运行后台合约测试**

Run: `node tests/test_browser_extension_managed_supplier_round.js`

Expected: PASS；同 token 无新标签，新 token 恰好一个新标签，其他绑定不变。

- [ ] **Step 4: 提交实现**

```powershell
git add browser_extension/ozon_v2_bridge/background.js tests/test_browser_extension_managed_supplier_round.js
git commit -m "fix: restore only missing supplier lanes"
```

### Task 5: 锁定覆盖采集字段和持久化回执

**Files:**
- Modify: `tests/test_supplier_selection_content_script.js`
- Modify: `tests/test_workbench_local_server.py:1182-1203`
- Test: `tests/test_supplier_selection_content_script.js`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: 扩展内容脚本字段断言**

在点击覆盖按钮后断言 `supplier_product` 同时包含：

```javascript
assert.ok(capturedPayload.supplier_product.seller);
assert.ok(capturedPayload.supplier_product.images.length);
assert.ok(capturedPayload.supplier_product.price);
assert.ok(capturedPayload.supplier_product.domestic_shipping_evidence);
assert.equal(typeof capturedPayload.supplier_product.attributes, "object");
assert.ok(Array.isArray(capturedPayload.supplier_product.sku_groups));
assert.ok(Array.isArray(capturedPayload.supplier_product.sku_options));
```

并保留“点击前不得提交”“成功回执后才发送 `collected`”断言。

- [ ] **Step 2: 扩展服务端原样保存断言**

在现有成功捕获测试中给商品加入确定的 `attributes`、`sku_groups` 和 `sku_options`，然后从 `supplier_collection_result.json` 读取保存对象：

```python
stored = self.repo.load_supplier_collection_result(run_id)["supplier_products"][0]
self.assertEqual(supplier_product["attributes"], stored["attributes"])
self.assertEqual(supplier_product["sku_groups"], stored["sku_groups"])
self.assertEqual(supplier_product["sku_options"], stored["sku_options"])
self.assertTrue(result["data"]["accepted"])
self.assertEqual("collected", result["data"]["lane_terminal"])
```

- [ ] **Step 3: 运行字段合约测试**

Run: `node tests/test_supplier_selection_content_script.js`

Run: `python -m pytest tests/test_workbench_local_server.py -q`

Expected: PASS；字段无丢失且终态只在持久化成功回执后产生。

- [ ] **Step 4: 提交字段回归测试**

```powershell
git add tests/test_supplier_selection_content_script.js tests/test_workbench_local_server.py
git commit -m "test: preserve managed supplier capture fields"
```

### Task 6: 版本升级与完整验证

**Files:**
- Modify: `browser_extension/ozon_v2_bridge/manifest.json`
- Test: `tests/test_browser_extension_node_contracts.py`

- [ ] **Step 1: 将扩展版本从 0.1.53 升到 0.1.54**

```json
"version": "0.1.54"
```

- [ ] **Step 2: 运行浏览器扩展合约**

Run: `python -m pytest tests/test_browser_extension_node_contracts.py -q`

Expected: 全部 PASS。

- [ ] **Step 3: 运行完整测试与静态检查**

Run: `python -m pytest -q`

Expected: 全部 PASS。

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 4: 提交版本升级**

```powershell
git add browser_extension/ozon_v2_bridge/manifest.json
git commit -m "chore: bump browser bridge to 0.1.54"
```

### Task 7: 推送、更新 PR、合并本地 main 并重启工具台

**Files:**
- No file changes.

- [ ] **Step 1: 推送当前功能分支并确认 PR 仍为 OPEN**

使用目标仓库专用 SSH key 推送 `codex/ozon-browser-task-recovery-ui`，再用 `gh pr view 3` 核对 base/head/state。

- [ ] **Step 2: 快进本地 main**

在工作树干净且全部测试通过后切到 `main`，执行：

```powershell
git merge --ff-only codex/ozon-browser-task-recovery-ui
```

- [ ] **Step 3: 在合并后的 main 再跑完整测试**

Run: `python -m pytest -q`

Expected: 与功能分支相同，全部 PASS。

- [ ] **Step 4: 重启本地工作台并做只读健康检查**

停止当前 Ozon V2 本地服务进程，使用仓库既有启动入口重新启动，验证 `http://127.0.0.1:8765/` 可访问且服务端期望扩展版本为 `0.1.54`。不得触发真实业务任务。

最终交接说明必须包含：单通道隔离关闭、明确重启补缺失通道、字段持久化测试、分支/PR/main 状态、扩展重新加载要求，以及“未触发真实采集、生图、上传、发布或最终审批”。
