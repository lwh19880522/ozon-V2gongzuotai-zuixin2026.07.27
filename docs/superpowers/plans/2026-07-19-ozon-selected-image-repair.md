# Ozon Selected Image Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Ozon V2 工作台中提供结构化的逐图勾选返修入口，只重新开放用户选中的图片槽位，并让生图技能读取反馈后把新版结果原位回写。

**Architecture:** 在现有 SQLite 图片队列上增加三个向后兼容的反馈字段，由新的原子 `request_repairs()` 操作把所选 `accepted` 槽位转为 `repair_pending` 并把 Job 重新置为 `pending`。工作台通过一个批次归属受控的 POST API 提交反馈；worker 继续领取整件商品，但只处理 `repair_pending` 槽位。旧版回执只允许作为用户未勾选的冻结历史成品保留，新返修回执始终使用当前 v3 契约。

**Tech Stack:** Python 3、SQLite、`http.server`、原生 HTML/CSS/JavaScript、pytest、Markdown Skill 契约测试。

---

### Task 1: 图片队列原子定点返修（Atomic Queue Repair Requests）

**Files:**
- Modify: `tests/test_ozon_image_worker.py`
- Modify: `src/ozon_v2/images/queue.py`

- [ ] **Step 1: 写入所选槽位原子返修失败测试**

在 `tests/test_ozon_image_worker.py` 复用 `_record_eight_v3_slots()`，新增：

```python
def test_user_review_requeues_only_selected_slots_and_preserves_old_outputs(tmp_path: Path) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path)
    queue.mark_ready_for_review(claimed["job_id"], claimed["worker_id"], claimed["lease_epoch"])
    before = {slot["slot_id"]: slot for slot in queue.list_slots(claimed["job_id"])}

    result = queue.request_repairs(
        claimed["job_id"],
        [
            {"slot_id": "main_02", "issue_code": "russian_copy", "note": "俄文太小"},
            {"slot_id": "detail_03", "issue_code": "selling_point", "note": "用途不清楚"},
        ],
        now_epoch=500,
    )

    after = {slot["slot_id"]: slot for slot in queue.list_slots(claimed["job_id"])}
    assert result["job"]["status"] == "pending"
    assert result["job"]["worker_id"] is None
    assert {slot_id for slot_id, slot in after.items() if slot["status"] == "repair_pending"} == {
        "main_02", "detail_03"
    }
    assert after["main_02"]["accepted_path"] == before["main_02"]["accepted_path"]
    assert after["main_02"]["receipt_json"] == before["main_02"]["receipt_json"]
    assert after["main_02"]["review_issue_code"] == "russian_copy"
    assert after["detail_03"]["review_note"] == "用途不清楚"
    assert after["detail_04"] == before["detail_04"]
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_ozon_image_worker.py::test_user_review_requeues_only_selected_slots_and_preserves_old_outputs -q`

Expected: FAIL，原因是 `ImageGenerationQueue` 尚无 `request_repairs`。

- [ ] **Step 3: 写入失败无副作用参数化测试**

覆盖空列表、重复槽位、未知问题类型、`other` 无说明、非 `manual_review_required`、未知/未接受槽位和 `repair_count >= 2`。每次调用前后比较 `queue.snapshot(job_id)`，断言完全相等，并断言错误代码分别为：

```python
image_job.repair_selection_invalid
image_job.repair_feedback_invalid
image_job.repair_not_reviewable
image_job.repair_slot_invalid
image_job.repair_limit_reached
```

- [ ] **Step 4: 运行参数化测试并确认 RED**

Run: `python -m pytest tests/test_ozon_image_worker.py -k "user_review_requeues or repair_request_validation" -q`

Expected: FAIL，原因是迁移字段、错误类型和事务操作尚不存在。

- [ ] **Step 5: 实现反馈字段迁移和原子队列方法**

在 `src/ozon_v2/images/queue.py` 添加：

```python
REPAIR_ISSUE_CODES = frozenset({
    "product_truth", "scene_quality", "composition",
    "selling_point", "russian_copy", "other",
})
MAX_REVIEW_NOTE_LENGTH = 500

class ImageRepairRequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
```

新增 `_ensure_review_feedback_columns()`，分别对 `review_issue_code TEXT`、`review_note TEXT`、`review_requested_at REAL` 执行幂等 `ALTER TABLE`。

实现：

```python
def request_repairs(
    self,
    job_id: str,
    repairs: list[dict[str, Any]],
    *,
    now_epoch: float | None = None,
) -> dict[str, Any]:
```

该方法必须在一个 `BEGIN IMMEDIATE` 事务内完成全部验证和更新；只更新所选槽位的 `status` 与三个反馈字段，保留 `accepted_path`、`receipt_json`、`attempt_count`，最后把 Job 更新为 `pending` 并清空 worker、租约和心跳。返回 `{"job": ..., "slots": ...}`。

- [ ] **Step 6: 运行队列聚焦测试并确认 GREEN**

Run: `python -m pytest tests/test_ozon_image_worker.py -k "user_review_requeues or repair_request_validation" -q`

Expected: PASS。

- [ ] **Step 7: 运行原队列和 worker 回归测试**

Run: `python -m pytest tests/test_image_generation_queue.py tests/test_ozon_image_worker.py -q`

Expected: PASS，且没有 imagegen 调用。

- [ ] **Step 8: 提交队列改动**

```powershell
git add src/ozon_v2/images/queue.py tests/test_ozon_image_worker.py
git commit -m "feat: queue selected image repairs"
```

### Task 2: 旧版回执定点迁移门禁（Legacy Receipt Migration Gate）

**Files:**
- Modify: `tests/test_ozon_image_worker.py`
- Modify: `src/ozon_v2/images/queue.py`

- [ ] **Step 1: 写入普通混版仍被拒绝测试**

保留现有 `test_ready_for_review_rejects_mixed_v2_and_v3_receipts`，并补充断言：没有任何 `review_requested_at` 时，v2/v3 混版继续报 `mixed prompt versions are not allowed`。

- [ ] **Step 2: 写入用户定点返修迁移失败测试**

新增测试，先准备 8 个历史回执，将其中 `main_02` 通过 `request_repairs()` 重新开放，再用当前 `ozon-image-v3` 的 `repair_single` 回执覆盖该槽位，最后调用 `mark_ready_for_review()`：

```python
def test_user_requested_v3_repair_can_coexist_with_frozen_legacy_slots(tmp_path: Path) -> None:
    queue, claimed = _record_eight_v3_slots(tmp_path, mixed_v2=True)
    # 将现有集合先模拟为旧系统已经交给人工审核，再仅开放 main_02。
    with queue._connect() as connection:
        connection.execute(
            """
            UPDATE image_jobs
            SET status = 'manual_review_required', worker_id = NULL,
                lease_expires = NULL, heartbeat_at = NULL
            WHERE job_id = ?
            """,
            (claimed["job_id"],),
        )
    queue.request_repairs(
        claimed["job_id"],
        [{"slot_id": "main_02", "issue_code": "scene_quality", "note": "场景不真实"}],
        now_epoch=500,
    )
    repair_job = queue.claim_next("ozon-image-worker-02", now_epoch=501, lease_seconds=30)
    # 写入当前 v3 单槽回执后，保留的旧槽位不变。
    repair_slot = next(
        slot for slot in queue.list_slots(claimed["job_id"])
        if slot["slot_id"] == "main_02"
    )
    master = SubjectMasterSelection.from_dict(json.loads(repair_job["subject_master_json"]))
    source = tmp_path / "repair-source.png"
    output = tmp_path / "repair-output.png"
    _solid_grid(source, ["red", "green"])
    Image.new("RGB", (120, 90), "red").save(output)
    queue.record_slot_result(
        _slot_receipt(
            job=repair_job,
            slot=repair_slot,
            source_path=source,
            output_path=output,
            accepted=True,
            source_kind="repair_single",
            prompt_version="ozon-image-v3",
            validation=_v3_validation(
                "main_02", master.source_sha256
            ),
        )
    )
    reviewed = queue.mark_ready_for_review(
        repair_job["job_id"], repair_job["worker_id"], repair_job["lease_epoch"]
    )
    assert reviewed["status"] == "manual_review_required"
```

另加一个真实历史值 `ozon-image-v1` 的持久化回执场景，证明它只能作为未勾选冻结槽位读取，不能通过 `record_slot_result()` 新写入。

- [ ] **Step 3: 运行迁移测试并确认 RED**

Run: `python -m pytest tests/test_ozon_image_worker.py -k "mixed_v2 or frozen_legacy or historical_v1" -q`

Expected: 新迁移测试 FAIL；原普通混版拒绝测试 PASS。

- [ ] **Step 4: 最小实现受限迁移规则**

调整 `mark_ready_for_review()`：

1. 查询 `slot_id`、`receipt_json`、`review_requested_at`；
2. 所有回执仍必须通过自身 SHA-256、源文件 SHA-256、输出 SHA-256，并匹配 Job 的 selection/subject 哈希；
3. 新写入当前 v3 回执继续执行完整单槽验证；
4. 只有存在用户反馈的返修周期，才允许未反馈槽位保留 `ozon-image-v1`/`ozon-image-v2` 历史回执；
5. 普通无反馈混版继续拒绝；
6. 全部 v3 时继续运行完整 `validate_visual_set()`；迁移混版时仅对现存 v3 `VisualSpec` 子集运行去重与差异校验，不伪造旧版 spec。

- [ ] **Step 5: 运行迁移与门禁测试并确认 GREEN**

Run: `python -m pytest tests/test_ozon_image_worker.py -k "ready_for_review or frozen_legacy or historical_v1" -q`

Expected: PASS。

- [ ] **Step 6: 运行完整 worker 回归测试**

Run: `python -m pytest tests/test_ozon_image_worker.py -q`

Expected: PASS。

- [ ] **Step 7: 提交迁移门禁改动**

```powershell
git add src/ozon_v2/images/queue.py tests/test_ozon_image_worker.py
git commit -m "feat: support user-directed legacy image migration"
```

### Task 3: 工作台服务与 HTTP 返修 API（Workbench Service and HTTP API）

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [ ] **Step 1: 添加可审核图片 Job 测试夹具**

在 `WorkbenchLocalServerTests` 中增加 `prepare_reviewable_image_job()`，使用当前测试 runtime 的图片队列创建一个属于指定 `run_id` 的 Job，并把 8 个槽位置为可验证的 `accepted` 后进入 `manual_review_required`。测试素材只使用 Pillow 本地色块图。

- [ ] **Step 2: 写入 HTTP 成功路径失败测试**

```python
def test_image_repair_api_requeues_only_selected_slots_and_records_event(self) -> None:
    run_id, job_id = self.prepare_reviewable_image_job()
    result = self.post_json(
        f"/api/batches/{run_id}/image-job/{job_id}/repair",
        {"repairs": [
            {"slot_id": "detail_02", "issue_code": "composition", "note": "主体被遮挡"}
        ]},
    )
    assert result["code"] == "image_job.repair_requested"
    assert result["data"]["image_job"]["status"] == "pending"
    assert result["data"]["image_job"]["slots"][3]["status"] == "repair_pending"
    assert self.repo.load_run_events(run_id)[-1].event_type == "image_job.repair_requested"
```

- [ ] **Step 3: 写入批次归属和无效反馈失败测试**

分别提交错误 `run_id`、未知槽位和 `other` 空说明，断言代码为 `image_job.not_found`、`image_job.repair_slot_invalid`、`image_job.repair_feedback_invalid`，并比较 API 前后 snapshot 无副作用。

- [ ] **Step 4: 运行 API 测试并确认 RED**

Run: `python -m pytest tests/test_workbench_local_server.py -k "image_repair_api" -q`

Expected: FAIL，端点与服务方法尚不存在。

- [ ] **Step 5: 实现服务方法**

在 `WorkbenchService` 新增：

```python
def request_image_repairs(
    self, run_id: str, job_id: str, repairs: list[dict[str, Any]]
) -> Result:
```

验证 Job 批次归属，调用 `queue.request_repairs()`；捕获 `ImageRepairRequestError` 并保留其稳定 code；成功时追加 `image_job.repair_requested` 运行事件，事件数据只包含 job ID、slot ID、issue code、note 和请求时间，不包含图片二进制。

- [ ] **Step 6: 实现 POST 路由**

在 `/api/batches/{run_id}/image-job/{job_id}/{action}` 路由中加入 `repair` 分支，只接受列表型 `repairs`，调用 `service.request_image_repairs()` 后 `_send_result()`。不得启动浏览器 runner、不得调用 imagegen、不得创建 Codex 任务。

- [ ] **Step 7: 运行 API 聚焦测试并确认 GREEN**

Run: `python -m pytest tests/test_workbench_local_server.py -k "image_repair_api" -q`

Expected: PASS。

- [ ] **Step 8: 提交服务与 API 改动**

```powershell
git add src/ozon_v2/services/workbench_service.py src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
git commit -m "feat: expose selected image repair API"
```

### Task 4: 工作台逐图审核交互（Per-Slot Review UI）

**Files:**
- Modify: `tests/test_workbench_local_server.py`
- Modify: `src/ozon_v2/workbench/local_server.py`

- [ ] **Step 1: 写入页面契约失败测试**

扩展 `test_image_workspace_page_and_api_show_real_source_assets`，断言页面包含：

```python
self.assertIn('class="generated-review-card"', page)
self.assertIn('checkbox.type = "checkbox"', page)
self.assertIn('select.className = "repair-issue"', page)
self.assertIn('textarea.className = "repair-note"', page)
self.assertIn('id="submitImageRepairs"', page)
self.assertIn('提交选中图片返修 (Repair Selected)', page)
self.assertIn('issue_code', page)
self.assertIn('review_issue_code', page)
self.assertIn('/repair`', page)
self.assertIn('slot.attempt_count', page)
```

同时断言不再用 `sourcePanel(... generatedImageUrls(item) ...)` 渲染无身份生成图列表。

- [ ] **Step 2: 运行页面测试并确认 RED**

Run: `python -m pytest tests/test_workbench_local_server.py::WorkbenchLocalServerTests::test_image_workspace_page_and_api_show_real_source_assets -q`

Expected: FAIL，逐图审核卡片尚不存在。

- [ ] **Step 3: 实现审核卡片 CSS 与 DOM**

在图片页 CSS 添加 `.generated-review-grid`、`.generated-review-card`、`.repair-fields`、`.repair-badge`、`.repair-submit`。新增 `generatedReviewPanel(item)`，按 `job.slots` 顺序创建卡片：

- 图片 URL 使用 `/slot/{slot_id}/file?v={attempt_count}-{repair_count}` 防止新图被浏览器旧缓存覆盖；
- 只有 Job 为 `manual_review_required` 且槽位为 `accepted` 时允许勾选；
- `repair_pending` 显示“等待返修”并保留旧图；
- 勾选时展开问题类型和说明；取消勾选时清除该卡片校验错误。

- [ ] **Step 4: 实现前端校验和提交**

新增 `collectRepairRequests()` 与 `submitSelectedRepairs()`：

```javascript
await api(`/api/batches/${encodeURIComponent(runId)}/image-job/${encodeURIComponent(job.job_id)}/repair`, {
  method: "POST",
  body: JSON.stringify({ repairs }),
});
```

没有勾选、缺 issue code、`other` 缺 note 时不发送请求。提交期间禁用按钮；成功后 `loadWorkspace(true)`，状态文案为“返修已入队，请重新启动生图总控”。

- [ ] **Step 5: 更新轮询签名**

把 `slot.attempt_count`、`slot.repair_count`、`slot.review_issue_code` 与 `slot.review_requested_at` 加入 `workspaceSignature()`，保证返修提交和新图回写都会触发局部重绘。

- [ ] **Step 6: 运行页面契约测试并确认 GREEN**

Run: `python -m pytest tests/test_workbench_local_server.py::WorkbenchLocalServerTests::test_image_workspace_page_and_api_show_real_source_assets -q`

Expected: PASS。

- [ ] **Step 7: 运行工作台本地服务器回归测试**

Run: `python -m pytest tests/test_workbench_local_server.py -q`

Expected: PASS；不启动真实浏览器或 imagegen。

- [ ] **Step 8: 提交 UI 改动**

```powershell
git add src/ozon_v2/workbench/local_server.py tests/test_workbench_local_server.py
git commit -m "feat: add per-image repair review controls"
```

### Task 5: 生图技能读取用户反馈（Image Skill Feedback Contract）

**Files:**
- Modify: `tests/test_ozon_image_controller_skill.py`
- Modify: `tests/test_image_worker_contract_files.py`
- Modify: `skills/ozon-image-generation-controller/SKILL.md`
- Modify: `skills/ozon-product-media-generator/SKILL.md`
- Modify: `skills/ozon-product-media-generator/assets/repair-slot-prompt.txt`
- Modify: `skills/ozon-product-media-generator/references/prompt-contract.md`

- [ ] **Step 1: 运行无新契约的基线技能场景并记录 RED**

使用一个内部审查子智能体，只给它“snapshot 中一个 `russian_copy` 和一个 `scene_quality` 的 `repair_pending` 槽位，如何处理”的场景，但不提供待修改技能正文。记录它是否会误把俄文问题送入 imagegen、重做未选槽位、把用户 note 当成产品事实或重新执行四次首轮生图。

- [ ] **Step 2: 写入技能契约失败测试**

在两个契约测试文件中断言：

```python
assert "review_issue_code" in skill
assert "review_note" in skill
assert "repair_pending" in skill
assert "only the explicitly selected slots" in skill
assert "russian_copy" in skill
assert "must not be treated as product evidence" in repair_prompt
assert "do not repeat the four mandatory first-attempt calls" in skill
```

- [ ] **Step 3: 运行契约测试并确认 RED**

Run: `python -m pytest tests/test_ozon_image_controller_skill.py tests/test_image_worker_contract_files.py -q`

Expected: FAIL，技能尚未完整描述用户定点返修。

- [ ] **Step 4: 最小更新 controller 与 worker 技能**

controller 增加：返修 Job 按普通整件商品领取，但子智能体只处理 `repair_pending`；不得重开未选 `accepted` 槽位。

worker 增加分支：

```text
if repair_pending slots exist:
  verify locked evidence and existing white-subject anchor
  read review_issue_code and review_note for each selected slot
  russian_copy -> local render-visual repair only, source_kind=copy_repair_local
  all other issue codes -> one repair_single imagegen call per selected slot
  never repeat the four first-attempt generation calls
  never change an unselected accepted slot
```

明确用户说明只是缺陷方向，不是 SKU、数量、尺寸、功能或卖点事实证据。

- [ ] **Step 5: 更新固定 repair prompt 和 prompt contract**

`repair-slot-prompt.txt` 允许附加 `slot_id`、`review_issue_code`、`review_note`，要求根据问题修复视觉缺陷，但禁止把 note 中无供应商 SHA-256 支持的内容画入图片或俄文标签。`prompt-contract.md` 写明 `russian_copy` 不调用 imagegen。

- [ ] **Step 6: 运行技能契约测试并确认 GREEN**

Run: `python -m pytest tests/test_ozon_image_controller_skill.py tests/test_image_worker_contract_files.py -q`

Expected: PASS。

- [ ] **Step 7: 使用相同技能场景复测**

把更新后的技能正文提供给同一个内部审查子智能体，验证它输出：俄文仅本地修复、场景问题只单图 imagegen、未选槽位冻结、note 不作为事实证据、完成后 `ready-for-review`。

- [ ] **Step 8: 提交技能契约改动**

```powershell
git add skills/ozon-image-generation-controller/SKILL.md skills/ozon-product-media-generator/SKILL.md skills/ozon-product-media-generator/assets/repair-slot-prompt.txt skills/ozon-product-media-generator/references/prompt-contract.md tests/test_ozon_image_controller_skill.py tests/test_image_worker_contract_files.py
git commit -m "feat: teach image workers user-selected repairs"
```

### Task 6: 聚焦回归与完成前验证（Focused Regression and Verification）

**Files:**
- Verify only; modify production files only if a failing regression exposes a real defect and first add a reproducing test.

- [ ] **Step 1: 运行图片队列与 worker 测试**

Run: `python -m pytest tests/test_image_generation_queue.py tests/test_ozon_image_worker.py -q`

Expected: PASS。

- [ ] **Step 2: 运行工作台服务和页面测试**

Run: `python -m pytest tests/test_workbench_local_server.py -q`

Expected: PASS。

- [ ] **Step 3: 运行技能与视觉契约测试**

Run: `python -m pytest tests/test_ozon_image_controller_skill.py tests/test_image_worker_contract_files.py tests/test_image_visual_design.py -q`

Expected: PASS。

- [ ] **Step 4: 运行相关回归集合**

Run: `python -m pytest tests/test_workbench_control.py tests/test_workbench_runtime_control.py tests/test_workbench_local_server.py tests/test_image_generation_queue.py tests/test_ozon_image_worker.py tests/test_image_worker_contract_files.py tests/test_ozon_image_controller_skill.py tests/test_image_visual_design.py -q`

Expected: PASS，0 failures。

- [ ] **Step 5: 静态检查变更范围**

Run: `git diff --check HEAD~4..HEAD && git status --short`

Expected: 无空白错误；工作树只包含明确记录的外部文档同步状态，不包含运行 DB、生成图片或浏览器产物。

- [ ] **Step 6: 验证未触发真实生图**

检查本次命令历史与 `git status`，确认没有调用 imagegen、没有新增运行图片、没有上传、没有更改 `E:\ozon-V2工作区\OzonOpsV2` 的业务数据。

### Task 7: Obsidian 项目记录（Obsidian Project Record）

**Files:**
- Create: `E:\obsidian仓库\萧机麦仓库\Ozon 工作台项目库 (Workbench Project)\2026-07-19 Ozon 定点图片返修闭环.md`

- [ ] **Step 1: 写入中文在前、英文括号在后的实施记录**

记录内容包括：

- 用户勾选槽位、必选问题类型、可选说明；
- 仅所选槽位进入 `repair_pending`，未选图片冻结；
- `russian_copy` 本地修复，其他类型单图 imagegen；
- 旧版回执逐槽迁移规则；
- 5 个动态内部生图子智能体 + 1 个恢复位契约不变；
- 验证未触发真实生图、未上传、未最终批准；
- 测试命令和实际通过数量。

- [ ] **Step 2: 只读回读并检查目标路径**

Run: `Get-Content -LiteralPath 'E:\obsidian仓库\萧机麦仓库\Ozon 工作台项目库 (Workbench Project)\2026-07-19 Ozon 定点图片返修闭环.md' -Raw`

Expected: 文件位于 E 盘正确仓库，中文在前、英文括号在后，没有误写到工作区或 C 盘。

- [ ] **Step 3: 最终状态汇报**

报告实现结果、测试证据、功能分支提交、工作台使用方式和 Obsidian 文件路径；不得声称已真实生成或上传图片。
