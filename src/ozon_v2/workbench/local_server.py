from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import traceback
from typing import Any
from urllib.parse import urlparse

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.app.result import Result
from ozon_v2.domain.models import utc_now_iso
from ozon_v2.images.queue import REGULAR_IMAGE_WORKER_IDS
from ozon_v2.services.credential_service import CredentialService
from ozon_v2.services.diagnostics_export_service import DiagnosticsExportService
from ozon_v2.services.workbench_service import WorkbenchService
from ozon_v2.workbench.runner import WorkbenchBackgroundRunner
from ozon_v2.workbench.runtime_control import WorkbenchRuntimeController
from ozon_v2.workbench.runtime_capsule import inject_runtime_capsule


BRIDGE_HEARTBEAT_TIMEOUT_SECONDS = 90


def required_extension_version(project_root: Path) -> str | None:
    manifest_path = project_root / "browser_extension" / "ozon_v2_bridge" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = str(manifest.get("version") or "").strip()
    return version or None


def heartbeat_age_seconds(payload: dict[str, Any]) -> float | None:
    updated_at = payload.get("connection_updated_at") or payload.get("updated_at")
    if not updated_at:
        return None
    try:
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()


def bridge_readiness(payload: dict[str, Any], required_version: str | None) -> dict[str, Any]:
    status = dict(payload)
    age_seconds = heartbeat_age_seconds(status)
    online = age_seconds is not None and age_seconds <= BRIDGE_HEARTBEAT_TIMEOUT_SECONDS
    loaded_version = str(status.get("extension_version") or "").strip() or None
    if not online:
        readiness_code = "offline"
    elif not loaded_version or not required_version:
        readiness_code = "version_unknown"
    elif loaded_version != required_version:
        readiness_code = "version_mismatch"
    else:
        readiness_code = "ready"
    status.update(
        {
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "online": online,
            "loaded_extension_version": loaded_version,
            "required_extension_version": required_version,
            "version_ready": readiness_code == "ready",
            "readiness_code": readiness_code,
        }
    )
    return status


def build_home_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ozon V2 工具台 (Workbench)</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --surface: #ffffff;
      --line: #d9dee8;
      --text: #172033;
      --muted: #62708a;
      --blue: #1d4ed8;
      --blue-soft: #e8f0ff;
      --green: #0f766e;
      --amber: #b45309;
      --red: #b91c1c;
      --shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
    }
    header {
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }
    .brand-subtitle { color: var(--muted); font-size: 12px; font-weight: 400; }
    .stage-nav {
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }
    .stage-nav-inner {
      min-height: 46px;
      display: flex;
      align-items: stretch;
      gap: 6px;
      max-width: 1440px;
      margin: 0 auto;
      padding: 0 18px;
    }
    .stage-link {
      display: inline-flex;
      align-items: center;
      padding: 0 14px;
      border-bottom: 3px solid transparent;
      color: var(--text);
      text-decoration: none;
      font-size: 13px;
      white-space: nowrap;
    }
    .stage-link.active {
      color: var(--blue);
      border-bottom-color: var(--blue);
      background: var(--blue-soft);
    }
    .stage-link.disabled {
      color: #a4adbd;
      cursor: default;
      pointer-events: none;
    }
    .stage-run {
      margin-left: auto;
      display: inline-flex;
      align-items: center;
      color: var(--muted);
      font: 12px Consolas, "Courier New", monospace;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: minmax(260px, 320px) minmax(520px, 1fr);
      gap: 14px;
      padding: 14px;
      max-width: 1440px;
      margin: 0 auto;
    }
    section, aside {
      background: var(--surface);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      border-radius: 6px;
    }
    aside { padding: 14px; }
    section { overflow: hidden; }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }
    h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .stack { display: grid; gap: 12px; }
    label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    input {
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      color: var(--text);
      background: #fff;
      font: inherit;
    }
    button {
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      background: #fff;
      color: var(--text);
      font: inherit;
      cursor: pointer;
      white-space: nowrap;
    }
    button.primary {
      border-color: var(--blue);
      background: var(--blue);
      color: #fff;
    }
    button.danger {
      border-color: #fecaca;
      background: #fff;
      color: var(--red);
    }
    button.danger:hover { background: #fff1f1; }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 14px;
    }
    .kv {
      display: grid;
      grid-template-columns: 112px minmax(0, 1fr);
      gap: 8px;
      padding: 10px 0;
      border-bottom: 1px solid #edf0f5;
    }
    .kv:last-child { border-bottom: 0; }
    .key { color: var(--muted); }
    .value {
      overflow-wrap: anywhere;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--blue-soft);
      color: var(--blue);
      font-size: 12px;
      font-weight: 600;
    }
    .pill.green { background: #e7f6f2; color: var(--green); }
    .pill.amber { background: #fff4df; color: var(--amber); }
    .pill.red { background: #feecec; color: var(--red); }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    .event-log {
      max-height: min(52vh, 560px);
      overflow: auto;
    }
    .event-log table {
      min-width: 720px;
    }
    th, td {
      padding: 10px 14px;
      border-bottom: 1px solid #edf0f5;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      background: #fbfcfe;
    }
    .event-log th {
      position: sticky;
      top: 0;
      z-index: 1;
    }
    td {
      font-size: 13px;
    }
    code {
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    pre {
      margin: 0;
      padding: 14px;
      max-height: 320px;
      overflow: auto;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      background: #fbfcfe;
      border-top: 1px solid var(--line);
    }
    .grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 14px;
    }
    .progress-list { display: grid; gap: 8px; padding: 14px; }
    .progress-row {
      min-height: 52px;
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) 126px;
      align-items: center;
      gap: 12px;
      padding: 8px 12px;
      border: 1px solid #edf0f5;
      border-radius: 6px;
      background: #fff;
    }
    .progress-number {
      width: 26px;
      height: 26px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 4px;
      color: var(--muted);
      background: #f1f4f8;
      font-weight: 650;
    }
    .progress-copy strong { display: block; font-size: 13px; }
    .progress-copy span { color: var(--muted); font-size: 12px; }
    .progress-state {
      min-height: 24px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 2px 8px;
      border-radius: 4px;
      color: var(--muted);
      background: #f1f4f8;
      font-size: 12px;
      font-weight: 600;
    }
    .progress-row.done .progress-number,
    .progress-row.done .progress-state { color: var(--green); background: #e7f6f2; }
    .progress-row.current .progress-number,
    .progress-row.current .progress-state { color: var(--amber); background: #fff4df; }
    .collection-progress-card .section-head { border-bottom: 0; padding-bottom: 8px; }
    .collection-progress-body { display: grid; gap: 12px; padding: 0 14px 14px; }
    .collection-progress-track {
      position: relative;
      height: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: #e9edf3;
      box-shadow: inset 0 1px 2px rgba(15, 23, 42, 0.09);
    }
    .collection-progress-track span {
      position: absolute;
      top: 0;
      bottom: 0;
      width: 0;
      transition: width 180ms ease, left 180ms ease;
    }
    .collection-progress-success { left: 0; background: #16a085; }
    .collection-progress-failure { background: #d9485f; }
    .collection-progress-metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .collection-progress-metrics span {
      min-height: 48px;
      display: grid;
      align-content: center;
      gap: 2px;
      padding: 7px 10px;
      border: 1px solid #edf0f5;
      border-radius: 6px;
      color: var(--muted);
      background: #fbfcfe;
      font-size: 11px;
    }
    .collection-progress-metrics strong { color: var(--text); font-size: 17px; }
    .event-type small, .event-message small {
      display: block;
      margin-top: 3px;
      color: #8b96a9;
      font: 11px/1.35 Consolas, "Courier New", monospace;
      overflow-wrap: anywhere;
    }
    .hint {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }
    .error { color: var(--red); }
    .app-shell {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 224px minmax(0, 1fr);
    }
    .sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 18px 12px;
      border: 0;
      border-radius: 0;
      background: #182130;
      color: #f7f9fc;
      box-shadow: none;
      overflow: auto;
      z-index: 20;
    }
    .sidebar-brand {
      padding: 4px 10px 20px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.09);
    }
    .sidebar-brand strong { display: block; font-size: 17px; }
    .sidebar-brand span { color: #9ba8ba; font-size: 11px; }
    .sidebar-group { margin-top: 18px; }
    .sidebar-label {
      padding: 0 10px 7px;
      color: #7f8da1;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .sidebar-link {
      min-height: 38px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 10px;
      border-radius: 5px;
      color: #c7d0dc;
      text-decoration: none;
      font-size: 12px;
    }
    .sidebar-link:hover { background: #253247; color: #fff; }
    .sidebar-link.active { background: #2457d6; color: #fff; }
    .sidebar-link.disabled { color: #66758a; pointer-events: none; }
    button.sidebar-link { width: 100%; height: 38px; border: 0; background: transparent; cursor: pointer; }
    .nav-mark {
      width: 18px;
      height: 18px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid currentColor;
      border-radius: 4px;
      font-size: 9px;
      font-weight: 700;
    }
    .app-main { min-width: 0; }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 12;
      height: 56px;
      padding: 0 18px;
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(8px);
    }
    .topbar-copy { min-width: 0; }
    .topbar-copy h1 { font-size: 16px; }
    .topbar-copy p { margin: 2px 0 0; color: var(--muted); font-size: 11px; }
    .topbar-actions { display: flex; align-items: center; gap: 8px; }
    .workspace {
      max-width: none;
      grid-template-columns: minmax(280px, 316px) minmax(520px, 1fr);
      padding: 16px 18px 74px;
      margin: 0;
    }
    .control-rail { padding: 14px; align-self: start; }
    .control-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .run-summary {
      padding: 2px 0 0;
      border-top: 1px solid var(--line);
    }
    .store-binding-panel {
      position: fixed;
      top: 68px;
      right: 18px;
      z-index: 40;
      width: min(390px, calc(100vw - 36px));
      display: none;
      gap: 12px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      box-shadow: 0 18px 44px rgba(24, 33, 48, 0.22);
    }
    .store-binding-panel.open { display: grid; }
    .store-panel-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .icon-button { width: 36px; padding: 0; font-size: 18px; line-height: 1; }
    .diagnostics-panel {
      position: fixed;
      left: 224px;
      right: 0;
      bottom: 0;
      z-index: 30;
      height: 46px;
      border-width: 1px 0 0;
      border-radius: 0;
      box-shadow: 0 -4px 18px rgba(24, 33, 48, 0.08);
      transition: height 160ms ease;
    }
    .diagnostics-panel.open { height: 300px; }
    .diagnostics-panel .section-head { height: 45px; padding: 0 18px; }
    .diagnostics-panel pre { height: 254px; max-height: none; }
    .diagnostics-panel:not(.open) pre { display: none; }
    .event-log { height: min(46vh, 470px); max-height: none; }
    @media (max-width: 860px) {
      .app-shell { grid-template-columns: 72px minmax(0, 1fr); }
      .sidebar { padding: 14px 8px; }
      .sidebar-brand { padding: 4px 4px 16px; text-align: center; }
      .sidebar-brand strong { font-size: 14px; }
      .sidebar-brand span, .sidebar-label, .sidebar-link span:last-child { display: none; }
      .sidebar-link { justify-content: center; padding: 0; }
      .workspace { grid-template-columns: 1fr; padding: 12px 12px 68px; }
      .topbar { align-items: center; height: 56px; padding: 0 12px; flex-direction: row; }
      .topbar-copy p { display: none; }
      .diagnostics-panel { left: 72px; }
      .stage-nav-inner { overflow-x: auto; padding: 0 10px; }
      .stage-run { display: none; }
      .stage-link { min-height: 44px; padding: 0 10px; }
      .event-log { max-height: 44vh; }
      .progress-row { grid-template-columns: 28px minmax(0, 1fr); }
      .progress-state { grid-column: 2; justify-self: start; }
      .collection-progress-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 620px) {
      .app-shell { display: block; }
      .sidebar { position: static; width: 100%; height: auto; display: flex; align-items: center; gap: 6px; overflow-x: auto; }
      .sidebar-brand { min-width: 92px; padding: 0 8px; border: 0; }
      .sidebar-group { display: flex; gap: 4px; margin: 0; }
      .sidebar-link { width: 38px; flex: 0 0 38px; }
      .diagnostics-panel { left: 0; }
      .topbar-copy h1 { font-size: 14px; }
      .topbar-actions button:not(.icon-button) { padding: 0 9px; font-size: 11px; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar" aria-label="主菜单 (Primary Navigation)">
      <div class="sidebar-brand">
        <strong>Ozon V2</strong>
        <span>运营驾驶舱 (Operations Cockpit)</span>
      </div>
      <div class="sidebar-group">
        <div class="sidebar-label">运营 (Operations)</div>
        <a class="sidebar-link active" href="/" aria-current="page"><span class="nav-mark">B</span><span>批次总览 (Batch)</span></a>
        <a id="sidebarSupplierNav" class="sidebar-link disabled" aria-disabled="true"><span class="nav-mark">S</span><span>供应商审核 (Supplier)</span></a>
        <a id="sidebarImagesNav" class="sidebar-link disabled" aria-disabled="true"><span class="nav-mark">I</span><span>图片处理 (Images)</span></a>
        <a id="sidebarUploadNav" class="sidebar-link disabled" aria-disabled="true"><span class="nav-mark">U</span><span>上传草稿 (Upload)</span></a>
      </div>
      <div class="sidebar-group">
        <div class="sidebar-label">系统 (System)</div>
        <button id="sidebarDiagnostics" class="sidebar-link" type="button"><span class="nav-mark">D</span><span>运行诊断 (Diagnostics)</span></button>
      </div>
      <div class="sidebar-group">
        <div class="sidebar-label">全局 (Global)</div>
        <a class="sidebar-link" href="/batches"><span class="nav-mark">H</span><span>批次历史 (History)</span></a>
        <a class="sidebar-link" href="/products"><span class="nav-mark">P</span><span>商品资料库 (Products)</span></a>
        <a class="sidebar-link" href="/store"><span class="nav-mark">K</span><span>店铺授权 (Store)</span></a>
        <a class="sidebar-link" href="/diagnostics"><span class="nav-mark">D</span><span>诊断中心 (Diagnostics)</span></a>
        <a class="sidebar-link" href="/settings"><span class="nav-mark">C</span><span>系统设置 (Settings)</span></a>
      </div>
    </aside>
    <div class="app-main">
      <header class="topbar">
        <div class="topbar-copy">
          <h1>Ozon V2 工具台 (Workbench)</h1>
          <p>自动采集、人工门禁与批次状态集中管理</p>
        </div>
        <div class="topbar-actions">
          <button id="openStoreBinding" type="button">店铺授权 (Store)</button>
          <span id="serverState" class="pill">连接中 (Connecting)</span>
        </div>
      </header>
      <nav id="stageNavigation" class="stage-nav" aria-label="批次阶段 (Batch Stages)">
        <div class="stage-nav-inner">
          <a id="batchOverviewNav" class="stage-link active" aria-current="page" href="/">批次总览 (Batch)</a>
          <a id="supplierReviewNav" class="stage-link disabled" aria-disabled="true">供应商审核 (Supplier Review)</a>
          <a id="imageProcessingNav" class="stage-link disabled" aria-disabled="true">图片处理 (Images)</a>
          <a id="uploadDraftNav" class="stage-link disabled" aria-disabled="true">上传草稿 (Upload)</a>
          <span id="stageRunId" class="stage-run">批次 -</span>
        </div>
      </nav>
      <main class="workspace">
    <aside class="control-rail stack">
      <div class="control-title"><h2>批次控制 (Batch Control)</h2><span class="pill">自动模式</span></div>
      <label>目标数量 (Target Count)
        <input id="targetCount" type="number" min="1" max="500" value="1">
      </label>
      <button id="startBatch" class="primary" disabled>开始自动执行 (Run Until Blocked)</button>
      <button id="continueAuto">继续自动执行 (Continue Autopilot)</button>
      <button id="stopRun">停止当前任务 (Stop Current Task)</button>
      <button id="clearAllBatches" class="danger">清空全部批次 (Clear All Batches)</button>
      <div class="run-summary">
        <div class="kv"><div class="key">批次 ID (Run ID)</div><div id="runId" class="value">-</div></div>
        <div class="kv"><div class="key">状态 (Status)</div><div id="status" class="value">-</div></div>
        <div class="kv"><div class="key">执行器 (Runner)</div><div id="runnerState" class="value">-</div></div>
        <div class="kv"><div class="key">发布锁 (Publish Lock)</div><div id="publishLock" class="value">-</div></div>
      </div>
      <h2>门禁 (Gates)</h2>
      <div>
        <div class="kv"><div class="key">凭证 (Credentials)</div><div id="credentialGate" class="value">-</div></div>
        <div class="kv"><div class="key">店铺 ID (Client ID)</div><div id="clientId" class="value">-</div></div>
        <div class="kv"><div class="key">店铺去重 (Store Dedupe)</div><div id="dedupeGate" class="value">-</div></div>
        <div class="kv"><div class="key">商品数量 (Products)</div><div id="dedupeCount" class="value">-</div></div>
      </div>
      <h2>进度 (Progress)</h2>
      <div>
        <div class="kv"><div class="key">已抽种子 (Seeds)</div><div id="sampledCount" class="value">0</div></div>
        <div class="kv"><div class="key">查询就绪 (Queries)</div><div id="queryReadyCount" class="value">0</div></div>
      </div>
      <h2>浏览器桥接 (Browser Bridge)</h2>
      <div>
        <div class="kv"><div class="key">连接 (Connection)</div><div id="bridgeConnection" class="value">-</div></div>
        <div class="kv"><div class="key">阶段 (Stage)</div><div id="bridgeStage" class="value">-</div></div>
        <div class="kv"><div class="key">心跳 (Heartbeat)</div><div id="bridgeHeartbeat" class="value">-</div></div>
        <div class="kv"><div class="key">扩展版本 (Extension Version)</div><div id="bridgeVersion" class="value">-</div></div>
        <div class="kv"><div class="key">版本状态 (Version Status)</div><div id="bridgeVersionStatus" class="value">检查中 (Checking)</div></div>
        <p id="bridgeVersionWarning" class="hint error" hidden></p>
      </div>
    </aside>
    <div class="grid">
      <section>
        <div class="section-head">
          <div>
            <h2>批次进度 (Batch Progress)</h2>
            <p class="hint">各阶段独立落盘，当前只等待必要输入。</p>
          </div>
        </div>
        <div class="progress-list">
          <div id="progressOzon" class="progress-row">
            <span class="progress-number">1</span>
            <div class="progress-copy"><strong>Ozon 采集</strong><span>标题、类目、属性、图片、价格、评价和配送证据</span></div>
            <span class="progress-state">未开始</span>
          </div>
          <div id="progressSupplier" class="progress-row">
            <span class="progress-number">2</span>
            <div class="progress-copy"><strong>供应商审核</strong><span>等待用户提供真实 1688 同款链接</span></div>
            <span class="progress-state">未开始</span>
          </div>
          <div id="progressImages" class="progress-row">
            <span class="progress-number">3</span>
            <div class="progress-copy"><strong>图片处理</strong><span>供应商采集完成后自动进入</span></div>
            <span class="progress-state">未开始</span>
          </div>
          <div id="progressUpload" class="progress-row">
            <span class="progress-number">4</span>
            <div class="progress-copy"><strong>上传草稿</strong><span>图片与字段通过门禁后生成</span></div>
            <span class="progress-state">未开始</span>
          </div>
        </div>
      </section>
      <section>
        <div class="section-head">
          <h2>人工监督动作 (Supervised Actions)</h2>
          <span id="actionCount" class="pill green">0</span>
        </div>
        <div id="actions" class="actions"></div>
      </section>
      <section id="ozonCollectionProgress" class="collection-progress-card" aria-label="Ozon 采集进度">
        <div class="section-head">
          <div>
            <h2>Ozon 采集进度</h2>
            <p id="ozonProcessed" class="hint">等待采集数据</p>
          </div>
          <div>
            <button id="restartOzonCollection" type="button" hidden>重新启动 Ozon 原商品采集</button>
            <p id="ozonRestartMessage" class="hint" hidden>已完成商品及其原始属性字段不会重复采集。</p>
          </div>
        </div>
        <div class="collection-progress-body">
          <div id="ozonProgressBar" class="collection-progress-track" role="progressbar"
               aria-valuemin="0" aria-valuemax="0" aria-valuenow="0">
            <span id="ozonProgressSuccess" class="collection-progress-success"></span>
            <span id="ozonProgressFailure" class="collection-progress-failure"></span>
          </div>
          <div class="collection-progress-metrics">
            <span>成功 (Success)<strong id="ozonSucceeded">0</strong></span>
            <span>最终失败 (Final Failure)<strong id="ozonFailed">0</strong></span>
            <span>已替换 (Replaced)<strong id="ozonReplaced">0</strong></span>
            <span>待处理 (Pending)<strong id="ozonPending">0</strong></span>
          </div>
        </div>
      </section>
      <section>
        <div class="section-head">
          <h2>运行事件 (Run Events)</h2>
          <button id="refresh">刷新 (Refresh)</button>
        </div>
        <div class="event-log">
          <table>
            <thead>
              <tr>
                <th>时间 (Time)</th>
                <th>类型 (Type)</th>
                <th>内容 (Message)</th>
              </tr>
            </thead>
            <tbody id="events"></tbody>
          </table>
        </div>
      </section>
    </div>
      </main>
      <section id="diagnosticsPanel" class="diagnostics-panel" aria-label="运行诊断 (Diagnostics)">
        <div class="section-head">
          <h2>运行诊断 (Diagnostics)</h2>
          <div>
            <button id="exportDiagnostics" type="button" disabled>导出当前批次诊断 ZIP (Export ZIP)</button>
            <button id="toggleDiagnostics" type="button" aria-expanded="false">展开 (Expand)</button>
          </div>
        </div>
        <pre id="raw">{}</pre>
      </section>
      <div id="storeBindingPanel" class="store-binding-panel" aria-hidden="true">
        <div class="store-panel-head">
          <h2>店铺授权绑定 (Store Authorization Binding)</h2>
          <button id="closeStoreBinding" class="icon-button" type="button" title="关闭 (Close)" aria-label="关闭">&times;</button>
        </div>
        <label>店铺 ID (Client ID)
          <input id="storeId" autocomplete="off" placeholder="Client-Id">
        </label>
        <label>密钥 (API Key)
          <input id="apiKey" type="password" autocomplete="off" placeholder="API Key">
        </label>
        <button id="bindStore" class="primary">绑定店铺 (Bind Store)</button>
        <p class="hint">不更换店铺时可留空密钥，系统沿用历史绑定 (Leave API key empty to reuse the historical binding).</p>
      </div>
    </div>
  </div>
  <script>
    const actionLabels = {
      check_credentials: "检查店铺授权 (Check Store Binding)",
      start_dedupe: "开始去重 (Start Dedupe)",
      save_credentials: "已绑定，继续 (Binding Done)",
      mark_store_deduped: "标记已去重 (Mark Deduped)",
      select_seeds: "抽取种子 (Select Seeds)",
      generate_ozon_queries: "生成 Ozon 查询词 (Generate Queries)",
      start_attribute_template_collection: "抓取属性模板 (Fetch Attribute Template)",
      mark_attribute_template_collected: "标记模板已采集 (Mark Template Done)",
      start_ozon_collection: "开始 Ozon 采集 (Start Ozon)",
      mark_ozon_collected: "标记 Ozon 已采集 (Mark Ozon Done)",
      start_supplier_search: "开始 1688 搜同款 (Start Supplier)",
      start_supplier_collection: "采集已核实供应商 (Collect Supplier)",
      mark_supplier_collected: "标记 1688 已采集 (Mark Supplier Done)",
      start_same_product_review: "同款复核 (Same Product Review)",
      approve_same_product: "通过同款 (Approve Same Product)",
      start_ai_fill: "AI 填充 (AI Fill)",
      start_image_processing: "图片处理 (Image Processing)",
      build_draft: "构建草稿 (Build Draft)",
      mark_draft_ready: "草稿完成 (Draft Ready)",
      mark_publish_submitted: "标记已提交 (Mark Submitted)",
      mark_done: "完成 (Done)",
      mark_needs_manual_review: "人工复核 (Manual Review)",
      mark_failed_retryable: "可重试失败 (Retryable Fail)",
      mark_failed_blocked: "阻塞失败 (Blocked Fail)"
    };
    const AUTO_ADVANCE_RUN_KEY = "ozon_v2_auto_advance_run_id";
    const requestedRunId = new URLSearchParams(window.location.search).get("run_id") || "";
    const state = {
      runId: requestedRunId || localStorage.getItem("ozon_v2_workbench_run_id") || "",
      pollTimer: null,
      restartPending: false,
    };
    if (requestedRunId) localStorage.setItem("ozon_v2_workbench_run_id", requestedRunId);
    const $ = (id) => document.getElementById(id);

    function updateStageNavigation(runId) {
      const supplierNav = $("supplierReviewNav");
      const sidebarSupplierNav = $("sidebarSupplierNav");
      const imageNav = $("imageProcessingNav");
      const sidebarImagesNav = $("sidebarImagesNav");
      const uploadNav = $("uploadDraftNav");
      const sidebarUploadNav = $("sidebarUploadNav");
      $("stageRunId").textContent = runId ? `批次 ${runId}` : "批次 -";
      $("exportDiagnostics").disabled = !runId;
      if (!runId) {
        supplierNav.removeAttribute("href");
        supplierNav.classList.add("disabled");
        supplierNav.setAttribute("aria-disabled", "true");
        sidebarSupplierNav.removeAttribute("href");
        sidebarSupplierNav.classList.add("disabled");
        sidebarSupplierNav.setAttribute("aria-disabled", "true");
        imageNav.removeAttribute("href");
        imageNav.classList.add("disabled");
        imageNav.setAttribute("aria-disabled", "true");
        sidebarImagesNav.removeAttribute("href");
        sidebarImagesNav.classList.add("disabled");
        sidebarImagesNav.setAttribute("aria-disabled", "true");
        uploadNav.removeAttribute("href");
        uploadNav.classList.add("disabled");
        uploadNav.setAttribute("aria-disabled", "true");
        sidebarUploadNav.removeAttribute("href");
        sidebarUploadNav.classList.add("disabled");
        sidebarUploadNav.setAttribute("aria-disabled", "true");
        return;
      }
      supplierNav.href = `/batches/${encodeURIComponent(runId)}/supplier-review`;
      supplierNav.classList.remove("disabled");
      supplierNav.removeAttribute("aria-disabled");
      sidebarSupplierNav.href = supplierNav.href;
      sidebarSupplierNav.classList.remove("disabled");
      sidebarSupplierNav.removeAttribute("aria-disabled");
      imageNav.href = `/batches/${encodeURIComponent(runId)}/images`;
      imageNav.classList.remove("disabled");
      imageNav.removeAttribute("aria-disabled");
      sidebarImagesNav.href = imageNav.href;
      sidebarImagesNav.classList.remove("disabled");
      sidebarImagesNav.removeAttribute("aria-disabled");
      uploadNav.href = `/batches/${encodeURIComponent(runId)}/upload`;
      uploadNav.classList.remove("disabled");
      uploadNav.removeAttribute("aria-disabled");
      sidebarUploadNav.href = uploadNav.href;
      sidebarUploadNav.classList.remove("disabled");
      sidebarUploadNav.removeAttribute("aria-disabled");
    }

    function setStoreBinding(open) {
      $("storeBindingPanel").classList.toggle("open", open);
      $("storeBindingPanel").setAttribute("aria-hidden", String(!open));
    }

    function setDiagnostics(open) {
      $("diagnosticsPanel").classList.toggle("open", open);
      $("toggleDiagnostics").setAttribute("aria-expanded", String(open));
      $("toggleDiagnostics").textContent = open ? "收起 (Collapse)" : "展开 (Expand)";
    }

    function renderStageProgress(status) {
      const orderedStages = ["progressOzon", "progressSupplier", "progressImages", "progressUpload"];
      const supplierStatuses = new Set(["supplier_review", "supplier_collecting", "supplier_collected", "same_product_review"]);
      const imageStatuses = new Set(["ai_filling", "image_processing", "image_processed"]);
      const uploadStatuses = new Set(["draft_building", "draft_ready", "publish_submitted", "done"]);
      let currentIndex = 0;
      if (supplierStatuses.has(status)) currentIndex = 1;
      if (imageStatuses.has(status)) currentIndex = 2;
      if (uploadStatuses.has(status)) currentIndex = 3;
      orderedStages.forEach((id, index) => {
        const row = $(id);
        const label = row.querySelector(".progress-state");
        row.classList.toggle("done", index < currentIndex || status === "done");
        row.classList.toggle("current", index === currentIndex && status !== "done");
        label.textContent = status === "done" || index < currentIndex
          ? "已完成 (Done)"
          : index === currentIndex ? "进行中 (Active)" : "未开始";
      });
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      const body = await response.json();
      if (!response.ok) throw body;
      return body;
    }

    function setServer(ok, text) {
      $("serverState").textContent = text;
      $("serverState").className = ok ? "pill green" : "pill red";
    }

    function renderOzonCollectionProgress(progress) {
      const hasProgress = !!progress;
      const value = progress || {};
      const total = Math.max(0, Number(value.total_count) || 0);
      const processed = Math.min(total, Math.max(0, Number(value.processed_count) || 0));
      const success = Math.min(total, Math.max(0, Number(value.success_count) || 0));
      const failure = Math.min(total - success, Math.max(0, Number(value.failure_count) || 0));
      const successPercent = total ? success / total * 100 : 0;
      const failurePercent = total ? failure / total * 100 : 0;
      $("ozonProcessed").textContent = hasProgress
        ? `已处理 ${processed} / ${total}`
        : "等待采集数据";
      $("ozonSucceeded").textContent = String(success);
      $("ozonFailed").textContent = String(failure);
      $("ozonReplaced").textContent = String(Math.max(0, Number(value.replacement_count) || 0));
      $("ozonPending").textContent = String(Math.max(total - processed, 0));
      const bar = $("ozonProgressBar");
      bar.setAttribute("aria-valuemax", String(total));
      bar.setAttribute("aria-valuenow", String(processed));
      $("ozonProgressSuccess").style.width = `${successPercent}%`;
      $("ozonProgressFailure").style.left = `${successPercent}%`;
      $("ozonProgressFailure").style.width = `${failurePercent}%`;
    }

    const EVENT_PRESENTATIONS = {
      "workbench.batch_created": ["批次已创建", "批次已创建并保持发布锁定。"],
      "ozon_collection.ingested": ["采集已入库", "Ozon 采集结果已接收，采集门禁已完成。"],
      "runner.started": ["后台执行器已启动", "后台执行器已从工作台启动。"],
      "runner.stopped": ["后台执行器已停止", "当前后台任务已停止。"],
      "runner.blocked": ["后台执行器等待处理", "后台执行器已到达需要处理的门禁。"],
      "autopilot.started": ["自动运行已启动", "系统将自动运行到下一个阻塞门禁。"],
      "autopilot.blocked": ["自动运行等待处理", "自动运行已停在需要用户处理的门禁。"],
      "supplier_selection.product_captured": ["供应商商品已采集", "一个用户确认的 1688 商品已回传工作台。"],
      "browser_task.cancelled": ["浏览器任务已取消", "当前浏览器任务已由用户停止。"],
      "browser_task.user_restart_requested": ["浏览器采集已请求恢复", "已保留成功结果，并重新派发当前阶段尚未完成的浏览器采集。"],
      "browser_candidate.rejected": ["候选商品未通过", "当前候选不符合采集要求，正在检查其他结果。"],
      "browser_candidate.exhausted": ["当前种子已耗尽", "当前种子没有找到合格候选，正在尝试补位。"],
      "browser_candidate.replaced": ["种子已替换", "失败种子已完成补位，批次继续运行。"],
      "browser_candidate.failed": ["种子最终失败", "失败种子无法补位，需要人工处理。"],
    };

    function eventPresentation(event) {
      const known = EVENT_PRESENTATIONS[event.event_type];
      return known
        ? { title: known[0], message: known[1], raw: event.message || "" }
        : {
            title: "系统事件（查看原始信息）",
            message: "该事件尚无中文模板，原始信息保留用于诊断。",
            raw: event.message || "",
          };
    }

    function textCell(row, value, className = "") {
      const cell = document.createElement("td");
      if (className) cell.className = className;
      cell.textContent = value || "";
      row.appendChild(cell);
      return cell;
    }

    function render(result, events = []) {
      const data = result.data || {};
      const run = data.run || {};
      const runId = run.run_id || data.run_id || state.runId || "";
      $("runId").textContent = runId || "-";
      $("status").textContent = run.status || data.status || "-";
      updateStageNavigation(runId);
      renderStageProgress(run.status || data.status || "created");
      const status = run.status || data.status || "";
      const ozonRestartable = status === "ozon_collecting" && run.ozon_collected !== true;
      const ozonComplete = run.ozon_collected === true;
      const restartButton = $("restartOzonCollection");
      restartButton.hidden = !(ozonRestartable || ozonComplete);
      restartButton.disabled = state.restartPending || !ozonRestartable;
      restartButton.textContent = ozonComplete
        ? "Ozon 原商品采集已完成"
        : "重新启动 Ozon 原商品采集";
      $("ozonRestartMessage").hidden = restartButton.hidden;
      const runner = data.runner || {};
      $("runnerState").textContent = runner.state
        ? `${runner.state}${runner.blocked_reason ? " / " + runner.blocked_reason : ""}`
        : "-";
      $("publishLock").textContent = String(run.publish_locked ?? data.publish_locked ?? "-");
      const gates = data.gates || {};
      const credentials = gates.credentials || {};
      const dedupe = gates.existing_store_dedupe || {};
      $("credentialGate").textContent = credentials.configured ? "已配置 (Configured)" : "缺失 (Missing)";
      $("clientId").textContent = credentials.client_id || "-";
      if (credentials.client_id && !$("storeId").value) $("storeId").value = credentials.client_id;
      if (runId && credentials.configured === false) setStoreBinding(true);
      $("dedupeGate").textContent = dedupe.ready ? `已刷新 (Ready) ${dedupe.refreshed_at || ""}` : "未刷新 (Not Ready)";
      $("dedupeCount").textContent = String(dedupe.product_count ?? "-");
      const progress = data.progress || {};
      $("sampledCount").textContent = String(progress.sampled_seed_count ?? 0);
      $("queryReadyCount").textContent = `${progress.query_ready_count ?? 0} / ${progress.sampled_seed_count ?? 0}`;
      renderOzonCollectionProgress(progress.ozon_collection_progress);
      $("raw").textContent = JSON.stringify(result, null, 2);
      const actions = data.allowed_actions || [];
      $("actionCount").textContent = String(actions.length);
      $("actions").innerHTML = "";
      actions.forEach((action) => {
        const button = document.createElement("button");
        button.textContent = actionLabels[action] || action;
        button.onclick = () => dispatch(action);
        $("actions").appendChild(button);
      });
      $("events").replaceChildren();
      events.forEach((event) => {
        const row = document.createElement("tr");
        textCell(row, event.created_at || "", "event-time");
        const presentation = eventPresentation(event);
        const typeCell = textCell(row, presentation.title, "event-type");
        const rawType = document.createElement("small");
        rawType.textContent = event.event_type || "";
        typeCell.appendChild(rawType);
        const messageCell = textCell(row, presentation.message, "event-message");
        if (presentation.raw) {
          const raw = document.createElement("small");
          raw.textContent = presentation.raw;
          messageCell.appendChild(raw);
        }
        $("events").appendChild(row);
      });
      const eventLog = document.querySelector(".event-log");
      if (eventLog) eventLog.scrollTop = eventLog.scrollHeight;
      if (
        (run.status || data.status) === "supplier_review"
        && localStorage.getItem(AUTO_ADVANCE_RUN_KEY) === runId
      ) {
        localStorage.removeItem(AUTO_ADVANCE_RUN_KEY);
        window.location.assign(`/batches/${encodeURIComponent(runId)}/supplier-review`);
      }
    }

    function renderBridge(status) {
      const data = status.data || status || {};
      const online = !!data.online;
      $("bridgeConnection").textContent = online
        ? `在线 (Online)${data.source ? " / " + data.source : ""}`
        : `离线 (Offline)${data.code ? " / " + data.code : ""}`;
      $("bridgeStage").textContent = [data.run_id, data.task_type, data.stage || data.code]
        .filter(Boolean)
        .join(" / ") || "-";
      $("bridgeHeartbeat").textContent = data.updated_at || "-";
      if (data.source === "workbench_content_script" && data.stage === "task_ready") {
        $("bridgeStage").textContent += " / 等待点击扩展或打开 Ozon 页 (Waiting Extension/Ozon Page)";
      }
      const loaded = data.loaded_extension_version || "未知 (Unknown)";
      const required = data.required_extension_version || "未知 (Unknown)";
      const ready = data.version_ready === true;
      const labels = {
        ready: "已就绪 (Ready)",
        offline: "离线 (Offline)",
        version_mismatch: "需要更新 (Update Required)",
        version_unknown: "版本未知 (Version Unknown)",
      };
      $("bridgeVersion").textContent = `已加载 ${loaded} / 要求 ${required}`;
      $("bridgeVersionStatus").textContent = labels[data.readiness_code] || "不可用 (Unavailable)";
      $("bridgeVersionStatus").className = ready ? "value" : "value error";
      $("startBatch").disabled = !ready;
      $("bridgeVersionWarning").hidden = ready;
      $("bridgeVersionWarning").textContent = ready
        ? ""
        : "浏览器扩展未就绪。请在 Edge 扩展页面重新加载，然后刷新工具台 (Reload the Edge extension, then refresh the workbench).";
    }

    async function loadBridgeStatus() {
      const status = await api("/api/browser-bridge/status");
      renderBridge(status);
      return status;
    }

    async function loadRun() {
      if (!state.runId) {
        updateStageNavigation("");
        return;
      }
      const result = await api(`/api/batches/${encodeURIComponent(state.runId)}`);
      const events = await api(`/api/batches/${encodeURIComponent(state.runId)}/events`);
      render(result, events.data.events || []);
    }

    function startLivePolling() {
      if (state.pollTimer) return;
      state.pollTimer = window.setInterval(() => {
        if (state.runId) loadRun().catch(() => {});
        loadBridgeStatus().catch(() => {});
      }, 1200);
    }

    async function startBatch() {
      try {
        const target = Number($("targetCount").value || 1);
        const result = await api("/api/batches", { method: "POST", body: JSON.stringify({ target_count: target }) });
        state.runId = result.data.run.run_id;
        localStorage.setItem(AUTO_ADVANCE_RUN_KEY, state.runId);
        localStorage.setItem("ozon_v2_workbench_run_id", state.runId);
        startLivePolling();
        await loadRun();
      } catch (error) {
        $("raw").textContent = JSON.stringify(error, null, 2);
        await loadBridgeStatus().catch(() => {});
      }
    }

    async function runUntilBlocked() {
      if (!state.runId) return;
      const result = await api(`/api/batches/${encodeURIComponent(state.runId)}/runner`, {
        method: "POST",
        body: JSON.stringify({ max_steps: 20 })
      }).catch((error) => error);
      startLivePolling();
      await loadRun();
      $("raw").textContent = JSON.stringify(result, null, 2);
    }

    async function restartOzonCollection() {
      if (!state.runId || state.restartPending) return;
      state.restartPending = true;
      $("restartOzonCollection").disabled = true;
      const message = $("ozonRestartMessage");
      message.className = "hint";
      try {
        const result = await api(`/api/batches/${encodeURIComponent(state.runId)}/browser-task/restart`, {
          method: "POST",
          body: JSON.stringify({})
        });
        message.textContent = result.data.dispatch_state === "waiting_for_extension"
          ? "恢复请求已保存，正在等待浏览器扩展上线。"
          : `已保留 ${result.data.completed_count} 件，继续 ${result.data.pending_count} 件。`;
      } catch (error) {
        message.className = "hint error";
        message.textContent = error.message || "Ozon 采集恢复失败。";
      } finally {
        state.restartPending = false;
        await loadRun().catch(() => {});
      }
    }

    async function stopCurrentTask() {
      if (!state.runId) return;
      localStorage.removeItem(AUTO_ADVANCE_RUN_KEY);
      const result = await api(`/api/batches/${encodeURIComponent(state.runId)}/runner/stop`, {
        method: "POST",
        body: JSON.stringify({})
      }).catch((error) => error);
      await loadRun();
      await loadBridgeStatus().catch(() => {});
      $("raw").textContent = JSON.stringify(result, null, 2);
    }

    async function clearAllBatches() {
      const confirmed = window.confirm(
        "此操作会永久删除全部当前及历史批次运行数据，且无法恢复。店铺授权、店铺去重和种子池会保留。确认继续吗？"
      );
      if (!confirmed) return;
      const result = await api("/api/batches/clear", {
        method: "POST",
        body: JSON.stringify({ confirm: true })
      }).catch((error) => error);
      $("raw").textContent = JSON.stringify(result, null, 2);
      if (!result || result.ok !== true) return;
      localStorage.removeItem(AUTO_ADVANCE_RUN_KEY);
      localStorage.removeItem("ozon_v2_workbench_run_id");
      state.runId = "";
      window.location.assign("/");
    }

    async function dispatch(action) {
      const result = await api(`/api/batches/${encodeURIComponent(state.runId)}/actions`, {
        method: "POST",
        body: JSON.stringify({ action })
      }).catch((error) => error);
      await loadRun();
      if (result && result.ok === false) {
        $("raw").textContent = JSON.stringify(result, null, 2);
      }
    }

    async function bindStore() {
      const result = await api("/api/store-binding", {
        method: "POST",
        body: JSON.stringify({
          client_id: $("storeId").value.trim(),
          api_key: $("apiKey").value.trim()
        })
      }).catch((error) => error);
      $("apiKey").value = "";
      if (state.runId && result && result.ok) {
        await dispatch("save_credentials");
        await runUntilBlocked();
      } else if (state.runId) {
        await loadRun();
      }
      $("raw").textContent = JSON.stringify(result, null, 2);
    }

    $("startBatch").onclick = startBatch;
    $("continueAuto").onclick = runUntilBlocked;
    $("restartOzonCollection").onclick = restartOzonCollection;
    $("stopRun").onclick = stopCurrentTask;
    $("clearAllBatches").onclick = clearAllBatches;
    $("refresh").onclick = loadRun;
    $("bindStore").onclick = bindStore;
    $("openStoreBinding").onclick = () => setStoreBinding(true);
    $("closeStoreBinding").onclick = () => setStoreBinding(false);
    $("toggleDiagnostics").onclick = () => setDiagnostics(!$("diagnosticsPanel").classList.contains("open"));
    $("sidebarDiagnostics").onclick = () => setDiagnostics(!$("diagnosticsPanel").classList.contains("open"));
    $("exportDiagnostics").onclick = () => {
      if (!state.runId) return;
      const link = document.createElement("a");
      link.href = `/api/batches/${encodeURIComponent(state.runId)}/diagnostics.zip`;
      link.download = `ozon-v2-diagnostics-${state.runId}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
    };

    api("/api/health").then(() => {
      setServer(true, "已连接 (Connected)");
      startLivePolling();
      loadBridgeStatus().catch(() => {});
      return loadRun();
    }).catch((error) => {
      setServer(false, "未连接 (Disconnected)");
      $("raw").textContent = JSON.stringify(error, null, 2);
    });
  </script>
</body>
</html>
"""


def build_supplier_review_html(run_id: str) -> str:
    safe_run_id = json.dumps(run_id)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>采集审核 (Collection Review) - Ozon V2</title>
  <style>
    :root {{ --bg:#f3f6fa; --surface:#fff; --line:#d9dee8; --text:#172033; --muted:#62708a; --blue:#1d4ed8; --blue-soft:#e8f0ff; --red:#b91c1c; --green:#0f766e; --amber:#b45309; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 "Segoe UI","Microsoft YaHei",Arial,sans-serif; }}
    header {{ height:56px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:0 18px; background:var(--surface); border-bottom:1px solid var(--line); }}
    h1 {{ margin:0; font-size:18px; letter-spacing:0; }}
    .brand-subtitle {{ color:var(--muted); font-size:12px; font-weight:400; }}
    .connected {{ min-height:24px; display:inline-flex; align-items:center; padding:2px 8px; border-radius:4px; color:var(--green); background:#e7f6f2; font-size:12px; font-weight:600; }}
    .stage-nav {{ background:var(--surface); border-bottom:1px solid var(--line); }}
    .stage-nav-inner {{ min-height:46px; display:flex; align-items:stretch; gap:6px; max-width:1440px; margin:0 auto; padding:0 18px; }}
    .stage-link {{ display:inline-flex; align-items:center; padding:0 14px; border-bottom:3px solid transparent; color:var(--text); text-decoration:none; font-size:13px; white-space:nowrap; }}
    .stage-link.active {{ color:var(--blue); border-bottom-color:var(--blue); background:var(--blue-soft); }}
    .stage-link.disabled {{ color:#a4adbd; cursor:default; pointer-events:none; }}
    .stage-run {{ margin-left:auto; display:inline-flex; align-items:center; color:var(--muted); font:12px Consolas,"Courier New",monospace; }}
    main {{ max-width:1440px; margin:0 auto; padding:20px; }}
    .summary {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }}
    .summary h2 {{ margin:0; font-size:16px; }}
    .muted {{ color:var(--muted); }}
    .table-wrap {{ overflow:auto; background:var(--surface); border:1px solid var(--line); border-radius:6px; }}
    table {{ width:100%; min-width:980px; border-collapse:collapse; }}
    th,td {{ padding:12px; border-bottom:1px solid #edf0f5; text-align:left; vertical-align:top; }}
    th {{ position:sticky; top:0; background:#fbfcfe; color:var(--muted); font-size:12px; z-index:1; }}
    .product {{ display:grid; grid-template-columns:88px minmax(220px,1fr); gap:12px; }}
    img {{ width:88px; height:88px; object-fit:contain; background:#fff; border:1px solid var(--line); border-radius:4px; }}
    a {{ color:var(--blue); text-decoration:none; }}
    dl {{ margin:0; display:grid; grid-template-columns:max-content minmax(120px,1fr); gap:4px 8px; font-size:12px; }}
    dt {{ color:var(--muted); }} dd {{ margin:0; overflow-wrap:anywhere; }}
    input {{ width:100%; min-width:310px; height:38px; padding:0 10px; border:1px solid var(--line); border-radius:6px; font:inherit; }}
    button {{ height:38px; padding:0 14px; border:1px solid var(--line); border-radius:6px; background:#fff; color:var(--text); font:inherit; cursor:pointer; }}
    button.primary {{ color:#fff; background:var(--blue); border-color:var(--blue); }}
    button.reject-button {{ width:100%; color:var(--red); border-color:#fecaca; background:#fff7f7; }}
    button.reject-button:hover {{ background:#fee2e2; }}
    button.secondary-button {{ width:100%; margin-top:8px; color:var(--blue); border-color:#bfdbfe; background:var(--blue-soft); }}
    button.secondary-button:hover {{ background:#dbeafe; }}
    button:disabled {{ opacity:.5; cursor:not-allowed; }}
    .status-pill {{ min-height:24px; display:inline-flex; align-items:center; padding:2px 9px; border-radius:4px; color:var(--amber); background:#fff4df; font-size:12px; font-weight:600; }}
    .bottom-grid {{ display:grid; grid-template-columns:minmax(0,1.7fr) minmax(300px,.9fr); gap:14px; margin-top:14px; }}
    .panel {{ background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:16px; }}
    .panel h2 {{ margin:0 0 14px; font-size:15px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(5,minmax(88px,1fr)); gap:10px; }}
    .metric {{ min-height:72px; padding:10px; border:1px solid #edf0f5; border-radius:4px; background:#f8fafc; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:6px; font-size:18px; }}
    .collection-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
    .collection-actions {{ display:grid; gap:9px; }}
    .collection-actions button {{ width:100%; }}
    .collection-feedback {{ grid-column:1 / -1; }}
    .evidence-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; margin-top:14px; }}
    .evidence-card {{ min-width:0; background:var(--surface); border:1px solid var(--line); border-radius:6px; overflow:hidden; }}
    .evidence-card-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; min-height:48px; padding:10px 14px; border-bottom:1px solid var(--line); background:#fbfcfe; }}
    .evidence-card-head h3 {{ margin:0; font-size:14px; }}
    .evidence-card-head-actions {{ display:flex; align-items:center; gap:10px; }}
    .evidence-pager {{ display:flex; align-items:center; gap:6px; }}
    .evidence-pager button {{ width:30px; height:30px; padding:0; font-size:18px; line-height:1; }}
    .evidence-pager span {{ min-width:54px; color:var(--muted); text-align:center; font-size:12px; }}
    .evidence-body {{ height:min(650px,70vh); display:grid; align-content:start; gap:14px; padding:14px; overflow:auto; }}
    .evidence-record {{ display:grid; gap:12px; min-width:0; }}
    .evidence-record + .evidence-record {{ padding-top:14px; border-top:2px solid var(--line); }}
    .record-title {{ margin:0; font-size:13px; }}
    .field-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }}
    .field {{ min-width:0; padding:9px; border:1px solid #edf0f5; border-radius:4px; background:#f8fafc; }}
    .field span {{ display:block; color:var(--muted); font-size:11px; }}
    .field strong {{ display:block; margin-top:4px; overflow-wrap:anywhere; font-size:12px; }}
    .gallery {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(82px,1fr)); gap:8px; }}
    .gallery img {{ width:100%; height:96px; }}
    .subhead {{ margin:0 0 8px; font-size:12px; }}
    details {{ border-top:1px solid #edf0f5; padding-top:10px; }}
    summary {{ cursor:pointer; color:var(--blue); font-size:12px; }}
    pre {{ max-height:260px; margin:8px 0 0; padding:10px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; background:#f8fafc; border:1px solid #edf0f5; border-radius:4px; font:11px/1.45 Consolas,"Courier New",monospace; }}
    .missing {{ color:var(--red); }}
    .feedback-list {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
    .feedback-item {{ min-height:64px; padding:10px; border:1px solid #edf0f5; border-radius:4px; background:#f8fafc; }}
    .feedback-item span {{ display:block; color:var(--muted); font-size:12px; }}
    .feedback-item strong {{ display:block; margin-top:5px; overflow-wrap:anywhere; font-size:13px; }}
    .sku-decision-panel {{ margin-top:14px; }}
    .sku-decision-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }}
    .sku-decision-head h2 {{ margin:0; }}
    .sku-options {{ display:grid; gap:12px; }}
    .sku-target {{ padding:12px; border:1px solid #bfdbfe; border-radius:6px; background:#f5f8ff; }}
    .sku-section-title {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin:0 0 8px; font-size:13px; }}
    .sku-facts {{ display:flex; flex-wrap:wrap; gap:7px; }}
    .sku-fact {{ display:inline-flex; align-items:center; gap:5px; padding:5px 8px; border:1px solid #dbe4f3; border-radius:5px; background:#fff; font-size:12px; }}
    .sku-fact span {{ color:var(--muted); }}
    .sku-mode-note {{ padding:9px 11px; border-radius:5px; background:#f8fafc; color:var(--muted); font-size:12px; }}
    .sku-mode-note.warning {{ color:var(--amber); background:#fff8e8; }}
    .sku-candidate-section {{ display:grid; gap:8px; }}
    .sku-candidate-section + .sku-candidate-section {{ margin-top:4px; }}
    .sku-candidate-section-title {{ margin:0; color:var(--text); font-size:12px; }}
    .sku-other-options {{ padding-top:0; border-top:0; }}
    .sku-other-options summary {{ padding:8px 0; font-weight:600; }}
    .sku-candidate-list {{ display:grid; gap:12px; }}
    .sku-candidate-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:10px; }}
    .sku-option {{ display:grid; grid-template-columns:auto minmax(0,1fr); gap:10px; min-height:96px; padding:12px; border:1px solid var(--line); border-radius:6px; background:#fbfcfe; cursor:pointer; }}
    .sku-option.recommended {{ border-color:#86b7fe; background:#f5f8ff; }}
    .sku-option:has(input:checked) {{ border-color:var(--blue); box-shadow:0 0 0 2px var(--blue-soft); }}
    .sku-option input {{ width:16px; min-width:16px; height:16px; margin-top:3px; }}
    .sku-option-main {{ display:grid; grid-template-columns:auto minmax(0,1fr); align-items:start; gap:10px; min-width:0; }}
    .sku-option-main.no-image {{ grid-template-columns:minmax(0,1fr); }}
    .sku-option-image {{ width:72px; height:72px; object-fit:contain; border:1px solid var(--line); border-radius:5px; background:#fff; }}
    .sku-option strong,.sku-option span {{ display:block; overflow-wrap:anywhere; }}
    .sku-option span {{ margin-top:4px; color:var(--muted); font-size:12px; }}
    .sku-option .sku-badge {{ display:inline-flex; width:max-content; margin:0 0 6px; padding:2px 6px; border-radius:4px; color:var(--blue); background:var(--blue-soft); font-size:10px; font-weight:700; }}
    .sku-option .sku-comparison {{ color:var(--green); }}
    .sku-action-row {{ display:flex; align-items:center; justify-content:flex-end; gap:10px; margin-top:12px; }}
    .subject-master-area {{ margin-top:16px; padding-top:14px; border-top:1px solid var(--line); }}
    .subject-gallery {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(112px,1fr)); gap:10px; margin-top:10px; }}
    .subject-choice {{ position:relative; min-height:132px; padding:6px; border:1px solid var(--line); border-radius:6px; background:#fff; cursor:pointer; }}
    .subject-choice:has(input:checked) {{ border-color:var(--blue); box-shadow:0 0 0 2px var(--blue-soft); }}
    .subject-choice input {{ position:absolute; top:8px; left:8px; z-index:2; width:16px; min-width:16px; height:16px; }}
    .subject-choice img {{ width:100%; height:118px; }}
    .subject-source {{ position:absolute; right:8px; top:8px; z-index:2; padding:2px 6px; border-radius:4px; background:rgba(24,33,48,.86); color:#fff; font-size:10px; }}
    .subject-evidence-meta {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:8px; }}
    .subject-confirm-row {{ display:grid; grid-template-columns:180px minmax(220px,1fr) auto; align-items:end; gap:12px; margin-top:12px; }}
    .subject-confirm-row label {{ color:var(--muted); font-size:12px; }}
    .subject-confirm-row input[type="number"] {{ min-width:0; margin-top:4px; }}
    .subject-composition {{ min-height:38px; padding:8px 10px; border:1px solid #edf0f5; border-radius:4px; background:#f8fafc; }}
    .subject-composition span,.subject-composition strong {{ display:block; overflow-wrap:anywhere; }}
    .subject-composition span {{ color:var(--muted); font-size:11px; }}
    .subject-composition strong {{ margin-top:3px; font-size:12px; }}
    .error {{ color:var(--red); }} .success {{ color:var(--green); }}
    .app-shell {{ min-height:100vh; display:grid; grid-template-columns:224px minmax(0,1fr); }}
    .sidebar {{ position:sticky; top:0; height:100vh; padding:18px 12px; background:#182130; color:#f7f9fc; overflow:auto; z-index:20; }}
    .sidebar-brand {{ padding:4px 10px 20px; border-bottom:1px solid rgba(255,255,255,.09); }}
    .sidebar-brand strong {{ display:block; font-size:17px; }}
    .sidebar-brand span {{ color:#9ba8ba; font-size:11px; }}
    .sidebar-group {{ margin-top:18px; }}
    .sidebar-label {{ padding:0 10px 7px; color:#7f8da1; font-size:10px; font-weight:700; text-transform:uppercase; }}
    .sidebar-link {{ min-height:38px; display:flex; align-items:center; gap:10px; padding:0 10px; border-radius:5px; color:#c7d0dc; text-decoration:none; font-size:12px; }}
    .sidebar-link:hover {{ background:#253247; color:#fff; }}
    .sidebar-link.active {{ background:#2457d6; color:#fff; }}
    .sidebar-link.disabled {{ color:#66758a; pointer-events:none; }}
    .nav-mark {{ width:18px; height:18px; display:inline-flex; align-items:center; justify-content:center; border:1px solid currentColor; border-radius:4px; font-size:9px; font-weight:700; }}
    .app-main {{ min-width:0; }}
    .topbar {{ position:sticky; top:0; z-index:12; height:56px; background:rgba(255,255,255,.96); backdrop-filter:blur(8px); }}
    .topbar-copy h1 {{ font-size:16px; }}
    .topbar-copy p {{ margin:2px 0 0; color:var(--muted); font-size:11px; }}
    .workspace {{ max-width:none; padding:18px; }}
    .review-toolbar {{ min-height:58px; display:flex; align-items:center; justify-content:space-between; gap:14px; margin-bottom:12px; padding:12px 14px; background:var(--surface); border:1px solid var(--line); border-radius:6px; }}
    @media(max-width:900px) {{
      .app-shell {{ grid-template-columns:72px minmax(0,1fr); }}
      .sidebar {{ padding:14px 8px; }}
      .sidebar-brand {{ padding:4px 4px 16px; text-align:center; }}
      .sidebar-brand strong {{ font-size:14px; }}
      .sidebar-brand span,.sidebar-label,.sidebar-link span:last-child {{ display:none; }}
      .sidebar-link {{ justify-content:center; padding:0; }}
      .stage-nav-inner {{ overflow-x:auto; padding:0 10px; }}
      .stage-run {{ display:none; }}
      .stage-link {{ min-height:44px; padding:0 10px; }}
      .bottom-grid {{ grid-template-columns:1fr; }}
      .evidence-grid {{ grid-template-columns:1fr; }}
      .metrics {{ grid-template-columns:repeat(2,minmax(100px,1fr)); }}
      .subject-confirm-row {{ grid-template-columns:1fr; }}
    }}
    @media(max-width:700px) {{ .topbar,.summary,.review-toolbar {{ align-items:flex-start; height:auto; flex-direction:column; padding:12px 14px; }} .workspace {{ padding:10px; }} }}
    @media(max-width:620px) {{
      .app-shell {{ display:block; }}
      .sidebar {{ position:static; width:100%; height:auto; display:flex; align-items:center; gap:6px; overflow-x:auto; }}
      .sidebar-brand {{ min-width:92px; padding:0 8px; border:0; }}
      .sidebar-group {{ display:flex; gap:4px; margin:0; }}
      .sidebar-link {{ width:38px; flex:0 0 38px; }}
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar" aria-label="主菜单 (Primary Navigation)">
      <div class="sidebar-brand"><strong>Ozon V2</strong><span>运营驾驶舱 (Operations Cockpit)</span></div>
      <div class="sidebar-group">
        <div class="sidebar-label">运营 (Operations)</div>
        <a class="sidebar-link" href="/?run_id={run_id}"><span class="nav-mark">B</span><span>批次总览 (Batch)</span></a>
        <a class="sidebar-link active" href="/batches/{run_id}/supplier-review" aria-current="page"><span class="nav-mark">S</span><span>采集审核 (Collection)</span></a>
        <a class="sidebar-link" href="/batches/{run_id}/images"><span class="nav-mark">I</span><span>图片处理 (Images)</span></a>
        <a class="sidebar-link" href="/batches/{run_id}/upload"><span class="nav-mark">U</span><span>上传草稿 (Upload)</span></a>
      </div>
      <div class="sidebar-group"><div class="sidebar-label">全局 (Global)</div>
        <a class="sidebar-link" href="/batches"><span class="nav-mark">H</span><span>批次历史 (History)</span></a><a class="sidebar-link" href="/products"><span class="nav-mark">P</span><span>商品资料库 (Products)</span></a><a class="sidebar-link" href="/store"><span class="nav-mark">K</span><span>店铺授权 (Store)</span></a><a class="sidebar-link" href="/diagnostics"><span class="nav-mark">D</span><span>诊断中心 (Diagnostics)</span></a><a class="sidebar-link" href="/settings"><span class="nav-mark">C</span><span>系统设置 (Settings)</span></a>
      </div>
    </aside>
    <div class="app-main">
      <header class="topbar">
        <div class="topbar-copy"><h1>Ozon V2 工具台 (Workbench)</h1><p>真实供应商由用户核实，系统负责链接采集与后续自动化</p></div>
        <span class="connected">已连接 (Connected)</span>
      </header>
      <nav id="stageNavigation" class="stage-nav" aria-label="批次阶段 (Batch Stages)">
        <div class="stage-nav-inner">
          <a id="batchOverviewNav" class="stage-link" href="/?run_id={run_id}">批次总览 (Batch)</a>
          <a id="supplierReviewNav" class="stage-link active" aria-current="page" href="/batches/{run_id}/supplier-review">采集审核 (Collection Review)</a>
          <a id="imageProcessingNav" class="stage-link" href="/batches/{run_id}/images">图片处理 (Images)</a>
          <a id="uploadDraftNav" class="stage-link" href="/batches/{run_id}/upload">上传草稿 (Upload)</a>
          <span class="stage-run">批次 {run_id}</span>
        </div>
      </nav>
  <main class="workspace">
    <div class="review-toolbar">
      <div><h2>采集审核 (Collection Review)</h2><span class="muted">供应商审核 (Supplier Review) · 批次 <code id="runId"></code> · 对照核实 Ozon 与 1688 采集结果</span></div>
      <div id="status" class="status-pill">加载中 (Loading)</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Ozon 商品 (Product)</th><th>尺寸与规格证据 (Size Evidence)</th><th>五通道采集 (5-Lane Collection)</th><th>处理 (Action)</th></tr></thead>
        <tbody id="items"></tbody>
      </table>
    </div>
    <div class="evidence-grid">
      <section id="ozonEvidence" class="evidence-card"><div class="evidence-card-head"><h3>Ozon 采集结果 (Ozon Evidence)</h3><div class="evidence-card-head-actions"><span id="ozonEvidenceStatus" class="muted">等待加载</span><div id="ozonEvidencePager" class="evidence-pager"><button id="ozonEvidencePrev" type="button" title="上一件 (Previous)">‹</button><span id="ozonEvidencePage">0 / 0</span><button id="ozonEvidenceNext" type="button" title="下一件 (Next)">›</button></div></div></div><div class="evidence-body"></div></section>
      <section id="supplierEvidence" class="evidence-card"><div class="evidence-card-head"><h3>1688 采集结果 (Supplier Evidence)</h3><div class="evidence-card-head-actions"><span id="supplierEvidenceStatus" class="muted">等待采集</span><div id="supplierEvidencePager" class="evidence-pager"><button id="supplierEvidencePrev" type="button" title="上一件 (Previous)">‹</button><span id="supplierEvidencePage">0 / 0</span><button id="supplierEvidenceNext" type="button" title="下一件 (Next)">›</button></div></div></div><div class="evidence-body"></div></section>
    </div>
    <section id="skuDecisionPanel" class="panel sku-decision-panel">
      <div class="sku-decision-head">
        <div><h2>真实 SKU 与主体确认 (SKU & Subject Truth)</h2><span id="skuDecisionContext" class="muted">等待 1688 采集结果</span></div>
        <span id="skuDecisionStatus" class="status-pill">未锁定 (Unlocked)</span>
      </div>
      <div id="skuOptions" class="sku-options"></div>
      <div class="sku-action-row"><span id="skuDecisionMessage" class="muted"></span><button id="lockSupplierSku" class="primary" disabled>锁定真实 SKU (Lock Real SKU)</button></div>
      <div id="subjectMasterArea" class="subject-master-area" hidden>
        <h3>主体证据图 (Subject Evidence)</h3>
        <span class="muted">可从已锁定 SKU 图片和同一供应商商品图库中多选。这里只确认真实主体与套装，干净白底图由生图技能生成。</span>
        <div class="subject-evidence-meta">
          <span id="subjectEvidenceCount" class="muted">已选 0 张 (0 Selected)</span>
          <span class="muted">工具台仅推荐，最终由用户确认。</span>
        </div>
        <div id="subjectMasterImages" class="subject-gallery"></div>
        <div class="subject-confirm-row">
          <label>可见主体数量 (Visible Quantity)<input id="visibleSubjectQuantity" type="number" min="1" step="1"></label>
          <div class="subject-composition"><span>真实套装组成 (Set Composition)</span><strong id="subjectSetComposition">-</strong></div>
          <button id="confirmSubjectMaster" class="primary" disabled>确认主体证据 (Confirm Subject Evidence)</button>
        </div>
      </div>
    </section>
    <div class="bottom-grid">
      <div class="panel">
        <h2>Ozon 证据摘要 (Evidence)</h2>
        <div class="metrics">
          <div class="metric"><span>商品</span><strong id="evidenceProducts">0</strong></div>
          <div class="metric"><span>图片</span><strong id="evidenceImages">0</strong></div>
          <div class="metric"><span>属性</span><strong id="evidenceAttributes">0</strong></div>
          <div class="metric"><span>尺寸证据</span><strong id="evidenceDimensions">0</strong></div>
          <div class="metric"><span>单 SKU</span><strong id="evidenceSku">0</strong></div>
        </div>
      </div>
      <div class="panel collection-actions">
        <div class="collection-head"><h2>采集控制 (Collection)</h2><strong id="linkProgress">0 / 0</strong></div>
        <span id="message" class="muted">只重新打开未完成通道；已回传的 1688 商品保持完成。</span>
        <button id="collect" class="primary">重新启动 1688 采集</button>
        <button id="approveCollection" class="primary" disabled>逐件锁定 SKU 与主体 (Lock SKU & Subject)</button>
      </div>
      <div class="panel collection-feedback">
        <h2>1688 采集反馈 (Collection Feedback)</h2>
        <div id="feedbackEmpty" class="muted">尚未收到浏览器公开字段 (No browser evidence yet)</div>
        <div id="feedbackList" class="feedback-list"></div>
      </div>
    </div>
  </main>
    </div>
  </div>
  <script>
    const runId = {safe_run_id};
    const state = {{ items: [], status: "", collectionProgress: null, canApprove: false, evidenceIndex: 0, restartPending: false }};
    let loading = false;
    const $ = (id) => document.getElementById(id);
    $("runId").textContent = runId;

    async function api(path, options = {{}}) {{
      const response = await fetch(path, {{ headers: {{ "Content-Type": "application/json" }}, ...options }});
      const body = await response.json();
      if (!response.ok) throw body;
      return body;
    }}

    function evidenceList(values) {{
      const dl = document.createElement("dl");
      const entries = Object.entries(values || {{}});
      if (!entries.length) {{
        const text = document.createElement("span");
        text.className = "muted";
        text.textContent = "页面未显示尺寸字段 (No visible size field)";
        return text;
      }}
      entries.forEach(([key, value]) => {{
        const dt = document.createElement("dt"); dt.textContent = key;
        const dd = document.createElement("dd"); dd.textContent = String(value);
        dl.append(dt, dd);
      }});
      return dl;
    }}

    function valueText(value) {{
      if (value == null || value === "") return "未采集 (Missing)";
      if (typeof value === "object") return JSON.stringify(value, null, 2);
      return String(value);
    }}

    function field(label, value) {{
      const node = document.createElement("div"); node.className = "field";
      const name = document.createElement("span"); name.textContent = label;
      const content = document.createElement("strong"); content.textContent = valueText(value);
      if (value == null || value === "" || (Array.isArray(value) && !value.length)) content.className = "missing";
      node.append(name, content);
      return node;
    }}

    function detailsBlock(label, value) {{
      const details = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = label;
      const pre = document.createElement("pre"); pre.textContent = JSON.stringify(value || {{}}, null, 2);
      details.append(summary, pre);
      return details;
    }}

    function imageGallery(images, title) {{
      const section = document.createElement("div");
      const heading = document.createElement("h4"); heading.className = "subhead"; heading.textContent = `${{title}} (${{(images || []).length}})`;
      const gallery = document.createElement("div"); gallery.className = "gallery";
      (images || []).forEach((url, index) => {{
        const link = document.createElement("a"); link.href = url; link.target = "_blank"; link.rel = "noopener";
        const image = document.createElement("img"); image.src = url; image.alt = `${{title}} ${{index + 1}}`; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
        link.append(image); gallery.append(link);
      }});
      if (!(images || []).length) gallery.append(field("图片", null));
      section.append(heading, gallery);
      return section;
    }}

    function renderEvidence({{ resetScroll = false }} = {{}}) {{
      const ozonCard = $("ozonEvidence");
      const ozonStatus = $("ozonEvidenceStatus");
      const ozonBody = ozonCard.querySelector(".evidence-body");
      const supplierCard = $("supplierEvidence");
      const supplierStatus = $("supplierEvidenceStatus");
      const supplierBody = supplierCard.querySelector(".evidence-body");
      const previousOzonScroll = ozonBody.scrollTop;
      const previousSupplierScroll = supplierBody.scrollTop;
      ozonBody.replaceChildren();
      supplierBody.replaceChildren();
      const ozonMissingCount = state.items.reduce((total, item) => total + (((item.ozon_completeness || {{}}).missing_fields || []).length), 0);
      const supplierMissingCount = state.items.reduce((total, item) => total + (((item.supplier_completeness || {{}}).missing_fields || []).length), 0);
      ozonStatus.textContent = ozonMissingCount ? `缺失 ${{ozonMissingCount}} 项` : "完整 (Complete)";
      ozonStatus.className = ozonMissingCount ? "missing" : "success";
      supplierStatus.textContent = supplierMissingCount ? `缺失 ${{supplierMissingCount}} 项` : "完整 (Complete)";
      supplierStatus.className = supplierMissingCount ? "missing" : "success";
      if (!state.items.length) {{
        ["ozonEvidencePage", "supplierEvidencePage"].forEach((id) => $(id).textContent = "0 / 0");
        ["ozonEvidencePrev", "ozonEvidenceNext", "supplierEvidencePrev", "supplierEvidenceNext"].forEach((id) => $(id).disabled = true);
        ozonBody.append(field("Ozon 数据", null));
        supplierBody.append(field("1688 数据", null));
        return;
      }}
      state.evidenceIndex = Math.max(0, Math.min(state.evidenceIndex, state.items.length - 1));
      const index = state.evidenceIndex;
      const item = state.items[state.evidenceIndex];
      const pageText = `${{index + 1}} / ${{state.items.length}}`;
      ["ozonEvidencePage", "supplierEvidencePage"].forEach((id) => $(id).textContent = pageText);
      ["ozonEvidencePrev", "supplierEvidencePrev"].forEach((id) => $(id).disabled = index === 0);
      ["ozonEvidenceNext", "supplierEvidenceNext"].forEach((id) => $(id).disabled = index >= state.items.length - 1);
      const ozon = item.ozon_product || {{}};
      const supplier = item.supplier_product || null;
      const ozonMissing = (item.ozon_completeness || {{}}).missing_fields || [];
      const supplierMissing = (item.supplier_completeness || {{}}).missing_fields || [];

      const ozonRecord = document.createElement("section"); ozonRecord.className = "evidence-record";
      const ozonTitle = document.createElement("h4"); ozonTitle.className = "record-title";
      ozonTitle.textContent = `${{index + 1}}. ${{ozon.title || item.ozon_title || item.seed_id}} · ${{ozonMissing.length ? `缺失 ${{ozonMissing.join(", ")}}` : "完整"}}`;
      const ozonFields = document.createElement("div"); ozonFields.className = "field-grid";
      ozonFields.append(
        field("标题 (Title)", ozon.title), field("Ozon ID", ozon.ozon_product_id),
        field("卖家 (Seller)", ozon.seller_name), field("精准类目 (Category)", ozon.category_path),
        field("单 SKU (Selected SKU)", ozon.target_sku), field("价格 (Price)", `${{ozon.price || "-"}} ${{ozon.currency || ""}}`.trim()),
        field("评分 / 评价 (Rating / Reviews)", `${{ozon.rating || "-"}} / ${{ozon.review_count ?? "-"}}`),
        field("配送证据 (Delivery)", `${{ozon.delivery_origin || "-"}} · ${{ozon.delivery_time || "-"}}`),
        field("中国跨境证据 (Cross-border)", ozon.domestic_seller_decision)
      );
      ozonRecord.append(
        ozonTitle, ozonFields,
        imageGallery(item.ozon_images || [], "Ozon 图片 (Images)"),
        detailsBlock(`属性字段 (Attributes) ${{Object.keys(ozon.attributes || {{}}).length}}`, ozon.attributes),
        detailsBlock("内容分证据 (Content Evidence)", ozon.content_score_evidence),
        detailsBlock("全部 Ozon 原始字段 (Raw Ozon Data)", ozon)
      );
      ozonBody.append(ozonRecord);

      const supplierRecord = document.createElement("section"); supplierRecord.className = "evidence-record";
      const supplierTitle = document.createElement("h4"); supplierTitle.className = "record-title";
      supplierTitle.textContent = `${{index + 1}}. ${{supplier ? (supplier.title || supplier.offer_id || item.seed_id) : item.seed_id}} · ${{supplier ? (supplierMissing.length ? `缺失 ${{supplierMissing.join(", ")}}` : "完整") : "等待采集"}}`;
      supplierRecord.append(supplierTitle);
      if (!supplier) {{
        supplierRecord.append(field("1688 数据", null));
      }} else {{
        const supplierFields = document.createElement("div"); supplierFields.className = "field-grid";
        supplierFields.append(
          field("商品标题 (Title)", supplier.title), field("Offer ID", supplier.offer_id),
          field("供应商 (Seller)", supplier.seller), field("价格 (Price)", supplier.price),
          field("SKU 证据 (SKU)", supplier.sku), field("国内运费 (Shipping)", supplier.domestic_shipping_evidence),
          field("1688 链接 (URL)", supplier.final_url || supplier.supplier_url)
        );
        supplierRecord.append(
          supplierFields,
          imageGallery(supplier.images || [], "供应商商品图 (Product Images)"),
          detailsBlock(`属性字段 (Attributes) ${{Object.keys(supplier.attributes || {{}}).length}}`, supplier.attributes),
          detailsBlock("全部 1688 原始字段 (Raw Supplier Data)", supplier)
        );
      }}
      supplierBody.append(supplierRecord);
      ozonBody.scrollTop = resetScroll ? 0 : previousOzonScroll;
      supplierBody.scrollTop = resetScroll ? 0 : previousSupplierScroll;
      renderSkuDecision();
    }}

    function moveEvidence(delta) {{
      const next = Math.max(0, Math.min(state.evidenceIndex + delta, state.items.length - 1));
      if (next === state.evidenceIndex) return;
      state.evidenceIndex = next;
      renderEvidence({{ resetScroll: true }});
    }}

    function skuFieldName(key) {{
      const normalized = String(key || "").toLowerCase();
      if (/количеств|упаков|quantity|count|数量|件数/.test(normalized)) return "包装数量";
      if (/размер|длина|ширина|высота|dimension|size|length|width|height|尺寸|长度|宽度|高度/.test(normalized)) return "尺寸";
      if (/цвет|color|colour|颜色/.test(normalized)) return "颜色";
      if (/комплект|модель|model|configuration|套装|型号|规格/.test(normalized)) return "套装 / 型号";
      return String(key || "规格");
    }}

    function skuPriceText(price) {{
      if (!price || typeof price !== "object" || Array.isArray(price)) return valueText(price);
      const amount = String(price.amount || "").trim();
      const visible = String(price.visible_text || "").trim();
      if (!amount) return visible || "价格未采集";
      const currencySymbols = {{ CNY:"¥", RUB:"₽", USD:"$", EUR:"€" }};
      const currency = String(price.currency || "").trim().toUpperCase();
      return `${{currencySymbols[currency] || (currency ? `${{currency}} ` : "")}}${{amount}}`;
    }}

    function skuStockText(stock) {{
      if (!stock || typeof stock !== "object" || Array.isArray(stock)) return `库存：${{valueText(stock)}}`;
      const labels = {{ in_stock:"有货", out_of_stock:"缺货", unknown:"库存未知" }};
      const status = labels[String(stock.status || "")] || "库存状态未采集";
      return stock.quantity == null ? status : `${{status}} · 库存 ${{stock.quantity}}`;
    }}

    function skuDecisionFacts(source) {{
      const payload = source && typeof source === "object" && !Array.isArray(source) ? source : {{}};
      return Object.entries(payload).map(([key, value]) => ({{
        key:String(key),
        label:skuFieldName(key),
        value:valueText(value),
      }})).filter((fact) => fact.value && fact.value !== "-");
    }}

    function skuTargetFacts(item) {{
      const facts = [];
      const title = String(item.ozon_title || (item.ozon_product && item.ozon_product.title) || "").trim();
      if (title) facts.push({{ key:"ozon_title", label:"Ozon 标题", value:title }});
      return facts.concat(skuDecisionFacts(item.selected_options || {{}}));
    }}

    function skuComparableTokens(facts) {{
      const tokens = new Set();
      facts.forEach((fact) => {{
        const raw = String(fact.value || "").trim().toLowerCase();
        const compact = raw.replace(/\\s+/g, "");
        if (compact.length >= 2) tokens.add(`值:${{compact}}`);
        if (fact.label === "包装数量") {{
          const quantity = raw.match(/\\d+(?:[.,]\\d+)?/);
          if (quantity) tokens.add(`包装数量:${{quantity[0].replace(",", ".")}}`);
        }}
        for (const match of raw.matchAll(/(\\d+(?:[.,]\\d+)?)\\s*(mm|cm|m|毫米|厘米|米|件|支|个|只|套)?/gi)) {{
          const number = Number(match[1].replace(",", "."));
          const unit = String(match[2] || "").toLowerCase();
          if (number > 1 || unit) tokens.add(`数值:${{number}}${{unit}}`);
        }}
      }});
      return tokens;
    }}

    function analyzeSupplierSkuOptions(item, options) {{
      const targetFacts = skuTargetFacts(item);
      const targetTokens = skuComparableTokens(targetFacts);
      const serverDecision = item.supplier_sku_decision || {{}};
      const serverCandidates = serverDecision.candidates || {{}};
      const candidates = (options || []).map((option) => {{
        const facts = skuDecisionFacts(option.selected_options || {{}});
        if (option.set_quantity) facts.push({{ key:"set_quantity", label:"包装数量", value:`${{option.set_quantity}} 件` }});
        const tokens = skuComparableTokens(facts);
        const serverCandidate = serverCandidates[String(option.supplier_sku_id || "")] || null;
        const matches = serverCandidate
          ? (serverCandidate.matched_measurements || []).map((token) => `尺寸:${{token}}`)
          : Array.from(tokens).filter((token) => targetTokens.has(token));
        const strongMatches = matches.filter((token) => !token.startsWith("包装数量:"));
        return {{ option, facts, matches, score:serverCandidate ? Number(serverCandidate.score || 0) : strongMatches.length }};
      }});
      const ranked = [...candidates].sort((left, right) => right.score - left.score);
      const best = ranked[0] || null;
      const second = ranked[1] || null;
      const recommendedSkuId = String(serverDecision.recommended_sku_id || (
        best && best.score > 0 && (!second || best.score > second.score)
          ? String(best.option.supplier_sku_id || "")
          : ""
      ));
      return {{ targetFacts, candidates, recommendedSkuId }};
    }}

    function renderSupplierSkuCandidate(candidate, context) {{
      const option = candidate.option;
      const label = document.createElement("label"); label.className = "sku-option";
      if (context.analysis.recommendedSkuId === option.supplier_sku_id) label.classList.add("recommended");
      const radio = document.createElement("input"); radio.type = "radio"; radio.name = "supplierSku"; radio.value = option.supplier_sku_id || "";
      radio.checked = (!!context.lockedSku && context.lockedSku.supplier_sku_id === option.supplier_sku_id)
        || (!context.receipt && (context.singlePageSku || context.analysis.recommendedSkuId === option.supplier_sku_id));
      radio.disabled = !!context.receipt;
      radio.addEventListener("change", updateSkuLockButton);
      const main = document.createElement("div"); main.className = "sku-option-main";
      const imageUrl = Array.isArray(option.image_urls) ? String(option.image_urls[0] || "") : "";
      if (imageUrl) {{
        const image = document.createElement("img"); image.className = "sku-option-image"; image.src = imageUrl; image.alt = option.raw_label || "1688 SKU"; image.loading = "lazy";
        main.append(image);
      }} else {{
        main.classList.add("no-image");
      }}
      const copy = document.createElement("div");
      if (context.analysis.recommendedSkuId === option.supplier_sku_id) {{
        const badge = document.createElement("span"); badge.className = "sku-badge"; badge.textContent = "建议选择 (Recommended)"; copy.append(badge);
      }}
      const title = document.createElement("strong"); title.textContent = candidate.facts.map((fact) => `${{fact.label}}：${{fact.value}}`).join(" · ") || option.raw_label || option.combination_key || option.supplier_sku_id;
      const composition = document.createElement("span"); composition.textContent = `套装数量 ${{option.set_quantity || "-"}} · ${{(option.set_composition || []).join(" / ") || valueText(option.selected_options)}}`;
      const price = document.createElement("span"); price.textContent = `价格 ${{skuPriceText(option.price)}} · ${{skuStockText(option.stock)}}`;
      const comparison = document.createElement("span"); comparison.className = "sku-comparison"; comparison.textContent = candidate.matches.length
        ? `与 Ozon 直接重合：${{candidate.matches.map((token) => token.split(":").slice(1).join(":" )).join("、")}}`
        : "未发现可直接核对的一致字段";
      copy.append(title, composition, price, comparison); main.append(copy); label.append(radio, main);
      return label;
    }}

    function renderSupplierSkuGroup(root, titleText, candidates, context) {{
      if (!candidates.length) return;
      const section = document.createElement("section"); section.className = "sku-candidate-section";
      const title = document.createElement("h4"); title.className = "sku-candidate-section-title"; title.textContent = titleText;
      const grid = document.createElement("div"); grid.className = "sku-candidate-grid";
      candidates.forEach((candidate) => grid.append(renderSupplierSkuCandidate(candidate, context)));
      section.append(title, grid); root.append(section);
    }}

    function appendSkuFacts(root, facts) {{
      const factsRoot = document.createElement("div"); factsRoot.className = "sku-facts";
      facts.forEach((fact) => {{
        const node = document.createElement("div"); node.className = "sku-fact";
        const label = document.createElement("span"); label.textContent = `${{fact.label}}：`;
        const value = document.createElement("strong"); value.textContent = fact.value;
        node.append(label, value); factsRoot.append(node);
      }});
      if (!facts.length) factsRoot.append(field("Ozon 目标规格", null));
      root.append(factsRoot);
    }}

    function selectedSkuOption(item) {{
      const selected = document.querySelector('input[name="supplierSku"]:checked');
      return (item.supplier_sku_options || []).find((option) => option.supplier_sku_id === (selected && selected.value));
    }}

    function updateSkuLockButton() {{
      const item = state.items[state.evidenceIndex];
      const button = $("lockSupplierSku");
      if (!item) {{ button.disabled = true; return; }}
      const options = item.supplier_sku_options || [];
      const receipt = item.supplier_sku_selection || null;
      const singlePageSku = options.length === 1 && options[0].evidence_source === "single_sku_detail_page";
      button.textContent = receipt
        ? "SKU 已确认 (Confirmed)"
        : singlePageSku
          ? "确认页面唯一 SKU"
          : "确认所选 SKU";
      button.disabled = !!receipt || !selectedSkuOption(item);
    }}

    function selectedSubjectEvidenceUrls() {{
      return Array.from(document.querySelectorAll('input[name="subjectEvidence"]:checked'))
        .map((input) => input.value)
        .filter(Boolean);
    }}

    function updateSubjectEvidenceCount() {{
      const selectedCount = selectedSubjectEvidenceUrls().length;
      $("subjectEvidenceCount").textContent = `已选 ${{selectedCount}} 张 (${{selectedCount}} Selected)`;
      $("confirmSubjectMaster").disabled = !!state.items[state.evidenceIndex]?.subject_master || selectedCount === 0;
    }}

    function renderSkuDecision() {{
      const optionsRoot = $("skuOptions");
      const subjectRoot = $("subjectMasterImages");
      optionsRoot.replaceChildren();
      subjectRoot.replaceChildren();
      if (!state.items.length) {{
        $("skuDecisionContext").textContent = "等待 1688 采集结果";
        $("skuDecisionMessage").textContent = "尚无可审核商品。";
        $("lockSupplierSku").disabled = true;
        $("subjectMasterArea").hidden = true;
        return;
      }}
      const item = state.items[state.evidenceIndex];
      const options = item.supplier_sku_options || [];
      const skuGroups = item.supplier_sku_groups || [];
      const receipt = item.supplier_sku_selection || null;
      const lockedSku = receipt && receipt.supplier_sku ? receipt.supplier_sku : null;
      const subjectMaster = item.subject_master || null;
      const skuNeedsConfirmation = !!item.supplier_product && !options.length && !receipt;
      const singlePageSku = options.length === 1 && options[0].evidence_source === "single_sku_detail_page";
      const analysis = analyzeSupplierSkuOptions(item, options);
      $("skuDecisionContext").textContent = `${{state.evidenceIndex + 1}} / ${{state.items.length}} · ${{item.ozon_title || item.seed_id}}`;
      $("skuDecisionStatus").textContent = subjectMaster
        ? "主体已锁定 (Subject Locked)"
        : receipt
          ? "SKU 已锁定 (SKU Locked)"
          : skuNeedsConfirmation
            ? "SKU 待人工确认 (Manual SKU Review)"
            : "未锁定 (Unlocked)";
      $("skuDecisionStatus").className = `status-pill ${{subjectMaster ? "success" : ""}}`;

      const target = document.createElement("section"); target.className = "sku-target";
      const targetTitle = document.createElement("h3"); targetTitle.className = "sku-section-title"; targetTitle.textContent = "Ozon 原商品目标";
      target.append(targetTitle); appendSkuFacts(target, analysis.targetFacts); optionsRoot.append(target);

      const candidateTitle = document.createElement("h3"); candidateTitle.className = "sku-section-title"; candidateTitle.textContent = "1688 可采购规格";
      optionsRoot.append(candidateTitle);
      const strongestScore = Math.max(0, ...analysis.candidates.map((candidate) => candidate.score));
      const matchingCandidates = analysis.candidates.filter((candidate) => strongestScore > 0 && candidate.score === strongestScore);
      const otherCandidates = analysis.candidates.filter((candidate) => !matchingCandidates.includes(candidate));
      const modeNote = document.createElement("div"); modeNote.className = "sku-mode-note";
      modeNote.textContent = singlePageSku
        ? "页面只有一个真实 SKU，无需选择规格；请核对商品和数量后确认。"
        : matchingCandidates.length > 1
          ? `已根据 Ozon 标题和属性收窄为 ${{matchingCandidates.length}} 项；只需判断剩余差异，其他 ${{otherCandidates.length}} 项已折叠。`
        : options.length > 1 && !analysis.recommendedSkuId
          ? "系统没有找到可证明的唯一对应项，不会替你猜。全部候选默认折叠，请对照 Ozon 目标后再展开选择。"
          : analysis.recommendedSkuId
            ? "系统根据明确重合字段标出建议项；请核对后再确认。"
            : "等待可确认的 1688 SKU 证据。";
      if (options.length > 1 && !analysis.recommendedSkuId && !matchingCandidates.length) modeNote.classList.add("warning");
      optionsRoot.append(modeNote);

      const candidatesRoot = document.createElement("div"); candidatesRoot.className = "sku-candidate-list";
      const candidateContext = {{ analysis, receipt, lockedSku, singlePageSku }};
      const lockedCandidate = lockedSku
        ? analysis.candidates.find((candidate) => candidate.option.supplier_sku_id === lockedSku.supplier_sku_id)
        : null;
      if (lockedCandidate && !matchingCandidates.includes(lockedCandidate)) {{
        renderSupplierSkuGroup(candidatesRoot, "当前已锁定 SKU", [lockedCandidate], candidateContext);
      }}
      if (singlePageSku) {{
        renderSupplierSkuGroup(candidatesRoot, "页面唯一 SKU", analysis.candidates, candidateContext);
      }} else if (matchingCandidates.length) {{
        renderSupplierSkuGroup(candidatesRoot, `符合 Ozon 目标（${{matchingCandidates.length}}）`, matchingCandidates, candidateContext);
      }}
      const hiddenCandidates = matchingCandidates.length
        ? otherCandidates.filter((candidate) => candidate !== lockedCandidate)
        : analysis.candidates.filter((candidate) => candidate !== lockedCandidate);
      if (!singlePageSku && hiddenCandidates.length) {{
        const otherDetails = document.createElement("details"); otherDetails.className = "sku-other-options";
        const summary = document.createElement("summary"); summary.textContent = matchingCandidates.length
          ? `查看其他规格（${{hiddenCandidates.length}}）`
          : `查看其他规格（全部 ${{hiddenCandidates.length}} 项）`;
        const otherRoot = document.createElement("div");
        renderSupplierSkuGroup(otherRoot, matchingCandidates.length ? "其他未匹配规格" : "全部真实规格", hiddenCandidates, candidateContext);
        otherDetails.append(summary, otherRoot); candidatesRoot.append(otherDetails);
      }}
      optionsRoot.append(candidatesRoot);
      if (!options.length) {{
        const visibleGroups = skuGroups
          .map((group) => {{
            const name = group && (group.name || group.label || group.group_name);
            const values = group && (group.values || group.options || group.items);
            return `${{name || "规格"}}: ${{Array.isArray(values) ? values.map(valueText).join(" / ") : valueText(values)}}`;
          }})
          .filter(Boolean)
          .join("\\n");
        candidatesRoot.append(field("可见规格组 (Visible SKU Groups)", visibleGroups || null));
      }}
      updateSkuLockButton();
      $("skuDecisionMessage").className = receipt ? "success" : "muted";
      $("skuDecisionMessage").textContent = receipt
        ? "真实供应商 SKU 已锁定，后续采购、标题、属性和图片均以此为准。"
        : skuNeedsConfirmation
          ? "商品公开证据已采集；完整 SKU 矩阵未解析，已转入用户确认门禁，不会伪造 SKU。"
          : singlePageSku
            ? "这是页面唯一 SKU；系统未自动锁定，请由用户确认。"
            : analysis.recommendedSkuId
              ? "建议项只来自明确字段重合；请核对后确认，不创建新套装。"
              : "请选择 1688 页面已经存在的真实 SKU，不创建新套装。";

      $("subjectMasterArea").hidden = !lockedSku;
      if (!lockedSku) return;
      const skuImages = Array.isArray(lockedSku.image_urls) ? lockedSku.image_urls.filter(Boolean) : [];
      const supplierImages = item.supplier_product && Array.isArray(item.supplier_product.images)
        ? item.supplier_product.images.filter(Boolean)
        : [];
      const candidateMap = new Map();
      skuImages.forEach((url) => candidateMap.set(url, {{ url, sources:["SKU 绑定图"] }}));
      supplierImages.forEach((url) => {{
        const candidate = candidateMap.get(url);
        if (candidate) {{
          if (!candidate.sources.includes("供应商商品图")) candidate.sources.push("供应商商品图");
        }} else {{
          candidateMap.set(url, {{ url, sources:["供应商商品图"] }});
        }}
      }});
      const candidates = Array.from(candidateMap.values());
      const savedUrls = subjectMaster
        ? (subjectMaster.source_image_urls || [subjectMaster.source_image_url]).filter(Boolean)
        : [];
      const recommendedUrls = new Set(savedUrls.length ? savedUrls : (skuImages.length ? skuImages : candidates.slice(0, 1).map((item) => item.url)));
      candidates.forEach((candidate, index) => {{
        const url = candidate.url;
        const label = document.createElement("label"); label.className = "subject-choice";
        const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.name = "subjectEvidence"; checkbox.value = url;
        checkbox.checked = recommendedUrls.has(url);
        checkbox.disabled = !!subjectMaster;
        checkbox.addEventListener("change", updateSubjectEvidenceCount);
        const badge = document.createElement("span"); badge.className = "subject-source"; badge.textContent = candidate.sources.join(" / ");
        const image = document.createElement("img"); image.src = url; image.alt = `SKU subject ${{index + 1}}`; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
        label.append(checkbox, badge, image); subjectRoot.append(label);
      }});
      if (!candidates.length) subjectRoot.append(field("供应商主体证据图片", null));
      $("visibleSubjectQuantity").value = subjectMaster ? subjectMaster.visible_subject_quantity : lockedSku.set_quantity;
      $("visibleSubjectQuantity").disabled = !!subjectMaster;
      $("subjectSetComposition").textContent = (lockedSku.set_composition || []).join(" / ") || valueText(lockedSku.selected_options);
      updateSubjectEvidenceCount();
    }}

    async function lockSupplierSku() {{
      const item = state.items[state.evidenceIndex];
      const option = selectedSkuOption(item);
      if (!option) return;
      $("lockSupplierSku").disabled = true;
      try {{
        await api(`/api/batches/${{encodeURIComponent(runId)}}/supplier-sku`, {{
          method:"POST",
          body:JSON.stringify({{
            seed_id:item.seed_id,
            supplier_sku_id:option.supplier_sku_id,
            differences:[{{ field:"sku", ozon:item.selected_options || {{}}, supplier:option.selected_options || {{}} }}]
          }})
        }});
        await load();
      }} catch (error) {{
        $("skuDecisionMessage").className = "error";
        $("skuDecisionMessage").textContent = error.message || "SKU 锁定失败 (Lock Failed)";
        $("lockSupplierSku").disabled = false;
      }}
    }}

    async function confirmSubjectMaster() {{
      const item = state.items[state.evidenceIndex];
      const selectedUrls = selectedSubjectEvidenceUrls();
      if (!selectedUrls.length) return;
      $("confirmSubjectMaster").disabled = true;
      try {{
        const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/subject-master`, {{
          method:"POST",
          body:JSON.stringify({{
            seed_id:item.seed_id,
            source_image_urls:selectedUrls,
            visible_subject_quantity:Number($("visibleSubjectQuantity").value)
          }})
        }});
        if (result.data.all_subject_masters_confirmed === true) {{
          window.location.assign(`/batches/${{encodeURIComponent(runId)}}/images`);
          return;
        }}
        await load();
        const nextIncomplete = state.items.findIndex((candidate) => !candidate.subject_master);
        if (nextIncomplete >= 0) state.evidenceIndex = nextIncomplete;
        renderEvidence({{ resetScroll:true }});
      }} catch (error) {{
        $("skuDecisionMessage").className = "error";
        $("skuDecisionMessage").textContent = error.message || "主体证据确认失败 (Confirmation Failed)";
        $("confirmSubjectMaster").disabled = false;
      }}
    }}

    function render() {{
      $("items").replaceChildren();
      state.items.forEach((item) => {{
        const row = document.createElement("tr");
        const productCell = document.createElement("td");
        const product = document.createElement("div"); product.className = "product";
        const image = document.createElement("img"); image.src = item.ozon_main_image || ""; image.alt = item.ozon_title || "Ozon product"; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
        const details = document.createElement("div");
        const title = document.createElement("strong"); title.textContent = item.ozon_title || item.ozon_product_id;
        const productId = document.createElement("div"); productId.className = "muted"; productId.textContent = `Ozon ID ${{item.ozon_product_id || "-"}}`;
        const link = document.createElement("a"); link.href = item.ozon_url; link.target = "_blank"; link.rel = "noopener"; link.textContent = "打开 Ozon 商品 (Open Product)";
        details.append(title, productId, link);
        product.append(image, details); productCell.append(product);
        const evidenceCell = document.createElement("td");
        evidenceCell.append(evidenceList(Object.keys(item.dimension_evidence || {{}}).length ? item.dimension_evidence : item.selected_options));
        const channelCell = document.createElement("td");
        const channelStatus = document.createElement("strong");
        channelStatus.className = item.supplier_url ? "success" : "muted";
        channelStatus.textContent = item.supplier_url
          ? "已从详情页采集 (Collected)"
          : "等待五通道采集 (Waiting for 5-Lane Collection)";
        const channelHint = document.createElement("div");
        channelHint.className = "muted";
        channelHint.textContent = item.supplier_url
          ? item.supplier_url
          : "在扩展管理的 1688 通道中以图搜索、打开同款详情页并点击采集。";
        channelCell.append(channelStatus, channelHint);
        const actionCell = document.createElement("td");
        const rejectButton = document.createElement("button");
        rejectButton.type = "button";
        rejectButton.className = "reject-button";
        rejectButton.textContent = "找不到供应商 (No Supplier Found)";
        rejectButton.disabled = state.status !== "supplier_review" || !!item.supplier_url;
        rejectButton.addEventListener("click", () => rejectSupplier(item, rejectButton));
        const recaptureButton = document.createElement("button");
        recaptureButton.type = "button";
        recaptureButton.className = "secondary-button";
        recaptureButton.textContent = "重新采集 (Re-collect)";
        recaptureButton.disabled = state.status !== "supplier_review" || !item.supplier_url;
        recaptureButton.addEventListener("click", () => recaptureSupplier(item, recaptureButton));
        actionCell.append(rejectButton, recaptureButton);
        row.append(productCell, evidenceCell, channelCell, actionCell); $("items").append(row);
      }});
      $("evidenceProducts").textContent = String(state.items.length);
      $("evidenceImages").textContent = String(state.items.reduce((total, item) => total + (Array.isArray(item.ozon_images) ? item.ozon_images.length : item.ozon_main_image ? 1 : 0), 0));
      $("evidenceAttributes").textContent = String(state.items.reduce((total, item) => total + Object.keys(item.ozon_attributes || item.attributes || {{}}).length, 0));
      $("evidenceDimensions").textContent = String(state.items.reduce((total, item) => total + Object.keys(item.dimension_evidence || {{}}).length, 0));
      $("evidenceSku").textContent = String(state.items.filter((item) => Object.keys(item.selected_options || {{}}).length > 0).length);
      renderCollectionProgress();
      renderEvidence();
      updateButton();
    }}

    function feedbackItem(label, value) {{
      const item = document.createElement("div"); item.className = "feedback-item";
      const name = document.createElement("span"); name.textContent = label;
      const content = document.createElement("strong"); content.textContent = value || "等待中 (Waiting)";
      item.append(name, content);
      return item;
    }}

    function renderCollectionProgress() {{
      const progress = state.collectionProgress;
      const product = progress && progress.partial_product ? progress.partial_product : null;
      $("feedbackList").replaceChildren();
      $("feedbackEmpty").hidden = !!product;
      if (!product) return;
      const skuLabels = ((product.sku || {{}}).selected_options || {{}}).visible_sku_labels || [];
      const seller = (product.seller || {{}}).shop_name || "";
      const price = (product.price || {{}}).visible_text || "";
      const shipping = (product.domestic_shipping_evidence || {{}}).visible_text || "";
      const missing = (progress.missing_fields || []).join(", ") || "无 (None)";
      $("feedbackList").append(
        feedbackItem("标题 (Title)", product.title),
        feedbackItem("卖家 (Seller)", seller),
        feedbackItem("SKU", skuLabels.join(" / ")),
        feedbackItem("图片 (Images)", String((product.images || []).length)),
        feedbackItem("价格 (Price)", price),
        feedbackItem("国内运费 (Shipping)", shipping),
        feedbackItem("缺失字段 (Missing)", missing)
      );
    }}

    function is1688ProductUrl(value) {{
      return new RegExp("^https://(detail|m)[.]1688[.]com/offer/.+[.]html(?:[?#].*)?$", "i").test(value || "");
    }}

    function updateButton() {{
      const completed = state.items.filter((item) => is1688ProductUrl(item.supplier_url)).length;
      $("linkProgress").textContent = `${{completed}} / ${{state.items.length}}`;
      $("approveCollection").disabled = true;
      const restartable = ["supplier_review", "supplier_collecting"].includes(state.status);
      $("collect").disabled = state.restartPending || !restartable;
      if (restartable) {{
        $("collect").textContent = state.restartPending
          ? "正在重新派发 (Restarting)"
          : "重新启动 1688 采集";
        $("message").className = "muted";
        $("message").textContent = "只重新打开未完成通道；已回传的 1688 商品保持完成。";
        return;
      }}
      if (state.status === "supplier_collected") {{
        $("collect").textContent = "供应商已采集 (Collected)";
        $("message").className = state.canApprove ? "success" : "error";
        $("message").textContent = state.canApprove
          ? "Ozon 与 1688 数据已回传；请逐件锁定真实 SKU 并确认主体证据图，完成后自动进入生图。"
          : "采集结果仍有缺失字段，不能进入图片处理。";
        return;
      }}
      if (state.status === "image_processing") {{
        $("approveCollection").disabled = true;
        $("message").className = "muted";
        $("message").textContent = "本批次已进入图片处理；当前采集结果仍可在此审阅。";
        return;
      }}
      $("collect").textContent = "五通道由扩展执行 (Extension Managed)";
      $("message").className = "muted";
      $("message").textContent = completed === state.items.length && completed > 0
        ? "五通道采集已完成，正在验收并进入下一阶段。"
        : `五通道采集进度 ${{completed}} / ${{state.items.length}}；可在 1688 详情页逐件采集或标记无供应商。`;
    }}

    async function load() {{
      if (loading) return;
      loading = true;
      try {{
        const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/supplier-review`);
        state.items = result.data.items || [];
        state.status = result.data.status || "";
        state.collectionProgress = result.data.collection_progress || null;
        state.canApprove = result.data.can_approve === true;
        $("status").textContent = result.data.status === "supplier_review"
          ? "等待用户核实 (Waiting for Verification)"
          : result.data.status === "supplier_collected"
            ? "等待采集审核 (Waiting for Review)"
            : result.data.status;
        render();
      }} finally {{ loading = false; }}
    }}

    async function rejectSupplier(item, button) {{
      const confirmed = window.confirm(
        `确认该 Ozon 商品找不到完全一致的 1688 供应商？\n\n${{item.ozon_title || item.ozon_product_id}}\n\n系统将永久禁用该种子和 Ozon 商品，并自动补采 1 个新品。`
      );
      if (!confirmed) return;
      button.disabled = true;
      $("message").className = "muted";
      $("message").textContent = "正在加入黑名单并补足采集队列 (Refilling Queue)";
      try {{
        await api(`/api/batches/${{encodeURIComponent(runId)}}/supplier-review/reject`, {{
          method: "POST",
          body: JSON.stringify({{
            seed_id: item.seed_id,
            reason: "User could not find an exact 1688 supplier.",
          }}),
        }});
        window.location.assign(`/?run_id=${{encodeURIComponent(runId)}}`);
      }} catch (error) {{
        button.disabled = false;
        $("message").className = "error";
        $("message").textContent = error.message || "补位失败 (Replacement Failed)";
      }}
    }}

    async function recaptureSupplier(item, button) {{
      const confirmed = window.confirm(
        `清除当前供应商结果并重新打开该商品的 1688 采集通道？\n\n${{item.ozon_title || item.ozon_product_id}}`
      );
      if (!confirmed) return;
      button.disabled = true;
      $("message").className = "muted";
      $("message").textContent = "正在清除错误回传并重新派发独立采集通道 (Re-collecting)";
      try {{
        await api(`/api/batches/${{encodeURIComponent(runId)}}/supplier-selection/reset`, {{
          method: "POST",
          body: JSON.stringify({{ seed_id: item.seed_id }}),
        }});
        await api(`/api/batches/${{encodeURIComponent(runId)}}/browser-task/restart`, {{
          method: "POST",
          body: JSON.stringify({{}}),
        }});
        await load();
      }} catch (error) {{
        button.disabled = false;
        $("message").className = "error";
        $("message").textContent = error.message || "重新采集派发失败 (Re-collect Failed)";
      }}
    }}

    async function restartSupplierCollection() {{
      if (state.restartPending) return;
      state.restartPending = true;
      updateButton();
      try {{
        const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/browser-task/restart`, {{
          method: "POST",
          body: JSON.stringify({{}})
        }});
        state.restartPending = false;
        await load();
        $("message").className = "muted";
        $("message").textContent = result.data.dispatch_state === "waiting_for_extension"
          ? "恢复请求已保存，正在等待浏览器扩展上线。"
          : `已保留 ${{result.data.completed_count}} 个通道，继续 ${{result.data.pending_count}} 个通道。`;
      }} catch (error) {{
        state.restartPending = false;
        updateButton();
        $("message").className = "error";
        $("message").textContent = error.message || "1688 采集恢复失败。";
      }}
    }}

    $("collect").addEventListener("click", restartSupplierCollection);
    $("lockSupplierSku").addEventListener("click", lockSupplierSku);
    $("confirmSubjectMaster").addEventListener("click", confirmSubjectMaster);
    $("ozonEvidencePrev").addEventListener("click", () => moveEvidence(-1));
    $("ozonEvidenceNext").addEventListener("click", () => moveEvidence(1));
    $("supplierEvidencePrev").addEventListener("click", () => moveEvidence(-1));
    $("supplierEvidenceNext").addEventListener("click", () => moveEvidence(1));
    async function poll() {{
      if (!["supplier_review", "supplier_collecting"].includes(state.status)) return;
      await load();
    }}

    load().catch((error) => {{
      $("status").textContent = "加载失败 (Load Failed)";
      $("status").className = "status-pill error";
      $("ozonEvidenceStatus").textContent = "加载失败 (Load Failed)";
      $("supplierEvidenceStatus").textContent = "加载失败 (Load Failed)";
      $("message").className = "error";
      $("message").textContent = error.message || String(error);
    }});
    setInterval(poll, 1500);
  </script>
</body>
</html>"""


def build_image_workspace_html(run_id: str) -> str:
    safe_run_id = json.dumps(run_id)
    worker_ids = "、".join(REGULAR_IMAGE_WORKER_IDS)
    subagent_mappings = "、".join(
        f"ozon_image_worker_{index:02d} -> {worker_id}"
        for index, worker_id in enumerate(REGULAR_IMAGE_WORKER_IDS, start=1)
    )
    controller_command = (
        f"\u542f\u52a8 Ozon V2 \u751f\u56fe\u603b\u63a7\uff1a\u6279\u6b21 {run_id}\u3002"
        "\u8bfb\u53d6\u5e76\u4e25\u683c\u6267\u884c\u5de5\u4f5c\u533a\u6280\u80fd skills/ozon-image-generation-controller/SKILL.md\uff1b"
        f"只在当前 Codex 总控任务内部使用 spawn_agent 创建并复用最多 5 个动态生图子智能体；每轮新增数量取 5 减现有生图子智能体数、尚未分派商品数、当前空闲内部并发位数三者最小值，映射为 {subagent_mappings}；"
        "\u4e25\u7981\u4f7f\u7528 create_thread\u3001fork_thread \u6216\u4efb\u4f55\u4f1a\u5728\u4fa7\u8fb9\u680f\u521b\u5efa\u7528\u6237\u53ef\u89c1\u4efb\u52a1\u6216\u7ebf\u7a0b\u7684\u63a5\u53e3\uff1b"
        "可用并发位少于 5 时继续使用所有成功创建的子智能体，不得停机；不得因没有新增空位而停止现有子智能体；若 spawn_agent 失败则缩小到成功数量；只有当前无可用并发位且尚无现有子智能体时才不领取任务并报告等待，不得退回 create_thread 或用户可见任务；"
        f"\u6bcf\u4e2a\u5b50\u667a\u80fd\u4f53\u53ea\u80fd\u7528\u6620\u5c04\u7684\u961f\u5217 worker ID \u9886\u53d6\u4efb\u52a1\uff1a{worker_ids}\uff1b\u5b50\u667a\u80fd\u4f53\u7a7a\u95f2\u540e\u4f7f\u7528 followup_task \u7ee7\u7eed\u6d3e\u53d1\uff1b"
        "\u6301\u7eed\u9886\u53d6\u5f53\u524d\u6279\u6b21\u961f\u5217\u4efb\u52a1\uff0c\u76f4\u5230\u961f\u5217\u4e3a\u7a7a\u3001\u8fdb\u5165\u4eba\u5de5\u5ba1\u6838\u6216\u9047\u5230\u963b\u585e\u95e8\u7981\u3002"
        "不得上传，不得修改业务代码；不得创建第 6 个常规生图 worker；当运行时提供第 6 个子智能体并发位时，保留 1 个子智能体位置用于失败恢复、诊断或人工介入。"
    )
    controller_command_html = html.escape(controller_command)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>图片处理 (Images) - Ozon V2</title>
  <style>
    :root {{ color-scheme:light; --canvas:#f3f5f8; --surface:#fff; --sidebar:#182130; --hover:#253247; --text:#182235; --muted:#68758a; --line:#dce2ea; --soft:#f8fafc; --blue:#2457d6; --blue-soft:#eaf0ff; --green:#087a62; --green-soft:#e7f6f1; --amber:#a45b08; --amber-soft:#fff3dc; --red:#b4232c; --red-soft:#fdebed; }}
    * {{ box-sizing:border-box; }}
    html,body {{ margin:0; min-height:100%; color:var(--text); background:var(--canvas); font:14px/1.45 "Segoe UI","Microsoft YaHei",Arial,sans-serif; }}
    button {{ font:inherit; letter-spacing:0; }}
    .app-shell {{ min-height:100vh; display:grid; grid-template-columns:224px minmax(0,1fr); }}
    .sidebar {{ position:sticky; top:0; height:100vh; padding:18px 12px; color:#f7f9fc; background:var(--sidebar); overflow:auto; z-index:20; }}
    .sidebar-brand {{ padding:4px 10px 20px; border-bottom:1px solid rgba(255,255,255,.09); }}
    .sidebar-brand strong {{ display:block; font-size:17px; }} .sidebar-brand span {{ color:#9ba8ba; font-size:11px; }}
    .sidebar-group {{ margin-top:18px; }} .sidebar-label {{ padding:0 10px 7px; color:#7f8da1; font-size:10px; font-weight:700; text-transform:uppercase; }}
    .sidebar-link {{ min-height:38px; display:flex; align-items:center; gap:10px; padding:0 10px; border-radius:5px; color:#c7d0dc; text-decoration:none; font-size:12px; }}
    .sidebar-link:hover {{ color:#fff; background:var(--hover); }} .sidebar-link.active {{ color:#fff; background:var(--blue); }} .sidebar-link.disabled {{ color:#66758a; pointer-events:none; }}
    .nav-mark {{ width:18px; height:18px; display:inline-flex; align-items:center; justify-content:center; border:1px solid currentColor; border-radius:4px; font-size:9px; font-weight:700; }}
    .app-main {{ min-width:0; }}
    .topbar {{ position:sticky; top:0; z-index:12; height:56px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:0 18px; border-bottom:1px solid var(--line); background:rgba(255,255,255,.96); backdrop-filter:blur(8px); }}
    .topbar h1 {{ margin:0; font-size:16px; }} .topbar p {{ margin:2px 0 0; color:var(--muted); font-size:11px; }}
    .pill {{ min-height:24px; display:inline-flex; align-items:center; padding:2px 8px; border-radius:4px; color:var(--green); background:var(--green-soft); font-size:12px; font-weight:650; }}
    .stage-nav {{ border-bottom:1px solid var(--line); background:var(--surface); }}
    .stage-nav-inner {{ min-height:46px; display:flex; align-items:stretch; gap:6px; padding:0 18px; }}
    .stage-link {{ display:inline-flex; align-items:center; padding:0 14px; border-bottom:3px solid transparent; color:var(--text); text-decoration:none; font-size:13px; white-space:nowrap; }}
    .stage-link.active {{ color:var(--blue); border-bottom-color:var(--blue); background:var(--blue-soft); }} .stage-link.disabled {{ color:#a4adbd; pointer-events:none; }}
    .stage-run {{ margin-left:auto; display:inline-flex; align-items:center; color:var(--muted); font:12px Consolas,"Courier New",monospace; }}
    .workspace {{ padding:18px; }}
    .page-head {{ min-height:72px; display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:14px; padding:14px 16px; border:1px solid var(--line); border-radius:6px; background:var(--surface); }}
    .page-head h2 {{ margin:0; font-size:18px; }} .page-head p {{ margin:4px 0 0; color:var(--muted); font-size:12px; }}
    .summary {{ display:grid; grid-template-columns:repeat(4,minmax(120px,1fr)); margin-bottom:14px; overflow:hidden; border:1px solid var(--line); border-radius:6px; background:var(--surface); }}
    .metric {{ min-height:76px; display:flex; flex-direction:column; justify-content:center; padding:12px 15px; border-right:1px solid var(--line); }} .metric:last-child {{ border-right:0; }}
    .metric span {{ color:var(--muted); font-size:11px; }} .metric strong {{ margin-top:4px; font-size:19px; }}
    .image-layout {{ display:grid; grid-template-columns:minmax(0,1fr) 280px; gap:14px; align-items:start; }}
    .panel {{ min-width:0; border:1px solid var(--line); border-radius:6px; background:var(--surface); overflow:hidden; }}
    .panel-head {{ min-height:50px; display:flex; align-items:center; justify-content:space-between; gap:10px; padding:0 14px; border-bottom:1px solid var(--line); }} .panel-head h3 {{ margin:0; font-size:14px; }}
    .image-items {{ display:grid; grid-template-rows:minmax(0,1fr); gap:12px; padding:12px; }}
    .image-items-viewport {{ height:clamp(460px,calc(100vh - 310px),620px); min-height:0; overflow:hidden; }}
    .image-pager {{ display:flex; align-items:center; gap:7px; }}
    .image-pager button {{ width:30px; height:30px; display:grid; place-items:center; padding:0; border:1px solid var(--line); border-radius:4px; color:var(--text); background:#fff; cursor:pointer; }}
    .image-pager button:hover:not(:disabled) {{ color:var(--blue); border-color:#9db3ef; background:var(--blue-soft); }}
    .image-pager button:disabled {{ color:#a4adbd; background:var(--soft); cursor:not-allowed; }}
    .image-position {{ min-width:54px; color:var(--muted); text-align:center; font:11px Consolas,"Courier New",monospace; }}
    .image-product {{ height:100%; min-height:0; display:grid; grid-template-rows:auto minmax(0,1fr); border:1px solid var(--line); border-radius:5px; overflow:hidden; background:#fff; }}
    .product-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 12px; border-bottom:1px solid var(--line); background:var(--soft); }}
    .product-head strong {{ font-size:13px; }} .product-head span {{ color:var(--muted); font-size:10px; overflow-wrap:anywhere; }}
    .source-grid {{ min-height:0; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); grid-template-rows:minmax(0,1fr); gap:1px; background:var(--line); }}
    .source-panel {{ min-width:0; min-height:0; display:grid; grid-template-rows:auto minmax(0,1fr); padding:11px; overflow:hidden; background:#fff; }}
    .source-title {{ display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:9px; font-size:11px; font-weight:700; }} .source-title span {{ color:var(--muted); font-weight:400; }}
    .gallery {{ min-height:0; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); grid-auto-rows:minmax(150px,190px); align-content:start; gap:8px; padding-right:4px; overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; }}
    .gallery img {{ width:100%; height:100%; min-height:150px; max-height:190px; object-fit:contain; border:1px solid var(--line); border-radius:4px; background:#fff; }}
    .generated-review-panel {{ grid-template-rows:auto minmax(0,1fr); }}
    .generated-review-grid {{ min-height:0; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); align-content:start; gap:8px; padding-right:4px; overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; }}
    .generated-review-card {{ min-width:0; padding:7px; border:1px solid var(--line); border-radius:5px; background:#fff; transition:border-color .15s ease,box-shadow .15s ease; }}
    .generated-review-card.selected {{ border-color:#e07b83; box-shadow:0 0 0 2px rgba(180,35,44,.08); }}
    .generated-review-card.repairing {{ border-color:#e4b862; background:#fffaf0; }}
    .generated-review-head {{ display:flex; align-items:center; justify-content:space-between; gap:6px; margin-bottom:6px; font:10px Consolas,"Courier New",monospace; }}
    .generated-review-head strong {{ color:var(--text); }}
    .repair-badge {{ padding:2px 5px; border-radius:3px; color:var(--green); background:var(--green-soft); font:9px "Segoe UI","Microsoft YaHei",sans-serif; }}
    .repairing .repair-badge {{ color:var(--amber); background:var(--amber-soft); }}
    .generated-review-card img {{ width:100%; height:108px; display:block; object-fit:contain; border:1px solid var(--line); border-radius:4px; background:#fff; }}
    .repair-check {{ display:flex; align-items:flex-start; gap:6px; margin-top:7px; color:var(--text); font-size:10px; cursor:pointer; }}
    .repair-check input {{ margin:2px 0 0; accent-color:var(--red); }}
    .repair-fields {{ display:grid; gap:5px; margin-top:7px; }}
    .repair-fields[hidden] {{ display:none; }}
    .repair-issue,.repair-note {{ width:100%; border:1px solid var(--line); border-radius:4px; color:var(--text); background:#fff; font:10px/1.35 "Segoe UI","Microsoft YaHei",sans-serif; }}
    .repair-issue {{ min-height:30px; padding:0 6px; }}
    .repair-note {{ min-height:56px; padding:6px; resize:vertical; }}
    .repair-feedback-summary {{ margin-top:7px; padding:6px; border-radius:4px; color:#74410a; background:var(--amber-soft); font-size:9px; overflow-wrap:anywhere; }}
    .empty {{ min-height:0; height:100%; display:grid; place-items:center; padding:18px; border:1px dashed #c9d1dc; border-radius:4px; color:var(--muted); background:var(--soft); text-align:center; font-size:11px; }}
    .generation-empty {{ color:var(--amber); background:#fffaf0; border-color:#e8c98f; }}
    .gate-list {{ padding:6px 14px 14px; }} .gate-row {{ display:grid; grid-template-columns:22px minmax(0,1fr); gap:9px; padding:11px 0; border-bottom:1px solid #edf0f4; }}
    .gate-row:last-child {{ border-bottom:0; }} .gate-icon {{ width:22px; height:22px; display:grid; place-items:center; border-radius:50%; color:var(--green); background:var(--green-soft); font-size:11px; font-weight:700; }}
    .gate-row.blocked .gate-icon {{ color:var(--amber); background:var(--amber-soft); }} .gate-row strong {{ display:block; font-size:11px; }} .gate-row span {{ display:block; margin-top:2px; color:var(--muted); font-size:10px; }}
    .gate-callout {{ margin:0 14px 14px; padding:11px 12px; border-left:3px solid var(--amber); color:#74410a; background:#fff8eb; font-size:11px; }}
    .controller-panel {{ margin:0 14px 14px; padding:10px 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); }}
    .controller-panel strong {{ display:block; font-size:11px; }} .controller-panel p {{ margin:3px 0 8px; color:var(--muted); font-size:10px; }}
    .controller-command {{ max-height:104px; overflow:auto; padding:9px; border:1px solid var(--line); border-radius:4px; background:var(--soft); font:10px/1.45 Consolas,"Courier New",monospace; white-space:pre-wrap; overflow-wrap:anywhere; user-select:text; }}
    .controller-actions {{ display:flex; align-items:center; gap:8px; margin-top:8px; }}
    .controller-actions button {{ min-height:32px; padding:0 10px; border:1px solid var(--blue); border-radius:4px; color:#fff; background:var(--blue); cursor:pointer; }}
    .copy-status {{ color:var(--green); font-size:10px; }}
    .primary {{ width:calc(100% - 28px); height:38px; margin:0 14px 14px; border:1px solid var(--blue); border-radius:5px; color:#fff; background:var(--blue); }} .primary:disabled {{ opacity:.48; cursor:not-allowed; }}
    .job-controls {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:0 14px 14px; }}
    .job-controls button {{ min-height:36px; border:1px solid var(--line); border-radius:5px; background:#fff; cursor:pointer; }}
    .job-controls button:disabled {{ opacity:.45; cursor:not-allowed; }}
    .job-controls .repair-submit {{ grid-column:1/-1; border-color:var(--red); color:#fff; background:var(--red); font-weight:650; }}
    .repair-selection-status {{ grid-column:1/-1; min-height:16px; color:var(--muted); font-size:10px; }}
    .repair-selection-status.error {{ color:var(--red); }}
    .repair-selection-status.success {{ color:var(--green); }}
    .job-status {{ grid-column:1/-1; color:var(--muted); font:11px Consolas,"Courier New",monospace; overflow-wrap:anywhere; }}
    .error {{ color:var(--red); }}
    @media(max-width:1050px) {{ .image-layout {{ grid-template-columns:1fr; }} .source-grid {{ grid-template-columns:1fr 1fr; }} .source-panel:last-child {{ grid-column:1/-1; }} }}
    @media(max-width:900px) {{ .app-shell {{ grid-template-columns:72px minmax(0,1fr); }} .sidebar {{ padding:14px 8px; }} .sidebar-brand {{ padding:4px 4px 16px; text-align:center; }} .sidebar-brand strong {{ font-size:14px; }} .sidebar-brand span,.sidebar-label,.sidebar-link span:last-child {{ display:none; }} .sidebar-link {{ justify-content:center; padding:0; }} .stage-nav-inner {{ overflow-x:auto; padding:0 10px; }} .stage-run {{ display:none; }} .summary {{ grid-template-columns:repeat(2,1fr); }} .metric:nth-child(2) {{ border-right:0; }} .metric:nth-child(-n+2) {{ border-bottom:1px solid var(--line); }} }}
    @media(max-width:620px) {{ .app-shell {{ display:block; }} .sidebar {{ position:static; width:100%; height:auto; display:flex; align-items:center; gap:6px; overflow-x:auto; }} .sidebar-brand {{ min-width:92px; padding:0 8px; border:0; }} .sidebar-group {{ display:flex; gap:4px; margin:0; }} .sidebar-link {{ width:38px; flex:0 0 38px; }} .workspace {{ padding:10px; }} .page-head {{ align-items:flex-start; flex-direction:column; }} .image-items-viewport {{ height:70vh; overflow:auto; }} .image-product {{ height:auto; }} .source-grid {{ grid-template-columns:1fr; grid-template-rows:repeat(3,320px); }} .source-panel:last-child {{ grid-column:auto; }} }}
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar" aria-label="主菜单 (Primary Navigation)">
      <div class="sidebar-brand"><strong>Ozon V2</strong><span>运营驾驶舱 (Operations Cockpit)</span></div>
      <div class="sidebar-group"><div class="sidebar-label">运营 (Operations)</div>
        <a class="sidebar-link" href="/?run_id={run_id}"><span class="nav-mark">B</span><span>批次总览 (Batch)</span></a>
        <a class="sidebar-link" href="/batches/{run_id}/supplier-review"><span class="nav-mark">S</span><span>供应商审核 (Supplier)</span></a>
        <a class="sidebar-link active" href="/batches/{run_id}/images" aria-current="page"><span class="nav-mark">I</span><span>图片处理 (Images)</span></a>
        <a class="sidebar-link" href="/batches/{run_id}/upload"><span class="nav-mark">U</span><span>上传草稿 (Upload)</span></a>
      </div>
      <div class="sidebar-group"><div class="sidebar-label">全局 (Global)</div>
        <a class="sidebar-link" href="/batches"><span class="nav-mark">H</span><span>批次历史 (History)</span></a><a class="sidebar-link" href="/products"><span class="nav-mark">P</span><span>商品资料库 (Products)</span></a><a class="sidebar-link" href="/store"><span class="nav-mark">K</span><span>店铺授权 (Store)</span></a><a class="sidebar-link" href="/diagnostics"><span class="nav-mark">D</span><span>诊断中心 (Diagnostics)</span></a><a class="sidebar-link" href="/settings"><span class="nav-mark">C</span><span>系统设置 (Settings)</span></a>
      </div>
    </aside>
    <div class="app-main">
      <header class="topbar"><div><h1>Ozon V2 工具台 (Workbench)</h1><p>参考图、真实供应商素材与生成结果严格分区</p></div><span class="pill">已连接 (Connected)</span></header>
      <nav id="stageNavigation" class="stage-nav" aria-label="批次阶段 (Batch Stages)"><div class="stage-nav-inner">
        <a id="batchOverviewNav" class="stage-link" href="/?run_id={run_id}">批次总览 (Batch)</a>
        <a id="supplierReviewNav" class="stage-link" href="/batches/{run_id}/supplier-review">供应商审核 (Supplier Review)</a>
        <a id="imageProcessingNav" class="stage-link active" aria-current="page" href="/batches/{run_id}/images">图片处理 (Images)</a>
        <a id="uploadDraftNav" class="stage-link" href="/batches/{run_id}/upload">上传草稿 (Upload)</a>
        <span class="stage-run">批次 {run_id}</span>
      </div></nav>
      <main class="workspace">
        <div class="page-head"><div><h2>图片处理 (Images)</h2><p>只展示真实落盘素材；生成能力启用前不会产生或放行图片。</p></div><span id="workspaceStatus" class="pill">加载中 (Loading)</span></div>
        <section class="summary">
          <div class="metric"><span>商品 (Products)</span><strong id="productCount">0</strong></div>
          <div class="metric"><span>Ozon 参考图 (References)</span><strong id="ozonImageCount">0</strong></div>
          <div class="metric"><span>供应商原图 (Supplier)</span><strong id="supplierImageCount">0</strong></div>
          <div class="metric"><span>生成结果 (Generated)</span><strong id="generatedImageCount">0</strong></div>
        </section>
        <div class="image-layout">
          <section class="panel"><div class="panel-head"><h3>素材工作区 (Source Assets)</h3><div class="image-pager" aria-label="商品切换 (Product Navigation)"><button id="previousImageItem" type="button" title="上一件 (Previous)" aria-label="上一件">&#8592;</button><span id="imageItemPosition" class="image-position" aria-live="polite">0 / 0</span><button id="nextImageItem" type="button" title="下一件 (Next)" aria-label="下一件">&#8594;</button><span class="pill">单 SKU</span></div></div><div id="imageItems" class="image-items image-items-viewport"></div></section>
          <aside id="imageGate" class="panel"><div class="panel-head"><h3>图片质量门禁 (Image Gate)</h3></div><div class="gate-list">
            <div class="gate-row"><span class="gate-icon">1</span><div><strong>Ozon 参考图 (Ozon Reference)</strong><span>只用于学习风格、布局和信息构造。</span></div></div>
            <div class="gate-row"><span class="gate-icon">2</span><div><strong>供应商原图 (Supplier Source)</strong><span>保持真实商品主体、颜色和结构。</span></div></div>
            <div class="gate-row blocked"><span class="gate-icon">!</span><div><strong>Codex 生图队列 (Codex Image Queue)</strong><span id="gateMessage">等待真实 SKU 与主体证据确认。</span></div></div>
          <div class="controller-panel">
            <strong>&#29983;&#22270;&#24635;&#25511;&#21629;&#20196; (Image Controller Command)</strong>
            <p>复制后粘贴到一个新的 Codex 总控任务；总控只使用 spawn_agent，按当前队列任务量和可用并发位创建并复用最多 5 个动态 Codex 生图子智能体，可用几个就使用几个，不会创建侧边栏任务。</p>
            <div id="imageControllerCommand" class="controller-command">{controller_command_html}</div>
            <div class="controller-actions">
              <button id="copyImageControllerCommand" type="button">&#22797;&#21046;&#24635;&#25511;&#21629;&#20196; (Copy Command)</button>
              <span id="imageControllerCopyStatus" class="copy-status" aria-live="polite"></span>
            </div>
          </div>
          </div><div class="gate-callout">最多 5 个动态 Codex 生图子智能体在总控任务内部按整件商品领取任务，可用几个就调动几个；当运行时提供第 6 个子智能体并发位时，保留 1 个子智能体位置用于失败恢复、诊断或人工介入。只回传通过真实性校验的 2 张主图与 6 张副图。</div><button id="startGeneration" class="primary" disabled>等待 Codex 生图子智能体 (Waiting for Codex Subagents)</button><div id="imageJobControls" class="job-controls"><div id="imageJobStatus" class="job-status">当前商品尚未入队</div><button id="submitImageRepairs" class="repair-submit" type="button" disabled>提交选中图片返修 (Repair Selected)</button><div id="repairSelectionStatus" class="repair-selection-status" aria-live="polite"></div><button id="stopImageJob" type="button" disabled>停止生图 (Stop Generation)</button><button id="resumeImageJob" type="button" disabled>继续生图 (Resume Generation)</button></div></aside>
        </div>
      </main>
    </div>
  </div>
  <script>
    const runId = {safe_run_id};
    const $ = (id) => document.getElementById(id);
    const imageControllerCommand = $("imageControllerCommand");
    const copyImageControllerCommand = $("copyImageControllerCommand");
    const imageControllerCopyStatus = $("imageControllerCopyStatus");
    async function copyControllerCommand() {{
      const command = imageControllerCommand.textContent.trim();
      try {{
        if (navigator.clipboard && window.isSecureContext) {{
          await navigator.clipboard.writeText(command);
        }} else {{
          const textarea = document.createElement("textarea");
          textarea.value = command;
          textarea.style.position = "fixed";
          textarea.style.opacity = "0";
          document.body.append(textarea);
          textarea.focus();
          textarea.select();
          document.execCommand("copy");
          textarea.remove();
        }}
        imageControllerCopyStatus.textContent = "\u5df2\u590d\u5236 (Copied)";
      }} catch (error) {{
        imageControllerCopyStatus.textContent = "\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u9009\u62e9 (Copy Failed)";
      }}
    }}
    copyImageControllerCommand.addEventListener("click", copyControllerCommand);
    let imageWorkspaceItems = [];
    let imageWorkspaceIndex = 0;
    let imageWorkspaceSignature = "";
    async function api(path, options = {{}}) {{ const response = await fetch(path, {{ headers: {{ "Content-Type":"application/json" }}, ...options }}); const body = await response.json(); if (!response.ok) throw body; return body; }}
    function renderImages(container, images, emptyText) {{
      if (!images.length) {{ const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = emptyText; container.append(empty); return; }}
      const gallery = document.createElement("div"); gallery.className = "gallery";
      images.forEach((src) => {{ const image = document.createElement("img"); image.src = src; image.alt = "Product source image"; image.loading = "lazy"; image.referrerPolicy = "no-referrer"; gallery.append(image); }});
      container.append(gallery);
    }}
    function sourcePanel(title, images, emptyText) {{
      const panel = document.createElement("div"); panel.className = "source-panel";
      const heading = document.createElement("div"); heading.className = "source-title"; heading.innerHTML = `<strong>${{title}}</strong><span>${{images.length}} 张</span>`;
      panel.append(heading); renderImages(panel, images, emptyText); return panel;
    }}
    const repairIssueOptions = [
      ["product_truth", "主体、数量、颜色或结构错误"],
      ["scene_quality", "场景不真实、不美观或融合差"],
      ["composition", "构图、裁切、遮挡或比例问题"],
      ["selling_point", "卖点不清楚或用途证明不足"],
      ["russian_copy", "俄文标签错误、不清晰或排版不佳"],
      ["other", "其他问题"],
    ];
    function generatedImageUrl(job, slot) {{
      const version = `${{slot.attempt_count || 0}}-${{slot.repair_count || 0}}`;
      return `/api/batches/${{encodeURIComponent(runId)}}/image-job/${{encodeURIComponent(job.job_id)}}/slot/${{encodeURIComponent(slot.slot_id)}}/file?v=${{version}}`;
    }}
    function updateRepairSubmitState() {{
      const button = $("submitImageRepairs");
      const selected = [...document.querySelectorAll(".generated-review-card")].filter((card) => card.querySelector('input[type="checkbox"]')?.checked);
      const valid = selected.length > 0 && selected.every((card) => {{
        const issue = card.querySelector(".repair-issue");
        const note = card.querySelector(".repair-note");
        return issue && issue.value && (issue.value !== "other" || note.value.trim());
      }});
      button.disabled = !valid;
      button.textContent = selected.length ? `提交选中 ${{selected.length}} 张图片返修 (Repair Selected)` : "提交选中图片返修 (Repair Selected)";
    }}
    function generatedReviewPanel(item) {{
      const panel = document.createElement("div"); panel.className = "source-panel generated-review-panel";
      const job = item.image_job || {{}};
      const slots = (job.slots || []).filter((slot) => slot.accepted_path);
      const heading = document.createElement("div"); heading.className = "source-title"; heading.innerHTML = `<strong>生成结果 (Generated)</strong><span>${{slots.length}} 张</span>`;
      panel.append(heading);
      if (!slots.length) {{ const empty = document.createElement("div"); empty.className = "empty generation-empty"; empty.textContent = "等待 Codex 生图子智能体回传真实结果"; panel.append(empty); return panel; }}
      const grid = document.createElement("div"); grid.className = "generated-review-grid";
      slots.forEach((slot) => {{
        const card = document.createElement("article"); card.className = "generated-review-card"; card.dataset.slotId = slot.slot_id;
        if (slot.status === "repair_pending") card.classList.add("repairing");
        const cardHead = document.createElement("div"); cardHead.className = "generated-review-head";
        const slotName = document.createElement("strong"); slotName.textContent = slot.slot_id;
        const badge = document.createElement("span"); badge.className = "repair-badge"; badge.textContent = slot.status === "repair_pending" ? "等待返修" : slot.status === "accepted" ? "待审核" : slot.status;
        cardHead.append(slotName, badge);
        const image = document.createElement("img"); image.src = generatedImageUrl(job, slot); image.alt = `${{slot.slot_id}} generated product image`; image.loading = "lazy";
        const checkLabel = document.createElement("label"); checkLabel.className = "repair-check";
        const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.disabled = job.status !== "manual_review_required" || slot.status !== "accepted" || Number(slot.repair_count || 0) >= 2;
        const checkText = document.createElement("span"); checkText.textContent = Number(slot.repair_count || 0) >= 2 ? "已达到返修上限" : "不合格，申请返修";
        checkLabel.append(checkbox, checkText);
        const fields = document.createElement("div"); fields.className = "repair-fields"; fields.hidden = true;
        const select = document.createElement("select"); select.className = "repair-issue"; select.setAttribute("aria-label", `${{slot.slot_id}} 问题类型`);
        const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "请选择问题类型"; select.append(placeholder);
        repairIssueOptions.forEach(([value, label]) => {{ const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option); }});
        const textarea = document.createElement("textarea"); textarea.className = "repair-note"; textarea.maxLength = 500; textarea.placeholder = "补充说明（可选；选择其他时必填）"; textarea.setAttribute("aria-label", `${{slot.slot_id}} 补充说明`);
        fields.append(select, textarea);
        checkbox.addEventListener("change", () => {{ card.classList.toggle("selected", checkbox.checked); fields.hidden = !checkbox.checked; if (!checkbox.checked) {{ select.value = ""; textarea.value = ""; }} $("repairSelectionStatus").textContent = ""; $("repairSelectionStatus").className = "repair-selection-status"; updateRepairSubmitState(); }});
        select.addEventListener("change", updateRepairSubmitState); textarea.addEventListener("input", updateRepairSubmitState);
        card.append(cardHead, image, checkLabel, fields);
        if (slot.status === "repair_pending") {{ const feedback = document.createElement("div"); feedback.className = "repair-feedback-summary"; feedback.textContent = `${{slot.review_issue_code || "返修"}}${{slot.review_note ? ` · ${{slot.review_note}}` : ""}}`; card.append(feedback); }}
        grid.append(card);
      }});
      panel.append(grid); return panel;
    }}
    function renderImageJobControls(item) {{
      const job = item && item.image_job ? item.image_job : null;
      const status = job ? String(job.status || "pending") : String((item && item.generation_status) || "not_queued");
      const repairPending = job ? (job.slots || []).filter((slot) => slot.status === "repair_pending").length : 0;
      $("imageJobStatus").textContent = job ? `${{job.job_id}} · ${{status}}${{repairPending ? ` · ${{repairPending}} 张等待返修` : ""}}` : status;
      $("stopImageJob").disabled = !job || ["stopped","manual_review_required","completed","failed"].includes(status);
      $("resumeImageJob").disabled = !job || status !== "stopped";
      $("startGeneration").textContent = repairPending ? "返修已入队，请重新启动生图总控" : status === "manual_review_required" ? "等待用户审核 8 张图 (Review Required)" : status === "in_progress" ? "Codex 正在生成 (Generating)" : status === "pending" ? "已进入 Codex 队列 (Queued)" : "等待 Codex 生图子智能体 (Waiting for Codex Subagents)";
      updateRepairSubmitState();
    }}
    function renderImageItemAt(index) {{
      const container = $("imageItems");
      container.replaceChildren();
      $("repairSelectionStatus").textContent = "";
      $("repairSelectionStatus").className = "repair-selection-status";
      const total = imageWorkspaceItems.length;
      imageWorkspaceIndex = total ? Math.max(0, Math.min(index, total - 1)) : 0;
      $("imageItemPosition").textContent = total ? `${{imageWorkspaceIndex + 1}} / ${{total}}` : "0 / 0";
      $("previousImageItem").disabled = !total || imageWorkspaceIndex === 0;
      $("nextImageItem").disabled = !total || imageWorkspaceIndex >= total - 1;
      if (!total) {{ const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "当前批次没有可用的 Ozon 图片证据"; container.append(empty); return; }}
      const item = imageWorkspaceItems[imageWorkspaceIndex];
      const card = document.createElement("article"); card.className = "image-product";
      const head = document.createElement("div"); head.className = "product-head"; const title = document.createElement("strong"); title.textContent = item.ozon_title || item.seed_id; const sku = document.createElement("span"); sku.textContent = JSON.stringify(item.selected_options || {{}}); head.append(title, sku);
      const sources = document.createElement("div"); sources.className = "source-grid";
      sources.append(sourcePanel("Ozon 参考图 (Ozon Reference)", item.ozon_reference_images || [], "没有 Ozon 参考图"));
      sources.append(sourcePanel("供应商原图 (Supplier Source)", item.supplier_source_images || [], "等待用户核实并采集 1688 商品"));
      sources.append(generatedReviewPanel(item));
      card.append(head, sources); container.append(card); container.scrollTop = 0;
      renderImageJobControls(item);
    }}
    function render(data, preserveIndex = false) {{
      imageWorkspaceItems = data.items || []; const counts = data.source_counts || {{}}; const gate = data.image_gate || {{}};
      $("productCount").textContent = String(imageWorkspaceItems.length); $("ozonImageCount").textContent = String(counts.ozon_reference_images || 0); $("supplierImageCount").textContent = String(counts.supplier_source_images || 0); $("generatedImageCount").textContent = String(counts.generated_images || 0);
      $("workspaceStatus").textContent = (counts.supplier_source_images || 0) > 0 ? "素材已就绪 (Sources Ready)" : "等待供应商素材 (Waiting for Supplier)";
      $("gateMessage").textContent = gate.message || "没有生成结果，禁止进入上传阶段。";
      renderImageItemAt(preserveIndex ? imageWorkspaceIndex : 0);
    }}
    function workspaceSignature(data) {{
      return JSON.stringify((data.items || []).map((item) => [item.seed_id, item.generation_status, (item.image_job || {{}}).status, ((item.image_job || {{}}).slots || []).map((slot) => [slot.slot_id, slot.status, slot.accepted_path, slot.attempt_count, slot.repair_count, slot.review_issue_code, slot.review_requested_at])]));
    }}
    async function loadWorkspace(force = false) {{
      const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/images`);
      const data = result.data || {{}};
      const signature = workspaceSignature(data);
      if (!force && signature === imageWorkspaceSignature) return;
      const preserveIndex = imageWorkspaceSignature !== "";
      imageWorkspaceSignature = signature;
      render(data, preserveIndex);
    }}
    async function updateImageJob(action) {{
      const item = imageWorkspaceItems[imageWorkspaceIndex];
      const job = item && item.image_job;
      if (!job) return;
      $("stopImageJob").disabled = true;
      $("resumeImageJob").disabled = true;
      try {{
        await api(`/api/batches/${{encodeURIComponent(runId)}}/image-job/${{encodeURIComponent(job.job_id)}}/${{action}}`, {{ method:"POST", body:"{{}}" }});
        await loadWorkspace(true);
      }} catch (error) {{
        $("imageJobStatus").textContent = error.message || "生图任务控制失败 (Control Failed)";
        renderImageJobControls(item);
      }}
    }}
    function collectRepairRequests() {{
      const selected = [...document.querySelectorAll(".generated-review-card")].filter((card) => card.querySelector('input[type="checkbox"]')?.checked);
      if (!selected.length) throw new Error("请先勾选至少一张不合格图片");
      return selected.map((card) => {{
        const issue = card.querySelector(".repair-issue");
        const note = card.querySelector(".repair-note");
        if (!issue.value) throw new Error(`${{card.dataset.slotId}}：请选择问题类型`);
        if (issue.value === "other" && !note.value.trim()) throw new Error(`${{card.dataset.slotId}}：其他问题必须填写说明`);
        return {{ slot_id: card.dataset.slotId, issue_code: issue.value, note: note.value.trim() }};
      }});
    }}
    async function submitSelectedRepairs() {{
      const item = imageWorkspaceItems[imageWorkspaceIndex];
      const job = item && item.image_job;
      if (!job || job.status !== "manual_review_required") return;
      const button = $("submitImageRepairs");
      const status = $("repairSelectionStatus");
      try {{
        const repairs = collectRepairRequests();
        button.disabled = true;
        status.className = "repair-selection-status";
        status.textContent = `正在提交 ${{repairs.length}} 张图片返修…`;
        await api(`/api/batches/${{encodeURIComponent(runId)}}/image-job/${{encodeURIComponent(job.job_id)}}/repair`, {{ method:"POST", body:JSON.stringify({{ repairs }}) }});
        await loadWorkspace(true);
        status.className = "repair-selection-status success";
        status.textContent = "返修已入队，请重新启动生图总控";
      }} catch (error) {{
        status.className = "repair-selection-status error";
        status.textContent = error.message || "提交返修失败 (Repair Request Failed)";
        updateRepairSubmitState();
      }}
    }}
    $("previousImageItem").addEventListener("click", () => renderImageItemAt(imageWorkspaceIndex - 1));
    $("nextImageItem").addEventListener("click", () => renderImageItemAt(imageWorkspaceIndex + 1));
    $("stopImageJob").addEventListener("click", () => updateImageJob("stop"));
    $("resumeImageJob").addEventListener("click", () => updateImageJob("resume"));
    $("submitImageRepairs").addEventListener("click", submitSelectedRepairs);
    loadWorkspace(true).catch((error) => {{ $("workspaceStatus").textContent = "加载失败 (Failed)"; $("workspaceStatus").className = "pill error"; $("gateMessage").textContent = error.message || String(error); }});
    setInterval(() => loadWorkspace(false).catch(() => undefined), 2000);
  </script>
</body>
</html>"""


def build_upload_workspace_html(run_id: str) -> str:
    safe_run_id = json.dumps(run_id)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>上传草稿 (Upload) - Ozon V2</title>
  <style>
    :root {{ color-scheme:light; --canvas:#f3f5f8; --surface:#fff; --sidebar:#182130; --hover:#253247; --text:#182235; --muted:#68758a; --line:#dce2ea; --soft:#f8fafc; --blue:#2457d6; --blue-soft:#eaf0ff; --green:#087a62; --green-soft:#e7f6f1; --amber:#a45b08; --amber-soft:#fff3dc; --red:#b4232c; --red-soft:#fdebed; }}
    * {{ box-sizing:border-box; }} html,body {{ margin:0; min-height:100%; color:var(--text); background:var(--canvas); font:14px/1.45 "Segoe UI","Microsoft YaHei",Arial,sans-serif; }} button {{ font:inherit; letter-spacing:0; }}
    .app-shell {{ min-height:100vh; display:grid; grid-template-columns:224px minmax(0,1fr); }}
    .sidebar {{ position:sticky; top:0; height:100vh; padding:18px 12px; color:#f7f9fc; background:var(--sidebar); overflow:auto; z-index:20; }}
    .sidebar-brand {{ padding:4px 10px 20px; border-bottom:1px solid rgba(255,255,255,.09); }} .sidebar-brand strong {{ display:block; font-size:17px; }} .sidebar-brand span {{ color:#9ba8ba; font-size:11px; }}
    .sidebar-group {{ margin-top:18px; }} .sidebar-label {{ padding:0 10px 7px; color:#7f8da1; font-size:10px; font-weight:700; text-transform:uppercase; }}
    .sidebar-link {{ min-height:38px; display:flex; align-items:center; gap:10px; padding:0 10px; border-radius:5px; color:#c7d0dc; text-decoration:none; font-size:12px; }} .sidebar-link:hover {{ color:#fff; background:var(--hover); }} .sidebar-link.active {{ color:#fff; background:var(--blue); }}
    .nav-mark {{ width:18px; height:18px; display:inline-flex; align-items:center; justify-content:center; border:1px solid currentColor; border-radius:4px; font-size:9px; font-weight:700; }}
    .app-main {{ min-width:0; }} .topbar {{ position:sticky; top:0; z-index:12; height:56px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:0 18px; border-bottom:1px solid var(--line); background:rgba(255,255,255,.96); backdrop-filter:blur(8px); }}
    .topbar h1 {{ margin:0; font-size:16px; }} .topbar p {{ margin:2px 0 0; color:var(--muted); font-size:11px; }}
    .pill {{ min-height:24px; display:inline-flex; align-items:center; padding:2px 8px; border-radius:4px; color:var(--green); background:var(--green-soft); font-size:12px; font-weight:650; }}
    .stage-nav {{ border-bottom:1px solid var(--line); background:var(--surface); }} .stage-nav-inner {{ min-height:46px; display:flex; align-items:stretch; gap:6px; padding:0 18px; }}
    .stage-link {{ display:inline-flex; align-items:center; padding:0 14px; border-bottom:3px solid transparent; color:var(--text); text-decoration:none; font-size:13px; white-space:nowrap; }} .stage-link.active {{ color:var(--blue); border-bottom-color:var(--blue); background:var(--blue-soft); }} .stage-run {{ margin-left:auto; display:inline-flex; align-items:center; color:var(--muted); font:12px Consolas,"Courier New",monospace; }}
    .workspace {{ padding:18px; }} .page-head {{ min-height:72px; display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:14px; padding:14px 16px; border:1px solid var(--line); border-radius:6px; background:var(--surface); }}
    .page-head h2 {{ margin:0; font-size:18px; }} .page-head p {{ margin:4px 0 0; color:var(--muted); font-size:12px; }}
    .summary {{ display:grid; grid-template-columns:repeat(4,minmax(120px,1fr)); margin-bottom:14px; overflow:hidden; border:1px solid var(--line); border-radius:6px; background:var(--surface); }}
    .metric {{ min-height:76px; display:flex; flex-direction:column; justify-content:center; padding:12px 15px; border-right:1px solid var(--line); }} .metric:last-child {{ border-right:0; }} .metric span {{ color:var(--muted); font-size:11px; }} .metric strong {{ margin-top:4px; font-size:19px; }}
    .upload-layout {{ display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:14px; align-items:start; }} .panel {{ min-width:0; overflow:hidden; border:1px solid var(--line); border-radius:6px; background:var(--surface); }}
    .panel-head {{ min-height:50px; display:flex; align-items:center; justify-content:space-between; gap:10px; padding:0 14px; border-bottom:1px solid var(--line); }} .panel-head h3 {{ margin:0; font-size:14px; }}
    .draft-items {{ display:grid; gap:12px; padding:12px; }} .draft-item {{ display:grid; grid-template-columns:124px minmax(0,1fr); gap:14px; padding:12px; border:1px solid var(--line); border-radius:5px; }}
    .draft-item img {{ width:124px; height:124px; object-fit:contain; border:1px solid var(--line); border-radius:4px; background:#fff; }} .image-placeholder {{ width:124px; height:124px; display:grid; place-items:center; border:1px dashed #c9d1dc; border-radius:4px; color:var(--muted); background:var(--soft); font-size:11px; text-align:center; }}
    .source-label {{ color:var(--muted); font-size:10px; }} .source-title {{ margin:3px 0 9px; font-size:16px; line-height:1.35; }} .notice {{ padding:8px 10px; border-left:3px solid var(--amber); color:#74410a; background:#fff8eb; font-size:10px; }}
    .meta-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); margin-top:11px; border:1px solid var(--line); border-radius:4px; overflow:hidden; }} .meta {{ padding:8px 9px; border-right:1px solid var(--line); }} .meta:last-child {{ border-right:0; }} .meta span {{ display:block; color:var(--muted); font-size:9px; }} .meta strong {{ display:block; margin-top:3px; overflow-wrap:anywhere; font-size:10px; }}
    .attribute-grid {{ display:grid; grid-template-columns:1fr 1fr; margin-top:11px; border:1px solid var(--line); border-radius:4px; overflow:hidden; }} .attribute {{ display:flex; justify-content:space-between; gap:10px; padding:7px 9px; border-right:1px solid var(--line); border-bottom:1px solid #edf0f4; font-size:10px; }} .attribute:nth-child(2n) {{ border-right:0; }} .attribute span {{ color:var(--muted); }}
    .gate-list {{ padding:6px 14px 12px; }} .gate-row {{ display:grid; grid-template-columns:22px minmax(0,1fr); gap:9px; padding:10px 0; border-bottom:1px solid #edf0f4; }} .gate-row:last-child {{ border-bottom:0; }} .gate-icon {{ width:22px; height:22px; display:grid; place-items:center; border-radius:50%; color:var(--green); background:var(--green-soft); font-size:11px; font-weight:700; }} .gate-row.blocked .gate-icon {{ color:var(--amber); background:var(--amber-soft); }} .gate-row strong {{ display:block; font-size:11px; }} .gate-row span {{ display:block; margin-top:2px; color:var(--muted); font-size:10px; }}
    .publish-lock {{ margin:0 14px 12px; padding:11px 12px; border-left:3px solid var(--amber); color:#74410a; background:#fff8eb; }} .publish-lock strong {{ display:block; font-size:11px; }} .publish-lock span {{ display:block; margin-top:3px; font-size:10px; }}
    .primary {{ width:calc(100% - 28px); height:38px; margin:0 14px 14px; border:1px solid var(--blue); border-radius:5px; color:#fff; background:var(--blue); }} .primary:disabled {{ opacity:.48; cursor:not-allowed; }} .empty {{ padding:26px; color:var(--muted); text-align:center; }}
    @media(max-width:1000px) {{ .upload-layout {{ grid-template-columns:1fr; }} }}
    @media(max-width:900px) {{ .app-shell {{ grid-template-columns:72px minmax(0,1fr); }} .sidebar {{ padding:14px 8px; }} .sidebar-brand {{ padding:4px 4px 16px; text-align:center; }} .sidebar-brand strong {{ font-size:14px; }} .sidebar-brand span,.sidebar-label,.sidebar-link span:last-child {{ display:none; }} .sidebar-link {{ justify-content:center; padding:0; }} .stage-nav-inner {{ overflow-x:auto; padding:0 10px; }} .stage-run {{ display:none; }} .summary {{ grid-template-columns:repeat(2,1fr); }} .metric:nth-child(2) {{ border-right:0; }} .metric:nth-child(-n+2) {{ border-bottom:1px solid var(--line); }} }}
    @media(max-width:620px) {{ .app-shell {{ display:block; }} .sidebar {{ position:static; width:100%; height:auto; display:flex; align-items:center; gap:6px; overflow-x:auto; }} .sidebar-brand {{ min-width:92px; padding:0 8px; border:0; }} .sidebar-group {{ display:flex; gap:4px; margin:0; }} .sidebar-link {{ width:38px; flex:0 0 38px; }} .workspace {{ padding:10px; }} .page-head {{ align-items:flex-start; flex-direction:column; }} .draft-item {{ grid-template-columns:1fr; }} .draft-item img,.image-placeholder {{ width:100%; height:180px; }} .attribute-grid {{ grid-template-columns:1fr; }} .attribute {{ border-right:0; }} }}
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar" aria-label="主菜单 (Primary Navigation)"><div class="sidebar-brand"><strong>Ozon V2</strong><span>运营驾驶舱 (Operations Cockpit)</span></div><div class="sidebar-group"><div class="sidebar-label">运营 (Operations)</div>
      <a class="sidebar-link" href="/?run_id={run_id}"><span class="nav-mark">B</span><span>批次总览 (Batch)</span></a><a class="sidebar-link" href="/batches/{run_id}/supplier-review"><span class="nav-mark">S</span><span>供应商审核 (Supplier)</span></a><a class="sidebar-link" href="/batches/{run_id}/images"><span class="nav-mark">I</span><span>图片处理 (Images)</span></a><a class="sidebar-link active" href="/batches/{run_id}/upload" aria-current="page"><span class="nav-mark">U</span><span>上传草稿 (Upload)</span></a>
    </div><div class="sidebar-group"><div class="sidebar-label">全局 (Global)</div><a class="sidebar-link" href="/batches"><span class="nav-mark">H</span><span>批次历史 (History)</span></a><a class="sidebar-link" href="/products"><span class="nav-mark">P</span><span>商品资料库 (Products)</span></a><a class="sidebar-link" href="/store"><span class="nav-mark">K</span><span>店铺授权 (Store)</span></a><a class="sidebar-link" href="/diagnostics"><span class="nav-mark">D</span><span>诊断中心 (Diagnostics)</span></a><a class="sidebar-link" href="/settings"><span class="nav-mark">C</span><span>系统设置 (Settings)</span></a></div></aside>
    <div class="app-main"><header class="topbar"><div><h1>Ozon V2 工具台 (Workbench)</h1><p>类目模板、预填计划、图片门禁与发布锁集中检查</p></div><span class="pill">已连接 (Connected)</span></header>
      <nav id="stageNavigation" class="stage-nav" aria-label="批次阶段 (Batch Stages)"><div class="stage-nav-inner"><a id="batchOverviewNav" class="stage-link" href="/?run_id={run_id}">批次总览 (Batch)</a><a id="supplierReviewNav" class="stage-link" href="/batches/{run_id}/supplier-review">供应商审核 (Supplier Review)</a><a id="imageProcessingNav" class="stage-link" href="/batches/{run_id}/images">图片处理 (Images)</a><a id="uploadDraftNav" class="stage-link active" aria-current="page" href="/batches/{run_id}/upload">上传草稿 (Upload)</a><span class="stage-run">批次 {run_id}</span></div></nav>
      <main class="workspace"><div class="page-head"><div><h2>上传草稿 (Upload)</h2><p>根据真实 Seller API 类目模板检查预填计划；标题、描述和图片未完成前不构建草稿。</p></div><span id="workspaceStatus" class="pill">加载中 (Loading)</span></div>
        <section class="summary"><div class="metric"><span>商品 (Products)</span><strong id="productCount">0</strong></div><div class="metric"><span>必填属性 (Required)</span><strong id="requiredCount">0</strong></div><div class="metric"><span>预填计划 (Prefill)</span><strong id="prefillCount">0</strong></div><div class="metric"><span>可用图片 (Images)</span><strong id="imageReadyCount">0</strong></div></section>
        <div class="upload-layout"><section class="panel"><div class="panel-head"><h3>草稿预填证据 (Draft Prefill Evidence)</h3><span class="pill">Seller API Template</span></div><div id="draftItems" class="draft-items"></div></section>
          <aside id="uploadGate" class="panel"><div class="panel-head"><h3>上传门禁 (Upload Gate)</h3></div><div class="gate-list"><div id="templateGate" class="gate-row"><span class="gate-icon">1</span><div><strong>类目模板 (Category Template)</strong><span>等待检查</span></div></div><div class="gate-row blocked"><span class="gate-icon">!</span><div><strong>原创内容 (Original Content)</strong><span>标题和描述生成尚未实现。</span></div></div><div class="gate-row blocked"><span class="gate-icon">!</span><div><strong>图片门禁 (Image Gate)</strong><span>没有通过门禁的生成图片。</span></div></div><div class="gate-row blocked"><span class="gate-icon">!</span><div><strong>草稿文件 (Draft Artifact)</strong><span>最终 Seller API 草稿尚未构建。</span></div></div></div><div class="publish-lock"><strong>发布锁已开启 (Publish Lock Active)</strong><span>本页不会自动提交到 Ozon；门禁通过后仍需独立发布确认。</span></div><button id="buildDraft" class="primary" disabled>等待内容与图片门禁 (Waiting for Gates)</button></aside>
        </div>
      </main>
    </div>
  </div>
  <script>
    const runId = {safe_run_id}; const $ = (id) => document.getElementById(id);
    async function api(path) {{ const response = await fetch(path); const body = await response.json(); if (!response.ok) throw body; return body; }}
    function renderAttributes(container, attributes) {{ const entries = Object.entries(attributes || {{}}).slice(0, 12); if (!entries.length) return; const grid = document.createElement("div"); grid.className = "attribute-grid"; entries.forEach(([key,value]) => {{ const row = document.createElement("div"); row.className = "attribute"; const label = document.createElement("span"); label.textContent = key; const fact = document.createElement("strong"); fact.textContent = String(value); row.append(label,fact); grid.append(row); }}); container.append(grid); }}
    function render(data) {{ const items = data.items || []; const gates = data.gates || {{}}; $("productCount").textContent = String(items.length); $("requiredCount").textContent = String(items.reduce((total,item) => total + (item.required_attribute_count || 0),0)); $("prefillCount").textContent = String(items.reduce((total,item) => total + (item.prefill_plan_count || 0),0)); $("imageReadyCount").textContent = gates.images_ready ? String(items.length) : "0"; $("workspaceStatus").textContent = gates.ready_to_build ? "可构建草稿 (Ready)" : "门禁阻塞 (Blocked)"; const templateStatus = $("templateGate").querySelector("span:last-child"); templateStatus.textContent = gates.category_template_ready ? "Seller API 类目模板已就绪。" : "Seller API 类目模板缺失。"; $("draftItems").replaceChildren();
      items.forEach((item) => {{ const card = document.createElement("article"); card.className = "draft-item"; if (item.source_image) {{ const image = document.createElement("img"); image.src = item.source_image; image.alt = item.source_title || "Ozon source"; image.loading = "lazy"; image.referrerPolicy = "no-referrer"; card.append(image); }} else {{ const empty = document.createElement("div"); empty.className = "image-placeholder"; empty.textContent = "没有来源图片"; card.append(empty); }} const body = document.createElement("div"); const label = document.createElement("div"); label.className = "source-label"; label.textContent = "Ozon 来源标题 (Source Evidence Only)"; const title = document.createElement("h3"); title.className = "source-title"; title.textContent = item.source_title || item.seed_id; const notice = document.createElement("div"); notice.className = "notice"; notice.textContent = "该标题仅作证据，不会直接复制到草稿；俄文标题与描述仍需原创生成。"; const meta = document.createElement("div"); meta.className = "meta-grid"; meta.innerHTML = `<div class="meta"><span>精准类目</span><strong>${{item.category_path || "-"}}</strong></div><div class="meta"><span>属性模板</span><strong>${{item.attribute_schema_count || 0}} 字段</strong></div><div class="meta"><span>预填计划</span><strong>${{item.prefill_plan_count || 0}} 项</strong></div>`; body.append(label,title,notice,meta); renderAttributes(body,item.source_attributes); card.append(body); $("draftItems").append(card); }});
      if (!items.length) {{ const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "当前批次没有可用的类目模板与商品证据"; $("draftItems").append(empty); }}
    }}
    api(`/api/batches/${{encodeURIComponent(runId)}}/upload`).then((result) => render(result.data || {{}})).catch((error) => {{ $("workspaceStatus").textContent = "加载失败 (Failed)"; $("draftItems").textContent = error.message || String(error); }});
  </script>
</body>
</html>"""


def build_operations_page_html(page_key: str) -> str:
    pages = {
        "batches": ("批次历史 (Batch History)", "查看运行阶段、阻塞点和批次证据。", "batchHistoryContent"),
        "products": ("商品资料库 (Product Library)", "汇总 Ozon 来源、供应商证据和所属批次。", "productLibraryContent"),
        "store": ("店铺授权 (Store Authorization)", "管理 Seller API 授权与店铺去重索引。", "storeContent"),
        "settings": ("系统设置 (System Settings)", "只读展示当前自动化边界、网络路由和数据目录。", "settingsContent"),
        "diagnostics": ("诊断中心 (Diagnostics)", "检查浏览器桥接、运行目录和最近批次事件。", "diagnosticsContent"),
    }
    title, subtitle, content_id = pages[page_key]
    safe_page_key = json.dumps(page_key)
    nav_items_list: list[str] = []
    for index, (key, label) in enumerate(
        [
            ("batches", "批次历史 (Batches)"),
            ("products", "商品资料库 (Products)"),
            ("store", "店铺授权 (Store)"),
            ("diagnostics", "诊断中心 (Diagnostics)"),
            ("settings", "系统设置 (Settings)"),
        ],
        start=1,
    ):
        active_class = " active" if key == page_key else ""
        current_attribute = ' aria-current="page"' if key == page_key else ""
        nav_items_list.append(
            f'<a class="sidebar-link{active_class}" href="/{key}"{current_attribute}>'
            f'<span class="nav-mark">{index}</span><span>{label}</span></a>'
        )
    nav_items = "".join(nav_items_list)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} - Ozon V2</title>
<style>
:root {{ color-scheme:light; --canvas:#f3f5f8; --surface:#fff; --sidebar:#182130; --hover:#253247; --text:#182235; --muted:#68758a; --line:#dce2ea; --soft:#f8fafc; --blue:#2457d6; --blue-soft:#eaf0ff; --green:#087a62; --green-soft:#e7f6f1; --amber:#a45b08; --amber-soft:#fff3dc; --red:#b4232c; --red-soft:#fdebed; }}
* {{ box-sizing:border-box; }} html,body {{ margin:0; min-height:100%; color:var(--text); background:var(--canvas); font:14px/1.45 "Segoe UI","Microsoft YaHei",Arial,sans-serif; }} button,input {{ font:inherit; letter-spacing:0; }} a {{ color:inherit; }}
.app-shell {{ min-height:100vh; display:grid; grid-template-columns:224px minmax(0,1fr); }} .sidebar {{ position:sticky; top:0; height:100vh; padding:18px 12px; color:#f7f9fc; background:var(--sidebar); overflow:auto; }}
.sidebar-brand {{ padding:4px 10px 20px; border-bottom:1px solid rgba(255,255,255,.09); }} .sidebar-brand strong {{ display:block; font-size:17px; }} .sidebar-brand span {{ color:#9ba8ba; font-size:11px; }} .sidebar-group {{ margin-top:18px; }} .sidebar-label {{ padding:0 10px 7px; color:#7f8da1; font-size:10px; font-weight:700; text-transform:uppercase; }}
.sidebar-link {{ min-height:38px; display:flex; align-items:center; gap:10px; padding:0 10px; border-radius:5px; color:#c7d0dc; text-decoration:none; font-size:12px; }} .sidebar-link:hover {{ color:#fff; background:var(--hover); }} .sidebar-link.active {{ color:#fff; background:var(--blue); }} .nav-mark {{ width:19px; height:19px; display:inline-flex; align-items:center; justify-content:center; border:1px solid currentColor; border-radius:4px; font-size:9px; font-weight:700; }}
.app-main {{ min-width:0; }} .topbar {{ height:56px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:0 20px; border-bottom:1px solid var(--line); background:var(--surface); }} .topbar a {{ color:var(--blue); text-decoration:none; font-size:12px; }}
.workspace {{ padding:18px; }} .page-head {{ min-height:78px; display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:14px; padding:14px 16px; border:1px solid var(--line); border-radius:6px; background:var(--surface); }} .page-head h1 {{ margin:0; font-size:20px; }} .page-head p {{ margin:4px 0 0; color:var(--muted); font-size:12px; }}
.status {{ min-height:24px; display:inline-flex; align-items:center; padding:2px 8px; border-radius:4px; color:var(--green); background:var(--green-soft); font-size:11px; font-weight:650; }} .status.warn {{ color:var(--amber); background:var(--amber-soft); }} .status.bad {{ color:var(--red); background:var(--red-soft); }}
.summary {{ display:grid; grid-template-columns:repeat(4,minmax(120px,1fr)); margin-bottom:14px; overflow:hidden; border:1px solid var(--line); border-radius:6px; background:var(--surface); }} .metric {{ min-height:74px; display:flex; flex-direction:column; justify-content:center; padding:12px 15px; border-right:1px solid var(--line); }} .metric:last-child {{ border-right:0; }} .metric span {{ color:var(--muted); font-size:10px; }} .metric strong {{ margin-top:4px; font-size:19px; }}
.panel {{ min-width:0; overflow:hidden; border:1px solid var(--line); border-radius:6px; background:var(--surface); }} .panel-head {{ min-height:50px; display:flex; align-items:center; justify-content:space-between; gap:10px; padding:0 14px; border-bottom:1px solid var(--line); }} .panel-head h2 {{ margin:0; font-size:14px; }}
.table-wrap {{ max-height:560px; overflow:auto; }} table {{ width:100%; min-width:760px; border-collapse:collapse; }} th,td {{ padding:10px 12px; border-bottom:1px solid #edf0f4; text-align:left; vertical-align:top; }} th {{ position:sticky; top:0; z-index:1; color:var(--muted); background:#fbfcfe; font-size:10px; }} td {{ font-size:11px; }} td a {{ color:var(--blue); text-decoration:none; }}
.product-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; padding:12px; }} .product-card {{ display:grid; grid-template-columns:92px minmax(0,1fr); gap:11px; padding:11px; border:1px solid var(--line); border-radius:5px; }} .product-card img {{ width:92px; height:92px; object-fit:contain; border:1px solid var(--line); border-radius:4px; }} .product-card h3 {{ margin:0 0 5px; font-size:12px; }} .product-card p {{ margin:3px 0; color:var(--muted); font-size:10px; overflow-wrap:anywhere; }}
.two-column {{ display:grid; grid-template-columns:minmax(0,1.4fr) minmax(280px,.8fr); gap:14px; }} .content {{ padding:14px; }} .kv {{ display:grid; grid-template-columns:180px minmax(0,1fr); gap:12px; padding:10px 0; border-bottom:1px solid #edf0f4; }} .kv:last-child {{ border-bottom:0; }} .kv span {{ color:var(--muted); font-size:11px; }} .kv strong {{ overflow-wrap:anywhere; font-size:11px; }}
.form-stack {{ display:grid; gap:10px; padding:14px; }} label {{ display:grid; gap:5px; color:var(--muted); font-size:11px; }} input {{ height:36px; padding:0 10px; border:1px solid var(--line); border-radius:5px; }} button.primary {{ height:36px; border:1px solid var(--blue); border-radius:5px; color:#fff; background:var(--blue); cursor:pointer; }} .hint {{ margin:0; color:var(--muted); font-size:10px; }}
.settings-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }} .setting-card {{ padding:14px; border:1px solid var(--line); border-radius:5px; background:var(--surface); }} .setting-card h2 {{ margin:0 0 10px; font-size:13px; }}
.event-window {{ height:470px; overflow:auto; color:#cbd5e1; background:#111827; font:10px/1.55 Consolas,"Courier New",monospace; }} .event-row {{ display:grid; grid-template-columns:140px 150px minmax(0,1fr); gap:10px; padding:8px 12px; border-bottom:1px solid #243044; }} .event-row span:first-child {{ color:#7f8da1; }} .event-row strong {{ color:#93c5fd; }} .empty {{ padding:32px; color:var(--muted); text-align:center; }}
@media(max-width:1050px) {{ .product-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .two-column {{ grid-template-columns:1fr; }} }}
@media(max-width:900px) {{ .app-shell {{ grid-template-columns:72px minmax(0,1fr); }} .sidebar {{ padding:14px 8px; }} .sidebar-brand {{ padding:4px 4px 16px; text-align:center; }} .sidebar-brand strong {{ font-size:14px; }} .sidebar-brand span,.sidebar-label,.sidebar-link span:last-child {{ display:none; }} .sidebar-link {{ justify-content:center; padding:0; }} .summary {{ grid-template-columns:repeat(2,1fr); }} .metric:nth-child(2) {{ border-right:0; }} .metric:nth-child(-n+2) {{ border-bottom:1px solid var(--line); }} .settings-grid {{ grid-template-columns:1fr; }} }}
@media(max-width:620px) {{ .app-shell {{ display:block; }} .sidebar {{ position:static; width:100%; height:auto; display:flex; align-items:center; gap:6px; overflow-x:auto; }} .sidebar-brand {{ min-width:92px; padding:0 8px; border:0; }} .sidebar-group {{ display:flex; gap:4px; margin:0; }} .sidebar-link {{ width:38px; flex:0 0 38px; }} .workspace {{ padding:10px; }} .page-head {{ align-items:flex-start; flex-direction:column; }} .product-grid {{ grid-template-columns:1fr; }} .kv {{ grid-template-columns:1fr; gap:3px; }} }}
</style></head><body><div class="app-shell"><aside class="sidebar"><div class="sidebar-brand"><strong>Ozon V2</strong><span>运营驾驶舱 (Operations Cockpit)</span></div><div class="sidebar-group"><div class="sidebar-label">全局菜单 (Global)</div>{nav_items}</div><div class="sidebar-group"><div class="sidebar-label">自动化 (Automation)</div><a class="sidebar-link" href="/"><span class="nav-mark">A</span><span>当前工具台 (Workbench)</span></a></div></aside>
<div class="app-main"><header class="topbar"><strong>Ozon V2 运营驾驶舱 (Operations Cockpit)</strong><a href="/">返回自动化工具台 (Back to Workbench)</a></header><main class="workspace"><div class="page-head"><div><h1>{title}</h1><p>{subtitle}</p></div><span id="pageStatus" class="status">加载中 (Loading)</span></div><div id="{content_id}"></div></main></div></div>
<script>
const pageKey={safe_page_key}; const content=document.getElementById({json.dumps(content_id)}); const status=document.getElementById("pageStatus");
async function api(path,options={{}}){{const response=await fetch(path,{{headers:{{"Content-Type":"application/json"}},...options}});const body=await response.json();if(!response.ok)throw body;return body;}}
function node(tag,className,text){{const el=document.createElement(tag);if(className)el.className=className;if(text!==undefined)el.textContent=text;return el;}}
function summary(metrics){{const wrap=node("section","summary");metrics.forEach(([label,value])=>{{const item=node("div","metric");item.append(node("span","",label),node("strong","",String(value)));wrap.append(item);}});return wrap;}}
function panel(titleText){{const wrap=node("section","panel");const head=node("div","panel-head");head.append(node("h2","",titleText));wrap.append(head);return wrap;}}
function renderBatches(data){{content.append(summary([["全部批次",data.summary.total],["活动批次",data.summary.active],["已完成",data.summary.done],["失败",data.summary.failed]]));const wrap=panel("批次列表 (Batch List)");const tableWrap=node("div","table-wrap");const table=document.createElement("table");table.innerHTML='<thead><tr><th>批次 ID</th><th>状态</th><th>目标</th><th>事件</th><th>创建时间</th><th>操作</th></tr></thead>';const body=document.createElement("tbody");data.items.forEach(item=>{{const row=document.createElement("tr");[item.run_id,item.status,item.target_count,item.event_count,item.created_at||"-"].forEach(value=>row.append(node("td","",String(value))));const action=node("td");const link=node("a","","打开批次");link.href=item.resume_url;action.append(link);row.append(action);body.append(row);}});table.append(body);tableWrap.append(table);wrap.append(tableWrap);content.append(wrap);}}
function renderProducts(data){{content.append(summary([["商品总数",data.summary.total],["已关联供应商",data.summary.supplier_linked],["待供应商",data.summary.total-data.summary.supplier_linked],["数据来源","运行批次"]]));const wrap=panel("商品证据 (Product Evidence)");const grid=node("div","product-grid");data.items.forEach(item=>{{const card=node("article","product-card");if(item.image){{const image=document.createElement("img");image.src=item.image;image.alt=item.title||"Product";image.loading="lazy";image.referrerPolicy="no-referrer";card.append(image);}}const body=node("div");body.append(node("h3","",item.title||item.product_id),node("p","",item.category_path||"-"),node("p","",`批次 ${{item.run_id}} · ${{item.price||"-"}} ${{item.currency||""}}`),node("p","",item.supplier_title?`供应商：${{item.supplier_title}}`:"供应商：待核实"));card.append(body);grid.append(card);}});if(!data.items.length)grid.append(node("div","empty","暂无已采集商品"));wrap.append(grid);content.append(wrap);}}
function kv(label,value){{const row=node("div","kv");row.append(node("span","",label),node("strong","",String(value??"-")));return row;}}
function renderStore(data){{content.append(summary([["凭证状态",data.credentials.configured?"已配置":"未配置"],["店铺 ID",data.credentials.client_id||"-"],["去重商品",data.dedupe.product_count||0],["去重状态",data.dedupe.ready?"已就绪":"未刷新"]]));const layout=node("div","two-column");const overview=panel("店铺状态 (Store Status)");const c=node("div","content");c.append(kv("Client ID",data.credentials.client_id),kv("凭证配置",data.credentials.configured?"Configured":"Missing"),kv("去重刷新时间",data.dedupe.refreshed_at),kv("运行数据目录",data.runtime_root));overview.append(c);const binding=panel("更新授权 (Update Binding)");const form=node("div","form-stack");form.innerHTML='<label>店铺 ID (Client ID)<input id="operationsStoreId" autocomplete="off"></label><label>密钥 (API Key)<input id="operationsApiKey" type="password" autocomplete="off"></label><button id="operationsBindStore" class="primary">绑定店铺 (Bind Store)</button><p id="operationsStoreMessage" class="hint">空密钥不会覆盖历史绑定。</p>';binding.append(form);layout.append(overview,binding);content.append(layout);document.getElementById("operationsStoreId").value=data.credentials.client_id||"";document.getElementById("operationsBindStore").onclick=async()=>{{const result=await api("/api/store-binding",{{method:"POST",body:JSON.stringify({{client_id:document.getElementById("operationsStoreId").value.trim(),api_key:document.getElementById("operationsApiKey").value.trim()}})}}).catch(error=>error);document.getElementById("operationsApiKey").value="";document.getElementById("operationsStoreMessage").textContent=result.message||"操作完成";}};}}
function renderSettings(data){{content.append(summary([["种子版本",data.config.seed_pool_version],["查询语言",data.config.ozon_query_language],["来源语言",data.config.seed_source_language],["发布锁",data.boundaries.publish_locked_by_default?"默认开启":"关闭"]]));const grid=node("div","settings-grid");[["自动化边界",data.boundaries],["网络路由",data.routes],["数据目录",data.paths],["基础配置",data.config]].forEach(([titleText,values])=>{{const card=node("section","setting-card");card.append(node("h2","",titleText));Object.entries(values).forEach(([key,value])=>card.append(kv(key,value)));grid.append(card);}});content.append(grid);}}
function renderDiagnostics(data){{content.append(summary([["浏览器桥接",data.browser_bridge.stage||data.browser_bridge.code||"-"],["扩展版本",data.browser_bridge.extension_version||"-"],["批次数",data.runtime.run_count],["活动批次",data.runtime.active_run_count]]));const wrap=panel("最近运行事件 (Recent Events)");const log=node("div","event-window");data.recent_events.forEach(event=>{{const row=node("div","event-row");row.append(node("span","",event.created_at||""),node("strong","",event.event_type||""),node("span","",`${{event.run_id}} · ${{event.message||""}}`));log.append(row);}});if(!data.recent_events.length)log.append(node("div","empty","暂无运行事件"));wrap.append(log);content.append(wrap);}}
api(`/api/operations/${{pageKey}}`).then(result=>{{const data=result.data||{{}};({{batches:renderBatches,products:renderProducts,store:renderStore,settings:renderSettings,diagnostics:renderDiagnostics}})[pageKey](data);status.textContent="已加载 (Loaded)";}}).catch(error=>{{status.textContent="加载失败 (Failed)";status.className="status bad";content.append(node("div","empty",error.message||String(error)));}});
</script></body></html>"""


def create_handler(
    repo: FsRepo | None = None,
    background_runner: WorkbenchBackgroundRunner | None = None,
    runtime_controller: WorkbenchRuntimeController | None = None,
) -> type[BaseHTTPRequestHandler]:
    selected_repo = repo or FsRepo()
    service = background_runner.service if background_runner is not None else WorkbenchService(selected_repo)
    credential_service = CredentialService(selected_repo)
    diagnostics_exporter = DiagnosticsExportService(selected_repo)
    runner = background_runner or WorkbenchBackgroundRunner(
        service,
        supplier_worker=None,
    )

    class WorkbenchRequestHandler(BaseHTTPRequestHandler):
        server_version = "OzonV2Workbench/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/":
                self._send_html(build_home_html())
                return
            if path == "/api/health":
                self._send_json({"ok": True, "code": "health.ok", "message": "Workbench server is running."})
                return
            if path == "/api/runtime/status":
                self._send_json(self._runtime_status_payload())
                return
            if path == "/api/browser-bridge/status":
                self._send_json(
                    {
                        "ok": True,
                        "code": "browser_bridge.status",
                        "message": "Browser bridge status loaded.",
                        "data": self._bridge_status_payload(),
                        "errors": [],
                    }
                )
                return
            if path == "/api/browser-task/active":
                self._send_json(self._active_browser_task())
                return
            operations_pages = {
                "/batches": "batches",
                "/products": "products",
                "/store": "store",
                "/settings": "settings",
                "/diagnostics": "diagnostics",
            }
            if path in operations_pages:
                self._send_html(build_operations_page_html(operations_pages[path]))
                return
            operations_apis = {
                "/api/operations/batches": service.batch_history,
                "/api/operations/products": service.product_library,
                "/api/operations/store": service.store_overview,
                "/api/operations/settings": service.settings_overview,
                "/api/operations/diagnostics": service.diagnostics_overview,
            }
            if path in operations_apis:
                self._send_result(operations_apis[path]())
                return
            if path == "/bridge/ozon-v2-browser-bridge.js":
                self._send_js((selected_repo.context.project_root / "scripts" / "ozon_browser_bridge.js").read_text(encoding="utf-8"))
                return
            parts = self._path_parts(path)
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "diagnostics.zip":
                archive = diagnostics_exporter.build_current_batch_zip(
                    parts[2],
                    runner_status=runner.status(parts[2]),
                    browser_bridge=self._bridge_status_payload(),
                )
                self._send_bytes(
                    archive.content,
                    content_type="application/zip",
                    filename=archive.filename,
                )
                return
            if (
                len(parts) == 8
                and parts[:2] == ["api", "batches"]
                and parts[3] == "image-job"
                and parts[5] == "slot"
                and parts[7] == "file"
            ):
                asset = service.image_slot_asset(parts[2], parts[4], parts[6])
                if not asset.ok:
                    self._send_result(asset, run_id=parts[2])
                    return
                asset_path = Path(str(asset.data["path"]))
                self._send_bytes(asset_path.read_bytes(), content_type=str(asset.data["content_type"]))
                return
            if len(parts) == 3 and parts[0] == "batches" and parts[2] == "supplier-review":
                self._send_html(build_supplier_review_html(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "batches" and parts[2] == "images":
                self._send_html(build_image_workspace_html(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "batches" and parts[2] == "upload":
                self._send_html(build_upload_workspace_html(parts[1]))
                return
            if len(parts) == 3 and parts[:2] == ["api", "batches"]:
                self._send_result(service.allowed_actions(parts[2]), run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "supplier-review":
                self._send_result(service.supplier_review(parts[2]), run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "images":
                self._send_result(service.image_workspace(parts[2]), run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "upload":
                self._send_result(service.upload_workspace(parts[2]), run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "events":
                events = [event.to_dict() for event in selected_repo.load_run_events(parts[2])]
                self._send_json(
                    {
                        "ok": True,
                        "code": "workbench.events",
                        "message": "Run events loaded.",
                        "data": {"run_id": parts[2], "events": events},
                        "errors": [],
                    }
                )
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "runner":
                self._send_json(
                    {
                        "ok": True,
                        "code": "runner.status",
                        "message": "Background runner status loaded.",
                        "data": {"run_id": parts[2], "runner": runner.status(parts[2])},
                        "errors": [],
                    }
                )
                return
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["runner", "stop"]:
                self._send_json(
                    {
                        "ok": True,
                        "code": "runner.status",
                        "message": "Background runner status loaded.",
                        "data": {"run_id": parts[2], "runner": runner.status(parts[2])},
                        "errors": [],
                    }
                )
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "browser-task":
                self._send_json(self._browser_task(parts[2]))
                return
            self._send_json({"ok": False, "code": "http.not_found", "message": "Not found.", "errors": []}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            try:
                self._handle_POST()
            except Exception as exc:
                self._send_internal_error(exc)

        def _handle_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            payload = self._read_json()
            if path in {"/api/runtime/stop", "/api/runtime/restart"}:
                if runtime_controller is None:
                    self._send_json(
                        {
                            "ok": False,
                            "code": "runtime.control_unavailable",
                            "message": "Runtime control is unavailable for this server.",
                            "data": {},
                            "errors": ["Runtime controller is not configured."],
                        },
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                action = path.rsplit("/", 1)[-1]
                self._send_json(
                    runtime_controller.schedule(
                        action,
                        self.server,
                        open_edge_after_restart=(
                            action == "restart" and payload.get("open_edge") is True
                        ),
                    )
                )
                return
            if path == "/api/batches":
                bridge = self._bridge_status_payload()
                if not bridge["online"]:
                    self._send_result(
                        Result.failure(
                            "browser_bridge.offline",
                            "Browser bridge is offline; reload the Edge extension and refresh the workbench.",
                            data={"browser_bridge": bridge},
                        )
                    )
                    return
                if not bridge["version_ready"]:
                    self._send_result(
                        Result.failure(
                            "browser_bridge.update_required",
                            "Browser bridge extension version does not match the project manifest.",
                            data={"browser_bridge": bridge},
                        )
                    )
                    return
                result = service.start_batch(int(payload.get("target_count", 1)))
                if result.ok:
                    run_id = str(result.data["run"]["run_id"])
                    runner.start(run_id, max_steps=int(payload.get("max_steps", 20)))
                    self._send_result(result, run_id=run_id)
                    return
                self._send_result(result)
                return
            if path == "/api/batches/clear":
                if payload.get("confirm") is not True:
                    self._send_result(service.clear_all_batches(confirmed=False))
                    return
                run_ids = selected_repo.workbench_run_ids()
                stop_result = runner.stop_all_and_wait(run_ids)
                if not stop_result.ok:
                    self._send_result(stop_result)
                    return
                result = service.clear_all_batches(confirmed=True)
                if result.ok:
                    runner.forget(run_ids)
                self._send_result(result)
                return
            if path == "/api/store-binding":
                self._send_result(
                    credential_service.bind_store(
                        client_id=str(payload.get("client_id", "")),
                        api_key=str(payload.get("api_key", "")),
                    )
                )
                return
            if path == "/api/browser-bridge/heartbeat":
                incoming = {
                    "source": str(payload.get("source", "unknown")),
                    "bridge_id": str(payload.get("bridge_id", "ozon_v2_bridge")),
                    "run_id": payload.get("run_id"),
                    "task_type": payload.get("task_type"),
                    "stage": payload.get("stage"),
                    "code": payload.get("code"),
                    "message": payload.get("message"),
                    "url": payload.get("url"),
                    "extension_version": payload.get("extension_version"),
                    "connection_only": payload.get("connection_only") is True,
                    "details": payload.get("details") if isinstance(payload.get("details"), dict) else None,
                }
                if self._should_ignore_invalidated_heartbeat(incoming):
                    saved = selected_repo.load_browser_bridge_status()
                    response_code = "browser_bridge.heartbeat_ignored"
                    response_message = "Invalidated old-page heartbeat was ignored."
                else:
                    saved = selected_repo.save_browser_bridge_status(incoming)
                    response_code = "browser_bridge.heartbeat"
                    response_message = "Browser bridge heartbeat saved."
                run_id = str(payload.get("run_id") or "").strip()
                code = str(payload.get("code") or "")
                if run_id and code.endswith(".candidate_rejected"):
                    selected_repo.append_run_event(
                        run_id,
                        "browser_candidate.rejected",
                        str(payload.get("message") or "Ozon browser candidate was rejected."),
                        {
                            "task_type": payload.get("task_type"),
                            "url": payload.get("url"),
                            "details": payload.get("details") if isinstance(payload.get("details"), dict) else {},
                        },
                    )
                elif run_id and code.endswith(".no_cross_border_candidate"):
                    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
                    selected_repo.append_run_event(
                        run_id,
                        "browser_candidate.exhausted",
                        str(payload.get("message") or "No verified Chinese cross-border candidate was found."),
                        {
                            "task_type": payload.get("task_type"),
                            "url": payload.get("url"),
                            "details": details,
                        },
                    )
                    rejected_seed_id = str(details.get("seed_id") or "").strip()
                    if payload.get("task_type") in {"ozon_attribute_template", "ozon_collection"} and rejected_seed_id:
                        task_type = str(payload.get("task_type") or "")
                        existing_outcome = next(
                            (
                                event
                                for event in selected_repo.load_run_events(run_id)
                                if event.event_type in {"browser_candidate.replaced", "browser_candidate.failed"}
                                and str(event.data.get("seed_id") or "") == rejected_seed_id
                                and str(event.data.get("task_type") or "") == task_type
                            ),
                            None,
                        )
                        if existing_outcome is not None:
                            response_code = "browser_bridge.exhausted_seed_outcome_recorded"
                            response_message = "The exhausted seed outcome was already recorded."
                        else:
                            replacement = service.replace_exhausted_attribute_template_seed(
                                run_id,
                                rejected_seed_id,
                                str(payload.get("message") or "No verified Chinese cross-border candidate was found."),
                            )
                            if replacement.ok:
                                selected_repo.append_run_event(
                                    run_id,
                                    "browser_candidate.replaced",
                                    "The exhausted Ozon seed was replaced.",
                                    {
                                        "seed_id": rejected_seed_id,
                                        "task_type": task_type,
                                        "replacement": replacement.data,
                                    },
                                )
                                runner.start(run_id, max_steps=20)
                                response_code = "browser_bridge.exhausted_seed_replaced"
                                response_message = "The exhausted Ozon seed was replaced and the batch resumed."
                            elif (
                                task_type == "ozon_collection"
                                and replacement.code == "workbench.exhausted_seed_no_replacement"
                            ):
                                selected_repo.append_run_event(
                                    run_id,
                                    "browser_candidate.failed",
                                    "The exhausted Ozon seed could not be replaced.",
                                    {
                                        "seed_id": rejected_seed_id,
                                        "task_type": task_type,
                                        "result_code": replacement.code,
                                    },
                                )
                                response_code = "browser_bridge.exhausted_seed_no_replacement"
                                response_message = "No eligible replacement seed remains; manual review is required."
                self._send_json(
                    {
                        "ok": True,
                        "code": response_code,
                        "message": response_message,
                        "data": self._bridge_status_payload(saved),
                        "errors": [],
                    }
                )
                return
            parts = self._path_parts(path)
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "actions":
                if runner.status(parts[2]).get("running"):
                    self._send_result(
                        Result.failure(
                            "runner.busy",
                            "Stop or wait for the background runner before using a supervised action.",
                        ),
                        run_id=parts[2],
                    )
                    return
                self._send_result(service.dispatch(parts[2], str(payload.get("action", ""))))
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "autopilot":
                self._send_result(service.run_until_blocked(parts[2], max_steps=int(payload.get("max_steps", 20))))
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "runner":
                self._resume_browser_task(parts[2])
                self._send_result(runner.start(parts[2], max_steps=int(payload.get("max_steps", 20))), run_id=parts[2])
                return
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["runner", "stop"]:
                self._cancel_browser_task(parts[2])
                self._send_result(runner.stop(parts[2]), run_id=parts[2])
                return
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["browser-task", "restart"]:
                result = service.restart_browser_task(parts[2])
                if result.ok:
                    bridge = self._bridge_status_payload()
                    result.data["dispatch_state"] = (
                        "dispatched" if bridge["version_ready"] else "waiting_for_extension"
                    )
                    result.data["browser_bridge"] = bridge
                self._send_result(result, run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "attribute-template":
                result = service.ingest_attribute_template_result(parts[2], payload)
                if result.ok:
                    runner.start(parts[2], max_steps=int(payload.get("max_steps", 20) or 20))
                self._send_result(result, run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "ozon-collection-progress":
                self._send_result(service.save_ozon_collection_progress(parts[2], payload), run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "ozon-collection":
                result = service.ingest_ozon_collection_result(parts[2], payload)
                if result.ok:
                    runner.start(parts[2], max_steps=int(payload.get("max_steps", 20) or 20))
                self._send_result(result, run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "supplier-review":
                links = payload.get("links") if isinstance(payload.get("links"), list) else []
                self._send_result(service.save_supplier_review_links(parts[2], links), run_id=parts[2])
                return
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["supplier-selection", "capture"]:
                self._send_result(service.capture_supplier_selection_product(parts[2], payload), run_id=parts[2])
                return
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["supplier-selection", "reset"]:
                self._send_result(
                    service.reset_supplier_selection_product(
                        parts[2],
                        str(payload.get("seed_id") or "").strip(),
                    ),
                    run_id=parts[2],
                )
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "supplier-sku":
                differences = payload.get("differences") if isinstance(payload.get("differences"), list) else []
                self._send_result(
                    service.confirm_supplier_sku(
                        parts[2],
                        seed_id=str(payload.get("seed_id") or "").strip(),
                        supplier_sku_id=str(payload.get("supplier_sku_id") or "").strip(),
                        differences=differences,
                    ),
                    run_id=parts[2],
                )
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "subject-master":
                source_image_urls = payload.get("source_image_urls")
                if not isinstance(source_image_urls, list):
                    source_image_urls = []
                self._send_result(
                    service.confirm_subject_master(
                        parts[2],
                        seed_id=str(payload.get("seed_id") or "").strip(),
                        source_image_urls=[
                            str(value or "").strip() for value in source_image_urls if str(value or "").strip()
                        ],
                        source_image_url=str(payload.get("source_image_url") or "").strip(),
                        visible_subject_quantity=int(payload.get("visible_subject_quantity") or 0),
                        white_background_confirmed=payload.get("white_background_confirmed") is True,
                    ),
                    run_id=parts[2],
                )
                return
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["supplier-review", "reject"]:
                result = service.reject_supplier_candidate(
                    parts[2],
                    str(payload.get("seed_id") or "").strip(),
                    str(payload.get("reason") or "User could not find an exact 1688 supplier.").strip(),
                )
                if result.ok:
                    runner.start(parts[2], max_steps=int(payload.get("max_steps", 20) or 20))
                self._send_result(result, run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "supplier-collection":
                result = service.start_supplier_collection(parts[2])
                if result.ok:
                    runner.start(parts[2], max_steps=int(payload.get("max_steps", 20) or 20))
                self._send_result(result, run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "supplier-collection-progress":
                self._send_result(service.save_supplier_collection_progress(parts[2], payload), run_id=parts[2])
                return
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "supplier-collection-result":
                result = service.ingest_supplier_collection_result(parts[2], payload)
                if result.ok:
                    runner.start(parts[2], max_steps=int(payload.get("max_steps", 20) or 20))
                self._send_result(result, run_id=parts[2])
                return
            if len(parts) == 6 and parts[:2] == ["api", "batches"] and parts[3] == "image-job":
                if parts[5] == "repair":
                    repairs = payload.get("repairs")
                    if not isinstance(repairs, list):
                        repairs = []
                    self._send_result(
                        service.request_image_repairs(parts[2], parts[4], repairs),
                        run_id=parts[2],
                    )
                    return
                if parts[5] == "stop":
                    self._send_result(service.stop_image_job(parts[2], parts[4]), run_id=parts[2])
                    return
                if parts[5] == "resume":
                    self._send_result(service.resume_image_job(parts[2], parts[4]), run_id=parts[2])
                    return
            self._send_json({"ok": False, "code": "http.not_found", "message": "Not found.", "errors": []}, HTTPStatus.NOT_FOUND)

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _path_parts(self, path: str) -> list[str]:
            return [part for part in path.split("/") if part]

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def _browser_task(self, run_id: str) -> dict[str, Any]:
            run = selected_repo.load_run(run_id)
            if run.get("browser_task_cancelled"):
                return {
                    "ok": True,
                    "code": "browser_task.none",
                    "message": "Browser task was stopped by the user.",
                    "data": {
                        "run_id": run_id,
                        "created_at": run.get("created_at"),
                        "status": run.get("status"),
                        "cancelled": True,
                    },
                    "errors": [],
                }
            if run.get("status") == "attribute_template_collecting" and run.get("attribute_template_contract_ready"):
                contract_path = selected_repo.run_dir(run_id) / "attribute_template_contract.json"
                if not contract_path.exists():
                    contract_result = service.collection_contract_service.build_attribute_template_contract(run_id)
                    if not contract_result.ok:
                        return contract_result.to_dict()
                    selected_repo.save_attribute_template_contract(run_id, contract_result.data)
                    run["attribute_template_contract_path"] = str(contract_path)
                    selected_repo.save_run(run)
                    selected_repo.append_run_event(
                        run_id,
                        "browser_task.contract_restored",
                        "Attribute template contract was restored for browser task pickup.",
                        {"contract_path": str(contract_path)},
                    )
                return {
                    "ok": True,
                    "code": "browser_task.attribute_template_ready",
                    "message": "Attribute template browser task is ready for a real browser bridge.",
                    "data": {
                        "run_id": run_id,
                        "created_at": run.get("created_at"),
                        "task_type": "ozon_attribute_template",
                        "dispatch_token": run.get("browser_task_resumed_at") or run.get("created_at"),
                        "contract": selected_repo.load_attribute_template_contract(run_id),
                        "result_worker": "workbench_browser_bridge",
                        "ingest_url": f"/api/batches/{run_id}/attribute-template",
                    },
                    "errors": [],
                }
            if run.get("status") == "ozon_collecting" and run.get("ozon_collection_contract_ready"):
                contract_path = selected_repo.run_dir(run_id) / "ozon_collection_contract.json"
                if not contract_path.exists():
                    contract_result = service.collection_contract_service.build_ozon_collection_contract(run_id)
                    if not contract_result.ok:
                        return contract_result.to_dict()
                    selected_repo.save_ozon_collection_contract(run_id, contract_result.data)
                    run["ozon_collection_contract_path"] = str(contract_path)
                    selected_repo.save_run(run)
                    selected_repo.append_run_event(
                        run_id,
                        "browser_task.contract_restored",
                        "Ozon collection contract was restored for browser task pickup.",
                        {"contract_path": str(contract_path)},
                    )
                checkpoint = service.ozon_collection_checkpoint(run_id)
                if not checkpoint.ok:
                    return Result.failure(
                        "browser_task.restart_checkpoint_invalid",
                        "The saved Ozon collection checkpoint is invalid and was not overwritten.",
                        errors=checkpoint.errors,
                        data=checkpoint.data,
                    ).to_dict()
                return {
                    "ok": True,
                    "code": "browser_task.ozon_collection_ready",
                    "message": "Ozon collection browser task is ready for a real browser bridge.",
                    "data": {
                        "run_id": run_id,
                        "created_at": run.get("created_at"),
                        "task_type": "ozon_collection",
                        "dispatch_token": run.get("browser_task_resumed_at") or run.get("created_at"),
                        "contract": selected_repo.load_ozon_collection_contract(run_id),
                        "result_worker": "workbench_browser_bridge",
                        "ingest_url": f"/api/batches/{run_id}/ozon-collection",
                        "progress_url": f"/api/batches/{run_id}/ozon-collection-progress",
                        "resume_candidates": checkpoint.data["ozon_candidates"],
                    },
                    "errors": [],
                }
            if run.get("status") == "supplier_review":
                try:
                    review = selected_repo.load_supplier_review(run_id)
                except FileNotFoundError:
                    return {
                        "ok": False,
                        "code": "browser_task.supplier_review_missing",
                        "message": "Supplier review data is missing.",
                        "data": {"run_id": run_id, "status": run.get("status")},
                        "errors": ["supplier_review.json is required"],
                    }
                draft_path = selected_repo.run_dir(run_id) / "supplier_selection_draft.json"
                captured_seed_ids: set[str] = set()
                if draft_path.exists():
                    draft = selected_repo.load_supplier_selection_draft(run_id)
                    captured_seed_ids = {
                        str(product.get("seed_id") or "")
                        for product in draft.get("supplier_products", [])
                        if isinstance(product, dict) and product.get("seed_id")
                    }
                channels = []
                for channel_index, item in enumerate(review.get("items", [])):
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("seed_id") or "") in captured_seed_ids:
                        continue
                    channels.append(
                        {
                            "channel_index": channel_index,
                            "seed_id": item.get("seed_id"),
                            "ozon_product_id": item.get("ozon_product_id"),
                            "ozon_title": item.get("ozon_title"),
                            "ozon_url": item.get("ozon_url"),
                            "reference_image_url": item.get("ozon_main_image"),
                            "selected_options": item.get("selected_options") or {},
                            "dimension_evidence": item.get("dimension_evidence") or {},
                        }
                    )
                    if len(channels) >= 5:
                        break
                return {
                    "ok": True,
                    "code": "browser_task.supplier_selection_ready",
                    "message": "Managed 1688 supplier-selection channels are ready.",
                    "data": {
                        "run_id": run_id,
                        "created_at": run.get("created_at"),
                        "task_type": "supplier_selection",
                        "dispatch_token": run.get("browser_task_resumed_at") or review.get("created_at") or run.get("created_at"),
                        "contract": {
                            "run_id": run_id,
                            "contract_type": "supplier_selection",
                            "network": {"mode": "direct", "proxy_disabled": True},
                            "items": channels,
                        },
                        "result_worker": "workbench_browser_bridge",
                        "capture_url": f"/api/batches/{run_id}/supplier-selection/capture",
                        "reject_url": f"/api/batches/{run_id}/supplier-review/reject",
                    },
                    "errors": [],
                }
            if run.get("status") == "supplier_collecting":
                contract_path = selected_repo.run_dir(run_id) / "supplier_collection_contract.json"
                if not contract_path.exists():
                    return {
                        "ok": False,
                        "code": "browser_task.supplier_contract_missing",
                        "message": "Supplier collection contract is missing.",
                        "data": {"run_id": run_id, "status": run.get("status")},
                        "errors": ["supplier_collection_contract.json is required"],
                    }
                return {
                    "ok": True,
                    "code": "browser_task.supplier_collection_ready",
                    "message": "1688 supplier collection task is ready for the browser bridge.",
                    "data": {
                        "run_id": run_id,
                        "created_at": run.get("created_at"),
                        "task_type": "supplier_collection",
                        "dispatch_token": run.get("browser_task_resumed_at") or run.get("created_at"),
                        "contract": selected_repo.load_supplier_collection_contract(run_id),
                        "result_worker": "workbench_browser_bridge",
                        "ingest_url": f"/api/batches/{run_id}/supplier-collection-result",
                    },
                    "errors": [],
                }
            return {
                "ok": True,
                "code": "browser_task.none",
                "message": "No browser task is pending for this batch.",
                "data": {
                    "run_id": run_id,
                    "created_at": run.get("created_at"),
                    "status": run.get("status"),
                },
                "errors": [],
            }

        def _cancel_browser_task(self, run_id: str) -> None:
            run = selected_repo.load_run(run_id)
            run["browser_task_cancelled"] = True
            run["browser_task_cancelled_at"] = utc_now_iso()
            run["browser_task_cancel_reason"] = "user_stopped"
            selected_repo.save_run(run)
            selected_repo.append_run_event(
                run_id,
                "browser_task.cancelled",
                "Current browser task was stopped by the user.",
                {"reason": "user_stopped"},
            )
            selected_repo.save_browser_bridge_status(
                {
                    "source": "workbench_server",
                    "bridge_id": "ozon_v2_browser_extension",
                    "run_id": run_id,
                    "task_type": None,
                    "stage": "stopped",
                    "code": "browser_task.cancelled",
                    "message": "Current browser task was stopped by the user.",
                    "url": "",
                    "extension_version": None,
                }
            )

        def _resume_browser_task(self, run_id: str) -> None:
            run = selected_repo.load_run(run_id)
            if not run.get("browser_task_cancelled"):
                return
            run["browser_task_cancelled"] = False
            run["browser_task_resumed_at"] = utc_now_iso()
            selected_repo.save_run(run)
            selected_repo.append_run_event(
                run_id,
                "browser_task.resumed",
                "Browser task cancellation was cleared by continuing autopilot.",
                {},
            )

        def _active_browser_task(self) -> dict[str, Any]:
            if not selected_repo.runs_dir.exists():
                return {
                    "ok": True,
                    "code": "browser_task.none",
                    "message": "No active browser task is pending.",
                    "data": {},
                    "errors": [],
                }
            newest_run: dict[str, Any] | None = None
            for run_dir in selected_repo.runs_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                if not (run_dir / "run.json").exists():
                    continue
                run = selected_repo.load_run(run_dir.name)
                if newest_run is None or str(run.get("created_at") or "") > str(newest_run.get("created_at") or ""):
                    newest_run = run
            if newest_run is not None:
                return self._browser_task(str(newest_run["run_id"]))
            return {
                "ok": True,
                "code": "browser_task.none",
                "message": "No active browser task is pending.",
                "data": {},
                "errors": [],
            }

        def _runtime_status_payload(self) -> dict[str, Any]:
            bridge = self._bridge_status_payload()
            extension = dict(bridge)
            extension["bridge_code"] = extension.get("code")
            extension["code"] = str(bridge.get("readiness_code") or "offline")

            active_task = self._active_browser_task()
            task_data = active_task.get("data") if isinstance(active_task.get("data"), dict) else {}
            run_id = str(task_data.get("run_id") or "").strip()
            runner_status = runner.status(run_id) if run_id else {}
            run_status = str(task_data.get("status") or "").strip().lower()
            if runner_status.get("running"):
                task_code = "running"
            elif task_data.get("cancelled") is True:
                task_code = "stopped"
            elif run_status.startswith("failed"):
                task_code = "failed"
            elif not run_id or run_status == "done":
                task_code = "idle"
            else:
                task_code = "blocked"

            return {
                "ok": True,
                "code": "runtime.status",
                "message": "Workbench runtime status loaded.",
                "data": {
                    "service": {
                        "code": "online",
                        "pid": os.getpid(),
                        "host": str(self.server.server_address[0]),
                        "port": int(self.server.server_address[1]),
                    },
                    "extension": extension,
                    "task": {
                        "code": task_code,
                        "run_id": run_id or None,
                        "workbench_status": run_status or None,
                        "browser_task_code": active_task.get("code"),
                        "runner": runner_status,
                    },
                },
                "errors": [],
            }

        def _bridge_status_payload(self, status: dict[str, Any] | None = None) -> dict[str, Any]:
            payload = dict(status or selected_repo.load_browser_bridge_status())
            required = required_extension_version(selected_repo.context.project_root)
            return bridge_readiness(payload, required)

        def _should_ignore_invalidated_heartbeat(self, incoming: dict[str, Any]) -> bool:
            message = str(incoming.get("message") or "")
            source = str(incoming.get("source") or "")
            required = required_extension_version(selected_repo.context.project_root)
            incoming_version = str(incoming.get("extension_version") or "").strip() or None
            current = self._bridge_status_payload()
            return (
                source == "workbench_content_script"
                and "Extension context invalidated" in message
                and incoming_version != required
                and current.get("version_ready") is True
            )

        def _send_result(self, result: Any, run_id: str | None = None) -> None:
            status = HTTPStatus.OK if result.ok else HTTPStatus.BAD_REQUEST
            payload = result.to_dict()
            if run_id and isinstance(payload.get("data"), dict):
                payload["data"]["runner"] = runner.status(run_id)
                payload["data"]["browser_bridge"] = self._bridge_status_payload()
            self._send_json(payload, status)

        def _send_internal_error(self, exc: Exception) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts = self._path_parts(path)
            run_id = parts[2] if len(parts) >= 3 and parts[:2] == ["api", "batches"] else None
            details = {
                "path": path,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            if run_id:
                try:
                    selected_repo.append_run_event(
                        run_id,
                        "http.request_failed",
                        f"Workbench request failed: {type(exc).__name__}: {exc}",
                        details,
                    )
                except Exception:
                    pass
            self._send_json(
                {
                    "ok": False,
                    "code": "http.internal_error",
                    "message": f"Workbench request failed: {type(exc).__name__}: {exc}",
                    "data": {
                        "run_id": run_id,
                        "path": path,
                        "error_type": type(exc).__name__,
                    },
                    "errors": [str(exc)],
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(raw)

        def _send_bytes(self, payload: bytes, content_type: str, filename: str | None = None) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(payload)

        def _send_html(self, html: str) -> None:
            raw = inject_runtime_capsule(html).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(raw)

        def _send_js(self, script: str) -> None:
            raw = script.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(raw)

        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    return WorkbenchRequestHandler


def make_server(
    host: str,
    port: int,
    repo: FsRepo | None = None,
    runtime_dir: Path | None = None,
) -> ThreadingHTTPServer:
    selected_repo = repo or FsRepo()
    selected_runtime_dir = runtime_dir or selected_repo.context.project_root / "runtime" / "workbench"
    runtime_controller = WorkbenchRuntimeController(
        selected_repo.context.project_root,
        selected_runtime_dir,
        port,
    )
    server = ThreadingHTTPServer(
        (host, port),
        create_handler(selected_repo, runtime_controller=runtime_controller),
    )
    server.runtime_controller = runtime_controller  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Ozon V2 local workbench.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runtime-dir", type=Path, default=None)
    args = parser.parse_args()
    server = make_server(args.host, args.port, runtime_dir=args.runtime_dir)
    runtime_controller = server.runtime_controller  # type: ignore[attr-defined]
    runtime_controller.mark_running(os.getpid())
    print(f"Ozon V2 Workbench listening at http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime_controller.mark_stopped()


if __name__ == "__main__":
    main()



