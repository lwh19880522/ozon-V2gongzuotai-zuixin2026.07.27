"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const listeners = {};
const stored = { openedTasks: {}, currentRunId: "" };
const tabs = new Map();
const windows = new Map();
const removedTabs = [];
const removedWindows = [];
const postedPaths = [];
const postedRequests = [];
const sentMessages = [];
const backCalls = [];
const updatedTabs = [];
const failBackTabs = new Set();
let browserTaskResponse = null;
let nextTabId = 10;
let nextWindowId = 70;

function event(name) {
  return { addListener(listener) { listeners[name] = listener; } };
}

function urlsFrom(value) {
  return Array.isArray(value) ? value : [value];
}

const chrome = {
  storage: { local: {
    async get(defaults) { return { ...defaults, ...stored }; },
    async set(values) { Object.assign(stored, values); },
  } },
  tabs: {
    onCreated: event("tabCreated"),
    onActivated: event("tabActivated"),
    onUpdated: event("tabUpdated"),
    onRemoved: event("tabRemoved"),
    async get(tabId) {
      if (!tabs.has(tabId)) throw new Error("tab not found");
      return tabs.get(tabId);
    },
    async query(queryInfo = {}) {
      const values = [...tabs.values()];
      if (!queryInfo.url) return values;
      return values.filter((tab) => /1688\.com\//i.test(tab.url || ""));
    },
    async create({ url, windowId, active = false }) {
      const tab = { id: nextTabId++, url, windowId, active };
      tabs.set(tab.id, tab);
      return tab;
    },
    async update(tabId, changes) {
      updatedTabs.push({ tabId, changes });
      const tab = { ...tabs.get(tabId), ...changes };
      tabs.set(tabId, tab);
      return tab;
    },
    async reload() {},
    async sendMessage(tabId, message) {
      sentMessages.push({ tabId, message });
      if (message.type === "ozon_v2_content_ping") {
        return { ok: true, bridge: "supplier", panel_present: false };
      }
      return { ok: true };
    },
    async goBack(tabId) {
      backCalls.push(tabId);
      if (failBackTabs.has(tabId)) throw new Error("no history");
    },
    async remove(tabId) {
      removedTabs.push(tabId);
      tabs.delete(tabId);
      if (listeners.tabRemoved) listeners.tabRemoved(tabId);
    },
  },
  windows: {
    onRemoved: event("windowRemoved"),
    async create({ url, focused, type }) {
      const windowId = nextWindowId++;
      // Edge can collapse an array of identical 1688 home URLs into one tab.
      // The bridge must add the remaining lane tabs explicitly in this window.
      const tabUrl = urlsFrom(url)[0];
      const createdTabs = [{ id: nextTabId++, url: tabUrl, windowId, active: true }];
      tabs.set(createdTabs[0].id, createdTabs[0]);
      const created = { id: windowId, tabs: createdTabs, focused, type };
      windows.set(windowId, created);
      // Edge may finish creating the tabs before Window.tabs is populated.
      // The bridge must recover them with chrome.tabs.query instead of opening again.
      return { ...created, tabs: [] };
    },
    async remove(windowId) {
      removedWindows.push(windowId);
      windows.delete(windowId);
      for (const [tabId, tab] of [...tabs.entries()]) {
        if (tab.windowId === windowId) tabs.delete(tabId);
      }
      if (listeners.windowRemoved) listeners.windowRemoved(windowId);
    },
  },
  scripting: { async executeScript() {} },
  alarms: { create() {}, onAlarm: event("alarm") },
  runtime: {
    getManifest() { return { version: "0.1.45-test" }; },
    onInstalled: event("installed"),
    onStartup: event("startup"),
    onMessage: event("message"),
  },
  action: { onClicked: event("action") },
};

const context = vm.createContext({
  chrome,
  console,
  fetch: async (url, options = {}) => {
    if (options.method === "POST") {
      postedPaths.push(String(url));
      postedRequests.push({
        url: String(url),
        body: options.body ? JSON.parse(options.body) : null,
      });
    }
    const response = !options.method && String(url).includes("/browser-task") && browserTaskResponse
      ? browserTaskResponse
      : { ok: true, code: "ok", data: {} };
    return { ok: true, json: async () => response };
  },
  setInterval() { return 1; },
  setTimeout,
  clearTimeout,
  encodeURIComponent,
  Date,
  Promise,
});
const backgroundPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "background_v2.js");
vm.runInContext(fs.readFileSync(backgroundPath, "utf8"), context, { filename: backgroundPath });

function supplierTask(itemCount, token = "managed-token") {
  return {
    ok: true,
    code: "browser_task.supplier_selection_ready",
    message: "ready",
    data: {
      run_id: "wb-managed-round",
      created_at: "2026-07-15T04:00:00+00:00",
      task_type: "supplier_selection",
      dispatch_token: token,
      capture_url: "/api/batches/wb-managed-round/supplier-selection/capture",
      reject_url: "/api/batches/wb-managed-round/supplier-review/reject",
      contract: {
        items: Array.from({ length: itemCount }, (_, index) => ({
          channel_index: index,
          seed_id: `seed-${index + 1}`,
          ozon_product_id: `ozon-${index + 1}`,
          reference_image_url: `https://ir.ozone.ru/${index + 1}.jpg`,
        })),
      },
    },
  };
}

function sendMessage(message, tab) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`message response timeout: ${message.type}`)), 1000);
    listeners.message(message, { tab }, (response) => {
      clearTimeout(timeout);
      resolve(response);
    });
  });
}

(async () => {
  const task = supplierTask(7);
  const result = await context.performOpenTask(task, "managed_round_test", { allowCreate: true });
  assert.equal(result.opened, true);
  assert.equal(windows.size, 1, "one managed window must be created per round");
  let entry = stored.openedTasks["wb-managed-round:supplier_selection"];
  assert.equal(entry.channels.length, 5, "one managed round must be capped at five lanes");
  assert.equal(tabs.size, 5, "all five lane tabs must be created atomically in the managed window");
  assert.ok(entry.channels.every((channel) => channel.state === "waiting_user"));

  const firstChannel = entry.channels[0];
  const originalTabId = firstChannel.tabId;
  const intent = await sendMessage(
    {
      type: "ozon_v2_supplier_native_new_tab_intent",
      url: "https://detail.1688.com/offer/999999999999.html",
    },
    tabs.get(originalTabId),
  );
  assert.equal(intent.ok, true, "a bound channel may mark one explicit native new-tab intent");
  const explicitNativeTab = {
    id: 199,
    windowId: entry.windowId,
    openerTabId: originalTabId,
    url: "https://detail.1688.com/offer/999999999999.html",
    active: false,
  };
  tabs.set(explicitNativeTab.id, explicitNativeTab);
  const skipped = await context.handleSupplierTabCreated(explicitNativeTab);
  assert.deepEqual(
    JSON.parse(JSON.stringify(skipped)),
    { adopted: false, nativeIntent: true },
    "an explicit Ctrl/middle-click tab must never be adopted",
  );
  assert.equal(firstChannel.tabId, originalTabId, "native new-tab intent must not rebind its channel");
  assert.equal(tabs.has(originalTabId), true, "native new-tab intent must not close the managed lane");
  tabs.delete(explicitNativeTab.id);

  const searchResultTab = {
    id: 200,
    windowId: entry.windowId,
    openerTabId: originalTabId,
    url: "https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch",
    active: true,
  };
  tabs.set(searchResultTab.id, searchResultTab);
  assert.equal(typeof context.handleSupplierTabCreated, "function", "managed child-tab adoption must be implemented");
  await context.handleSupplierTabCreated(searchResultTab);
  assert.equal(entry.channels[0].tabId, searchResultTab.id, "the image-search result must inherit the lane binding");
  assert.ok(removedTabs.includes(originalTabId), "the previous lane page must close after the search result is adopted");

  const detailTab = {
    id: 201,
    windowId: entry.windowId,
    openerTabId: searchResultTab.id,
    url: "https://detail.1688.com/offer/123456789012.html",
    active: true,
  };
  tabs.set(detailTab.id, detailTab);
  sentMessages.length = 0;
  await context.handleSupplierTabCreated(detailTab);
  assert.equal(entry.channels[0].tabId, detailTab.id, "the product detail must keep the same lane binding");
  assert.ok(removedTabs.includes(searchResultTab.id), "the search result page must close after the detail page is adopted");
  assert.equal(tabs.size, 5, "search and detail navigation must not increase the managed lane count");
  assert.equal(postedPaths.some((value) => value.includes("/runner/stop")), false, "lane adoption must not pause the batch");
  assert.ok(sentMessages.some((item) => (
    item.tabId === detailTab.id && item.message.type === "ozon_v2_supplier_channel_refresh"
  )), "an adopted detail tab must refresh its managed panel immediately");

  sentMessages.length = 0;
  await context.handleSupplierTabUpdated(detailTab.id, { url: detailTab.url }, detailTab);
  assert.ok(sentMessages.some((item) => (
    item.tabId === detailTab.id && item.message.type === "ozon_v2_supplier_channel_refresh"
  )), "a bound URL update must reconcile the managed panel");

  const back = await sendMessage({ type: "ozon_v2_supplier_channel_back" }, detailTab);
  assert.equal(back.ok, true);
  assert.equal(back.mode, "history");
  assert.deepEqual(backCalls, [detailTab.id]);

  const fallbackChannel = entry.channels[1];
  const fallbackTab = tabs.get(fallbackChannel.tabId);
  failBackTabs.add(fallbackTab.id);
  const tabCountBeforeFallback = tabs.size;
  const removedCountBeforeFallback = removedTabs.length;
  const fallback = await sendMessage({ type: "ozon_v2_supplier_channel_back" }, fallbackTab);
  assert.equal(fallback.ok, true);
  assert.equal(fallback.mode, "home");
  assert.ok(updatedTabs.some((item) => (
    item.tabId === fallbackTab.id && item.changes.url === "https://www.1688.com/"
  )));
  assert.equal(tabs.size, tabCountBeforeFallback, "home fallback must not create a tab");
  assert.equal(removedTabs.length, removedCountBeforeFallback, "home fallback must not close a channel");

  const channelTabIds = entry.channels.map((channel) => channel.tabId);
  const orphanTab = {
    id: 298,
    windowId: entry.windowId,
    url: "https://detail.1688.com/offer/298298298298.html",
    active: true,
  };
  tabs.set(orphanTab.id, orphanTab);
  const orphanResult = await context.handleSupplierTabCreated(orphanTab);
  assert.deepEqual(JSON.parse(JSON.stringify(orphanResult)), { adopted: false });
  assert.deepEqual(entry.channels.map((channel) => channel.tabId), channelTabIds, "an orphan must never be guessed into a channel");
  const orphanLookup = await sendMessage({ type: "ozon_v2_get_supplier_channel" }, orphanTab);
  assert.equal(orphanLookup.ok, true);
  assert.equal(orphanLookup.binding, null);
  assert.deepEqual(JSON.parse(JSON.stringify(orphanLookup.diagnostics)), {
    tab_id: orphanTab.id,
    window_id: orphanTab.windowId,
    opener_tab_id: null,
    url: orphanTab.url,
  });
  tabs.delete(orphanTab.id);

  const intentChannel = entry.channels[3];
  const intentSourceTab = tabs.get(intentChannel.tabId);
  const navigationIntent = await sendMessage(
    { type: "ozon_v2_supplier_navigation_intent" },
    intentSourceTab,
  );
  assert.equal(navigationIntent.ok, true, "a managed lane must persist its pending navigation ownership");
  const openerlessSearchTab = {
    id: 299,
    windowId: entry.windowId,
    url: "https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch",
    active: true,
  };
  tabs.set(openerlessSearchTab.id, openerlessSearchTab);
  const intentAdoption = await context.handleSupplierTabCreated(openerlessSearchTab);
  assert.equal(intentAdoption.adopted, true, "an opener-less 1688 page must inherit the lane's persisted navigation intent");
  assert.equal(intentChannel.tabId, openerlessSearchTab.id);
  assert.ok(removedTabs.includes(intentSourceTab.id));

  const serializedChannels = [entry.channels[2], entry.channels[4]];
  const serializedSources = serializedChannels.map((channel) => tabs.get(channel.tabId));
  const firstLease = await sendMessage(
    { type: "ozon_v2_supplier_navigation_intent" },
    serializedSources[0],
  );
  assert.equal(firstLease.ok, true);
  const blockedLease = await sendMessage(
    { type: "ozon_v2_supplier_navigation_intent" },
    serializedSources[1],
  );
  assert.equal(blockedLease.ok, false, "only one opener-less image search may be in flight per managed window");
  assert.equal(blockedLease.code, "supplier_selection.navigation_busy");
  const serializedOrphans = serializedChannels.map((channel, index) => ({
    id: 300 + index,
    windowId: entry.windowId,
    url: `https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch&lane=${channel.channel_index}`,
    active: index === 0,
  }));
  tabs.set(serializedOrphans[0].id, serializedOrphans[0]);
  const firstAdoption = await context.handleSupplierTabCreated(serializedOrphans[0]);
  assert.equal(firstAdoption.adopted, true);
  const secondLease = await sendMessage(
    { type: "ozon_v2_supplier_navigation_intent" },
    serializedSources[1],
  );
  assert.equal(secondLease.ok, true, "the next lane may navigate only after the first result is bound");
  tabs.set(serializedOrphans[1].id, serializedOrphans[1]);
  const secondAdoption = await context.handleSupplierTabCreated(serializedOrphans[1]);
  assert.equal(secondAdoption.adopted, true);
  assert.deepEqual(
    serializedChannels.map((channel) => channel.tabId),
    serializedOrphans.map((tab) => tab.id),
    "serialized navigation leases must preserve exact lane ownership",
  );

  browserTaskResponse = task;
  const closedChannelIndex = 2;
  const closedChannel = entry.channels[closedChannelIndex];
  const closedTabId = closedChannel.tabId;
  const healthyTabIds = entry.channels
    .filter((channel) => channel.channel_index !== closedChannelIndex)
    .map((channel) => channel.tabId);
  const stoppedBeforeLaneClose = postedPaths.filter((value) => value.includes("/runner/stop")).length;
  tabs.delete(closedTabId);
  const laneClose = await context.handleTaskTabRemoved(closedTabId);
  assert.equal(laneClose.stopped, false, "closing one managed lane must not report a whole-task stop");
  assert.equal(closedChannel.tabId, null, "only the closed lane must lose its tab binding");
  assert.deepEqual(
    entry.channels
      .filter((channel) => channel.channel_index !== closedChannelIndex)
      .map((channel) => channel.tabId),
    healthyTabIds,
    "closing one lane must preserve every healthy lane binding",
  );
  assert.equal(entry.launchState, "incomplete", "a single closed lane must leave the round recoverable");
  assert.equal(entry.closedByUser, false, "a single closed lane must not mark the whole round user-closed");
  assert.equal(
    postedPaths.filter((value) => value.includes("/runner/stop")).length,
    stoppedBeforeLaneClose,
    "a single closed lane must not pause the whole batch",
  );
  for (const tabId of healthyTabIds) {
    const binding = await context.supplierChannelForTab(tabId);
    assert.ok(binding, `healthy lane ${tabId} must remain eligible for its collection panel`);
  }

  const tabCountAfterLaneClose = tabs.size;
  const sameDispatch = await context.performOpenTask(task, "managed_round_poll", { allowCreate: true });
  assert.equal(sameDispatch.waiting, true, "the same dispatch must wait for an explicit restart");
  assert.equal(tabs.size, tabCountAfterLaneClose, "background polling must not reopen a user-closed lane");

  const restartTask = supplierTask(5, "managed-token-restart");
  browserTaskResponse = restartTask;
  updatedTabs.length = 0;
  const restarted = await context.performOpenTask(restartTask, "managed_round_restart", { allowCreate: true });
  entry = stored.openedTasks["wb-managed-round:supplier_selection"];
  assert.equal(restarted.reused, true);
  assert.equal(tabs.size, tabCountAfterLaneClose + 1, "explicit restart must create exactly one missing lane tab");
  assert.deepEqual(
    entry.channels
      .filter((channel) => channel.channel_index !== closedChannelIndex)
      .map((channel) => channel.tabId),
    healthyTabIds,
    "explicit restart must preserve stable bindings for healthy lanes",
  );
  assert.ok(entry.channels[closedChannelIndex].tabId, "the missing lane must receive a replacement tab");
  assert.equal(updatedTabs.length, 0, "restart must not navigate or reload healthy lane tabs");

  for (const channel of entry.channels.slice(0, 4)) {
    const response = await sendMessage(
      { type: "ozon_v2_supplier_channel_terminal", state: "collected" },
      tabs.get(channel.tabId) || { id: channel.tabId, windowId: entry.windowId },
    );
    assert.equal(response.ok, true);
  }
  const lastChannel = entry.channels[4];
  const terminal = await sendMessage(
    { type: "ozon_v2_supplier_channel_terminal", state: "user_skipped" },
    tabs.get(lastChannel.tabId),
  );
  assert.equal(terminal.ok, true);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(entry.channels.map((channel) => channel.state), [
    "collected", "collected", "collected", "collected", "user_skipped",
  ]);
  assert.equal(removedWindows.length, 1, "the managed window must close after every lane is terminal");
  assert.equal(entry.closedByUser, false, "extension-completed closure must never look like a user cancellation");

  stored.openedTasks = {};
  tabs.clear();
  windows.clear();
  removedWindows.length = 0;
  const selfHealTask = supplierTask(1, "self-heal-token");
  await context.performOpenTask(selfHealTask, "managed_round_test", { allowCreate: true });
  const selfHealEntry = stored.openedTasks["wb-managed-round:supplier_selection"];
  const staleBoundTabId = selfHealEntry.channels[0].tabId;
  const selfHealTab = {
    id: 401,
    windowId: selfHealEntry.windowId,
    url: "https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch",
    active: true,
  };
  tabs.set(selfHealTab.id, selfHealTab);
  const unownedLookup = await sendMessage({ type: "ozon_v2_get_supplier_channel" }, selfHealTab);
  assert.equal(unownedLookup.ok, true);
  assert.equal(unownedLookup.binding, null, "an orphan must never be guessed into the only remaining lane");
  assert.equal(selfHealEntry.channels[0].tabId, staleBoundTabId);
  const selfHealIntent = await sendMessage(
    { type: "ozon_v2_supplier_navigation_intent" },
    tabs.get(staleBoundTabId),
  );
  assert.equal(selfHealIntent.ok, true);
  const recoveredLookup = await sendMessage({ type: "ozon_v2_get_supplier_channel" }, selfHealTab);
  assert.equal(recoveredLookup.binding.seed_id, "seed-1", "a persisted lane lease must self-heal after event loss or extension reload");
  assert.equal(selfHealEntry.channels[0].tabId, selfHealTab.id);
  assert.ok(removedTabs.includes(staleBoundTabId), "self-healing must retire the stale lane page after rebinding");

  stored.openedTasks = {};
  tabs.clear();
  windows.clear();
  removedWindows.length = 0;
  const protectedTask = supplierTask(1, "protected-native-token");
  await context.performOpenTask(protectedTask, "managed_round_test", { allowCreate: true });
  const protectedEntry = stored.openedTasks["wb-managed-round:supplier_selection"];
  const protectedSource = tabs.get(protectedEntry.channels[0].tabId);
  await sendMessage(
    {
      type: "ozon_v2_supplier_native_new_tab_intent",
      url: "https://detail.1688.com/offer/777777777777.html",
    },
    protectedSource,
  );
  const protectedNativeTab = {
    id: 402,
    windowId: protectedEntry.windowId,
    url: "https://detail.1688.com/offer/777777777777.html",
    active: true,
  };
  tabs.set(protectedNativeTab.id, protectedNativeTab);
  const protectedCreation = await context.handleSupplierTabCreated(protectedNativeTab);
  assert.equal(protectedCreation.nativeIntent, true, "an opener-less explicit native tab must be recognized and protected");
  const protectedLookup = await sendMessage({ type: "ozon_v2_get_supplier_channel" }, protectedNativeTab);
  assert.equal(protectedLookup.binding, null, "a protected native tab must never be claimed by unique-lane recovery");
  assert.equal(protectedEntry.channels[0].tabId, protectedSource.id);

  stored.openedTasks = {};
  tabs.clear();
  windows.clear();
  removedWindows.length = 0;
  const reloadRecoveryTask = supplierTask(1, "reload-recovery-token");
  await context.performOpenTask(reloadRecoveryTask, "managed_round_test", { allowCreate: true });
  const reloadEntry = stored.openedTasks["wb-managed-round:supplier_selection"];
  const reloadSourceTab = tabs.get(reloadEntry.channels[0].tabId);
  const reloadIntent = await sendMessage(
    { type: "ozon_v2_supplier_navigation_intent" },
    reloadSourceTab,
  );
  assert.equal(reloadIntent.ok, true);
  reloadSourceTab.active = false;
  const preexistingActiveOrphan = {
    id: 403,
    windowId: reloadEntry.windowId,
    url: "https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch",
    active: true,
  };
  tabs.set(preexistingActiveOrphan.id, preexistingActiveOrphan);
  await context.performOpenTask(reloadRecoveryTask, "extension_reload_poll", { allowCreate: true });
  assert.equal(
    reloadEntry.channels[0].tabId,
    preexistingActiveOrphan.id,
    "polling after extension reload must recover the active opener-less page",
  );
  assert.ok(removedTabs.includes(reloadSourceTab.id));

  stored.openedTasks = {};
  tabs.clear();
  windows.clear();
  removedWindows.length = 0;
  const activationRecoveryTask = supplierTask(1, "activation-recovery-token");
  await context.performOpenTask(activationRecoveryTask, "managed_round_test", { allowCreate: true });
  const activationEntry = stored.openedTasks["wb-managed-round:supplier_selection"];
  const activationSource = tabs.get(activationEntry.channels[0].tabId);
  const activationIntent = await sendMessage(
    { type: "ozon_v2_supplier_navigation_intent" },
    activationSource,
  );
  assert.equal(activationIntent.ok, true);
  activationSource.active = false;
  const activatedOrphan = {
    id: 404,
    windowId: activationEntry.windowId,
    url: "https://detail.1688.com/offer/404404404404.html",
    active: true,
  };
  tabs.set(activatedOrphan.id, activatedOrphan);
  const activationRecovery = await context.handleSupplierTabActivated({
    tabId: activatedOrphan.id,
    windowId: activatedOrphan.windowId,
  });
  assert.equal(activationRecovery.recovered, true, "activating an existing orphan after extension reload must restore its lane");
  assert.equal(activationEntry.channels[0].tabId, activatedOrphan.id);
  assert.ok(removedTabs.includes(activationSource.id));

  stored.openedTasks = {};
  tabs.clear();
  windows.clear();
  removedWindows.length = 0;
  const captureTask = supplierTask(1, "capture-proxy-token");
  await context.performOpenTask(captureTask, "managed_round_test", { allowCreate: true });
  const captureEntry = stored.openedTasks["wb-managed-round:supplier_selection"];
  const captureTab = tabs.get(captureEntry.channels[0].tabId);
  captureTab.url = "https://detail.1688.com/offer/919191919191.html";
  const capturePostCount = postedRequests.length;
  const mismatchedCapture = await sendMessage(
    {
      type: "ozon_v2_capture_supplier_channel",
      supplier_product: {
        supplier_url: "https://detail.1688.com/offer/929292929292.html",
        final_url: "https://detail.1688.com/offer/929292929292.html",
        offer_id: "929292929292",
      },
    },
    captureTab,
  );
  assert.equal(mismatchedCapture.ok, false);
  assert.equal(mismatchedCapture.code, "supplier_selection.offer_identity_mismatch");
  assert.equal(postedRequests.length, capturePostCount, "a mismatched page offer must never be persisted");

  const captured = await sendMessage(
    {
      type: "ozon_v2_capture_supplier_channel",
      channel_index: 999,
      seed_id: "forged-seed",
      ozon_product_id: "forged-ozon",
      dispatch_token: "forged-token",
      supplier_product: {
        supplier_url: captureTab.url,
        final_url: captureTab.url,
        offer_id: "919191919191",
        title: "Exact current supplier product",
      },
    },
    captureTab,
  );
  assert.equal(captured.ok, true, "an exactly bound detail tab must capture through the background");
  assert.equal(postedRequests.length, capturePostCount + 1);
  const trustedCapture = postedRequests.at(-1);
  assert.ok(trustedCapture.url.endsWith("/api/batches/wb-managed-round/supplier-selection/capture"));
  assert.equal(trustedCapture.body.channel_index, 0, "page-supplied channel indexes must be ignored");
  assert.equal(trustedCapture.body.seed_id, "seed-1", "page-supplied seed ids must be ignored");
  assert.equal(trustedCapture.body.ozon_product_id, "ozon-1", "page-supplied Ozon ids must be ignored");
  assert.equal(trustedCapture.body.dispatch_token, "capture-proxy-token", "the stored dispatch generation must be authoritative");
  assert.equal(captureEntry.channels[0].state, "collected", "capture persistence and terminal state must be one background operation");

  const orphanCaptureTab = {
    id: 405,
    windowId: captureEntry.windowId,
    url: "https://detail.1688.com/offer/929292929292.html",
    active: true,
  };
  tabs.set(orphanCaptureTab.id, orphanCaptureTab);
  const orphanCapture = await sendMessage(
    {
      type: "ozon_v2_capture_supplier_channel",
      supplier_product: {
        supplier_url: orphanCaptureTab.url,
        final_url: orphanCaptureTab.url,
        offer_id: "929292929292",
      },
    },
    orphanCaptureTab,
  );
  assert.equal(orphanCapture.ok, false);
  assert.equal(orphanCapture.code, "supplier_selection.channel_missing");
  assert.equal(postedRequests.length, capturePostCount + 1, "an unbound tab must never reach the workbench capture API");

  stored.openedTasks = {};
  tabs.clear();
  windows.clear();
  const sameTabLeaseTask = supplierTask(2, "same-tab-lease-token");
  await context.performOpenTask(sameTabLeaseTask, "managed_round_test", { allowCreate: true });
  const sameTabEntry = stored.openedTasks["wb-managed-round:supplier_selection"];
  const sameTabFirst = tabs.get(sameTabEntry.channels[0].tabId);
  const sameTabSecond = tabs.get(sameTabEntry.channels[1].tabId);
  assert.equal((await sendMessage({ type: "ozon_v2_supplier_navigation_intent" }, sameTabFirst)).ok, true);
  assert.equal((await sendMessage({ type: "ozon_v2_supplier_navigation_intent" }, sameTabSecond)).code, "supplier_selection.navigation_busy");
  await context.handleSupplierTabUpdated(sameTabFirst.id, { status: "loading" }, sameTabFirst);
  assert.equal(
    (await sendMessage({ type: "ozon_v2_supplier_navigation_intent" }, sameTabSecond)).ok,
    true,
    "a successful same-tab image search must release the previous lane immediately",
  );
  const released = await sendMessage({ type: "ozon_v2_supplier_navigation_release" }, sameTabSecond);
  assert.equal(released.ok, true, "an image-recognition failure must be able to release its lane explicitly");
  assert.equal(
    (await sendMessage({ type: "ozon_v2_supplier_navigation_intent" }, sameTabFirst)).ok,
    true,
    "another lane must continue immediately after an explicit recognition-failure release",
  );
  tabs.delete(sameTabFirst.id);
  await context.handleTaskTabRemoved(sameTabFirst.id);
  assert.equal(
    (await sendMessage({ type: "ozon_v2_supplier_navigation_intent" }, sameTabSecond)).ok,
    true,
    "closing the lease-owning lane must never block the remaining channels",
  );

  stored.openedTasks = {};
  tabs.clear();
  windows.clear();
  removedWindows.length = 0;
  const userCloseTask = supplierTask(1, "user-close-token");
  await context.performOpenTask(userCloseTask, "managed_round_test", { allowCreate: true });
  const userEntry = stored.openedTasks["wb-managed-round:supplier_selection"];
  assert.equal(typeof context.handleManagedSupplierWindowRemoved, "function", "managed window close handling must be implemented");
  await context.handleManagedSupplierWindowRemoved(userEntry.windowId);
  assert.equal(userEntry.closedByUser, true, "closing the managed window manually must pause the task");
  const reopen = await context.performOpenTask(userCloseTask, "managed_round_test", { allowCreate: true });
  assert.equal(reopen.stopped, true, "the same dispatch token must not reopen after a user closure");
  assert.ok(postedPaths.some((value) => value.includes("/api/batches/wb-managed-round/runner/stop")));

  process.stdout.write("browser managed supplier round: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
