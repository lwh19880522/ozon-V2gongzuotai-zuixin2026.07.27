# Ozon V2 工具台一键启动与一体化运行控制实施计划 (One-click Workbench Launcher And Integrated Runtime Control Implementation Plan)

> **执行说明 (Execution Note):** 本计划在当前线程内按测试驱动方式执行。只修改 Ozon V2；验证不打开 Edge、不启动真实采集、不调用真实生图。

**目标 (Goal):** 修复陈旧 PID 导致的启动阻塞，提供桌面单入口，并在所有工具台页面挂载一个可移动的“服务 / 扩展 / 任务”一体化运行控制条。

**架构 (Architecture):** PowerShell 生命周期脚本只负责短生命周期的 Start/Status/Stop/Restart；权威运行文件迁入项目内 `runtime/workbench/`。Python 服务提供真实运行状态以及受控的停止/重启 API。所有 HTML 响应在发送前统一注入一个独立的悬浮控制条，避免逐页复制和改动业务页面。

**技术栈 (Tech Stack):** Python 3.11、`ThreadingHTTPServer`、PowerShell 5.1、原生 HTML/CSS/JavaScript、pytest/unittest。

---

## 任务 1：锁定生命周期契约 (Lifecycle Contract)

**文件：**

- 新增：`tests/test_workbench_control.py`
- 修改：`scripts/workbench_control.ps1`

### 1.1 先写失败测试

新增一个仅使用随机本地端口和临时运行目录的集成测试，覆盖：

```python
def test_start_is_idempotent_and_stale_record_does_not_block(self):
    stale = {"pid": os.getpid(), "port": self.port, "project_root": "C:/wrong-project"}
    self.state_file.write_text(json.dumps(stale), encoding="utf-8")
    first = self.run_control("Start")
    first_pid = json.loads(self.state_file.read_text(encoding="utf-8"))["pid"]
    second = self.run_control("Start")
    second_pid = json.loads(self.state_file.read_text(encoding="utf-8"))["pid"]
    self.assertIn("STARTED", first.stdout)
    self.assertIn("ALREADY_RUNNING", second.stdout)
    self.assertEqual(first_pid, second_pid)
```

再覆盖健康服务停止、重复停止幂等、错误 PID 不得误杀当前测试进程。

### 1.2 运行测试，确认先失败

运行：

```powershell
python -m pytest tests/test_workbench_control.py -q
```

预期：失败，因为脚本还不支持项目内 JSON 状态、`-RuntimeState` 和安全的自停止。

### 1.3 最小实现生命周期脚本

把 `scripts/workbench_control.ps1` 改为：

- `ValidateSet` 增加 `Restart`；
- 默认状态目录为 `$ProjectRoot\runtime\workbench`，并允许测试传入 `-RuntimeState`；
- 使用 `workbench.json`，内容至少包括 `pid`、`port`、`project_root`、`started_at`、`status`；
- 先看 `/api/health`，健康时复用服务；
- 陈旧或不匹配记录只记日志和替换，不终止其中的进程；
- 启动命令使用当前项目绝对路径 `scripts\start_workbench.py`，便于核验进程归属；
- 停止健康服务时优先调用 `/api/runtime/stop`；服务不健康时，仅当命令行同时匹配当前启动脚本、项目根和端口才允许终止；
- 日志写入 `runtime/workbench/lifecycle.log`；
- 状态文件先写临时文件，再在同一目录替换。

### 1.4 运行聚焦测试

运行：

```powershell
python -m pytest tests/test_workbench_control.py -q
```

预期：全部通过，且测试结束后随机端口已释放。

## 任务 2：实现可测试的停止与重启内核 (Runtime Control Core)

**文件：**

- 新增：`src/ozon_v2/workbench/runtime_control.py`
- 新增：`scripts/workbench_restart_helper.ps1`
- 新增：`tests/test_workbench_runtime_control.py`

### 2.1 先写失败测试

覆盖原子状态更新、停止只安排一次、重启助手命令包含当前 PID/端口/项目目录，以及未知动作被拒绝：

```python
controller = WorkbenchRuntimeController(project_root, runtime_dir, port=8765)
with patch("subprocess.Popen") as popen:
    result = controller.schedule("restart", fake_server)
    self.assertEqual("runtime.restart_scheduled", result["code"])
    command = popen.call_args.args[0]
    self.assertIn(str(project_root / "scripts" / "workbench_restart_helper.ps1"), command)
```

### 2.2 运行测试，确认先失败

运行：

```powershell
python -m pytest tests/test_workbench_runtime_control.py -q
```

预期：失败，因为模块和助手尚不存在。

### 2.3 实现运行控制器

`WorkbenchRuntimeController` 只承担以下职责：

```python
class WorkbenchRuntimeController:
    def mark_running(self, pid: int) -> None: ...
    def mark_stopped(self) -> None: ...
    def schedule(self, action: str, server: ThreadingHTTPServer) -> dict[str, Any]: ...
```

- `stop`：先更新 `stopping`，用短线程在 HTTP 响应发出后调用 `server.shutdown()`；
- `restart`：启动无窗口短生命周期 PowerShell 助手，再安排当前服务器关闭；
- 重复请求通过锁保持幂等；
- 重启助手等待旧 PID 退出，调用同一个 `workbench_control.ps1 -Action Start`，失败最多补试一次；
- 助手把结果写入 `restart-status.json`，不常驻、不打开浏览器。

### 2.4 运行聚焦测试

运行：

```powershell
python -m pytest tests/test_workbench_runtime_control.py -q
```

预期：全部通过。

## 任务 3：增加真实运行状态和操作 API (Runtime Status And Action API)

**文件：**

- 修改：`src/ozon_v2/workbench/local_server.py`
- 修改：`tests/test_workbench_local_server.py`

### 3.1 先写失败测试

增加：

```python
def test_runtime_status_reports_service_extension_and_task(self):
    result = self.get_json("/api/runtime/status")
    self.assertEqual("online", result["data"]["service"]["code"])
    self.assertEqual("ready", result["data"]["extension"]["code"])
    self.assertIn(result["data"]["task"]["code"], {"idle", "running", "blocked", "stopped", "failed"})
```

并注入假的 runtime controller，验证 `/api/runtime/stop` 和 `/api/runtime/restart` 只调用对应动作，不在测试中真实关闭或启动进程。

### 3.2 运行测试，确认先失败

运行：

```powershell
python -m pytest tests/test_workbench_local_server.py -q -k "runtime_status or runtime_stop or runtime_restart"
```

预期：404 或断言失败。

### 3.3 实现 API 与服务接线

- `create_handler` 增加可选 `runtime_controller` 注入；
- GET `/api/runtime/status` 聚合：当前服务、`bridge_readiness` 的真实心跳与版本、最新批次和 runner 状态；
- POST `/api/runtime/stop`、`/api/runtime/restart` 调用控制器；未配置控制器时明确返回 503；
- `make_server` 创建真实控制器；
- `main` 增加 `--runtime-dir`，启动时 `mark_running`，退出时 `mark_stopped`。

### 3.4 运行聚焦测试

运行：

```powershell
python -m pytest tests/test_workbench_local_server.py -q -k "runtime or home_page"
```

预期：全部通过。

## 任务 4：注入一体化可移动控制条 (Integrated Movable Runtime Capsule)

**文件：**

- 新增：`src/ozon_v2/workbench/runtime_capsule.py`
- 修改：`src/ozon_v2/workbench/local_server.py`
- 修改：`tests/test_workbench_local_server.py`

### 4.1 先写失败测试

对首页、批次审核、图片、上传和运营页逐一请求，断言每页恰好有一个控制条，并验证关键交互契约：

```python
for path in ["/", supplier_path, images_path, upload_path, "/diagnostics"]:
    body = self.get_text(path)
    self.assertEqual(1, body.count('id="ozonRuntimeCapsule"'))
    self.assertIn("ozon_v2_runtime_capsule_position", body)
    self.assertIn('/api/runtime/restart', body)
    self.assertIn('确认停止 (Confirm Stop)', body)
```

### 4.2 运行测试，确认先失败

运行：

```powershell
python -m pytest tests/test_workbench_local_server.py -q -k "runtime_capsule"
```

预期：失败，因为页面尚未注入控制条。

### 4.3 实现统一注入器

`runtime_capsule.py` 暴露 `inject_runtime_capsule(document: str) -> str`：

- 在 `</head>` 前加入局部命名 CSS；
- 在 `</body>` 前加入单个胶囊 DOM 和无依赖脚本；
- 石墨黑半透明表面、翡翠绿正常、琥珀等待、红色离线；
- 收起时同时显示服务、扩展、任务三点；
- 展开显示真实细节、重新检测、重启、停止、诊断；
- 重启按钮原位显示进度，等待健康接口经历离线再恢复后刷新；
- 停止按钮第一次变为“确认停止”，第二次才请求停止；
- 拖动后吸附左右边缘，坐标写入 `localStorage`，窗口变化时自动限制在可视区；
- 点击空白或 `Esc` 收起。

在 `_send_html` 内统一调用注入器，确保新页面自动继承控制条。

### 4.4 运行聚焦测试

运行：

```powershell
python -m pytest tests/test_workbench_local_server.py -q -k "runtime_capsule or home_page_loads or operations_pages"
```

预期：全部通过。

## 任务 5：增加桌面单入口 (Desktop Single Entry)

**文件：**

- 新增：`scripts/launch_workbench.ps1`
- 新增：`scripts/create_workbench_shortcut.ps1`
- 修改：`tests/test_workbench_control.py`

### 5.1 先写失败测试

静态验证启动器必须调用同一生命周期脚本，支持 `-NoOpen`，且不含临时 Edge 配置或强制加载扩展：

```python
launcher = (PROJECT_ROOT / "scripts" / "launch_workbench.ps1").read_text(encoding="utf-8")
self.assertIn("workbench_control.ps1", launcher)
self.assertIn("NoOpen", launcher)
self.assertNotIn("--user-data-dir", launcher)
self.assertNotIn("--load-extension", launcher)
```

再用 `-NoOpen` 运行启动器，断言服务健康且不会打开浏览器。

### 5.2 运行测试，确认先失败

运行：

```powershell
python -m pytest tests/test_workbench_control.py -q -k "launcher"
```

预期：失败，因为启动器尚不存在。

### 5.3 实现启动器与快捷方式创建脚本

- `launch_workbench.ps1` 调用 `workbench_control.ps1 -Action Start`；健康后仅用正常 Edge 打开 `http://127.0.0.1:8765/`；测试可传 `-NoOpen`；失败时显示简短错误和日志路径；
- `create_workbench_shortcut.ps1` 只创建一个 `Ozon V2 工具台.lnk`，目标为隐藏窗口 PowerShell 启动器；不安装、不复制项目、不设置开机启动；
- 创建快捷方式需要写桌面时单独请求系统授权。

### 5.4 运行聚焦测试

运行：

```powershell
python -m pytest tests/test_workbench_control.py -q
```

预期：全部通过。

## 任务 6：回归、实测和记录 (Regression, Smoke Test, And Record)

**文件：**

- 新增：`E:\obsidian仓库\萧机麦仓库\Ozon 工作台项目库 (Workbench Project)\2026-07-18 Ozon V2 工具台一键启动与一体化运行控制.md`

### 6.1 静态安全检查

运行：

```powershell
rg -n -- "--user-data-dir|--load-extension|imagegen|start_supplier_collection" scripts/launch_workbench.ps1 scripts/workbench_control.ps1 tests/test_workbench_control.py tests/test_workbench_runtime_control.py
```

预期：启动脚本不含临时 Edge 配置、强制扩展加载或生图调用。

### 6.2 完整测试

运行：

```powershell
python -m pytest -q
node tests/test_browser_workbench_content.js
```

预期：Python 和现有浏览器内容脚本测试全部通过。

### 6.3 无浏览器启动冒烟验证

使用随机端口和临时运行目录依次执行 Start、Status、Restart、Status、Stop；全部使用 `-NoOpen` 或直接生命周期脚本，不打开 Edge。验证 PID 更换、健康恢复和端口释放。

### 6.4 创建桌面入口

经系统授权运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/create_workbench_shortcut.ps1
```

验证桌面只新增一个 `Ozon V2 工具台.lnk`，不产生安装目录或常驻服务。

### 6.5 同步 Obsidian 记录

记录采用中文在前、英文括号在后，包含：

- 原因：旧外部 PID 状态导致启动阻塞；
- 结果：项目内状态、一键启动、一体化可移动控制条、真实扩展状态、快速停止/重启；
- 边界：无安装器、无 Windows 服务、无开机自启、无常驻控制器；
- 验证：未打开临时 Edge、未触发真实采集、未生成真实图片。

