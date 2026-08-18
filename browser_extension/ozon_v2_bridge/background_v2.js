"use strict";

const BASE_URL = "http://127.0.0.1:8765";
const POLL_ALARM = "ozon_v2_browser_bridge_poll";
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const MAX_SUPPLIER_LANES = 5;
const MANAGED_TAB_QUERY_ATTEMPTS = 30;
const MANAGED_TAB_QUERY_DELAY_MS = 100;
const NATIVE_NEW_TAB_INTENT_MS = 2500;
const SUPPLIER_NAVIGATION_INTENT_MS = 30000;
const SUPPLIER_TERMINAL_STATES = new Set(["collected", "user_skipped"]);
const WORKBENCH_AUTH_STORAGE_KEY = "workbenchAuthToken";
const supplierNativeNewTabIntents = new Map();
let openTaskQueue = Promise.resolve();
let supplierNavigationQueue = Promise.resolve();
let pollingStarted = false;

function searchUrl(query) {
  const scopedQuery = /из\s+китая/iu.test(String(query || ""))
    ? String(query || "").trim()
    : `${String(query || "").trim()} из Китая`.trim();
  return `https://www.ozon.ru/search/?from_global=true&text=${encodeURIComponent(scopedQuery)}&__rr=1`;
}

async function workbenchFetch(path, options = {}) {
  if (typeof path !== "string" || !path.startsWith("/api/")) {
    throw new Error("Only local workbench API paths may be requested.");
  }
  const stored = await chrome.storage.local.get({ [WORKBENCH_AUTH_STORAGE_KEY]: "" });
  const token = String(stored[WORKBENCH_AUTH_STORAGE_KEY] || "");
  if (!token) throw new Error("Open or refresh the local workbench to authenticate the browser bridge.");
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
    "X-Ozon-Workbench-Token": token,
  };
  return await fetch(`${BASE_URL}${path}`, { ...options, headers });
}

async function workbenchJson(path, options = {}) {
  const response = await workbenchFetch(path, options);
  return await response.json();
}

async function postJson(path, payload) {
  return await workbenchJson(path, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function stopRunUntilExplicitResume(runId) {
  if (!runId) return false;
  try {
    await postJson(`/api/batches/${encodeURIComponent(runId)}/runner/stop`, {});
    return true;
  } catch (_) {
    return false;
  }
}

async function heartbeat(payload = {}) {
  try {
    await postJson("/api/browser-bridge/heartbeat", {
      bridge_id: "ozon_v2_browser_extension",
      source: payload.source || "background",
      extension_version: EXTENSION_VERSION,
      url: payload.url || "",
      run_id: payload.run_id || null,
      task_type: payload.task_type || null,
      stage: payload.stage || null,
      code: payload.code || null,
      message: payload.message || null,
      connection_only: payload.connection_only === true,
      details: payload.details && typeof payload.details === "object" ? payload.details : null,
    });
  } catch (_) {
    // The workbench may be closed. Keep the extension quiet until it returns.
  }
}

async function activeTask() {
  const stored = await chrome.storage.local.get({ currentRunId: "" });
  if (stored.currentRunId) {
    const current = await workbenchJson(`/api/batches/${encodeURIComponent(stored.currentRunId)}/browser-task`);
    if (isRunnableTask(current)) return current;
    const active = await workbenchJson("/api/browser-task/active");
    if (isRunnableTask(active) && isNewerTask(active, current)) return active;
    return current;
  }
  return await workbenchJson("/api/browser-task/active");
}

async function currentWorkbenchTask() {
  return await activeTask();
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

function taskKey(task) {
  const data = task && task.data ? task.data : {};
  return `${data.run_id || "none"}:${data.task_type || task.code || "none"}`;
}

function taskDispatchToken(task) {
  const data = task && task.data ? task.data : {};
  return data.dispatch_token || data.created_at || taskKey(task);
}

function taskSearchUrl(task) {
  const seeds = (((task.data || {}).contract || {}).payload || {}).seeds || [];
  const seed = seeds[0] || {};
  const query = (seed.ozon_query_terms_ru || [])[0] || seed.source_text_zh || "";
  return query ? searchUrl(query) : "https://www.ozon.ru/";
}

function isSupplierTask(task) {
  return !!task && (
    task.code === "browser_task.supplier_collection_ready" ||
    task.code === "browser_task.supplier_selection_ready"
  );
}

function isManagedSupplierTask(task) {
  return !!task && task.code === "browser_task.supplier_selection_ready";
}

function taskTargetUrl(task) {
  if (!isSupplierTask(task)) return taskSearchUrl(task);
  const items = (((task.data || {}).contract || {}).items || []);
  return items[0] && items[0].supplier_url
    ? String(items[0].supplier_url)
    : "https://www.1688.com/";
}

function isRunnableTask(task) {
  return !!task && (
    task.code === "browser_task.ozon_collection_ready" ||
    task.code === "browser_task.supplier_collection_ready" ||
    task.code === "browser_task.supplier_selection_ready"
  );
}

async function loadOpenedTasks() {
  const stored = await chrome.storage.local.get({ openedTasks: {} });
  return stored.openedTasks || {};
}

async function saveOpenedTasks(openedTasks) {
  await chrome.storage.local.set({ openedTasks });
}

async function handleTaskTabRemoved(tabId) {
  const openedTasks = await loadOpenedTasks();
  let protectionChanged = false;
  for (const entry of Object.values(openedTasks)) {
    if (!entry || !Array.isArray(entry.protectedTabIds) || !entry.protectedTabIds.includes(tabId)) continue;
    entry.protectedTabIds = entry.protectedTabIds.filter((value) => value !== tabId);
    protectionChanged = true;
  }
  const matches = Object.entries(openedTasks).filter(([, entry]) => entry && (
    entry.tabId === tabId ||
    (Array.isArray(entry.channels) && entry.channels.some((channel) => channel.tabId === tabId))
  ));
  if (!matches.length) {
    if (protectionChanged) await saveOpenedTasks(openedTasks);
    return { stopped: false };
  }
  const closedAt = new Date().toISOString();
  let stopped = false;
  for (const [key, entry] of matches) {
    if (entry.closingByExtension) continue;
    const closedChannel = Array.isArray(entry.channels)
      ? entry.channels.find((channel) => channel.tabId === tabId)
      : null;
    if (closedChannel) {
      if (entry.pendingNavigation && entry.pendingNavigation.sourceTabId === tabId) {
        entry.pendingNavigation = null;
      }
      entry.pendingNavigations = (Array.isArray(entry.pendingNavigations) ? entry.pendingNavigations : [])
        .filter((intent) => intent.sourceTabId !== tabId);
      closedChannel.tabId = null;
      closedChannel.closedAt = closedAt;
      if (!SUPPLIER_TERMINAL_STATES.has(closedChannel.state)) closedChannel.state = "waiting_user";
      entry.launchState = "incomplete";
      entry.closedByUser = false;
      entry.lastUpdatedAt = closedAt;
      openedTasks[key] = entry;
      await saveOpenedTasks(openedTasks);
      continue;
    }
    const runId = key.split(":", 1)[0];
    let task = null;
    try {
      task = await workbenchJson(`/api/batches/${encodeURIComponent(runId)}/browser-task`);
    } catch (_) {
      // Local suppression still prevents a reopen while the workbench restarts.
    }
    entry.tabId = null;
    entry.pendingNavigation = null;
    entry.pendingNavigations = [];
    entry.channels = Array.isArray(entry.channels)
      ? entry.channels.map((channel) => ({ ...channel, tabId: null }))
      : entry.channels;
    entry.closedByUser = true;
    entry.closedAt = closedAt;
    if (Array.isArray(entry.channels)) entry.launchState = "user_closed";
    entry.closedDispatchToken = isRunnableTask(task) && taskKey(task) === key
      ? taskDispatchToken(task)
      : entry.dispatchToken || null;
    stopped = true;
    openedTasks[key] = entry;
    await saveOpenedTasks(openedTasks);
    if (!isRunnableTask(task) || taskKey(task) !== key) continue;
    try {
      await postJson(`/api/batches/${encodeURIComponent(runId)}/runner/stop`, {});
    } catch (_) {
      // The local close marker remains authoritative until an explicit resume token arrives.
    }
    await heartbeat({
      source: "background_tab_removed",
      run_id: runId,
      task_type: task.data.task_type,
      stage: "stopped_by_user",
      code: "browser_task.cancelled",
      message: "The assigned browser tab was closed by the user; collection will remain stopped until resumed.",
      url: "",
    });
  }
  return { stopped };
}

async function supplierChannelForTab(tabId) {
  if (!Number.isInteger(tabId)) return null;
  const openedTasks = await loadOpenedTasks();
  for (const entry of Object.values(openedTasks)) {
    if (!entry || !Array.isArray(entry.channels)) continue;
    const channel = entry.channels.find((item) => item.tabId === tabId);
    if (channel) return channel;
  }
  return null;
}

async function managedSupplierEntryForTab(tabId) {
  if (!Number.isInteger(tabId)) return null;
  const openedTasks = await loadOpenedTasks();
  for (const [key, entry] of Object.entries(openedTasks)) {
    if (!entry || !Array.isArray(entry.channels)) continue;
    const channel = entry.channels.find((item) => item.tabId === tabId);
    if (channel) return { key, entry, channel, openedTasks };
  }
  return null;
}

function managedSupplierTaskForChannel(channel) {
  return {
    code: "browser_task.supplier_selection_ready",
    data: {
      run_id: channel.run_id,
      task_type: channel.task_type,
    },
  };
}

async function recordSupplierNativeNewTabIntent(tabId, url) {
  const match = await managedSupplierEntryForTab(tabId);
  if (!match) return { ok: false, code: "supplier_selection.channel_missing" };
  supplierNativeNewTabIntents.set(tabId, {
    url: String(url || ""),
    windowId: match.entry.windowId,
    expiresAt: Date.now() + NATIVE_NEW_TAB_INTENT_MS,
  });
  return { ok: true };
}

async function consumeSupplierNativeNewTabIntent(openerTabId) {
  await new Promise((resolve) => setTimeout(resolve, 50));
  const intent = supplierNativeNewTabIntents.get(openerTabId);
  if (!intent) return false;
  supplierNativeNewTabIntents.delete(openerTabId);
  return intent.expiresAt >= Date.now();
}

async function consumeSupplierNativeNewTabIntentForWindow(windowId) {
  await new Promise((resolve) => setTimeout(resolve, 50));
  for (const [sourceTabId, intent] of supplierNativeNewTabIntents.entries()) {
    if (intent.expiresAt < Date.now()) {
      supplierNativeNewTabIntents.delete(sourceTabId);
      continue;
    }
    if (intent.windowId !== windowId) continue;
    supplierNativeNewTabIntents.delete(sourceTabId);
    return sourceTabId;
  }
  return null;
}

async function protectSupplierTab(sourceTabId, tab) {
  const match = await managedSupplierEntryForTab(sourceTabId);
  if (!match || !tab || tab.windowId !== match.entry.windowId) return false;
  match.entry.protectedTabIds = Array.from(new Set([
    ...(Array.isArray(match.entry.protectedTabIds) ? match.entry.protectedTabIds : []),
    tab.id,
  ]));
  await saveOpenedTasks(match.openedTasks);
  return true;
}

async function isProtectedSupplierTab(tab) {
  if (!tab || !Number.isInteger(tab.id) || !Number.isInteger(tab.windowId)) return false;
  const openedTasks = await loadOpenedTasks();
  return Object.values(openedTasks).some((entry) => (
    entry
    && entry.windowId === tab.windowId
    && Array.isArray(entry.protectedTabIds)
    && entry.protectedTabIds.includes(tab.id)
  ));
}

async function recordSupplierNavigationIntent(tabId) {
  const match = await managedSupplierEntryForTab(tabId);
  if (!match) return { ok: false, code: "supplier_selection.channel_missing" };
  const now = Date.now();
  const candidates = [
    ...(match.entry.pendingNavigation ? [match.entry.pendingNavigation] : []),
    ...(Array.isArray(match.entry.pendingNavigations) ? match.entry.pendingNavigations : []),
  ].filter((intent) => Number(intent.expiresAt || 0) >= now);
  const existing = candidates.find((intent) => (
    intent.sourceTabId === tabId
    && intent.channel_index === match.channel.channel_index
    && String(intent.seed_id || "") === String(match.channel.seed_id || "")
    && String(intent.ozon_product_id || "") === String(match.channel.ozon_product_id || "")
  ));
  if (existing) {
    match.entry.pendingNavigation = existing;
    match.entry.pendingNavigations = [];
    await saveOpenedTasks(match.openedTasks);
    return { ok: true, granted: true };
  }
  if (candidates.length) {
    return {
      ok: false,
      granted: false,
      code: "supplier_selection.navigation_busy",
      channel_index: candidates[0].channel_index,
    };
  }
  match.entry.pendingNavigation = {
    sourceTabId: tabId,
    channel_index: match.channel.channel_index,
    seed_id: match.channel.seed_id,
    ozon_product_id: match.channel.ozon_product_id,
    createdAt: now,
    expiresAt: now + SUPPLIER_NAVIGATION_INTENT_MS,
  };
  match.entry.pendingNavigations = [];
  match.entry.lastUpdatedAt = new Date().toISOString();
  await saveOpenedTasks(match.openedTasks);
  return { ok: true, granted: true };
}

async function releaseSupplierNavigationIntent(tabId) {
  const match = await managedSupplierEntryForTab(tabId);
  if (!match) return { ok: false, code: "supplier_selection.channel_missing" };
  const intent = match.entry.pendingNavigation;
  if (!intent) return { ok: true, released: false };
  if (intent.sourceTabId !== tabId) {
    return { ok: false, released: false, code: "supplier_selection.navigation_not_owner" };
  }
  match.entry.pendingNavigation = null;
  match.entry.pendingNavigations = [];
  match.entry.lastUpdatedAt = new Date().toISOString();
  await saveOpenedTasks(match.openedTasks);
  return { ok: true, released: true };
}

async function managedSupplierEntryForNavigation(tab) {
  if (!tab || !Number.isInteger(tab.windowId)) return null;
  const openedTasks = await loadOpenedTasks();
  for (const [key, entry] of Object.entries(openedTasks)) {
    if (!entry || entry.windowId !== tab.windowId || !Array.isArray(entry.channels)) continue;
    const now = Date.now();
    const intent = entry.pendingNavigation;
    if (!intent || Number(intent.expiresAt || 0) < now) continue;
    const channel = entry.channels.find((item) => (
      item.channel_index === intent.channel_index
      && String(item.seed_id || "") === String(intent.seed_id || "")
      && String(item.ozon_product_id || "") === String(intent.ozon_product_id || "")
    ));
    if (!channel) continue;
    entry.pendingNavigation = null;
    entry.pendingNavigations = [];
    await saveOpenedTasks(openedTasks);
    return { key, entry, channel, openedTasks };
  }
  return null;
}

async function adoptSupplierTab(match, tab) {
  const replacedTabId = match.channel.tabId;
  match.channel.tabId = tab.id;
  match.channel.windowId = tab.windowId;
  match.entry.pendingNavigation = null;
  match.entry.pendingNavigations = [];
  match.entry.lastUpdatedAt = new Date().toISOString();
  await saveOpenedTasks(match.openedTasks);

  if (Number.isInteger(replacedTabId) && replacedTabId !== tab.id) {
    try {
      await chrome.tabs.remove(replacedTabId);
    } catch (_) {
      // The old lane page may already have closed after opening its replacement.
    }
  }
  let injected = false;
  try {
    const adoptedTab = await chrome.tabs.get(tab.id);
    injected = await ensureContentScript(adoptedTab, managedSupplierTaskForChannel(match.channel));
  } catch (_) {
    injected = false;
  }
  return {
    adopted: true,
    injected,
    tabId: tab.id,
    replacedTabId,
    channelIndex: match.channel.channel_index,
  };
}

async function resolveSupplierChannelForTab(tab) {
  if (!tab || !Number.isInteger(tab.id)) return null;
  const exact = await managedSupplierEntryForTab(tab.id);
  if (exact) return exact.channel;
  if (await isProtectedSupplierTab(tab)) return null;
  if (!isSupplierTab(tab)) return null;
  const recoverable = await managedSupplierEntryForNavigation(tab);
  if (!recoverable) return null;
  await adoptSupplierTab(recoverable, tab);
  return recoverable.channel;
}

async function handleSupplierTabCreated(tab) {
  if (!tab || !Number.isInteger(tab.id)) {
    return { adopted: false };
  }
  const nativeSourceTabId = Number.isInteger(tab.openerTabId)
    ? (await consumeSupplierNativeNewTabIntent(tab.openerTabId) ? tab.openerTabId : null)
    : await consumeSupplierNativeNewTabIntentForWindow(tab.windowId);
  if (Number.isInteger(nativeSourceTabId)) {
    await protectSupplierTab(nativeSourceTabId, tab);
    return { adopted: false, nativeIntent: true };
  }
  let match = Number.isInteger(tab.openerTabId)
    ? await managedSupplierEntryForTab(tab.openerTabId)
    : null;
  if (!match) match = await managedSupplierEntryForNavigation(tab);
  if (!match || tab.windowId !== match.entry.windowId) {
    return { adopted: false };
  }
  return await adoptSupplierTab(match, tab);
}

async function handleSupplierTabUpdated(tabId, changeInfo, tab) {
  if (!changeInfo || (!changeInfo.url && !["loading", "complete"].includes(changeInfo.status))) {
    return { injected: false };
  }
  const match = await managedSupplierEntryForTab(tabId);
  if (!match || !tab || tab.windowId !== match.entry.windowId) return { injected: false };
  if (changeInfo.url || changeInfo.status === "loading") await releaseSupplierNavigationIntent(tabId);
  const task = managedSupplierTaskForChannel(match.channel);
  return { injected: await ensureContentScript(tab, task) };
}

async function handleSupplierTabActivated(activeInfo) {
  if (!activeInfo || !Number.isInteger(activeInfo.tabId) || !Number.isInteger(activeInfo.windowId)) {
    return { recovered: false };
  }
  let tab = null;
  try {
    tab = await chrome.tabs.get(activeInfo.tabId);
  } catch (_) {
    return { recovered: false };
  }
  if (!tab || tab.windowId !== activeInfo.windowId || !isSupplierTab(tab)) return { recovered: false };
  const existing = await supplierChannelForTab(tab.id);
  const binding = await resolveSupplierChannelForTab(tab);
  if (!binding) return { recovered: false };
  if (existing) await ensureContentScript(tab, managedSupplierTaskForChannel(binding));
  return { recovered: !existing, binding };
}

async function closeManagedSupplierRound(match, reason) {
  const { entry, openedTasks } = match;
  entry.closingByExtension = true;
  entry.launchState = "closing";
  entry.closeIntent = reason;
  entry.closedByUser = false;
  entry.lastUpdatedAt = new Date().toISOString();
  await saveOpenedTasks(openedTasks);
  const windowId = entry.windowId;
  if (Number.isInteger(windowId)) {
    try {
      await chrome.windows.remove(windowId);
    } catch (_) {
      // A user or the browser may have closed it between persistence and cleanup.
    }
  }
  entry.windowId = null;
  entry.closingByExtension = false;
  entry.closedAt = new Date().toISOString();
  entry.closedByUser = false;
  entry.launchState = "closed";
  await saveOpenedTasks(openedTasks);
}

async function markSupplierChannelTerminal(tabId, state, options = {}) {
  if (!SUPPLIER_TERMINAL_STATES.has(state)) {
    return { ok: false, code: "supplier_selection.invalid_terminal_state" };
  }
  const match = await managedSupplierEntryForTab(tabId);
  if (!match) return { ok: false, code: "supplier_selection.channel_missing" };
  match.channel.state = state;
  match.channel.terminalAt = new Date().toISOString();
  match.entry.lastUpdatedAt = match.channel.terminalAt;
  await saveOpenedTasks(match.openedTasks);
  const allTerminal = match.entry.channels.every((channel) => SUPPLIER_TERMINAL_STATES.has(channel.state));
  if (allTerminal || options.forceClose === true) {
    await closeManagedSupplierRound(match, options.closeReason || "round_complete");
  }
  return { ok: true, state, allTerminal };
}

function supplierOfferId(value) {
  const match = String(value || "").match(/\/offer\/(\d+)\.html/i);
  return match ? match[1] : "";
}

async function captureSupplierSelectionFromTab(tab, supplierProduct) {
  if (!tab || !Number.isInteger(tab.id)) {
    return { ok: false, code: "supplier_selection.channel_missing" };
  }
  const match = await managedSupplierEntryForTab(tab.id);
  if (!match || match.entry.windowId !== tab.windowId) {
    return { ok: false, code: "supplier_selection.channel_missing" };
  }
  if (SUPPLIER_TERMINAL_STATES.has(match.channel.state)) {
    return { ok: false, code: "supplier_selection.channel_terminal" };
  }
  const currentUrl = String(tab.url || "");
  const currentOfferId = supplierOfferId(currentUrl);
  if (!currentOfferId || !isSupplierTab(tab)) {
    return { ok: false, code: "supplier_selection.detail_page_required" };
  }
  const product = supplierProduct && typeof supplierProduct === "object"
    ? { ...supplierProduct }
    : {};
  const reportedOfferIds = [
    supplierOfferId(product.supplier_url),
    supplierOfferId(product.final_url),
    String(product.offer_id || product.supplier_product_id || "").trim(),
  ].filter(Boolean);
  if (
    !reportedOfferIds.length
    || reportedOfferIds.some((offerId) => offerId !== currentOfferId)
  ) {
    return { ok: false, code: "supplier_selection.offer_identity_mismatch" };
  }
  product.supplier_url = currentUrl;
  product.final_url = currentUrl;
  product.offer_id = currentOfferId;
  const result = await postJson(match.channel.capture_url, {
    channel_index: match.channel.channel_index,
    seed_id: match.channel.seed_id,
    ozon_product_id: match.channel.ozon_product_id,
    dispatch_token: match.channel.dispatch_token,
    supplier_product: product,
  });
  if (!result || result.ok === false) {
    return {
      ok: false,
      result,
      code: result && result.code
        ? result.code
        : "supplier_selection.capture_failed",
    };
  }
  await markSupplierChannelTerminal(tab.id, "collected");
  return { ok: true, result };
}

async function handleManagedSupplierWindowRemoved(windowId) {
  if (!Number.isInteger(windowId)) return { stopped: false };
  const openedTasks = await loadOpenedTasks();
  const matches = Object.entries(openedTasks).filter(([, entry]) => (
    entry && entry.windowId === windowId && Array.isArray(entry.channels)
  ));
  if (!matches.length) return { stopped: false };
  let stopped = false;
  for (const [key, entry] of matches) {
    entry.pendingNavigation = null;
    entry.pendingNavigations = [];
    if (entry.closingByExtension) {
      entry.windowId = null;
      entry.closedByUser = false;
      entry.closedAt = new Date().toISOString();
      entry.launchState = "closed";
      openedTasks[key] = entry;
      continue;
    }
    const runId = key.split(":", 1)[0];
    entry.windowId = null;
    entry.channels = entry.channels.map((channel) => ({ ...channel, tabId: null }));
    entry.closedByUser = true;
    entry.closedAt = new Date().toISOString();
    entry.launchState = "user_closed";
    entry.closedDispatchToken = entry.dispatchToken || null;
    openedTasks[key] = entry;
    stopped = true;
    try {
      await postJson(`/api/batches/${encodeURIComponent(runId)}/runner/stop`, {});
    } catch (_) {
      // The persisted close marker still suppresses automatic reopen.
    }
    await heartbeat({
      source: "background_window_removed",
      run_id: runId,
      task_type: "supplier_selection",
      stage: "stopped_by_user",
      code: "browser_task.cancelled",
      message: "The managed 1688 window was closed by the user; collection will remain stopped until resumed.",
      url: "",
    });
  }
  await saveOpenedTasks(openedTasks);
  return { stopped };
}

async function existingTab(tabId) {
  if (!tabId) return null;
  try {
    return await chrome.tabs.get(tabId);
  } catch (_) {
    return null;
  }
}

async function managedWindowTabs(windowId) {
  if (!Number.isInteger(windowId)) return [];
  try {
    const tabs = await chrome.tabs.query({ windowId });
    return (Array.isArray(tabs) ? tabs : []).filter((tab) => tab && tab.windowId === windowId);
  } catch (_) {
    return [];
  }
}

async function waitForManagedWindowTabs(windowId, expectedCount, initialTabs = []) {
  const tabsById = new Map();
  const remember = (tabs) => {
    for (const tab of Array.isArray(tabs) ? tabs : []) {
      if (tab && Number.isInteger(tab.id) && tab.windowId === windowId) tabsById.set(tab.id, tab);
    }
  };
  remember(initialTabs);
  for (let attempt = 0; attempt < MANAGED_TAB_QUERY_ATTEMPTS; attempt += 1) {
    remember(await managedWindowTabs(windowId));
    if (tabsById.size >= expectedCount) break;
    if (attempt + 1 < MANAGED_TAB_QUERY_ATTEMPTS) {
      await new Promise((resolve) => setTimeout(resolve, MANAGED_TAB_QUERY_DELAY_MS));
    }
  }
  return [...tabsById.values()].slice(0, expectedCount);
}

function sameSupplierChannel(channel, item, fallbackIndex) {
  return !!channel
    && channel.channel_index === (Number.isInteger(item.channel_index) ? item.channel_index : fallbackIndex)
    && String(channel.seed_id || "") === String(item.seed_id || "")
    && String(channel.ozon_product_id || "") === String(item.ozon_product_id || "");
}

async function recoverActiveManagedSupplierTab(openedTasks, key, entry, items, windowTabs) {
  const channels = Array.isArray(entry.channels) ? entry.channels : [];
  const boundTabIds = new Set(channels.map((channel) => channel.tabId).filter(Number.isInteger));
  const protectedTabIds = new Set(Array.isArray(entry.protectedTabIds) ? entry.protectedTabIds : []);
  const activeOrphans = windowTabs.filter((tab) => (
    tab
    && tab.active === true
    && isSupplierTab(tab)
    && !boundTabIds.has(tab.id)
    && !protectedTabIds.has(tab.id)
  ));
  const intent = entry.pendingNavigation;
  if (!intent || Number(intent.expiresAt || 0) < Date.now() || activeOrphans.length !== 1) return null;
  const recoverableChannel = channels.find((channel) => (
    channel.channel_index === intent.channel_index
    && String(channel.seed_id || "") === String(intent.seed_id || "")
    && String(channel.ozon_product_id || "") === String(intent.ozon_product_id || "")
    && !SUPPLIER_TERMINAL_STATES.has(channel.state)
  ));
  if (!recoverableChannel) return null;
  await adoptSupplierTab({ key, entry, channel: recoverableChannel, openedTasks }, activeOrphans[0]);
  return activeOrphans[0];
}

function managedSupplierChannels(task, items, dispatchToken, tabs, windowId, previousChannels = []) {
  return items.map((item, index) => {
    const previous = previousChannels.find((channel) => sameSupplierChannel(channel, item, index)) || {};
    const tab = tabs[index] || null;
    return {
      ...previous,
      run_id: task.data.run_id,
      task_type: task.data.task_type,
      dispatch_token: dispatchToken,
      capture_url: task.data.capture_url,
      reject_url: task.data.reject_url,
      channel_index: Number.isInteger(item.channel_index) ? item.channel_index : index,
      seed_id: String(item.seed_id || ""),
      ozon_product_id: String(item.ozon_product_id || ""),
      ozon_title: String(item.ozon_title || ""),
      ozon_url: String(item.ozon_url || ""),
      reference_image_url: String(item.reference_image_url || ""),
      selected_options: item.selected_options || {},
      dimension_evidence: item.dimension_evidence || {},
      tabId: tab && Number.isInteger(tab.id) ? tab.id : null,
      windowId,
      state: previous.state || "waiting_user",
    };
  });
}

async function liveManagedSupplierEntry(openedTasks, excludedKey = "") {
  for (const [entryKey, entry] of Object.entries(openedTasks)) {
    if (entryKey === excludedKey || !entry || !Array.isArray(entry.channels)) continue;
    if (!Number.isInteger(entry.windowId) || entry.closingByExtension) continue;
    const tabs = await managedWindowTabs(entry.windowId);
    if (tabs.length) return { key: entryKey, entry, tabs };
  }
  return null;
}

function isOzonTab(tab) {
  return !!tab && /^https:\/\/(?:www\.)?ozon\.ru\//i.test(tab.url || "");
}

function isSupplierTab(tab) {
  return !!tab && (
    /^https:\/\/(?:[^/]+\.)?1688\.com\//i.test(tab.url || "") ||
    /^https:\/\/login\.taobao\.com\//i.test(tab.url || "")
  );
}

function tabMatchesTask(tab, task) {
  return isSupplierTask(task) ? isSupplierTab(tab) : isOzonTab(tab);
}

async function findExistingTaskTab(task) {
  const urls = isSupplierTask(task)
    ? ["https://www.1688.com/*", "https://*.1688.com/*", "https://login.taobao.com/*"]
    : ["https://www.ozon.ru/*", "https://ozon.ru/*"];
  const tabs = await chrome.tabs.query({ url: urls });
  return tabs[0] || null;
}

function contentScriptFiles(task) {
  return isSupplierTask(task)
    ? ["supplier_content.js"]
    : ["seller_evidence.js", "product_evidence.js", "content.js"];
}

async function openManagedSupplierTask(task, source, openedTasks, key, previous, dispatchToken) {
  const items = ((((task || {}).data || {}).contract || {}).items || [])
    .filter(Boolean)
    .slice(0, MAX_SUPPLIER_LANES);
  if (!items.length) return { opened: false, code: "browser_task.supplier_channels_missing" };

  const previousChannels = Array.isArray(previous.channels) ? previous.channels : [];
  const sameDispatch = previous.dispatchToken === dispatchToken;
  const sameChannelContract = previousChannels.length === items.length
    && items.every((item, index) => (
      previousChannels.some((channel) => sameSupplierChannel(channel, item, index))
    ));
  const sameRound = sameDispatch && sameChannelContract;
  const existingWindowTabs = await managedWindowTabs(previous.windowId);
  if (existingWindowTabs.length) {
    await recoverActiveManagedSupplierTab(openedTasks, key, previous, items, existingWindowTabs);
    const tabsById = new Map(existingWindowTabs.map((tab) => [tab.id, tab]));
    const alignedTabs = items.map((item, index) => {
      const matched = previousChannels.find((channel) => sameSupplierChannel(channel, item, index));
      return matched && Number.isInteger(matched.tabId) ? tabsById.get(matched.tabId) || null : null;
    });
    let missingIndexes = alignedTabs
      .map((tab, index) => (tab ? -1 : index))
      .filter((index) => index >= 0);
    const assignedTabIds = new Set(alignedTabs.filter(Boolean).map((tab) => tab.id));
    const recyclableTabs = previousChannels
      .map((channel) => ({ channel, tab: tabsById.get(channel.tabId) || null }))
      .filter(({ tab }) => tab && !assignedTabIds.has(tab.id));
    if (!sameRound) {
      for (const index of missingIndexes) {
        const item = items[index];
        const recyclableIndex = recyclableTabs.findIndex(({ channel }) => (
          channel.channel_index === (Number.isInteger(item.channel_index) ? item.channel_index : index)
        ));
        const fallbackIndex = recyclableIndex >= 0 ? recyclableIndex : (recyclableTabs.length ? 0 : -1);
        const recyclable = fallbackIndex >= 0 ? recyclableTabs.splice(fallbackIndex, 1)[0] : null;
        try {
          alignedTabs[index] = recyclable
            ? await chrome.tabs.update(recyclable.tab.id, {
              url: "https://www.1688.com/",
              active: false,
            })
            : await chrome.tabs.create({
              windowId: previous.windowId,
              url: "https://www.1688.com/",
              active: false,
            });
        } catch (_) {
          alignedTabs[index] = null;
        }
      }
      missingIndexes = alignedTabs
        .map((tab, index) => (tab ? -1 : index))
        .filter((index) => index >= 0);
    }
    const channels = managedSupplierChannels(
      task,
      items,
      dispatchToken,
      alignedTabs,
      previous.windowId,
      previousChannels,
    );
    openedTasks[key] = {
      ...previous,
      windowId: previous.windowId,
      channels,
      extensionVersion: EXTENSION_VERSION,
      dispatchToken,
      launchState: missingIndexes.length ? "incomplete" : "opened",
      closingByExtension: false,
      closedByUser: false,
      pendingNavigation: sameRound ? previous.pendingNavigation || null : null,
      pendingNavigations: [],
      lastUpdatedAt: new Date().toISOString(),
    };
    await saveOpenedTasks(openedTasks);
    for (const { tab } of recyclableTabs) {
      try {
        await chrome.tabs.remove(tab.id);
      } catch (_) {
        // The stale managed lane may already be gone.
      }
    }
    for (const tab of alignedTabs.filter(Boolean)) await ensureContentScript(tab, task);
    return {
      opened: false,
      reused: true,
      waiting: sameRound && missingIndexes.length > 0,
      partial: missingIndexes.length > 0,
      windowId: previous.windowId,
      tabIds: alignedTabs.filter(Boolean).map((tab) => tab.id),
    };
  }

  if (sameDispatch && previous.launchAttemptedAt) {
    return {
      opened: false,
      reused: true,
      waiting: true,
      code: "browser_task.managed_window_launch_already_attempted",
    };
  }

  const busy = await liveManagedSupplierEntry(openedTasks, key);
  if (busy) {
    return {
      opened: false,
      reused: true,
      waiting: true,
      code: "browser_task.managed_window_busy",
      windowId: busy.entry.windowId,
    };
  }

  const launchAttemptedAt = new Date().toISOString();
  openedTasks[key] = {
    windowId: null,
    channels: managedSupplierChannels(task, items, dispatchToken, [], null),
    openedAt: launchAttemptedAt,
    launchAttemptedAt,
    launchState: "opening",
    targetUrl: "https://www.1688.com/",
    extensionVersion: EXTENSION_VERSION,
    dispatchToken,
    closingByExtension: false,
    closedByUser: false,
    pendingNavigation: null,
    pendingNavigations: [],
  };
  await saveOpenedTasks(openedTasks);

  let firstWindow = null;
  try {
    firstWindow = await chrome.windows.create({
      url: "https://www.1688.com/",
      focused: true,
      type: "normal",
    });
  } catch (error) {
    openedTasks[key].launchState = "failed";
    openedTasks[key].lastLaunchError = error && error.message ? error.message : String(error);
    await saveOpenedTasks(openedTasks);
    await stopRunUntilExplicitResume(task.data.run_id);
    await heartbeat({
      source,
      run_id: task.data.run_id,
      task_type: task.data.task_type,
      stage: "managed_supplier_window_failed",
      code: "browser_task.managed_window_create_failed",
      message: openedTasks[key].lastLaunchError,
      url: "https://www.1688.com/",
    });
    return { opened: false, reused: true, waiting: true, code: "browser_task.managed_window_create_failed" };
  }

  if (!firstWindow || !Number.isInteger(firstWindow.id)) {
    openedTasks[key].launchState = "failed";
    openedTasks[key].lastLaunchError = "Managed 1688 window was created without a window id.";
    await saveOpenedTasks(openedTasks);
    await stopRunUntilExplicitResume(task.data.run_id);
    return { opened: false, reused: true, waiting: true, code: "browser_task.managed_window_id_missing" };
  }

  openedTasks[key].windowId = firstWindow.id;
  openedTasks[key].channels = openedTasks[key].channels.map((channel) => ({
    ...channel,
    windowId: firstWindow.id,
  }));
  await saveOpenedTasks(openedTasks);

  const initialTabs = await waitForManagedWindowTabs(firstWindow.id, 1, firstWindow.tabs || []);
  for (let index = initialTabs.length; index < items.length; index += 1) {
    try {
      const tab = await chrome.tabs.create({
        windowId: firstWindow.id,
        url: "https://www.1688.com/",
        active: false,
      });
      if (tab) initialTabs.push(tab);
    } catch (_) {
      break;
    }
  }
  const tabs = await waitForManagedWindowTabs(firstWindow.id, items.length, initialTabs);
  const channels = managedSupplierChannels(task, items, dispatchToken, tabs, firstWindow.id);
  openedTasks[key] = {
    ...openedTasks[key],
    windowId: firstWindow.id,
    channels,
    launchState: tabs.length >= items.length ? "opened" : "incomplete",
    lastUpdatedAt: new Date().toISOString(),
  };
  await saveOpenedTasks(openedTasks);
  if (tabs.length < items.length) await stopRunUntilExplicitResume(task.data.run_id);
  for (const tab of tabs) await ensureContentScript(tab, task);
  await heartbeat({
    source,
    run_id: task.data.run_id,
    task_type: task.data.task_type,
    stage: tabs.length >= items.length ? "managed_supplier_window_opened" : "managed_supplier_window_incomplete",
    code: task.code,
    message: tabs.length >= items.length
      ? `A dedicated 1688 window opened with ${channels.length} bound product channels.`
      : `The managed 1688 window opened, but only ${tabs.length} of ${items.length} lane tabs became available. It will not be opened again automatically.`,
    url: "https://www.1688.com/",
  });
  return {
    opened: true,
    partial: tabs.length < items.length,
    windowId: firstWindow.id,
    tabIds: tabs.map((tab) => tab.id),
  };
}

async function ensureContentScript(tab, task) {
  if (!tab || !tab.id || !tabMatchesTask(tab, task)) return false;
  try {
    const response = await chrome.tabs.sendMessage(tab.id, { type: "ozon_v2_content_ping" });
    if (response && response.ok) {
      if (isManagedSupplierTask(task)) {
        try {
          await chrome.tabs.sendMessage(tab.id, { type: "ozon_v2_supplier_channel_refresh" });
        } catch (_) {
          // A navigation may finish between ping and refresh; onUpdated will retry.
        }
      }
      return true;
    }
  } catch (_) {
    // A freshly reloaded unpacked extension does not inject into an already open page.
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: contentScriptFiles(task),
    });
    return true;
  } catch (error) {
    await heartbeat({
      source: "background_content_injection",
      stage: "content_injection_failed",
      code: "browser_bridge.content_injection_failed",
      message: error && error.message ? error.message : String(error),
      url: tab.url || "",
    });
    return false;
  }
}

async function triggerContentTask(tab) {
  return await chrome.tabs.sendMessage(tab.id, { type: "ozon_v2_run_task" });
}

function openTask(task, source, options = {}) {
  const operation = openTaskQueue.then(() => performOpenTask(task, source, options));
  openTaskQueue = operation.catch(() => null);
  return operation;
}

async function performOpenTask(task, source, options = {}) {
  if (!isRunnableTask(task)) {
    await heartbeat({
      source,
      code: task ? task.code : "browser_task.unavailable",
      message: task ? task.message : "No task response.",
      stage: "idle",
    });
    return { opened: false, code: task ? task.code : "browser_task.unavailable" };
  }
  await chrome.storage.local.set({
    currentRunId: task.data.run_id || "",
    currentTask: task,
  });
  const key = taskKey(task);
  const openedTasks = await loadOpenedTasks();
  const previous = openedTasks[key] || {};
  const url = taskTargetUrl(task);
  const dispatchToken = taskDispatchToken(task);
  if (previous.closedByUser && previous.closedDispatchToken === dispatchToken) {
    return { opened: false, stopped: true, code: "browser_task.user_closed_tab" };
  }
  if (previous.closedByUser) {
    delete previous.closedByUser;
    delete previous.closedAt;
    delete previous.closedDispatchToken;
    openedTasks[key] = previous;
    await saveOpenedTasks(openedTasks);
  }
  if (isManagedSupplierTask(task)) {
    return await openManagedSupplierTask(task, source, openedTasks, key, previous, dispatchToken);
  }
  const mappedTabCandidate = await existingTab(previous.tabId);
  const mappedTab = tabMatchesTask(mappedTabCandidate, task) ? mappedTabCandidate : null;
  if (mappedTab) {
    const versionChanged = previous.extensionVersion !== EXTENSION_VERSION;
    openedTasks[key] = {
      tabId: mappedTab.id,
      openedAt: previous.openedAt || new Date().toISOString(),
      targetUrl: url,
      extensionVersion: EXTENSION_VERSION,
      dispatchToken: versionChanged ? null : previous.dispatchToken || null,
    };
    await saveOpenedTasks(openedTasks);
    if (versionChanged) {
      if (mappedTab.url === url) {
        await chrome.tabs.reload(mappedTab.id);
      } else {
        await chrome.tabs.update(mappedTab.id, { url, active: true });
      }
    } else {
      const currentTab = await existingTab(mappedTab.id);
      const ready = await ensureContentScript(currentTab, task);
      const hasNewDispatch = previous.dispatchToken !== dispatchToken;
      if (ready && hasNewDispatch) {
        const response = await triggerContentTask(currentTab);
        const result = response && response.result ? response.result : null;
        const accepted = response == null || (response.ok && (!result || result.ok !== false));
        if (accepted) openedTasks[key].dispatchToken = dispatchToken;
        openedTasks[key].dispatchOutcome = accepted ? "accepted" : "failed";
        await saveOpenedTasks(openedTasks);
        if (!accepted) {
          await heartbeat({
            source: "background_task_dispatch",
            run_id: task.data.run_id,
            task_type: task.data.task_type,
            stage: "task_failed",
            code: result && result.code ? result.code : "browser_bridge.content_task_failed",
            message: result && result.message
              ? result.message
              : "The browser content task finished without submitting a valid result.",
            details: {
              missing_fields: result && Array.isArray(result.missing_fields) ? result.missing_fields : [],
            },
            url: currentTab.url || url,
          });
        }
      }
    }
    if (versionChanged) {
      await heartbeat({
        source,
        run_id: task.data.run_id,
        task_type: task.data.task_type,
        stage: "tab_version_refreshed",
        code: task.code,
        message: "The controlled browser tab was refreshed once after an extension version change.",
        url: mappedTab.url || url,
      });
    }
    return { opened: false, reused: true, refreshed: versionChanged, tabId: mappedTab.id };
  }
  const reusableTab = await findExistingTaskTab(task);
  if (reusableTab) {
    openedTasks[key] = {
      tabId: reusableTab.id,
      openedAt: new Date().toISOString(),
      targetUrl: url,
      extensionVersion: EXTENSION_VERSION,
      dispatchToken: null,
    };
    await saveOpenedTasks(openedTasks);
    if (options.allowNavigate) {
      await chrome.tabs.update(reusableTab.id, { url, active: true });
    } else {
      await ensureContentScript(reusableTab, task);
    }
    await heartbeat({
      source,
      run_id: task.data.run_id,
      task_type: task.data.task_type,
      stage: options.allowNavigate ? "tab_navigated" : "tab_reused",
      code: task.code,
      message: options.allowNavigate
        ? "An existing browser tab was assigned and navigated once for this task."
        : "An existing browser tab was assigned to this task.",
      url,
    });
    return { opened: false, reused: true, tabId: reusableTab.id };
  }
  if (!options.allowCreate) {
    await heartbeat({
      source,
      run_id: task.data.run_id,
      task_type: task.data.task_type,
      stage: "waiting_existing_task_tab",
      code: task.code,
      message: "Browser task is ready; background polling will not open a new tab automatically.",
      url,
    });
    return { opened: false, waiting: true };
  }
  const createOptions = { url, active: true };
  if (Number.isInteger(options.windowId)) createOptions.windowId = options.windowId;
  const tab = await chrome.tabs.create(createOptions);
  openedTasks[key] = {
    tabId: tab.id,
    openedAt: new Date().toISOString(),
    targetUrl: url,
    extensionVersion: EXTENSION_VERSION,
    dispatchToken: null,
  };
  await saveOpenedTasks(openedTasks);
  await heartbeat({
    source,
    run_id: task.data.run_id,
    task_type: task.data.task_type,
    stage: "tab_opened",
    code: task.code,
    message: "Browser bridge tab opened for active task.",
    url,
  });
  return { opened: true, tabId: tab.id };
}

async function pollTask(source = "background_poll", options = {}) {
  await heartbeat({
    source,
    connection_only: true,
  });
  try {
    const task = await activeTask();
    return await openTask(task, source, {
      allowCreate: true,
      allowNavigate: true,
      ...options,
    });
  } catch (error) {
    await heartbeat({
      source,
      stage: "poll_failed",
      code: "browser_bridge.poll_failed",
      message: error && error.message ? error.message : String(error),
    });
    return { opened: false, error: String(error) };
  }
}

function startPolling() {
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: 1 });
  if (pollingStarted) return;
  pollingStarted = true;
  setInterval(() => pollTask("background_interval"), 5000);
}

chrome.runtime.onInstalled.addListener(() => {
  startPolling();
  pollTask("installed");
});

chrome.runtime.onStartup.addListener(() => {
  startPolling();
  pollTask("startup");
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) {
    pollTask("alarm");
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  const operation = openTaskQueue.then(() => handleTaskTabRemoved(tabId));
  openTaskQueue = operation.catch(() => null);
});

if (chrome.tabs.onCreated) {
  chrome.tabs.onCreated.addListener((tab) => {
    const operation = openTaskQueue.then(() => handleSupplierTabCreated(tab));
    openTaskQueue = operation.catch(() => null);
  });
}

if (chrome.tabs.onUpdated) {
  chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    const operation = openTaskQueue.then(() => handleSupplierTabUpdated(tabId, changeInfo, tab));
    openTaskQueue = operation.catch(() => null);
  });
}

if (chrome.tabs.onActivated) {
  chrome.tabs.onActivated.addListener((activeInfo) => {
    const operation = openTaskQueue.then(() => handleSupplierTabActivated(activeInfo));
    openTaskQueue = operation.catch(() => null);
  });
}

if (chrome.windows && chrome.windows.onRemoved) {
  chrome.windows.onRemoved.addListener((windowId) => {
    const operation = openTaskQueue.then(() => handleManagedSupplierWindowRemoved(windowId));
    openTaskQueue = operation.catch(() => null);
  });
}

chrome.action.onClicked.addListener(async () => {
  const result = await pollTask("action_click", { allowCreate: true, allowNavigate: true });
  if (!result.opened && !result.reused) {
    await chrome.tabs.create({ url: `${BASE_URL}/` });
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message) return false;
  const senderWindowId = sender && sender.tab && Number.isInteger(sender.tab.windowId)
    ? sender.tab.windowId
    : null;
  if (message.type === "ozon_v2_set_workbench_auth") {
    const senderUrl = String((sender && sender.url) || (sender && sender.tab && sender.tab.url) || "");
    const token = String(message.token || "");
    if (!/^http:\/\/(?:127\.0\.0\.1|localhost):8765\//i.test(senderUrl) || token.length < 32) {
      sendResponse({ ok: false, error: "Workbench authentication bootstrap was rejected." });
      return false;
    }
    chrome.storage.local.set({ [WORKBENCH_AUTH_STORAGE_KEY]: token })
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_local_api_request") {
    const request = message.request && typeof message.request === "object" ? message.request : {};
    const path = String(request.path || "");
    const method = String(request.method || "GET").toUpperCase();
    if (!path.startsWith("/api/") || !["GET", "POST"].includes(method)) {
      sendResponse({ transport_ok: false, error: "Unsupported local API request." });
      return false;
    }
    workbenchJson(path, {
      method,
      body: method === "POST" ? String(request.body || "{}") : undefined,
    })
      .then((body) => sendResponse({ transport_ok: true, body }))
      .catch((error) => sendResponse({ transport_ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_content_task_failed") {
    const tabId = sender && sender.tab && Number.isInteger(sender.tab.id)
      ? sender.tab.id
      : null;
    loadOpenedTasks()
      .then(async (openedTasks) => {
        let reset = false;
        for (const [key, entry] of Object.entries(openedTasks)) {
          if (!entry || entry.tabId !== tabId) continue;
          openedTasks[key] = {
            ...entry,
            dispatchToken: null,
            dispatchOutcome: "failed",
            lastDispatchError: String(message.error || ""),
          };
          reset = true;
        }
        if (reset) await saveOpenedTasks(openedTasks);
        return { reset };
      })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_task_ready") {
    openTask(message.task, "workbench_content_script", {
      allowCreate: true,
      allowNavigate: true,
      windowId: senderWindowId,
    })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_current_task") {
    const runId = message.task && message.task.data ? message.task.data.run_id : "";
    chrome.storage.local.set({ currentRunId: runId || "", currentTask: message.task || null })
      .then(() => openTask(message.task, "workbench_content_script", {
        allowCreate: true,
        allowNavigate: true,
        windowId: senderWindowId,
      }))
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_get_current_task") {
    currentWorkbenchTask()
      .then((task) => sendResponse({ ok: true, task }))
    .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_get_supplier_channel") {
    const senderTab = sender && sender.tab ? sender.tab : {};
    resolveSupplierChannelForTab(senderTab)
      .then((binding) => sendResponse({
        ok: true,
        binding,
        diagnostics: binding ? null : {
          tab_id: Number.isInteger(senderTab.id) ? senderTab.id : null,
          window_id: Number.isInteger(senderTab.windowId) ? senderTab.windowId : null,
          opener_tab_id: Number.isInteger(senderTab.openerTabId) ? senderTab.openerTabId : null,
          url: String(senderTab.url || ""),
        },
      }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_supplier_native_new_tab_intent") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    recordSupplierNativeNewTabIntent(tabId, message.url)
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_supplier_navigation_intent") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    const operation = supplierNavigationQueue.then(() => recordSupplierNavigationIntent(tabId));
    supplierNavigationQueue = operation.catch(() => null);
    operation
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_supplier_channel_back") {
    const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
    supplierChannelForTab(tabId)
      .then(async (binding) => {
        if (!binding) return { ok: false, code: "supplier_selection.channel_missing" };
        try {
          await chrome.tabs.goBack(tabId);
          return { ok: true, mode: "history" };
        } catch (_) {
          await chrome.tabs.update(tabId, { url: "https://www.1688.com/", active: true });
          return { ok: true, mode: "home" };
        }
      })
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_supplier_channel_terminal") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    markSupplierChannelTerminal(tabId, String(message.state || ""))
      .then((result) => sendResponse({ ok: result.ok === true, result }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_supplier_navigation_release") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    const operation = supplierNavigationQueue.then(() => releaseSupplierNavigationIntent(tabId));
    supplierNavigationQueue = operation.catch(() => null);
    operation
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_capture_supplier_channel") {
    const senderTab = sender && sender.tab ? sender.tab : null;
    captureSupplierSelectionFromTab(senderTab, message.supplier_product)
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_reject_supplier_channel") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    supplierChannelForTab(tabId)
      .then(async (binding) => {
        if (!binding) return { ok: false, code: "supplier_selection.channel_missing" };
        const result = await postJson(binding.reject_url, {
          seed_id: binding.seed_id,
          reason: "User could not find an exact 1688 supplier in the managed selection channel.",
        });
        if (result && result.ok !== false) {
          await markSupplierChannelTerminal(tabId, "user_skipped", {
            forceClose: true,
            closeReason: "round_replaced",
          });
        }
        return result;
      })
      .then((result) => sendResponse({ ok: !!result && result.ok !== false, result }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === "ozon_v2_fetch_reference_image") {
    const value = String(message.url || "");
    let referenceUrl = null;
    try {
      referenceUrl = new URL(value);
    } catch (_) {
      referenceUrl = null;
    }
    const trustedReferenceHost = referenceUrl
      && referenceUrl.protocol === "https:"
      && [".ozone.ru", ".ozon.ru"].some((suffix) => referenceUrl.hostname.toLowerCase().endsWith(suffix));
    if (!trustedReferenceHost) {
      sendResponse({ ok: false, error: "An approved Ozon CDN reference image URL is required." });
      return false;
    }
    fetch(value)
      .then(async (response) => {
        if (!response.ok) throw new Error(`Reference image request failed: ${response.status}`);
        const finalUrl = new URL(response.url || value);
        if (
          finalUrl.protocol !== "https:"
          || ![".ozone.ru", ".ozon.ru"].some((suffix) => finalUrl.hostname.toLowerCase().endsWith(suffix))
        ) throw new Error("Reference image redirected outside the approved Ozon CDN.");
        const contentType = response.headers.get("Content-Type") || "";
        if (!contentType.toLowerCase().startsWith("image/")) {
          throw new Error("Reference URL did not return an image.");
        }
        const contentLength = Number(response.headers.get("Content-Length") || 0);
        if (Number.isFinite(contentLength) && contentLength > 20 * 1024 * 1024) {
          throw new Error("Reference image exceeds the 20 MB limit.");
        }
        const buffer = await response.arrayBuffer();
        if (buffer.byteLength > 20 * 1024 * 1024) {
          throw new Error("Reference image exceeds the 20 MB limit.");
        }
        return {
          ok: true,
          contentType,
          bytes: Array.from(new Uint8Array(buffer)),
        };
      })
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  return false;
});

startPolling();
