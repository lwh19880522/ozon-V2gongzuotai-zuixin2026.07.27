# Ozon 采集链路解耦重构设计

日期：2026-08-08
状态：已确认，待实施

## 目标

重构 Ozon 商品采集到字段模板生成这一段流程，解决以下系统性问题：

- 种子主体匹配过宽，采入相似但不同的商品；
- Ozon 商品字段被错误用于生成和验收 Seller API 字段模板；
- 模板匹配失败导致已经采集的 Ozon 商品被淘汰并整批回退；
- 补采重新处理已完成槽位；
- 旧浏览器任务在阶段切换后继续运行并覆盖当前状态；
- 10 件商品因多轮返工而长时间无法进入供应商审核。

本次只重构 Ozon 采集、1688 真值锁定、官方类目解析和字段模板获取的边界，不修改生图、定价和上传业务规则。

## 核心原则

1. 种子定义要找的商品主体。
2. Ozon 商品是市场参考来源，不是上传类目和字段模板的真值来源。
3. 1688 锁定商品及 SKU 是商品事实的真值来源。
4. Seller API 是官方上传类目、字段结构、必填项和字典值的唯一来源。
5. 模板解析失败不得淘汰 Ozon 商品，不得触发 Ozon 重新采集。
6. 任何失败只影响当前商品槽位，已完成槽位不得回退或重采。

## 新数据流

```text
种子抽取
  -> 生成精确俄语查询
  -> Ozon 搜索页主体预筛
  -> Ozon 详情页一次性采集市场参考证据
  -> 锁定 Ozon 商品与 SKU
  -> 1688 精确匹配并锁定供应商 SKU
  -> 生成 supplier_truth_profile
  -> 根据 1688 真值解析官方 Ozon type_id
  -> Seller API 下载并缓存字段模板
  -> 自动字段草稿或单件类目待确认
  -> 后续优化、定价和上传
```

流程中不再存在独立的 Ozon `attribute_template_collecting` 浏览器阶段。Ozon 页面可见字段在第一次详情页访问时一次采完。

## 组件设计

### 1. 种子主体锁

每个种子在浏览器搜索前生成结构化主体约束：

```text
seed_subject_contract:
  seed_id
  source_text_zh
  primary_query_ru
  required_subject_terms
  required_qualifiers
  optional_terms
  negative_terms
```

规则：

- `required_subject_terms` 必须命中商品标题、类型或核心属性；
- 不能再使用“任意两个四字符词根相同”作为通过条件；
- 数量、用途、适用对象等决定商品身份的限定词必须参与判断；
- 修复并测试当前乱码的俄语停用词和通用词表；
- 搜索结果无法证明主体一致时，不进入详情页。

### 2. Ozon 一次性参考采集

通过主体预筛后，只访问一次 Ozon 详情页并收集：

- 商品 ID、URL、标题、卖家和价格；
- 单一目标 SKU、选中规格和图片；
- 页面类目、类型、属性、描述和富内容；
- 评分、评论和市场热度证据；
- 中国制造、中国原产地或中国跨境配送证据。

Ozon 的类目、类型和属性全部标记为 `market_reference`。它们可以用于俄语表达、内容结构和市场参考，但不得：

- 选择 Seller API 字段模板；
- 决定 1688 商品事实；
- 触发商品淘汰；
- 覆盖供应商证据。

### 3. 快速失败与种子替换

每个种子的 Ozon 发现阶段设置明确预算：

- 最多检查 3 个通过搜索页预筛的详情候选；
- 正常情况下单种子最多使用 90 秒；
- 候选主体错误、重复、非中国跨境或证据不完整时继续下一个候选；
- 预算耗尽后淘汰该种子并抽取新种子补同一槽位；
- 不重新处理其他已锁定槽位。

预算只控制候选发现，不允许因页面暂时加载较慢而保存不完整证据。

### 4. 1688 商品真值

供应商审核锁定单一 1688 商品和 SKU 后生成：

```text
supplier_truth_profile:
  slot_id
  candidate_revision
  supplier_offer_id
  selected_sku_id
  subject
  intended_use
  material
  dimensions
  weight
  quantity
  package_contents
  compatibility
  audience
  selected_sku_images
  evidence_sources
```

类目解析和客观字段草稿只能读取该真值对象。Ozon 市场参考与 1688 证据冲突时，1688 证据优先。

### 5. 官方类目和字段模板

1688 真值锁定后，由独立的 `SellerCategoryResolver` 执行：

1. 使用 `supplier_truth_profile` 在 Seller API 官方类目树中确定 `description_category_id + type_id`；
2. 唯一高置信匹配时自动确认；
3. 调用 `/v1/description-category/attribute` 下载官方字段模板；
4. 对字典字段按需调用官方字典值接口；
5. 以 `description_category_id + type_id` 为键缓存模板，同类型商品直接复用。

必须删除当前“使用公开 Ozon 类目、标题和类型模糊匹配模板”的依赖。Seller API 返回失败只影响模板解析，不得更改 Ozon 锁定结果。

### 6. 单件人工类目确认

只有无法从完整 1688 真值唯一确定官方类型时，当前商品进入 `category_confirmation_required`。

确认卡片位于供应商审核之后，展示：

- 1688 锁定商品主图、标题和 SKU；
- 工作台推荐的 2 至 3 个官方 Ozon 类型；
- 每个类型的关键区别和匹配证据；
- 官方类型搜索；
- “确认此类目”按钮。

用户确认后保存 `description_category_id + type_id`，立即下载字段模板并继续。该状态不阻塞其他商品，不触发 Ozon 或 1688 重采。

若 1688 页面本身缺失核心证据或 SKU 主体矛盾，应重新采集当前 1688 槽位，而不是让用户凭空选择类目。

### 7. 槽位级状态机

每个商品槽位独立保存：

```text
slot_id
candidate_revision
seed_id
ozon_product_id
supplier_offer_id
selected_supplier_sku_id
category_resolution_status
description_category_id
type_id
field_template_status
```

推荐状态：

```text
seed_ready
ozon_searching
ozon_locked
supplier_searching
supplier_locked
category_resolving
category_confirmation_required
field_template_ready
```

状态只允许向前推进。替换候选时仅增加当前槽位的 `candidate_revision`，旧版本数据只进入审计历史，不能重新进入活动批次。

### 8. 浏览器任务隔离

所有浏览器请求必须绑定：

```text
run_id + slot_id + candidate_revision + task_type + dispatch_token
```

扩展在搜索等待、详情等待和提交前都必须重新读取当前任务身份。任一字段变化时立即中止旧任务。后台服务拒绝旧令牌写入，浏览器状态按任务身份保存，禁止旧标签页覆盖当前阶段。

补采合同只包含待补槽位；已完成槽位仅由服务端检查点提供，不再发给扩展重新采集。

## 错误处理

- 主体不符：搜索阶段拒绝候选，不保存商品。
- 无中国跨境证据：拒绝候选并记录具体缺失证据。
- 单种子候选耗尽：只替换当前种子和槽位。
- 1688 无精确供应商：按现有无供应商规则淘汰当前商品并进入黑名单，只补当前槽位。
- 1688 证据不完整：只重采当前 1688 槽位。
- 官方类型不唯一：进入单件人工确认。
- Seller API 暂时不可用：保留已锁定商品并重试模板下载，不回退采集。
- 字段模板缺少必填值：字段优化器先依据 1688 真值补全；只有无法可靠确定的字段才交给用户。

## 迁移策略

- 新批次只使用新状态机和新合同版本；
- 旧活动批次不自动混入新状态机，需明确重新启动或归档；
- 保留现有 Ozon、1688、黑名单、店铺去重和种子使用审计记录；
- 移除旧的 Ozon 模板浏览器任务入口及其回退路径；
- 不保留兼容旧 `attribute_template_collecting` 回退行为的隐藏分支。

## 验收测试

至少覆盖以下回归：

1. “手球计分记录夹”不能接受普通写字板。
2. 俄语核心主体词缺失时，即使两个泛词相同也必须拒绝。
3. Ozon 商品原类目错误时，商品仍可作为市场参考被锁定。
4. Ozon 类目或 Seller API 模板解析失败不能触发 Ozon 补采。
5. 10 个商品完成后不能因字段模板处理回退到 Ozon 采集。
6. 单槽失败只下发该槽位，已完成槽位不重采。
7. 旧 `dispatch_token` 在等待、导航和提交阶段均被拒绝。
8. 中国原产地或中国制造证据可以直接作为高置信中国商品证据。
9. 1688 锁定 SKU 后才能解析官方类型和下载模板。
10. 同一官方类型的字段模板命中缓存，不重复下载。
11. 自动类型唯一时不显示人工确认。
12. 类型不唯一时只阻塞单件商品，用户确认后可继续。
13. Seller API 暂时失败时保留 Ozon 与 1688 锁定数据。
14. Ozon 参考字段不得覆盖任何 1688 客观真值。

## 完成标准

- Ozon 采集阶段不再调用 Seller API 模板匹配；
- 不再存在模板失败导致商品淘汰或整批回退的路径；
- Ozon 页面字段明确标记为参考数据；
- 1688 真值是官方类目解析和字段草稿的输入；
- 补采、重试和人工确认全部是槽位级操作；
- 正常网络下 10 件商品不发生重复整批采集；
- 全部相关单元测试、浏览器扩展测试和工作台回归测试通过。
