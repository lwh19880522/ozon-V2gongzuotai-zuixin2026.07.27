"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const heartbeats = [];
const sessionValues = new Map();

const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    onMessage: { addListener() {} },
    async sendMessage() { return { ok: true }; },
  },
};

const productEvidence = {
  missingPublicFields() { return []; },
  blockingCandidateFields(missing) { return missing; },
  hasUsedProductId() { return false; },
  isExcludedProductId() { return false; },
  matchesQueryIntent() { return true; },
};

const document = {
  title: "Ozon product",
  body: { textContent: "Ozon product", scrollHeight: 1000 },
  querySelector(selector) {
    if (selector === "h1") return { textContent: "Ozon product" };
    return null;
  },
  querySelectorAll() { return []; },
};

const sessionStorage = {
  getItem(key) { return sessionValues.has(key) ? sessionValues.get(key) : null; },
  setItem(key, value) { sessionValues.set(key, String(value)); },
  removeItem(key) { sessionValues.delete(key); },
};

const location = { href: "https://www.ozon.ru/product/test-100/" };

const context = vm.createContext({
  chrome,
  console,
  document,
  location,
  sessionStorage,
  fetch: async (url, options = {}) => {
    if (String(url).endsWith("/api/browser-bridge/heartbeat")) {
      heartbeats.push(JSON.parse(options.body));
    }
    return { ok: true, json: async () => ({ ok: true, code: "ok", data: {} }) };
  },
  setTimeout,
  clearTimeout,
  URL,
  Date,
  Promise,
  JSON,
  window: { scrollTo() {} },
  OzonV2ProductEvidence: productEvidence,
});

const contentPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "content.js");
const source = fs.readFileSync(contentPath, "utf8").replace(/\ntriggerRun\(\);\s*$/, "\n");
vm.runInContext(source, context, { filename: contentPath });

function snapshot(productId = "100") {
  return {
    product_id: productId,
    sku_id: `sku-${productId}`,
    url: `https://www.ozon.ru/product/test-${productId}/`,
    title: `Product ${productId}`,
    seller_name: "China seller",
    seller_url: "https://www.ozon.ru/seller/test/",
    seller_evidence: "seller evidence",
    delivery_evidence: "delivery from China",
    brand: "Test",
    selected_options: { color: "black" },
    price: "1000",
    availability: "in_stock",
    main_gallery_images: ["https://cdn.example/main.jpg"],
    selected_sku_images: ["https://cdn.example/sku.jpg"],
    detail_page_images: ["https://cdn.example/detail.jpg"],
    category_path: ["Home", "Test"],
    leaf_category: "Test",
    category_url: "https://www.ozon.ru/category/test-1/",
    category_id: "1",
    attributes: { Material: "Steel" },
    rating: "4.9",
    review_count: 10,
    currency: "RUB",
  };
}

function progressOf(stage) {
  const item = [...heartbeats].reverse().find((heartbeat) => heartbeat.stage === stage);
  assert.ok(item, `missing heartbeat stage: ${stage}`);
  return item.details && item.details.collection_progress;
}

function assertProgress(value, expected) {
  assert.deepEqual(JSON.parse(JSON.stringify(value)), expected);
  for (const count of Object.values(value)) {
    assert.equal(Number.isInteger(count) && count >= 0, true);
  }
  assert.ok(value.processed_count <= value.total_count);
}

(async () => {
  assertProgress(context.collectionProgress({ candidates: [{}, {}] }, 5), {
    total_count: 5,
    processed_count: 2,
    success_count: 2,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 3,
  });
  assertProgress(context.collectionProgress({ candidates: [{}, {}, {}, {}] }, 3), {
    total_count: 3,
    processed_count: 3,
    success_count: 3,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 0,
  });

  heartbeats.length = 0;
  const reusableTask = {
    data: {
      run_id: "wb-reusable",
      dispatch_token: "dispatch-reusable",
      ingest_url: "/api/batches/wb-reusable/ozon-collection",
      contract: { payload: { excluded_ozon_product_ids: [], seeds: [{
        seed_id: "seed-reusable",
        source_text_zh: "测试商品",
        ozon_query_terms_ru: ["test"],
        public_product_snapshot: snapshot("101"),
        domestic_seller_decision: {
          is_chinese_domestic_seller: true,
          confidence: "high",
          signals: [],
        },
      }] } },
    },
  };
  await context.runOzonCollection(reusableTask);
  assertProgress(progressOf("snapshot_reused"), {
    total_count: 1,
    processed_count: 1,
    success_count: 1,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 0,
  });
  assertProgress(progressOf("submitting"), {
    total_count: 1,
    processed_count: 1,
    success_count: 1,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 0,
  });
  assertProgress(progressOf("submitted"), {
    total_count: 1,
    processed_count: 1,
    success_count: 1,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 0,
  });

  heartbeats.length = 0;
  const rejectedState = {
    candidates: [],
    searchEvidence: { productLinks: [{ href: "https://www.ozon.ru/product/a-1/" }] },
    productLinkIndex: 0,
    rejectedCandidates: [],
  };
  await context.requireChineseCrossBorderDetail(
    rejectedState,
    "wb-exhausted",
    "ozon_collection",
    "bridge.ozon_collection",
    {
      sellerDecision: { is_chinese_domestic_seller: false, confidence: "high", signals: [] },
      missingFields: ["seller_origin"],
    },
    "seed-exhausted",
    3,
  );
  assertProgress(progressOf("no_cross_border_candidate"), {
    total_count: 3,
    processed_count: 0,
    success_count: 0,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 3,
  });

  heartbeats.length = 0;
  await context.requireChineseCrossBorderDetail(
    {
      candidates: [],
      searchEvidence: { productLinks: [{ href: "https://www.ozon.ru/product/a-1/" }] },
      productLinkIndex: 0,
      rejectedCandidates: [],
    },
    "wb-template",
    "ozon_attribute_template",
    "bridge.ozon_attribute_template",
    {
      sellerDecision: { is_chinese_domestic_seller: false, confidence: "high", signals: [] },
      missingFields: ["seller_origin"],
    },
    "seed-template",
  );
  assert.equal(progressOf("no_cross_border_candidate"), undefined, "template heartbeats must not expose Ozon progress");

  heartbeats.length = 0;
  sessionStorage.setItem("ozon_v2_browser_bridge_state", JSON.stringify({
    schemaVersion: 5,
    runId: "wb-normal",
    taskType: "ozon_collection",
    dispatchToken: "dispatch-normal",
    seedIndex: 0,
    stage: "detail",
    candidates: [],
    searchEvidence: { url: "https://www.ozon.ru/search/", title: "search", productLinks: [] },
    productLinkIndex: 0,
    rejectedCandidates: [],
  }));
  context.waitForDetailEvidence = async () => ({
    snapshot: snapshot("102"),
    url: "https://www.ozon.ru/product/test-102/",
    title: "Product 102",
    sellerDecision: { is_chinese_domestic_seller: true, confidence: "high", signals: [] },
    missingFields: [],
    hasChallenge: false,
  });
  await context.runOzonCollection({
    data: {
      run_id: "wb-normal",
      dispatch_token: "dispatch-normal",
      ingest_url: "/api/batches/wb-normal/ozon-collection",
      contract: { payload: { excluded_ozon_product_ids: [], seeds: [{
        seed_id: "seed-normal",
        source_text_zh: "正常商品",
        ozon_query_terms_ru: ["normal"],
      }] } },
    },
  });
  assertProgress(progressOf("submitted"), {
    total_count: 1,
    processed_count: 1,
    success_count: 1,
    failure_count: 0,
    replacement_count: 0,
    pending_count: 0,
  });

  process.stdout.write("browser Ozon collection progress: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
