"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const runtimeMessages = [];
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
        return { transport_ok: false, error: "Failed to fetch" };
      }
      return { ok: true };
    },
  },
};

const context = vm.createContext({
  chrome,
  console: { info() {}, error() {} },
  document: {},
  location: { href: "https://www.ozon.ru/product/test-1/" },
  localStorage: {
    getItem() { return null; },
    setItem() {},
    removeItem() {},
  },
  setTimeout,
  clearTimeout,
  URL,
  window: {},
});

const contentPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "content.js");
const source = fs.readFileSync(contentPath, "utf8").replace(/\ntriggerRun\(\);\s*$/, "\n");
vm.runInContext(source, context, { filename: contentPath });
assert.doesNotThrow(
  () => vm.runInContext(source, context, { filename: contentPath }),
  "content script reinjection after an extension reload must be idempotent",
);

(async () => {
  await context.window.OzonV2BrowserBridge.run().catch(() => null);
  const failure = runtimeMessages.find((message) => message.type === "ozon_v2_content_task_failed");
  assert.ok(failure, "a failed content run must notify the extension background worker");
  assert.equal(failure.error, "Failed to fetch");
  process.stdout.write("browser content failure recovery: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
