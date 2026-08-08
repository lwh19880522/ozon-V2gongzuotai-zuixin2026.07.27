"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const heartbeats = [];
const checkpointRequests = [];
const finalRequests = [];
const sessionValues = new Map();
let failNextCheckpoint = false;

const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    onMessage: { addListener() {} },
    async sendMessage() { return { ok: true }; },
  },
};

const productEvidence = {
  missingPublicFields(snapshot) {
    const missing = [];
    if (!snapshot || !snapshot.rating) missing.push("rating");
    if (!snapshot || !Number.isInteger(snapshot.review_count)) missing.push("review_count");
    return missing;
  },
  blockingCandidateFields(missing) {
    return (missing || []).filter((field) => !["rating", "review_count"].includes(field));
  },
  hasUsedProductId() { return false; },
  isExcludedProductId() { return false; },
  matchesQueryIntent() { return true; },
  evaluateSeedSubject(contract) {
    return { seed_id: String((contract || {}).seed_id || ""), accepted: true };
  },
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
    if (String(url).endsWith("/ozon-collection-progress")) {
      const body = JSON.parse(options.body);
      checkpointRequests.push(body);
      if (failNextCheckpoint) {
        failNextCheckpoint = false;
        return {
          ok: false,
          json: async () => ({ ok: false, code: "checkpoint.failed", message: "checkpoint failed" }),
        };
      }
    }
    if (String(url).endsWith("/ozon-collection")) {
      finalRequests.push(JSON.parse(options.body));
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
  sessionStorage.setItem("ozon_v2_browser_bridge_state", JSON.stringify({
    schemaVersion: 5,
    runId: "wb-old-schema",
    taskType: "ozon_collection",
    dispatchToken: "dispatch-old-schema",
    seedIndex: 1,
    stage: "search",
    candidates: [{ seed_id: "unsafe-old-candidate", ozon_product_id: "old-product" }],
  }));
  assert.equal(
    context.loadState("wb-old-schema", "ozon_collection", "dispatch-old-schema"),
    null,
    "pre-checkpoint session state must be invalidated after the schema upgrade",
  );
  sessionStorage.removeItem("ozon_v2_browser_bridge_state");

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
      progress_url: "/api/batches/wb-reusable/ozon-collection-progress",
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
  const originSnapshotWithoutMarketSignals = snapshot("106");
  originSnapshotWithoutMarketSignals.rating = null;
  originSnapshotWithoutMarketSignals.review_count = null;
  await context.runOzonCollection({
    data: {
      run_id: "wb-reusable-origin",
      dispatch_token: "dispatch-reusable-origin",
      ingest_url: "/api/batches/wb-reusable-origin/ozon-collection",
      progress_url: "/api/batches/wb-reusable-origin/ozon-collection-progress",
      contract: { payload: { excluded_ozon_product_ids: [], seeds: [{
        seed_id: "seed-reusable-origin",
        source_text_zh: "金属落地式杂志收纳架",
        ozon_query_terms_ru: ["металлическая напольная полка для журналов"],
        public_product_snapshot: originSnapshotWithoutMarketSignals,
        domestic_seller_decision: {
          is_chinese_domestic_seller: true,
          confidence: "high",
          signals: [{ kind: "product_origin_china", raw_text: "Страна-изготовитель: Китай" }],
        },
      }] } },
    },
  });
  assertProgress(progressOf("snapshot_reused"), {
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
    schemaVersion: 6,
    runId: "wb-early-subject-reject",
    taskType: "ozon_collection",
    dispatchToken: "dispatch-early-subject-reject",
    seedIndex: 0,
    stage: "detail",
    candidates: [],
    searchEvidence: {
      url: "https://www.ozon.ru/search/",
      title: "search",
      productLinks: [{ href: "https://www.ozon.ru/product/wrong-1/", card_text: "Wrong product" }],
    },
    productLinkIndex: 0,
    rejectedCandidates: [],
  }));
  location.href = "https://www.ozon.ru/product/wrong-1/";
  const originalEvaluateSeedSubject = productEvidence.evaluateSeedSubject;
  productEvidence.evaluateSeedSubject = (_contract, candidate) => ({
    accepted: Boolean(candidate && candidate.product_id),
    matched_stems: [],
    missing_stems: ["core"],
  });
  let detailEvidenceCalls = 0;
  context.waitForDetailEvidence = async () => {
    detailEvidenceCalls += 1;
    return {
      snapshot: snapshot("101"),
      url: "https://www.ozon.ru/product/test-101/",
      title: "Product 101",
      sellerDecision: { is_chinese_domestic_seller: true, confidence: "high", signals: [] },
      missingFields: [],
      hasChallenge: false,
    };
  };
  await context.runOzonCollection({
    data: {
      run_id: "wb-early-subject-reject",
      dispatch_token: "dispatch-early-subject-reject",
      ingest_url: "/api/batches/wb-early-subject-reject/ozon-collection",
      progress_url: "/api/batches/wb-early-subject-reject/ozon-collection-progress",
      contract: { payload: { excluded_ozon_product_ids: [], seeds: [{
        seed_id: "seed-early-subject-reject",
        source_text_zh: "wrong",
        ozon_query_terms_ru: ["expected product"],
        seed_subject_contract: { seed_id: "seed-early-subject-reject", required_stems: ["core"] },
      }] } },
    },
  });
  productEvidence.evaluateSeedSubject = originalEvaluateSeedSubject;
  assert.equal(
    detailEvidenceCalls,
    1,
    "an incomplete heading or search card must not reject a candidate before the full detail snapshot is collected",
  );

  heartbeats.length = 0;
  sessionStorage.setItem("ozon_v2_browser_bridge_state", JSON.stringify({
    schemaVersion: 6,
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
      progress_url: "/api/batches/wb-normal/ozon-collection-progress",
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

  checkpointRequests.length = 0;
  finalRequests.length = 0;
  const completeCandidate = {
    seed_id: "seed-complete",
    ozon_product_id: "103",
    attributes: { Material: "Steel" },
  };
  sessionStorage.setItem("ozon_v2_browser_bridge_state", JSON.stringify({
    schemaVersion: 6,
    runId: "wb-resume",
    taskType: "ozon_collection",
    dispatchToken: "dispatch-resume",
    seedIndex: 1,
    stage: "detail",
    candidates: [],
    searchEvidence: { url: "https://www.ozon.ru/search/", title: "search", productLinks: [] },
    productLinkIndex: 0,
    rejectedCandidates: [],
  }));
  context.waitForDetailEvidence = async () => ({
    snapshot: snapshot("104"),
    url: "https://www.ozon.ru/product/test-104/",
    title: "Product 104",
    sellerDecision: { is_chinese_domestic_seller: true, confidence: "high", signals: [] },
    missingFields: [],
    hasChallenge: false,
  });
  await context.runOzonCollection({
    data: {
      run_id: "wb-resume",
      dispatch_token: "dispatch-resume",
      ingest_url: "/api/batches/wb-resume/ozon-collection",
      progress_url: "/api/batches/wb-resume/ozon-collection-progress",
      resume_candidates: [completeCandidate],
      contract: { payload: { excluded_ozon_product_ids: [], seeds: [
        { seed_id: "seed-complete", source_text_zh: "complete", ozon_query_terms_ru: ["complete"] },
        { seed_id: "seed-pending", source_text_zh: "pending", ozon_query_terms_ru: ["pending"] },
      ] } },
    },
  });
  assert.equal(checkpointRequests.length, 1, "only the unfinished seed must create a checkpoint request");
  assert.equal(checkpointRequests[0].ozon_candidate.seed_id, "seed-pending");
  assert.equal(finalRequests.length, 1);
  assert.deepEqual(
    finalRequests[0].ozon_candidates.map((item) => item.seed_id),
    ["seed-complete", "seed-pending"],
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(finalRequests[0].ozon_candidates[0].attributes)),
    { Material: "Steel" },
    "original Ozon attributes from the durable checkpoint must survive final submission",
  );

  checkpointRequests.length = 0;
  const finalCountBeforeFailure = finalRequests.length;
  sessionStorage.setItem("ozon_v2_browser_bridge_state", JSON.stringify({
    schemaVersion: 6,
    runId: "wb-checkpoint-failure",
    taskType: "ozon_collection",
    dispatchToken: "dispatch-checkpoint-failure",
    seedIndex: 0,
    stage: "detail",
    candidates: [],
    searchEvidence: { url: "https://www.ozon.ru/search/", title: "search", productLinks: [] },
    productLinkIndex: 0,
    rejectedCandidates: [],
  }));
  context.waitForDetailEvidence = async () => ({
    snapshot: snapshot("105"),
    url: "https://www.ozon.ru/product/test-105/",
    title: "Product 105",
    sellerDecision: { is_chinese_domestic_seller: true, confidence: "high", signals: [] },
    missingFields: [],
    hasChallenge: false,
  });
  failNextCheckpoint = true;
  await assert.rejects(
    context.runOzonCollection({
      data: {
        run_id: "wb-checkpoint-failure",
        dispatch_token: "dispatch-checkpoint-failure",
        ingest_url: "/api/batches/wb-checkpoint-failure/ozon-collection",
        progress_url: "/api/batches/wb-checkpoint-failure/ozon-collection-progress",
        resume_candidates: [],
        contract: { payload: { excluded_ozon_product_ids: [], seeds: [{
          seed_id: "seed-checkpoint-failure",
          source_text_zh: "failure",
          ozon_query_terms_ru: ["failure"],
        }] } },
      },
    }),
    /checkpoint failed/,
  );
  const safeState = JSON.parse(sessionStorage.getItem("ozon_v2_browser_bridge_state"));
  assert.equal(safeState.seedIndex, 0, "a failed checkpoint must not advance the seed cursor");
  assert.deepEqual(safeState.candidates, [], "a failed checkpoint must not count the candidate as durable");
  assert.equal(finalRequests.length, finalCountBeforeFailure, "a failed checkpoint must block final submission");

  sessionStorage.removeItem("ozon_v2_browser_bridge_state");
  location.href = "https://www.ozon.ru/search/?from_global=true&text=old-seed-query";
  context.isSearchEvidencePage = () => true;
  context.waitForSearchEvidence = async () => {
    throw new Error("a stale search page must not be scanned for a replacement seed");
  };
  const staleSearchResult = await context.runOzonCollection({
    data: {
      run_id: "wb-stale-search",
      dispatch_token: "dispatch-stale-search",
      ingest_url: "/api/batches/wb-stale-search/ozon-collection",
      progress_url: "/api/batches/wb-stale-search/ozon-collection-progress",
      contract: { payload: { excluded_ozon_product_ids: [], seeds: [{
        seed_id: "seed-stale-search",
        source_text_zh: "replacement seed",
        ozon_query_terms_ru: ["new seed query"],
        seed_subject_contract: { seed_id: "seed-stale-search" },
      }] } },
    },
  });
  assert.equal(staleSearchResult.ok, true);
  assert.equal(staleSearchResult.code, "bridge.ozon_collection_navigating_search");
  assert.equal(location.href, context.searchUrl("new seed query"));

  heartbeats.length = 0;
  sessionStorage.removeItem("ozon_v2_browser_bridge_state");
  location.href = context.searchUrl("card inference");
  context.isSearchEvidencePage = () => true;
  context.waitForSearchEvidence = async () => ({
    productLinks: [{
      href: "https://www.ozon.ru/product/card-inference-1/",
      text: "Incomplete card title",
      card_text: "Incomplete search card",
    }],
    allProductLinkCount: 1,
    hasChallenge: false,
  });
  productEvidence.evaluateSeedSubject = (_contract, candidate) => ({
    accepted: Boolean(candidate && candidate.product_id),
    matched_stems: [],
    missing_stems: ["core"],
  });
  const cardInferenceResult = await context.runOzonCollection({
    data: {
      run_id: "wb-card-inference",
      dispatch_token: "dispatch-card-inference",
      ingest_url: "/api/batches/wb-card-inference/ozon-collection",
      progress_url: "/api/batches/wb-card-inference/ozon-collection-progress",
      contract: { payload: { excluded_ozon_product_ids: [], seeds: [{
        seed_id: "seed-card-inference",
        source_text_zh: "card inference",
        ozon_query_terms_ru: ["card inference"],
        seed_subject_contract: { seed_id: "seed-card-inference", required_stems: ["core"] },
      }] } },
    },
  });
  productEvidence.evaluateSeedSubject = originalEvaluateSeedSubject;
  assert.equal(cardInferenceResult.ok, true);
  assert.equal(cardInferenceResult.code, "bridge.ozon_collection_navigating_detail");
  assert.equal(location.href, "https://www.ozon.ru/product/card-inference-1/");

  heartbeats.length = 0;
  sessionStorage.removeItem("ozon_v2_browser_bridge_state");
  location.href = context.searchUrl("no match");
  context.isSearchEvidencePage = () => true;
  context.waitForSearchEvidence = async () => ({
    productLinks: [],
    allProductLinkCount: 12,
    hasChallenge: false,
  });
  const noMatchResult = await context.runOzonCollection({
    data: {
      run_id: "wb-search-no-match",
      dispatch_token: "dispatch-search-no-match",
      ingest_url: "/api/batches/wb-search-no-match/ozon-collection",
      progress_url: "/api/batches/wb-search-no-match/ozon-collection-progress",
      contract: { payload: { excluded_ozon_product_ids: [], seeds: [{
        seed_id: "seed-search-no-match",
        source_text_zh: "no match",
        ozon_query_terms_ru: ["no match"],
        seed_subject_contract: { seed_id: "seed-search-no-match" },
      }] } },
    },
  });
  assert.equal(noMatchResult.ok, false);
  assert.equal(noMatchResult.code, "bridge.ozon_collection.no_relevant_search_candidate");
  const noMatchHeartbeat = heartbeats.find(
    (item) => item.code === "bridge.ozon_collection.no_relevant_search_candidate",
  );
  assert.ok(noMatchHeartbeat, "a terminal no-match search must be reported to the workbench");
  assert.equal(noMatchHeartbeat.details.seed_id, "seed-search-no-match");
  assert.equal(noMatchHeartbeat.details.search_result_count, 12);

  process.stdout.write("browser Ozon collection progress: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
