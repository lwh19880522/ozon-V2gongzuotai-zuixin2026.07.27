"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const supplierUrl = "https://detail.1688.com/offer/123456789012.html";
const elements = new Map();
let capturedPayload = null;
let channelLookupCount = 0;
let rejectedChannel = false;
const terminalStates = [];

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

const skuScript = {
  textContent: `window.__INIT_DATA__ = ${JSON.stringify({
    skuProps: [{ prop: "颜色", value: [{ name: "黑色", imageUrl: "https://cbu01.alicdn.com/sku-black.jpg" }] }],
    skuMap: {
      "黑色": {
        skuId: "sku-black-1",
        specAttrs: "黑色",
        discountPrice: "12.80",
        canBookCount: 88,
        imageUrl: "https://cbu01.alicdn.com/sku-black.jpg",
      },
    },
  })};`,
};

const body = textNode("广东测试供应商有限公司 测试收纳盒 ¥12.80 送至 福建泉州 包邮");
const attributeRow = {
  querySelectorAll(selector) {
    return selector === "th,td" ? [textNode("材质"), textNode("不锈钢")] : [];
  },
};
const document = {
  title: "测试收纳盒 - 1688",
  body,
  documentElement: textNode(),
  createElement() { return textNode(); },
  addEventListener() {},
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) {
    if (selector === "[class*='title-text']") {
      const staleTitle = textNode("过期推荐商品标题");
      staleTitle.hidden = true;
      staleTitle.getClientRects = () => [];
      return staleTitle;
    }
    if (selector.includes("og:title")) return { getAttribute: () => "测试收纳盒" };
    if (selector === "h1") return textNode("广东测试供应商有限公司");
    if (selector.includes("price") || selector.includes("Price")) return textNode("¥12.80");
    if (selector.includes("logistics") || selector.includes("freight")) return textNode("福建泉州 包邮");
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "img") return [imageNode("https://cbu01.alicdn.com/img/ibank/product.jpg")];
    if (selector === "script") return [skuScript];
    if (selector === "tr") return [attributeRow];
    return [];
  },
};

const binding = {
  run_id: "wb-managed",
  channel_index: 0,
  seed_id: "seed-1",
  ozon_product_id: "ozon-1",
  dispatch_token: "supplier-dispatch-current",
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
      if (message.type === "ozon_v2_get_supplier_channel") {
        channelLookupCount += 1;
        return channelLookupCount === 1 ? { ok: false } : { ok: true, binding };
      }
      if (message.type === "ozon_v2_capture_supplier_channel") {
        capturedPayload = message;
        return {
          ok: true,
          result: {
            ok: true,
            code: "supplier_selection.batch_complete",
            message: "saved",
          },
        };
      }
      if (message.type === "ozon_v2_reject_supplier_channel") {
        rejectedChannel = true;
        return { ok: true };
      }
      if (message.type === "ozon_v2_supplier_channel_terminal") {
        terminalStates.push(message.state);
        return { ok: true };
      }
      return { ok: true };
    },
    onMessage: { addListener() {} },
  },
  storage: { local: {
    async get(defaults) { return defaults; },
    async set() {},
    async remove() {},
  } },
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
  while (!document.getElementById("ozon-v2-supplier-panel") && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  const panel = document.getElementById("ozon-v2-supplier-panel");
  assert.ok(panel, "a fixed managed supplier panel must be rendered on 1688 pages");
  assert.ok(channelLookupCount >= 2, "the content script must retry if the tab loads before its channel binding is saved");
  assert.equal(capturedPayload, null, "managed supplier evidence must never be submitted before a user click");
  const collectButton = document.getElementById("ozon-v2-collect-current-product");
  const rejectButton = document.getElementById("ozon-v2-no-supplier");
  assert.ok(collectButton, "the current-product collection button must remain visible");
  assert.ok(rejectButton, "the no-supplier button must remain visible");
  await collectButton.onclick();
  assert.ok(capturedPayload, "a user click must proxy current public supplier evidence through the background");
  assert.equal(
    capturedPayload.type,
    "ozon_v2_capture_supplier_channel",
    "the content page must not post channel identity directly to the workbench",
  );
  const product = capturedPayload.supplier_product;
  assert.equal(product.supplier_url, supplierUrl);
  assert.equal(product.final_url, supplierUrl);
  assert.equal(product.offer_id, "123456789012");
  assert.equal(product.title, "测试收纳盒");
  assert.ok(product.seller && product.seller.shop_name);
  assert.deepEqual(product.images, ["https://cbu01.alicdn.com/img/ibank/product.jpg"]);
  assert.equal(product.price.currency, "CNY");
  assert.ok(product.domestic_shipping_evidence.visible_text);
  assert.equal(product.attributes["材质"], "不锈钢");
  assert.equal(product.sku_groups.length, 1, "embedded SKU properties must be returned as groups");
  assert.equal(product.sku_groups[0].name, "颜色");
  assert.equal(product.sku_groups[0].options[0].label, "黑色");
  assert.equal(product.sku_options.length, 1);
  assert.equal(product.sku_options[0].supplier_sku_id, "sku-black-1");
  assert.deepEqual(
    terminalStates,
    [],
    "the background must persist the capture and mark the lane terminal atomically",
  );
  await rejectButton.onclick();
  assert.equal(rejectedChannel, true, "the no-supplier action must reject the product through its bound channel");
  process.stdout.write("supplier managed page controls: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
