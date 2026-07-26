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

## 3. 新电脑一键安装

下载或克隆完整仓库后，直接双击仓库根目录：

```text
安装并启动 Ozon V2.cmd
```

这是默认安装入口。它会启动下面的 PowerShell 安装器；如果安装失败，窗口会保留错误信息，不会把“仅 Skill 安装成功”显示成整套安装成功。

命令行执行方式：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1
```

脚本会：

1. 检查 Python 3.11+。
2. 创建或复用仓库内的 `.venv`。
3. 安装工作台、Pillow、FastMCP 和项目代码。
4. 将仓库同版本的字段 Skill、生图总控 Skill 和单品媒体 Skill 安装到当前 Windows 用户的 `.codex\skills`。
5. 创建桌面“`Ozon V2 工具台`”快捷方式。
6. 启动并打开本地服务 `http://127.0.0.1:8765/`。
7. 严格验证虚拟环境、运行依赖、3 个 Skill 的文件版本、桌面快捷方式和 `/api/health`。

只有第 7 步输出 `DOCTOR_PASSED` 后，才算完整安装成功。只复制 3 个 Skill 不算安装完成，也不能用 Ozon 官方卖家后台网址代替本地工具台。

安装器可重复执行；更新代码后再次执行即可刷新依赖。常用选项：

```powershell
# 只安装，不启动
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1 -NoStart

# 不创建桌面快捷方式
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1 -NoShortcut

# 安装并启动服务，但不自动打开浏览器
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1 -NoOpen

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

一键安装器已经将 3 个 Skill 安装到当前用户目录。在 Codex Desktop 中仍需把本仓库作为工作区打开，使仓库内 `.mcp.json` 与工作台服务共同生效。插件刷新后新建一个 Codex 任务。若公司环境通过本地 marketplace 管理插件，安装名为：

```text
ozon-v2-ops-controller
```

该插件依赖仓库内的 `.venv\Scripts\python.exe`，所以必须先完成第 3 步。不要只复制单个 Skill 文件夹；工作台 API、字段 Skill、生图 Skill 和 MCP 必须来自同一个提交版本。看到 Skill 存在不能报告“安装完成”，必须继续检查本地 `/api/health` 与桌面快捷方式。

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

完整安装验证：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_ozon_v2_install.ps1
```

验证器只有在以下项目全部成功时才输出 `DOCTOR_PASSED`：

- 仓库本体和工作台服务入口存在。
- `.venv` 与 Python 运行依赖可用。
- 当前用户安装的 3 个 Skill 与仓库版本一致。
- 桌面“`Ozon V2 工具台`”快捷方式指向当前仓库。
- `http://127.0.0.1:8765/api/health` 返回真实健康状态。

开发者代码回归（普通使用者不需要执行；先安装 `dev` 测试依赖）：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
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
- 只有 Skill、没有工作台：说明只复制了 `.codex\skills`；返回完整仓库，双击“`安装并启动 Ozon V2.cmd`”。
- Codex 显示 `doctor_passed` 但没有 `127.0.0.1:8765`：该检查不是本仓库的严格 Doctor，重新运行 `scripts\verify_ozon_v2_install.ps1`。
- 扩展离线：在 Edge 重新加载唯一的项目扩展，并刷新 1688/Ozon 标签页。
- MCP 启动失败：重新执行安装脚本，确认 `.venv\Scripts\python.exe` 存在。
- 页面打开但数据不更新：先用 `Status` 检查服务，再看扩展版本门禁。
- 端口被占用：停止旧服务，或使用 `-Port <端口>` 启动。
