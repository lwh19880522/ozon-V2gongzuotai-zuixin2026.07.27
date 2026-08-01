# Ozon V2 工作台 (Ozon V2 Workbench)

本仓库包含 Ozon V2 工作台、本地浏览器桥接、字段草稿 Skill、专用生图
Skill、安装脚本和自动化测试。

仓库地址：<https://github.com/lwh19880522/ozon-V2gongzuotai-zuixin2026.07.27>

- [详细安装说明](docs/INSTALLATION.md)
- [完整使用流程](docs/USER_GUIDE.md)
- [干净发布与隐私边界](docs/RELEASE_AND_PRIVACY.md)

## 组件

| 组件 | 用途 | 安装结果 |
| --- | --- | --- |
| 本地工作台 | 批次、采集审核、SKU 主体、价格、字段草稿与建品 | `http://127.0.0.1:8765/` |
| Edge 扩展 | 受管浏览器采集桥接 | 加载 `browser_extension/ozon_v2_bridge` |
| 字段 Skill | 根据已锁定证据补全字段 | 安装到 Codex Skills |
| Ozon 专用生图 Skill | 单线程锁定主体、生成图库和视频并更新 Ozon | 安装到 Codex Skills |

主流程：

```text
安装并启动 → 店铺授权 → 创建批次 → Ozon/1688 采集
→ 锁定真实 SKU 与主体 → 智能字段草稿 → 逐商品建品
→ 写入 image_tasks/pending → 专用生图 Skill 单线程更新商品媒体
```

## 安装和启动

运行环境：Windows、PowerShell 5.1+、Python 3.11+。

双击：

```text
安装并启动 Ozon V2.cmd
```

或运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ozon_v2.ps1
```

完整安装必须同时通过本地健康接口、桌面启动入口、运行依赖和 Skill
版本一致性检查。只安装 Skill 不代表工作台安装完成。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_ozon_v2_install.ps1
```

## 专用生图契约

- 用户侧只有一个生图入口：`ozon-product-media-generator`。
- 严格单线程，一次完整处理一件商品，再领取下一件。
- Codex 自动启动本地只读媒体服务和免费的 Cloudflare Quick Tunnel；用户
  不需要提供 bucket、域名、账号、Token 或公网地址。
- 每件商品先根据已锁定 1688 SKU 证据生成一张白底主体图，校验后冻结
  路径和 SHA-256。
- 成品生成只允许使用这同一张白底主体图作为唯一图片参考，同时结合固定
  提示词一次生成 4×2 八宫格。
- 八宫格按行优先裁成 8 张严格 3:4 图片，不拉伸、不重排。任何主体外形、
  颜色、数量、结构、零件、配件、印花或规格变化都必须拒绝。
- 只返修失败槽位，仍使用同一张冻结白底主体图作为唯一图片参考。
- 8 张图片通过验收后，本地生成滚动视频和封面，经自动公网通道提交到
  同一个 Ozon 商品；任务结果写入独立图片任务区，不回传工作台。

Cloudflare Quick Tunnel 是匿名临时通道。专用 Skill 会在批次开始前启动，
在所有 Ozon 媒体提交完成后关闭。

## 生命周期控制

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Restart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\workbench_control.ps1 -Action Stop
```

## 验证

```powershell
python -m pytest -q
```

测试不得触发真实 Ozon/1688 采集、真实上传或真实生图。

## 隐私与发布

店铺凭证、浏览器资料、运行数据库、采集证据、生成媒体、临时公网通道
状态、日志、本机用户名和绝对路径都不得进入发布包。

```powershell
python .\scripts\build_clean_release.py --destination .\.release\ozon-v2
python .\scripts\build_clean_release.py --scan-only .\.release\ozon-v2
```
