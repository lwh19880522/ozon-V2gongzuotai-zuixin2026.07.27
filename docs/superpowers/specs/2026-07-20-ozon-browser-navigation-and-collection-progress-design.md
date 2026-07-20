# Ozon 浏览器通道导航与中文采集进度设计

日期：2026-07-20

## 目标（Goal）

修复受控 1688 供应商通道在商品选择过程中的三个连续性问题：普通点击详情商品时必须覆盖当前通道标签而不是另开标签；详情页始终显示采集控件；用户返回、前进或重新选择商品后采集控件必须自动恢复。同时在 Ozon V2 工作台批次页增加中文采集数字进度条，并把运行事件的主要显示转换为中文。

## 已确认的交互（Confirmed Interaction）

采用方案 A：一体式悬浮操作条与主区进度卡。

1688 悬浮操作条包含：

- `← 返回选品`；
- 当前通道编号与绑定商品摘要；
- 当前页面是否可以采集的中文状态；
- `采集当前商品`；
- `无供应商`。

返回按钮优先返回该通道上一页；如果标签没有可返回历史，则在同一标签导航到 1688 首页，并重新准备该通道已经分配的参考图。悬浮操作条允许拖动，并把最后位置保存到扩展本地存储；页面恢复时要把位置限制在当前可视区域内。

工作台在“运行事件”上方显示 Ozon 采集进度卡：

- 已处理 `X / Y`；
- 成功 `S`；
- 最终失败 `F`；
- 已替换 `R`；
- 待处理 `P`。

种子尝试耗尽但随后被新种子补位时只增加“已替换”，不增加“最终失败”，也不得导致已处理数超过总数。

## 根因（Root Causes）

现有 managed supplier channel 以 `tabId` 绑定通道。1688 搜索结果可能使用 `_blank`、`window.open` 或 `noopener` 打开详情页；后台只能在新标签具有有效 `openerTabId` 且仍在受控窗口时接管它。没有 opener 的新标签无法确定所属通道，因此详情页取不到 binding，采集面板不会创建。

内容脚本当前只在初次执行时初始化一次；后台的 ping 成功只证明脚本存在，不证明采集面板仍存在。1688 的 SPA 跳转、浏览器前进/后退、BFCache 恢复或页面局部重建可能删除面板，却不会重新执行初始化流程。

工作台当前直接渲染事件的英文 `event_type` 与 `message`。Ozon 实时采集进度只存在于浏览器内容脚本的运行状态和英文 heartbeat 文本中，服务端没有可以安全显示的完整结构化实时计数。

## 1688 同标签导航（Same-Tab Navigation）

`supplier_content.js` 在确认当前标签具有 managed channel binding 后，使用捕获阶段的委托点击监听处理合法的 1688 商品详情链接：

- 只拦截无修饰键的普通左键点击；
- 链接必须是允许的 `*.1688.com/offer/<offer-id>.html` 商品详情地址；
- 调用 `preventDefault()` 与 `stopImmediatePropagation()`；
- 使用 `location.assign(detailUrl)` 在当前标签导航。

Ctrl、Shift、Alt、Meta 或中键表示用户明确要求浏览器原生行为，不做错误的通道猜测。由这些操作产生的非受控新标签不得自动绑定到任意通道。

后台保留现有 popup 接管作为兼容兜底，但它不再是正常详情导航路径。成功接管新标签后必须立即执行内容脚本确认与面板刷新，不能只等待 `tabs.onUpdated(status=complete)`。

## 面板生命周期（Panel Lifecycle）

把一次性的 `managedPanel(binding)` 重构为幂等的 `reconcileManagedPanel(binding)`：

- 页面不存在面板时创建一个；
- 已存在时更新通道标题、URL 状态和按钮可用性；
- 任意时刻只能有一个 `#ozon-v2-supplier-panel`；
- binding 不存在时不得凭窗口或标签顺序猜测通道。

以下入口都必须调用重新协调：

1. 初次获得 channel binding；
2. `pageshow`，包括 BFCache 恢复；
3. `popstate`；
4. `history.pushState()`、`history.replaceState()` 或 URL 发生变化；
5. 后台发送 `ozon_v2_supplier_channel_refresh`；
6. 受限的 `MutationObserver` 发现面板被页面移除。

面板恢复必须去抖，避免 1688 高频 DOM 更新重复创建控件。内容脚本 ping 返回扩展版本、binding 是否存在和 panel 是否存在；后台在 ping 成功后仍发送 refresh，而不是把“脚本存在”等同于“界面就绪”。

无法绑定时发送结构化诊断 heartbeat，包含 tab、window、opener 和 URL 信息，但不暴露页面敏感内容。

## 返回选品（Back To Selection）

悬浮操作条向后台发送当前通道的返回请求。后台只操作消息发送者所在的受控 tab：

- 能后退时使用浏览器 tab history 返回；
- 不能后退时更新同一 tab 到 `https://www.1688.com/`；
- 回到首页后由既有 reference-image preparation 流程恢复分配的参考图；
- 操作完成后发送 refresh，确保面板和通道状态恢复。

不创建新的选品页，不关闭当前通道，也不影响其他通道。

## Ozon 结构化采集进度（Structured Collection Progress）

浏览器 Ozon 内容脚本的 heartbeat `details` 增加：

```json
{
  "collection_progress": {
    "total_count": 5,
    "processed_count": 3,
    "success_count": 2,
    "failure_count": 0,
    "replacement_count": 1,
    "pending_count": 2
  }
}
```

字段约束：

- 所有值都是非负整数；
- `processed_count <= total_count`；
- `pending_count = max(total_count - processed_count, 0)`；
- 候选级 `candidate_rejected` 不增加 seed 失败；
- seed 耗尽并触发补位只增加 `replacement_count`；
- 只有批次在 Ozon 采集门禁处终止且没有可用补位时才增加 `failure_count`；
- heartbeat 的 `run_id` 必须与当前工作台批次一致，否则页面忽略；
- 存在最终 `ozon_collection_result.json` 时，以最终候选数覆盖实时成功数。

不新增数据库表。实时数据继续使用浏览器桥状态，稳定的最终数据继续来自批次运行文件与事件。服务层向首页快照提供经过归一化的 `ozon_collection_progress`，页面不解析英文 heartbeat 文本。

## 中文运行事件（Chinese Run Event Presentation）

原始 `RunEvent.event_type`、`message`、`data`、`events.jsonl`、事件 API 和诊断 ZIP 均保持不变。中文转换只发生在工作台显示层。

首页增加 `eventPresentation(event)`：

- 对常见事件类型提供中文名称和中文消息模板；
- 动态数字和商品信息只从 `event.data` 读取，不解析英文 message；
- 主行显示中文；
- 原始 `event_type` 以灰色小字保留；
- 未知事件显示“系统事件（查看原始信息）”，并把原始 message 放在次要诊断区域；
- 所有事件内容通过 `textContent` 写入 DOM，避免把外部数据拼入 `innerHTML`。

第一批映射至少覆盖：Ozon 采集入库、runner 启动/停止/阻塞、自动运行启动/阻塞、供应商商品采集、浏览器任务取消、候选拒绝与 seed 耗尽补位。

## 错误与恢复（Errors And Recovery）

- 无 channel binding：不显示可提交的采集按钮，发送中文可识别状态与结构化诊断。
- 返回失败：面板保留，显示“返回失败，请重试”，不清除 binding。
- 面板被页面删除：去抖后自动恢复。
- 进度 heartbeat 缺失或陈旧：显示已知最终数据；没有可靠数据时显示“等待采集数据”，不猜测计数。
- 未知运行事件：中文主提示加原始诊断信息，不修改历史事件。

## 测试策略（Testing）

先写失败测试，再做最小实现。

浏览器 Node 测试覆盖：

1. 现有 `test_supplier_same_tab_navigation.js` 从 RED 转 GREEN；
2. 普通详情链接同标签导航，修饰键、中键与非详情链接不拦截；
3. `pageshow`、`popstate`、URL 变化、面板被移除和后台 refresh 后只恢复一个面板；
4. 返回使用同一受控 tab，有历史时后退，无历史时回首页；
5. popup 接管后立即刷新内容脚本；无 opener 的孤立页只诊断、不误绑；
6. Ozon heartbeat 在 snapshot reuse、普通成功、seed 耗尽补位和最终提交时携带合法计数。

Python 测试覆盖：

1. 首页包含中文进度卡、数字字段和 `aria-valuenow` / `aria-valuemax`；
2. 只接受当前 `run_id` 的实时进度；
3. 最终结果覆盖实时 heartbeat；
4. 已替换与最终失败分开，任何状态下处理数不超过总数；
5. 常见运行事件显示中文，未知事件安全回退；
6. 原始事件 API 和诊断分类保持原值。

Node 浏览器扩展测试必须加入统一验证命令，避免独立 RED 脚本继续被标准 `pytest` 漏过。

## 验收标准（Acceptance Criteria）

- 五个通道行为一致，第三、第四通道没有特殊分支；
- 普通选择商品始终覆盖所属通道原标签；
- 所有受控详情页都有采集面板；
- 返回、重新选品和页面局部刷新后面板自动恢复；
- 返回不会关闭通道或打开新选品标签；
- 工作台显示中文 Ozon 进度和成功/最终失败/已替换/待处理数字；
- 运行事件主显示为中文，同时保留机器事件码；
- 不启动真实采集、不生成图片、不上传、不最终批准即可完成自动测试验证。

## 非目标（Non-Goals）

- 不建立新的扩展管理页或浏览器侧边栏；
- 不修改 1688 网站自身布局；
- 不把显式 Ctrl/中键新标签错误接管为 managed channel；
- 不修改原始事件机器码或历史事件文件；
- 不新增数据库表；
- 不在验证过程中触发真实采集、生图或上传。
