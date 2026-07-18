# ADR-0006 店铺授权绑定 (Store Authorization Binding)

日期 (Date): 2026-07-09

状态 (Status): Accepted

## 决策 (Decision)

Ozon V2 的第一道门禁不叫单纯的检查凭证 (Credential Check)，而是店铺授权绑定 (Store Authorization Binding)。

用户可以在工具台主动输入:

- 店铺 ID (Client ID)
- 密钥 (API Key)

如果用户不更换店铺，就沿用历史绑定。工具台只显示店铺 ID 和脱敏密钥状态，不回显真实 API Key。

## 规则 (Rules)

- 没有历史绑定时，必须同时提供店铺 ID 和 API Key。
- 已有历史绑定且店铺 ID 不变时，API Key 可以留空，系统沿用历史密钥。
- 更换店铺 ID 时，必须重新提供 API Key。
- 店铺授权绑定完成后，才允许刷新店铺已有商品去重。
- Obsidian 日志、API 响应、工具台页面都不能写出真实 API Key。

## 后果 (Consequences)

- 用户首次安装时只需要绑定一次店铺。
- 用户不更换店铺时，后续批次默认使用历史店铺授权。
- 工具台不再把第一步设计成被动弹窗，而是一个明确、可见、可复用的授权绑定入口。
