# ADR-0001 Architecture Contract

Date: 2026-07-04

## Status

Accepted

## Decision

Ozon V2 使用明确分层：

- FastMCP Layer
- Application Layer
- Domain Layer
- Adapter Layer
- Runtime Data

FastMCP 只做工具、资源、lifespan 注册。业务流程进入 Application service，业务规则进入 Domain，外部 IO 进入 Adapter。

## Reason

防止 V2 继续出现“先堆能力、后补救”的结构问题。每个功能都必须先有边界，再实现能力。

## Consequences

- `mcp/server.py` 不能写业务流程。
- tools 只能做薄入口。
- 状态机必须集中在 Domain。
- Playwright MCP 不嵌入 controller。
- Runtime data 不放业务代码。
- 不导入旧插件，不共享旧运行目录或旧 run id。
