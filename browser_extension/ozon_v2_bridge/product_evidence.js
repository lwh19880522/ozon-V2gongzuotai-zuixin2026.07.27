"use strict";

(function exposeProductEvidence(root) {
  function normalized(value) {
    return String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  }

  function intentStems(value) {
    const stopwords = new Set(["без", "для", "или", "из", "на", "над", "от", "по", "под", "при", "со"]);
    const noiseRoots = new Set(["дост", "зака", "кита", "купи", "озон", "прод", "това", "цена"]);
    const tokens = normalized(value)
      .toLowerCase()
      .replace(/ё/g, "е")
      .replace(/[-_/]+/g, " ")
      .match(/[\p{L}\p{N}]+/gu) || [];
    return tokens
      .filter((token) => token.length >= 4 && !stopwords.has(token))
      .map((token) => token.slice(0, 4))
      .filter((stem) => !noiseRoots.has(stem));
  }

  const stemAliasGroups = [
    new Set(["мешк", "паке"]),
  ];

  function stemMatches(requiredStem, productStems) {
    if (productStems.has(requiredStem)) return true;
    const group = stemAliasGroups.find((values) => values.has(requiredStem));
    return Boolean(group && Array.from(group).some((stem) => productStems.has(stem)));
  }

  function subjectSnapshotText(snapshot = {}) {
    return [
      snapshot.title,
      snapshot.category_path,
      snapshot.leaf_category,
      ...Object.keys(snapshot.attributes || {}),
      ...Object.values(snapshot.attributes || {}),
      ...Object.values(snapshot.selected_options || {}),
      snapshot.card_text,
    ].filter(Boolean).join(" ");
  }

  function evaluateSeedSubject(contract = {}, snapshot = {}) {
    const required = Array.from(new Set((contract.required_stems || []).map(normalized).filter(Boolean)));
    const productStems = new Set(intentStems(subjectSnapshotText(snapshot)));
    const matched = required.filter((stem) => stemMatches(stem, productStems));
    const missing = required.filter((stem) => !stemMatches(stem, productStems));
    const matchRatio = required.length ? matched.length / required.length : 0;
    const minimumMatches = Math.max(1, Number(contract.minimum_matches || 1));
    const minimumRatio = Number(contract.minimum_match_ratio || 0.70);
    return {
      seed_id: normalized(contract.seed_id) || null,
      accepted: required.length > 0 && matched.length >= minimumMatches && matchRatio >= minimumRatio,
      required_stems: required,
      matched_stems: matched,
      missing_stems: missing,
      match_ratio: Number(matchRatio.toFixed(4)),
      minimum_matches: minimumMatches,
      minimum_match_ratio: minimumRatio,
    };
  }

  function matchesQueryIntent(queryTerms, snapshot = {}) {
    const queryStems = Array.from(new Set(intentStems((queryTerms || []).join(" "))));
    return evaluateSeedSubject({
      required_stems: queryStems,
      minimum_matches: Math.max(1, Math.ceil(queryStems.length * 0.70)),
      minimum_match_ratio: 0.70,
    }, snapshot).accepted;
  }

  function normalizeMedia(values, limit = 24) {
    const urls = [];
    for (const value of Array.isArray(values) ? values : [values]) {
      const url = normalized(value);
      if (!/^https?:\/\//i.test(url) || /favicon|sprite|\/icons?\/|logo(?:[._/-]|$)/i.test(url)) continue;
      if (!urls.includes(url)) urls.push(url);
      if (urls.length >= limit) break;
    }
    return urls;
  }

  function mergePublicSnapshots(previous = {}, current = {}) {
    const merged = { ...previous, ...current };
    for (const key of Object.keys(previous || {})) {
      const value = current ? current[key] : null;
      if (value == null || value === "") merged[key] = previous[key];
    }
    merged.attributes = { ...(previous.attributes || {}), ...(current.attributes || {}) };
    merged.selected_options = { ...(previous.selected_options || {}), ...(current.selected_options || {}) };
    for (const key of ["main_gallery_images", "selected_sku_images", "detail_page_images"]) {
      merged[key] = normalizeMedia([...(previous[key] || []), ...(current[key] || [])]);
    }
    const categoryLinks = [...(previous.category_links || []), ...(current.category_links || [])];
    const seenCategoryUrls = new Set();
    merged.category_links = categoryLinks.filter((item) => {
      const url = normalized(item && item.href);
      if (!url || seenCategoryUrls.has(url)) return false;
      seenCategoryUrls.add(url);
      return true;
    });
    return merged;
  }

  function crossBorderSearchQuery(query) {
    const value = normalized(query);
    return /из\s+китая/iu.test(value) ? value : `${value} из Китая`.trim();
  }

  function parseSellerEvidence(input = {}) {
    const rawText = normalized(input.rawText);
    const linkText = normalized(input.linkText);
    const match = rawText.match(
      /(?:магазин|продавец)\s+(.+?)(?=\s+(?:подписаться|заказы|отзывы|о магазине|чат)(?:\s|$)|$)/iu,
    );
    const linkName = /^(?:магазин|продавец|перейти)$/iu.test(linkText) ? "" : linkText;
    return {
      name: normalized(match ? match[1] : linkName) || null,
      url: normalized(input.url) || null,
      raw_text: rawText,
    };
  }

  function parseDeliveryEvidence(rawValue) {
    const rawText = normalized(rawValue);
    const sellerWarehouse = rawText.match(
      /со\s+склада\s+продавца,?\s*(?:[a-z-]+(?:\s+sheng)?|[а-яё-]+(?:\s+(?:область|край|республика))?)/iu,
    );
    const origin = sellerWarehouse || rawText.match(/товар\s+из\s+китая/iu) || rawText.match(
      /(?:доставка|отправка|доставим|товар)[^.!?]{0,100}из\s+китая/iu,
    ) || rawText.match(/со\s+склада\s+ozon[^.!?]{0,100}/iu);
    const time = rawText.match(/с\s+\d{1,2}\s+[а-яё]+/iu)
      || rawText.match(/\d{1,2}\s*[-–]\s*\d{1,2}\s+(?:дней|дня|день|дн\.?)/iu)
      || rawText.match(/(?:достав\w*|получ\w*)[^.!?]{0,100}(?:сегодня|завтра|послезавтра|\d{1,2}\s+[а-яё]+)/iu);
    const global = rawText.match(/ozon\s*global/iu);
    const foreignDelivery = rawText.match(/условия\s+доставки\s+из-за\s+рубежа/iu);
    const fulfillment = global
      ? global[0]
      : sellerWarehouse && foreignDelivery
        ? "cross_border_foreign_seller_warehouse"
      : origin && /из\s+китая/iu.test(origin[0])
        ? "cross_border_delivery_from_china"
        : origin && /склада\s+ozon/iu.test(origin[0])
          ? "local_ozon_warehouse"
          : null;
    return {
      raw_text: rawText,
      origin: origin ? normalized(origin[0]) : null,
      time: time ? normalized(time[0]) : null,
      fulfillment,
    };
  }

  function prioritizeCrossBorderSearchLinks(items) {
    return (items || []).filter((item) => {
      const cardText = normalized(item && item.card_text);
      return /(?:товар[а-яё]*\s+из\s+китая|достав[а-яё]*[^.!?]{0,80}из\s+китая|ozon\s*global|из-за\s+рубежа|зарубежн[а-яё]*\s+товар|конец\s+месяца|следующ[а-яё]*\s+месяц)/iu.test(cardText);
    });
  }

  function selectSearchCandidates(items, fallbackLimit = 3) {
    const source = Array.isArray(items) ? items : [];
    const qualified = prioritizeCrossBorderSearchLinks(source);
    const ordered = [...qualified, ...source.filter((item) => !qualified.includes(item))];
    return ordered.slice(0, Math.max(0, fallbackLimit));
  }

  function reconcileTemplateProgress(state = {}, seeds = [], dispatchToken = "") {
    const validSeedIds = new Set((seeds || []).map((seed) => normalized(seed && seed.seed_id)).filter(Boolean));
    const templates = (state.templates || []).filter((template) => validSeedIds.has(normalized(template && template.seed_id)));
    const completedSeedIds = new Set(templates.map((template) => normalized(template && template.seed_id)));
    const seedIndex = (seeds || []).findIndex((seed) => !completedSeedIds.has(normalized(seed && seed.seed_id)));
    return {
      ...state,
      dispatchToken,
      seedIndex: seedIndex < 0 ? (seeds || []).length : seedIndex,
      stage: "search",
      templates,
      searchEvidence: null,
      productLinkIndex: 0,
      rejectedCandidates: [],
    };
  }

  function jsonLdNodes(value) {
    if (Array.isArray(value)) return value.flatMap(jsonLdNodes);
    if (!value || typeof value !== "object") return [];
    const graph = Array.isArray(value["@graph"]) ? value["@graph"].flatMap(jsonLdNodes) : [];
    return [value, ...graph];
  }

  function parseProductJsonLd(rawDocuments) {
    const nodes = [];
    for (const raw of rawDocuments || []) {
      try {
        nodes.push(...jsonLdNodes(JSON.parse(String(raw || ""))));
      } catch (_) {
        // Ignore unrelated or malformed structured-data blocks.
      }
    }
    const product = nodes.find((node) => {
      const types = Array.isArray(node["@type"]) ? node["@type"] : [node["@type"]];
      return types.some((type) => String(type || "").toLowerCase() === "product");
    }) || {};
    const offers = Array.isArray(product.offers) ? product.offers[0] || {} : product.offers || {};
    const rating = product.aggregateRating || {};
    const brand = typeof product.brand === "object" ? product.brand.name : product.brand;
    const images = normalizeMedia(product.image || []);
    const reviewCount = Number(String(rating.reviewCount || rating.ratingCount || "").replace(/\s+/g, ""));
    return {
      title: normalized(product.name) || null,
      sku_id: normalized(product.sku || product.productID || product.mpn) || null,
      brand: normalized(brand) || null,
      description: normalized(product.description) || null,
      images,
      price: normalized(offers.price || offers.lowPrice) || null,
      currency: normalized(offers.priceCurrency) || null,
      availability: normalized(offers.availability) || null,
      rating: normalized(rating.ratingValue) || null,
      review_count: Number.isFinite(reviewCount) && reviewCount >= 0 ? reviewCount : null,
    };
  }

  function normalizeAttributePairs(pairs) {
    const table = {};
    for (const pair of pairs || []) {
      if (!Array.isArray(pair) || pair.length < 2) continue;
      const key = normalized(pair[0]);
      const value = normalized(pair[1]);
      if (!key || !value || key === value) continue;
      if (!table[key]) table[key] = value;
    }
    return table;
  }

  function hasRealAttributes(attributes) {
    const entries = Object.entries(attributes || {});
    return entries.length > 0 && entries.every(([key, value]) => {
      const text = normalized(value).toLowerCase();
      return normalized(key) && text && ![
        "visible_on_detail_page",
        "unavailable",
        "unknown",
        "n/a",
      ].includes(text);
    });
  }

  function missingPublicFields(snapshot) {
    const missing = [];
    const requiredText = [
      "product_id", "title", "sku_id", "category_path", "leaf_category", "category_url",
      "category_id", "price", "currency", "rating", "seller_name", "seller_url",
      "delivery_origin", "delivery_time", "fulfillment_label",
    ];
    for (const field of requiredText) {
      if (!normalized(snapshot && snapshot[field])) missing.push(field);
    }
    if (!snapshot || !Number.isInteger(snapshot.review_count) || snapshot.review_count < 0) missing.push("review_count");
    if (!snapshot || !snapshot.selected_options || !Object.keys(snapshot.selected_options).length) missing.push("selected_options");
    if (!hasRealAttributes(snapshot && snapshot.attributes)) missing.push("attributes");
    if (!normalizeMedia(snapshot && snapshot.main_gallery_images).length) missing.push("main_gallery_images");
    if (!normalizeMedia(snapshot && snapshot.selected_sku_images).length) missing.push("selected_sku_images");
    return Array.from(new Set(missing));
  }

  function blockingCandidateFields(missingFields, decision) {
    const optionalMarketSignals = new Set(["rating", "review_count"]);
    const fields = Array.from(new Set(Array.isArray(missingFields) ? missingFields : []))
      .filter((field) => !optionalMarketSignals.has(field));
    const signals = decision && Array.isArray(decision.signals) ? decision.signals : [];
    const hasChinaProductOrigin = Boolean(
      decision
      && decision.is_chinese_domestic_seller === true
      && decision.confidence === "high"
      && signals.some((signal) => signal && signal.kind === "product_origin_china"),
    );
    if (!hasChinaProductOrigin) return fields;
    const deliveryDisplayFields = new Set(["delivery_origin", "delivery_time", "fulfillment_label"]);
    return fields.filter((field) => !deliveryDisplayFields.has(field));
  }

  function hasUsedProductId(candidates, productId) {
    const target = normalized(productId);
    if (!target) return false;
    return (candidates || []).some((candidate) => normalized(candidate && candidate.ozon_product_id) === target);
  }

  function isExcludedProductId(excludedProductIds, productId) {
    const target = normalized(productId);
    if (!target) return false;
    return (excludedProductIds || []).some((value) => normalized(value) === target);
  }

  const api = {
    crossBorderSearchQuery,
    evaluateSeedSubject,
    blockingCandidateFields,
    hasRealAttributes,
    hasUsedProductId,
    isExcludedProductId,
    matchesQueryIntent,
    missingPublicFields,
    mergePublicSnapshots,
    normalizeAttributePairs,
    normalizeMedia,
    parseDeliveryEvidence,
    parseProductJsonLd,
    parseSellerEvidence,
    prioritizeCrossBorderSearchLinks,
    reconcileTemplateProgress,
    selectSearchCandidates,
  };
  root.OzonV2ProductEvidence = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
