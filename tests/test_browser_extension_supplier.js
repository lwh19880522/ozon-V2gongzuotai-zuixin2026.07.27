"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const listeners = {};
const stored = { openedTasks: {}, currentRunId: "" };
const tabs = new Map();
const injectedFiles = [];
let createCount = 0;
let windowCreateCount = 0;
let nextWindowId = 70;
let contentReady = false;
let currentServerTask = { ok: true, code: "browser_task.none", data: {} };
const postedPaths = [];

function event(name) {
  return { addListener(listener) { listeners[name] = listener; } };
}

const chrome = {
  storage: { local: {
    async get(defaults) { return { ...defaults, ...stored }; },
    async set(values) { Object.assign(stored, values); },
  } },
  tabs: {
    onRemoved: event("tabRemoved"),
    async get(tabId) { if (!tabs.has(tabId)) throw new Error("tab not found"); return tabs.get(tabId); },
    async query(queryInfo = {}) {
      const values = [...tabs.values()];
      if (!queryInfo.url) return values;
      return values.filter((tab) => /1688\.com\//i.test(tab.url || ""));
    },
    async create({ url, windowId }) {
      createCount += 1;
      const tab = { id: createCount, url, windowId };
      tabs.set(tab.id, tab);
      return tab;
    },
    async update(tabId, changes) {
      const tab = { ...tabs.get(tabId), ...changes };
      tabs.set(tabId, tab);
      return tab;
    },
    async reload() {},
    async sendMessage(_tabId, message) {
      if (!contentReady) throw new Error("no receiver");
      if (message.type === "ozon_v2_content_ping") return { ok: true };
      return { ok: true, result: { ok: true, code: "bridge.supplier_running" } };
    },
  },
  windows: {
    onRemoved: event("windowRemoved"),
    async create({ url }) {
      windowCreateCount += 1;
      const windowId = nextWindowId++;
      const createdTabs = (Array.isArray(url) ? url : [url]).map((tabUrl) => {
        createCount += 1;
        const tab = { id: createCount, url: tabUrl, windowId };
        tabs.set(tab.id, tab);
        return tab;
      });
      return { id: windowId, tabs: createdTabs };
    },
  },
  scripting: {
    async executeScript(options) {
      injectedFiles.push(...(options.files || []));
      contentReady = true;
    },
  },
  alarms: { create() {}, onAlarm: event("alarm") },
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
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
    if (options.method === "POST") postedPaths.push(String(url));
    return { json: async () => currentServerTask };
  },
  setInterval() { return 1; },
  encodeURIComponent,
  Date,
  Promise,
});
const backgroundPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "background.js");
vm.runInContext(fs.readFileSync(backgroundPath, "utf8"), context, { filename: backgroundPath });

const supplierUrl = "https://detail.1688.com/offer/123456789012.html";
const supplierTask = {
  ok: true,
  code: "browser_task.supplier_collection_ready",
  message: "ready",
  data: {
    run_id: "wb-supplier",
    created_at: "2026-07-12T04:00:00+00:00",
    task_type: "supplier_collection",
    dispatch_token: "supplier-token",
    ingest_url: "/api/batches/wb-supplier/supplier-collection-result",
    contract: { items: [{ seed_id: "seed-test", supplier_url: supplierUrl }] },
  },
};
const managedSupplierTask = {
  ok: true,
  code: "browser_task.supplier_selection_ready",
  message: "ready",
  data: {
    run_id: "wb-managed",
    created_at: "2026-07-15T04:00:00+00:00",
    task_type: "supplier_selection",
    dispatch_token: "managed-token",
    capture_url: "/api/batches/wb-managed/supplier-selection/capture",
    reject_url: "/api/batches/wb-managed/supplier-review/reject",
    contract: {
      items: [
        { channel_index: 0, seed_id: "seed-1", ozon_product_id: "ozon-1", reference_image_url: "https://ir.ozone.ru/one.jpg" },
        { channel_index: 1, seed_id: "seed-2", ozon_product_id: "ozon-2", reference_image_url: "https://ir.ozone.ru/two.jpg" },
      ],
    },
  },
};

function sendTask(task) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("message response timeout")), 1000);
    listeners.message({ type: "ozon_v2_current_task", task }, { tab: { windowId: 42 } }, (response) => {
      clearTimeout(timeout);
      resolve(response);
    });
  });
}

(async () => {
  await sendTask(supplierTask);
  assert.equal(createCount, 1, "one supplier task must create one controlled 1688 tab");
  assert.equal(tabs.get(1).url, supplierUrl, "supplier task must open the user-verified link exactly");

  await sendTask(supplierTask);
  assert.equal(createCount, 1, "repeated supplier polling must not create another tab");

  tabs.set(1, { ...tabs.get(1), url: supplierUrl });
  stored.openedTasks["wb-supplier:supplier_collection"].dispatchToken = null;
  await sendTask(supplierTask);
  assert.ok(
    injectedFiles.includes("supplier_content.js"),
    "a supplier task must inject the dedicated 1688 content script",
  );

  tabs.clear();
  stored.openedTasks = {};
  createCount = 0;
  currentServerTask = managedSupplierTask;
  await sendTask(managedSupplierTask);
  assert.equal(windowCreateCount, 1, "managed supplier selection must create one dedicated Edge window");
  assert.equal(tabs.size, 2, "one managed tab must be created for every supplier-review channel");
  const managedTabs = [...tabs.values()];
  assert.ok(managedTabs.every((tab) => tab.windowId === managedTabs[0].windowId));
  assert.notEqual(managedTabs[0].windowId, 42, "the workbench window must never be reused");

  await sendTask(managedSupplierTask);
  assert.equal(windowCreateCount, 1, "polling the same dispatch token must not open another managed window");

  const firstBinding = await new Promise((resolve) => {
    listeners.message(
      { type: "ozon_v2_get_supplier_channel" },
      { tab: managedTabs[0] },
      resolve,
    );
  });
  assert.equal(firstBinding.binding.seed_id, "seed-1");
  assert.equal(firstBinding.binding.channel_index, 0);

  tabs.delete(managedTabs[0].id);
  listeners.tabRemoved(managedTabs[0].id);
  await new Promise((resolve) => setTimeout(resolve, 20));
  const managedEntry = stored.openedTasks["wb-managed:supplier_selection"];
  assert.equal(
    postedPaths.some((value) => value.includes("/api/batches/wb-managed/runner/stop")),
    false,
    "closing one managed lane must not pause the whole batch",
  );
  assert.equal(managedEntry.channels[0].tabId, null);
  assert.equal(managedEntry.channels[1].tabId, managedTabs[1].id);
  await sendTask(managedSupplierTask);
  assert.equal(windowCreateCount, 1, "polling must not recreate a single user-closed lane");
  assert.equal(tabs.size, 1);

  listeners.windowRemoved(managedEntry.windowId);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.ok(
    postedPaths.some((value) => value.includes("/api/batches/wb-managed/runner/stop")),
    "closing the whole managed window must still pause the batch",
  );
  await sendTask(managedSupplierTask);
  assert.equal(windowCreateCount, 1, "a user-closed managed window must stay stopped until explicit resume");
  process.stdout.write("browser supplier task routing: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
