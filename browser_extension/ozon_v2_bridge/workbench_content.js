"use strict";

const BASE_URL = "http://127.0.0.1:8765";
const EXTENSION_VERSION = chrome.runtime.getManifest().version;

async function api(path, options = {}) {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  return await response.json();
}

async function heartbeat(payload = {}) {
  try {
    await api("/api/browser-bridge/heartbeat", {
      method: "POST",
      body: JSON.stringify({
        bridge_id: "ozon_v2_browser_extension",
        source: "workbench_content_script",
        extension_version: EXTENSION_VERSION,
        url: location.href,
        run_id: payload.run_id || null,
        task_type: payload.task_type || null,
        stage: payload.stage || "workbench_open",
        code: payload.code || null,
        message: payload.message || null,
        connection_only: true,
      }),
    });
  } catch (_) {
    // Keep the visible workbench quiet if the local server restarts mid-poll.
  }
}

function taskKey(task) {
  const data = task && task.data ? task.data : {};
  return `${data.run_id || "none"}:${data.task_type || task.code || "none"}`;
}

function isRunnableTask(task) {
  return !!task && (
    task.code === "browser_task.attribute_template_ready" ||
    task.code === "browser_task.ozon_collection_ready" ||
    task.code === "browser_task.supplier_selection_ready" ||
    task.code === "browser_task.supplier_collection_ready"
  );
}

function isNewerTask(candidate, current) {
  const candidateData = candidate && candidate.data ? candidate.data : {};
  const currentData = current && current.data ? current.data : {};
  if (!candidateData.run_id || candidateData.run_id === currentData.run_id) return false;
  const candidateCreatedAt = Date.parse(candidateData.created_at || "");
  const currentCreatedAt = Date.parse(currentData.created_at || "");
  return Number.isFinite(candidateCreatedAt)
    && Number.isFinite(currentCreatedAt)
    && candidateCreatedAt > currentCreatedAt;
}

async function tick() {
  try {
    const runId = localStorage.getItem("ozon_v2_workbench_run_id") || "";
    let task = await api(runId ? `/api/batches/${encodeURIComponent(runId)}/browser-task` : "/api/browser-task/active");
    if (runId && !isRunnableTask(task)) {
      const activeTask = await api("/api/browser-task/active");
      if (isRunnableTask(activeTask) && isNewerTask(activeTask, task)) {
        task = activeTask;
        localStorage.setItem("ozon_v2_workbench_run_id", task.data.run_id);
      }
    }
    await heartbeat({
      run_id: task.data ? task.data.run_id : null,
      task_type: task.data ? task.data.task_type : null,
      stage: isRunnableTask(task) ? "task_ready" : "idle",
      code: task.code,
      message: task.message,
    });
    if (!isRunnableTask(task)) return;
    await chrome.runtime.sendMessage({ type: "ozon_v2_current_task", task });
  } catch (error) {
    await heartbeat({
      stage: "workbench_poll_failed",
      code: "browser_bridge.workbench_poll_failed",
      message: error && error.message ? error.message : String(error),
    });
  }
}

tick();
setInterval(tick, 1500);
