# ADR-0005 工具台架构 (Workbench Architecture)

日期 (Date): 2026-07-08

状态 (Status): Accepted

## 决策 (Decision)

Ozon V2 后续要以工具台 (Workbench) 作为生产运行主体。Codex 只作为开发协作工具，不作为日常采集、AI 填充、图片处理、上架发布的运行脑。

工具台必须由这些部分组成:

- 工具台前端 (Workbench UI)
- 编排后端 (Orchestrator Backend)
- 领域状态机 (Domain State Machine)
- Microsoft Playwright MCP 采集 worker (Collection Worker)
- AI 填充器 (AI Filler)
- 图片处理中心 (Image Processing Center)
- 上架草稿构建器 (Listing Draft Builder)
- 运行数据和审计日志 (Runtime Data And Audit Log)

## 原因 (Reason)

通用智能体如果同时拥有浏览器、代码修改、文件系统和业务判断能力，就可能绕路径、跳门禁、临时改代码或补捷径。工具台架构通过状态机、权限隔离和强制证据，把生产运行流程固定下来。

Superpowers 可以用于开发过程，例如架构先行、写计划、测试驱动和代码审查，但不能替代生产运行期的权限锁。

## 后果 (Consequences)

- 采集只能通过 Microsoft Playwright MCP 任务合约执行。
- Ozon 走代理，1688 直连。
- 1688 以图搜款必须从首页开始。
- AI 只能输出结构化 JSON 草稿，不能控制浏览器或发布商品。
- v0.1 只生成证据和草稿，不自动真实上架。
- 旧 Ozon 插件、Yandex 插件、Codex 内置浏览器、直接 Node Playwright 都不能作为 V2 正式采集路径。
- 后续实现必须先做走通骨架 (Walking Skeleton)，再逐步补真实采集、AI 填充、图片处理和发布门禁。

## 参考 (Reference)

- `docs/workbench_architecture.md`
