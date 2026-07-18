---
title: 浏览器桥接 (Browser Bridge)
date: 2026-07-09
status: accepted
tags:
  - OzonV2/浏览器桥接-BrowserBridge
  - OzonV2/工具台-Workbench
  - OzonV2/采集架构-CollectionArchitecture
---

# 浏览器桥接 (Browser Bridge)

## 定义 (Definition)

浏览器桥接 (Browser Bridge) 是一套让真实浏览器执行网页采集任务、再把结果回写到本地工具台的连接机制。

它不是让智能体直接控制所有网页路径，也不是让后端脚本直接伪装浏览器抓网页。它的核心是：

```text
工具台 (Workbench) 发任务合同
浏览器扩展 (Browser Extension) 在真实浏览器页面执行
内容脚本 (Content Script) 采集页面证据
本地服务 (Local Server) 接收结果并推进状态机
```

## 为什么需要 (Why)

Ozon、1688、Yandex 等页面经常有动态渲染、代理要求、登录态、风控、滑块和真实浏览器环境差异。

如果只让智能体或后端 Playwright 自己访问，容易出现：

- 代理没有按站点正确切换。
- 页面被风控或空白。
- 智能体绕过架构路径，跳过门禁。
- 采集结果没有实时回写工具台。
- 用户看不到当前卡在哪里。

浏览器桥接的价值是：让网页动作发生在用户真实浏览器里，但流程控制仍然归工具台和状态机管。

## 架构 (Architecture)

```text
Workbench UI
  - 显示批次状态
  - 显示浏览器桥接心跳
  - 创建/继续自动执行

Local Server
  - 暴露 browser-task 接口
  - 暴露 heartbeat 接口
  - 暴露 ingest 回写接口
  - 校验结果并推进状态机

Browser Extension
  - 读取当前工具台批次任务
  - 复用真实浏览器页面
  - 不主动乱开多个标签页
  - 只执行当前任务合同

Content Script
  - 注入目标网页
  - 等待页面加载和跳转稳定
  - 采集 DOM 证据
  - 回写到本地服务
```

## 工作流程 (Workflow)

1. 用户在工具台创建批次。
2. 工具台自动推进：凭证检查、店铺去重、抽种子、生成俄语查询词。
3. 状态机生成浏览器任务合同。
4. 浏览器扩展读取当前任务。
5. 用户点击一次扩展，或扩展复用已存在目标页面。
6. 内容脚本在真实网页里执行采集。
7. 采集结果通过本地接口回写。
8. 本地服务校验结果。
9. 校验通过才推进下一步；缺少关键证据则停在门禁。

## 心跳字段 (Heartbeat Fields)

工具台里的浏览器桥接面板显示的是扩展和工具台之间的实时连接状态。

```text
连接 (Connection)
  是否在线，以及由哪个脚本上报。

阶段 (Stage)
  当前批次 ID、任务类型、任务阶段。

心跳 (Heartbeat)
  最近一次扩展上报时间。
```

常见来源：

```text
workbench_content_script
  工具台页面里的内容脚本，负责登记当前批次任务。

ozon_content_script
  Ozon 页面里的内容脚本，负责页面采集和结果回写。

background_interval
  扩展后台轮询，负责保持状态，但不应该主动乱开新标签页。
```

## 能做什么 (Capabilities)

浏览器桥接适合做：

- Ozon 公开页采集。
- Ozon 商品详情页证据采集。
- Ozon 图片、标题、价格、评分、评论、类目路径采集。
- 公开属性证据采集。
- 1688 以图搜款过程采集。
- 需要真实浏览器环境的页面检查。
- 需要用户偶尔处理滑块或风控的半自动流程。

## 不能做什么 (Limits)

浏览器桥接不能替代业务规则。

特别注意：

- 它不能把 Ozon 公开页文字当成上传属性模板。
- 它不能决定哪些字段可以复制、哪些字段必须重写。
- 它不能绕过 Seller API 的真实类目属性模板。
- 它不能跳过店铺去重、种子剔除、同款判断这些门禁。

正确边界是：

```text
公开网页 = 证据来源
Seller API = 上传模板来源
Domain 规则 = 是否允许继续的判断来源
```

## Ozon V2 当前规则 (Current Ozon V2 Rules)

Ozon V2 里，浏览器桥接只负责采公开页证据。

真实上传字段必须来自：

```text
Ozon Seller API description-category attribute template
```

也就是必须拿到：

- `description_category_id`
- `type_id`
- `attribute_id`
- 字段名
- 字段类型
- 是否必填
- 可选值或字典 ID
- 单位

没有这些真实模板字段，流程不能进入后续上传。

## 复用模式 (Reusable Pattern)

以后其他插件也可以照这个结构做：

```text
1. Workbench 生成任务合同
2. Browser Extension 读取当前任务
3. Content Script 在真实网页执行
4. Local Server 接收回写
5. Validator 校验结果
6. State Machine 决定是否推进
```

关键原则：

- 工具台只发合同，不写复杂网页逻辑。
- 扩展只执行当前合同，不自己决定业务流程。
- 内容脚本只采证据，不做最终业务判断。
- 本地服务只接受结构正确、来源可信、门禁满足的结果。
- 状态机是唯一流程入口，不能让智能体绕路径。

## 防乱跑规则 (Guardrails)

浏览器桥接必须遵守：

- 后台轮询不能主动无限打开新标签页。
- 一个批次只能登记一个当前浏览器任务。
- 没有工具台当前任务时，不执行旧任务 fallback。
- 任务完成后必须清理当前任务状态。
- 结果回写必须带 `run_id` 和任务来源。
- 服务端必须再次校验，不信任前端采集结果。

## 一句话总结 (Summary)

浏览器桥接 (Browser Bridge) 的本质是：

```text
把网页操作放回真实浏览器，把流程控制留在工具台，把业务判断交给状态机和校验器。
```
