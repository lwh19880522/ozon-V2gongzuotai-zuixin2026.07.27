# Temu 官方 API 操作说明

本通道在现有 Ozon V2 工作台内增加独立的 Temu 官方 API 操作入口，当前交付边界是常规店铺。它不依赖海外仓，也不实现半托管流程；Temu 与 Ozon 的凭据、预览、提交记录和状态记录均保持隔离。

依据仓库内封存的 [官方 API 合约](official-api-contract.json)，商品创建使用：

- 请求：`POST https://openapi-b-global.temu.com/openapi/router`
- 发布方法：`temu.local.goods.v3.add`
- 状态方法：`temu.local.goods.list.retrieve`
- 授权模型：Temu Partner Platform 应用经卖家授权
- 权限包：`Local Product Management`

## 1. Temu 官方授权

1. 在 [Temu Partner Platform](https://partner.temu.com/login) 创建并发布应用。
2. 为应用申请当前流程所需的 `Local Product Management` 权限。
3. 由目标常规店铺的卖家授权该应用，取得该店铺对应的访问令牌。
4. 把凭据只写入运行机器的本地配置，不要写入本仓库、聊天记录、截图、日志或诊断输出。
5. 配置经过批准的签名提供器。官方公共方法页明确要求公共参数 `sign`；本项目不臆测或内置未验证的签名算法。

官方公共参数还包括 `type`、`app_key`、`access_token` 和十位秒级 `timestamp`。签名提供器默认缺失，因此即使预览可用，真实提交仍会在本地拒绝。

## 2. 本地配置

默认凭据文件：

```text
state/config/temu_credentials.json
```

只使用占位结构准备文件，随后由有权限的操作员在本机替换占位值：

```json
{
  "store_id": "<TEMU_STORE_ID>",
  "app_key": "<TEMU_PARTNER_APP_KEY>",
  "access_token": "<TEMU_SELLER_ACCESS_TOKEN>"
}
```

不要把应用密钥、访问令牌或签名材料放进 JSON 示例、源码或 Git。签名能力通过经过批准的本地签名提供器注入，不由预览请求承载。默认幂等记录位于：

```text
state/temu_idempotency.json
```

`goodsBasic.externalGoodsId` 和每个 `skuList[].externalSkuId` 必须在同一店铺内唯一。工作台会在发出请求前持久化这些标识，重复操作必须沿用既有记录，不能通过更换或复用标识绕开幂等保护。

## 3. 工作台操作顺序

### 3.1 生成预览

在商品证据、已确认价格、SKU、公开图片 URL 和包装数据齐全后，点击 Temu 区域的预览操作。预览只包含拟发送的业务字段，不包含 `access_token`、签名材料或其他密钥。

正式操作前还要确认目标 Temu 站点接受预览中的币种、金额精度和限额。包装重量使用克（g），长、宽、高使用厘米（cm），请求值为不带单位的数字字符串。当前工作台复用已确认的价格和包装证据，但这不等于目标站点已经接受该币种和数值。

### 3.2 核对并精确确认

逐字段核对预览，特别是：

- 外部商品和 SKU 标识；
- 商品名称、类目提示、属性与变体；
- 图片是否为 Temu 可下载的公开 URL；
- 币种、价格、库存；
- 包装重量和尺寸。

工作台为完整预览计算 SHA-256。真实提交必须同时提供：

```json
{
  "confirmed": true,
  "preview_hash": "<当前预览的精确 SHA-256>"
}
```

任何字段变化都会产生新的哈希；旧哈希不得确认新预览。工作台不会自动确认，也不会把普通按钮点击当作确认。

### 3.3 提交创建请求

通过哈希门禁后，工作台才会尝试调用 `temu.local.goods.v3.add`。下列任一条件缺失时都必须在本地 fail-closed，不发出真实请求：

- 真实、有效且与店铺匹配的本地 Temu 凭据；
- 卖家已完成官方应用授权；
- 经过批准的签名提供器；
- 当前预览的精确哈希和显式 `confirmed=true`。

成功响应中的 `result.goodsId` 只确认 Temu 已创建商品记录，不代表商品已经发布、审核通过或可售。工作台必须保留提交为待查询状态。

### 3.4 查询官方状态

官方合约建议创建后约 600 秒再查询。状态请求使用 `temu.local.goods.list.retrieve`，并把创建返回的标识作为数组传入：

```json
{
  "goodsIdList": ["<goodsId>"]
}
```

只记录并展示官方响应，不把请求成功、`goodsId`、`Draft` 或 `Incomplete` 自行解释为已发布。最终状态以目标店铺的官方查询结果为准。

## 4. 错误恢复

- **缺少凭据或签名提供器：** 这是预期的本地安全拒绝。补齐官方授权、本地凭据和批准的签名提供器后，重新生成并核对预览；不要把密钥贴入界面或日志。
- **预览哈希不匹配：** 说明预览已变化或确认对象不一致。停止提交，重新生成预览，逐项复核后使用新哈希确认。
- **站点字段或币种被拒绝：** 根据官方错误响应修正类目、属性、币种、价格精度、图片或包装字段，再生成新预览；不要假设其他站点的规则相同。
- **网络或 Temu API 错误：** 保留可读的失败记录和既有外部标识，核对官方错误码后再重试。不要新建标识或重复提交来规避幂等记录。
- **已取得 `goodsId` 但状态未定：** 不要再次发布同一商品。等待建议查询时间后，用 `goodsIdList` 查询并记录官方状态。

如果官方接口字段、签名要求或店铺权限发生变化，应先更新并复核 `official-api-contract.json`，再修改实现；文档不得承诺尚未通过官方资料和测试验证的行为。

## 5. 密钥与本地状态边界

- Temu 不复用 Ozon 凭据，Ozon 也不读取 Temu 凭据。
- 凭据仅从本机运行时配置读取；预览、日志、异常、诊断和版本库均不得出现密钥。
- `state/config/temu_credentials.json`、`state/temu_idempotency.json` 及工作台运行记录属于本机状态，不应提交到 Git。
- `/.inception/` 是 DreamGraph 的本地执行状态，已由仓库根目录 `.gitignore` 排除。
- 自动化测试不得调用真实 Temu 发布接口；本文档也不授权任何发布或上传。

更多字段限制、官方文档地址和已封存证据见 [official-api-contract.json](official-api-contract.json)。
