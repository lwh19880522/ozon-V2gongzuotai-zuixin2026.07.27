# ADR-0008 Ozon 内容分优化证据 (Ozon Content Score Evidence)

日期 (Date): 2026-07-09

状态 (Status): Accepted

## 决策 (Decision)

Ozon 采集必须尽量详细，采集结果不只是为了找到同款，也要为后续内容分优化 (Content Score Optimization) 留证。

每个被接受的 Ozon 候选必须包含 `content_score_evidence`。如果采集器只返回商品链接、标题、价格、图片，而没有内容分优化证据，ingest 必须拒绝。

## 必须采集 (Required Public Evidence)

- 标题原文和标题长度 (Raw Title And Title Length)
- 描述和富内容块 (Description And Rich Content Blocks)
- 属性表和原始 Ozon 字段标签 (Attribute Table And Original Labels)
- 必填或结构性重要属性 (Required Or Important Attributes)
- 精准类目路径、叶子类目、类目 URL、类目 ID (Precise Category Evidence)
- 品牌、价格、币种、原价、折扣、促销信号 (Brand, Price, Discount, Promotion)
- 评分和评论数 (Rating And Review Count)
- 卖家、店铺链接、店铺主体证据 (Seller Evidence)
- 发货地、配送时效、履约标签 (Delivery And Fulfillment Evidence)
- 主图、选中 SKU 图、详情图、图片风格说明 (Media And Style Evidence)

## 可选外部指标 (Optional External Analytics)

如果允许的数据源能提供，可以记录月销量、月销售额、趋势、广告费用占比、促销天数、跟卖/竞争卖家、浏览量、加购率、曝光、转化、点击份额、FBS/FBP 佣金等。

这些外部指标不能编造；没有来源时必须记录为 unavailable，不阻断 Ozon 采集。

## 后果 (Consequences)

- 后续内容分优化不再靠回头补采。
- 采集器必须返回结构化证据，不能只写自然语言总结。
- Ozon ingest 会拒绝缺少 `content_score_evidence` 的候选。
- 这不会恢复多 SKU 采集；仍然只采一个目标 SKU。
