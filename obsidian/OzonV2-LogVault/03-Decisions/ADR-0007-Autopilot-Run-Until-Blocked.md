# ADR-0007 自动推进到阻塞点 (Autopilot Run Until Blocked)

日期 (Date): 2026-07-09

状态 (Status): Accepted

## 决策 (Decision)

Ozon V2 工具台的默认运行方式是自动推进到阻塞点 (Autopilot Run Until Blocked)。

用户只需要输入批次目标数量 (Target Count)，点击一次开始自动执行 (Run Until Blocked)。之后由工具台按状态机自动推进，直到遇到必须人工处理、人工确认或后续尚未实现的门禁。

## 范围 (Scope)

自动推进 (Autopilot) 不是让智能体自由选择路径，也不是让用户手动点完整流程。自动推进只能调用工具台状态机允许的动作。

当前已自动覆盖:

- 店铺授权状态检查 (Store Authorization Check)
- 店铺已有商品去重刷新 (Existing Store Dedupe Refresh)
- 种子抽取 (Seed Selection)
- 查询词生成门禁 (Query Generation Gate)
- Ozon 采集契约生成 (Ozon Collection Contract)

当前会停住的门禁:

- 店铺授权绑定缺失 (Store Authorization Binding Required)
- 查询词无法安全生成 (Query Generation Required)
- Ozon/1688 登录、滑块、风控 (Login, Slider, Risk Control)
- 同款证据不足 (Same Product Evidence Required)
- 图片处理结果需确认 (Image Processing Confirmation Required)
- 最终真实发布确认 (Final Publish Confirmation)
- 后续采集工人尚未实现 (Collection Worker Required)

## 规则 (Rules)

- 自动推进只能走 `WorkbenchService.run_until_blocked()`。
- 状态流转必须由 `domain/state_machine.py` 决定。
- 工具台可以显示人工监督动作 (Supervised Actions)，但这些动作是排查入口，不是用户日常主流程。
- 发布锁 (Publish Lock) 继续保持开启，自动推进不能提交真实发布。
- 遇到阻塞点返回 `autopilot.blocked`，这是正常停止，不是异常失败。

## 后果 (Consequences)

- 用户日常操作从多按钮流程收敛为一次开始自动执行。
- 后续新增采集、同款复核、图片处理、AI 填充时，都应接入同一个自动推进入口。
- 每个新能力必须先声明会自动通过哪些状态、会在哪些门禁停住，避免越加越臃肿。
