"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const elements = new Map();
let clickCapture = null;
let assignedUrl = "";

function node(text = "") {
  return {
    innerText: text,
    textContent: text,
    style: {},
    children: [],
    appendChild(child) {
      this.children.push(child);
      if (child.id) elements.set(child.id, child);
      return child;
    },
    getAttribute() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
}

const document = {
  title: "1688 image search",
  body: node("1688 image search"),
  documentElement: { dataset: {} },
  createElement() { return node(); },
  getElementById(id) { return elements.get(id) || null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener(type, listener, capture) {
    if (type === "click" && capture === true) clickCapture = listener;
  },
};

const binding = {
  run_id: "wb-managed",
  channel_index: 0,
  seed_id: "seed-1",
  ozon_product_id: "ozon-1",
  ozon_title: "Ozon test product",
  reference_image_url: "https://ir.ozone.ru/reference.jpg",
  capture_url: "/api/batches/wb-managed/supplier-selection/capture",
  reject_url: "/api/batches/wb-managed/supplier-review/reject",
};

const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    async sendMessage(message) {
      if (message.type === "ozon_v2_get_current_task") {
        return { ok: true, task: { code: "browser_task.supplier_selection_ready" } };
      }
      if (message.type === "ozon_v2_get_supplier_channel") return { ok: true, binding };
      return { ok: true };
    },
    onMessage: { addListener() {} },
  },
  storage: { local: { async get(defaults) { return defaults; }, async set() {}, async remove() {} } },
};

const location = {
  href: "https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch",
  hostname: "air.1688.com",
  assign(url) {
    assignedUrl = String(url);
    this.href = assignedUrl;
  },
};

const context = vm.createContext({
  chrome,
  console,
  document,
  location,
  fetch: async () => ({ json: async () => ({ ok: true }) }),
  setTimeout,
  clearTimeout,
  URL,
  Date,
  Promise,
  JSON,
});

const scriptPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "supplier_content.js");
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });

(async () => {
  const deadline = Date.now() + 1000;
  while (!clickCapture && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(clickCapture, "a managed 1688 page must capture detail-link clicks for same-tab navigation");

  const detailUrl = "https://detail.1688.com/offer/123456789012.html";
  const anchor = {
    href: detailUrl,
    getAttribute(name) { return name === "href" ? detailUrl : null; },
    closest(selector) { return selector === "a[href]" ? this : null; },
  };
  let prevented = false;
  let stopped = false;
  clickCapture({
    target: { closest() { return anchor; } },
    button: 0,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    altKey: false,
    defaultPrevented: false,
    preventDefault() { prevented = true; },
    stopImmediatePropagation() { stopped = true; },
  });

  assert.equal(assignedUrl, detailUrl, "the exact 1688 detail URL must replace the current managed lane page");
  assert.equal(prevented, true);
  assert.equal(stopped, true);
  process.stdout.write("supplier same-tab detail navigation: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});