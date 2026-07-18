from __future__ import annotations


RUNTIME_CAPSULE_STYLE = r"""
<style id="ozonRuntimeCapsuleStyle">
#ozonRuntimeCapsule {
  all: initial;
  position: fixed;
  top: 72px;
  right: 16px;
  z-index: 2147483000;
  width: 330px;
  color: #f4f7f5;
  font: 12px/1.45 "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
  letter-spacing: 0;
  filter: drop-shadow(0 14px 34px rgba(0, 0, 0, .28));
  user-select: none;
}
#ozonRuntimeCapsule, #ozonRuntimeCapsule * { box-sizing: border-box; }
#ozonRuntimeCapsule button, #ozonRuntimeCapsule a { font: inherit; }
#ozonRuntimeCapsule .ozon-runtime-shell {
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, .12);
  border-radius: 18px;
  background: rgba(20, 24, 23, .94);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .06);
  backdrop-filter: blur(18px) saturate(125%);
}
#ozonRuntimeCapsule .ozon-runtime-summary {
  width: 100%;
  min-height: 48px;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 8px 11px;
  border: 0;
  color: #f4f7f5;
  background: transparent;
  cursor: grab;
  text-align: left;
}
#ozonRuntimeCapsule.ozon-runtime-dragging .ozon-runtime-summary { cursor: grabbing; }
#ozonRuntimeCapsule .ozon-runtime-brand {
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(70, 236, 166, .35);
  border-radius: 10px;
  color: #69efb2;
  background: linear-gradient(145deg, rgba(33, 78, 59, .86), rgba(24, 35, 31, .92));
  font-size: 10px;
  font-weight: 800;
  letter-spacing: .08em;
}
#ozonRuntimeCapsule .ozon-runtime-summary-statuses {
  min-width: 0;
  flex: 1;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
#ozonRuntimeCapsule .ozon-runtime-summary-status {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 5px;
  color: #c5ccc8;
  white-space: nowrap;
}
#ozonRuntimeCapsule .ozon-runtime-summary-status span:last-child {
  overflow: hidden;
  text-overflow: ellipsis;
}
#ozonRuntimeCapsule .ozon-runtime-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: #7a817e;
  box-shadow: 0 0 0 3px rgba(122, 129, 126, .12);
}
#ozonRuntimeCapsule [data-state="ok"] .ozon-runtime-dot { background: #4ee6a1; box-shadow: 0 0 0 3px rgba(78, 230, 161, .13), 0 0 12px rgba(78, 230, 161, .4); }
#ozonRuntimeCapsule [data-state="warn"] .ozon-runtime-dot { background: #f5b849; box-shadow: 0 0 0 3px rgba(245, 184, 73, .13); }
#ozonRuntimeCapsule [data-state="bad"] .ozon-runtime-dot { background: #ff6b6b; box-shadow: 0 0 0 3px rgba(255, 107, 107, .13); }
#ozonRuntimeCapsule .ozon-runtime-chevron { color: #7f8984; font-size: 14px; transition: transform .18s ease; }
#ozonRuntimeCapsule.ozon-runtime-expanded .ozon-runtime-chevron { transform: rotate(180deg); }
#ozonRuntimeCapsule .ozon-runtime-panel {
  display: none;
  padding: 3px 12px 12px;
  border: 0;
  border-top: 1px solid rgba(255, 255, 255, .08);
  border-radius: 0;
  color: #f4f7f5;
  background: rgba(20, 24, 23, .96);
  box-shadow: none;
}
#ozonRuntimeCapsule.ozon-runtime-expanded .ozon-runtime-panel { display: block; }
#ozonRuntimeCapsule .ozon-runtime-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 1px 9px;
}
#ozonRuntimeCapsule .ozon-runtime-title-row strong { color: #f7faf8; font-size: 13px; font-weight: 680; }
#ozonRuntimeCapsule .ozon-runtime-title-row span { color: #7f8984; font-size: 10px; }
#ozonRuntimeCapsule .ozon-runtime-details {
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, .08);
  border-radius: 12px;
  background: rgba(255, 255, 255, .025);
}
#ozonRuntimeCapsule .ozon-runtime-detail {
  min-height: 48px;
  display: grid;
  grid-template-columns: 9px 92px minmax(0, 1fr);
  align-items: center;
  gap: 7px;
  padding: 8px 10px;
  border-bottom: 1px solid rgba(255, 255, 255, .065);
}
#ozonRuntimeCapsule .ozon-runtime-detail:last-child { border-bottom: 0; }
#ozonRuntimeCapsule .ozon-runtime-detail-label { color: #e8ece9; font-weight: 600; }
#ozonRuntimeCapsule .ozon-runtime-detail-value { min-width: 0; color: #929c97; overflow-wrap: anywhere; font-size: 10px; }
#ozonRuntimeCapsule .ozon-runtime-message { min-height: 28px; padding: 8px 2px 4px; color: #9aa39f; font-size: 10px; }
#ozonRuntimeCapsule .ozon-runtime-message[data-tone="bad"] { color: #ff8d8d; }
#ozonRuntimeCapsule .ozon-runtime-message[data-tone="ok"] { color: #69efb2; }
#ozonRuntimeCapsule .ozon-runtime-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
#ozonRuntimeCapsule .ozon-runtime-action {
  min-height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 9px;
  border: 1px solid rgba(255, 255, 255, .11);
  border-radius: 9px;
  color: #d8dedb;
  background: rgba(255, 255, 255, .05);
  text-decoration: none;
  cursor: pointer;
  transition: border-color .15s ease, background .15s ease, color .15s ease;
}
#ozonRuntimeCapsule .ozon-runtime-action:hover { border-color: rgba(105, 239, 178, .38); color: #fff; background: rgba(105, 239, 178, .08); }
#ozonRuntimeCapsule .ozon-runtime-action:disabled { opacity: .48; cursor: wait; }
#ozonRuntimeCapsule .ozon-runtime-action-primary { border-color: rgba(78, 230, 161, .25); color: #69efb2; background: rgba(78, 230, 161, .08); }
#ozonRuntimeCapsule .ozon-runtime-action-danger { color: #ff9a9a; }
#ozonRuntimeCapsule .ozon-runtime-action-danger[data-confirm="true"] { border-color: rgba(255, 107, 107, .55); color: #fff; background: rgba(190, 45, 45, .65); }
#ozonRuntimeCapsule.ozon-runtime-compact { width: 330px; }
@media (max-width: 520px) {
  #ozonRuntimeCapsule { width: min(330px, calc(100vw - 20px)); }
  #ozonRuntimeCapsule .ozon-runtime-summary-statuses { gap: 5px; }
  #ozonRuntimeCapsule .ozon-runtime-summary-status { font-size: 10px; }
}
@media (prefers-reduced-motion: reduce) {
  #ozonRuntimeCapsule *, #ozonRuntimeCapsule *::before, #ozonRuntimeCapsule *::after { transition: none !important; }
}
</style>
"""


RUNTIME_CAPSULE_BODY = r"""
<div id="ozonRuntimeCapsule" class="ozon-runtime-compact" aria-live="polite">
  <div class="ozon-runtime-shell">
    <button id="ozonRuntimeSummary" class="ozon-runtime-summary" type="button" aria-expanded="false" aria-controls="ozonRuntimePanel" title="拖动或展开运行控制 (Drag or Expand Runtime Control)">
      <span class="ozon-runtime-brand" aria-hidden="true">OZ</span>
      <span class="ozon-runtime-summary-statuses">
        <span id="ozonRuntimeServiceSummary" class="ozon-runtime-summary-status" data-state="warn"><span class="ozon-runtime-dot"></span><span>服务</span></span>
        <span id="ozonRuntimeExtensionSummary" class="ozon-runtime-summary-status" data-state="warn"><span class="ozon-runtime-dot"></span><span>扩展</span></span>
        <span id="ozonRuntimeTaskSummary" class="ozon-runtime-summary-status" data-state="warn"><span class="ozon-runtime-dot"></span><span>任务</span></span>
      </span>
      <span class="ozon-runtime-chevron" aria-hidden="true">⌄</span>
    </button>
    <section id="ozonRuntimePanel" class="ozon-runtime-panel" aria-label="工具台运行控制 (Workbench Runtime Control)">
      <div class="ozon-runtime-title-row"><strong>工具台运行状态 (Runtime)</strong><span>真实本地状态 (Live)</span></div>
      <div class="ozon-runtime-details">
        <div id="ozonRuntimeServiceDetail" class="ozon-runtime-detail" data-state="warn"><span class="ozon-runtime-dot"></span><span class="ozon-runtime-detail-label">服务 (Service)</span><span class="ozon-runtime-detail-value">正在检测 (Checking)</span></div>
        <div id="ozonRuntimeExtensionDetail" class="ozon-runtime-detail" data-state="warn"><span class="ozon-runtime-dot"></span><span class="ozon-runtime-detail-label">扩展 (Extension)</span><span class="ozon-runtime-detail-value">正在检测 (Checking)</span></div>
        <div id="ozonRuntimeTaskDetail" class="ozon-runtime-detail" data-state="warn"><span class="ozon-runtime-dot"></span><span class="ozon-runtime-detail-label">任务 (Task)</span><span class="ozon-runtime-detail-value">正在检测 (Checking)</span></div>
      </div>
      <div id="ozonRuntimeMessage" class="ozon-runtime-message">状态每 3 秒刷新 (Refreshes every 3 seconds)</div>
      <div class="ozon-runtime-actions">
        <button id="ozonRuntimeRefresh" class="ozon-runtime-action" type="button">重新检测 (Refresh)</button>
        <a class="ozon-runtime-action" href="/diagnostics">运行诊断 (Diagnostics)</a>
        <button id="ozonRuntimeRestart" class="ozon-runtime-action ozon-runtime-action-primary" type="button">重启工具台 (Restart)</button>
        <button id="ozonRuntimeStop" class="ozon-runtime-action ozon-runtime-action-danger" type="button">停止工具台 (Stop)</button>
      </div>
    </section>
  </div>
</div>
<script id="ozonRuntimeCapsuleScript">
(() => {
  "use strict";
  const root = document.getElementById("ozonRuntimeCapsule");
  if (!root || root.dataset.ready === "true") return;
  root.dataset.ready = "true";

  const positionKey = "ozon_v2_runtime_capsule_position";
  const summary = document.getElementById("ozonRuntimeSummary");
  const message = document.getElementById("ozonRuntimeMessage");
  const refreshButton = document.getElementById("ozonRuntimeRefresh");
  const restartButton = document.getElementById("ozonRuntimeRestart");
  const stopButton = document.getElementById("ozonRuntimeStop");
  let lastStatus = null;
  let stopConfirmTimer = null;
  let dragging = null;
  let moved = false;

  const labels = {
    service: "服务 (Service)",
    extension: "扩展 (Extension)",
    task: "任务 (Task)",
  };
  const summaryLabels = { service: "服务", extension: "扩展", task: "任务" };

  function toneFor(kind, code) {
    if (kind === "service") return code === "online" ? "ok" : "bad";
    if (kind === "extension") return code === "ready" ? "ok" : (code === "offline" ? "bad" : "warn");
    if (code === "running") return "ok";
    if (code === "failed") return "bad";
    if (code === "blocked") return "warn";
    return "idle";
  }

  function setStatus(kind, code, detail) {
    const title = labels[kind];
    const summaryElement = document.getElementById(`ozonRuntime${kind[0].toUpperCase()}${kind.slice(1)}Summary`);
    const detailElement = document.getElementById(`ozonRuntime${kind[0].toUpperCase()}${kind.slice(1)}Detail`);
    const tone = toneFor(kind, code);
    summaryElement.dataset.state = tone;
    summaryElement.querySelector("span:last-child").textContent = summaryLabels[kind];
    detailElement.dataset.state = tone;
    detailElement.querySelector(".ozon-runtime-detail-value").textContent = detail;
  }

  function extensionDetail(extension) {
    if (extension.code === "ready") {
      return `在线 · v${extension.loaded_extension_version || "?"} (Online)`;
    }
    if (extension.code === "version_mismatch") {
      return `版本不一致 ${extension.loaded_extension_version || "?"} → ${extension.required_extension_version || "?"}`;
    }
    if (extension.age_seconds != null) {
      return `离线 · 上次心跳 ${Math.round(extension.age_seconds)} 秒前 (Offline)`;
    }
    return "离线 · 未收到真实心跳 (Offline)";
  }

  function taskDetail(task) {
    const codeLabels = {
      idle: "空闲 (Idle)", running: "运行中 (Running)", blocked: "等待处理 (Blocked)",
      stopped: "已停止 (Stopped)", failed: "失败 (Failed)",
    };
    const suffix = task.run_id ? ` · ${task.run_id}` : "";
    return `${codeLabels[task.code] || task.code || "未知 (Unknown)"}${suffix}`;
  }

  function render(status) {
    lastStatus = status;
    const service = status.service || { code: "offline" };
    const extension = status.extension || { code: "offline" };
    const task = status.task || { code: "idle" };
    setStatus("service", service.code, service.code === "online" ? `正常 · PID ${service.pid} · 端口 ${service.port}` : "离线 (Offline)");
    setStatus("extension", extension.code, extensionDetail(extension));
    setStatus("task", task.code, taskDetail(task));
    message.dataset.tone = service.code === "online" ? "ok" : "bad";
    message.textContent = service.code === "online" ? "工具台服务正常 (Workbench Online)" : "工具台连接失败 (Connection Failed)";
  }

  async function fetchStatus() {
    const response = await fetch("/api/runtime/status", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.message || "Runtime status failed");
    return payload.data;
  }

  async function refresh() {
    refreshButton.disabled = true;
    try {
      render(await fetchStatus());
    } catch (error) {
      setStatus("service", "offline", "离线 (Offline)");
      setStatus("extension", "offline", "无法读取心跳 (Unavailable)");
      setStatus("task", "failed", "状态不可用 (Unavailable)");
      message.dataset.tone = "bad";
      message.textContent = error.message || "连接失败 (Connection Failed)";
    } finally {
      refreshButton.disabled = false;
    }
  }

  function clampPosition(x, y) {
    const margin = 10;
    const width = root.offsetWidth || 330;
    const height = root.offsetHeight || 48;
    return {
      x: Math.max(margin, Math.min(x, window.innerWidth - width - margin)),
      y: Math.max(margin, Math.min(y, window.innerHeight - height - margin)),
    };
  }

  function applyPosition(position, persist = false) {
    const next = clampPosition(position.x, position.y);
    root.style.left = `${next.x}px`;
    root.style.top = `${next.y}px`;
    root.style.right = "auto";
    if (persist) {
      try { localStorage.setItem(positionKey, JSON.stringify(next)); } catch (_) { /* local storage may be disabled */ }
    }
  }

  function restorePosition() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(positionKey) || "null"); } catch (_) { saved = null; }
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      applyPosition(saved);
    } else {
      requestAnimationFrame(() => applyPosition({ x: window.innerWidth - root.offsetWidth - 16, y: 72 }));
    }
  }

  function setExpanded(expanded) {
    root.classList.toggle("ozon-runtime-expanded", expanded);
    summary.setAttribute("aria-expanded", String(expanded));
    requestAnimationFrame(() => {
      const rect = root.getBoundingClientRect();
      applyPosition({ x: rect.left, y: rect.top });
    });
  }

  summary.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const rect = root.getBoundingClientRect();
    dragging = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: rect.left, y: rect.top };
    moved = false;
    summary.setPointerCapture(event.pointerId);
  });

  summary.addEventListener("pointermove", (event) => {
    if (!dragging || dragging.pointerId !== event.pointerId) return;
    const dx = event.clientX - dragging.startX;
    const dy = event.clientY - dragging.startY;
    if (Math.abs(dx) + Math.abs(dy) > 4) {
      moved = true;
      root.classList.add("ozon-runtime-dragging");
    }
    if (moved) applyPosition({ x: dragging.x + dx, y: dragging.y + dy });
  });

  summary.addEventListener("pointerup", (event) => {
    if (!dragging || dragging.pointerId !== event.pointerId) return;
    summary.releasePointerCapture(event.pointerId);
    const rect = root.getBoundingClientRect();
    dragging = null;
    root.classList.remove("ozon-runtime-dragging");
    if (moved) {
      const snappedX = rect.left + rect.width / 2 < window.innerWidth / 2 ? 12 : window.innerWidth - rect.width - 12;
      applyPosition({ x: snappedX, y: rect.top }, true);
      event.preventDefault();
    }
  });

  summary.addEventListener("click", () => {
    if (moved) { moved = false; return; }
    setExpanded(!root.classList.contains("ozon-runtime-expanded"));
  });

  refreshButton.addEventListener("click", () => refresh());

  restartButton.addEventListener("click", async () => {
    const previousPid = lastStatus && lastStatus.service ? lastStatus.service.pid : null;
    restartButton.disabled = true;
    stopButton.disabled = true;
    restartButton.textContent = "正在重启 (Restarting)";
    message.dataset.tone = "warn";
    message.textContent = "正在关闭并恢复服务 (Restart in Progress)";
    try {
      const response = await fetch("/api/runtime/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (!response.ok) throw new Error("Restart request failed");
      let sawOffline = false;
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        try {
          const status = await fetchStatus();
          if ((sawOffline && status.service.code === "online") || (previousPid && status.service.pid !== previousPid)) {
            window.location.reload();
            return;
          }
        } catch (_) {
          sawOffline = true;
        }
      }
      throw new Error("Restart timed out");
    } catch (error) {
      message.dataset.tone = "bad";
      message.textContent = `${error.message || error} · 请用桌面图标启动`;
      restartButton.disabled = false;
      stopButton.disabled = false;
      restartButton.textContent = "重启工具台 (Restart)";
    }
  });

  function resetStopConfirmation() {
    stopButton.dataset.confirm = "false";
    stopButton.textContent = "停止工具台 (Stop)";
    if (stopConfirmTimer) clearTimeout(stopConfirmTimer);
    stopConfirmTimer = null;
  }

  stopButton.addEventListener("click", async () => {
    if (stopButton.dataset.confirm !== "true") {
      stopButton.dataset.confirm = "true";
      stopButton.textContent = "确认停止 (Confirm Stop)";
      message.dataset.tone = "warn";
      message.textContent = "再次点击将完全停止服务 (Click Again to Stop)";
      stopConfirmTimer = setTimeout(resetStopConfirmation, 5000);
      return;
    }
    if (stopConfirmTimer) clearTimeout(stopConfirmTimer);
    stopButton.disabled = true;
    restartButton.disabled = true;
    stopButton.textContent = "正在停止 (Stopping)";
    message.dataset.tone = "warn";
    message.textContent = "停止后请用桌面图标再次启动 (Use Desktop Icon to Start Again)";
    try {
      await fetch("/api/runtime/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      setTimeout(() => {
        setStatus("service", "offline", "已停止 (Stopped)");
        message.dataset.tone = "ok";
        message.textContent = "工具台已停止 (Workbench Stopped)";
      }, 700);
    } catch (_) {
      setStatus("service", "offline", "连接已关闭 (Connection Closed)");
    }
  });

  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target)) setExpanded(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setExpanded(false);
  });
  window.addEventListener("resize", () => {
    const rect = root.getBoundingClientRect();
    applyPosition({ x: rect.left, y: rect.top }, true);
  });

  restorePosition();
  refresh();
  setInterval(refresh, 3000);
})();
</script>
"""


def inject_runtime_capsule(document: str) -> str:
    if 'id="ozonRuntimeCapsule"' in document:
        return document
    injected = document
    if "</head>" in injected:
        injected = injected.replace("</head>", RUNTIME_CAPSULE_STYLE + "</head>", 1)
    if "</body>" in injected:
        injected = injected.replace("</body>", RUNTIME_CAPSULE_BODY + "</body>", 1)
    return injected

