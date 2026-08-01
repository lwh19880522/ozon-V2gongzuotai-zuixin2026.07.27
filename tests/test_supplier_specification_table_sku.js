"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const supplierUrl = "https://detail.1688.com/offer/559479796544.html";
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

function rowNode(left, right) {
  const cells = [textNode(left), textNode(right)];
  return { querySelectorAll(selector) { return selector === "th,td" ? cells : []; } };
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

const rows = [
  rowNode("\u54c1\u724c", "\u6885\u82b3"),
  rowNode("\u5203\u53e3\u6750\u8d28", "\u78b3\u94a2"),
  rowNode("\u578b\u53f7", "\u5168\u957f(mm)"),
  rowNode("1018\uff086\u5bf8\u9e21\u773c\u94b3\u5e26\u9501\u6263\uff09", "135"),
  rowNode("1020\uff089\u5bf8\u591a\u529f\u80fd\u4e09\u5408\u4e00\u76ae\u5e26\u6253\u5b54\u94b3\uff09", "210"),
  rowNode("1022A\uff08\u8010\u7528\u5347\u7ea7\u6b3e\uff09", "300"),
  rowNode("\u5168\u957f", "\u5168\u90e8 135 210 300"),
  rowNode("\u6253\u5b54\u76f4\u5f84", "\u5168\u90e8 2.5 ... \u5c55\u5f00\u53c2\u6570"),
];
const imageUrl = "https://cbu01.alicdn.com/img/ibank/punch-pliers.jpg";
const document = {
  title: "Punch pliers - 1688",
  body: textNode("Test supplier Punch pliers \u00a54.70 \u5305\u90ae"),
  documentElement: textNode(),
  createElement() { return textNode(); },
  addEventListener() {},
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) {
    if (selector.startsWith("#")) return elements.get(selector.slice(1)) || null;
    if (selector.includes("og:title")) return { getAttribute: () => "Punch pliers" };
    if (selector.includes("company.1688.com") || selector.includes("winport.1688.com")) return textNode("Test supplier");
    if (selector.includes("price") || selector.includes("Price")) return textNode("\u00a54.70");
    if (selector.includes("logistics") || selector.includes("freight")) return textNode("\u5305\u90ae");
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "tr") return rows;
    if (selector === "img") return [imageNode(imageUrl)];
    if (selector === "script") return [];
    return [];
  },
};

const binding = {
  run_id: "wb-spec-table",
  channel_index: 0,
  seed_id: "seed-1599",
  ozon_product_id: "2097521796",
  ozon_title: "Punch pliers 210 mm",
  reference_image_url: "https://ir.ozone.ru/reference.jpg",
  capture_url: "/api/batches/wb-spec-table/supplier-selection/capture",
  reject_url: "/api/batches/wb-spec-table/supplier-review/reject",
};
const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    async sendMessage(message) {
      if (message.type === "ozon_v2_get_current_task") return { ok: true, task: { code: "browser_task.supplier_selection_ready" } };
      if (message.type === "ozon_v2_get_supplier_channel") return { ok: true, binding };
      if (message.type === "ozon_v2_capture_supplier_channel") {
        capturedPayload = message;
        return { ok: true, result: { ok: true, code: "supplier_selection.batch_complete", message: "saved" } };
      }
      return { ok: true };
    },
    onMessage: { addListener() {} },
  },
  storage: { local: { async get(defaults) { return defaults; }, async set() {}, async remove() {} } },
};
const context = vm.createContext({
  chrome,
  console,
  document,
  location: { href: supplierUrl, hostname: "detail.1688.com" },
  fetch: async () => ({ json: async () => ({ ok: true }) }),
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

const scriptPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "supplier_content.js");
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });

(async () => {
  const deadline = Date.now() + 1000;
  while (!document.getElementById("ozon-v2-collect-current-product") && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  const collectButton = document.getElementById("ozon-v2-collect-current-product");
  assert.ok(collectButton, "the managed page must expose the collection button");
  await collectButton.onclick();
  assert.ok(
    capturedPayload,
    `the specification-table product must be submitted: ${document.getElementById("ozon-v2-supplier-status").textContent}`,
  );
  const product = capturedPayload.supplier_product;
  assert.equal(product.sku_options.length, 3, "each valid specification row must become a selectable SKU");
  const target = product.sku_options.find((option) => option.selected_options["\u578b\u53f7"] === "1020");
  assert.ok(target, "model 1020 must be reconstructed from the table row");
  assert.deepEqual(target.selected_options, {
    "\u578b\u53f7": "1020",
    "\u89c4\u683c": "9\u5bf8\u591a\u529f\u80fd\u4e09\u5408\u4e00\u76ae\u5e26\u6253\u5b54\u94b3",
    "\u5168\u957f": "210\u6beb\u7c73",
  });
  assert.equal(target.evidence_source, "dom_specification_table");
  assert.equal(target.complete, true);
  assert.equal(product.sku.evidence, "specification_table_sku_rows");
  assert.equal(product.attributes["\u54c1\u724c"], "\u6885\u82b3");
  assert.equal(product.attributes["\u5203\u53e3\u6750\u8d28"], "\u78b3\u94a2");
  assert.equal(product.attributes["\u578b\u53f7"], undefined, "table headers must not leak into base attributes");
  assert.equal(product.attributes["1020\uff089\u5bf8\u591a\u529f\u80fd\u4e09\u5408\u4e00\u76ae\u5e26\u6253\u5b54\u94b3\uff09"], undefined);
  assert.equal(product.attributes["\u5168\u957f"], undefined, "aggregate filter rows must not become attributes");
  process.stdout.write("supplier specification table SKU reconstruction: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
