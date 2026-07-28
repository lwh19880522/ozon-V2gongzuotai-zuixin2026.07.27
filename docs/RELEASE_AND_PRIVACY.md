# Ozon V2 干净发布与隐私边界

本项目使用“显式发布白名单 + 成品隐私扫描 + 逐文件哈希清单”。发布包不是旧仓库历史的镜像，也不会复制整个开发工作区。

## 发布包包含什么

- 本地工具台源代码与 MCP 入口。
- Windows 一键安装、启动、停止、重启和 Doctor 脚本。
- Edge 采集桥接扩展。
- `ozon-intelligent-field-drafter`、`ozon-image-generation-controller`、`ozon-product-media-generator` 三个完整 Skill。
- 版本化 5000 条种子池静态资产。
- 安装说明、完整使用流程、公开架构契约和自动化测试。
- `RELEASE_MANIFEST.json`：仓库地址、文件数量和所有发布文件的 SHA-256。

## 永远不进入仓库的内容

- Seller API 的 `Client-Id`、`Api-Key`、访问令牌或任何店铺凭证。
- Cloudflare R2 的账户 ID、访问密钥、Secret、bucket 私有配置和用户公网域名。
- 浏览器用户目录、Cookies、会话、下载、缓存和扩展本地状态。
- 运行数据库、批次状态、采集证据、商品草稿、生成图片、视频、日志和诊断包。
- `.env`、私钥、证书、SQLite/DB 文件。
- 本机用户名、聊天软件临时路径、Obsidian 私有记录和开发机绝对路径。
- 旧 Git 历史、旧分支、旧 Pull Request 引用和历史工作记录。

这些运行数据默认位于仓库同级的 `OzonOpsV2` 目录或 Git 已忽略的本地目录中。安装器只在本机创建它们。

## 生成干净发布包

在仓库根目录执行：

```powershell
python .\scripts\build_clean_release.py --destination .\.release\ozon-v2
```

目标目录必须为空。构建器只复制固定白名单，不复制 `.git`、`.venv`、运行目录、临时目录、历史设计记录或 Obsidian 文件。构建完成后会自动扫描成品；发现隐私或密钥特征时返回非零状态并打印 `PRIVACY_SCAN_FAILED`。

单独复核已有目录：

```powershell
python .\scripts\build_clean_release.py --scan-only .\.release\ozon-v2
```

成功标志：

```text
PRIVACY_SCAN_PASSED
```

## 发布前完整检查

```powershell
python -m pytest -q
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1 -DryRun -NoStart -NoShortcut
python .\scripts\build_clean_release.py --scan-only .
```

最后一条应在“干净发布目录”中执行，而不是在包含个人运行数据的开发工作区中执行。

## 新电脑复核

1. 从仓库默认分支下载或克隆项目。
2. 确认根目录存在 `安装并启动 Ozon V2.cmd`、`启动 Ozon V2.cmd`、`skills`、`browser_extension` 和 `scripts`。
3. 双击 `安装并启动 Ozon V2.cmd`。
4. 运行 `scripts\verify_ozon_v2_install.ps1`，确认本地健康接口、虚拟环境、三个 Skill 和桌面入口全部通过。
5. 店铺凭证和 R2 配置只能在新电脑本地录入，不要提交回 Git。

## 发现隐私泄漏时

不要只删除当前文件后继续使用旧历史。应先撤销并轮换已暴露的密钥，再从干净白名单重新构建一个无旧历史的仓库。任何已经公开过的密钥都必须视为失效。
