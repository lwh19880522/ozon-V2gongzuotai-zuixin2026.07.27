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
          <div id="progressUpload" class="progress-row">
            <span class="progress-number">3</span>
            <div class="progress-copy"><strong>上传草稿</strong><span>锁定原图、字段和价格通过后逐件提交</span></div>
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
      const orderedStages = ["progressOzon", "progressSupplier", "progressUpload"];
      const supplierStatuses = new Set(["supplier_review", "supplier_collecting", "supplier_collected", "same_product_review"]);
      const uploadStatuses = new Set(["ai_filling", "image_processing", "image_processed", "draft_building", "draft_ready", "publish_submitted", "done"]);
      let currentIndex = 0;
      if (supplierStatuses.has(status)) currentIndex = 1;
      if (uploadStatuses.has(status)) currentIndex = 2;
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
      const pending = Math.max(0, Number(value.pending_count ?? (total - processed)) || 0);
      const successPercent = total ? success / total * 100 : 0;
      const failurePercent = total ? failure / total * 100 : 0;
      $("ozonProcessed").textContent = !hasProgress
        ? "当前没有正在采集的批次"
        : !total
          ? "当前批次尚未生成采集任务"
          : pending === 0
            ? `采集完成 ${processed} / ${total}`
            : `实时已保存 ${success} / ${total} · 已处理 ${processed}`;
      $("ozonSucceeded").textContent = String(success);
      $("ozonFailed").textContent = String(failure);
      $("ozonReplaced").textContent = String(Math.max(0, Number(value.replacement_count) || 0));
      $("ozonPending").textContent = String(pending);
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
      "supplier_review.replacement_recovery_started": ["替补商品恢复补采", "已返回替补商品尚未完成的采集阶段，其他成功商品保持不变。"],
      "ozon_collection.replacement_checkpoint_reconciled": ["替补采集断点已清理", "已移除被拒商品遗留的采集断点，只继续采集当前替补商品。"],
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
      const replacementPending =
        Array.isArray(run.replacement_pending_seed_ids) &&
        run.replacement_pending_seed_ids.length > 0;
      const ozonRestartable =
        (status === "ozon_collecting" && run.ozon_collected !== true) ||
        replacementPending;
      const ozonComplete = run.ozon_collected === true;
      const restartButton = $("restartOzonCollection");
      restartButton.hidden = !(ozonRestartable || ozonComplete);
      restartButton.disabled = state.restartPending || !ozonRestartable;
      restartButton.textContent = replacementPending
        ? "继续补采替补商品"
        : ozonComplete
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

    async function resolveLatestRunId() {
      const history = await api("/api/operations/batches");
      const latest = (history.data?.items || [])[0] || null;
      if (!latest?.run_id) {
        state.runId = "";
        localStorage.removeItem("ozon_v2_workbench_run_id");
        updateStageNavigation("");
        renderOzonCollectionProgress(null);
        return "";
      }
      state.runId = String(latest.run_id);
      localStorage.setItem("ozon_v2_workbench_run_id", state.runId);
      return state.runId;
    }

    async function loadRun(allowRecovery = true) {
      if (!state.runId && !(await resolveLatestRunId())) return;
      try {
        const result = await api(`/api/batches/${encodeURIComponent(state.runId)}`);
        const events = await api(`/api/batches/${encodeURIComponent(state.runId)}/events`);
        render(result, events.data.events || []);
      } catch (error) {
        if (allowRecovery && error && error.code === "workbench.batch_not_found") {
          state.runId = "";
          localStorage.removeItem("ozon_v2_workbench_run_id");
          if (await resolveLatestRunId()) return loadRun(false);
          return;
        }
        throw error;
      }
    }

    function startLivePolling() {
      if (state.pollTimer) return;
      state.pollTimer = window.setInterval(() => {
        loadRun().catch(() => {});
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
      <div class="sku-action-row"><span id="skuDecisionMessage" class="muted"></span><button id="reopenSupplierSku" type="button" hidden>重新选择 SKU</button><button id="lockSingleSupplierSku" class="primary" type="button" hidden disabled>单一 SKU，直接锁定主体</button><button id="lockSupplierSku" class="primary" disabled>锁定真实 SKU (Lock Real SKU)</button></div>
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
    const subjectDrafts = new Map();
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
      if (context.readOnly) label.classList.add("blocked");
      if (context.analysis.recommendedSkuId === option.supplier_sku_id) label.classList.add("recommended");
      const radio = document.createElement("input"); radio.type = "radio"; radio.name = "supplierSku"; radio.value = option.supplier_sku_id || "";
      radio.checked = !context.readOnly && ((!!context.lockedSku && context.lockedSku.supplier_sku_id === option.supplier_sku_id)
        || (!context.receipt && (context.selectedSupplierSkuId
          ? context.selectedSupplierSkuId === option.supplier_sku_id
          : context.singlePageSku || context.analysis.recommendedSkuId === option.supplier_sku_id)));
      radio.disabled = !!context.receipt || context.readOnly === true;
      radio.addEventListener("change", updateSkuLockButton);
      const main = document.createElement("div"); main.className = "sku-option-main";
      const imageUrl = Array.isArray(option.image_urls) ? String(option.image_urls[0] || "") : "";
      const fallbackUrls = [imageUrl, ...(context.supplierFallbackImages || [])]
        .map((value) => String(value || "").trim())
        .filter((value, index, values) => value && values.indexOf(value) === index);
      if (fallbackUrls.length) {{
        let fallbackIndex = 0;
        const image = document.createElement("img"); image.className = "sku-option-image"; image.src = fallbackUrls[0]; image.alt = option.raw_label || "1688 SKU"; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
        image.addEventListener("error", () => {{
          fallbackIndex += 1;
          if (fallbackIndex < fallbackUrls.length) {{
            image.src = fallbackUrls[fallbackIndex];
            return;
          }}
          image.hidden = true;
          main.classList.add("no-image");
        }});
        main.append(image);
      }} else {{
        main.classList.add("no-image");
      }}
      const copy = document.createElement("div");
      if (context.readOnly) {{
        const badge = document.createElement("span"); badge.className = "sku-badge"; badge.textContent = "已识别但不可锁定"; copy.append(badge);
        const mediaNote = document.createElement("span"); mediaNote.className = "sku-comparison"; mediaNote.textContent = imageUrl
          ? "SKU 证据仍不完整"
          : "无 SKU 专属图；下方仅显示公共商品图";
        copy.append(mediaNote);
      }}
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

    function isPageUniqueSupplierSku(item, options) {{
      const serverDecision = item && item.supplier_sku_decision
        ? item.supplier_sku_decision
        : {{}};
      const option = Array.isArray(options) && options.length === 1
        ? options[0]
        : null;
      return serverDecision.single_option_confirmable === true
        && Number(serverDecision.canonical_option_count) === 1
        && !!option
        && String(serverDecision.single_option_supplier_sku_id || "")
          === String(option.supplier_sku_id || "");
    }}

    function canRecaptureSupplier(item) {{
      const stageAllowsRecapture = ["supplier_review", "supplier_collected", "image_processing"].includes(state.status);
      return stageAllowsRecapture
        && !item.supplier_sku_selection
        && !item.subject_master;
    }}

    function candidateOnlyMissingSkuImage(candidate) {{
      const allowed = new Set([
        "SKU-bound image_urls are required",
        "complete supplier SKU evidence is required",
      ]);
      const errors = Array.isArray(candidate && candidate.validation_errors)
        ? candidate.validation_errors
        : [];
      return !!String(candidate && candidate.supplier_sku_id || "").trim()
        && errors.length > 0
        && errors.every((error) => allowed.has(String(error)));
    }}

    function updateSkuLockButton() {{
      const item = state.items[state.evidenceIndex];
      const button = $("lockSupplierSku");
      const singleButton = $("lockSingleSupplierSku");
      const reopenButton = $("reopenSupplierSku");
      if (!item) {{
        button.disabled = true;
        button.hidden = false;
        singleButton.hidden = true;
        singleButton.disabled = true;
        reopenButton.hidden = true;
        reopenButton.disabled = true;
        return;
      }}
      const options = item.supplier_sku_options || [];
      const receipt = item.supplier_sku_selection || null;
      const singlePageSku = isPageUniqueSupplierSku(item, options);
      button.textContent = receipt
        ? "SKU 已确认 (Confirmed)"
        : "确认所选 SKU";
      button.hidden = !!receipt || singlePageSku;
      button.disabled = !!receipt || !selectedSkuOption(item);
      singleButton.hidden = !!receipt || !singlePageSku;
      singleButton.disabled = !!receipt || !singlePageSku || options.length !== 1;
      reopenButton.hidden = !receipt;
      reopenButton.disabled = !receipt;
    }}

    function selectedSubjectEvidenceUrls() {{
      return Array.from(document.querySelectorAll('input[name="subjectEvidence"]:checked'))
        .map((input) => input.value)
        .filter(Boolean);
    }}

    function rememberSubjectDraft(seedId) {{
      if (!seedId) return;
      const evidenceInputs = document.querySelectorAll('input[name="subjectEvidence"]');
      if (!evidenceInputs.length) return;
      subjectDrafts.set(String(seedId), {{
        sourceImageUrls: selectedSubjectEvidenceUrls(),
        visibleSubjectQuantity: $("visibleSubjectQuantity").value,
      }});
    }}

    function updateSubjectEvidenceCount() {{
      const selectedCount = selectedSubjectEvidenceUrls().length;
      $("subjectEvidenceCount").textContent = `已选 ${{selectedCount}} 张 (${{selectedCount}} Selected)`;
      $("confirmSubjectMaster").disabled = !!state.items[state.evidenceIndex]?.subject_master || selectedCount === 0;
    }}

    function renderSkuDecision() {{
      const optionsRoot = $("skuOptions");
      const subjectRoot = $("subjectMasterImages");
      const previousItemSeedId = optionsRoot.dataset.seedId || "";
      const previousSubjectSeedId = subjectRoot.dataset.seedId || "";
      const previousSelection = document.querySelector('input[name="supplierSku"]:checked');
      const previousInteraction = {{
        selectedSupplierSkuId: previousSelection ? previousSelection.value : "",
        otherOptionsOpen: document.querySelector("details.sku-other-options")?.open === true,
      }};
      rememberSubjectDraft(previousSubjectSeedId);
      optionsRoot.replaceChildren();
      subjectRoot.replaceChildren();
      if (!state.items.length) {{
        optionsRoot.dataset.seedId = "";
        subjectRoot.dataset.seedId = "";
        $("skuDecisionContext").textContent = "等待 1688 采集结果";
        $("skuDecisionMessage").textContent = "尚无可审核商品。";
        $("lockSupplierSku").disabled = true;
        $("lockSingleSupplierSku").hidden = true;
        $("lockSingleSupplierSku").disabled = true;
        $("subjectMasterArea").hidden = true;
        return;
      }}
      const item = state.items[state.evidenceIndex];
      const preservedInteraction = previousItemSeedId === String(item.seed_id || "")
        ? previousInteraction
        : {{ selectedSupplierSkuId:"", otherOptionsOpen:false }};
      optionsRoot.dataset.seedId = String(item.seed_id || "");
      subjectRoot.dataset.seedId = String(item.seed_id || "");
      const options = item.supplier_sku_options || [];
      const blockedOptions = item.supplier_sku_candidates || [];
      const skuGroups = item.supplier_sku_groups || [];
      const receipt = item.supplier_sku_selection || null;
      const lockedSku = receipt && receipt.supplier_sku ? receipt.supplier_sku : null;
      const subjectMaster = item.subject_master || null;
      if (subjectMaster) subjectDrafts.delete(String(item.seed_id || ""));
      const subjectDraft = subjectDrafts.get(String(item.seed_id || "")) || null;
      const skuNeedsConfirmation = !!item.supplier_product && !options.length && !receipt;
      const singlePageSku = isPageUniqueSupplierSku(item, options);
      const analysis = analyzeSupplierSkuOptions(item, options);
      const blockedAnalysis = analyzeSupplierSkuOptions(item, blockedOptions);
      const candidatesOnlyMissingImages = blockedOptions.length > 0
        && blockedOptions.every(candidateOnlyMissingSkuImage);
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
      modeNote.textContent = !options.length && blockedOptions.length
        ? `已识别 ${{blockedOptions.length}} 个真实 SKU，但证据不完整，当前不可锁定。公共商品图仅供核对。`
        : singlePageSku
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
      const candidateContext = {{
        analysis,
        receipt,
        lockedSku,
        singlePageSku,
        selectedSupplierSkuId: preservedInteraction.selectedSupplierSkuId || "",
        supplierFallbackImages: item.supplier_product && Array.isArray(item.supplier_product.images)
          ? item.supplier_product.images
          : []
      }};
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
        otherDetails.open = preservedInteraction.otherOptionsOpen === true;
        const summary = document.createElement("summary"); summary.textContent = matchingCandidates.length
          ? `查看其他规格（${{hiddenCandidates.length}}）`
          : `查看其他规格（全部 ${{hiddenCandidates.length}} 项）`;
        const otherRoot = document.createElement("div");
        renderSupplierSkuGroup(otherRoot, matchingCandidates.length ? "其他未匹配规格" : "全部真实规格", hiddenCandidates, candidateContext);
        otherDetails.append(summary, otherRoot); candidatesRoot.append(otherDetails);
      }}
      if (blockedOptions.length) {{
        const blockedContext = {{
          ...candidateContext,
          analysis: blockedAnalysis,
          readOnly: true,
        }};
        renderSupplierSkuGroup(
          candidatesRoot,
          `已识别但不可锁定（${{blockedOptions.length}}）`,
          blockedAnalysis.candidates,
          blockedContext,
        );
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
        if (!candidatesOnlyMissingImages && canRecaptureSupplier(item)) {{
          const recaptureSkuButton = document.createElement("button");
          recaptureSkuButton.type = "button";
          recaptureSkuButton.className = "secondary-button";
          recaptureSkuButton.textContent = "重新采集此商品 SKU";
          recaptureSkuButton.addEventListener("click", () => recaptureSupplier(item, recaptureSkuButton));
          candidatesRoot.append(recaptureSkuButton);
        }}
      }}
      updateSkuLockButton();
      $("skuDecisionMessage").className = receipt ? "success" : "muted";
      $("skuDecisionMessage").textContent = receipt
        ? "真实供应商 SKU 已锁定，后续采购、标题、属性和图片均以此为准。"
        : candidatesOnlyMissingImages
          ? "已识别真实 SKU，但 1688 未提供 SKU 专属图；公共商品图已展示供核对，系统不会放宽锁定门禁，也无需反复重新采集。"
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
      const recommendedUrls = new Set(savedUrls.length
        ? savedUrls
        : subjectDraft ? subjectDraft.sourceImageUrls : (skuImages.length ? skuImages : candidates.slice(0, 1).map((item) => item.url)));
      candidates.forEach((candidate, index) => {{
        const url = candidate.url;
        const label = document.createElement("label"); label.className = "subject-choice";
        const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.name = "subjectEvidence"; checkbox.value = url;
        checkbox.checked = recommendedUrls.has(url);
        checkbox.disabled = !!subjectMaster;
        checkbox.addEventListener("change", () => {{
          rememberSubjectDraft(String(item.seed_id || ""));
          updateSubjectEvidenceCount();
        }});
        const badge = document.createElement("span"); badge.className = "subject-source"; badge.textContent = candidate.sources.join(" / ");
        const image = document.createElement("img"); image.src = url; image.alt = `SKU subject ${{index + 1}}`; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
        label.append(checkbox, badge, image); subjectRoot.append(label);
      }});
      if (!candidates.length) subjectRoot.append(field("供应商主体证据图片", null));
      const quantityInput = $("visibleSubjectQuantity");
      quantityInput.value = subjectMaster
        ? subjectMaster.visible_subject_quantity
        : subjectDraft ? subjectDraft.visibleSubjectQuantity : lockedSku.set_quantity;
      quantityInput.disabled = !!subjectMaster;
      quantityInput.oninput = subjectMaster
        ? null
        : () => rememberSubjectDraft(String(item.seed_id || ""));
      $("subjectSetComposition").textContent = (lockedSku.set_composition || []).join(" / ") || valueText(lockedSku.selected_options);
      updateSubjectEvidenceCount();
    }}

    async function submitSupplierSkuLock(item, option) {{
      return await api(`/api/batches/${{encodeURIComponent(runId)}}/supplier-sku`, {{
        method:"POST",
        body:JSON.stringify({{
          seed_id:item.seed_id,
          supplier_sku_id:option.supplier_sku_id,
          differences:[{{ field:"sku", ozon:item.selected_options || {{}}, supplier:option.selected_options || {{}} }}]
        }})
      }});
    }}

    async function persistSupplierSkuLock(item, option, button) {{
      if (!item || !option) return;
      button.disabled = true;
      try {{
        await submitSupplierSkuLock(item, option);
        await load();
      }} catch (error) {{
        $("skuDecisionMessage").className = "error";
        $("skuDecisionMessage").textContent = error.message || "SKU 锁定失败 (Lock Failed)";
        button.disabled = false;
      }}
    }}

    async function lockSupplierSku() {{
      const item = state.items[state.evidenceIndex];
      const option = selectedSkuOption(item);
      await persistSupplierSkuLock(item, option, $("lockSupplierSku"));
    }}

    async function lockSingleSupplierSku() {{
      const item = state.items[state.evidenceIndex];
      const options = item && Array.isArray(item.supplier_sku_options)
        ? item.supplier_sku_options
        : [];
      if (!isPageUniqueSupplierSku(item || {{}}, options)) return;
      const option = options[0];
      const subjectUrls = Array.from(new Set(
        (Array.isArray(option.image_urls) ? option.image_urls : [])
          .map((value) => String(value || "").trim())
          .filter(Boolean)
      ));
      const button = $("lockSingleSupplierSku");
      if (!subjectUrls.length) {{
        $("skuDecisionMessage").className = "error";
        $("skuDecisionMessage").textContent = "该单一 SKU 没有可锁定的供应商主体图，请重新采集。";
        return;
      }}
      button.disabled = true;
      let skuLocked = false;
      try {{
        await submitSupplierSkuLock(item, option);
        skuLocked = true;
        const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/subject-master`, {{
          method:"POST",
          body:JSON.stringify({{
            seed_id:item.seed_id,
            source_image_urls:subjectUrls,
            visible_subject_quantity:Number(option.set_quantity || 1)
          }})
        }});
        if (result.data.all_subject_masters_confirmed === true) {{
          window.location.assign(`/batches/${{encodeURIComponent(runId)}}/upload`);
          return;
        }}
        await load();
      }} catch (error) {{
        if (skuLocked) await load();
        $("skuDecisionMessage").className = "error";
        $("skuDecisionMessage").textContent = skuLocked
          ? "SKU 已锁定，但主体图自动确认失败；请在下方检查并手动确认主体证据。"
          : (error.message || "SKU 锁定失败 (Lock Failed)");
        button.disabled = false;
      }}
    }}

    async function reopenSupplierSku() {{
      const item = state.items[state.evidenceIndex];
      if (!item || !item.supplier_sku_selection) return;
      if (!window.confirm("会停止尚未开工的图片任务，并把旧 SKU 和主体证据保存到历史记录，是否继续？")) return;
      $("reopenSupplierSku").disabled = true;
      try {{
        await api(`/api/batches/${{encodeURIComponent(runId)}}/supplier-sku/reopen`, {{
          method:"POST",
          body:JSON.stringify({{ seed_id:item.seed_id }})
        }});
        await load();
      }} catch (error) {{
        $("skuDecisionMessage").className = "error";
        $("skuDecisionMessage").textContent = error.message || "SKU 重选失败 (Reopen Failed)";
        $("reopenSupplierSku").disabled = false;
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
          window.location.assign(`/batches/${{encodeURIComponent(runId)}}/upload`);
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
        // A captured URL is not proof that the supplier is correct. Keep the
        // rejection/refill escape hatch available so a wrong 1688 match cannot
        // strand the whole batch.
        rejectButton.disabled = state.status !== "supplier_review";
        rejectButton.addEventListener("click", () => rejectSupplier(item, rejectButton));
        const recaptureButton = document.createElement("button");
        recaptureButton.type = "button";
        recaptureButton.className = "secondary-button";
        recaptureButton.textContent = "重新采集 (Re-collect)";
        recaptureButton.disabled = !canRecaptureSupplier(item);
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
          ? "Ozon 与 1688 数据已回传；请逐件锁定真实 SKU 并确认主体原图，完成后直接进入上传草稿。"
          : "采集结果仍有缺失字段，不能进入上传草稿。";
        return;
      }}
      if (state.status === "image_processing") {{
        $("approveCollection").disabled = true;
        $("message").className = "muted";
        $("message").textContent = "本批次主体原图已锁定；请进入上传草稿完善字段、价格并逐件提交。";
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
    $("lockSingleSupplierSku").addEventListener("click", lockSingleSupplierSku);
    $("reopenSupplierSku").addEventListener("click", reopenSupplierSku);
    $("confirmSubjectMaster").addEventListener("click", confirmSubjectMaster);
    $("ozonEvidencePrev").addEventListener("click", () => moveEvidence(-1));
    $("ozonEvidenceNext").addEventListener("click", () => moveEvidence(1));
    $("supplierEvidencePrev").addEventListener("click", () => moveEvidence(-1));
    $("supplierEvidenceNext").addEventListener("click", () => moveEvidence(1));
    async function poll() {{
      if (!["supplier_review", "supplier_collecting", "supplier_collected", "image_processing"].includes(state.status)) return;
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


def build_image_generation_command(run_id: str) -> str:
    return (
        f"启动 Ozon V2 工作台专用生图：批次 {run_id}。"
        "读取并严格执行 skills/ozon-product-media-generator/SKILL.md；这是唯一生图技能入口。"
        "自动启动免费公网媒体通道，不要求用户提供域名、账户、令牌或公网地址。"
        "全程单线程，一次只处理一件商品；先生成并锁定白底主体图，冻结路径和哈希。"
        "随后只把这同一张白底主体图作为唯一图片参考，结合固定提示词一次生成 4×2 八宫格，"
        "再按行优先裁成 8 张严格 3:4 图片。主体外形、颜色、数量、结构、配件和规格不得改变。"
        "逐图验收、只返修失败槽位，完成图库与视频上传后再领取下一件商品。"
        "持续到 pending=0 且 in_progress=0，最后关闭临时公网通道。"
    )


def build_image_workspace_html(run_id: str) -> str:
    safe_run_id = json.dumps(run_id)
    controller_command = build_image_generation_command(run_id)
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
    .batch-generation-overview {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:-2px 0 14px; padding:10px 12px; border:1px solid var(--line); border-radius:6px; background:var(--surface); }}
    .batch-generation-overview strong {{ margin-right:4px; font-size:11px; }}
    .queue-state-chip {{ display:inline-flex; align-items:center; gap:5px; padding:4px 7px; border-radius:4px; color:var(--muted); background:var(--soft); font-size:10px; }}
    .queue-state-chip b {{ color:var(--text); font-size:12px; }}
    .queue-state-chip.warn {{ color:var(--amber); background:var(--amber-soft); }} .queue-state-chip.stop {{ color:var(--red); background:var(--red-soft); }} .queue-state-chip.ready {{ color:var(--green); background:var(--green-soft); }}
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
    .image-product {{ height:100%; min-height:0; display:grid; grid-template-rows:auto auto minmax(0,1fr); border:1px solid var(--line); border-radius:5px; overflow:hidden; background:#fff; }}
    .product-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 12px; border-bottom:1px solid var(--line); background:var(--soft); }}
    .product-head strong {{ font-size:13px; }} .product-head span {{ color:var(--muted); font-size:10px; overflow-wrap:anywhere; }}
    .product-state-banner {{ display:flex; align-items:center; gap:8px; min-height:34px; padding:7px 12px; border-bottom:1px solid var(--line); color:var(--muted); background:#fff; font-size:10px; }}
    .product-state-banner.warn {{ color:#74410a; background:#fffaf0; }} .product-state-banner.stop {{ color:var(--red); background:var(--red-soft); }} .product-state-banner.ready {{ color:var(--green); background:var(--green-soft); }}
    .product-state-banner a {{ margin-left:auto; color:var(--blue); font-weight:650; text-decoration:none; }}
    .source-grid {{ min-height:0; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); grid-template-rows:minmax(0,1fr); gap:1px; background:var(--line); }}
    .source-panel {{ min-width:0; min-height:0; display:grid; grid-template-rows:auto minmax(0,1fr); padding:11px; overflow:hidden; background:#fff; }}
    .source-title {{ display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:9px; font-size:11px; font-weight:700; }} .source-title span {{ color:var(--muted); font-weight:400; }}
    .gallery {{ min-height:0; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); grid-auto-rows:minmax(150px,190px); align-content:start; gap:8px; padding-right:4px; overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; }}
    .source-image-link {{ min-width:0; display:block; }}
    .gallery img {{ width:100%; height:100%; min-height:150px; max-height:190px; object-fit:contain; border:1px solid var(--line); border-radius:4px; background:#fff; }}
    .generated-review-panel {{ grid-template-rows:auto minmax(0,1fr); }}
    .generated-review-grid {{ min-height:0; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); align-content:start; gap:8px; padding-right:4px; overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; }}
    .generated-review-card {{ min-width:0; padding:7px; border:1px solid var(--line); border-radius:5px; background:#fff; transition:border-color .15s ease,box-shadow .15s ease; }}
    .generated-review-card.selected {{ border-color:#e07b83; box-shadow:0 0 0 2px rgba(180,35,44,.08); }}
    .generated-review-card.repair-incomplete {{ border-color:var(--red); background:#fff9fa; box-shadow:0 0 0 2px rgba(180,35,44,.1); }}
    .generated-review-card.repairing {{ border-color:#e4b862; background:#fffaf0; }}
    .generated-review-head {{ display:flex; align-items:center; justify-content:space-between; gap:6px; margin-bottom:6px; font:10px Consolas,"Courier New",monospace; }}
    .generated-review-head strong {{ color:var(--text); }}
    .repair-badge {{ padding:2px 5px; border-radius:3px; color:var(--green); background:var(--green-soft); font:9px "Segoe UI","Microsoft YaHei",sans-serif; }}
    .repairing .repair-badge {{ color:var(--amber); background:var(--amber-soft); }}
    .generated-image-link {{ position:relative; display:block; border-radius:4px; overflow:hidden; }}
    .generated-image-link::after {{ content:"点击查看高清原图"; position:absolute; right:5px; bottom:5px; padding:3px 6px; border-radius:3px; color:#fff; background:rgba(24,34,53,.82); font-size:9px; opacity:.9; }}
    .generated-image-link:hover::after {{ background:var(--blue); }}
    .generated-review-card img {{ width:100%; height:108px; display:block; object-fit:contain; border:1px solid var(--line); border-radius:4px; background:#fff; }}
    .generated-original-link {{ display:block; margin-top:5px; color:var(--blue); text-align:center; font-size:10px; font-weight:650; text-decoration:none; }}
    .generated-original-link:hover {{ text-decoration:underline; }}
    .slot-reference-proof {{ display:block; margin-top:5px; padding:4px 6px; border-radius:3px; color:var(--green); background:var(--green-soft); text-align:center; font-size:9px; font-weight:650; text-decoration:none; }}
    .slot-reference-proof:hover {{ text-decoration:underline; }}
    .slot-reference-proof.missing {{ color:var(--red); background:var(--red-soft); }}
    .repair-check {{ display:flex; align-items:flex-start; gap:6px; margin-top:7px; color:var(--text); font-size:10px; cursor:pointer; }}
    .repair-check input {{ margin:2px 0 0; accent-color:var(--red); }}
    .repair-fields {{ display:grid; gap:5px; margin-top:7px; }}
    .repair-fields[hidden] {{ display:none; }}
    .repair-issue,.repair-note {{ width:100%; border:1px solid var(--line); border-radius:4px; color:var(--text); background:#fff; font:10px/1.35 "Segoe UI","Microsoft YaHei",sans-serif; }}
    .repair-issue {{ min-height:30px; padding:0 6px; }}
    .repair-note {{ min-height:56px; padding:6px; resize:vertical; }}
    .repair-field-error {{ color:var(--red); font-size:9px; }} .repair-field-error[hidden] {{ display:none; }}
    .repair-feedback-summary {{ margin-top:7px; padding:6px; border-radius:4px; color:#74410a; background:var(--amber-soft); font-size:9px; overflow-wrap:anywhere; }}
    .empty {{ min-height:0; height:100%; display:grid; place-items:center; padding:18px; border:1px dashed #c9d1dc; border-radius:4px; color:var(--muted); background:var(--soft); text-align:center; font-size:11px; }}
    .generation-empty {{ color:var(--amber); background:#fffaf0; border-color:#e8c98f; }}
    .gate-list {{ padding:6px 14px 14px; }} .gate-row {{ display:grid; grid-template-columns:22px minmax(0,1fr); gap:9px; padding:11px 0; border-bottom:1px solid #edf0f4; }}
    .gate-row:last-child {{ border-bottom:0; }} .gate-icon {{ width:22px; height:22px; display:grid; place-items:center; border-radius:50%; color:var(--green); background:var(--green-soft); font-size:11px; font-weight:700; }}
    .gate-row.blocked .gate-icon {{ color:var(--amber); background:var(--amber-soft); }} .gate-row strong {{ display:block; font-size:11px; }} .gate-row span {{ display:block; margin-top:2px; color:var(--muted); font-size:10px; }}
    .gate-callout {{ margin:0 14px 14px; padding:11px 12px; border-left:3px solid var(--amber); color:#74410a; background:#fff8eb; font-size:11px; }}
    .controller-panel {{ margin:0 14px 14px; padding:10px 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); }}
    .controller-panel strong {{ display:block; font-size:11px; }} .controller-panel p {{ margin:3px 0 8px; color:var(--muted); font-size:10px; }}
    .controller-variant {{ padding:9px; border:1px solid #b8c7ef; border-radius:5px; background:#f7f9ff; }}
    .controller-variant-head {{ display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:6px; }}
    .controller-badge {{ padding:2px 6px; border-radius:3px; color:var(--blue); background:var(--blue-soft); font-size:9px; font-weight:700; }}
    .controller-command {{ max-height:104px; overflow:auto; padding:9px; border:1px solid var(--line); border-radius:4px; background:var(--soft); font:10px/1.45 Consolas,"Courier New",monospace; white-space:pre-wrap; overflow-wrap:anywhere; user-select:text; }}
    .controller-actions {{ display:flex; align-items:center; gap:8px; margin-top:8px; }}
    .controller-actions button {{ min-height:32px; padding:0 10px; border:1px solid var(--blue); border-radius:4px; color:#fff; background:var(--blue); cursor:pointer; }}
    .controller-actions .production-copy {{ width:100%; min-height:38px; font-weight:700; }}
    .copy-status {{ color:var(--green); font-size:10px; }}
    .job-controls {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:0 14px 14px; }}
    .job-controls button {{ min-height:36px; border:1px solid var(--line); border-radius:5px; background:#fff; cursor:pointer; }}
    .job-controls button:disabled {{ opacity:.45; cursor:not-allowed; }}
    .job-controls .repair-submit {{ grid-column:1/-1; border-color:var(--red); color:#fff; background:var(--red); font-weight:650; }}
    .job-controls .repair-submit:disabled {{ opacity:1; border-color:#e2b9bd; border-style:dashed; color:#985a60; background:#f8e9eb; cursor:not-allowed; }}
    .job-controls .approve-submit {{ grid-column:1/-1; border-color:var(--green); color:#fff; background:var(--green); font-weight:700; }}
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
        <section id="batchGenerationOverview" class="batch-generation-overview" aria-live="polite"><strong>整批生图状态 (Batch Status)</strong><span class="queue-state-chip">正在加载…</span></section>
        <div class="image-layout">
          <section class="panel"><div class="panel-head"><h3>素材工作区 (Source Assets)</h3><div class="image-pager" aria-label="商品切换 (Product Navigation)"><button id="previousImageItem" type="button" title="上一件 (Previous)" aria-label="上一件">&#8592;</button><span id="imageItemPosition" class="image-position" aria-live="polite">0 / 0</span><button id="nextImageItem" type="button" title="下一件 (Next)" aria-label="下一件">&#8594;</button><span class="pill">单 SKU</span></div></div><div id="imageItems" class="image-items image-items-viewport"></div></section>
          <aside id="imageGate" class="panel"><div class="panel-head"><h3>图片质量门禁 (Image Gate)</h3></div><div class="gate-list">
            <div class="gate-row"><span class="gate-icon">1</span><div><strong>Ozon 参考图 (Ozon Reference)</strong><span>只用于学习风格、布局和信息构造。</span></div></div>
            <div class="gate-row"><span class="gate-icon">2</span><div><strong>供应商原图 (Supplier Source)</strong><span>保持真实商品主体、颜色和结构。</span></div></div>
            <div class="gate-row blocked"><span class="gate-icon">!</span><div><strong>Codex 生图队列 (Codex Image Queue)</strong><span id="gateMessage">等待真实 SKU 与主体证据确认。</span></div></div>
          <div class="controller-panel">
            <strong>Ozon 专用生图执行 (Product Media Generation)</strong>
            <p>用于日常整批生图和返修续跑。已审核通过的图片会保留，只处理待生成或待返修槽位；不会上传。</p>
            <div class="controller-variant">
              <div class="controller-variant-head"><strong>唯一生图 Skill 命令 (Single Skill Command)</strong><span class="controller-badge">单线程生产 / 返修续跑</span></div>
              <div id="imageControllerCommand" class="controller-command">{controller_command_html}</div>
              <div class="controller-actions">
                <button id="copyImageControllerCommand" class="production-copy" type="button">复制整批生图命令 (Copy Production Command)</button>
              </div>
              <span id="imageControllerCopyStatus" class="copy-status" aria-live="polite">复制后粘贴到当前 Codex 对话执行</span>
            </div>
          </div>
          </div><div class="gate-callout">Ozon 工作台只使用一个专用生图 Skill，并严格单线程处理：一次完成一件商品后再领取下一件。每件商品先锁定白底主体图，再以该图作为唯一图片参考一次生成 4×2 八宫格并裁成 8 张 3:4 图片；公网媒体通道由 Codex 自动启动。</div><div id="imageJobControls" class="job-controls"><div id="imageJobStatus" class="job-status">当前商品尚未入队</div><button id="approveImageJob" class="approve-submit" type="button" disabled>确认本件 8 张图片可用</button><button id="submitImageRepairs" class="repair-submit" type="button" disabled>提交选中图片返修 (Repair Selected)</button><div id="repairSelectionStatus" class="repair-selection-status" aria-live="polite"></div><button id="stopImageJob" type="button" disabled>停止生图 (Stop Generation)</button><button id="resumeImageJob" type="button" disabled>继续生图 (Resume Generation)</button></div></aside>
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
    async function copyCommand(commandElement, statusElement) {{
      const command = commandElement.textContent.trim();
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
        statusElement.textContent = "命令已复制，请粘贴到当前 Codex 对话执行";
      }} catch (error) {{
        statusElement.textContent = "\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u9009\u62e9 (Copy Failed)";
      }}
    }}
    async function copyControllerCommand() {{ await copyCommand(imageControllerCommand, imageControllerCopyStatus); }}
    copyImageControllerCommand.addEventListener("click", copyControllerCommand);
    let imageWorkspaceItems = [];
    let imageWorkspaceIndex = 0;
    let imageWorkspaceSignature = "";
    async function api(path, options = {{}}) {{ const response = await fetch(path, {{ headers: {{ "Content-Type":"application/json" }}, ...options }}); const body = await response.json(); if (!response.ok) throw body; return body; }}
    function renderImages(container, images, emptyText) {{
      if (!images.length) {{ const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = emptyText; container.append(empty); return; }}
      const gallery = document.createElement("div"); gallery.className = "gallery";
      images.forEach((src) => {{
        const imageLink = document.createElement("a"); imageLink.className = "source-image-link"; imageLink.href = src; imageLink.target = "_blank"; imageLink.rel = "noopener"; imageLink.title = "查看高清原图";
        const image = document.createElement("img"); image.src = src; image.alt = "Product source image"; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
        imageLink.append(image); gallery.append(imageLink);
      }});
      container.append(gallery);
    }}
    function sourcePanel(title, images, emptyText) {{
      const panel = document.createElement("div"); panel.className = "source-panel";
      const heading = document.createElement("div"); heading.className = "source-title"; heading.innerHTML = `<strong>${{title}}</strong><span>${{images.length}} 张</span>`;
      panel.append(heading); renderImages(panel, images, emptyText); return panel;
    }}
    const repairIssueOptions = [
      ["product_truth", "主体、数量、颜色或结构错误"],
      ["scene_quality", "场景单调、重复、不真实或不美观"],
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
      const approveButton = $("approveImageJob");
      const status = $("repairSelectionStatus");
      const selected = [...document.querySelectorAll(".generated-review-card")].filter((card) => card.querySelector('input[type="checkbox"]')?.checked);
      const missingIssue = selected.filter((card) => !card.querySelector(".repair-issue")?.value);
      const missingOtherNote = selected.filter((card) => {{
        const issue = card.querySelector(".repair-issue");
        const note = card.querySelector(".repair-note");
        return issue?.value === "other" && !note?.value.trim();
      }});
      selected.forEach((card) => {{
        const issueMissing = missingIssue.includes(card);
        const noteMissing = missingOtherNote.includes(card);
        const fieldError = card.querySelector(".repair-field-error");
        card.classList.toggle("repair-incomplete", issueMissing || noteMissing);
        if (fieldError) {{
          fieldError.hidden = !(issueMissing || noteMissing);
          fieldError.textContent = issueMissing ? "请选择这张图片的问题类型" : noteMissing ? "选择“其他问题”时必须填写说明" : "";
        }}
      }});
      const valid = selected.length > 0 && !missingIssue.length && !missingOtherNote.length;
      button.disabled = !valid;
      const currentJob = imageWorkspaceItems[imageWorkspaceIndex]?.image_job || null;
      approveButton.disabled = !currentJob || currentJob.status !== "manual_review_required" || selected.length > 0 || (currentJob.slots || []).filter((slot) => slot.status === "accepted").length !== 8;
      if (!selected.length) {{
        button.textContent = "提交选中图片返修 (Repair Selected)";
        status.className = "repair-selection-status";
        status.textContent = "勾选不合格图片后，请逐张选择问题类型";
      }} else if (missingIssue.length) {{
        button.textContent = `还需填写 ${{missingIssue.length}} 张问题类型`;
        status.className = "repair-selection-status error";
        status.textContent = `还需为 ${{missingIssue.length}} 张图片选择问题类型：${{missingIssue.map((card) => card.dataset.slotId).join("、")}}`;
      }} else if (missingOtherNote.length) {{
        button.textContent = `还需填写 ${{missingOtherNote.length}} 张其他问题说明`;
        status.className = "repair-selection-status error";
        status.textContent = `还需为 ${{missingOtherNote.length}} 张“其他问题”填写说明：${{missingOtherNote.map((card) => card.dataset.slotId).join("、")}}`;
      }} else {{
        button.textContent = `提交选中 ${{selected.length}} 张图片返修 (Repair Selected)`;
        status.className = "repair-selection-status success";
        status.textContent = `已填写完整，可以提交 ${{selected.length}} 张返修`;
      }}
    }}
    function generatedReviewPanel(item) {{
      const panel = document.createElement("div"); panel.className = "source-panel generated-review-panel";
      const job = item.image_job || {{}};
      const slots = (job.slots || []).filter((slot) => slot.accepted_path);
      const heading = document.createElement("div"); heading.className = "source-title"; heading.innerHTML = `<strong>最终成图总览 (Final Gallery)</strong><span>${{slots.length}} 张 · 点击图片查看高清原图</span>`;
      panel.append(heading);
      if (!slots.length) {{ const empty = document.createElement("div"); empty.className = "empty generation-empty"; empty.textContent = "等待 Codex 生图子智能体回传真实结果"; panel.append(empty); return panel; }}
      const grid = document.createElement("div"); grid.className = "generated-review-grid";
      slots.forEach((slot) => {{
        const card = document.createElement("article"); card.className = "generated-review-card"; card.dataset.slotId = slot.slot_id;
        if (slot.status === "repair_pending") card.classList.add("repairing");
        const cardHead = document.createElement("div"); cardHead.className = "generated-review-head";
        const slotName = document.createElement("strong"); slotName.textContent = slot.slot_id;
        const badge = document.createElement("span"); badge.className = "repair-badge"; badge.textContent = job.status === "completed" ? "已确认" : slot.status === "repair_pending" ? "等待返修" : slot.status === "accepted" ? "待审核" : slot.status;
        cardHead.append(slotName, badge);
        const fullImageUrl = generatedImageUrl(job, slot);
        const imageLink = document.createElement("a"); imageLink.className = "generated-image-link"; imageLink.href = fullImageUrl; imageLink.target = "_blank"; imageLink.rel = "noopener"; imageLink.title = `查看 ${{slot.slot_id}} 高清原图`;
        const image = document.createElement("img"); image.src = fullImageUrl; image.alt = `${{slot.slot_id}} generated product image`; image.loading = "lazy";
        imageLink.append(image);
        const fullResolutionLink = document.createElement("a"); fullResolutionLink.className = "generated-original-link"; fullResolutionLink.href = fullImageUrl; fullResolutionLink.target = "_blank"; fullResolutionLink.rel = "noopener"; fullResolutionLink.textContent = "查看高清原图";
        const mapping = slot.ozon_reference_mapping || {{}};
        const referenceProof = document.createElement(mapping.reference_url ? "a" : "span");
        referenceProof.className = `slot-reference-proof${{mapping.reference_slot_index ? "" : " missing"}}`;
        referenceProof.textContent = mapping.reference_slot_index
          ? `对应 Ozon 参考图 #${{mapping.reference_slot_index}}${{mapping.reference_reused ? " · 复用" : ""}}`
          : "缺少逐槽 Ozon 参考图回执";
        if (mapping.reference_url) {{
          referenceProof.href = mapping.reference_url;
          referenceProof.target = "_blank";
          referenceProof.rel = "noopener";
        }}
        const checkLabel = document.createElement("label"); checkLabel.className = "repair-check";
        const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.disabled = job.status !== "manual_review_required" || slot.status !== "accepted" || Number(slot.repair_count || 0) >= 2;
        const checkText = document.createElement("span"); checkText.textContent = Number(slot.repair_count || 0) >= 2 ? "已达到返修上限" : "不合格，申请返修";
        checkLabel.append(checkbox, checkText);
        const fields = document.createElement("div"); fields.className = "repair-fields"; fields.hidden = true;
        const select = document.createElement("select"); select.className = "repair-issue"; select.setAttribute("aria-label", `${{slot.slot_id}} 问题类型`);
        const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "请选择问题类型"; select.append(placeholder);
        repairIssueOptions.forEach(([value, label]) => {{ const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option); }});
        const textarea = document.createElement("textarea"); textarea.className = "repair-note"; textarea.maxLength = 500; textarea.placeholder = "补充说明（可选；选择其他时必填）"; textarea.setAttribute("aria-label", `${{slot.slot_id}} 补充说明`);
        const fieldError = document.createElement("div"); fieldError.className = "repair-field-error"; fieldError.hidden = true;
        fields.append(select, textarea, fieldError);
        checkbox.addEventListener("change", () => {{ card.classList.toggle("selected", checkbox.checked); fields.hidden = !checkbox.checked; if (!checkbox.checked) {{ select.value = ""; textarea.value = ""; card.classList.remove("repair-incomplete"); }} updateRepairSubmitState(); }});
        select.addEventListener("change", updateRepairSubmitState); textarea.addEventListener("input", updateRepairSubmitState);
        card.append(cardHead, imageLink, fullResolutionLink, referenceProof, checkLabel, fields);
        if (slot.status === "repair_pending") {{ const feedback = document.createElement("div"); feedback.className = "repair-feedback-summary"; feedback.textContent = `${{slot.review_issue_code || "返修"}}${{slot.review_note ? ` · ${{slot.review_note}}` : ""}}`; card.append(feedback); }}
        grid.append(card);
      }});
      panel.append(grid); return panel;
    }}
    const imageQueueStateLabels = [
      ["manual_review_required", "待人工审核", "ready"],
      ["repair_pending", "返修待生成", "warn"],
      ["pending", "待生成", "warn"],
      ["in_progress", "生成中", "warn"],
      ["stopped", "已停止", "stop"],
      ["completed", "已完成", "ready"],
      ["failed", "失败", "stop"],
      ["waiting_for_supplier_sku", "未锁定 SKU", "stop"],
      ["waiting_for_subject_master", "未确认主体", "stop"],
      ["queue_missing", "未进入队列", "stop"],
    ];
    function renderBatchGenerationOverview(summary) {{
      const container = $("batchGenerationOverview");
      container.replaceChildren();
      const heading = document.createElement("strong");
      heading.textContent = `整批生图状态：共 ${{summary.total_products || 0}} 件`;
      container.append(heading);
      imageQueueStateLabels.forEach(([key, label, tone]) => {{
        const count = Number(summary[key] || 0);
        if (!count) return;
        const chip = document.createElement("span");
        chip.className = `queue-state-chip ${{tone}}`;
        chip.innerHTML = `<span>${{label}}</span><b>${{count}}</b>`;
        container.append(chip);
      }});
    }}
    function productStatePresentation(item) {{
      const job = item.image_job || {{}};
      const status = String(item.generation_status || job.status || "queue_missing");
      if (status === "waiting_for_supplier_sku") return {{ tone:"stop", text:"未进入生图队列：尚未锁定真实 1688 SKU", action:"去选择 SKU" }};
      if (status === "waiting_for_subject_master") return {{ tone:"stop", text:"未进入生图队列：尚未确认真实商品主体证据" }};
      if (status === "queue_missing") return {{ tone:"stop", text:"SKU 与主体已确认，但生图队列记录缺失；请检查入队流程" }};
      if (status === "stopped") return {{ tone:"stop", text:`生图已停止：${{job.stop_reason || "旧任务未记录停止原因"}}；确认后可点右侧“继续生图”` }};
      if (status === "manual_review_required") return {{ tone:"ready", text:"8 张图片已生成；等待人工审核不会阻塞其他商品继续生图" }};
      if (status === "completed") return {{ tone:"ready", text:"本件 8 张图片已由用户确认，可进入上传图片门禁" }};
      if ((job.slots || []).some((slot) => slot.status === "repair_pending")) return {{ tone:"warn", text:"返修已入队；点击右侧“复制继续返修生图命令”，粘贴到 Codex 执行" }};
      if (status === "pending") return {{ tone:"warn", text:"已进入生图队列，等待总控领取" }};
      if (status === "in_progress") return {{ tone:"warn", text:"正在生成图片；其他商品仍按空闲并发位继续调度" }};
      if (status === "failed") return {{ tone:"stop", text:"生图任务失败；请查看任务事件中的失败原因" }};
      return {{ tone:"ready", text:`当前生图状态：${{status}}` }};
    }}
    function renderImageJobControls(item) {{
      const job = item && item.image_job ? item.image_job : null;
      const status = job ? String(job.status || "pending") : String((item && item.generation_status) || "not_queued");
      const repairPending = job ? (job.slots || []).filter((slot) => slot.status === "repair_pending").length : 0;
      $("imageJobStatus").textContent = job ? `${{job.job_id}} · ${{status}}${{status === "stopped" ? ` · ${{job.stop_reason || "旧任务未记录停止原因"}}` : ""}}${{repairPending ? ` · ${{repairPending}} 张等待返修` : ""}}` : status;
      $("stopImageJob").disabled = !job || ["stopped","manual_review_required","completed","failed"].includes(status);
      $("resumeImageJob").disabled = !job || status !== "stopped";
      $("approveImageJob").textContent = status === "completed" ? "本件 8 张已确认" : "确认本件 8 张图片可用";
      copyImageControllerCommand.textContent = repairPending ? "复制继续返修生图命令 (Copy Repair Command)" : "复制整批生图命令 (Copy Production Command)";
      imageControllerCopyStatus.textContent = repairPending ? `当前商品有 ${{repairPending}} 张待返修；复制命令后粘贴到 Codex 执行` : "复制后粘贴到当前 Codex 对话执行";
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
      const presentation = productStatePresentation(item);
      const stateBanner = document.createElement("div"); stateBanner.className = "product-state-banner"; stateBanner.classList.add(presentation.tone); stateBanner.textContent = presentation.text;
      if (presentation.action) {{ const action = document.createElement("a"); action.href = `/batches/${{encodeURIComponent(runId)}}/supplier-review`; action.textContent = presentation.action; stateBanner.append(action); }}
      const sources = document.createElement("div"); sources.className = "source-grid";
      const ozonReferenceInputs = item.ozon_reference_inputs || [];
      sources.append(sourcePanel("Ozon 参考图（按序供 Skill 映射）", ozonReferenceInputs.map((reference) => reference.url), "没有 Ozon 参考图"));
      sources.append(sourcePanel("供应商原图 (Supplier Source)", item.supplier_source_images || [], "等待用户核实并采集 1688 商品"));
      sources.append(generatedReviewPanel(item));
      card.append(head, stateBanner, sources); container.append(card); container.scrollTop = 0;
      renderImageJobControls(item);
    }}
    function render(data, preserveIndex = false) {{
      imageWorkspaceItems = data.items || []; const counts = data.source_counts || {{}}; const gate = data.image_gate || {{}};
      $("productCount").textContent = String(imageWorkspaceItems.length); $("ozonImageCount").textContent = String(counts.ozon_reference_images || 0); $("supplierImageCount").textContent = String(counts.supplier_source_images || 0); $("generatedImageCount").textContent = String(counts.generated_images || 0);
      $("workspaceStatus").textContent = (counts.supplier_source_images || 0) > 0 ? "素材已就绪 (Sources Ready)" : "等待供应商素材 (Waiting for Supplier)";
      $("gateMessage").textContent = gate.message || "没有生成结果，禁止进入上传阶段。";
      renderBatchGenerationOverview(data.image_queue_summary || {{ total_products:imageWorkspaceItems.length }});
      renderImageItemAt(preserveIndex ? imageWorkspaceIndex : 0);
    }}
    function workspaceSignature(data) {{
      return JSON.stringify([data.image_queue_summary || {{}}, (data.items || []).map((item) => [item.seed_id, item.generation_status, (item.image_job || {{}}).status, (item.image_job || {{}}).stop_reason, ((item.image_job || {{}}).slots || []).map((slot) => [slot.slot_id, slot.status, slot.accepted_path, slot.attempt_count, slot.repair_count, slot.review_issue_code, slot.review_requested_at, slot.ozon_reference_mapping || null])])]);
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
        status.textContent = "返修已入队。点击上方“复制继续返修生图命令”，粘贴到 Codex 执行";
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
    $("approveImageJob").addEventListener("click", () => updateImageJob("approve"));
    $("submitImageRepairs").addEventListener("click", submitSelectedRepairs);
    loadWorkspace(true).catch((error) => {{ $("workspaceStatus").textContent = "加载失败 (Failed)"; $("workspaceStatus").className = "pill error"; $("gateMessage").textContent = error.message || String(error); }});
    setInterval(() => loadWorkspace(false).catch(() => undefined), 2000);
  </script>
</body>
</html>"""


def build_upload_workspace_html(run_id: str) -> str:
    safe_run_id = json.dumps(run_id)
    content_controller_command = (
        f"Use $ozon-intelligent-field-drafter for Ozon V2 batch {run_id}. "
        "Read and follow the workspace Skill at "
        "skills/ozon-intelligent-field-drafter/SKILL.md. "
        "Use the complete locked 1688 SKU plus existing Ozon and supplier evidence. "
        "For visual-supported fields, inspect every visual_evidence_refs image: "
        "first run skills/ozon-intelligent-field-drafter/scripts/"
        "materialize_visual_evidence.py, then use view_image on every local_path. "
        "For every visual field, identify the locked SKU primary product subject "
        "and follow visual_target_scope. Exclude accessories, packaging, backgrounds, "
        "text overlays, decorations, and reference variants from primary-subject "
        "facts. Only locked original 1688 images may prove color or directly visible "
        "package/set counts. Submit field-specific visual_analysis and "
        "subject_analysis receipts; "
        "generated images are never product-fact evidence. "
        "Complete every pending field decision, translate customer-facing facts into "
        "Russian, classify genuine evidence gaps, and report ready, gap, and blocked "
        "products separately. "
        "Stop before upload, publish, or final approval."
    )
    escaped_content_controller_command = html.escape(content_controller_command)
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
    .draft-head-actions,.draft-pager {{ display:flex; align-items:center; gap:7px; }} .draft-pager button {{ width:28px; height:28px; display:grid; place-items:center; border:1px solid #cbd4e1; border-radius:5px; color:var(--blue); background:#fff; cursor:pointer; }} .draft-pager button:disabled {{ color:#aab3c1; background:#f4f6f9; cursor:not-allowed; }} .draft-page-label {{ min-width:128px; color:var(--muted); font-size:10px; text-align:center; }}
    .draft-item img {{ width:124px; height:124px; object-fit:contain; border:1px solid var(--line); border-radius:4px; background:#fff; }} .image-placeholder {{ width:124px; height:124px; display:grid; place-items:center; border:1px dashed #c9d1dc; border-radius:4px; color:var(--muted); background:var(--soft); font-size:11px; text-align:center; }}
    .source-label {{ color:var(--muted); font-size:10px; }} .source-title {{ margin:3px 0 9px; font-size:16px; line-height:1.35; }} .notice {{ padding:8px 10px; border-left:3px solid var(--amber); color:#74410a; background:#fff8eb; font-size:10px; }} .inline-action {{ margin-left:8px; padding:3px 7px; border:1px solid #d5a14d; border-radius:4px; color:#74410a; background:#fff; cursor:pointer; }} .inline-action:disabled {{ opacity:.55; cursor:wait; }}
    .item-gate-summary {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-top:10px; padding:8px 9px; border:1px solid #e8c98f; border-radius:4px; color:#74410a; background:#fffaf0; font-size:10px; }} .item-gate-summary.ready {{ border-color:#9dd8c7; color:var(--green); background:var(--green-soft); }} .item-gate-summary strong {{ margin-right:3px; }} .item-gate-chip {{ padding:2px 6px; border-radius:3px; color:inherit; background:rgba(255,255,255,.72); }}
    .meta-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); margin-top:11px; border:1px solid var(--line); border-radius:4px; overflow:hidden; }} .meta {{ padding:8px 9px; border-right:1px solid var(--line); }} .meta:last-child {{ border-right:0; }} .meta span {{ display:block; color:var(--muted); font-size:9px; }} .meta strong {{ display:block; margin-top:3px; overflow-wrap:anywhere; font-size:10px; }}
    .upload-core {{ margin-top:10px; padding:9px; border:1px solid #b9ddcf; border-radius:5px; background:#f2fbf8; }} .upload-core-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }} .upload-core-head strong {{ color:var(--green); font-size:11px; }} .upload-core-head span {{ color:var(--muted); font-size:9px; }} .upload-core-grid {{ display:grid; grid-template-columns:repeat(4,minmax(110px,1fr)); gap:6px; margin-top:8px; }} .upload-core-field {{ padding:7px 8px; border:1px solid #d8ebe4; border-radius:4px; background:#fff; }} .upload-core-field span {{ display:block; color:var(--muted); font-size:9px; }} .upload-core-field strong {{ display:block; margin-top:2px; color:var(--text); font-size:10px; }}
    .mapping-summary {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }} .mapping-chip {{ padding:3px 7px; border-radius:4px; color:var(--green); background:var(--green-soft); font-size:10px; }} .mapping-chip.warn {{ color:var(--red); background:var(--red-soft); }}
    .score-progress {{ margin-top:8px; padding:8px 9px; border-left:3px solid var(--blue); color:#29456f; background:#f1f5ff; font-size:10px; }}
    .required-field-skill-pending {{ margin-top:10px; padding:9px 10px; border-left:3px solid var(--blue); color:#29456f; background:#f1f5ff; font-size:10px; }} .required-field-editor {{ margin-top:10px; padding:10px; border:1px solid #efbd68; border-radius:5px; background:#fffaf0; }} .required-field-editor h4 {{ margin:0; color:#74410a; font-size:12px; }} .required-field-editor p {{ margin:3px 0 9px; color:var(--muted); font-size:10px; }} .required-field-grid {{ display:grid; grid-template-columns:repeat(2,minmax(180px,1fr)); gap:8px; }} .required-field-input {{ display:grid; gap:4px; }} .required-field-input label {{ color:#74410a; font-size:10px; font-weight:650; }} .required-field-input input,.required-field-input select {{ width:100%; height:34px; padding:0 8px; border:1px solid #d7b574; border-radius:4px; color:var(--text); background:#fff; }} .required-field-actions {{ display:flex; align-items:center; gap:9px; margin-top:9px; }} .required-field-actions button {{ min-height:34px; padding:0 13px; border:1px solid var(--blue); border-radius:4px; color:#fff; background:var(--blue); cursor:pointer; }} .required-field-actions button:disabled {{ opacity:.5; cursor:wait; }} .required-field-status {{ color:var(--muted); font-size:10px; }} .required-field-status.error {{ color:var(--red); }} .required-field-status.success {{ color:var(--green); }}
    .attribute-grid {{ display:grid; grid-template-columns:1fr 1fr; margin-top:8px; border:1px solid var(--line); border-radius:4px; overflow:hidden; }} .attribute {{ display:grid; grid-template-columns:minmax(110px,.8fr) minmax(0,1.2fr); gap:10px; padding:8px 9px; border-right:1px solid var(--line); border-bottom:1px solid #edf0f4; font-size:10px; }} .attribute:nth-child(2n) {{ border-right:0; }} .attribute span {{ color:var(--muted); }} .attribute strong {{ overflow-wrap:anywhere; }} .attribute.missing_fact strong {{ color:var(--red); }} .attribute.optional-evidence-gap strong {{ color:var(--muted); font-weight:500; }} .attribute.rewrite_required strong {{ color:var(--amber); }} .attribute.not_applicable strong {{ color:var(--muted); font-weight:500; }} .all-mappings {{ margin-top:8px; color:var(--muted); font-size:10px; }} .all-mappings summary {{ cursor:pointer; color:var(--blue); font-weight:650; }}
    .gate-list {{ padding:6px 14px 12px; }} .gate-row {{ display:grid; grid-template-columns:22px minmax(0,1fr); gap:9px; padding:10px 0; border-bottom:1px solid #edf0f4; }} .gate-row:last-child {{ border-bottom:0; }} .gate-icon {{ width:22px; height:22px; display:grid; place-items:center; border-radius:50%; color:var(--green); background:var(--green-soft); font-size:11px; font-weight:700; }} .gate-row.blocked .gate-icon {{ color:var(--amber); background:var(--amber-soft); }} .gate-row strong {{ display:block; font-size:11px; }} .gate-row span {{ display:block; margin-top:2px; color:var(--muted); font-size:10px; }}
    .content-controller {{ margin:12px 14px; padding:11px; border:1px solid #bfd0f8; border-radius:5px; background:var(--blue-soft); }} .content-controller strong {{ display:block; font-size:11px; }} .content-controller p {{ margin:4px 0 8px; color:var(--muted); font-size:10px; }} .content-command {{ max-height:190px; overflow:auto; padding:9px; border:1px solid #c9d5ec; border-radius:4px; white-space:pre-wrap; color:#263756; background:#fff; font:10px/1.5 Consolas,"Courier New",monospace; }} .content-copy {{ width:100%; min-height:34px; margin-top:8px; border:1px solid var(--blue); border-radius:4px; color:#fff; background:var(--blue); cursor:pointer; }} .content-copy-status {{ display:block; min-height:16px; margin-top:4px; color:var(--green); font-size:10px; text-align:center; }}
    .pricing-workspace {{ margin-bottom:14px; }} .pricing-head-note {{ color:var(--muted); font-size:11px; }}
    .pricing-layout {{ min-height:470px; display:grid; grid-template-columns:300px minmax(0,1fr); }} .pricing-product-column {{ height:470px; align-self:start; display:grid; grid-template-rows:426px 44px; border-right:1px solid var(--line); background:#f8fafc; }} .pricing-products {{ min-height:0; padding:10px; overflow:hidden; }}
    .pricing-product {{ width:100%; display:grid; grid-template-columns:54px minmax(0,1fr); gap:9px; margin-bottom:8px; padding:9px; border:1px solid var(--line); border-radius:6px; color:var(--text); background:#fff; cursor:pointer; text-align:left; }} .pricing-product:hover {{ border-color:#9eb5ee; }} .pricing-product.active {{ border-color:var(--blue); box-shadow:0 0 0 2px rgba(36,87,214,.10); }} .pricing-product img {{ width:54px; height:54px; object-fit:contain; border:1px solid var(--line); border-radius:4px; background:#fff; }} .pricing-product strong {{ display:block; max-height:38px; overflow:hidden; font-size:11px; line-height:1.35; }} .pricing-product span {{ display:block; margin-top:5px; color:var(--muted); font-size:10px; }} .pricing-product .confirmed {{ color:var(--green); }}
    .pricing-pager {{ display:flex; align-items:center; justify-content:center; gap:8px; padding:6px 10px; border-top:1px solid var(--line); background:#fff; }} .pricing-pager button {{ width:28px; height:28px; display:grid; place-items:center; border:1px solid #cbd4e1; border-radius:5px; color:var(--blue); background:#fff; cursor:pointer; }} .pricing-pager button:disabled {{ color:#aab3c1; background:#f4f6f9; cursor:not-allowed; }} .pricing-page-label {{ min-width:104px; color:var(--muted); font-size:10px; text-align:center; }}
    .pricing-editor {{ min-width:0; padding:16px; }} .pricing-editor-empty {{ min-height:430px; display:grid; place-items:center; color:var(--muted); }} .pricing-reference {{ display:grid; grid-template-columns:86px minmax(0,1fr); gap:13px; padding-bottom:14px; border-bottom:1px solid var(--line); }} .pricing-reference img {{ width:86px; height:86px; object-fit:contain; border:1px solid var(--line); border-radius:5px; background:#fff; }} .pricing-reference h4 {{ margin:0 0 7px; font-size:15px; }} .pricing-reference-row {{ display:flex; flex-wrap:wrap; gap:7px 14px; color:var(--muted); font-size:11px; }} .supplier-link {{ color:var(--blue); font-weight:650; text-decoration:none; }} .supplier-link:hover {{ text-decoration:underline; }}
    .pricing-policy {{ display:flex; flex-wrap:wrap; gap:6px; margin:12px 0; }} .policy-chip {{ padding:4px 8px; border-radius:4px; color:#38506f; background:#edf3fb; font-size:10px; }}
    .pricing-form {{ display:grid; grid-template-columns:repeat(4,minmax(130px,1fr)); gap:10px; }} .pricing-field {{ align-content:start; display:grid; gap:5px; }} .pricing-field label {{ color:var(--muted); font-size:10px; }} .pricing-field input {{ width:100%; height:36px; padding:0 9px; border:1px solid #cbd4e1; border-radius:5px; color:var(--text); background:#fff; }} .pricing-field input:focus {{ outline:2px solid rgba(36,87,214,.16); border-color:var(--blue); }} .pricing-prefill-note {{ color:var(--green); font-size:9px; line-height:1.35; }}
    .pricing-actions {{ display:flex; align-items:center; gap:9px; margin-top:14px; }} .pricing-action {{ min-height:38px; padding:0 16px; border:1px solid var(--blue); border-radius:5px; color:var(--blue); background:#fff; cursor:pointer; }} .pricing-action.primary-action {{ color:#fff; background:var(--blue); }} .pricing-action.danger-action {{ margin-left:auto; border-color:var(--red); color:var(--red); background:#fff; }} .pricing-action:disabled {{ opacity:.5; cursor:not-allowed; }} .pricing-message {{ min-height:20px; margin-top:8px; color:var(--muted); font-size:11px; }} .pricing-message.error {{ color:var(--red); }} .pricing-message.success {{ color:var(--green); }}
    .pricing-result {{ display:grid; grid-template-columns:repeat(6,minmax(105px,1fr)); margin-top:14px; overflow:hidden; border:1px solid var(--line); border-radius:6px; }} .pricing-result-cell {{ min-height:70px; padding:10px; border-right:1px solid var(--line); background:#fbfcfe; }} .pricing-result-cell:last-child {{ border-right:0; }} .pricing-result-cell span {{ display:block; color:var(--muted); font-size:9px; }} .pricing-result-cell strong {{ display:block; margin-top:5px; font-size:14px; }} .pricing-result-cell.price {{ background:var(--green-soft); }} .pricing-result-cell.price strong {{ color:var(--green); }}
    .publish-lock {{ margin:0 14px 12px; padding:11px 12px; border-left:3px solid var(--amber); color:#74410a; background:#fff8eb; }} .publish-lock strong {{ display:block; font-size:11px; }} .publish-lock span {{ display:block; margin-top:3px; font-size:10px; }}
    .primary {{ width:calc(100% - 28px); height:38px; margin:0 14px 14px; border:1px solid var(--blue); border-radius:5px; color:#fff; background:var(--blue); }} .primary:disabled {{ opacity:.48; cursor:not-allowed; }} .empty {{ padding:26px; color:var(--muted); text-align:center; }}
    .product-upload-actions {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:10px; padding:10px; border:1px solid #bfd0f8; border-radius:5px; background:#f5f8ff; }} .product-upload-actions button {{ min-height:34px; padding:0 12px; border:1px solid var(--blue); border-radius:4px; color:var(--blue); background:#fff; cursor:pointer; }} .product-upload-actions button.confirm {{ color:#fff; background:var(--blue); }} .product-upload-actions button:disabled {{ opacity:.48; cursor:not-allowed; }} .product-upload-status {{ flex:1 1 260px; color:var(--muted); font-size:10px; }} .product-upload-status.error {{ color:var(--red); }} .product-upload-status.success {{ color:var(--green); }} .product-upload-status.non-blocking-warning {{ color:var(--amber); }}
    .batch-upload-controller {{ margin:0 14px 12px; padding:11px; border:1px solid #bfd0f8; border-radius:5px; background:#f5f8ff; }} .batch-upload-controller strong {{ display:block; font-size:11px; }} .batch-upload-controller p {{ margin:4px 0 8px; color:var(--muted); font-size:10px; }} .batch-upload-controller button {{ width:100%; min-height:38px; border:1px solid var(--blue); border-radius:4px; color:#fff; background:var(--blue); cursor:pointer; }} .batch-upload-controller button:disabled {{ opacity:.5; cursor:wait; }} .batch-upload-status {{ display:block; min-height:17px; margin-top:6px; color:var(--muted); font-size:10px; }} .batch-upload-status.error {{ color:var(--red); }} .batch-upload-status.success {{ color:var(--green); }}
    @media(max-width:1100px) {{ .pricing-form {{ grid-template-columns:repeat(2,minmax(130px,1fr)); }} .pricing-result {{ grid-template-columns:repeat(3,1fr); }} .pricing-result-cell:nth-child(3) {{ border-right:0; }} }}
    @media(max-width:1000px) {{ .upload-layout {{ grid-template-columns:1fr; }} .pricing-layout {{ grid-template-columns:250px minmax(0,1fr); }} }}
    @media(max-width:900px) {{ .app-shell {{ grid-template-columns:72px minmax(0,1fr); }} .sidebar {{ padding:14px 8px; }} .sidebar-brand {{ padding:4px 4px 16px; text-align:center; }} .sidebar-brand strong {{ font-size:14px; }} .sidebar-brand span,.sidebar-label,.sidebar-link span:last-child {{ display:none; }} .sidebar-link {{ justify-content:center; padding:0; }} .stage-nav-inner {{ overflow-x:auto; padding:0 10px; }} .stage-run {{ display:none; }} .summary {{ grid-template-columns:repeat(2,1fr); }} .metric:nth-child(2) {{ border-right:0; }} .metric:nth-child(-n+2) {{ border-bottom:1px solid var(--line); }} }}
    @media(max-width:620px) {{ .app-shell {{ display:block; }} .sidebar {{ position:static; width:100%; height:auto; display:flex; align-items:center; gap:6px; overflow-x:auto; }} .sidebar-brand {{ min-width:92px; padding:0 8px; border:0; }} .sidebar-group {{ display:flex; gap:4px; margin:0; }} .sidebar-link {{ width:38px; flex:0 0 38px; }} .workspace {{ padding:10px; }} .page-head {{ align-items:flex-start; flex-direction:column; }} .pricing-layout {{ display:block; }} .pricing-product-column {{ height:138px; grid-template-rows:94px 44px; border-right:0; border-bottom:1px solid var(--line); }} .pricing-products {{ display:flex; gap:8px; overflow:hidden; }} .pricing-product {{ min-width:230px; margin-bottom:0; }} .pricing-form,.required-field-grid {{ grid-template-columns:1fr; }} .pricing-result {{ grid-template-columns:repeat(2,1fr); }} .pricing-result-cell:nth-child(2n) {{ border-right:0; }} .draft-item {{ grid-template-columns:1fr; }} .draft-item img,.image-placeholder {{ width:100%; height:180px; }} .attribute-grid {{ grid-template-columns:1fr; }} .attribute {{ border-right:0; }} }}
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar" aria-label="主菜单 (Primary Navigation)"><div class="sidebar-brand"><strong>Ozon V2</strong><span>运营驾驶舱 (Operations Cockpit)</span></div><div class="sidebar-group"><div class="sidebar-label">运营 (Operations)</div>
      <a class="sidebar-link" href="/?run_id={run_id}"><span class="nav-mark">B</span><span>批次总览 (Batch)</span></a><a class="sidebar-link" href="/batches/{run_id}/supplier-review"><span class="nav-mark">S</span><span>供应商审核 (Supplier)</span></a><a class="sidebar-link active" href="/batches/{run_id}/upload" aria-current="page"><span class="nav-mark">U</span><span>上传草稿 (Upload)</span></a>
    </div><div class="sidebar-group"><div class="sidebar-label">全局 (Global)</div><a class="sidebar-link" href="/batches"><span class="nav-mark">H</span><span>批次历史 (History)</span></a><a class="sidebar-link" href="/products"><span class="nav-mark">P</span><span>商品资料库 (Products)</span></a><a class="sidebar-link" href="/store"><span class="nav-mark">K</span><span>店铺授权 (Store)</span></a><a class="sidebar-link" href="/diagnostics"><span class="nav-mark">D</span><span>诊断中心 (Diagnostics)</span></a><a class="sidebar-link" href="/settings"><span class="nav-mark">C</span><span>系统设置 (Settings)</span></a></div></aside>
    <div class="app-main"><header class="topbar"><div><h1>Ozon V2 工具台 (Workbench)</h1><p>完整类目模板、Ozon 证据映射、90 分内容完善、单张原图建品与发布锁</p></div><span class="pill">已连接 (Connected)</span></header>
      <nav id="stageNavigation" class="stage-nav" aria-label="批次阶段 (Batch Stages)"><div class="stage-nav-inner"><a id="batchOverviewNav" class="stage-link" href="/?run_id={run_id}">批次总览 (Batch)</a><a id="supplierReviewNav" class="stage-link" href="/batches/{run_id}/supplier-review">供应商审核 (Supplier Review)</a><a id="uploadDraftNav" class="stage-link active" aria-current="page" href="/batches/{run_id}/upload">上传草稿 (Upload)</a><span class="stage-run">批次 {run_id}</span></div></nav>
      <main class="workspace"><div class="page-head"><div><h2>上传草稿 (Upload)</h2><p>完整读取 Seller API 类目字段，使用 Ozon 与已确认 1688 事实准备客观属性，并明确列出待原创内容和真实缺口；合格商品不等待整批。</p></div><span id="workspaceStatus" class="pill">加载中 (Loading)</span></div>
        <section class="summary"><div class="metric"><span>商品 (Products)</span><strong id="productCount">0</strong></div><div class="metric"><span>必填已映射 (Required)</span><strong id="requiredCount">0 / 0</strong></div><div class="metric"><span>模板已填 (Template Filled)</span><strong id="prefillCount">0</strong></div><div class="metric"><span>锁定原图 (Original)</span><strong id="imageReadyCount">0 / 0</strong></div></section>
        <section id="pricingWorkspace" class="panel pricing-workspace">
          <div class="panel-head"><div><h3>价格与包装证据</h3><span class="pricing-head-note">逐件确认成本、包装尺寸、GUOO 跨境运费和 Ozon 建议上架价；确认后的包装数据会作为后续字段证据。</span></div><span id="pricingProgress" class="pill">0 / 0 已确认</span></div>
          <div class="pricing-layout"><div class="pricing-product-column"><nav id="pricingProductList" class="pricing-products" aria-label="价格证据商品清单"></nav><div id="pricingPager" class="pricing-pager"><button id="pricingPrevPage" type="button" aria-label="上一页">‹</button><span id="pricingPageLabel" class="pricing-page-label" aria-live="polite">第 1 / 1 页 · 每页 5 件</span><button id="pricingNextPage" type="button" aria-label="下一页">›</button></div></div><section id="pricingEditor" class="pricing-editor"><div class="pricing-editor-empty">正在读取商品价格证据…</div></section></div>
        </section>
        <div class="upload-layout"><section class="panel"><div class="panel-head"><h3>类目模板自动映射结果 · 完整字段与内容完善</h3><div class="draft-head-actions"><div id="draftPager" class="draft-pager"><button id="draftPrevPage" type="button" aria-label="上一页">‹</button><span id="draftPageLabel" class="draft-page-label" aria-live="polite">第 1 / 1 页 · 每页 5 件</span><button id="draftNextPage" type="button" aria-label="下一页">›</button></div><span class="pill">Seller API Template</span></div></div><div id="draftItems" class="draft-items"></div></section>
          <aside id="uploadGate" class="panel"><div class="panel-head"><h3>逐商品上传门禁 · 逐商品真实上传 (Per-product Upload)</h3></div><div class="content-controller"><strong>智能字段草稿总控（证据约束）</strong><p>Ozon 数据用于模仿结构与写法，1688 与已锁定 SKU 用于约束客观事实；无法证明的字段明确保留为未解决，不会自动上传。</p><div id="contentControllerCommand" class="content-command">{escaped_content_controller_command}</div><button id="copyContentControllerCommand" class="content-copy" type="button">复制整批智能字段草稿命令</button><span id="contentControllerCopyStatus" class="content-copy-status"></span></div><div class="gate-list"><div id="templateGate" class="gate-row"><span class="gate-icon">1</span><div><strong>类目模板 (Category Template)</strong><span>等待检查</span></div></div><div id="attributeGate" class="gate-row blocked"><span class="gate-icon">!</span><div><strong>必填属性 (Required Attributes)</strong><span>等待自动映射。</span></div></div><div id="originalContentGate" class="gate-row blocked"><span class="gate-icon">!</span><div><strong>原创内容 (Original Content)</strong><span>等待利用 Ozon 证据完成俄文内容。</span></div></div><div id="bootstrapImageGate" class="gate-row blocked"><span class="gate-icon">!</span><div><strong>锁定原图 (Locked Original)</strong><span>等待锁定一张真实 1688 主体原图。</span></div></div><div id="pricingGate" class="gate-row blocked"><span class="gate-icon">!</span><div><strong>价格与包装证据 (Pricing)</strong><span>等待逐件确认成本、包装和建议上架价。</span></div></div><div id="draftGate" class="gate-row blocked"><span class="gate-icon">!</span><div><strong>可独立推进 (Ready Products)</strong><span>逐件计算，不再等待整批。</span></div></div></div><div class="batch-upload-controller"><strong>批量独立校验与上传</strong><p>最多同时处理 4 件；合格商品直接上传，缺字段或模板异常的商品单独保留，不拖住其他商品。</p><button id="batchUploadProducts" type="button">批量校验并上传可用商品</button><span id="batchUploadStatus" class="batch-upload-status"></span></div><div class="publish-lock"><strong>发布锁已开启 (Publish Lock Active) · 逐商品确认</strong><span>建品时只提交锁定的 1688 原图；只有 Ozon 确认上传成功后才输出图片生成与上传任务包，失败或未上传商品绝不派发生图。</span></div></aside>
        </div>
      </main>
    </div>
  </div>
  <script>
    const runId = {safe_run_id}; const $ = (id) => document.getElementById(id);
    async function api(path, options={{}}) {{ const response = await fetch(path, options); const body = await response.json(); if (!response.ok) throw body; return body; }}
    const pricingState = {{ selectedSeedId:null, items:[], drafts:new Map(), previews:new Map(), page:0, pageSize:5 }};
    const draftState = {{ page:0, pageSize:5, items:[] }};
    const productUploadState = {{ previews:new Map(), submissions:new Map() }};
    let latestUploadData = null;
    const pricingEndpoints = {{
      preview:`/api/batches/${{encodeURIComponent(runId)}}/pricing-evidence/preview`,
      confirm:`/api/batches/${{encodeURIComponent(runId)}}/pricing-evidence/confirm`
    }};
    const pricingFieldDefinitions = [
      ["purchase_price_cny","采购价（人工确认）","元"],
      ["domestic_shipping_cny","国内运费","元"],
      ["package_weight_g","包装后重量","克"],
      ["package_length_cm","包装长度","厘米"],
      ["package_width_cm","包装宽度","厘米"],
      ["package_height_cm","包装高度","厘米"],
      ["target_margin_rate","目标净利润率（20%=0.20）","小数"]
    ];
    function pricingDraftFor(item) {{
      if (pricingState.drafts.has(item.seed_id)) return pricingState.drafts.get(item.seed_id);
      const prefilled = item.pricing_prefill && item.pricing_prefill.values ? item.pricing_prefill.values : {{}};
      const saved = item.pricing_evidence && item.pricing_evidence.inputs ? item.pricing_evidence.inputs : {{}};
      const draft = {{ target_margin_rate:"0.20", ...prefilled, ...saved }};
      pricingState.drafts.set(item.seed_id, draft);
      if (item.pricing_evidence && item.pricing_evidence.calculation) pricingState.previews.set(item.seed_id, item.pricing_evidence);
      return draft;
    }}
    function saveVisiblePricingDraft() {{
      const seedId = pricingState.selectedSeedId;
      if (!seedId) return;
      const draft = {{ ...(pricingState.drafts.get(seedId) || {{}}) }};
      pricingFieldDefinitions.forEach(([key]) => {{ const input = document.querySelector(`[data-pricing-field="${{key}}"]`); if (input) draft[key] = input.value.trim(); }});
      pricingState.drafts.set(seedId, draft);
    }}
    function pricingStatusLabel(item) {{
      if (item.pricing_status === "confirmed") return "已确认";
      if (item.pricing_status === "invalid") return "包装密度或数据无效";
      if (item.pricing_status === "stale") return "参数已变，需重算";
      return "待填写";
    }}
    function renderPricingProductList() {{
      const list = $("pricingProductList"); list.replaceChildren();
      const pageCount = Math.max(1, Math.ceil(pricingState.items.length / pricingState.pageSize));
      pricingState.page = Math.min(Math.max(0, pricingState.page), pageCount - 1);
      const pageStart = pricingState.page * pricingState.pageSize;
      const pageItems = pricingState.items.slice(pageStart, pageStart + pricingState.pageSize);
      pageItems.forEach((item, pageIndex) => {{
        const index = pageStart + pageIndex;
        const button = document.createElement("button"); button.type = "button"; button.className = `pricing-product${{item.seed_id === pricingState.selectedSeedId ? " active" : ""}}`;
        const image = document.createElement("img"); image.src = item.generated_image_url || item.source_image || ""; image.alt = item.source_title || `商品 ${{index + 1}}`; image.referrerPolicy = "no-referrer";
        const body = document.createElement("span"); const title = document.createElement("strong"); title.textContent = `${{index + 1}}. ${{item.source_title || item.seed_id}}`;
        const status = document.createElement("span"); status.className = item.pricing_ready ? "confirmed" : ""; status.textContent = pricingStatusLabel(item);
        body.append(title,status); button.append(image,body);
        button.addEventListener("click", () => {{ saveVisiblePricingDraft(); pricingState.selectedSeedId = item.seed_id; renderPricingProductList(); renderPricingEditor(); }});
        list.append(button);
      }});
      $("pricingPageLabel").textContent = `第 ${{pricingState.page + 1}} / ${{pageCount}} 页 · 每页 5 件`;
      $("pricingPrevPage").disabled = pricingState.page === 0;
      $("pricingNextPage").disabled = pricingState.page >= pageCount - 1;
    }}
    function changePricingPage(offset) {{
      saveVisiblePricingDraft();
      const pageCount = Math.max(1, Math.ceil(pricingState.items.length / pricingState.pageSize));
      const nextPage = Math.min(Math.max(0, pricingState.page + offset), pageCount - 1);
      if (nextPage === pricingState.page) return;
      pricingState.page = nextPage;
      const firstItem = pricingState.items[nextPage * pricingState.pageSize];
      pricingState.selectedSeedId = firstItem ? firstItem.seed_id : null;
      renderPricingProductList(); renderPricingEditor();
    }}
    function pricingResultView(record) {{
      const calculation = record && record.calculation; if (!calculation) return null;
      const result = document.createElement("div"); result.className = "pricing-result";
      [
        ["GUOO 跨境运费",`${{calculation.cross_border_freight_cny}} 元`],
        ["总成本",`${{calculation.total_cost_cny}} 元`],
        ["未取整售价",`${{Number(calculation.raw_listing_price_cny).toFixed(2)}} 元`],
        ["建议上架价",`${{calculation.listing_price_cny}} 元`,"price"],
        ["Ozon 上架价",`${{calculation.listing_price_rub}} ₽`,"price"],
        ["划线原价",`${{calculation.old_price_rub}} ₽`]
      ].forEach(([label,value,tone]) => {{ const cell = document.createElement("div"); cell.className = `pricing-result-cell${{tone ? ` ${{tone}}` : ""}}`; const name = document.createElement("span"); name.textContent = label; const amount = document.createElement("strong"); amount.textContent = value; cell.append(name,amount); result.append(cell); }});
      return result;
    }}
    function renderPricingEditor() {{
      const editor = $("pricingEditor"); editor.replaceChildren();
      const item = pricingState.items.find((entry) => entry.seed_id === pricingState.selectedSeedId);
      if (!item) {{ const empty = document.createElement("div"); empty.className = "pricing-editor-empty"; empty.textContent = "当前没有可填写的商品"; editor.append(empty); return; }}
      const draft = pricingDraftFor(item);
      const reference = document.createElement("div"); reference.className = "pricing-reference";
      const image = document.createElement("img"); image.src = item.generated_image_url || item.source_image || ""; image.alt = item.source_title || item.seed_id; image.referrerPolicy = "no-referrer";
      const details = document.createElement("div"); const title = document.createElement("h4"); title.textContent = item.source_title || item.seed_id;
      const rows = document.createElement("div"); rows.className = "pricing-reference-row";
      const sku = document.createElement("span"); const selected = item.supplier_selected_sku || {{}}; sku.textContent = `已锁定 1688 SKU：${{selected.raw_label || selected.combination_key || selected.supplier_sku_id || "未找到"}}`;
      const referencePrice = document.createElement("span"); const price = item.supplier_reference_price || {{}}; referencePrice.textContent = `1688 参考价：${{price.amount || price.visible_text || "-"}} ${{price.currency || "CNY"}}（仅参考）`;
      const link = document.createElement("a"); link.className = "supplier-link"; link.target = "_blank"; link.rel = "noopener noreferrer"; link.href = item.supplier_url || "#"; link.textContent = "打开 1688 商品页"; if (!item.supplier_url) {{ link.setAttribute("aria-disabled","true"); link.addEventListener("click",(event) => event.preventDefault()); }}
      rows.append(sku,referencePrice,link); details.append(title,rows); reference.append(image,details); editor.append(reference);
      const policy = item.pricing_policy || {{}}; const policyRow = document.createElement("div"); policyRow.className = "pricing-policy";
      [`佣金 ${{Number(policy.commission_rate || 0) * 100}}%`,`包装/标签费 ${{policy.packaging_fee_cny || "-"}} 元`,`汇率 1 CNY = ${{policy.rub_per_cny || "-"}} RUB`,`GUOO 陆空标快 ${{policy.freight_rule_version || ""}}`,`售价向上取 .90`].forEach((text) => {{ const chip = document.createElement("span"); chip.className = "policy-chip"; chip.textContent = text; policyRow.append(chip); }}); editor.append(policyRow);
      const form = document.createElement("div"); form.className = "pricing-form";
      const savedInputs = item.pricing_evidence && item.pricing_evidence.inputs ? item.pricing_evidence.inputs : {{}};
      const prefillFields = item.pricing_prefill && item.pricing_prefill.fields ? item.pricing_prefill.fields : {{}};
      pricingFieldDefinitions.forEach(([key,label,unit]) => {{ const field = document.createElement("div"); field.className = "pricing-field"; const name = document.createElement("label"); name.htmlFor = `pricing-${{key}}`; name.textContent = `${{label}}（${{unit}}）`; const input = document.createElement("input"); input.id = `pricing-${{key}}`; input.type = "number"; input.step = "any"; input.min = key === "domestic_shipping_cny" || key === "target_margin_rate" ? "0" : "0.000001"; input.value = draft[key] || ""; input.dataset.pricingField = key; input.addEventListener("input", () => {{ pricingState.previews.delete(item.seed_id); const message = $("pricingMessage"); if (message) {{ message.className = "pricing-message"; message.textContent = "输入已保留，点击“计算建议价格”查看新结果。"; }} }}); field.append(name,input); const prefill = prefillFields[key]; if (prefill && !Object.prototype.hasOwnProperty.call(savedInputs,key)) {{ const note = document.createElement("span"); note.className = "pricing-prefill-note"; note.textContent = `该项已按${{prefill.label || "现有证据"}}预填，仍需用户确认。`; field.append(note); }} form.append(field); }}); editor.append(form);
      const actions = document.createElement("div"); actions.className = "pricing-actions"; const previewButton = document.createElement("button"); previewButton.type = "button"; previewButton.className = "pricing-action"; previewButton.textContent = "计算建议价格"; const confirmButton = document.createElement("button"); confirmButton.type = "button"; confirmButton.className = "pricing-action primary-action"; confirmButton.textContent = "确认并写入本件价格与包装证据"; const excludeButton = document.createElement("button"); excludeButton.type = "button"; excludeButton.className = "pricing-action danger-action"; excludeButton.textContent = "剔除商品（不补位）"; excludeButton.title = "从本批次剔除且永久加入黑名单，后续选品不再出现"; const submissionStatus = String(((item.upload_submission || {{}}).status || "")).toLowerCase(); excludeButton.disabled = submissionStatus && !["failed","error","declined"].includes(submissionStatus); actions.append(previewButton,confirmButton,excludeButton); editor.append(actions);
      const preview = pricingState.previews.get(item.seed_id) || item.pricing_evidence;
      const validationErrors = item.pricing_validation_errors || [];
      const message = document.createElement("div"); message.id = "pricingMessage"; message.className = `pricing-message${{item.pricing_ready || (preview && item.pricing_status !== "invalid") ? " success" : (item.pricing_status === "invalid" ? " error" : "")}}`; message.textContent = item.pricing_ready ? "本件价格与包装证据已确认，可独立进入后续门禁。" : item.pricing_status === "invalid" ? `包装重量或尺寸不符合 Ozon 密度范围：${{validationErrors.join("；") || "请核对含包装重量和长宽高"}}。修改后重新计算并确认。` : item.pricing_status === "stale" ? "固定参数已变化，请重新计算并确认本件。" : preview ? "建议价格已计算；确认无误后写入本件价格与包装证据。" : "已按真实证据预填可确认项；请补齐剩余空白，再计算并确认写入。"; editor.append(message);
      const resultView = pricingResultView(preview); if (resultView) editor.append(resultView);
      const submit = async (mode, button) => {{ saveVisiblePricingDraft(); const payload = {{ seed_id:item.seed_id, ...pricingState.drafts.get(item.seed_id) }}; previewButton.disabled = true; confirmButton.disabled = true; message.className = "pricing-message"; message.textContent = mode === "preview" ? "正在计算 GUOO 运费与建议上架价…" : "正在确认本件价格与包装证据…"; try {{ const response = await api(pricingEndpoints[mode], {{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(payload)}}); pricingState.previews.set(item.seed_id,response.data); if (mode === "confirm") {{ await loadUploadWorkspace(); }} else {{ renderPricingEditor(); }} }} catch (error) {{ message.className = "pricing-message error"; message.textContent = (error.errors && error.errors[0]) || error.message || "价格计算失败"; previewButton.disabled = false; confirmButton.disabled = false; }} }};
      const excludeProduct = async () => {{
        const productName = item.source_title || item.seed_id;
        if (!window.confirm(`确认剔除“${{productName}}”？\n\n本操作不会补抽新商品；当前批次将继续处理剩余商品。该种子和 Ozon 商品会永久加入黑名单，后续选品不再出现。`)) return;
        previewButton.disabled = true; confirmButton.disabled = true; excludeButton.disabled = true; message.className = "pricing-message"; message.textContent = "正在剔除商品并写入永久黑名单…";
        try {{
          await api(`/api/batches/${{encodeURIComponent(runId)}}/products/${{encodeURIComponent(item.seed_id)}}/exclude`, {{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{confirmed:true,reason:"用户判定商品重量、体积或成本不适合跨境销售"}})}});
          pricingState.drafts.delete(item.seed_id); pricingState.previews.delete(item.seed_id); await loadUploadWorkspace();
          $("workspaceStatus").textContent = "已剔除 1 件商品；未补位，剩余商品继续处理";
        }} catch (error) {{ message.className = "pricing-message error"; message.textContent = (error.errors && error.errors[0]) || error.message || "剔除失败"; previewButton.disabled = false; confirmButton.disabled = false; excludeButton.disabled = false; }}
      }};
      previewButton.addEventListener("click", () => submit("preview",previewButton)); confirmButton.addEventListener("click", () => submit("confirm",confirmButton)); excludeButton.addEventListener("click", excludeProduct);
    }}
    function renderPricingWorkspace(data) {{
      saveVisiblePricingDraft(); pricingState.items = data.items || [];
      if (!pricingState.items.some((item) => item.seed_id === pricingState.selectedSeedId)) pricingState.selectedSeedId = pricingState.items.length ? pricingState.items[0].seed_id : null;
      const selectedIndex = pricingState.items.findIndex((item) => item.seed_id === pricingState.selectedSeedId);
      if (selectedIndex >= 0) pricingState.page = Math.floor(selectedIndex / pricingState.pageSize);
      const confirmed = (data.gates || {{}}).pricing_ready_count || 0; $("pricingProgress").textContent = `${{confirmed}} / ${{pricingState.items.length}} 已确认`;
      renderPricingProductList(); renderPricingEditor();
    }}
    function renderUploadCoreFields(container, item) {{
      const fields = item.upload_core_fields;
      if (!fields) return;
      const section = document.createElement("section"); section.className = "upload-core";
      const head = document.createElement("div"); head.className = "upload-core-head";
      const title = document.createElement("strong"); title.textContent = "上传基础字段（价格与包装物流）";
      const note = document.createElement("span"); note.textContent = "已由用户确认；构建草稿时自动写入。包装字段不会冒充商品净尺寸字段";
      head.append(title,note); section.append(head);
      const grid = document.createElement("div"); grid.className = "upload-core-grid";
      [
        ["Ozon 售价", `${{fields.price}} ${{fields.currency_code}}`],
        ["划线原价", `${{fields.old_price}} ${{fields.currency_code}}`],
        ["包装尺寸", `${{fields.depth}} × ${{fields.width}} × ${{fields.height}} ${{fields.dimension_unit}}`],
        ["含包装重量", `${{fields.weight}} ${{fields.weight_unit}}`]
      ].forEach(([label,value]) => {{ const cell = document.createElement("div"); cell.className = "upload-core-field"; const name = document.createElement("span"); name.textContent = label; const fact = document.createElement("strong"); fact.textContent = value; cell.append(name,fact); grid.append(cell); }});
      section.append(grid); container.append(section);
    }}
    function mappingGrid(fields) {{ const statusText = {{ rewrite_required:"待原创（依据采集事实，不复制 Ozon 原文）", missing_fact:"缺少事实，需补充", not_applicable:"未提供可选素材", excluded:"不参与本阶段" }}; const grid = document.createElement("div"); grid.className = "attribute-grid"; (fields || []).forEach((field) => {{ const row = document.createElement("div"); const optionalGap = field.status === "missing_fact" && !field.required; row.className = `attribute ${{optionalGap ? "optional-evidence-gap" : (field.status || "missing_fact")}}`; const label = document.createElement("span"); label.textContent = `${{field.label || field.field_key}}${{field.required ? " *" : ""}}`; const fact = document.createElement("strong"); const pendingVisual = field.status === "missing_fact" && field.visual_inference_supported; fact.textContent = field.status === "mapped" ? String(field.value) : (optionalGap ? `可选属性证据不足（不阻止上传）：${{field.reason || "来源未提供"}}` : (pendingVisual ? "可从锁定 1688 原图判定，等待智能字段 Skill 重判" : (field.intelligence_decision === "unresolved" ? `证据不足：${{field.reason || "无法确认"}}` : (statusText[field.status] || "缺少事实，需补充")))); row.append(label,fact); grid.append(row); }}); return grid; }}
    function requiredOptionValue(option) {{ if (option && typeof option === "object") return String(option.id ?? option.value ?? option.name ?? option.label ?? ""); return String(option ?? ""); }}
    function requiredOptionLabel(option) {{ if (option && typeof option === "object") return String(option.name ?? option.label ?? option.value ?? option.id ?? ""); return String(option ?? ""); }}
    function requiredBooleanOptionLabel(field,value) {{ const positive = value === "true"; const label = String(field.label || "").toLowerCase(); if (label.includes("18+")) return positive ? "商品需要 18+ 标识" : "商品不需要 18+ 标识"; return positive ? "需要标记代码" : "不需要标记代码"; }}
    function renderRequiredAttributeEditor(container,item) {{
      const pendingFields = (item.skill_pending_required_fields || []).filter((field) => field && field.field_key);
      const fields = (item.manual_required_fields || []).filter((field) => field && field.field_key);
      if (!item.template_ready) return;
      if (pendingFields.length) {{
        const pending = document.createElement("div"); pending.className = "required-field-skill-pending";
        pending.textContent = `${{pendingFields.length}} 项必填字段正在等待智能字段 Skill 补足，当前无需用户填写。`;
        container.append(pending);
      }}
      if (!fields.length) return;
      const editor = document.createElement("section"); editor.className = "required-field-editor";
      const title = document.createElement("h4"); title.textContent = "补充技能无法确认的必填字段";
      const note = document.createElement("p"); note.textContent = "这里只显示智能字段 Skill 已穷尽采集证据后仍无法确认的必填项；不会显示待技能处理或已经映射的字段。";
      const grid = document.createElement("div"); grid.className = "required-field-grid";
      fields.forEach((field) => {{
        const wrapper = document.createElement("div"); wrapper.className = "required-field-input";
        const label = document.createElement("label"); label.textContent = field.label || field.field_key; label.htmlFor = `required-${{item.seed_id}}-${{field.field_key}}`;
        const options = Array.isArray(field.allowed_values) ? field.allowed_values : [];
        const input = options.length ? document.createElement("select") : document.createElement("input");
        input.id = label.htmlFor; input.dataset.fieldKey = field.field_key;
        if (options.length) {{
          const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "请选择"; input.append(placeholder);
          options.forEach((option) => {{ const choice = document.createElement("option"); choice.value = requiredOptionValue(option); const rawLabel = requiredOptionLabel(option); choice.textContent = String(field.attribute_type || "").toLowerCase() === "boolean" ? requiredBooleanOptionLabel(field,choice.value) : rawLabel; input.append(choice); }});
        }} else {{
          input.type = field.attribute_type === "integer" || field.attribute_type === "decimal" ? "number" : "text";
          input.placeholder = field.reason ? `缺失：${{field.reason}}` : "请输入真实值";
        }}
        wrapper.append(label,input); grid.append(wrapper);
      }});
      const actions = document.createElement("div"); actions.className = "required-field-actions";
      const save = document.createElement("button"); save.type = "button"; save.textContent = "保存必填证据";
      const status = document.createElement("span"); status.className = "required-field-status";
      save.addEventListener("click", async () => {{
        const values = {{}};
        editor.querySelectorAll("[data-field-key]").forEach((input) => {{ const value = String(input.value || "").trim(); if (value) values[input.dataset.fieldKey] = value; }});
        if (!Object.keys(values).length) {{ status.className = "required-field-status error"; status.textContent = "请至少填写一项真实必填字段。"; return; }}
        save.disabled = true; status.className = "required-field-status"; status.textContent = "正在保存并重新计算本商品门禁…";
        try {{
          const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/required-attributes/${{encodeURIComponent(item.seed_id)}}`, {{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{values}})}});
          status.className = "required-field-status success"; status.textContent = `已保存 ${{(result.data.saved_field_keys || []).length}} 项真实证据。`;
          await loadUploadWorkspace();
        }} catch (error) {{
          status.className = "required-field-status error"; status.textContent = error.message || (error.errors || []).join("；") || "保存失败";
          save.disabled = false;
        }}
      }});
      actions.append(save,status); editor.append(title,note,grid,actions); container.append(editor);
    }}
    function renderMapping(container, item) {{ if (!item.template_ready) {{ const summary = document.createElement("div"); summary.className = "mapping-summary"; const blocked = document.createElement("span"); blocked.className = "mapping-chip warn"; blocked.textContent = "模板错配，未执行字段映射"; summary.append(blocked); container.append(summary); return; }} const fields = item.attribute_mapping || []; const summary = document.createElement("div"); summary.className = "mapping-summary"; const ready = document.createElement("span"); ready.className = `mapping-chip${{item.required_attributes_ready ? "" : " warn"}}`; ready.textContent = `必填 ${{item.required_mapped_count || 0}} / ${{item.required_attribute_count || 0}}`; const mapped = document.createElement("span"); mapped.className = "mapping-chip"; mapped.textContent = `模板已填 ${{item.mapped_attribute_count || 0}}`; const rewrite = document.createElement("span"); rewrite.className = `mapping-chip${{(item.rewrite_required_count || 0) ? " warn" : ""}}`; rewrite.textContent = `待智能生成/规范 ${{item.rewrite_required_count || 0}}`; const missing = document.createElement("span"); missing.className = `mapping-chip${{(item.missing_fact_count || 0) ? " warn" : ""}}`; missing.textContent = `缺少事实 ${{item.missing_fact_count || 0}}`; const assets = document.createElement("span"); assets.className = "mapping-chip"; assets.textContent = `未提供可选素材 ${{item.not_applicable_count || 0}}`; summary.append(ready,mapped,rewrite,missing,assets); container.append(summary); const score = item.attribute_score_progress || {{}}; if (score.scorable_attribute_count) {{ const progress = document.createElement("div"); progress.className = "score-progress"; const paths = []; if (score.fields_to_50_percent > 0) paths.push(`再补 ${{score.fields_to_50_percent}} 项进入 15 分档`); if (score.fields_to_70_percent > 0) paths.push(`再补 ${{score.fields_to_70_percent}} 项进入 30 分档`); if (!paths.length) paths.push("已达到属性完整度 30 分档"); progress.textContent = `Ozon 属性分预估 ${{score.estimated_attribute_points}} / 30 · 计分属性 ${{score.filled_attribute_count}} / ${{score.scorable_attribute_count}} (${{score.completion_percent}}%) · ${{paths.join("；")}}`; container.append(progress); }} const details = document.createElement("details"); details.className = "all-mappings"; details.open = false; const label = document.createElement("summary"); label.textContent = `查看全部模板字段（只读） (${{fields.length}})`; details.append(label,mappingGrid(fields)); container.append(details); }}
    async function previewProductUpload(item, controls) {{
      const previewButton = controls.querySelector("[data-action=preview]");
      const confirmButton = controls.querySelector("[data-action=confirm]");
      const status = controls.querySelector(".product-upload-status");
      previewButton.disabled = true; confirmButton.disabled = true; status.className = "product-upload-status"; status.textContent = "正在校验字段、价格和锁定的 1688 原图，并生成单原图建品载荷…";
      try {{
        const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/product-upload/${{encodeURIComponent(item.seed_id)}}/preview`, {{method:"POST",headers:{{"Content-Type":"application/json"}},body:"{{}}"}});
        productUploadState.previews.set(item.seed_id,result.data);
        const payload = result.data.seller_api_item || {{}};
        status.className = "product-upload-status success";
        status.textContent = `建品载荷已就绪：${{payload.name || item.source_title}} · 1 张锁定 1688 原图 · Seller API 售价 ${{payload.price || "-"}} ${{payload.currency_code || "-"}}`;
        previewButton.textContent = "重新预览载荷";
        confirmButton.disabled = false;
      }} catch (error) {{
        status.className = "product-upload-status error";
        status.textContent = error.message || "最终上传预览生成失败";
        previewButton.disabled = false;
      }}
    }}
    function sellerUploadDiagnostics(submission) {{
      const payload = submission && submission.seller_api_status;
      const diagnostics = {{blocking:[], warnings:[]}};
      if (!payload) return diagnostics;
      const warningLevels = new Set(["warning", "ERROR_LEVEL_WARNING".toLowerCase()]);
      const blockingLevels = new Set(["error", "critical", "fatal", "error_level_error", "error_level_critical", "error_level_fatal"]);
      const visit = (value) => {{
        if (Array.isArray(value)) {{ value.forEach(visit); return; }}
        if (!value || typeof value !== "object") return;
        const level = String(value.level || "").trim().toLowerCase();
        const details = [value.code,value.message,value.description].filter(Boolean).map(String);
        if (details.length) {{
          const rendered = [...new Set(details)].join("：");
          if (warningLevels.has(level) || level.includes("warning")) diagnostics.warnings.push(rendered);
          else if (blockingLevels.has(level) || level.endsWith("_error") || level.endsWith("_critical") || level.endsWith("_fatal") || String(submission.status || "").toLowerCase() === "failed") diagnostics.blocking.push(rendered);
        }}
        Object.entries(value).forEach(([key,nested]) => {{ if (!["code","message","description","level"].includes(key)) visit(nested); }});
      }};
      visit(payload);
      diagnostics.blocking = [...new Set(diagnostics.blocking)].slice(0,4);
      diagnostics.warnings = [...new Set(diagnostics.warnings)].slice(0,4);
      return diagnostics;
    }}
    function sellerUploadError(submission) {{
      return sellerUploadDiagnostics(submission).blocking.join(" · ") || "Ozon 未返回详细阻断原因";
    }}
    function sellerUploadWarning(submission) {{
      return sellerUploadDiagnostics(submission).warnings.join(" · ");
    }}
    function scheduleProductUploadStatusRetry(item, controls, remaining) {{
      if (remaining <= 0) return false;
      const attempt = 31 - remaining;
      const delay = Math.min(8000, 2000 + Math.max(0, attempt) * 500);
      setTimeout(
        () => pollProductUploadStatus(item, controls, remaining - 1),
        delay,
      );
      return true;
    }}
    async function pollProductUploadStatus(item, controls, remaining=30) {{
      const confirmButton = controls.querySelector("[data-action=confirm]");
      const previewButton = controls.querySelector("[data-action=preview]");
      const status = controls.querySelector(".product-upload-status");
      try {{
        const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/product-upload/${{encodeURIComponent(item.seed_id)}}/status`, {{method:"POST",headers:{{"Content-Type":"application/json"}},body:"{{}}" }});
        const submission = result.data || {{}};
        productUploadState.submissions.set(item.seed_id,submission);
        if (submission.status === "accepted_by_ozon") {{
          const warning = sellerUploadWarning(submission);
          const packageId = String(submission.image_task_package_id || "");
          status.className = packageId ? (warning ? "product-upload-status non-blocking-warning" : "product-upload-status success") : "product-upload-status error";
          status.textContent = packageId
            ? `Ozon 已确认接收 · task_id ${{submission.task_id}} · 生图任务包 ${{packageId}} · ${{submission.image_task_package_status || "pending"}}。本商品工作台流程完成。${{warning ? ` 非阻断警告：${{warning}}。` : ""}}`
            : "Ozon 已确认接收，但生图任务包尚未写入，工作台正在自动补建。";
          confirmButton.disabled = true; previewButton.disabled = true; confirmButton.textContent = "上传完成";
          if (!packageId) scheduleProductUploadStatusRetry(item, controls, remaining);
          return;
        }}
        if (submission.status === "failed") {{
          const warning = sellerUploadWarning(submission);
          const packageId = String(submission.image_task_package_id || "");
          status.className = "product-upload-status error";
          status.textContent = `已提交 Seller API，但 Ozon 建品校验失败：${{sellerUploadError(submission)}}。${{warning ? `非阻断警告另列：${{warning}}。` : ""}}${{packageId ? `生图任务包 ${{packageId}} 已保留并继续执行；建品成功后自动绑定上传。` : "生图任务包正在自动补建。"}}修正后可重新准备并上传。`;
          previewButton.disabled = !item.ready_to_build; confirmButton.disabled = true; previewButton.textContent = "修正后重新准备";
          return;
        }}
        status.className = "product-upload-status";
        status.textContent = `Ozon 正在处理 · task_id ${{submission.task_id}}，工作台正在自动回查…`;
        if (!scheduleProductUploadStatusRetry(item, controls, remaining)) {{
          status.textContent = `Ozon 仍在处理 · task_id ${{submission.task_id}}。刷新页面会继续回查。`;
          previewButton.disabled = true; confirmButton.disabled = true;
        }}
      }} catch (error) {{
        if (scheduleProductUploadStatusRetry(item, controls, remaining)) {{
          status.className = "product-upload-status";
          status.textContent = `状态回查遇到临时网络异常：${{error.message || error}}。正在自动重试，不会重复提交商品。`;
        }} else {{
          status.className = "product-upload-status error";
          status.textContent = `状态回查暂时不可用：${{error.message || error}}。刷新页面只会重新查询状态，不会重复提交商品。`;
        }}
      }}
    }}
    async function confirmProductUpload(item, controls) {{
      const preview = productUploadState.previews.get(item.seed_id);
      const confirmButton = controls.querySelector("[data-action=confirm]");
      const previewButton = controls.querySelector("[data-action=preview]");
      const status = controls.querySelector(".product-upload-status");
      if (!preview || !preview.confirmation_token) {{ status.className = "product-upload-status error"; status.textContent = "请先生成并检查最终上传预览。"; return; }}
      if (!window.confirm(`确认将“${{(preview.seller_api_item || {{}}).name || item.source_title}}”这一件商品提交到 Ozon？`)) return;
      confirmButton.disabled = true; previewButton.disabled = true; status.className = "product-upload-status"; status.textContent = "正在提交这一件商品到 Ozon...";
      try {{
        const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/product-upload/${{encodeURIComponent(item.seed_id)}}/confirm`, {{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{confirmation_token:preview.confirmation_token}})}});
        productUploadState.submissions.set(item.seed_id,result.data);
        status.className = "product-upload-status";
        status.textContent = `Ozon 已受理任务 · task_id ${{result.data.task_id}} · 生图任务包 ${{result.data.image_task_package_id || "正在补建"}}；建品与生图独立继续执行。`;
        confirmButton.textContent = "Ozon 处理中";
        await pollProductUploadStatus(item,controls);
      }} catch (error) {{
        status.className = "product-upload-status error"; status.textContent = error.message || "上传失败";
        productUploadState.previews.delete(item.seed_id);
        confirmButton.disabled = true;
        previewButton.disabled = !item.ready_to_build;
        previewButton.textContent = item.ready_to_build ? "重新预览载荷" : "修正后重新准备";
      }}
    }}
    function renderProductUploadActions(container,item) {{
      const controls = document.createElement("div"); controls.className = "product-upload-actions";
      const previewButton = document.createElement("button"); previewButton.type = "button"; previewButton.dataset.action = "preview"; previewButton.textContent = "预览单原图建品"; previewButton.disabled = !item.ready_to_build;
      const confirmButton = document.createElement("button"); confirmButton.type = "button"; confirmButton.dataset.action = "confirm"; confirmButton.className = "confirm"; confirmButton.textContent = "确认上传到 Ozon"; confirmButton.disabled = true;
      const status = document.createElement("span"); status.className = "product-upload-status"; status.textContent = item.ready_to_build ? "建品载荷只使用锁定的 1688 原图；提交后立即输出独立生图任务包，Ozon 建品成功后再绑定上传。" : "本商品尚有门禁未完成，不能提交。";
      if (item.ready_to_build && item.upload_preview && item.upload_preview.confirmation_token) {{ productUploadState.previews.set(item.seed_id,item.upload_preview); previewButton.textContent = "重新预览载荷"; confirmButton.disabled = false; const payload = item.upload_preview.seller_api_item || {{}}; status.textContent = `已准备：1 张锁定原图 · ${{payload.price || "-"}} ${{payload.currency_code || "-"}}。`; }}
      if (item.upload_submission && item.upload_submission.task_id != null) {{
        productUploadState.submissions.set(item.seed_id,item.upload_submission);
        const submissionStatus = item.upload_submission.status || "processing";
        if (submissionStatus === "failed") {{
          const packageId = String(item.upload_submission.image_task_package_id || ""); previewButton.disabled = !item.ready_to_build; confirmButton.disabled = true; previewButton.textContent = "修正后重新准备"; status.className = "product-upload-status error"; status.textContent = `已提交 Seller API，但 Ozon 建品校验失败：${{sellerUploadError(item.upload_submission)}}。${{packageId ? ` 生图任务包 ${{packageId}} · ${{item.upload_submission.image_task_package_status || "pending"}} 已保留。` : " 生图任务包正在自动补建。"}}`;
        }} else if (submissionStatus === "accepted_by_ozon") {{
          const warning = sellerUploadWarning(item.upload_submission); const packageId = String(item.upload_submission.image_task_package_id || ""); previewButton.disabled = true; confirmButton.disabled = true; confirmButton.textContent = "上传完成"; status.className = packageId ? (warning ? "product-upload-status non-blocking-warning" : "product-upload-status success") : "product-upload-status error"; status.textContent = packageId ? `Ozon 已确认接收 · task_id ${{item.upload_submission.task_id}} · 生图任务包 ${{packageId}} · ${{item.upload_submission.image_task_package_status || "pending"}}${{warning ? ` · 非阻断警告：${{warning}}` : ""}}` : "Ozon 已确认接收，但生图任务包尚未写入，工作台正在自动补建。"; if (!packageId) setTimeout(() => pollProductUploadStatus(item,controls),0);
        }} else {{
          previewButton.disabled = true; confirmButton.disabled = true; confirmButton.textContent = "Ozon 处理中"; status.textContent = `Ozon 正在处理 · task_id ${{item.upload_submission.task_id}}`;
          setTimeout(() => pollProductUploadStatus(item,controls),0);
        }}
      }}
      previewButton.addEventListener("click",() => previewProductUpload(item,controls)); confirmButton.addEventListener("click",() => confirmProductUpload(item,controls));
      controls.append(previewButton,confirmButton,status); container.append(controls);
    }}
    function renderUploadSubmissionSummary(gates) {{
      let summary = $("batchUploadReconciliation");
      if (!summary) {{
        summary = document.createElement("span");
        summary.id = "batchUploadReconciliation";
        summary.className = "batch-upload-status";
        $("batchUploadProducts").parentElement.insertBefore(summary, $("batchUploadStatus"));
      }}
      const attempted = gates.submission_attempted_count || 0;
      const accepted = gates.submission_accepted_count || 0;
      const failed = gates.submission_failed_count || 0;
      const packages = gates.image_task_package_count || 0;
      const missing = gates.image_task_missing_count || 0;
      summary.className = `batch-upload-status${{missing ? " error" : " success"}}`;
      summary.textContent = `已提交 Seller API ${{attempted}} 件 · Ozon 建品成功 ${{accepted}} 件 · Ozon 建品校验失败 ${{failed}} 件 · 生图任务包 ${{packages}} 个${{missing ? ` · 缺失 ${{missing}} 个，正在自动补建` : ""}}`;
    }}
    async function batchUploadProducts() {{
      const button = $("batchUploadProducts"); const status = $("batchUploadStatus");
      const items = (latestUploadData && latestUploadData.items) || [];
      const seedIds = items.filter((item) => !item.upload_submission || ["failed","error","declined"].includes(String(item.upload_submission.status || "").toLowerCase())).map((item) => item.seed_id);
      if (!seedIds.length) {{ status.className = "batch-upload-status success"; status.textContent = "当前批次没有待提交商品。"; return; }}
      if (!window.confirm(`确认并发校验这 ${{seedIds.length}} 件商品，并把通过自身门禁的商品提交到 Ozon？未通过的商品会单独保留。`)) return;
      button.disabled = true; status.className = "batch-upload-status"; status.textContent = `正在并发校验并上传 ${{seedIds.length}} 件商品（最多 4 件同时处理）…`;
      try {{
        const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/product-upload/batch`, {{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{seed_ids:seedIds,confirmed:true,max_workers:4}})}});
        const data = result.data || {{}};
        status.className = "batch-upload-status success";
        status.textContent = `已提交 ${{data.submitted_product_count || 0}} 件，已提交过 ${{data.already_submitted_product_count || 0}} 件，保留待补 ${{data.failed_or_blocked_product_count || 0}} 件。`;
        await loadUploadWorkspace();
      }} catch (error) {{
        const data = error.data || {{}};
        status.className = "batch-upload-status error";
        status.textContent = error.message || `没有商品通过校验；保留待补 ${{data.failed_or_blocked_product_count || seedIds.length}} 件。`;
        await loadUploadWorkspace();
      }} finally {{ button.disabled = false; }}
    }}
    function setGate(id, ready, text) {{ const row = $(id); row.classList.toggle("blocked", !ready); row.querySelector(".gate-icon").textContent = ready ? "✓" : "!"; row.querySelector("span:last-child").textContent = text; }}
    function render(data) {{
      latestUploadData = data;
      renderPricingWorkspace(data);
      const items = data.items || [];
      draftState.items = items;
      const draftPageCount = Math.max(1, Math.ceil(draftState.items.length / draftState.pageSize));
      draftState.page = Math.min(Math.max(0, draftState.page), draftPageCount - 1);
      const pageStart = draftState.page * draftState.pageSize;
      const pageItems = draftState.items.slice(pageStart, pageStart + draftState.pageSize);
      const gates = data.gates || {{}};
      renderUploadSubmissionSummary(gates);
      const total = gates.product_count || items.length;
      const readyCount = gates.ready_to_build_count || 0;
      $("productCount").textContent = String(items.length);
      const requiredTotal = gates.valid_required_attribute_count || 0;
      $("requiredCount").textContent = `${{gates.required_mapped_count || 0}} / ${{requiredTotal}}`;
      $("prefillCount").textContent = `${{gates.mapped_attribute_count || 0}} 项 · 待智能生成/规范 ${{gates.rewrite_required_count || 0}}`;
      $("imageReadyCount").textContent = `${{gates.bootstrap_image_ready_count || 0}} / ${{total}} 件`;
      $("workspaceStatus").textContent = readyCount > 0 ? `${{readyCount}} 件可独立推进 (Ready)` : "暂无可推进商品 (Waiting)";
      setGate("templateGate", (gates.template_ready_count || 0) > 0, `${{gates.template_ready_count || 0}} / ${{total}} 件模板已就绪。`);
      setGate("attributeGate", (gates.required_attributes_ready_count || 0) > 0, `${{gates.required_attributes_ready_count || 0}} / ${{total}} 件必填属性已自动映射完成。`);
      setGate("originalContentGate", (gates.original_content_ready_count || 0) > 0, `${{gates.original_content_ready_count || 0}} / ${{total}} 件俄文标题、简介、标签与 Rich Content 已完善。`);
      setGate("bootstrapImageGate", (gates.bootstrap_image_ready_count || 0) > 0, `${{gates.bootstrap_image_ready_count || 0}} / ${{total}} 件已锁定一张 1688 主体原图。`);
      setGate("pricingGate", (gates.pricing_ready_count || 0) > 0, `${{gates.pricing_ready_count || 0}} / ${{total}} 件价格与包装证据已确认。`);
      setGate("draftGate", readyCount > 0, readyCount > 0 ? `${{readyCount}} 件已通过自身门禁，可先进入草稿阶段。` : "每件商品独立等待自身缺失项。" );
      $("draftItems").replaceChildren();
      $("draftPageLabel").textContent = `第 ${{draftState.page + 1}} / ${{draftPageCount}} 页 · 每页 5 件`;
      $("draftPrevPage").disabled = draftState.page === 0;
      $("draftNextPage").disabled = draftState.page >= draftPageCount - 1;
      const blockerLabels = {{ category_template:"类目模板错配", required_attributes:"必填属性", original_content:"原创俄文内容", bootstrap_image:"锁定 1688 原图", pricing:"价格与包装证据" }};
      pageItems.forEach((item) => {{
        const card = document.createElement("article"); card.className = "draft-item";
        const preview = item.bootstrap_image_url || item.source_image;
        if (preview) {{ const image = document.createElement("img"); image.src = preview; image.alt = item.source_title || "Product image"; image.loading = "lazy"; image.referrerPolicy = "no-referrer"; card.append(image); }}
        else {{ const empty = document.createElement("div"); empty.className = "image-placeholder"; empty.textContent = "没有来源图片"; card.append(empty); }}
        const body = document.createElement("div");
        const label = document.createElement("div"); label.className = "source-label"; label.textContent = item.bootstrap_image_ready ? "锁定 1688 原图 · 建品临时主图" : "Ozon 来源标题 (Source Evidence Only)";
        const title = document.createElement("h3"); title.className = "source-title"; title.textContent = item.source_title || item.seed_id;
        const notice = document.createElement("div"); notice.className = "notice"; notice.textContent = item.template_ready ? `已读取完整 ${{item.attribute_schema_count || 0}} 字段模板：客观事实映射 ${{item.mapped_attribute_count || 0}} 项，待智能生成/规范 ${{item.rewrite_required_count || 0}} 项，缺少真实事实 ${{item.missing_fact_count || 0}} 项，可选素材未提供 ${{item.not_applicable_count || 0}} 项。` : `检测到类目模板错配：${{item.category_path || "未知模板"}}。本件停止映射，避免把错误字段写进草稿。`; if (!item.template_ready) {{ const refresh = document.createElement("button"); refresh.type = "button"; refresh.className = "inline-action"; refresh.textContent = "重新解析正确类目模板"; refresh.addEventListener("click", async () => {{ refresh.disabled = true; refresh.textContent = "正在解析..."; try {{ await api(`/api/batches/${{encodeURIComponent(runId)}}/attribute-template/refresh`, {{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{seed_id:item.seed_id}})}}); window.location.reload(); }} catch (error) {{ refresh.textContent = error.message || "解析失败"; refresh.disabled = false; }} }}); notice.append(refresh); }}
        const gateSummary = document.createElement("div"); gateSummary.className = `item-gate-summary${{item.ready_to_build ? " ready" : ""}}`;
        const gateTitle = document.createElement("strong"); gateTitle.textContent = item.ready_to_build ? "本件可独立推进" : "本件继续等待"; gateSummary.append(gateTitle);
        (item.blocking_gates || []).forEach((gate) => {{ const chip = document.createElement("span"); chip.className = "item-gate-chip"; chip.textContent = `缺少：${{blockerLabels[gate] || gate}}`; gateSummary.append(chip); }});
        if (item.ready_to_build) {{ const ready = document.createElement("span"); ready.className = "item-gate-chip"; ready.textContent = "不等待其他商品"; gateSummary.append(ready); }}
        const meta = document.createElement("div"); meta.className = "meta-grid"; meta.innerHTML = `<div class="meta"><span>精准类目</span><strong>${{item.category_path || "-"}}</strong></div><div class="meta"><span>属性模板</span><strong>${{item.attribute_schema_count || 0}} 字段</strong></div><div class="meta"><span>建品图片</span><strong>${{item.bootstrap_image_ready ? "锁定原图已就绪" : "缺少锁定原图"}}</strong></div>`;
        body.append(label,title,notice,gateSummary,meta); renderUploadCoreFields(body,item); renderRequiredAttributeEditor(body,item); renderMapping(body,item); renderProductUploadActions(body,item); card.append(body); $("draftItems").append(card);
      }});
      if (!items.length) {{ const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "当前批次没有可用的类目模板与商品证据"; $("draftItems").append(empty); }}
    }}
    function changeDraftPage(offset) {{ const pageCount = Math.max(1, Math.ceil(draftState.items.length / draftState.pageSize)); const nextPage = Math.min(Math.max(0, draftState.page + offset), pageCount - 1); if (nextPage === draftState.page || !latestUploadData) return; draftState.page = nextPage; render(latestUploadData); document.getElementById("draftPager").scrollIntoView({{behavior:"smooth",block:"start"}}); }}
    async function copyText(text) {{ if (navigator.clipboard && window.isSecureContext) {{ await navigator.clipboard.writeText(text); return; }} const area = document.createElement("textarea"); area.value = text; area.style.position = "fixed"; area.style.opacity = "0"; document.body.append(area); area.select(); const copied = document.execCommand("copy"); area.remove(); if (!copied) throw new Error("复制失败"); }}
    $("copyContentControllerCommand").addEventListener("click", async () => {{ const button = $("copyContentControllerCommand"); const status = $("contentControllerCopyStatus"); button.disabled = true; try {{ await copyText($("contentControllerCommand").textContent); status.textContent = "整批智能字段草稿命令已复制"; }} catch (error) {{ status.textContent = error.message || "复制失败，请手动复制"; }} finally {{ button.disabled = false; }} }});
    $("pricingPrevPage").addEventListener("click", () => changePricingPage(-1));
    $("pricingNextPage").addEventListener("click", () => changePricingPage(1));
    $("draftPrevPage").addEventListener("click", () => changeDraftPage(-1));
    $("draftNextPage").addEventListener("click", () => changeDraftPage(1));
    $("batchUploadProducts").addEventListener("click", batchUploadProducts);
    async function loadUploadWorkspace() {{ const result = await api(`/api/batches/${{encodeURIComponent(runId)}}/upload`); render(result.data || {{}}); return result.data || {{}}; }}
    loadUploadWorkspace().catch((error) => {{ $("workspaceStatus").textContent = "加载失败 (Failed)"; $("draftItems").textContent = error.message || String(error); }});
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
                self._send_redirect(f"/batches/{parts[1]}/upload")
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
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "content-tasks":
                self._send_result(service.content_tasks(parts[2]), run_id=parts[2])
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
                incoming_run_id = str(incoming.get("run_id") or "").strip()
                if incoming_run_id and not selected_repo.is_current_workbench_run_id(incoming_run_id):
                    incoming.update(
                        {
                            "run_id": None,
                            "task_type": None,
                            "stage": "stale_task_ignored",
                            "code": "browser_task.stale_ignored",
                            "message": f"Stale browser task reference was discarded: {incoming_run_id}",
                            "details": {"stale_run_id": incoming_run_id},
                        }
                    )
                if self._should_ignore_invalidated_heartbeat(incoming):
                    saved = selected_repo.load_browser_bridge_status()
                    response_code = "browser_bridge.heartbeat_ignored"
                    response_message = "Invalidated old-page heartbeat was ignored."
                else:
                    saved = selected_repo.save_browser_bridge_status(incoming)
                    response_code = "browser_bridge.heartbeat"
                    response_message = "Browser bridge heartbeat saved."
                run_id = str(incoming.get("run_id") or "").strip()
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
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "upload-draft":
                self._send_result(service.build_upload_draft(parts[2]), run_id=parts[2])
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "batches"]
                and parts[3:] == ["product-upload", "batch"]
            ):
                raw_seed_ids = payload.get("seed_ids")
                seed_ids = (
                    [str(seed_id) for seed_id in raw_seed_ids]
                    if isinstance(raw_seed_ids, list)
                    else None
                )
                self._send_result(
                    service.batch_upload_products(
                        parts[2],
                        seed_ids=seed_ids,
                        confirmed=payload.get("confirmed") is True,
                        max_workers=int(payload.get("max_workers") or 4),
                    ),
                    run_id=parts[2],
                )
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "batches"]
                and parts[3] == "required-attributes"
            ):
                values = payload.get("values")
                self._send_result(
                    service.save_required_attribute_evidence(
                        parts[2],
                        parts[4],
                        values if isinstance(values, dict) else {},
                    ),
                    run_id=parts[2],
                )
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "batches"]
                and parts[3] == "product-upload"
                and parts[5] == "preview"
            ):
                self._send_result(
                    service.preview_product_upload(parts[2], parts[4]),
                    run_id=parts[2],
                )
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "batches"]
                and parts[3] == "products"
                and parts[5] == "exclude"
            ):
                self._send_result(
                    service.exclude_product_without_replacement(
                        parts[2],
                        parts[4],
                        str(payload.get("reason") or ""),
                        confirmed=payload.get("confirmed") is True,
                    ),
                    run_id=parts[2],
                )
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "batches"]
                and parts[3] == "product-upload"
                and parts[5] == "confirm"
            ):
                self._send_result(
                    service.submit_product_upload(
                        parts[2],
                        parts[4],
                        confirmation_token=str(
                            payload.get("confirmation_token") or ""
                        ),
                    ),
                    run_id=parts[2],
                )
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "batches"]
                and parts[3] == "product-upload"
                and parts[5] == "status"
            ):
                self._send_result(
                    service.refresh_product_upload_status(parts[2], parts[4]),
                    run_id=parts[2],
                )
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "batches"]
                and parts[3:] == ["pricing-evidence", "preview"]
            ):
                self._send_result(
                    service.preview_pricing_evidence(parts[2], payload),
                    run_id=parts[2],
                )
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "batches"]
                and parts[3:] == ["pricing-evidence", "confirm"]
            ):
                self._send_result(
                    service.confirm_pricing_evidence(parts[2], payload),
                    run_id=parts[2],
                )
                return
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["content-tasks", "complete"]:
                self._send_result(
                    service.complete_content_task(parts[2], payload),
                    run_id=parts[2],
                )
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
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["attribute-template", "refresh"]:
                self._send_result(
                    service.refresh_attribute_template(
                        parts[2],
                        str(payload.get("seed_id") or "").strip(),
                    ),
                    run_id=parts[2],
                )
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
            if len(parts) == 5 and parts[:2] == ["api", "batches"] and parts[3:] == ["supplier-sku", "reopen"]:
                self._send_result(
                    service.reopen_supplier_sku_selection(
                        parts[2],
                        seed_id=str(payload.get("seed_id") or "").strip(),
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
                if parts[5] == "approve":
                    self._send_result(service.approve_image_job(parts[2], parts[4]), run_id=parts[2])
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
            try:
                run = service.recover_browser_task_state(run_id)
            except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
                return {
                    "ok": True,
                    "code": "browser_task.none",
                    "message": "The referenced browser task no longer belongs to a current workbench batch.",
                    "data": {"stale_run_id": run_id},
                    "errors": [],
                }
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
            recapture_seed_ids = {
                str(seed_id).strip()
                for seed_id in run.get("supplier_recapture_seed_ids") or []
                if str(seed_id).strip()
            }
            if run.get("status") == "supplier_review" or (
                recapture_seed_ids
                and run.get("status") in {"supplier_collected", "image_processing"}
            ):
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
                    if recapture_seed_ids and str(item.get("seed_id") or "") not in recapture_seed_ids:
                        continue
                    if str(item.get("seed_id") or "") in captured_seed_ids:
                        continue
                    reference_image_urls = []
                    for value in item.get("ozon_reference_images") or [item.get("ozon_main_image")]:
                        image_url = str(value or "").strip()
                        if image_url.startswith("https://") and image_url not in reference_image_urls:
                            reference_image_urls.append(image_url)
                    channels.append(
                        {
                            "channel_index": channel_index,
                            "seed_id": item.get("seed_id"),
                            "ozon_product_id": item.get("ozon_product_id"),
                            "ozon_title": item.get("ozon_title"),
                            "ozon_url": item.get("ozon_url"),
                            "reference_image_url": item.get("ozon_main_image"),
                            "reference_image_urls": reference_image_urls,
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
            for run in selected_repo.list_workbench_runs():
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

        def _send_redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self._send_cors_headers()
            self.end_headers()

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
