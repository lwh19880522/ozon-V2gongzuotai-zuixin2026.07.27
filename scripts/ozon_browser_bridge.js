(function () {
  "use strict";

  const DEFAULT_BASE_URL = "http://127.0.0.1:8765";
  const STATE_KEY = "ozon_v2_browser_bridge_state";

  function config() {
    const userConfig = window.OZON_V2_BRIDGE || {};
    return {
      baseUrl: userConfig.baseUrl || DEFAULT_BASE_URL,
      runId: userConfig.runId || localStorage.getItem("ozon_v2_workbench_run_id") || "",
    };
  }

  function absoluteUrl(path) {
    const base = config().baseUrl.replace(/\/+$/, "");
    return `${base}${path}`;
  }

  async function api(path, options) {
    const response = await fetch(absoluteUrl(path), {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const body = await response.json();
    if (!response.ok || body.ok === false) {
      throw new Error(body.message || body.code || "Ozon V2 bridge API failed.");
    }
    return body;
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

  function textOf(node, limit) {
    return (node && node.textContent ? node.textContent : "").trim().replace(/\s+/g, " ").slice(0, limit || 240);
  }

  function pageHasChallenge() {
    const text = `${location.href} ${document.title} ${textOf(document.body, 1600)}`;
    return /captcha|robot|fab_chlg|challenge|checking|enable JavaScript|not a robot|VPN/i.test(text);
  }

  function collectLinks(selector, limit) {
    return Array.from(document.querySelectorAll(selector))
      .map((node) => ({ href: normalizeUrl(node.href), text: textOf(node, 160) }))
      .filter((item) => item.href)
      .slice(0, limit || 12);
  }

  function collectAttributeLabels() {
    const labels = Array.from(document.querySelectorAll("dt, th, [data-widget], [class*='characteristic'], [class*='attribute']"))
      .map((node) => textOf(node, 80))
      .filter((value) => value && value.length <= 80)
      .filter((value) => !/^\d+$/.test(value))
      .slice(0, 24);
    return Array.from(new Set(labels)).slice(0, 12);
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

  function buildTemplate(seed, query, searchEvidence, detailEvidence) {
    const searchCategoryLinks = searchEvidence.categoryLinks || [];
    const productLinks = searchEvidence.productLinks || [];
    const categoryLinks = detailEvidence.categoryLinks.length ? detailEvidence.categoryLinks : searchCategoryLinks;
    const leaf = categoryLinks.length ? categoryLinks[categoryLinks.length - 1].text : query;
    const categoryUrl = categoryLinks.length ? categoryLinks[categoryLinks.length - 1].href : detailEvidence.url || searchEvidence.url;
    const labels = detailEvidence.attributeLabels.length
      ? detailEvidence.attributeLabels
      : ["Visible product attribute", "Material", "Color", "Size", "Dimensions"];
    const schema = labels.slice(0, 12).map(attributeFromLabel);
    return {
      seed_id: seed.seed_id,
      source_query: query,
      category_candidates: [
        {
          category_path: categoryLinks.length ? categoryLinks.map((item) => item.text || item.href).join(" / ") : query,
          leaf_category: leaf,
          category_url: categoryUrl,
          category_id: categoryUrl,
          confidence: categoryLinks.length ? "medium" : "low",
          source_evidence: categoryLinks.length ? "Ozon visible breadcrumb/category evidence" : "Ozon page URL fallback evidence",
        },
      ],
      upload_attribute_schema: schema,
      draft_prefill_plan: schema.map((attribute) => ({
        field_key: attribute.attribute_id,
        source: "ozon_public_objective_attribute_evidence",
        prefill_allowed: true,
        rewrite_required: false,
        reason: "Objective product attribute visible in Ozon evidence or required template baseline.",
      })),
      evidence: {
        search_url: searchEvidence.url,
        search_title: searchEvidence.title,
        product_url: detailEvidence.url,
        product_title: detailEvidence.title,
        product_links_seen: productLinks.map((item) => item.href).slice(0, 8),
        bridge_url: location.href,
        has_challenge: pageHasChallenge(),
      },
    };
  }

  function loadState(runId) {
    try {
      const state = JSON.parse(sessionStorage.getItem(STATE_KEY) || "{}");
      return state.runId === runId ? state : null;
    } catch (_) {
      return null;
    }
  }

  function saveState(state) {
    sessionStorage.setItem(STATE_KEY, JSON.stringify(state));
  }

  function searchUrl(query) {
    return `https://www.ozon.ru/search/?text=${encodeURIComponent(query)}&__rr=1`;
  }

  async function runBridge() {
    const configured = config();
    const task = configured.runId
      ? await api(`/api/batches/${encodeURIComponent(configured.runId)}/browser-task`)
      : await api("/api/browser-task/active");
    if (task.code !== "browser_task.attribute_template_ready") {
      return { ok: true, code: task.code, message: task.message };
    }
    const runId = task.data.run_id;
    const payload = task.data.contract.payload;
    const seeds = payload.seeds || [];
    let state = loadState(runId) || { runId, seedIndex: 0, stage: "search", templates: [], searchEvidence: null };
    const seed = seeds[state.seedIndex];
    if (!seed) {
      return submit(runId, task.data.ingest_url, state.templates);
    }
    const query = (seed.ozon_query_terms_ru || [])[0] || seed.source_text_zh || "";
    if (state.stage === "search") {
      if (!location.href.includes("/search/")) {
        saveState(state);
        location.href = searchUrl(query);
        return { ok: true, code: "bridge.navigating_search", query };
      }
      const productLinks = collectLinks('a[href*="/product/"]', 12);
      const categoryLinks = collectLinks('a[href*="/category/"]', 12);
      state.searchEvidence = {
        url: location.href,
        title: document.title,
        productLinks,
        categoryLinks,
        hasChallenge: pageHasChallenge(),
      };
      const productUrl = productLinks.length ? productLinks[0].href : "";
      if (!productUrl || state.searchEvidence.hasChallenge) {
        saveState(state);
        return { ok: false, code: "bridge.search_blocked", message: "Ozon search evidence is not ready.", state };
      }
      state.stage = "detail";
      state.productUrl = productUrl;
      saveState(state);
      location.href = productUrl;
      return { ok: true, code: "bridge.navigating_detail", productUrl };
    }
    const detailEvidence = {
      url: location.href,
      title: document.querySelector("h1") ? textOf(document.querySelector("h1"), 240) : document.title,
      categoryLinks: collectLinks('a[href*="/category/"]', 12),
      attributeLabels: collectAttributeLabels(),
      hasChallenge: pageHasChallenge(),
    };
    if (detailEvidence.hasChallenge) {
      saveState(state);
      return { ok: false, code: "bridge.detail_blocked", message: "Ozon detail evidence is blocked by challenge.", state };
    }
    state.templates.push(buildTemplate(seed, query, state.searchEvidence || {}, detailEvidence));
    state.seedIndex += 1;
    state.stage = "search";
    state.searchEvidence = null;
    state.productUrl = null;
    saveState(state);
    if (state.seedIndex < seeds.length) {
      location.href = searchUrl((seeds[state.seedIndex].ozon_query_terms_ru || [])[0] || seeds[state.seedIndex].source_text_zh || "");
      return { ok: true, code: "bridge.next_seed", seedIndex: state.seedIndex };
    }
    return submit(runId, task.data.ingest_url, state.templates);
  }

  async function submit(runId, ingestUrl, templates) {
    const result = await api(ingestUrl, {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        worker: "workbench_browser_bridge",
        source: "ozon_browser_bridge_content_script",
        seed_templates: templates,
      }),
    });
    sessionStorage.removeItem(STATE_KEY);
    return { ok: true, code: "bridge.submitted", result };
  }

  window.OzonV2BrowserBridge = { run: runBridge };
  runBridge()
    .then((result) => console.info("[OzonV2Bridge]", result))
    .catch((error) => console.error("[OzonV2Bridge]", error));
})();
