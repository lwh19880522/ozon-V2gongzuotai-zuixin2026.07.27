# Ozon 浏览器采集人工恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有自动流程的同时，为 Ozon 原商品及页面原始属性采集、1688 受管采集增加明确的人工恢复入口，并且只续跑尚未完成的商品或通道。

**Architecture:** 后端只暴露一个由批次状态推导任务类型的受限重派发入口；Ozon 每件成功候选原子写入批次级 JSON 检查点，活动任务把已验证候选交给扩展重建游标；1688 继续复用现有逐件草稿和本地游标。新派发令牌沿用扩展既有的“旧令牌关页保持停止、新令牌仅重新打开一次”机制。

**Tech Stack:** Python 3.11、标准库 `ThreadingHTTPServer`、文件系统 JSON 仓库、Manifest V3 JavaScript、Node.js `assert` 合同测试、pytest。

---

## 范围边界

本计划只实施 `docs/superpowers/specs/2026-07-20-ozon-browser-task-recovery-design.md`。

“字段回传”只指 `ozon_collection` 候选中的 Ozon 原商品页面属性（`attributes` 与 `content_score_evidence.attribute_table`）。本计划不修改 Ozon Seller API 类目属性模板、AI 字段优化、Yandex 字段控制器、上传字段写回、图片生成或发布审批。

所有测试只使用本地假数据；不得打开真实 Ozon/1688 业务页，不得触发真实采集、生图、上传、发布或最终审批。

## 文件清单

- Modify: `src/ozon_v2/adapters/fs_repo.py` — 增加 `ozon_collection_draft.json` 的原子保存和读取方法。
- Modify: `src/ozon_v2/services/workbench_service.py` — 校验/合并 Ozon 单件检查点，推导并记录允许恢复的浏览器任务。
- Modify: `src/ozon_v2/workbench/local_server.py` — 增加两个 POST 路由、活动任务恢复载荷、中文按钮和中文事件展示。
- Modify: `browser_extension/ozon_v2_bridge/content.js` — 从服务端检查点重建 Ozon 游标，每件成功后先持久化再推进。
- Modify: `browser_extension/ozon_v2_bridge/manifest.json` — 扩展行为改变后从 `0.1.52` 升到 `0.1.53`。
- Modify: `tests/test_workbench_local_server.py` — 覆盖检查点、恢复 API、活动任务和两个页面入口。
- Modify: `tests/test_browser_collection_progress.js` — 覆盖 Ozon 服务端检查点恢复、原始属性保留和检查点失败不推进。
- Modify: `tests/test_browser_extension_background.js` — 锁定旧令牌不重开、新令牌只重开一次的回归行为。
- Reference: `docs/superpowers/specs/2026-07-20-ozon-browser-task-recovery-design.md` — 已由用户确认的范围和验收标准。

### Task 1: 建立 Ozon 单件采集检查点

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [ ] **Step 1: 写检查点 API 的失败测试**

在 `tests/test_workbench_local_server.py` 增加以下夹具，创建带俄文查询的冻结 seed、保存 Ozon 合同，并把批次固定在 `ozon_collecting`：

```python
def prepare_ozon_collecting_run(self, count: int = 2) -> tuple[str, list[SeedProduct]]:
    seeds = [
        SeedProduct(
            seed_id=f"seed-progress-{index}",
            title_or_keyword=f"test product {index}",
            product_clue=f"test product {index}",
            ozon_query_terms_ru=[f"тестовый товар {index}"],
            query_generation_status="generated",
        )
        for index in range(1, count + 1)
    ]
    run = self.repo.create_workbench_batch_record(target_count=count)
    run_id = run["run_id"]
    self.repo.save_sampled_seeds(run_id, seeds)
    contract = CollectionContractService(self.repo).build_ozon_collection_contract(run_id)
    self.assertTrue(contract.ok)
    self.repo.save_ozon_collection_contract(run_id, contract.data)
    run["status"] = WorkbenchState.OZON_COLLECTING.value
    run["ozon_collection_contract_ready"] = True
    run["ozon_collection_contract_path"] = str(self.repo.run_dir(run_id) / "ozon_collection_contract.json")
    self.repo.save_run(run)
    return run_id, seeds
```

继续复用现有 `ozon_candidate_payload()` 生成完整候选，仅为第二件修改 `seed_id`、`ozon_product_id`、URL、SKU 和图片 URL。

增加以下测试；先提交第二件再提交第一件，证明客户端顺序不能改变合同顺序，并确认原商品属性字段完整保留：

```python
def test_ozon_collection_progress_persists_candidates_in_contract_order(self) -> None:
    run_id, seeds = self.prepare_ozon_collecting_run(count=2)
    second = self.ozon_candidate_payload(seeds[1])
    second.update({"ozon_product_id": "ozon-2", "ozon_url": "https://www.ozon.ru/product/test-2/"})
    second["target_sku"]["sku_id"] = "ozon-sku-2"
    second["attributes"] = {"Материал": "сталь", "Цвет": "черный"}
    second["content_score_evidence"]["attribute_table"] = dict(second["attributes"])
    first = self.ozon_candidate_payload(seeds[0])

    for candidate in (second, first, first):
        result = self.post_json(
            f"/api/batches/{run_id}/ozon-collection-progress",
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "test_browser_checkpoint",
                "ozon_candidate": candidate,
            },
        )
        self.assertTrue(result["ok"])

    draft = self.repo.load_ozon_collection_draft(run_id)
    self.assertEqual([seed.seed_id for seed in seeds], [item["seed_id"] for item in draft["ozon_candidates"]])
    self.assertEqual(2, len(draft["ozon_candidates"]))
    self.assertEqual(second["attributes"], draft["ozon_candidates"][1]["attributes"])
    self.assertEqual(
        second["content_score_evidence"]["attribute_table"],
        draft["ozon_candidates"][1]["content_score_evidence"]["attribute_table"],
    )
```

再增加三个独立测试：

- `test_ozon_collection_progress_rejects_run_seed_and_candidate_conflicts`：依次提交不一致 `run_id`、合同外 seed、同一 seed 的不同候选、不同 seed 的重复商品 ID；逐次断言 `ok is False`、结果码分别匹配下列清单，最后断言草稿仍只含第一个合法候选。
- `test_ozon_collection_progress_rejects_invalid_public_attributes`：把候选 `attributes` 和 `content_score_evidence.attribute_table` 置空，断言返回 `ozon_collection.progress_invalid`，并断言 `ozon_collection_draft.json` 不存在。
- `test_ozon_collection_progress_reports_corrupt_existing_draft`：先用 `Path.write_text("{not-json", encoding="utf-8")` 写入损坏草稿，记录原始字节；POST 后断言返回 `ozon_collection.checkpoint_invalid`，并断言文件字节未改变。

这些测试使用准确结果码：

```text
ozon_collection.progress_run_mismatch
ozon_collection.progress_seed_not_expected
ozon_collection.progress_conflict
ozon_collection.progress_duplicate_product
ozon_collection.progress_invalid
ozon_collection.checkpoint_invalid
```

- [ ] **Step 2: 运行检查点测试并确认 RED**

```powershell
python -m pytest tests/test_workbench_local_server.py -k "ozon_collection_progress" -q
```

Expected: FAIL；POST 路由尚不存在，且 `FsRepo` 没有 `save_ozon_collection_draft` / `load_ozon_collection_draft`。

- [ ] **Step 3: 增加原子文件仓库方法**

在 `src/ozon_v2/adapters/fs_repo.py` 的 Ozon 合同/结果方法旁加入：

```python
def save_ozon_collection_draft(self, run_id: str, payload: dict[str, Any]) -> Path:
    path = self.run_dir(run_id) / "ozon_collection_draft.json"
    self._write_json(path, payload)
    return path

def load_ozon_collection_draft(self, run_id: str) -> dict[str, Any]:
    return self._read_json(self.run_dir(run_id) / "ozon_collection_draft.json")
```

必须复用现有 `_write_json()` 的临时文件替换，不另写非原子 I/O。

- [ ] **Step 4: 实现检查点读取校验和单件合并**

在 `src/ozon_v2/services/workbench_service.py` 增加 `ozon_collection_checkpoint()` 与 `save_ozon_collection_progress()`。单件候选仍使用现有终态校验器，不创建第二套模型：

```python
single_payload = {
    "worker": payload.get("worker"),
    "source": payload.get("source"),
    "ozon_candidates": [candidate],
}
errors = validate_ozon_collection_result(single_payload, [seed_id])
```

`ozon_collection_checkpoint(run_id)` 必须执行并返回结构化 `Result`：

1. 读取冻结的 `ozon_collection_contract.json`，从 `contract["payload"]["seeds"]` 得到唯一合同顺序。
2. 草稿不存在时返回空 `ozon_candidates`，不是错误。
3. 捕获 `json.JSONDecodeError`、`TypeError`、`ValueError`，返回 `ozon_collection.checkpoint_invalid`，不得覆盖源文件。
4. 校验草稿 `run_id`、每个候选的合同 seed、现有候选基础字段、商品 ID 唯一性、合同排除 ID 和当前黑名单。
5. 校验草稿中的 seed 顺序严格等于合同顺序中过滤出的已捕获 seed；不接受客户端自定义顺序。

`save_ozon_collection_progress()` 的核心合并逻辑如下：

```python
candidate_by_seed = {
    str(item.get("seed_id") or ""): item
    for item in checkpoint.data["ozon_candidates"]
}
existing = candidate_by_seed.get(seed_id)
if existing is not None and existing != candidate:
    return Result.failure(
        "ozon_collection.progress_conflict",
        "This Ozon seed already has a different verified checkpoint candidate.",
        data={"run_id": run_id, "seed_id": seed_id},
    )

candidate_product_id = str(candidate.get("ozon_product_id") or "")
for other_seed_id, other in candidate_by_seed.items():
    if other_seed_id != seed_id and str(other.get("ozon_product_id") or "") == candidate_product_id:
        return Result.failure(
            "ozon_collection.progress_duplicate_product",
            "The Ozon product is already checkpointed for another seed.",
            data={"run_id": run_id, "seed_id": seed_id, "ozon_product_id": candidate_product_id},
        )

candidate_by_seed[seed_id] = candidate
ordered_candidates = [candidate_by_seed[item_id] for item_id in contract_seed_ids if item_id in candidate_by_seed]
draft = {
    "schema_version": 1,
    "run_id": run_id,
    "worker": str(payload.get("worker") or ""),
    "source": str(payload.get("source") or ""),
    "ozon_candidates": ordered_candidates,
    "created_at": checkpoint.data.get("created_at") or utc_now_iso(),
    "updated_at": utc_now_iso(),
}
draft_path = self.repo.save_ozon_collection_draft(run_id, draft)
```

相同 seed、相同候选的重复提交必须幂等成功，不增加候选数；批次状态不是 `ozon_collecting` 时返回 `ozon_collection.progress_not_expected`。

- [ ] **Step 5: 接入固定进度路由**

在 `src/ozon_v2/workbench/local_server.py::_handle_POST()` 中、正式 Ozon 结果入口之前加入：

```python
if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "ozon-collection-progress":
    self._send_result(service.save_ozon_collection_progress(parts[2], payload), run_id=parts[2])
    return
```

该路由只保存检查点，不启动 runner、不推进状态机、不生成 `ozon_collection_result.json`。

- [ ] **Step 6: 运行检查点测试并确认 GREEN**

```powershell
python -m pytest tests/test_workbench_local_server.py -k "ozon_collection_progress" -q
```

Expected: 新增检查点测试全部通过；损坏草稿仍保留原内容。

- [ ] **Step 7: 提交 Task 1**

```powershell
git add src/ozon_v2/adapters/fs_repo.py src/ozon_v2/services/workbench_service.py src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
git commit -m "feat: persist Ozon collection checkpoints"
```

### Task 2: 增加受限的浏览器任务重派发 API

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [ ] **Step 1: 写恢复 API 和活动任务的失败测试**

在 `tests/test_workbench_local_server.py` 增加：

```python
def test_ozon_browser_task_exposes_verified_checkpoint_and_progress_url(self) -> None:
    run_id, seeds = self.prepare_ozon_collecting_run(count=2)
    candidate = self.ozon_candidate_payload(seeds[0])
    self.post_json(f"/api/batches/{run_id}/ozon-collection-progress", {
        "run_id": run_id,
        "worker": "workbench_browser_bridge",
        "source": "test",
        "ozon_candidate": candidate,
    })

    task = self.get_json(f"/api/batches/{run_id}/browser-task")

    self.assertEqual(f"/api/batches/{run_id}/ozon-collection-progress", task["data"]["progress_url"])
    self.assertEqual([candidate], task["data"]["resume_candidates"])

def test_restart_ozon_browser_task_preserves_checkpoint_and_refreshes_token(self) -> None:
    run_id, seeds = self.prepare_ozon_collecting_run(count=2)
    candidate = self.ozon_candidate_payload(seeds[0])
    self.post_json(f"/api/batches/{run_id}/ozon-collection-progress", {
        "run_id": run_id,
        "worker": "workbench_browser_bridge",
        "source": "test",
        "ozon_candidate": candidate,
    })
    before = self.get_json(f"/api/batches/{run_id}/browser-task")["data"]["dispatch_token"]
    self.post_json(f"/api/batches/{run_id}/runner/stop", {})

    restarted = self.post_json(f"/api/batches/{run_id}/browser-task/restart", {})
    after = self.get_json(f"/api/batches/{run_id}/browser-task")["data"]["dispatch_token"]

    self.assertEqual("ozon_collection", restarted["data"]["task_type"])
    self.assertEqual(1, restarted["data"]["completed_count"])
    self.assertEqual(1, restarted["data"]["pending_count"])
    self.assertNotEqual(before, after)
    self.assertFalse(self.repo.load_run(run_id)["browser_task_cancelled"])
    self.assertEqual([candidate], self.repo.load_ozon_collection_draft(run_id)["ozon_candidates"])
    self.assertEqual("browser_task.user_restart_requested", self.repo.load_run_events(run_id)[-1].event_type)
```

再增加独立测试覆盖：

- `supplier_review -> supplier_selection`：先保存一件 `supplier_selection_draft.json`，恢复结果只返回剩余通道，完成/待处理计数准确。
- `supplier_collecting -> supplier_collection`：保留合同与状态，只刷新令牌。
- `supplier_collected`、无关状态，以及“正式 Ozon 结果已存在且没有 `replacement_pending_seed_ids`”的不一致完成态：返回 `browser_task.restart_not_allowed` 或 `browser_task.restart_completed`，令牌不变。
- 损坏或不匹配的 Ozon 草稿：返回 `browser_task.restart_checkpoint_invalid`，令牌不变。
- 用 `patch("ozon_v2.workbench.local_server.BRIDGE_HEARTBEAT_TIMEOUT_SECONDS", -1)` 模拟离线：请求仍保存新令牌，但 `dispatch_state == "waiting_for_extension"`。

- [ ] **Step 2: 运行恢复测试并确认 RED**

```powershell
python -m pytest tests/test_workbench_local_server.py -k "restart_browser_task or exposes_verified_checkpoint" -q
```

Expected: FAIL；恢复路由返回 `http.not_found`，活动 Ozon 任务缺少 `progress_url` 和 `resume_candidates`。

- [ ] **Step 3: 在服务层推导唯一允许恢复的任务**

在 `src/ozon_v2/services/workbench_service.py` 增加：

```python
def restart_browser_task(self, run_id: str) -> Result:
    run = self.repo.load_run(run_id)
    task_type_by_status = {
        WorkbenchState.OZON_COLLECTING: "ozon_collection",
        WorkbenchState.SUPPLIER_REVIEW: "supplier_selection",
        WorkbenchState.SUPPLIER_COLLECTING: "supplier_collection",
    }
    status = WorkbenchState(run["status"])
    task_type = task_type_by_status.get(status)
    if task_type is None:
        return Result.failure(
            "browser_task.restart_not_allowed",
            "The current batch stage has no restartable browser collection task.",
            data={"run_id": run_id, "status": run["status"]},
        )
```

然后按任务类型计算计数并拒绝完成态：

- `ozon_collection`：先调用 `ozon_collection_checkpoint()`；正式结果文件存在且没有 `replacement_pending_seed_ids` 时拒绝；若正处于已有结果的补位流程，则允许恢复当前冻结合同里的待补 seed。总数来自当前冻结合同 seed，完成数来自验证后草稿。
- `supplier_selection`：总数来自 `supplier_review.json`，完成数是草稿中与 review seed 相交的唯一 seed；完成数等于总数时拒绝。
- `supplier_collection`：正式结果文件存在即拒绝；总数来自冻结合同；现有服务端没有逐件完成终态文件，因此恢复计数保守为 `0 / total`，扩展继续复用自身持久游标。

合法请求只变更浏览器派发元数据：

```python
dispatch_token = utc_now_iso()
run["browser_task_cancelled"] = False
run.pop("browser_task_cancelled_at", None)
run.pop("browser_task_cancel_reason", None)
run["browser_task_resumed_at"] = dispatch_token
self.repo.save_run(run)
event = self.repo.append_run_event(
    run_id,
    "browser_task.user_restart_requested",
    "The user requested a safe restart of the current browser collection task.",
    {
        "task_type": task_type,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "dispatch_token": dispatch_token,
    },
)
return Result.success(
    "browser_task.restart_requested",
    "The current browser collection task was queued for redispatch.",
    {
        "run_id": run_id,
        "status": run["status"],
        "task_type": task_type,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "dispatch_token": dispatch_token,
        "last_event": event.to_dict(),
    },
)
```

客户端不得提交 `task_type`；服务端不得读取或信任任意任务类型参数。

- [ ] **Step 4: 接入恢复路由并返回扩展等待状态**

在 `src/ozon_v2/workbench/local_server.py::_handle_POST()` 增加：

```python
if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["browser-task", "restart"]:
    result = service.restart_browser_task(parts[2])
    if result.ok:
        bridge = self._bridge_status_payload()
        result.data["dispatch_state"] = "dispatched" if bridge["version_ready"] else "waiting_for_extension"
        result.data["browser_bridge"] = bridge
    self._send_result(result, run_id=parts[2])
    return
```

此入口不调用 `runner.start()`，因为浏览器扩展通过现有任务轮询领取新令牌；正式结果回传后仍由原入口继续自动流程。

- [ ] **Step 5: 把验证后草稿加入活动 Ozon 任务**

在 `_browser_task()` 的 `ozon_collecting` 分支中先加载检查点；损坏时直接返回它的失败 `Result.to_dict()`，成功时加入：

```python
checkpoint = service.ozon_collection_checkpoint(run_id)
if not checkpoint.ok:
    return Result.failure(
        "browser_task.restart_checkpoint_invalid",
        "The saved Ozon collection checkpoint is invalid and was not overwritten.",
        errors=checkpoint.errors,
        data=checkpoint.data,
    ).to_dict()

task_data = {
    "run_id": run_id,
    "created_at": run.get("created_at"),
    "task_type": "ozon_collection",
    "dispatch_token": run.get("browser_task_resumed_at") or run.get("created_at"),
    "contract": selected_repo.load_ozon_collection_contract(run_id),
    "result_worker": "workbench_browser_bridge",
    "ingest_url": f"/api/batches/{run_id}/ozon-collection",
    "progress_url": f"/api/batches/{run_id}/ozon-collection-progress",
    "resume_candidates": checkpoint.data["ozon_candidates"],
}
```

保留现有 1688 `captured_seed_ids` 过滤逻辑，不重建另一套 1688 草稿。

- [ ] **Step 6: 增加中文事件映射**

在首页 `EVENT_PRESENTATIONS` 中加入：

```javascript
"browser_task.user_restart_requested": [
  "浏览器采集已请求恢复",
  "已保留成功结果，并重新派发当前阶段尚未完成的浏览器采集。",
],
```

事件表仍在中文展示下方保留原始机器事件名。

- [ ] **Step 7: 运行恢复 API 测试并确认 GREEN**

```powershell
python -m pytest tests/test_workbench_local_server.py -k "restart_browser_task or exposes_verified_checkpoint" -q
```

Expected: 全部通过；批次状态、合同内容、检查点内容和正式结果均未被恢复请求改变。

- [ ] **Step 8: 提交 Task 2**

```powershell
git add src/ozon_v2/services/workbench_service.py src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
git commit -m "feat: redispatch incomplete browser collection tasks"
```

### Task 3: 让扩展从 Ozon 检查点续跑并先保存后推进

**Files:**
- Modify: `tests/test_browser_collection_progress.js`
- Modify: `browser_extension/ozon_v2_bridge/content.js`
- Modify: `browser_extension/ozon_v2_bridge/manifest.json`

- [ ] **Step 1: 写服务端检查点恢复的失败 Node 测试**

在 `tests/test_browser_collection_progress.js` 的 fetch fixture 中分别记录进度请求和最终请求：

```javascript
const checkpointRequests = [];
const finalRequests = [];
let failNextCheckpoint = false;

// In fetch fixture:
if (String(url).endsWith("/ozon-collection-progress")) {
  const body = JSON.parse(options.body);
  checkpointRequests.push(body);
  if (failNextCheckpoint) {
    failNextCheckpoint = false;
    return { ok: false, json: async () => ({ ok: false, message: "checkpoint failed" }) };
  }
}
if (String(url).endsWith("/ozon-collection")) finalRequests.push(JSON.parse(options.body));
return { ok: true, json: async () => ({ ok: true, code: "ok", data: {} }) };
```

构造两个 seed：`resume_candidates` 已含第一件带 `attributes` 的完整候选，当前页面模拟第二件详情页。执行后断言：

```javascript
assert.equal(checkpointRequests.length, 1, "only the unfinished seed must create a checkpoint request");
assert.equal(checkpointRequests[0].ozon_candidate.seed_id, "seed-pending");
assert.equal(finalRequests.length, 1);
assert.deepEqual(
  finalRequests[0].ozon_candidates.map((item) => item.seed_id),
  ["seed-complete", "seed-pending"],
);
assert.deepEqual(
  finalRequests[0].ozon_candidates[0].attributes,
  { Material: "Steel" },
  "original Ozon attributes from the durable checkpoint must survive final submission",
);
```

再设置 `failNextCheckpoint = true`，直接调用 `runOzonCollection()` 并断言 Promise 被拒绝、最终请求没有增加、session state 的 `seedIndex` 未推进且候选没有加入。更新现有快照复用和正常详情任务夹具，使它们都带 `progress_url`。

- [ ] **Step 2: 运行 Node 测试并确认 RED**

```powershell
node tests/test_browser_collection_progress.js
```

Expected: FAIL；当前扩展忽略 `resume_candidates`，也不会 POST 单件检查点。

- [ ] **Step 3: 实现合同顺序的状态重建**

在 `browser_extension/ozon_v2_bridge/content.js` 增加以下纯函数。它同时合并服务端已验证候选和同一派发令牌下已经成功落盘的 session 候选，按合同排序，并把游标定位到第一件未完成 seed：

```javascript
function reconcileOzonCollectionState(task, currentState = null) {
  const seeds = task.data.contract.payload.seeds || [];
  const excluded = new Set((task.data.contract.payload.excluded_ozon_product_ids || []).map(String));
  const expectedSeedIds = new Set(seeds.map((seed) => String(seed.seed_id || "")));
  const bySeed = new Map();
  const usedProductIds = new Set();
  const sourceCandidates = [
    ...(Array.isArray(task.data.resume_candidates) ? task.data.resume_candidates : []),
    ...((currentState && Array.isArray(currentState.candidates)) ? currentState.candidates : []),
  ];
  for (const candidate of sourceCandidates) {
    if (!candidate || typeof candidate !== "object") continue;
    const seedId = String(candidate.seed_id || "");
    const productId = String(candidate.ozon_product_id || "");
    if (!expectedSeedIds.has(seedId) || !productId || excluded.has(productId)) continue;
    const previous = bySeed.get(seedId);
    if (!previous && usedProductIds.has(productId)) continue;
    if (previous) usedProductIds.delete(String(previous.ozon_product_id || ""));
    bySeed.set(seedId, candidate);
    usedProductIds.add(productId);
  }
  const candidates = seeds.map((seed) => bySeed.get(String(seed.seed_id || ""))).filter(Boolean);
  const seedIndex = seeds.findIndex((seed) => !bySeed.has(String(seed.seed_id || "")));
  const normalizedSeedIndex = seedIndex < 0 ? seeds.length : seedIndex;
  const preserveNavigation = currentState && currentState.seedIndex === normalizedSeedIndex;
  return {
    ...(currentState || {}),
    runId: task.data.run_id,
    taskType: "ozon_collection",
    dispatchToken: task.data.dispatch_token,
    candidates,
    seedIndex: normalizedSeedIndex,
    stage: preserveNavigation ? (currentState.stage || "search") : "search",
    searchEvidence: preserveNavigation ? (currentState.searchEvidence || null) : null,
    productLinkIndex: preserveNavigation ? (currentState.productLinkIndex || 0) : 0,
    rejectedCandidates: preserveNavigation ? (currentState.rejectedCandidates || []) : [],
  };
}
```

在 `runOzonCollection()` 开头替换初始化：

```javascript
const sessionState = loadState(runId, "ozon_collection", task.data.dispatch_token);
let state = reconcileOzonCollectionState(task, sessionState);
```

新令牌不会接纳旧令牌的 session state；服务端检查点是跨关页恢复的唯一来源。

- [ ] **Step 4: 每件候选先持久化再推进游标**

增加：

```javascript
async function checkpointOzonCandidate(task, candidate) {
  return await api(task.data.progress_url, {
    method: "POST",
    body: JSON.stringify({
      run_id: task.data.run_id,
      worker: "workbench_browser_bridge",
      source: "ozon_browser_extension_content_script",
      ozon_candidate: candidate,
    }),
  });
}

function advanceOzonCollectionState(state, seeds, candidate) {
  const bySeed = new Map(state.candidates.map((item) => [String(item.seed_id || ""), item]));
  bySeed.set(String(candidate.seed_id || ""), candidate);
  state.candidates = seeds.map((seed) => bySeed.get(String(seed.seed_id || ""))).filter(Boolean);
  const nextIndex = seeds.findIndex((seed) => !bySeed.has(String(seed.seed_id || "")));
  state.seedIndex = nextIndex < 0 ? seeds.length : nextIndex;
  state.stage = "search";
  state.searchEvidence = null;
  state.productLinkIndex = 0;
}
```

在“复用完整快照”分支中按以下顺序执行：

```javascript
const candidate = buildOzonCandidate(
  reusableSeed,
  reusableQuery,
  { productLinks: [{ href: snapshot.url }] },
  { snapshot, url: snapshot.url, title: snapshot.title },
  decision,
);
saveState(state);
await checkpointOzonCandidate(task, candidate);
advanceOzonCollectionState(state, seeds, candidate);
saveState(state);
```

在“详情页候选通过”分支中使用完整的现有参数执行同样顺序：

```javascript
const candidate = buildOzonCandidate(
  seed,
  query,
  state.searchEvidence || {},
  detailEvidence,
  sellerCheck.decision,
);
saveState(state);
await checkpointOzonCandidate(task, candidate);
advanceOzonCollectionState(state, seeds, candidate);
saveState(state);
```

检查点 POST 失败时让错误继续抛出；不得把候选放进 `state.candidates`，不得增加 `seedIndex`，不得提交最终结果。

- [ ] **Step 5: 运行 Node 测试和语法检查并确认 GREEN**

```powershell
node tests/test_browser_collection_progress.js
node --check browser_extension/ozon_v2_bridge/content.js
```

Expected: 输出 `browser Ozon collection progress: OK`，语法检查退出码为 0。

- [ ] **Step 6: 升级扩展版本并验证清单**

把 `browser_extension/ozon_v2_bridge/manifest.json` 的版本从 `0.1.52` 改为 `0.1.53`，然后运行：

```powershell
python -m json.tool browser_extension/ozon_v2_bridge/manifest.json > $null
```

Expected: 退出码为 0。版本升级确保仍运行旧内容脚本的 Edge 扩展不能通过现有版本门禁。

- [ ] **Step 7: 提交 Task 3**

```powershell
git add browser_extension/ozon_v2_bridge/content.js browser_extension/ozon_v2_bridge/manifest.json tests/test_browser_collection_progress.js
git commit -m "feat: resume Ozon collection from durable checkpoints"
```

### Task 4: 增加两个阶段专用恢复按钮

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [ ] **Step 1: 写页面入口的失败测试**

在 `tests/test_workbench_local_server.py` 增加：

```python
def test_home_page_contains_stage_specific_ozon_restart_control(self) -> None:
    page = self.get_text("/")
    self.assertIn('id="restartOzonCollection"', page)
    self.assertIn("重新启动 Ozon 原商品采集", page)
    self.assertIn("已完成商品及其原始属性字段不会重复采集", page)
    self.assertIn("/browser-task/restart", page)
    self.assertIn('status === "ozon_collecting"', page)

def test_supplier_review_contains_stage_specific_1688_restart_control(self) -> None:
    run_id, _seed = self.prepare_supplier_review_run()
    page = self.get_text(f"/batches/{run_id}/supplier-review")
    self.assertIn('id="collect"', page)
    self.assertIn("重新启动 1688 采集", page)
    self.assertIn("只重新打开未完成通道", page)
    self.assertIn("/browser-task/restart", page)
    self.assertIn('["supplier_review", "supplier_collecting"].includes(state.status)', page)
```

- [ ] **Step 2: 运行页面测试并确认 RED**

```powershell
python -m pytest tests/test_workbench_local_server.py -k "stage_specific" -q
```

Expected: FAIL；首页尚无 Ozon 恢复按钮，采集审核页的 `collect` 仍是永久禁用说明按钮。

- [ ] **Step 3: 在首页 Ozon 进度卡加入恢复入口**

在 `build_home_html()` 的 `ozonCollectionProgress` 标题区加入：

```html
<div>
  <button id="restartOzonCollection" type="button" hidden>重新启动 Ozon 原商品采集</button>
  <p id="ozonRestartMessage" class="hint" hidden>已完成商品及其原始属性字段不会重复采集。</p>
</div>
```

扩展首页 state：

```javascript
const state = {
  runId: requestedRunId || localStorage.getItem("ozon_v2_workbench_run_id") || "",
  pollTimer: null,
  restartPending: false,
};
```

在 `render()` 中根据最新批次状态设置按钮：

```javascript
const status = run.status || data.status || "";
const ozonRestartable = status === "ozon_collecting" && run.ozon_collected !== true;
const ozonComplete = run.ozon_collected === true;
const restartButton = $("restartOzonCollection");
restartButton.hidden = !(ozonRestartable || ozonComplete);
restartButton.disabled = state.restartPending || !ozonRestartable;
restartButton.textContent = ozonComplete ? "Ozon 原商品采集已完成" : "重新启动 Ozon 原商品采集";
$("ozonRestartMessage").hidden = restartButton.hidden;
```

增加 click handler；请求期间禁用，失败时恢复并显示中文错误，离线时只显示等待扩展：

```javascript
async function restartOzonCollection() {
  if (!state.runId || state.restartPending) return;
  state.restartPending = true;
  $("restartOzonCollection").disabled = true;
  try {
    const result = await api(`/api/batches/${encodeURIComponent(state.runId)}/browser-task/restart`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    $("ozonRestartMessage").textContent = result.data.dispatch_state === "waiting_for_extension"
      ? "恢复请求已保存，正在等待浏览器扩展上线。"
      : `已保留 ${result.data.completed_count} 件，继续 ${result.data.pending_count} 件。`;
  } catch (error) {
    $("ozonRestartMessage").textContent = error.message || "Ozon 采集恢复失败。";
  } finally {
    state.restartPending = false;
    await loadRun().catch(() => {});
  }
}

$("restartOzonCollection").onclick = restartOzonCollection;
```

- [ ] **Step 4: 把采集审核页现有 `collect` 改成真正恢复按钮**

保留原元素 ID，初始文案改为：

```html
<button id="collect" class="primary">重新启动 1688 采集</button>
```

把页面 state 增加 `restartPending: false`，并在 `updateButton()` 中使用：

```javascript
const restartable = ["supplier_review", "supplier_collecting"].includes(state.status);
$("collect").disabled = state.restartPending || !restartable;
if (restartable) {
  $("collect").textContent = state.restartPending ? "正在重新派发 (Restarting)" : "重新启动 1688 采集";
  $("message").className = "muted";
  $("message").textContent = "只重新打开未完成通道；已回传的 1688 商品保持完成。";
  return;
}
```

完成态继续显示 `供应商已采集 (Collected)` 并禁用。新增 handler：

```javascript
async function restartSupplierCollection() {
  if (state.restartPending) return;
  state.restartPending = true;
  updateButton();
  try {
    const result = await api(`/api/batches/${encodeURIComponent(runId)}/browser-task/restart`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    $("message").textContent = result.data.dispatch_state === "waiting_for_extension"
      ? "恢复请求已保存，正在等待浏览器扩展上线。"
      : `已保留 ${result.data.completed_count} 个通道，继续 ${result.data.pending_count} 个通道。`;
  } catch (error) {
    $("message").className = "error";
    $("message").textContent = error.message || "1688 采集恢复失败。";
  } finally {
    state.restartPending = false;
    await load().catch(() => {});
    updateButton();
  }
}

$("collect").addEventListener("click", restartSupplierCollection);
```

- [ ] **Step 5: 运行页面和本地服务器回归测试并确认 GREEN**

```powershell
python -m pytest tests/test_workbench_local_server.py -q
```

Expected: 本文件全部通过；测试只请求本地 `127.0.0.1` 假数据服务器。

- [ ] **Step 6: 提交 Task 4**

```powershell
git add src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
git commit -m "feat: add stage-specific collection recovery controls"
```

### Task 5: 锁定关页语义并完成全量验证

**Files:**
- Modify: `tests/test_browser_extension_background.js`

- [ ] **Step 1: 加强已有用户关页回归测试**

在 `tests/test_browser_extension_background.js` 已有 `wb-close` 场景尾部，保留“旧令牌不重开”和“新令牌重开一次”断言，再对同一新令牌重复轮询：

```javascript
const createsAfterExplicitResume = createCount;
await context.pollTask("repeat_same_resume_token");
assert.equal(
  createCount,
  createsAfterExplicitResume,
  "repeated polling with the same explicit resume token must not open another tab",
);
```

这是对既有 background 行为的回归锁定；若该断言已通过，不修改 `background.js`。

- [ ] **Step 2: 运行所有扩展 Node 合同**

```powershell
python -m pytest tests/test_browser_extension_node_contracts.py -q
```

Expected: 所有 Node 合同通过；没有真实浏览器或外部网络调用。

- [ ] **Step 3: 运行后端聚焦测试**

```powershell
python -m pytest tests/test_workbench_local_server.py tests/test_workbench_skeleton.py -q
```

Expected: 两个测试文件全部通过，无失败和错误。

- [ ] **Step 4: 运行全量测试**

```powershell
python -m pytest -q
```

Expected: 退出码为 0，汇总以 `passed` 结束；不得出现任何真实采集、生图、上传、发布或审批动作。

- [ ] **Step 5: 做静态差异和边界检查**

```powershell
git diff --check
git status --short
rg -n "TODO|TBD|PLACEHOLDER|NotImplemented" src/ozon_v2/adapters/fs_repo.py src/ozon_v2/services/workbench_service.py src/ozon_v2/workbench/local_server.py browser_extension/ozon_v2_bridge/content.js tests/test_workbench_local_server.py tests/test_browser_collection_progress.js tests/test_browser_extension_background.js
```

Expected:

- `git diff --check` 无输出且退出码为 0。
- `git status --short` 只列出本计划文件清单中的预期修改。
- `rg` 不出现本次新增的占位实现；历史既有匹配必须逐条确认与本次无关。

- [ ] **Step 6: 对照规格完成自审**

逐项确认：

- 首页按钮只恢复 `ozon_collecting`，审核页按钮只恢复 `supplier_review` / `supplier_collecting`。
- POST body 不接收任务类型，服务端由最新批次状态唯一推导。
- Ozon 正式结果、1688 正式结果、合同和批次状态未被重置。
- Ozon 检查点按合同顺序、seed 唯一、商品 ID 唯一，并保留 `attributes`。
- 新令牌从第一件未完成 seed 继续；已有非连续检查点也会保留并在补齐前序 seed 后跳过。
- 1688 `supplier_selection_draft.json` 仍是唯一逐件检查点，活动任务继续过滤完成通道。
- 旧令牌在用户关页后保持停止；新令牌只重开一次。
- 扩展离线时界面只显示等待，不显示已开始。
- 中文事件展示保留原始 `browser_task.user_restart_requested` 机器事件。

- [ ] **Step 7: 提交回归测试**

```powershell
git add tests/test_browser_extension_background.js
git commit -m "test: lock browser collection restart behavior"
```

## 完成条件

只有在 Task 1–5 全部勾选、聚焦测试和 `python -m pytest -q` 均通过、`git diff --check` 无输出后，才可声明实施完成。最终交接必须明确说明没有触发真实采集、生图、上传、发布或最终审批，并提醒用户在 Edge 扩展页重新加载 `0.1.53` 后再进行人工业务验证。
