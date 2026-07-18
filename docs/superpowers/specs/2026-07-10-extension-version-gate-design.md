# Ozon V2 扩展版本门禁设计 (Extension Version Gate Design)

日期 (Date): 2026-07-10

状态 (Status): 已确认，可进入实现 (Approved For Implementation)

## 目标 (Goal)

防止工作台在 Edge 仍运行旧版 Ozon V2 Browser Bridge 时创建新批次。用户必须看到当前加载版本、项目要求版本和明确处理提示；浏览器桥接离线或版本不一致时，新建批次必须在页面与 HTTP 接口两层被阻止。

## 范围 (Scope)

- 只修改 Ozon V2 新工具台、浏览器桥接状态和本地 HTTP 门禁。
- 不修改旧插件，不修改 Ozon 采集、海外店证据、种子池、生图或上传规则。
- 不实现扩展自动更新；开发阶段仍由用户在 `edge://extensions` 重新加载解压缩扩展。

## 版本来源 (Version Source)

- 项目要求版本以 `browser_extension/ozon_v2_bridge/manifest.json` 的 `version` 为唯一来源。
- 服务端不得另存一份手写版本常量，避免版本漂移。
- 浏览器心跳继续上报实际加载的 `extension_version`。

## 服务端门禁 (Server Gate)

`GET /api/browser-bridge/status` 在现有字段之外返回：

- `loaded_extension_version`: 最近有效桥接心跳的扩展版本。
- `required_extension_version`: 清单要求版本。
- `version_ready`: 两者完全一致且桥接在线时为 `true`。
- `readiness_code`: `ready`、`offline`、`version_mismatch` 或 `version_unknown`。

`POST /api/batches` 创建批次前执行硬门禁：

- 桥接离线：返回 `browser_bridge.offline`，不创建运行目录，不抽种子。
- 版本未知或不一致：返回 `browser_bridge.update_required`，不创建运行目录，不抽种子。
- 在线且版本一致：保持现有批次创建与自动执行流程。

## 失效页面心跳 (Invalidated Page Heartbeat)

- 扩展重载后，旧工作台内容脚本可能持续发送 `Extension context invalidated`。
- 这类旧版本失败心跳只用于诊断，不得覆盖最近的兼容版本后台心跳。
- 若没有兼容版本心跳，状态仍应显示版本不一致或离线，不能误报已就绪。
- 版本就绪只依赖 20 秒内的兼容版本心跳，不永久缓存旧的成功状态。

## 工具台界面 (Workbench UI)

浏览器桥接区域新增：

- `扩展版本 (Extension Version)`: `已加载 0.1.x / 要求 0.1.y`。
- `版本状态 (Version Status)`: 已就绪、需要更新、离线或未知。

交互规则：

- 页面加载时“开始自动执行”默认禁用，完成桥接状态检查后再决定是否启用。
- 版本不一致时显示醒目的中文在前、英文括号在后的提示，并给出“在 Edge 扩展页重新加载，然后刷新工作台”的动作说明。
- 前端禁用只改善体验；真正边界仍由服务端硬门禁保证。

## 数据与安全 (Data And Security)

- 不记录浏览器 Cookie、店铺 API Key 或页面隐私数据。
- 状态文件只保存桥接来源、版本、阶段、时间和既有诊断字段。
- 新电脑必须使用本机扩展心跳和本机 `127.0.0.1:8765`，不得复用另一台电脑的就绪状态。

## 测试 (Tests)

- 清单版本能被服务端正确读取。
- 无心跳、过期心跳、未知版本和旧版本均不能创建批次。
- 当前版本且在线时可以创建批次。
- 旧页面的失效心跳不能覆盖兼容版本后台心跳。
- 兼容心跳超过 20 秒后不再视为就绪。
- 首页包含中英文版本信息，并根据 `version_ready` 禁用或启用开始按钮。
- 全量 Python 测试、浏览器后台行为测试和扩展脚本语法检查保持通过。

## 完成标准 (Acceptance Criteria)

1. Edge 运行 `0.1.9`、项目要求 `0.1.11` 时，工作台明确显示版本不一致并拒绝创建批次。
2. Edge 重新加载到要求版本并产生新心跳后，无需修改运行数据即可启用新建批次。
3. 旧失效页面继续报错时，不会把已经就绪的新扩展状态改回旧版本。
4. 被拒绝的新建请求不抽种子、不产生批次目录、不消耗种子池。
