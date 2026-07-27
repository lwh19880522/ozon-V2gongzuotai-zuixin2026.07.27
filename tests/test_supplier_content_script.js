"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const supplierUrl = "https://detail.1688.com/offer/123456789012.html";
const stored = {};
let submittedPayload = null;
const progressPayloads = [];

function textNode(text) {
  return {
    innerText: text,
    textContent: text,
    getAttribute() { return null; },
    querySelectorAll() { return []; },
  };
}

function imageNode(url, width, height, inProductRegion) {
  return {
    currentSrc: url,
    src: url,
    naturalWidth: width,
    naturalHeight: height,
    width,
    height,
    getAttribute(name) { return name === "data-src" ? url : null; },
    closest() { return inProductRegion ? {} : null; },
  };
}

const productImage = "https://cbu01.alicdn.com/img/ibank/O1CN-product-main.jpg_.webp";
let fakeNow = 0;
const FastDate = {
  now() {
    fakeNow += 20000;
    return fakeNow;
  },
};
const pageImages = [
  imageNode("https://img.alicdn.com/imgextra/icon-55-tps-16-16.svg", 16, 16, false),
  imageNode(productImage, 800, 800, false),
  imageNode("https://cbu01.alicdn.com/img/ibank/O1CN-product-main.jpg_sum.jpg", 100, 100, true),
  imageNode("https://cbu01.alicdn.com/img/ibank/company-logo.jpg", 600, 600, false),
];
const skuScript = {
  textContent: `window.__INIT_DATA__ = ${JSON.stringify({
    skuProps: [
      {
        prop: "颜色",
        value: [
          { name: "粉色", imageUrl: "https://cbu01.alicdn.com/img/ibank/sku-pink.jpg" },
          { name: "蓝色", imageUrl: "https://cbu01.alicdn.com/img/ibank/sku-blue.jpg" },
        ],
      },
      {
        prop: "数量",
        value: [
          { name: "2支套装" },
          { name: "4支套装" },
        ],
      },
    ],
    skuMap: [],
    skuInfoMap: {
      "粉色>2支套装": {
        skuId: "sku-pink-2",
        specAttrs: "粉色&gt;2支套装",
        discountPrice: "11.80",
        canBookCount: 99,
      },
      "粉色>4支套装": {
        skuId: "sku-pink-4",
        specAttrs: "粉色&gt;4支套装",
        discountPrice: "12.80",
        canBookCount: 88,
      },
      "蓝色>2支套装": {
        skuId: "sku-blue-2",
        specAttrs: "蓝色&gt;2支套装",
        discountPrice: "12.20",
        canBookCount: 77,
      },
      "蓝色>4支套装": {
        skuId: "sku-blue-4",
        specAttrs: "蓝色&gt;4支套装",
        discountPrice: "13.20",
        canBookCount: 66,
      },
    },
  })};`,
};

const document = {
  title: "测试收纳盒 - 1688",
  body: textNode("广东测试供应商有限公司 测试收纳盒 ¥ 12 .80 送至 福建泉州 包邮"),
  querySelector(selector) {
    if (selector.includes("og:title")) return { getAttribute: () => "测试收纳盒" };
    if (selector.includes("og:image")) return null;
    if (selector === "h1") return textNode("广东测试供应商有限公司");
    if (selector.includes("offerTitle")) return null;
    if (selector.includes("shop-company-name") || selector.includes("company-name")) return null;
    if (selector.includes("price") || selector.includes("Price")) return textNode("价格");
    if (selector.includes("logistics") || selector.includes("freight")) return textNode("广东发货 包邮");
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "img") {
      return pageImages;
    }
    if (selector === "script") {
      return [skuScript];
    }
    return [];
  },
};

const task = {
  ok: true,
  code: "browser_task.supplier_collection_ready",
  data: {
    run_id: "wb-supplier",
    task_type: "supplier_collection",
    ingest_url: "/api/batches/wb-supplier/supplier-collection-result",
    contract: { items: [{ seed_id: "seed-test", supplier_url: supplierUrl }] },
  },
};

const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    async sendMessage(message) {
      if (message.type === "ozon_v2_get_current_task") return { ok: true, task };
      return { ok: true };
    },
    onMessage: { addListener() {} },
  },
  storage: { local: {
    async get(defaults) { return { ...defaults, ...stored }; },
    async set(values) { Object.assign(stored, values); },
    async remove(key) { delete stored[key]; },
  } },
};

const context = vm.createContext({
  chrome,
  console,
  document,
  location: { href: supplierUrl, hostname: "detail.1688.com" },
  fetch: async (url, options = {}) => {
    if (String(url).includes("supplier-collection-progress")) progressPayloads.push(JSON.parse(options.body));
    if (String(url).includes("supplier-collection-result")) submittedPayload = JSON.parse(options.body);
    return { json: async () => ({ ok: true, code: "supplier_collection.ingested", message: "saved" }) };
  },
  setTimeout,
  clearTimeout,
  URL,
  Date: FastDate,
  Promise,
  JSON,
});

const scriptPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "supplier_content.js");
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });

(async () => {
  const deadline = Date.now() + 6000;
  while (!submittedPayload && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.ok(submittedPayload, "a complete public 1688 page must be submitted to the workbench");
  assert.ok(progressPayloads.length, "partial supplier evidence must be reported before final submission");
  assert.equal(progressPayloads.at(-1).partial_product.seller.shop_name, "广东测试供应商有限公司");
  assert.deepEqual(submittedPayload.network, { mode: "direct", proxy_disabled: true });
  assert.equal(submittedPayload.supplier_products.length, 1);
  const product = submittedPayload.supplier_products[0];
  assert.equal(product.supplier_url, supplierUrl, "the user-verified URL must remain exact");
  assert.equal(product.title, "测试收纳盒");
  assert.equal(product.seller.shop_name, "广东测试供应商有限公司");
  assert.ok(product.sku.selected_options.visible_sku_labels.length);
  assert.equal(product.sku.complete, false, "visible labels must not claim a complete real SKU");
  assert.equal(product.sku_options.length, 4, "all embedded multi-axis SKU combinations must be returned");
  assert.deepEqual(
    product.sku_options.map((option) => option.supplier_sku_id),
    ["sku-pink-2", "sku-pink-4", "sku-blue-2", "sku-blue-4"],
  );
  assert.deepEqual(product.sku_options[0].selected_options, { 颜色: "粉色", 数量: "2支套装" });
  assert.equal(product.sku_options[0].set_quantity, 2);
  assert.deepEqual(product.sku_options[0].set_composition, ["2支套装"]);
  assert.deepEqual(product.sku_options[0].price, { currency: "CNY", amount: "11.80" });
  assert.deepEqual(product.sku_options[0].stock, { status: "in_stock", quantity: 99 });
  assert.deepEqual(product.sku_options[0].image_urls, ["https://cbu01.alicdn.com/img/ibank/sku-pink.jpg"]);
  assert.equal(product.sku_options[0].evidence_source, "embedded_sku_map");
  assert.equal(product.sku_options[0].complete, true);
  assert.deepEqual(product.images, [productImage], "only full-size product images may be submitted");
  assert.equal(product.price.currency, "CNY");
  assert.equal(product.price.visible_text, "¥12.80");
  assert.equal(product.domestic_shipping_evidence.fee, 0);
  process.stdout.write("supplier content public fields: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
