# ADR-0009 种子属性模板前置 (Seed Attribute Template Prerequisite)

日期 (Date): 2026-07-09

状态 (Status): Accepted

## 决策 (Decision)

抽取种子后，必须先抓取 Ozon 类目和属性模板 (Category And Attribute Template)，再进入 Ozon 同行商品采集。

这一步用于提前知道上传时要选择什么类目、需要填写哪些字段、哪些字段有 Ozon 字典选项。后续构建上传草稿时，可以提前批量预填客观属性，避免上传前逐个商品补字段。

## 流程位置 (Workflow Position)

```text
店铺去重 (Store Dedupe)
-> 抽取种子 (Select Seeds)
-> 生成 Ozon 查询词 (Generate Ozon Queries)
-> 抓取种子属性模板 (Fetch Seed Attribute Template)
-> Ozon 同行商品采集 (Ozon Product Collection)
```

`seed_selected` 不能再直接进入 `ozon_collecting`。必须先进入 `attribute_template_collecting`，模板采集完成后进入 `attribute_template_collected`，才允许开始 Ozon 商品采集。

## 草稿预填规则 (Draft Prefill Policy)

可以复用的客观事实字段 (Objective Facts):

- 尺寸 (Dimensions)
- 重量 (Weight)
- 材质 (Material)
- 颜色 (Color)
- 尺码 (Size)
- 容量 (Capacity)
- 数量/套装内容 (Quantity / Package Contents)
- 兼容型号 (Compatibility)
- 型号 (Model)
- 电压/功率 (Voltage / Power)

必须重写的创意内容 (Creative Content):

- 标题 (Title)
- 描述 (Description)
- 富内容 (Rich Content)
- 营销卖点 (Marketing Claims)
- 要点文案 (Bullet Points)
- SEO 关键词 (SEO Keywords)
- 图片文字 (Image Text)

如果 1688 真实供应商证据与 Ozon 同行属性冲突，以真实供应商证据为准。

## 后果 (Consequences)

- Ozon 采集前必须先完成模板门禁。
- 上传草稿可以提前按模板填入客观属性。
- 标题、描述、富内容不能照抄 Ozon 同行。
- 后续 Playwright MCP 执行器必须先实现模板采集工人，再实现 Ozon 商品采集工人。
