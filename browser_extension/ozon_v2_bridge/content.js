"use strict";

const BASE_URL = "http://127.0.0.1:8765";
const STATE_KEY = "ozon_v2_browser_bridge_state";
const STATE_SCHEMA_VERSION = 6;
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const API_TIMEOUT_MS = Number(globalThis.OZON_V2_API_TIMEOUT_MS) || 15000;
let activeRunPromise = null;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "ozon_v2_content_ping") {
    sendResponse({ ok: true, extension_version: EXTENSION_VERSION });
    return false;
  }
  if (message && message.type === "ozon_v2_run_task") {
    triggerRun()
      .then((result) => sendResponse({ ok: Boolean(result && result.ok), result }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  return false;
});

function absoluteUrl(path) {
  return `${BASE_URL}${path}`;
}

async function api(path, options = {}) {
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timeoutError = new Error(`Workbench request timed out after ${API_TIMEOUT_MS} ms.`);
  let timedOut = false;
  let timeoutId = null;
  const timeoutPromise = new Promise((_resolve, reject) => {
    timeoutId = setTimeout(() => {
      timedOut = true;
      if (controller) controller.abort(timeoutError);
      reject(timeoutError);
    }, API_TIMEOUT_MS);
  });
  try {
    const requestOptions = {
      headers: { "Content-Type": "application/json" },
      ...options,
    };
    if (controller) requestOptions.signal = controller.signal;
    const response = await Promise.race([
      fetch(absoluteUrl(path), requestOptions),
      timeoutPromise,
    ]);
    const body = await response.json();
    if (!response.ok || body.ok === false) {
      throw new Error(body.message || body.code || "Ozon V2 bridge API failed.");
    }
    return body;
  } catch (error) {
    if (timedOut || (controller && controller.signal.aborted)) throw timeoutError;
    throw error;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
}

async function browserTask() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "ozon_v2_get_current_task" });
    if (response && response.ok && response.task) {
      return response.task;
    }
  } catch (_) {
    // Fallback to the workbench active task endpoint if the background worker is restarting.
  }
  return await api("/api/browser-task/active");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function heartbeat(payload = {}) {
  try {
    await fetch(absoluteUrl("/api/browser-bridge/heartbeat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bridge_id: "ozon_v2_browser_extension",
        source: "ozon_content_script",
        extension_version: EXTENSION_VERSION,
        url: location.href,
        run_id: payload.run_id || null,
        task_type: payload.task_type || "ozon_attribute_template",
        stage: payload.stage || null,
        code: payload.code || null,
        message: payload.message || null,
        details: payload.details || null,
      }),
    });
  } catch (_) {
    // Ozon collection should not fail only because the local workbench restarted.
  }
}

function collectionProgress(state, totalCount) {
  const total = Math.max(0, Number.parseInt(totalCount, 10) || 0);
  const success = Math.max(0, Array.isArray(state && state.candidates) ? state.candidates.length : 0);
  const processed = Math.min(total, success);
  return {
    total_count: total,
    processed_count: processed,
    success_count: Math.min(success, total),
    failure_count: 0,
    replacement_count: 0,
    pending_count: Math.max(total - processed, 0),
  };
}

function collectionProgressDetails(state, totalCount, details = {}) {
  return {
    ...details,
    collection_progress: collectionProgress(state, totalCount),
  };
}

function taskProgressDetails(state, taskType, totalCount, details = {}) {
  return taskType === "ozon_collection" && totalCount !== null && totalCount !== undefined
    ? collectionProgressDetails(state, totalCount, details)
    : details;
}

function normalizeUrl(value) {
  try {
    const url = new URL(value, "https://www.ozon.ru");
    url.search = "";
    return url.toString();
  } catch (_) {
    return "";
  }
}

function textOf(node, limit = 240) {
  return (node && node.textContent ? node.textContent : "").trim().replace(/\s+/g, " ").slice(0, limit);
}

function hasChallenge() {
  const text = `${location.href} ${document.title} ${textOf(document.body, 1600)}`;
  return /captcha|robot|fab_chlg|challenge|checking|enable JavaScript|not a robot|VPN/i.test(text);
}

function links(selector, limit = 12) {
  const seen = new Set();
  return Array.from(document.querySelectorAll(selector))
    .map((node) => ({ href: normalizeUrl(node.href), text: textOf(node, 160) }))
    .filter((item) => item.href)
    .filter((item) => {
      if (seen.has(item.href)) return false;
      seen.add(item.href);
      return true;
    })
    .slice(0, limit);
}

function nearestProductCardText(anchor) {
  let node = anchor;
  let best = textOf(anchor, 240);
  for (let depth = 0; depth < 6 && node.parentElement; depth += 1) {
    node = node.parentElement;
    const text = textOf(node, 1400);
    if (text.length > 1400) break;
    if (text.length >= best.length) best = text;
    if (/товар\w*\s+из\s+китая|достав\w*[^.!?]{0,80}из\s+китая|ozon\s*global/iu.test(text)) break;
  }
  return best;
}

function searchProductLinks(limit = 24) {
  const byHref = new Map();
  for (const anchor of document.querySelectorAll('a[href*="/product/"]')) {
    const href = normalizeUrl(anchor.href);
    if (!href) continue;
    const item = {
      href,
      text: textOf(anchor, 240),
      card_text: nearestProductCardText(anchor),
    };
    const previous = byHref.get(href);
    if (!previous || item.card_text.length > previous.card_text.length) byHref.set(href, item);
  }
  const all = Array.from(byHref.values()).slice(0, limit);
  const evidence = globalThis.OzonV2ProductEvidence;
  const qualified = evidence && typeof evidence.prioritizeCrossBorderSearchLinks === "function"
    ? evidence.prioritizeCrossBorderSearchLinks(all)
    : all.filter((item) => /из\s+китая|ozon\s*global/iu.test(item.card_text));
  return { all, qualified };
}

async function requireChineseCrossBorderDetail(
  state,
  runId,
  taskType,
  codePrefix,
  detailEvidence,
  seedId,
  totalCount = null,
) {
  const decision = detailEvidence.sellerDecision || {
    is_chinese_domestic_seller: null,
    confidence: "unknown",
    signals: [],
    evidence: "Seller decision evidence was not collected.",
  };
  const missingFields = detailEvidence.missingFields || ["public_product_snapshot"];
  const evidence = globalThis.OzonV2ProductEvidence;
  const blockingFields = evidence && typeof evidence.blockingCandidateFields === "function"
    ? evidence.blockingCandidateFields(missingFields, decision)
    : missingFields;
  if (
    decision.is_chinese_domestic_seller === true
    && decision.confidence === "high"
    && blockingFields.length === 0
  ) {
    return { accepted: true, decision, missingFields };
  }

  const linksSeen = (state.searchEvidence && state.searchEvidence.productLinks) || [];
  const currentLink = linksSeen[state.productLinkIndex] || {};
  const candidateTitle = document.querySelector("h1") ? textOf(document.querySelector("h1"), 240) : document.title;
  const rejectionDetails = {
    seed_id: seedId,
    candidate_index: state.productLinkIndex,
    url: normalizeUrl(location.href),
    title: candidateTitle,
    decision,
    missing_fields: blockingFields,
    public_missing_fields: missingFields,
    timed_out: Boolean(detailEvidence.timedOut),
  };
  state.rejectedCandidates = state.rejectedCandidates || [];
  state.rejectedCandidates.push({
    ...rejectionDetails,
    search_link: currentLink.href || null,
  });
  state.productLinkIndex += 1;
  const nextLink = linksSeen[state.productLinkIndex];
  saveState(state);

  if (nextLink) {
    await heartbeat({
      run_id: runId,
      task_type: taskType,
      stage: "candidate_rejected",
      code: `${codePrefix}.candidate_rejected`,
      message: `Candidate ${state.productLinkIndex} was not a complete verified Chinese cross-border product; checking the next result.`,
      details: taskProgressDetails(state, taskType, totalCount, rejectionDetails),
    });
    location.href = nextLink.href;
    return { accepted: false, navigating: true, decision };
  }

  await heartbeat({
    run_id: runId,
    task_type: taskType,
    stage: "no_cross_border_candidate",
    code: `${codePrefix}.no_cross_border_candidate`,
    message: "No complete verified Chinese cross-border product was found in the visible Ozon search candidates.",
    details: taskProgressDetails(state, taskType, totalCount, {
      seed_id: seedId,
      rejected_candidate_count: state.rejectedCandidates.length,
      rejected_candidates: state.rejectedCandidates,
    }),
  });
  return { accepted: false, navigating: false, decision };
}

function attributeFromLabel(label, index) {
  const normalized = String(label || "").trim();
  const key = normalized.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "_").replace(/^_+|_+$/g, "");
  return {
    attribute_id: key || `visible_attribute_${index + 1}`,
    attribute_label: normalized || `Visible attribute ${index + 1}`,
    attribute_type: "text",
    is_required: false,
    allowed_values: [],
    unit: null,
    group: "visible_attributes",
    example_value_when_visible: null,
  };
}

function searchUrl(query) {
  const evidence = globalThis.OzonV2ProductEvidence;
  const scopedQuery = evidence && typeof evidence.crossBorderSearchQuery === "function"
    ? evidence.crossBorderSearchQuery(query)
    : `${query} из Китая`.trim();
  return `https://www.ozon.ru/search/?from_global=true&text=${encodeURIComponent(scopedQuery)}&__rr=1`;
}

function isSearchEvidencePage() {
  if (location.href.includes("/search/")) return true;
  if (location.href.includes("/category/")) {
    try {
      return new URL(location.href).searchParams.has("text");
    } catch (_) {
      return true;
    }
  }
  return false;
}

async function waitForSearchEvidence(runId, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  let firstResultsAt = null;
  while (Date.now() < deadline) {
    const searchLinks = searchProductLinks(24);
    const categoryLinks = links('a[href*="/category/"]', 12);
    const challenged = hasChallenge();
    if (challenged) {
      return {
        url: location.href,
        title: document.title,
        productLinks: [],
        allProductLinkCount: searchLinks.all.length,
        categoryLinks,
        hasChallenge: true,
      };
    }
    if (searchLinks.all.length && firstResultsAt === null) firstResultsAt = Date.now();
    if (searchLinks.all.length >= 8 || (firstResultsAt !== null && Date.now() - firstResultsAt >= 5000)) {
      const productLinks = globalThis.OzonV2ProductEvidence
        && typeof globalThis.OzonV2ProductEvidence.selectSearchCandidates === "function"
        ? globalThis.OzonV2ProductEvidence.selectSearchCandidates(searchLinks.all, 8)
        : searchLinks.all.slice(0, 8);
      return {
        url: location.href,
        title: document.title,
        productLinks,
        allProductLinkCount: searchLinks.all.length,
        categoryLinks,
        hasChallenge: false,
        usedDetailFallback: searchLinks.qualified.length === 0 && productLinks.length > 0,
      };
    }
    await heartbeat({
      run_id: runId,
      stage: "waiting_search_evidence",
      code: "bridge.waiting_search",
      message: "Waiting for Ozon search evidence to render.",
    });
    await sleep(2000);
  }
  return {
    url: location.href,
    title: document.title,
    productLinks: globalThis.OzonV2ProductEvidence
      && typeof globalThis.OzonV2ProductEvidence.selectSearchCandidates === "function"
      ? globalThis.OzonV2ProductEvidence.selectSearchCandidates(searchProductLinks(24).all, 8)
      : searchProductLinks(24).all.slice(0, 8),
    allProductLinkCount: searchProductLinks(24).all.length,
    categoryLinks: links('a[href*="/category/"]', 12),
    hasChallenge: hasChallenge(),
    timedOut: true,
  };
}

function firstNode(selectors) {
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (node) return node;
  }
  return null;
}

function uniqueNodes(selectors) {
  const seen = new Set();
  const nodes = [];
  for (const selector of selectors) {
    for (const node of document.querySelectorAll(selector)) {
      if (!seen.has(node)) {
        seen.add(node);
        nodes.push(node);
      }
    }
  }
  return nodes;
}

function productJsonLd() {
  const evidence = globalThis.OzonV2ProductEvidence;
  if (!evidence || typeof evidence.parseProductJsonLd !== "function") return {};
  return evidence.parseProductJsonLd(
    Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map((node) => node.textContent || ""),
  );
}

function productCategoryLinks() {
  const selectors = [
    '[data-widget*="bread" i] a[href*="/category/"]',
    'nav[aria-label*="breadcrumb" i] a[href*="/category/"]',
    'ol a[href*="/category/"]',
  ];
  const seen = new Set();
  return uniqueNodes(selectors)
    .map((node) => ({ href: normalizeUrl(node.href), text: textOf(node, 160) }))
    .filter((item) => item.href && item.text && categoryIdFromUrl(item.href))
    .filter((item) => {
      if (seen.has(item.href)) return false;
      seen.add(item.href);
      return true;
    })
    .slice(0, 12);
}

function detailBlock(selectors, marker, limit = 2400) {
  const direct = uniqueNodes(selectors)
    .map((node) => ({ node, text: textOf(node, limit) }))
    .find((item) => item.text && marker.test(item.text));
  if (direct) return direct.text;
  const markerNode = Array.from(document.querySelectorAll("h2, h3, h4, div, span"))
    .find((node) => marker.test(textOf(node, 120)));
  if (!markerNode) return "";
  let node = markerNode;
  for (let depth = 0; depth < 4 && node.parentElement; depth += 1) {
    node = node.parentElement;
    const text = textOf(node, limit);
    if (text.length >= 40 && text.length <= limit) return text;
  }
  return textOf(markerNode, limit);
}

function realAttributeTable() {
  const evidence = globalThis.OzonV2ProductEvidence;
  if (!evidence || typeof evidence.normalizeAttributePairs !== "function") return {};
  const roots = uniqueNodes([
    '[data-widget*="character" i]',
    '[id*="character" i]',
    '[class*="characteristic" i]',
    'section[aria-label*="характер" i]',
  ]);
  const pairs = [];
  for (const root of roots) {
    for (const label of root.querySelectorAll("dt")) {
      const value = label.nextElementSibling;
      if (value) pairs.push([textOf(label, 120), textOf(value, 500)]);
    }
    for (const row of root.querySelectorAll("tr, li, [role='listitem']")) {
      const cells = Array.from(row.children).filter((node) => textOf(node, 500));
      if (cells.length >= 2) pairs.push([textOf(cells[0], 120), textOf(cells[1], 500)]);
    }
    for (const node of root.querySelectorAll("div")) {
      const children = Array.from(node.children).filter((child) => textOf(child, 500));
      if (children.length === 2) {
        const key = textOf(children[0], 120);
        const value = textOf(children[1], 500);
        if (key && value && key.length <= 120 && value.length <= 500) pairs.push([key, value]);
      }
    }
  }
  return evidence.normalizeAttributePairs(pairs);
}

function galleryImages(structuredProduct) {
  const evidence = globalThis.OzonV2ProductEvidence;
  if (!evidence || typeof evidence.normalizeMedia !== "function") return [];
  const domImages = uniqueNodes([
    '[data-widget*="gallery" i] img',
    '[data-widget*="image" i] img',
    'main img[src*="ozon"]',
  ]).map((node) => node.currentSrc || node.src || "");
  return evidence.normalizeMedia([...(structuredProduct.images || []), ...domImages], 24);
}

function sellerDetails() {
  const evidence = globalThis.OzonV2ProductEvidence;
  const sellerRoot = uniqueNodes(['[data-widget*="seller" i]', '[data-widget*="webCurrentSeller" i]'])
    .find((node) => /магазин|продавец|о магазине/iu.test(textOf(node, 2200)));
  const anchor = (sellerRoot && sellerRoot.querySelector('a[href*="/seller/"]')) || firstNode([
    'a[href*="/seller/"]',
  ]);
  const block = detailBlock(
    ['[data-widget*="seller" i]', '[data-widget*="webCurrentSeller" i]'],
    /магазин|продавец|о магазине/iu,
    2200,
  );
  if (!evidence || typeof evidence.parseSellerEvidence !== "function") {
    return { name: anchor ? textOf(anchor, 160) : null, url: anchor ? normalizeUrl(anchor.href) : null, raw_text: block };
  }
  return evidence.parseSellerEvidence({
    rawText: block,
    url: anchor ? normalizeUrl(anchor.href) : null,
    linkText: anchor ? textOf(anchor, 160) : "",
  });
}

function deliveryDetails() {
  const evidence = globalThis.OzonV2ProductEvidence;
  const raw = detailBlock(
    ['[data-widget*="delivery" i]', '[data-widget*="webCurrentSeller" i]', '[data-widget*="webAddToCart" i]'],
    /доставка и возврат|доставка|получени|товар\s+из\s+китая|ozon\s*global/iu,
    3000,
  );
  return evidence && typeof evidence.parseDeliveryEvidence === "function"
    ? evidence.parseDeliveryEvidence(raw)
    : { raw_text: raw, origin: null, time: null, fulfillment: null };
}

function selectedOptionsFrom(attributes, skuId) {
  const options = {};
  for (const [key, value] of Object.entries(attributes || {})) {
    if (/цвет|размер|модел|комплектац|количеств|единиц/iu.test(key)) options[key] = String(value);
  }
  if (!Object.keys(options).length && skuId) options.single_sku_id = String(skuId);
  return options;
}

function currentPublicSnapshot() {
  const structured = productJsonLd();
  const categoryLinks = productCategoryLinks();
  const categoryUrl = categoryLinks.length ? categoryLinks[categoryLinks.length - 1].href : null;
  const productId = productIdFromPage(location.href);
  const skuId = structured.sku_id || productId;
  const attributes = realAttributeTable();
  const images = galleryImages(structured);
  const seller = sellerDetails();
  const delivery = deliveryDetails();
  const price = structured.price || priceText();
  const currency = structured.currency || (price && /₽|руб/iu.test(price) ? "RUB" : null);
  const pageText = textOf(document.body, 12000);
  return {
    product_id: productId,
    url: normalizeUrl(location.href),
    title: structured.title || textOf(document.querySelector("h1"), 320) || null,
    sku_id: skuId,
    selected_options: selectedOptionsFrom(attributes, skuId),
    brand: structured.brand || null,
    description: structured.description || detailBlock(['[data-widget*="description" i]'], /описание/iu, 4000) || null,
    category_links: categoryLinks,
    category_path: categoryLinks.map((item) => item.text).join(" / ") || null,
    leaf_category: categoryLinks.length ? categoryLinks[categoryLinks.length - 1].text : null,
    category_url: categoryUrl,
    category_id: categoryIdFromUrl(categoryUrl),
    attributes,
    main_gallery_images: images.slice(0, 12),
    selected_sku_images: images.slice(0, 12),
    detail_page_images: images.slice(12, 24),
    price,
    currency,
    rating: structured.rating || ratingText(),
    review_count: structured.review_count != null ? structured.review_count : reviewCount(),
    availability: structured.availability || (pageText.includes("В корзину") ? "available" : null),
    seller_name: seller.name,
    seller_url: seller.url,
    seller_evidence: seller.raw_text,
    delivery_origin: delivery.origin,
    delivery_time: delivery.time,
    fulfillment_label: delivery.fulfillment,
    delivery_evidence: delivery.raw_text,
  };
}

function sellerDecisionForSnapshot(snapshot) {
  const classifier = globalThis.OzonV2SellerEvidence;
  if (!classifier || typeof classifier.classifyEvidence !== "function") {
    return { is_chinese_domestic_seller: null, confidence: "unknown", signals: [], evidence: "Classifier unavailable." };
  }
  return classifier.classifyEvidence({
    deliveryText: snapshot.delivery_evidence || "",
    globalText: `${snapshot.delivery_evidence || ""} ${snapshot.seller_evidence || ""}`,
    sellerText: snapshot.seller_evidence || "",
    attributeText: Object.entries(snapshot.attributes || {})
      .map(([key, value]) => `${key}: ${value}`)
      .join(" | "),
  });
}

function snapshotMissingFields(snapshot) {
  const evidence = globalThis.OzonV2ProductEvidence;
  return evidence && typeof evidence.missingPublicFields === "function"
    ? evidence.missingPublicFields(snapshot)
    : ["product_evidence_module"];
}

function snapshotFingerprint(snapshot, decision) {
  return JSON.stringify({
    product_id: snapshot.product_id,
    sku_id: snapshot.sku_id,
    attributes: snapshot.attributes,
    images: snapshot.main_gallery_images,
    seller_url: snapshot.seller_url,
    delivery: snapshot.delivery_evidence,
    decision,
  });
}

function expandDetailSections() {
  for (const node of document.querySelectorAll("button, [role='button']")) {
    if (/показать все|развернуть|все характеристики|перейти к описанию/iu.test(textOf(node, 120))) {
      try { node.click(); } catch (_) { /* The next polling pass will try again. */ }
    }
  }
}

async function waitForDetailEvidence(runId, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let previousFingerprint = null;
  let latest = null;
  let accumulatedSnapshot = null;
  let expanded = false;
  while (Date.now() < deadline) {
    const challenged = hasChallenge();
    if (challenged) return { hasChallenge: true, url: location.href };
    const visibleSnapshot = currentPublicSnapshot();
    const evidence = globalThis.OzonV2ProductEvidence;
    const snapshot = evidence && typeof evidence.mergePublicSnapshots === "function"
      ? evidence.mergePublicSnapshots(accumulatedSnapshot || {}, visibleSnapshot)
      : { ...(accumulatedSnapshot || {}), ...visibleSnapshot };
    accumulatedSnapshot = snapshot;
    const sellerDecision = sellerDecisionForSnapshot(snapshot);
    const missingFields = snapshotMissingFields(snapshot);
    const blockingFields = evidence && typeof evidence.blockingCandidateFields === "function"
      ? evidence.blockingCandidateFields(missingFields, sellerDecision)
      : missingFields;
    latest = {
      url: snapshot.url,
      title: snapshot.title,
      categoryLinks: snapshot.category_links,
      attributeLabels: Object.keys(snapshot.attributes || {}),
      snapshot,
      sellerDecision,
      missingFields,
      blockingFields,
      hasChallenge: false,
    };
    if (sellerDecision.confidence === "high" && sellerDecision.is_chinese_domestic_seller === false) return latest;
    const fingerprint = snapshotFingerprint(snapshot, sellerDecision);
    if (!blockingFields.length && sellerDecision.confidence === "high" && sellerDecision.is_chinese_domestic_seller === true) {
      if (fingerprint === previousFingerprint) return latest;
      previousFingerprint = fingerprint;
    } else {
      previousFingerprint = null;
    }
    await heartbeat({
      run_id: runId,
      stage: "waiting_detail_evidence",
      code: "bridge.waiting_detail",
      message: `Waiting for required Ozon detail evidence: ${blockingFields.join(", ") || "seller_origin"}.`,
      details: { missing_fields: blockingFields, public_missing_fields: missingFields, seller_decision: sellerDecision },
    });
    if (!expanded) {
      expandDetailSections();
      window.scrollTo({ top: Math.floor(document.body.scrollHeight * 0.55), behavior: "instant" });
      expanded = true;
      await sleep(800);
    } else {
      await sleep(1000);
    }
  }
  return { ...(latest || {}), timedOut: true };
}

function loadState(runId, taskType, dispatchToken, seeds = []) {
  try {
    const state = JSON.parse(sessionStorage.getItem(STATE_KEY) || "{}");
    const matchesTask = state.schemaVersion === STATE_SCHEMA_VERSION
      && state.runId === runId
      && (!taskType || state.taskType === taskType);
    if (!matchesTask) return null;
    if (state.dispatchToken === dispatchToken) return state;
    const evidence = globalThis.OzonV2ProductEvidence;
    if (
      taskType === "ozon_attribute_template"
      && evidence
      && typeof evidence.reconcileTemplateProgress === "function"
    ) {
      return evidence.reconcileTemplateProgress(state, seeds, dispatchToken);
    }
    return null;
  } catch (_) {
    return null;
  }
}

function saveState(state) {
  sessionStorage.setItem(STATE_KEY, JSON.stringify({ ...state, schemaVersion: STATE_SCHEMA_VERSION }));
}

function buildTemplate(seed, query, searchEvidence, detailEvidence, sellerDecision) {
  const snapshot = detailEvidence.snapshot;
  const categoryLinks = snapshot.category_links || [];
  const productLinks = searchEvidence.productLinks || [];
  const visibleSchemaGuess = Object.keys(snapshot.attributes || {})
    .slice(0, 12)
    .map(attributeFromLabel);
  return {
    seed_id: seed.seed_id,
    source_query: query,
    category_candidates: [
      {
        category_path: snapshot.category_path,
        leaf_category: snapshot.leaf_category,
        category_url: snapshot.category_url,
        category_id: snapshot.category_id,
        confidence: "high",
        source_evidence: "Ozon product breadcrumb evidence",
      },
    ],
    public_attribute_evidence: {
      attribute_labels: Object.keys(snapshot.attributes || {}),
      attribute_table: snapshot.attributes,
      visible_public_schema_guess: visibleSchemaGuess,
      note: "Public Ozon page evidence only. Server must replace upload_attribute_schema with Seller API category template.",
    },
    evidence: {
      search_url: searchEvidence.url,
      search_title: searchEvidence.title,
      product_url: detailEvidence.url,
      product_title: detailEvidence.title,
      public_product_snapshot: snapshot,
      product_links_seen: productLinks.map((item) => item.href).slice(0, 8),
      bridge_url: location.href,
      has_challenge: hasChallenge(),
      domestic_seller_decision: sellerDecision,
    },
  };
}

function productIdFromPage(url) {
  const bodyText = textOf(document.body, 5000);
  const articleMatch = bodyText.match(/Артикул:\s*(\d+)/i);
  if (articleMatch) return articleMatch[1];
  const urlMatch = String(url || location.href).match(/-(\d+)\/?$/);
  if (urlMatch) return urlMatch[1];
  return normalizeUrl(url || location.href);
}

function categoryIdFromUrl(url) {
  const match = String(url || "").match(/-(\d+)\/?$/);
  return match ? match[1] : String(url || "").trim();
}

function priceText() {
  const text = textOf(document.body, 4000);
  const match = text.match(/(?:от\s*)?\d[\d\s]*(?:₽|руб)/i);
  return match ? match[0].replace(/\s+/g, " ").trim() : null;
}

function reviewCount() {
  const text = textOf(document.body, 4000);
  const match = text.match(/([\d\s]+)\s+отзыв/i);
  return match ? Number(match[1].replace(/\s+/g, "")) || null : null;
}

function ratingText() {
  const text = textOf(document.body, 2000);
  const match = text.match(/\b[1-5][.,]\d\b/);
  return match ? match[0].replace(",", ".") : null;
}

function buildOzonCandidate(seed, query, searchEvidence, detailEvidence, sellerDecision) {
  const snapshot = detailEvidence.snapshot;
  return {
    seed_id: seed.seed_id,
    seed_title_or_keyword: seed.source_text_zh || seed.title_or_keyword || "",
    seed_source_language: "zh-CN",
    ozon_query_terms_ru: seed.ozon_query_terms_ru || [query],
    ozon_product_id: String(snapshot.product_id),
    ozon_url: snapshot.url,
    title: snapshot.title,
    seller_name: snapshot.seller_name,
    brand: snapshot.brand,
    seller_url: snapshot.seller_url,
    seller_evidence: { raw_text: `${snapshot.seller_evidence} | ${snapshot.delivery_evidence}` },
    target_sku: {
      sku_id: String(snapshot.sku_id),
      selected_options: snapshot.selected_options,
      price: snapshot.price,
      availability: snapshot.availability,
      image_reference: snapshot.main_gallery_images[0] || null,
      notes: "Single visible SKU captured from Ozon detail page.",
    },
    selected_sku_media: {
      main_gallery_images: snapshot.main_gallery_images,
      selected_sku_images: snapshot.selected_sku_images,
      detail_page_images: snapshot.detail_page_images,
      selected_option_label_when_visible: JSON.stringify(snapshot.selected_options),
      image_role: "single_sku_reference_and_style_evidence",
      style_notes: {
        source: "ozon_public_detail_page",
        purpose: "later image regeneration should learn layout, composition, and visual style without copying text verbatim",
      },
      source_url_or_reference: snapshot.url,
    },
    category_path: snapshot.category_path,
    leaf_category: snapshot.leaf_category,
    category_url: snapshot.category_url,
    category_id: snapshot.category_id,
    price: snapshot.price,
    currency: snapshot.currency,
    rating: snapshot.rating,
    review_count: snapshot.review_count,
    delivery_origin: snapshot.delivery_origin,
    delivery_time: snapshot.delivery_time,
    fulfillment_label: snapshot.fulfillment_label,
    attributes: snapshot.attributes,
    domestic_seller_decision: sellerDecision,
    hot_product_evidence: {
      rating: snapshot.rating,
      review_count: snapshot.review_count,
      search_result_position: searchEvidence.productLinks ? 1 : null,
      visible_badges: textOf(document.body, 4000).match(/Распродажа|Цена что надо|Оригинал/gi) || [],
    },
    content_score_evidence: {
      title_raw: snapshot.title,
      title_length: snapshot.title.length,
      description_or_rich_content_blocks: snapshot.description ? [snapshot.description] : [],
      attribute_table: snapshot.attributes,
      required_or_visible_attributes: Object.keys(snapshot.attributes || {}),
      category_path: snapshot.category_path,
      leaf_category: snapshot.leaf_category,
      brand: snapshot.brand,
      price: snapshot.price,
      currency: snapshot.currency,
      old_price_or_discount: null,
      rating: snapshot.rating,
      review_count: snapshot.review_count,
      seller_name: snapshot.seller_name,
      seller_url: snapshot.seller_url,
      delivery_origin: snapshot.delivery_origin,
      delivery_time: snapshot.delivery_time,
      fulfillment_label: snapshot.fulfillment_label,
      main_gallery_images: snapshot.main_gallery_images,
      selected_sku_images: snapshot.selected_sku_images,
      detail_page_images: snapshot.detail_page_images,
      image_style_notes: {
        composition: "captured from public Ozon detail gallery",
        text_overlay: "record only; rewrite/regenerate later",
      },
      missing_fields: {
        monthly_sales: "external analytics unavailable",
        monthly_revenue: "external analytics unavailable",
        advertising_cost_share: "external analytics unavailable",
      },
    },
    collector_notes: "Captured by Ozon V2 browser bridge from a real browser session.",
  };
}

async function submit(runId, ingestUrl, templates) {
  await heartbeat({
    run_id: runId,
    stage: "submitting",
    code: "bridge.submitting",
    message: "Submitting Ozon attribute template evidence to workbench.",
  });
  const result = await api(ingestUrl, {
    method: "POST",
    body: JSON.stringify({
      run_id: runId,
      worker: "workbench_browser_bridge",
      source: "ozon_browser_extension_content_script",
      seed_templates: templates,
    }),
  });
  sessionStorage.removeItem(STATE_KEY);
  await heartbeat({
    run_id: runId,
    stage: "submitted",
    code: "bridge.submitted",
    message: "Ozon attribute template evidence submitted to workbench.",
  });
  return result;
}

async function submitOzonCollection(runId, ingestUrl, state, totalCount) {
  await heartbeat({
    run_id: runId,
    task_type: "ozon_collection",
    stage: "submitting",
    code: "bridge.ozon_collection_submitting",
    message: "Submitting Ozon product collection evidence to workbench.",
    details: collectionProgressDetails(state, totalCount),
  });
  const result = await api(ingestUrl, {
    method: "POST",
    body: JSON.stringify({
      run_id: runId,
      worker: "workbench_browser_bridge",
      source: "ozon_browser_extension_content_script",
      ozon_candidates: state.candidates,
    }),
  });
  sessionStorage.removeItem(STATE_KEY);
  await heartbeat({
    run_id: runId,
    task_type: "ozon_collection",
    stage: "submitted",
    code: "bridge.ozon_collection_submitted",
    message: "Ozon product collection evidence submitted to workbench.",
    details: collectionProgressDetails(state, totalCount),
  });
  return result;
}

function reconcileOzonCollectionState(task, currentState = null) {
  const seeds = task.data.contract.payload.seeds || [];
  const excluded = new Set(
    (task.data.contract.payload.excluded_ozon_product_ids || []).map(String),
  );
  const expectedSeedIds = new Set(seeds.map((seed) => String(seed.seed_id || "")));
  const bySeed = new Map();
  const usedProductIds = new Set();
  const sourceCandidates = [
    ...(Array.isArray(task.data.resume_candidates) ? task.data.resume_candidates : []),
    ...(
      currentState && Array.isArray(currentState.candidates)
        ? currentState.candidates
        : []
    ),
  ];
  for (const candidate of sourceCandidates) {
    if (!candidate || typeof candidate !== "object") continue;
    const seedId = String(candidate.seed_id || "");
    const productId = String(candidate.ozon_product_id || "");
    if (!expectedSeedIds.has(seedId) || !productId || excluded.has(productId)) continue;
    const previous = bySeed.get(seedId);
    const previousProductId = String((previous && previous.ozon_product_id) || "");
    if (usedProductIds.has(productId) && previousProductId !== productId) continue;
    if (previousProductId) usedProductIds.delete(previousProductId);
    bySeed.set(seedId, candidate);
    usedProductIds.add(productId);
  }
  const candidates = seeds
    .map((seed) => bySeed.get(String(seed.seed_id || "")))
    .filter(Boolean);
  const seedIndex = seeds.findIndex(
    (seed) => !bySeed.has(String(seed.seed_id || "")),
  );
  const normalizedSeedIndex = seedIndex < 0 ? seeds.length : seedIndex;
  const preserveNavigation = currentState && currentState.seedIndex === normalizedSeedIndex;
  return {
    ...(currentState || {}),
    runId: task.data.run_id,
    taskType: "ozon_collection",
    dispatchToken: task.data.dispatch_token,
    candidates,
    seedIndex: normalizedSeedIndex,
    stage: preserveNavigation ? (currentState.stage || "search") : "search",
    searchEvidence: preserveNavigation ? (currentState.searchEvidence || null) : null,
    productLinkIndex: preserveNavigation ? (currentState.productLinkIndex || 0) : 0,
    rejectedCandidates: preserveNavigation ? (currentState.rejectedCandidates || []) : [],
  };
}

async function checkpointOzonCandidate(task, candidate) {
  return await api(task.data.progress_url, {
    method: "POST",
    body: JSON.stringify({
      run_id: task.data.run_id,
      worker: "workbench_browser_bridge",
      source: "ozon_browser_extension_content_script",
      ozon_candidate: candidate,
    }),
  });
}

function advanceOzonCollectionState(state, seeds, candidate) {
  const bySeed = new Map(
    state.candidates.map((item) => [String(item.seed_id || ""), item]),
  );
  bySeed.set(String(candidate.seed_id || ""), candidate);
  state.candidates = seeds
    .map((seed) => bySeed.get(String(seed.seed_id || "")))
    .filter(Boolean);
  const nextIndex = seeds.findIndex(
    (seed) => !bySeed.has(String(seed.seed_id || "")),
  );
  state.seedIndex = nextIndex < 0 ? seeds.length : nextIndex;
  state.stage = "search";
  state.searchEvidence = null;
  state.productLinkIndex = 0;
}

async function runOzonCollection(task) {
  const runId = task.data.run_id;
  const seeds = task.data.contract.payload.seeds || [];
  const excludedProductIds = task.data.contract.payload.excluded_ozon_product_ids || [];
  const sessionState = loadState(runId, "ozon_collection", task.data.dispatch_token);
  let state = reconcileOzonCollectionState(task, sessionState);
  while (state.stage === "search" && state.seedIndex < seeds.length) {
    const reusableSeed = seeds[state.seedIndex];
    const snapshot = reusableSeed.public_product_snapshot;
    const decision = reusableSeed.domestic_seller_decision || {};
    const evidence = globalThis.OzonV2ProductEvidence;
    const reusableQuery = (reusableSeed.ozon_query_terms_ru || [])[0] || reusableSeed.source_text_zh || "";
    if (
      !snapshot
      || snapshotMissingFields(snapshot).length
      || decision.is_chinese_domestic_seller !== true
      || decision.confidence !== "high"
      || (evidence && evidence.hasUsedProductId(state.candidates, snapshot.product_id))
      || (evidence && evidence.isExcludedProductId(excludedProductIds, snapshot.product_id))
      || (evidence && !evidence.matchesQueryIntent([reusableQuery], snapshot))
    ) break;
    const candidate = buildOzonCandidate(
      reusableSeed,
      reusableQuery,
      { productLinks: [{ href: snapshot.url }] },
      { snapshot, url: snapshot.url, title: snapshot.title },
      decision,
    );
    saveState(state);
    await checkpointOzonCandidate(task, candidate);
    advanceOzonCollectionState(state, seeds, candidate);
    saveState(state);
    await heartbeat({
      run_id: runId,
      task_type: "ozon_collection",
      stage: "snapshot_reused",
      code: "bridge.ozon_collection_snapshot_reused",
      message: `Reused the complete verified public snapshot for seed ${state.seedIndex} of ${seeds.length}.`,
      details: collectionProgressDetails(state, seeds.length),
    });
  }
  saveState(state);
  const seed = seeds[state.seedIndex];
  if (!seed) {
    return await submitOzonCollection(runId, task.data.ingest_url, state, seeds.length);
  }
  const query = (seed.ozon_query_terms_ru || [])[0] || seed.source_text_zh || "";
  await heartbeat({
    run_id: runId,
    task_type: "ozon_collection",
    stage: state.stage,
    code: "bridge.ozon_collection_running",
    message: `Collecting Ozon product seed ${state.seedIndex + 1} of ${seeds.length}.`,
    details: collectionProgressDetails(state, seeds.length),
  });
  if (state.stage === "search") {
    if (!isSearchEvidencePage()) {
      saveState(state);
      location.href = searchUrl(query);
      return { ok: true, code: "bridge.ozon_collection_navigating_search" };
    }
    state.searchEvidence = await waitForSearchEvidence(runId);
    if (!state.searchEvidence.productLinks.length || state.searchEvidence.hasChallenge) {
      await heartbeat({
        run_id: runId,
        task_type: "ozon_collection",
        stage: "search_blocked",
        code: "bridge.ozon_collection_search_blocked",
        message: state.searchEvidence.hasChallenge
          ? "Ozon search page is blocked by challenge."
          : `Ozon search exposed ${state.searchEvidence.allProductLinkCount || 0} products but none had China cross-border card evidence.`,
      });
      saveState(state);
      return { ok: false, code: "bridge.ozon_collection_search_blocked" };
    }
    state.stage = "detail";
    state.productLinkIndex = 0;
    saveState(state);
    location.href = state.searchEvidence.productLinks[0].href;
    return { ok: true, code: "bridge.ozon_collection_navigating_detail" };
  }
  const detailEvidence = await waitForDetailEvidence(runId);
  if (detailEvidence.hasChallenge) {
    await heartbeat({
      run_id: runId,
      task_type: "ozon_collection",
      stage: "detail_blocked",
      code: "bridge.ozon_collection_detail_blocked",
      message: "Ozon product detail page is blocked by challenge.",
    });
    saveState(state);
    return { ok: false, code: "bridge.ozon_collection_detail_blocked" };
  }
  const evidence = globalThis.OzonV2ProductEvidence;
  const detailProductId = detailEvidence.snapshot && detailEvidence.snapshot.product_id;
  if (evidence && !evidence.matchesQueryIntent([query], detailEvidence.snapshot || {})) {
    const relevanceCheck = await requireChineseCrossBorderDetail(
      state,
      runId,
      "ozon_collection",
      "bridge.ozon_collection",
      { ...detailEvidence, missingFields: ["query_intent_mismatch"] },
      seed.seed_id,
      seeds.length,
    );
    return {
      ok: relevanceCheck.navigating,
      code: relevanceCheck.navigating
        ? "bridge.ozon_collection_irrelevant_candidate"
        : "bridge.ozon_collection_no_relevant_candidate",
    };
  }
  if (evidence && evidence.isExcludedProductId(excludedProductIds, detailProductId)) {
    const excludedCheck = await requireChineseCrossBorderDetail(
      state,
      runId,
      "ozon_collection",
      "bridge.ozon_collection",
      { ...detailEvidence, missingFields: ["excluded_ozon_product_id"] },
      seed.seed_id,
      seeds.length,
    );
    return {
      ok: excludedCheck.navigating,
      code: excludedCheck.navigating
        ? "bridge.ozon_collection_excluded_candidate"
        : "bridge.ozon_collection_no_unique_candidate",
    };
  }
  if (evidence && evidence.hasUsedProductId(state.candidates, detailProductId)) {
    const duplicateCheck = await requireChineseCrossBorderDetail(
      state,
      runId,
      "ozon_collection",
      "bridge.ozon_collection",
      { ...detailEvidence, missingFields: ["duplicate_ozon_product_id"] },
      seed.seed_id,
      seeds.length,
    );
    return {
      ok: duplicateCheck.navigating,
      code: duplicateCheck.navigating
        ? "bridge.ozon_collection_duplicate_candidate"
        : "bridge.ozon_collection_no_unique_candidate",
    };
  }
  const sellerCheck = await requireChineseCrossBorderDetail(
    state,
    runId,
    "ozon_collection",
    "bridge.ozon_collection",
    detailEvidence,
    seed.seed_id,
    seeds.length,
  );
  if (!sellerCheck.accepted) {
    return {
      ok: sellerCheck.navigating,
      code: sellerCheck.navigating
        ? "bridge.ozon_collection_next_candidate"
        : "bridge.ozon_collection_no_cross_border_candidate",
    };
  }
  const candidate = buildOzonCandidate(
    seed,
    query,
    state.searchEvidence || {},
    detailEvidence,
    sellerCheck.decision,
  );
  saveState(state);
  await checkpointOzonCandidate(task, candidate);
  advanceOzonCollectionState(state, seeds, candidate);
  saveState(state);
  if (state.seedIndex < seeds.length) {
    const next = seeds[state.seedIndex];
    location.href = searchUrl((next.ozon_query_terms_ru || [])[0] || next.source_text_zh || "");
    return { ok: true, code: "bridge.ozon_collection_next_seed" };
  }
  return await submitOzonCollection(runId, task.data.ingest_url, state, seeds.length);
}

async function run() {
  const task = await browserTask();
  if (task.code === "browser_task.ozon_collection_ready") {
    return await runOzonCollection(task);
  }
  if (task.code !== "browser_task.attribute_template_ready") {
    await heartbeat({ stage: "idle", code: task.code, message: task.message });
    return { ok: true, code: task.code };
  }
  const runId = task.data.run_id;
  const seeds = task.data.contract.payload.seeds || [];
  let state = loadState(runId, "ozon_attribute_template", task.data.dispatch_token, seeds) || {
    runId,
    taskType: "ozon_attribute_template",
    dispatchToken: task.data.dispatch_token,
    seedIndex: 0,
    stage: "search",
    templates: [],
    searchEvidence: null,
    productLinkIndex: 0,
    rejectedCandidates: [],
  };
  const seed = seeds[state.seedIndex];
  if (!seed) {
    return await submit(runId, task.data.ingest_url, state.templates);
  }
  const query = (seed.ozon_query_terms_ru || [])[0] || seed.source_text_zh || "";
  await heartbeat({
    run_id: runId,
    stage: state.stage,
    code: "bridge.running",
    message: `Collecting seed ${state.seedIndex + 1} of ${seeds.length}.`,
  });
  if (state.stage === "search") {
    if (!isSearchEvidencePage()) {
      saveState(state);
      await heartbeat({
        run_id: runId,
        stage: "navigating_search",
        code: "bridge.navigating_search",
        message: "Navigating to Ozon search page.",
      });
      location.href = searchUrl(query);
      return { ok: true, code: "bridge.navigating_search" };
    }
    state.searchEvidence = await waitForSearchEvidence(runId);
    if (!state.searchEvidence.productLinks.length || state.searchEvidence.hasChallenge) {
      await heartbeat({
        run_id: runId,
        stage: "search_blocked",
        code: "bridge.search_blocked",
        message: state.searchEvidence.hasChallenge
          ? "Ozon search page is blocked by challenge."
          : `Ozon search exposed ${state.searchEvidence.allProductLinkCount || 0} products but none had China cross-border card evidence.`,
      });
      saveState(state);
      return { ok: false, code: "bridge.search_blocked" };
    }
    state.stage = "detail";
    state.productLinkIndex = 0;
    saveState(state);
    await heartbeat({
      run_id: runId,
      stage: "navigating_detail",
      code: "bridge.navigating_detail",
      message: "Navigating to first Ozon product detail page.",
    });
    location.href = state.searchEvidence.productLinks[0].href;
    return { ok: true, code: "bridge.navigating_detail" };
  }
  const detailEvidence = await waitForDetailEvidence(runId);
  if (detailEvidence.hasChallenge) {
    await heartbeat({
      run_id: runId,
      stage: "detail_blocked",
      code: "bridge.detail_blocked",
      message: "Ozon product detail page is blocked by challenge.",
    });
    saveState(state);
    return { ok: false, code: "bridge.detail_blocked" };
  }
  const evidence = globalThis.OzonV2ProductEvidence;
  if (evidence && !evidence.matchesQueryIntent([query], detailEvidence.snapshot || {})) {
    const relevanceCheck = await requireChineseCrossBorderDetail(
      state,
      runId,
      "ozon_attribute_template",
      "bridge.attribute_template",
      { ...detailEvidence, missingFields: ["query_intent_mismatch"] },
      seed.seed_id,
    );
    return {
      ok: relevanceCheck.navigating,
      code: relevanceCheck.navigating
        ? "bridge.attribute_template_irrelevant_candidate"
        : "bridge.attribute_template_no_relevant_candidate",
    };
  }
  const sellerCheck = await requireChineseCrossBorderDetail(
    state,
    runId,
    "ozon_attribute_template",
    "bridge.attribute_template",
    detailEvidence,
    seed.seed_id,
  );
  if (!sellerCheck.accepted) {
    return {
      ok: sellerCheck.navigating,
      code: sellerCheck.navigating
        ? "bridge.attribute_template_next_candidate"
        : "bridge.attribute_template_no_cross_border_candidate",
    };
  }
  state.templates.push(
    buildTemplate(seed, query, state.searchEvidence || {}, detailEvidence, sellerCheck.decision),
  );
  state.seedIndex += 1;
  state.stage = "search";
  state.searchEvidence = null;
  state.productLinkIndex = 0;
  saveState(state);
  if (state.seedIndex < seeds.length) {
    const next = seeds[state.seedIndex];
    location.href = searchUrl((next.ozon_query_terms_ru || [])[0] || next.source_text_zh || "");
    return { ok: true, code: "bridge.next_seed" };
  }
  return await submit(runId, task.data.ingest_url, state.templates);
}

function triggerRun() {
  if (activeRunPromise) return activeRunPromise;
  activeRunPromise = run()
  .then((result) => {
    console.info("[OzonV2Bridge]", result);
    return result;
  })
  .catch(async (error) => {
    await heartbeat({
      stage: "script_error",
      code: "browser_bridge.content_script_error",
      message: error && error.message ? error.message : String(error),
    });
    try {
      await chrome.runtime.sendMessage({
        type: "ozon_v2_content_task_failed",
        error: error && error.message ? error.message : String(error),
      });
    } catch (_) {
      // The background worker can restart independently; its next poll will recover.
    }
    console.error("[OzonV2Bridge]", error);
    throw error;
  })
  .finally(() => {
    activeRunPromise = null;
  });
  return activeRunPromise;
}

window.OzonV2BrowserBridge = { run: triggerRun };
triggerRun();
