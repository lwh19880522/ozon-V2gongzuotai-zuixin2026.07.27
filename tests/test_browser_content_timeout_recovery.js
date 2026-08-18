"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const runtimeMessages = [];
let activeTaskRequests = 0;

const chrome = {
  runtime: {
    getManifest() {
      return { version: "9.8.7" };
    },
    onMessage: {
      addListener() {},
    },
    async sendMessage(message) {
      runtimeMessages.push(message);
      if (message.type === "ozon_v2_get_current_task") {
        throw new Error("background unavailable");
      }
      if (message.type === "ozon_v2_local_api_request") {
        const path = String((message.request || {}).path || "");
        if (path.endsWith("/api/browser-bridge/heartbeat")) {
          return { transport_ok: true, body: { ok: true } };
        }
        if (path.endsWith("/api/browser-task/active")) {
          activeTaskRequests += 1;
          if (activeTaskRequests === 1) return await new Promise(() => {});
          return {
            transport_ok: true,
            body: { ok: true, code: "browser_task.none", message: "No browser task is pending." },
          };
        }
      }
      return { ok: true };
    },
  },
};

const context = vm.createContext({
  AbortController,
  chrome,
  console: { info() {}, error() {} },
  document: {},
  location: { href: "https://www.ozon.ru/product/test-1/" },
  localStorage: {
    getItem() { return null; },
    setItem() {},
    removeItem() {},
  },
  sessionStorage: {
    getItem() { return null; },
    setItem() {},
    removeItem() {},
  },
  setTimeout,
  clearTimeout,
  URL,
  window: {},
  OZON_V2_API_TIMEOUT_MS: 10,
});

const contentPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "content.js");
const source = fs.readFileSync(contentPath, "utf8").replace(/\ntriggerRun\(\);\s*$/, "\n");
vm.runInContext(source, context, { filename: contentPath });

(async () => {
  await assert.rejects(
    context.window.OzonV2BrowserBridge.run(),
    /timed out/i,
    "a hung workbench request must time out",
  );
  const failure = runtimeMessages.find((message) => message.type === "ozon_v2_content_task_failed");
  assert.ok(failure, "a timed-out content run must release the dispatch token through the background worker");

  const retry = await context.window.OzonV2BrowserBridge.run();
  assert.equal(retry.code, "browser_task.none", "the content-script run lock must be released after timeout");
  assert.equal(activeTaskRequests, 2, "the next dispatch must issue a fresh workbench request");
  process.stdout.write("browser content timeout recovery: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
