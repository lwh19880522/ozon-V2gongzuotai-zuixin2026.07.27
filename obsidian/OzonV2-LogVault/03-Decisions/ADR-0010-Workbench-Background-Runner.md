# ADR-0010 工具台后台执行器 (Workbench Background Runner)

日期 (Date): 2026-07-09

状态 (Status): Accepted

## 决策 (Decision)

工具台点击 `新建批次/开始自动执行 (Run Until Blocked)` 后，必须由本地后台执行器 (Background Runner) 接管流程，而不是依赖用户回到 Codex 聊天框继续发指令。

后台执行器只允许调用应用层状态机 (Application State Machine)，不能直接跳状态、不能绕过门禁、不能修改业务代码。

## 执行边界 (Execution Boundary)

允许 (Allowed):

- 创建批次后自动启动 Runner。
- Runner 调用 `WorkbenchService.run_until_blocked()`。
- Runner 将执行过程写入 `events.jsonl`。
- 页面自动轮询并显示实时状态、事件、阻塞原因。

禁止 (Forbidden):

- 页面按钮直接绕过状态机。
- Runner 直接写采集结果或发布结果。
- Browser worker 修改业务状态。
- 为了加速而改用 Codex 内置浏览器、旧插件或直接 Playwright。

## 当前门禁 (Current Gate)

当前 Runner 已能自动推进到下一个阻塞点。真实 Ozon/1688 网页采集仍必须由后续 Microsoft Playwright MCP Worker 适配层完成。

也就是说，工具台现在负责实时调度和显示；采集 Worker 负责按任务契约采集证据；业务状态仍由应用层统一收口。

## 后果 (Consequences)

- 用户点击新建批次后，页面会实时更新，不需要回聊天框继续推进前置流程。
- 遇到登录、滑块、查询词缺失、Playwright MCP Worker 未实现等门禁时，Runner 停止并写明原因。
- 下一步必须实现 Playwright MCP Worker 适配层，才能把 `worker_required` 门禁替换成真实采集。
