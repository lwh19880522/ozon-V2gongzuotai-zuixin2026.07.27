"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const listeners = {};
const stored = {
  openedTasks: {},
  currentRunId: "",
  workbenchAuthToken: "test-workbench-auth-token-00000000000000000000",
};
const tabs = new Map();
const manifestVersion = "9.8.7";
const postedPayloads = [];
const postedRequests = [];
let createCount = 0;
let lastCreatedWindowId = null;
let updateCount = 0;
let reloadCount = 0;
let injectionCount = 0;
let contentScriptReady = false;
let contentRunCount = 0;
let contentTaskResponse = { ok: true, result: { ok: true, code: "bridge.running" } };
let activeFetchCount = 0;
let activeTaskResponse = null;
let closeTaskResponse = null;
const intervalCallbacks = [];
const alarmCreates = [];

function event(name) {
  return {
    addListener(listener) {
      listeners[name] = listener;
    },
  };
}

const chrome = {
  storage: {
    local: {
      async get(defaults) {
        return { ...defaults, ...stored };
      },
      async set(values) {
        Object.assign(stored, values);
      },
    },
  },
  tabs: {
    onRemoved: event("tabRemoved"),
    async get(tabId) {
      if (!tabs.has(tabId)) throw new Error("tab not found");
      return tabs.get(tabId);
    },
    async query(queryInfo = {}) {
      const values = [...tabs.values()];
      if (queryInfo.url) return values.filter((tab) => /^https:\/\/(?:www\.)?ozon\.ru\//i.test(tab.url || ""));
      return values;
    },
    async create({ url, windowId }) {
      createCount += 1;
      lastCreatedWindowId = windowId == null ? null : windowId;
      const tab = { id: createCount, url, windowId };
      tabs.set(tab.id, tab);
      return tab;
    },
    async update(tabId, changes) {
      updateCount += 1;
      const tab = { ...tabs.get(tabId), ...changes };
      tabs.set(tabId, tab);
      return tab;
    },
    async reload() {
      reloadCount += 1;
    },
    async sendMessage(_tabId, message) {
      if (!contentScriptReady) throw new Error("no receiver");
      if (message.type === "ozon_v2_run_task") contentRunCount += 1;
      return contentTaskResponse;
    },
  },
  scripting: {
    async executeScript() {
      injectionCount += 1;
      contentScriptReady = true;
    },
  },
  alarms: {
    create(name, options) {
      alarmCreates.push({ name, options });
    },
    onAlarm: event("alarm"),
  },
  runtime: {
    getManifest() {
      return { version: manifestVersion };
    },
    onInstalled: event("installed"),
    onStartup: event("startup"),
    onMessage: event("message"),
  },
  action: {
    onClicked: event("action"),
  },
};

const context = vm.createContext({
  chrome,
  console,
  setInterval(callback) {
    intervalCallbacks.push(callback);
    return intervalCallbacks.length;
  },
  fetch: async (url, options = {}) => {
    if (options.method === "POST") {
      const body = JSON.parse(options.body);
      postedPayloads.push(body);
      postedRequests.push({ url: String(url), body });
      return { json: async () => ({ ok: true }) };
    }
    if (url.includes("/api/batches/wb-close/browser-task")) {
      return { json: async () => closeTaskResponse };
    }
    if (url.includes("/api/batches/wb-stale/browser-task")) {
      return { json: async () => ({ ok: true, code: "browser_task.none", data: { run_id: "wb-stale", created_at: "2026-07-10T04:00:00+00:00", cancelled: true } }) };
    }
    if (url.includes("/api/batches/wb-complete/browser-task")) {
      return { json: async () => ({ ok: true, code: "browser_task.none", data: { run_id: "wb-complete", created_at: "2026-07-10T04:00:00+00:00", status: "ozon_collected", cancelled: false } }) };
    }
    if (url.includes("/api/browser-task/active")) {
      activeFetchCount += 1;
      return { json: async () => activeTaskResponse || task("ozon_collection") };
    }
    return { json: async () => ({ ok: true, code: "browser_task.none", data: {} }) };
  },
  encodeURIComponent,
  Date,
  Promise,
});
const backgroundPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "background_v2.js");
vm.runInContext(fs.readFileSync(backgroundPath, "utf8"), context, { filename: backgroundPath });

function task(taskType, runId = "wb-test", createdAt = "2026-07-10T04:30:00+00:00") {
  return {
    ok: true,
    code: taskType === "ozon_collection"
      ? "browser_task.ozon_collection_ready"
      : "browser_task.ozon_collection_ready",
    message: "ready",
    data: {
      run_id: runId,
      created_at: createdAt,
      task_type: taskType,
      dispatch_token: `${runId}:${taskType}:${createdAt}`,
      contract: {
        payload: {
          seeds: [{ ozon_query_terms_ru: ["органайзер для хранения"] }],
        },
      },
    },
  };
}

function sendMessage(message) {
  return sendMessageFromTab(message, null);
}

function sendMessageFromTab(message, tabId) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("message response timeout")), 1000);
    const handled = listeners.message(message, { tab: { id: tabId, windowId: 42 } }, (response) => {
      clearTimeout(timeout);
      resolve(response);
    });
    if (!handled) {
      clearTimeout(timeout);
      resolve(null);
    }
  });
}

(async () => {
  const first = task("ozon_collection");
  stored.currentRunId = "";
  await Promise.all([
    sendMessage({ type: "ozon_v2_current_task", task: first }),
    sendMessage({ type: "ozon_v2_current_task", task: first }),
    context.pollTask("concurrent_poll"),
  ]);
  assert.equal(createCount, 1, "concurrent task notifications must create at most one controlled Ozon tab");
  assert.equal(lastCreatedWindowId, 42, "a workbench-triggered task must open inside the sender's Edge window");
  assert.equal(injectionCount, 1, "a mapped Ozon tab without a content-script receiver must be injected once");
  assert.equal(contentRunCount, 1, "one dispatch token must trigger one content-script run");
  assert.ok(tabs.get(1).url.startsWith("https://www.ozon.ru/search/"));
  assert.equal(
    new URL(tabs.get(1).url).searchParams.get("text"),
    `${first.data.contract.payload.seeds[0].ozon_query_terms_ru[0]} из Китая`,
    "the background must prequalify China candidates before opening detail pages",
  );
  assert.ok(stored.openedTasks["wb-test:ozon_collection"]);
  assert.ok(
    postedPayloads.some((payload) => payload.extension_version === manifestVersion),
    "heartbeats must report the version loaded from chrome.runtime.getManifest()",
  );
  assert.ok(
    postedPayloads.some((payload) => payload.connection_only === true),
    "background polling must send an independent liveness heartbeat before task dispatch",
  );
  assert.ok(
    alarmCreates.some(({ options }) => options && options.periodInMinutes === 1),
    "the MV3 fallback alarm must use the broadly supported one-minute interval",
  );

  const heartbeatsBeforeQuietReuse = postedPayloads.length;
  await sendMessage({ type: "ozon_v2_current_task", task: first });
  assert.equal(createCount, 1, "the first runnable task must open one Ozon tab");
  assert.equal(updateCount, 0);
  assert.equal(
    postedPayloads.length,
    heartbeatsBeforeQuietReuse,
    "a reused tab without a new dispatch token must not overwrite the content-script stage",
  );

  await sendMessage({ type: "ozon_v2_current_task", task: first });
  assert.equal(createCount, 1, "repeated polling must not create another tab");
  assert.equal(updateCount, 0, "repeated polling must not bounce the mapped tab");

  contentTaskResponse = undefined;
  const navigationTask = task("ozon_collection", "wb-test", "2026-07-10T04:30:30+00:00");
  const heartbeatsBeforeNavigation = postedPayloads.length;
  await sendMessage({ type: "ozon_v2_current_task", task: navigationTask });
  assert.equal(
    stored.openedTasks["wb-test:ozon_collection"].dispatchToken,
    navigationTask.data.dispatch_token,
    "a content task that navigates away before replying must still consume its dispatch token",
  );
  assert.equal(
    postedPayloads.length,
    heartbeatsBeforeNavigation,
    "a navigation handoff must not be reported as a failed content task",
  );

  const successfulDispatchToken = stored.openedTasks["wb-test:ozon_collection"].dispatchToken;
  contentTaskResponse = {
    ok: false,
    result: {
      ok: false,
      code: "bridge.search_blocked",
      message: "Required public fields are still missing.",
      missing_fields: ["seller", "price"],
    },
  };
  await sendMessage({
    type: "ozon_v2_current_task",
    task: task("ozon_collection", "wb-test", "2026-07-10T04:31:00+00:00"),
  });
  assert.ok(
    postedPayloads.some((payload) => (
      payload.stage === "task_failed" && payload.code === "bridge.search_blocked"
    )),
    "a failed content task must remain visible as a failed browser stage",
  );
  assert.equal(
    stored.openedTasks["wb-test:ozon_collection"].dispatchToken,
    successfulDispatchToken,
    "a failed content task must not consume the new dispatch token",
  );
  assert.ok(
    postedPayloads.some((payload) => (
      payload.code === "bridge.search_blocked"
      && payload.message === "Required public fields are still missing."
      && payload.details.missing_fields.join(",") === "seller,price"
    )),
    "failed content evidence must reach diagnostics without being replaced by a generic message",
  );
  contentTaskResponse = { ok: true, result: { ok: true, code: "bridge.running" } };

  const collectionTask = task("ozon_collection");
  await sendMessage({ type: "ozon_v2_current_task", task: collectionTask });
  assert.equal(createCount, 1, "the collection task must reuse the existing Ozon tab");
  assert.equal(updateCount, 0, "repeated collection polling must not navigate the tab again");
  await sendMessage({ type: "ozon_v2_current_task", task: collectionTask });
  const collectionKey = "wb-test:ozon_collection";
  const runsBeforeRecovery = contentRunCount;
  assert.equal(
    stored.openedTasks[collectionKey].dispatchToken,
    collectionTask.data.dispatch_token,
    "an accepted content run records the active collection dispatch token",
  );
  const failed = await sendMessageFromTab({
    type: "ozon_v2_content_task_failed",
    error: "Failed to fetch",
  }, stored.openedTasks[collectionKey].tabId);
  assert.equal(failed.result.reset, true, "a content-script failure must release the mapped task token");
  assert.equal(
    stored.openedTasks[collectionKey].dispatchToken,
    null,
    "the failed dispatch token must not remain consumed",
  );
  await sendMessage({ type: "ozon_v2_current_task", task: collectionTask });
  assert.equal(
    contentRunCount,
    runsBeforeRecovery + 1,
    "the next poll must dispatch the same collection task again after script failure",
  );

  stored.currentRunId = "wb-complete";
  activeTaskResponse = task("ozon_collection", "wb-historical", "2026-07-10T03:00:00+00:00");
  const activeFetchesBeforeTerminalCheck = activeFetchCount;
  const terminal = await sendMessage({ type: "ozon_v2_get_current_task" });
  assert.equal(terminal.task.code, "browser_task.none", "a completed current run must remain terminal");
  assert.equal(
    activeFetchCount,
    activeFetchesBeforeTerminalCheck + 1,
    "a terminal run should check whether a genuinely newer batch exists",
  );
  assert.equal(stored.currentRunId, "wb-complete", "an older historical task must not replace the current run");

  stored.currentRunId = "wb-stale";
  activeTaskResponse = task("ozon_collection", "wb-historical", "2026-07-10T03:00:00+00:00");
  const activeFetchesBeforeStoppedCheck = activeFetchCount;
  const stopped = await sendMessage({ type: "ozon_v2_get_current_task" });
  assert.equal(stopped.task.code, "browser_task.none", "a stopped current run must remain stopped");
  assert.equal(
    activeFetchCount,
    activeFetchesBeforeStoppedCheck + 1,
    "a stopped run should check whether a genuinely newer batch exists",
  );
  assert.equal(stored.currentRunId, "wb-stale", "an older historical task must not replace a stopped run");

  stored.currentRunId = "wb-complete";
  activeTaskResponse = task("ozon_collection", "wb-new", "2026-07-10T05:00:00+00:00");
  const next = await sendMessage({ type: "ozon_v2_get_current_task" });
  assert.equal(next.task.data.run_id, "wb-new", "a genuinely newer batch must be discoverable automatically");

  const updatesBeforeNewBatch = updateCount;
  await intervalCallbacks[0]();
  assert.equal(stored.currentRunId, "wb-new", "background polling must adopt the genuinely newer batch");
  assert.equal(updateCount, updatesBeforeNewBatch + 1, "background polling must navigate the reused Ozon tab once");

  stored.openedTasks["wb-new:ozon_collection"] = {
    tabId: 1,
    targetUrl: tabs.get(1).url,
    extensionVersion: "0.1.10",
  };
  const updatesBeforeVersionRefresh = updateCount;
  const reloadsBeforeVersionRefresh = reloadCount;
  await sendMessage({
    type: "ozon_v2_current_task",
    task: task("ozon_collection", "wb-new", "2026-07-10T05:00:00+00:00"),
  });
  assert.equal(updateCount, updatesBeforeVersionRefresh, "same-URL version refresh must not use a no-op URL update");
  assert.equal(reloadCount, reloadsBeforeVersionRefresh + 1, "same-URL version refresh must force one tab reload");
  assert.equal(stored.openedTasks["wb-new:ozon_collection"].extensionVersion, manifestVersion);

  tabs.set(1, { id: 1, url: "edge://extensions/" });
  stored.openedTasks["wb-remap:ozon_collection"] = {
    tabId: 1,
    targetUrl: "https://www.ozon.ru/search/?text=old",
    extensionVersion: manifestVersion,
    dispatchToken: "old-token",
  };
  const createsBeforeInvalidMapping = createCount;
  await sendMessage({
    type: "ozon_v2_current_task",
    task: task("ozon_collection", "wb-remap", "2026-07-10T05:30:00+00:00"),
  });
  assert.equal(
    createCount,
    createsBeforeInvalidMapping + 1,
    "a mapped tab that is no longer on Ozon must be replaced instead of silently reused",
  );
  assert.ok(tabs.get(createCount).url.startsWith("https://www.ozon.ru/search/"));

  const closeTask = task("ozon_collection", "wb-close", "2026-07-10T05:40:00+00:00");
  closeTaskResponse = closeTask;
  await sendMessage({ type: "ozon_v2_current_task", task: closeTask });
  const closeKey = "wb-close:ozon_collection";
  const closedTabId = stored.openedTasks[closeKey].tabId;
  const createsBeforeUserClose = createCount;
  tabs.delete(closedTabId);
  assert.equal(typeof listeners.tabRemoved, "function", "the extension must observe user-closed task tabs");
  listeners.tabRemoved(closedTabId, { windowId: 42, isWindowClosing: false });
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (postedRequests.some(({ url }) => url.includes("/api/batches/wb-close/runner/stop"))) break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(
    postedRequests.some(({ url }) => url.includes("/api/batches/wb-close/runner/stop")),
    "closing the assigned tab must stop the matching browser task",
  );
  await context.pollTask("after_user_close");
  assert.equal(createCount, createsBeforeUserClose, "background polling must not reopen a user-closed task tab");

  closeTaskResponse = task("ozon_collection", "wb-close", "2026-07-10T05:41:00+00:00");
  await context.pollTask("after_explicit_resume");
  assert.equal(createCount, createsBeforeUserClose + 1, "a new resume token may reopen the task tab once");
  const createsAfterExplicitResume = createCount;
  await context.pollTask("repeat_same_resume_token");
  assert.equal(
    createCount,
    createsAfterExplicitResume,
    "repeated polling with the same explicit resume token must not open another tab",
  );

  process.stdout.write("browser background task reuse: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
