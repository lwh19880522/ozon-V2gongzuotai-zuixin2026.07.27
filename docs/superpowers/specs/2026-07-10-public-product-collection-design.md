# Ozon 公开商品完整采集设计 (Public Product Collection Design)

日期 (Date): 2026-07-10

状态 (Status): 用户已批准直接实施 (Approved For Direct Implementation)

## 目标 (Goal)

一个 Ozon 商品只有在真实产出标题、精准类目、真实属性值、单一 SKU、商品图片、价格、评分评价、卖家、配送和中国跨境店证据后才算采集成功。页面跳转、候选标题或任务合同不算采集结果。

## 数据来源 (Sources)

- JSON-LD 商品结构：商品 ID/SKU、标题、品牌、价格、币种、评分、评价数和商品图片。
- Ozon 稳定 `data-widget` 与面包屑 DOM：类目路径、类目 URL、类目 ID、卖家、配送、履约、属性名称和值、详情图片。
- Ozon Seller API：根据已确认类目取得 `description_category_id`、`type_id`、必填上传属性与字典值。

## 成功合同 (Success Contract)

每个商品必须包含：

- `ozon_product_id`、`ozon_url`、`title`；
- 数字 `category_id`、`category_path`、`leaf_category`、`category_url`；
- 真实 `target_sku.sku_id` 和选中规格；
- 非空真实属性表，禁止 `visible_on_detail_page` 等占位符；
- 非空主图与选中 SKU 图片；
- `price`、`currency`、`rating`、`review_count`；
- `seller_name`、`seller_url`；
- `delivery_origin`、`delivery_time`、`fulfillment_label`；
- 中国跨境店高置信判断与证据原文；
- Seller API 上传属性模板。

缺少关键字段时保持阻塞，不生成完成结果，不移除种子，不进入生图或上传。

## 页面就绪 (Readiness)

- 不能再用整页出现“商店/卖家/公司”等通用词判断就绪。
- 明确本土仓证据可快速拒绝；明确中国配送/Ozon Global 中国/中国卖家主体证据可继续采集。
- 判断为 `unknown` 时等待目标配送与卖家区域，直到出现强证据或 25 秒超时；超时按“证据不可用”记录，不冒充本土店。
- 商品结构必须在连续两次采样中稳定且通过核心字段检查后才能提交。

## 范围 (Scope)

只修 Ozon V2 新扩展、结果校验和本地工作台采集结果。1688、经营分析、生图、上传及旧插件均不修改。

## 验证 (Verification)

- 纯函数测试覆盖 JSON-LD、属性值、图片筛选与完整性判断。
- 海外店证据测试证明通用卖家词不再触发就绪。
- Python 门禁测试证明占位属性、缺 SKU/图片/卖家/配送的数据不能落盘。
- 使用 `seed-0247`“鼠标垫”重新实战；只有同时生成并通过校验的 `attribute_template_result.json` 与 `ozon_collection_result.json` 才能报告成功。
