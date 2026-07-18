# ADR-0004 Obsidian Log Vault

Date: 2026-07-05

## Status

Accepted

## Decision

为 Ozon V2 建立专用 Obsidian 日志库：

```text
E:\ozon-V2工作区\ozon-v2自动化运行\obsidian\OzonV2-LogVault
```

日志库记录：

- 操作日志。
- 架构决策。
- 规则变更。
- 执行边界。
- 校验结果。

## Reason

项目需要可追溯的操作记录，避免规则在多轮施工中漂移，也方便后续打开 Obsidian 直接查看 V2 的历史和决策。

## Consequences

- 每次关键操作都应追加到日志库。
- 日志库不存业务代码。
- 日志库不存 runtime 数据。
- 业务证据仍以 evidence CSV 和 run artifacts 为准。
