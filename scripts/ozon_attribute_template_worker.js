const fs = require("fs");
const path = require("path");

const SEARCH_WAIT_MS = Number(process.env.OZON_V2_SEARCH_WAIT_MS || "180000");
const DETAIL_WAIT_MS = Number(process.env.OZON_V2_DETAIL_WAIT_MS || "90000");

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (_) {
    return require(path.join(
      process.env.LOCALAPPDATA || "",
      "npm-cache",
      "_npx",
      "9833c18b2d85bc59",
      "node_modules",
      "playwright"
    ));
  }
}

function slug(value) {
  return String(value || "")
    .replace(/[^\w.-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80) || "seed";
}

function normalizeUrl(href) {
  if (!href) return "";
  try {
    const url = new URL(href, "https://www.ozon.ru");
    url.search = "";
    return url.toString();
  } catch (_) {
    return "";
  }
}

function publicProxyLabel(proxyServer) {
  return proxyServer.replace(/\/\/([^:@/]+):([^@/]+)@/, "//***:***@");
}

function attrFromLabel(label, index) {
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

function isChallengeText(text) {
  return /captcha|robot|робот|капча|fab_chlg|challenge|checking/i.test(String(text || ""));
}

async function readSearchState(page) {
  return await page.evaluate(() => {
    const links = Array.from(document.querySelectorAll('a[href*="/product/"]'))
      .map((a) => ({ href: a.href, text: (a.textContent || a.getAttribute("aria-label") || "").trim().slice(0, 240) }))
      .filter((item) => item.href)
      .slice(0, 12);
    const categoryLinks = Array.from(document.querySelectorAll('a[href*="/category/"]'))
      .map((a) => ({ href: a.href, text: (a.textContent || "").trim().slice(0, 120) }))
      .filter((item) => item.href && item.text)
      .slice(0, 8);
    const pageText = (document.body && document.body.textContent ? document.body.textContent : "").slice(0, 3000);
    const challengeText = `${location.href} ${document.title} ${pageText}`;
    return {
      url: location.href,
      title: document.title,
      links,
      categoryLinks,
      hasCaptcha: /captcha|robot|робот|капча|fab_chlg|challenge|checking/i.test(challengeText),
      pageTextSample: pageText.slice(0, 500),
    };
  });
}

async function readDetailState(page) {
  return await page.evaluate(() => {
    const categoryLinks = Array.from(document.querySelectorAll('a[href*="/category/"]'))
      .map((a) => ({ href: a.href, text: (a.textContent || "").trim().slice(0, 120) }))
      .filter((item) => item.href && item.text)
      .slice(0, 12);
    const labels = Array.from(document.querySelectorAll("dt, th, span, div"))
      .map((el) => (el.textContent || "").trim())
      .filter((text) => /^(Бренд|Тип|Цвет|Материал|Размер|Вес|Высота|Ширина|Длина|Форма|Особенности)$/i.test(text))
      .slice(0, 12);
    const title = document.querySelector("h1") ? document.querySelector("h1").textContent.trim() : document.title;
    const pageText = (document.body && document.body.textContent ? document.body.textContent : "").slice(0, 3000);
    const challengeText = `${location.href} ${document.title} ${pageText}`;
    return {
      url: location.href,
      title,
      categoryLinks,
      attributeLabels: Array.from(new Set(labels)),
      hasCaptcha: /captcha|robot|робот|капча|fab_chlg|challenge|checking/i.test(challengeText),
      pageTextSample: pageText.slice(0, 500),
    };
  });
}

async function waitForEvidence(page, readState, hasEvidence, waitMs, label) {
  const startedAt = Date.now();
  let state = await readState(page);
  while (Date.now() - startedAt < waitMs) {
    if (!state.hasCaptcha && hasEvidence(state)) {
      return { ...state, waited_ms: Date.now() - startedAt, wait_label: label };
    }
    await page.waitForTimeout(3000);
    state = await readState(page);
  }
  return {
    ...state,
    waited_ms: Date.now() - startedAt,
    wait_label: label,
    challenge_timed_out: Boolean(state.hasCaptcha),
  };
}

async function assertVisitorOnly(page) {
  const state = await page.evaluate(() => ({ url: location.href }));
  if (/seller\.ozon\.ru|\/my\/|\/account\//i.test(state.url)) {
    throw new Error(`Ozon worker must use visitor access only, but account area was detected: ${state.url}`);
  }
}

async function collectSearch(page, query, seedDir) {
  const searchUrl = `https://www.ozon.ru/search/?text=${encodeURIComponent(query)}`;
  await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  const state = await waitForEvidence(
    page,
    readSearchState,
    (item) => item.links.length > 0 || item.categoryLinks.length > 0,
    SEARCH_WAIT_MS,
    "ozon_search"
  );
  await assertVisitorOnly(page);
  const screenshot = path.join(seedDir, "ozon-search.png");
  await page.screenshot({ path: screenshot, fullPage: false });
  return { ...state, screenshot };
}

async function collectDetail(page, productUrl, seedDir) {
  await page.goto(productUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  const state = await waitForEvidence(
    page,
    readDetailState,
    (item) => item.categoryLinks.length > 0 || item.attributeLabels.length > 0,
    DETAIL_WAIT_MS,
    "ozon_detail"
  );
  await assertVisitorOnly(page);
  const screenshot = path.join(seedDir, "ozon-detail.png");
  await page.screenshot({ path: screenshot, fullPage: false });
  return { ...state, screenshot };
}

function buildTemplate(runId, seed, query, search, detail) {
  if (search.hasCaptcha || (detail && detail.hasCaptcha)) {
    throw new Error("Ozon captcha or robot check was detected.");
  }
  const categoryLinks = detail && detail.categoryLinks.length ? detail.categoryLinks : search.categoryLinks;
  if (!categoryLinks.length && !detail) {
    throw new Error("Ozon search did not expose category or product evidence.");
  }
  const categoryPath = categoryLinks.length ? categoryLinks.map((item) => item.text).join(" / ") : query;
  const leaf = categoryLinks.length ? categoryLinks[categoryLinks.length - 1].text : query;
  const categoryUrl = categoryLinks.length ? normalizeUrl(categoryLinks[categoryLinks.length - 1].href) : search.url;
  const labels = detail && detail.attributeLabels.length
    ? detail.attributeLabels
    : ["Бренд", "Тип", "Цвет", "Материал", "Размер"];
  const schema = labels.slice(0, 12).map(attrFromLabel);
  return {
    seed_id: seed.seed_id,
    source_query: query,
    category_candidates: [
      {
        category_path: categoryPath,
        leaf_category: leaf,
        category_url: categoryUrl,
        category_id: categoryUrl || `search:${query}`,
        confidence: detail ? "medium" : "low",
        source_evidence: detail ? "Ozon product/category page evidence" : "Ozon search/category page evidence",
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
      search_url: search.url,
      search_title: search.title,
      search_screenshot: search.screenshot,
      product_url: detail ? detail.url : null,
      product_title: detail ? detail.title : null,
      product_screenshot: detail ? detail.screenshot : null,
      product_links_seen: search.links.map((item) => normalizeUrl(item.href)).filter(Boolean).slice(0, 8),
    },
  };
}

async function main() {
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  if (!inputPath || !outputPath) throw new Error("Usage: node ozon_attribute_template_worker.js input.json output.json");
  const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
  const artifactsDir = input.artifacts_dir || path.dirname(outputPath);
  fs.mkdirSync(artifactsDir, { recursive: true });

  const { chromium } = loadPlaywright();
  const proxyServer = process.env.OZON_V2_OZON_PROXY || "http://127.0.0.1:7890";
  if (!proxyServer) {
    throw new Error("Ozon proxy is required for Ozon collection.");
  }
  const visitorProfileDir = input.visitor_profile_dir
    || process.env.OZON_V2_VISITOR_PROFILE
    || path.resolve("runtime", "ozon-visitor-profile");
  fs.mkdirSync(visitorProfileDir, { recursive: true });
  const context = await chromium.launchPersistentContext(visitorProfileDir, {
    channel: "msedge",
    headless: false,
    proxy: { server: proxyServer },
    viewport: { width: 1365, height: 900 },
    locale: "ru-RU",
  });
  const page = context.pages()[0] || await context.newPage();
  const networkMode = {
    ozon_proxy_required: true,
    proxy_server: publicProxyLabel(proxyServer),
    access_identity: "visitor_profile",
    logged_in_account_allowed: false,
    browser_context: "persistent_visitor_profile",
    visitor_profile_dir: visitorProfileDir,
  };
  fs.writeFileSync(path.join(artifactsDir, "worker_network_mode.json"), JSON.stringify(networkMode, null, 2), "utf8");
  const seedTemplates = [];
  const errors = [];
  try {
    for (const seed of input.seeds || []) {
      const query = Array.isArray(seed.ozon_query_terms_ru) && seed.ozon_query_terms_ru.length
        ? seed.ozon_query_terms_ru[0]
        : seed.title_or_keyword;
      const seedDir = path.join(artifactsDir, slug(seed.seed_id));
      fs.mkdirSync(seedDir, { recursive: true });
      const search = await collectSearch(page, query, seedDir);
      fs.writeFileSync(path.join(seedDir, "search.json"), JSON.stringify(search, null, 2), "utf8");
      let detail = null;
      const firstProduct = search.links.map((item) => normalizeUrl(item.href)).find(Boolean);
      if (firstProduct) {
        detail = await collectDetail(page, firstProduct, seedDir);
        fs.writeFileSync(path.join(seedDir, "detail.json"), JSON.stringify(detail, null, 2), "utf8");
      }
      seedTemplates.push(buildTemplate(input.run_id, seed, query, search, detail));
    }
  } catch (error) {
    errors.push(error && error.stack ? error.stack : String(error));
  } finally {
    await context.close();
  }
  const ok = errors.length === 0 && seedTemplates.length === (input.seeds || []).length;
  const result = ok
    ? {
        ok: true,
        payload: {
          run_id: input.run_id,
          worker: "workbench_browser_worker",
          source: "workbench_attribute_template_browser_worker",
          network_mode: networkMode,
          seed_templates: seedTemplates,
        },
      }
    : {
        ok: false,
        message: "Attribute template browser worker could not collect enough Ozon evidence.",
        errors,
        payload: {
          run_id: input.run_id,
          worker: "workbench_browser_worker",
          source: "workbench_attribute_template_browser_worker",
          network_mode: networkMode,
          seed_templates: seedTemplates,
        },
      };
  fs.writeFileSync(outputPath, JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify({ ok: result.ok, outputPath, errors }, null, 2));
  if (!result.ok) process.exitCode = 2;
}

main().catch((error) => {
  const outputPath = process.argv[3];
  const result = {
    ok: false,
    message: "Attribute template browser worker crashed.",
    errors: [error && error.stack ? error.stack : String(error)],
  };
  if (outputPath) fs.writeFileSync(outputPath, JSON.stringify(result, null, 2), "utf8");
  console.error(error);
  process.exitCode = 1;
});
