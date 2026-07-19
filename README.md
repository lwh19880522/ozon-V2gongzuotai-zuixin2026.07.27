# Ozon V2 工具台 (Ozon V2 Workbench)

本仓库包含 Ozon V2 工具台的完整可复现程序本体：本地工具台服务、浏览器扩展、MCP 控制器、采集与生图队列、Codex 技能、Windows 一键启动脚本、测试和设计文档。

## 一键启动 (One-click launch)

运行环境：Windows、PowerShell 5.1+、Python 3.11+。

首次在当前目录创建桌面入口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\create_workbench_shortcut.ps1
```

之后双击桌面的“`Ozon V2 工具台`”即可启动服务并打开：

```text
http://127.0.0.1:8765/
```

工具台右上角的一体化运行胶囊提供真实服务、Edge 扩展和当前任务状态，并支持快速重启、停止和诊断。

## 生命周期控制 (Lifecycle control)

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Restart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Stop
```

运行状态、浏览器配置、日志、采集证据和生成图片只保存在本机，不进入 Git 仓库。

## 生图并发契约 (Image worker contract)

- 可用生图身份为 `ozon_image_worker_01` 至 `ozon_image_worker_05`，分别映射队列 worker ID `ozon-image-worker-01` 至 `ozon-image-worker-05`。
- 总控通过 `spawn_agent` 按待处理商品数和当前空闲并发位动态启用 0–5 个内部子智能体；可用几个就使用几个，只增补空位、不缩减现有池，不因少于 5 个而停止。
- 常规生图最多使用 5 个子智能体；运行时提供第 6 个子智能体位置时，将其保留给失败恢复、诊断或人工介入。不得使用 `create_thread` 或创建用户可见的侧边栏任务。
- 生图队列按整件商品原子领取，保留租约、续租、停止、恢复和结果回写边界。

## 验证 (Verification)

```powershell
python -m pytest -q
```

测试不应触发真实 Ozon/1688 采集、真实上传或真实生图。

## 目录 (Structure)

- `src/ozon_v2/`：领域、服务、适配器和本地工具台。
- `browser_extension/`：Edge 浏览器桥接扩展。
- `scripts/`：启动、停止、重启、快捷方式和 worker 工具。
- `skills/`：Ozon V2 总控与商品媒体执行技能。
- `mcp/`：FastMCP 入口。
- `tests/`：自动化测试。
- `docs/`：契约、设计和实施计划。
- `assets/`：版本化种子池等静态资产。
