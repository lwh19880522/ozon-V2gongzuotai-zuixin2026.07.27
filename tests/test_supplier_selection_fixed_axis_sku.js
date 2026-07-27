"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const supplierUrl = "https://detail.1688.com/offer/1050780789075.html";
const elements = new Map();
let capturedPayload = null;

function textNode(text = "") {
  return {
    innerText: text,
    textContent: text,
    style: {},
    dataset: {},
    children: [],
    appendChild(child) { this.children.push(child); if (child.id) elements.set(child.id, child); return child; },
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

function skuOption(label, imageUrl, selected = false) {
  return {
    className: selected ? "sku-item selected" : "sku-item",
    style: {},
    disabled: false,
    getAttribute(name) {
      if (name === "title") return label;
      if (name === "aria-selected") return selected ? "true" : "false";
      return null;
    },
    querySelector(selector) { return selector === "img" ? imageNode(imageUrl) : null; },
  };
}

function skuGroup(name, options) {
  return {
    className: "sku-item-wrapper",
    getAttribute(attribute) { return attribute === "data-property-name" ? name : null; },
    querySelector() { return null; },
    querySelectorAll() { return options; },
  };
}

const groups = [
  skuGroup("规格", [
    skuOption("清洁剂100ml*2+刷子*1", "https://cbu01.alicdn.com/img/ibank/hammock-grey.jpg"),
    skuOption("儿童飞机脚踏板【黑色】", "https://cbu01.alicdn.com/img/ibank/hammock-black.jpg", true),
  ]),
  skuGroup("颜色", [
    skuOption("黑色", "https://cbu01.alicdn.com/img/ibank/hammock-black.jpg", true),
  ]),
];

const body = textNode("测试供应商有限公司 儿童飞机吊床 ¥19.90 送至 福建泉州 运费5元");
const document = {
  title: "儿童飞机吊床 - 1688",
  body,
  documentElement: textNode(),
  createElement() { return textNode(); },
  addEventListener() {},
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) {
    if (selector.includes("og:title")) return { getAttribute: () => "儿童飞机吊床" };
    if (selector.includes("company.1688.com") || selector.includes("winport.1688.com")) return textNode("测试供应商有限公司");
    if (selector === "h1") return textNode("儿童飞机吊床");
    if (selector.includes("price") || selector.includes("Price")) return textNode("¥19.90");
    if (selector.includes("logistics") || selector.includes("freight")) return textNode("送至 福建泉州 运费5元");
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "img") return [imageNode("https://cbu01.alicdn.com/img/ibank/hammock-main.jpg")];
    if (selector.includes("[data-yandex-role='sku-group']")) return groups;
    return [];
  },
};

const binding = {
  run_id: "wb-managed",
  channel_index: 0,
  seed_id: "seed-hammock",
  ozon_product_id: "3210687850",
  ozon_title: "Детская переносная люлька-гамак для самолета",
  reference_image_url: "https://ir.ozone.ru/hammock.jpg",
  capture_url: "/api/batches/wb-managed/supplier-selection/capture",
  reject_url: "/api/batches/wb-managed/supplier-review/reject",
};

const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    async sendMessage(message) {
      if (message.type === "ozon_v2_get_current_task") return { ok: true, task: { code: "browser_task.supplier_selection_ready" } };
      if (message.type === "ozon_v2_get_supplier_channel") return { ok: true, binding };
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
  fetch: async (url, options = {}) => {
    if (String(url).includes("supplier-selection/capture")) capturedPayload = JSON.parse(options.body);
    return { json: async () => ({ ok: true, code: "supplier_selection.recapture_complete", message: "saved" }) };
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

const scriptPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "supplier_content.js");
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });

(async () => {
  const deadline = Date.now() + 1000;
  while (!document.getElementById("ozon-v2-collect-current-product") && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  const collectButton = document.getElementById("ozon-v2-collect-current-product");
  assert.ok(collectButton, "the managed product page must expose the explicit collection button");
  await collectButton.onclick();
  assert.ok(capturedPayload, "the user-confirmed product must be captured");

  const product = capturedPayload.supplier_product;
  assert.equal(product.sku_groups.length, 2);
  assert.equal(product.sku_options.length, 2, "one varying group plus fixed singleton groups must become selectable SKU cards");
  assert.deepEqual(product.sku_options[0].selected_options, {
    "规格": "清洁剂100ml*2+刷子*1",
    "颜色": "黑色",
  });
  assert.equal(
    product.sku_options[0].set_quantity,
    3,
    "a composite SKU label must sum every explicit item multiplier",
  );
  assert.deepEqual(product.sku_options[1].selected_options, {
    "规格": "儿童飞机脚踏板【黑色】",
    "颜色": "黑色",
  });
  assert.equal(product.sku_options[0].evidence_source, "dom_single_axis_sku");
  assert.equal(product.sku_options[0].complete, true);
  assert.notEqual(product.sku_options[0].supplier_sku_id, product.sku_options[1].supplier_sku_id);
  process.stdout.write("supplier fixed-axis SKU combinations: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
