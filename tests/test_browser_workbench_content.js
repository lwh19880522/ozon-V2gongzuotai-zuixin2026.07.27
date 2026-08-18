"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const stored = { ozon_v2_workbench_run_id: "wb-old" };
const sentMessages = [];
const postedHeartbeats = [];
let activeFetchCount = 0;

const runnableTask = {
  ok: true,
  code: "browser_task.supplier_selection_ready",
  message: "ready",
  data: {
    run_id: "wb-new",
    task_type: "supplier_selection",
    created_at: "2026-07-11T04:00:00+00:00",
  },
};

const context = vm.createContext({
  document: {
    querySelector(selector) {
      if (selector !== 'meta[name="ozon-v2-workbench-token"]') return null;
      return { getAttribute() { return "test-workbench-auth-token-00000000000000000000"; } };
    },
  },
  chrome: {
    runtime: {
      getManifest() {
        return { version: "9.8.7" };
      },
      async sendMessage(message) {
        sentMessages.push(message);
        return { ok: true };
      },
    },
  },
  location: { href: "http://127.0.0.1:8765/" },
  localStorage: {
    getItem(key) {
      return stored[key] || null;
    },
    setItem(key, value) {
      stored[key] = String(value);
    },
  },
  fetch: async (url, options = {}) => {
    if (options.method === "POST") {
      postedHeartbeats.push(JSON.parse(options.body));
      return { json: async () => ({ ok: true }) };
    }
    if (url.endsWith("/api/batches/wb-old/browser-task")) {
      return {
        json: async () => ({
          ok: true,
          code: "browser_task.none",
          message: "old batch is terminal",
          data: { run_id: "wb-old", created_at: "2026-07-10T04:00:00+00:00", cancelled: true },
        }),
      };
    }
    if (url.endsWith("/api/browser-task/active")) {
      activeFetchCount += 1;
      return { json: async () => runnableTask };
    }
    throw new Error(`unexpected fetch: ${url}`);
  },
  setInterval() {
    return 1;
  },
  console,
  JSON,
  encodeURIComponent,
});

const scriptPath = path.join(
  __dirname,
  "..",
  "browser_extension",
  "ozon_v2_bridge",
  "workbench_content.js",
);
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });

setTimeout(() => {
  const taskMessages = sentMessages.filter((message) => message.type === "ozon_v2_current_task");
  const authMessages = sentMessages.filter((message) => message.type === "ozon_v2_set_workbench_auth");
  assert.equal(activeFetchCount, 1, "a stale terminal batch must fall back to the newest active browser task");
  assert.equal(authMessages.length, 1, "the workbench token must be delivered to the extension background");
  assert.equal(taskMessages.length, 1, "the newest runnable task must be dispatched automatically");
  assert.equal(taskMessages[0].task.data.run_id, "wb-new");
  assert.equal(stored.ozon_v2_workbench_run_id, "wb-new", "the workbench must follow the recovered active batch");
  assert.equal(postedHeartbeats.length, 1, "the workbench must report bridge liveness on every poll");
  assert.equal(postedHeartbeats[0].connection_only, true, "workbench liveness must not overwrite task evidence");
  process.stdout.write("browser workbench active-task recovery: OK\n");
}, 20);
