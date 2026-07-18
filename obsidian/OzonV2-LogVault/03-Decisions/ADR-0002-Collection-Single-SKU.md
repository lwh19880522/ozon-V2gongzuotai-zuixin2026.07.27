# ADR-0002 Collection Single SKU

Date: 2026-07-05

## Status

Accepted

## Decision

采集只做单 SKU：

- Ozon 只选一个 `target_sku`。
- 1688 只找一个完全一样的 `matched_supplier_sku`。
- 不采多规格。
- 不采多 SKU。
- 不把相似款当同款。

## Reason

当前阶段的目标是验证产品一致性和供应商真实可采，不是建立完整多变体商品池。多 SKU 会增加误匹配、重复采集、后续上传错误的风险。

## Consequences

- Ozon 多规格结构不作为采集目标。
- 1688 多规格结构不作为采集目标。
- 不能证明完全一致时进入人工复核或拒绝。
- 后续上传和生图阶段必须基于这个单 SKU 匹配事实继续展开。
