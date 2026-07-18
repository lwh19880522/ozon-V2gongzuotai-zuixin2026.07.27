# ADR-0003 Seed And Dedupe Gates

Date: 2026-07-05

## Status

Accepted

## Decision

每次运行必须按固定顺序：

1. 刷新目标店铺已有产品 dedupe list。
2. 用 dedupe list 过滤种子。
3. 从 active seed pool 随机抽取 `target_count` 个种子。
4. 从抽中的种子开始 Ozon 采集。
5. 匹配一个完全一样的 1688 单 SKU。
6. 运行结束后，将抽过的种子从 active seed pool 剔除并写入 used-seed archive。

## Reason

防止乱采集、重复采集、重复上传，以及反复从同一批种子里抽到已处理产品。

## Consequences

- 店铺已有产品绝不继续采集。
- 抽过的种子不再进入 active pool。
- 原始 500 种子包只用于新用户初始化，不能在后续启动时恢复已用种子。
- 可用种子不足时返回 `insufficient-seeds`，不能自由浏览补数。
