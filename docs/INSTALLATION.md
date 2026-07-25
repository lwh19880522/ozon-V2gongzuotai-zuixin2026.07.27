# Ozon V2 完整安装说明

本文安装的是一个整体产品：本地工作台、Edge 采集桥接扩展、Ozon V2 MCP、字段 Skill、生图总控 Skill 和商品媒体 Skill。安装过程不会自动采集、生成图片或上传商品。

## 1. 环境要求

- Windows 10/11 64 位。
- PowerShell 5.1 或更高版本。
- Python 3.11 或更高版本，并在安装时勾选加入 `PATH`。
- Microsoft Edge。
- Git。
- Codex Desktop。字段和生图流程需要在本仓库工作区中使用。

Ozon Seller API 的 `Client-Id` 和 `Api-Key` 属于店铺私密信息。不要写入 Git、截图、文档或任务包。

## 2. 获取项目

HTTPS：

```powershell
git clone https://github.com/lwh19880522/OZON-gongjutai-2026.07.18.git
cd OZON-gongjutai-2026.07.18
```

如果本机的 GitHub HTTPS 路由不可用，可改用已配置的 SSH：

```powershell
git clone git@github.com:lwh19880522/OZON-gongjutai-2026.07.18.git
cd OZON-gongjutai-2026.07.18
```

项目路径可以包含中文和空格。安装脚本只使用仓库相对路径，不依赖某台电脑上的固定盘符。

## 3. 一键安装

在仓库根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1
```

脚本会：

1. 检查 Python 3.11+。
2. 创建或复用仓库内的 `.venv`。
3. 安装工作台、Pillow、FastMCP 和项目代码。
4. 创建桌面“`Ozon V2 工具台`”快捷方式。
5. 启动本地服务 `http://127.0.0.1:8765/`。

安装器可重复执行；更新代码后再次执行即可刷新依赖。常用选项：

```powershell
# 只安装，不启动
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1 -NoStart

# 不创建桌面快捷方式
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1 -NoShortcut

# 只检查将执行的步骤，不修改电脑
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1 -DryRun -NoStart -NoShortcut
```

## 4. 安装 Edge 扩展

1. 在 Edge 打开 `edge://extensions/`。
2. 打开“开发人员模式”。
3. 点击“加载解压缩的扩展”。
4. 选择仓库中的：

   ```text
   browser_extension\ozon_v2_bridge
   ```

5. 刷新工具台，确认右上角显示扩展已连接、版本已就绪。

更新扩展代码后，在 `edge://extensions/` 点击该扩展的“重新加载”，再刷新工具台。不要同时加载仓库的多个旧副本。

## 5. 启用 Codex 插件和 Skills

仓库根目录本身就是 Codex 插件：

- 清单：`.codex-plugin/plugin.json`
- MCP：`.mcp.json`
- 字段 Skill：`skills/ozon-intelligent-field-drafter/`
- 生图总控 Skill：`skills/ozon-image-generation-controller/`
- 单品媒体 Skill：`skills/ozon-product-media-generator/`

在 Codex Desktop 中把本仓库作为工作区打开，并将该仓库作为本地插件源安装或刷新。插件刷新后新建一个 Codex 任务，使新版本的 MCP 与 Skills 生效。若公司环境通过本地 marketplace 管理插件，安装名为：

```text
ozon-v2-ops-controller
```

该插件依赖仓库内的 `.venv\Scripts\python.exe`，所以必须先完成第 3 步。不要只复制单个 Skill 文件夹；工作台 API、字段 Skill、生图 Skill 和 MCP 必须来自同一个提交版本。

## 6. 配置店铺授权

1. 启动工作台。
2. 打开左侧“店铺授权”。
3. 输入 Ozon Seller API `Client-Id` 和 `Api-Key`。
4. 保存后执行授权检查。

凭据只保存在本机运行目录，不进入仓库。浏览器扩展不应包含 Seller API 密钥。

## 7. 启动、停止和重启

桌面快捷方式用于日常启动。命令行控制：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Restart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Stop
```

服务只监听 `127.0.0.1`，默认端口为 `8765`。

## 8. 更新

先停止工作台，再更新并重装：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Stop
git pull --ff-only
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1
```

更新后重新加载 Edge 扩展，并在 Codex 中刷新本地插件/新建任务。

## 9. 验证安装

离线验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
python C:\Users\<用户名>\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py .
```

测试不会触发真实采集、生图、上传或最终审批。

## 10. 日志与故障排查

默认日志目录：

```text
runtime\workbench\
```

重点文件：

- `lifecycle.log`：启动、停止和重启。
- `server.stdout.log`：服务标准输出。
- `server.stderr.log`：服务错误。
- `launcher.log`：桌面入口启动结果。
- `workbench.json`：当前服务 PID、端口和状态。

常见问题：

- `PYTHON_NOT_FOUND`：安装 Python 3.11+，重新打开 PowerShell。
- 扩展离线：在 Edge 重新加载唯一的项目扩展，并刷新 1688/Ozon 标签页。
- MCP 启动失败：重新执行安装脚本，确认 `.venv\Scripts\python.exe` 存在。
- 页面打开但数据不更新：先用 `Status` 检查服务，再看扩展版本门禁。
- 端口被占用：停止旧服务，或使用 `-Port <端口>` 启动。
