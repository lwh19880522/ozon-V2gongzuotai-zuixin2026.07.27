# Ozon 定点图片返修设计（Ozon Selected Image Repair Design）

## 目标（Goal）

在图片处理工作台中允许用户勾选一张或多张不合格成品，选择明确的问题类型并可补充说明。系统只把所选槽位重新放入返修队列，生图技能读取反馈后定点修复，并把新图片与新回执写回原槽位；未选图片不得重生成、覆盖或丢失。

## 范围与边界（Scope and Boundaries）

- 适用于已经进入 `manual_review_required` 的 Ozon V2 图片 Job。
- 只允许返修当前 Job 中状态为 `accepted` 的已生成槽位。
- 不启动浏览器，不上传图片，不进行最终批准，不从本地服务直接创建 Codex 子智能体。
- 提交返修只负责原子入队；用户随后重新启动生图总控，固定映射的内部子智能体领取整件商品，但只处理 `repair_pending` 槽位。
- 继续遵守最多 5 个动态生图子智能体和 1 个恢复位的生产契约。
- 保持既有每槽最多两次场景返修限制；达到上限时拒绝新返修请求并给出明确错误。

## 用户交互（User Interaction）

生成结果区不再使用无身份的普通图片列表，而是按 `slot_id` 展示 8 张审核卡片。每张卡片包含图片、槽位名称、状态和“不合格，申请返修”复选框。

勾选后显示：

1. 必选问题类型：
   - `product_truth`：主体、数量、颜色或结构错误
   - `scene_quality`：场景不真实、不美观或融合差
   - `composition`：构图、裁切、遮挡或比例问题
   - `selling_point`：卖点不清楚或用途证明不足
   - `russian_copy`：俄文标签错误、不清晰或排版不佳
   - `other`：其他问题
2. 可选补充说明；选择 `other` 时补充说明必填。

“提交选中图片返修”按钮显示选中数量。没有勾选、缺少问题类型或 `other` 缺少说明时禁止提交并就地提示。提交成功后：

- 所选卡片保留旧图并显示“等待返修”；
- 未选卡片保持原状态；
- Job 显示“返修已入队，请重新启动生图总控”；
- 页面轮询新状态，新回执被接受后在相同卡片原位替换图片。

## API 契约（API Contract）

新增端点：

`POST /api/batches/{run_id}/image-job/{job_id}/repair`

请求体：

```json
{
  "repairs": [
    {
      "slot_id": "detail_03",
      "issue_code": "selling_point",
      "note": "使用方法不直观，场景没有证明收纳能力"
    }
  ]
}
```

服务层验证：

- Job 存在并属于路径中的批次；
- Job 当前为 `manual_review_required`，且没有活动租约；
- `repairs` 非空、槽位不重复、槽位存在且当前为 `accepted`；
- `issue_code` 在固定白名单中；
- `note` 是字符串且长度受限；`other` 必须有非空说明；
- 所选槽位没有达到返修次数上限。

成功返回 `image_job.repair_requested`、更新后的 Job、所选槽位和反馈。任何验证失败均不得部分更新数据库。

## 队列与持久化（Queue and Persistence）

对 `image_slots` 采用向后兼容的加列迁移，保存最近一次用户反馈：

- `review_issue_code TEXT`
- `review_note TEXT`
- `review_requested_at REAL`

`ImageGenerationQueue.request_repairs()` 在一个 `BEGIN IMMEDIATE` 事务内完成：

1. 验证 Job 与全部请求槽位；
2. 仅把所选槽位从 `accepted` 改为 `repair_pending`；
3. 保留旧 `accepted_path` 和旧 `receipt_json`，使返修期间仍能查看旧图；
4. 写入问题类型、说明和请求时间；
5. 把 Job 改为 `pending`，并清空 `worker_id`、租约和心跳。

worker 领取 Job 后读取槽位反馈。`russian_copy` 只允许本地文字层返修，不调用 imagegen；其他类型使用 `repair-slot-prompt.txt` 对所选槽位执行单图场景返修。成功回执覆盖该槽位的成品路径和回执，槽位恢复 `accepted`；其他槽位不写入。

## 旧版回执兼容（Legacy Receipt Compatibility）

当前批次含旧版技能生成的历史回执。定点返修必须允许用户保留未勾选的旧图，同时让所选槽位写入当前 `ozon-image-v3` 回执。

兼容规则限定为用户发起的返修周期：

- 新写入的返修回执必须始终使用当前版本并通过当前单槽验证；禁止新写入旧版回执。
- 未勾选、未改写的历史槽位可以保留其原回执和文件哈希。
- `ready-for-review` 允许“保留的历史槽位 + 用户定点返修后的当前槽位”混合回到人工审核，但只在数据库存在对应用户返修反馈时启用迁移兼容。
- 当 8 张图片全部为当前版本时，继续执行完整的 `ozon-visual-v1` 八图集合校验；迁移期间不伪造旧图不存在的 `visual_spec`。
- 文件存在性、SHA-256、商品选择哈希和主体证据哈希仍必须验证，兼容规则不得绕过产品真实性校验。

## 错误处理（Error Handling）

- 非人工审核状态提交：返回 `image_job.repair_not_reviewable`。
- 空选择或重复槽位：返回 `image_job.repair_selection_invalid`。
- 未知槽位或未接受槽位：返回 `image_job.repair_slot_invalid`。
- 问题类型或说明无效：返回 `image_job.repair_feedback_invalid`。
- 返修次数已满：返回 `image_job.repair_limit_reached`。
- 任一错误均保持 Job、所有槽位、文件和租约原样不变。
- 返修 worker 失败时只保留对应槽位为 `repair_pending` 或进入既有人工门禁，不影响未选槽位。

## 测试策略（Testing Strategy）

采用测试驱动开发，不触发真实生图：

1. 队列测试先证明多个选中槽位会原子转为 `repair_pending`，未选槽位的状态、路径和回执完全不变。
2. 覆盖空选择、重复槽位、无效问题类型、`other` 无说明、非审核状态和返修次数上限，验证失败无副作用。
3. 服务与 HTTP 测试验证批次归属、端点请求体、事件记录和响应代码。
4. 页面契约测试验证 8 张槽位卡片、复选框、问题类型、补充说明、选中数量、提交按钮和返修状态文案。
5. 技能契约测试验证 worker 必须读取 `review_issue_code` 与 `review_note`，只处理 `repair_pending`，俄文问题只走本地文字层，其他问题才允许单图 imagegen。
6. 兼容测试使用旧版回执与新版返修回执的混合集合，证明只有用户定点返修迁移路径可回到 `manual_review_required`，普通混版仍被拒绝。
7. 运行图片队列、worker、工作台服务与本地服务器相关回归测试；禁止调用真实 imagegen。

## 完成标准（Completion Criteria）

- 用户可在工作台勾选任意已接受槽位并提交结构化返修反馈。
- 队列只重新开放所选槽位，未选图片及其哈希不变。
- 生图技能能从快照读取反馈并按问题类型选择本地文字返修或单图场景返修。
- 新图通过后自动替换原槽位并再次进入人工审核。
- 当前旧版批次支持逐张迁移，不要求一次性重做全部 8 张。
- 所有聚焦测试与相关回归测试通过，验证过程不生成真实图片。
