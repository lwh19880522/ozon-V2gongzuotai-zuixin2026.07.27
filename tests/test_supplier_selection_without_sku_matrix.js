"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const supplierUrl = "https://detail.1688.com/offer/123456789012.html";
const elements = new Map();
let capturedPayload = null;

function textNode(text = "") {
  return {
    innerText: text,
    textContent: text,
    style: {},
    dataset: {},
    children: [],
    appendChild(child) {
      this.children.push(child);
      if (child.id) elements.set(child.id, child);
      return child;
    },
    addEventListener(type, handler) { this[`on${type}`] = handler; },
    setAttribute(name, value) { if (name === "data-role") this.dataset.role = String(value); },
    getAttribute() { return null; },
    querySelector(selector) {
      if (selector.startsWith("#")) return elements.get(selector.slice(1)) || null;
      const stack = [...this.children];
      while (stack.length) {
        const item = stack.shift();
        if (selector === "[data-role='channel-title']" && item.dataset && item.dataset.role === "channel-title") return item;
        stack.push(...(item.children || []));
      }
      return null;
    },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
}

function imageNode(url) {
  return {
    currentSrc: url,
    src: url,
    naturalWidth: 800,
    naturalHeight: 800,
    width: 800,
    height: 800,
    getAttribute(name) { return name === "data-src" ? url : null; },
    closest() { return {}; },
  };
}

const body = textNode(
  "Test Supplier \u6709\u9650\u516c\u53f8 Test Product \u00a510.00 \u9001\u81f3 \u798f\u5efa\u6cc9\u5dde \u8fd0\u8d395\u5143",
);
const document = {
  title: "Test Product - 1688",
  body,
  documentElement: textNode(),
  createElement() { return textNode(); },
  addEventListener() {},
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) {
    if (selector.includes("og:title")) return { getAttribute: () => "Test Product" };
    if (selector.includes("company.1688.com") || selector.includes("winport.1688.com")) {
      return textNode("Test Supplier \u6709\u9650\u516c\u53f8");
    }
    if (selector === "h1") return textNode("Test Product");
    if (selector.includes("price") || selector.includes("Price")) return textNode("\u00a510.00");
    if (selector.includes("logistics") || selector.includes("freight")) {
      return textNode("\u9001\u81f3 \u798f\u5efa\u6cc9\u5dde \u8fd0\u8d395\u5143");
    }
    if (selector.includes("[class*='sku'] button")) return {};
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "img") {
      return [imageNode("https://cbu01.alicdn.com/img/ibank/product.jpg")];
    }
    return [];
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
  storage: {
    local: {
      async get(defaults) { return defaults; },
      async set() {},
      async remove() {},
    },
  },
};

const context = vm.createContext({
  chrome,
  console,
  document,
  location: { href: supplierUrl, hostname: "detail.1688.com" },
  fetch: async (url, options = {}) => {
    if (String(url).includes("supplier-selection/capture")) {
      capturedPayload = JSON.parse(options.body);
    }
    return {
      json: async () => ({
        ok: true,
        code: "supplier_selection.batch_complete",
        message: "saved",
      }),
    };
  },
  addEventListener() {},
  history: { pushState() {}, replaceState() {} },
  innerWidth: 1280,
  innerHeight: 720,
  MutationObserver: class { observe() {} },
  setTimeout,
  clearTimeout,
  URL,
  Date,
  Promise,
  JSON,
});

const scriptPath = path.join(
  __dirname,
  "..",
  "browser_extension",
  "ozon_v2_bridge",
  "supplier_content.js",
);
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });

(async () => {
  const deadline = Date.now() + 1000;
  while (!document.getElementById("ozon-v2-collect-current-product") && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  const collectButton = document.getElementById("ozon-v2-collect-current-product");
  assert.ok(collectButton, "the managed detail page must expose a collection button");

  await collectButton.onclick();

  assert.ok(
    capturedPayload,
    "user-confirmed public detail evidence must be submitted even when the complete SKU matrix is not parsed",
  );
  assert.deepEqual(capturedPayload.supplier_product.sku_options, []);
  process.stdout.write("supplier managed capture without SKU matrix: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
