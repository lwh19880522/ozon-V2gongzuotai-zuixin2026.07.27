const fs = require("fs");
const path = require("path");

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

const root = process.cwd();
const cases = [
  {
    seed_id: "seed-0149",
    name_zh: "儿童火箭图案睡衣",
    ozon_image: "ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg",
    expected_offer_id: "841777531690",
  },
  {
    seed_id: "seed-0019",
    name_zh: "奶油色塑料收纳篮三只装",
    ozon_image: "ozon-v2-run-671115c119b7-seed-0019-product-3665681034-main-image.jpg",
    expected_offer_id: "1034315601907",
  },
  {
    seed_id: "seed-0166",
    name_zh: "肤色加绒触屏手套",
    ozon_image: "ozon-v2-run-671115c119b7-seed-0166-product-2658379368-main-image.jpg",
    expected_offer_id: "1063452996301",
  },
  {
    seed_id: "seed-0167",
    name_zh: "深咖色保暖耳罩",
    ozon_image: "ozon-v2-run-671115c119b7-seed-0167-product-1734911018-main-image.jpg",
    expected_offer_id: "624398665952",
  },
  {
    seed_id: "seed-0163",
    name_zh: "黄色拼色围巾",
    ozon_image: "ozon-v2-run-671115c119b7-seed-0163-product-3120352853-main-image.jpg",
    expected_offer_id: "1018551096503",
  },
];

function normalizeSpaces(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function offerIdFromUrl(url) {
  const value = String(url || "");
  const detailMatch = value.match(/offer\/(\d+)\.html/);
  if (detailMatch) return detailMatch[1];
  const queryMatch = value.match(/[?&]offerId=(\d+)/);
  return queryMatch ? queryMatch[1] : null;
}

function detailUrl(offerId) {
  return `https://detail.1688.com/offer/${offerId}.html?offerId=${offerId}&forcePC=1`;
}

async function waitForHomepageImageId(page) {
  for (let i = 0; i < 24; i += 1) {
    const imageId = await page.evaluate(() => {
      const hosts = Array.from(document.querySelectorAll(".copy-image-container .search-btn, .image-input-button, body"));
      for (const host of hosts) {
        const fiberKey = Object.keys(host || {}).find((key) => key.startsWith("__reactFiber$"));
        let fiber = fiberKey ? host[fiberKey] : null;
        for (let depth = 0; fiber && depth < 18; depth += 1) {
          const info = fiber.memoizedProps?.uploadedImagesListInfo;
          if (info?.list?.length) return String(info.list[0]);
          fiber = fiber.return;
        }
      }
      return null;
    });
    if (imageId) return imageId;
    await page.waitForTimeout(500);
  }
  return null;
}

async function extractSearchCards(page) {
  return page.evaluate(() => {
    const normalizeSpaces = (text) => String(text || "").replace(/\s+/g, " ").trim();
    const offerIdFromUrl = (url) => {
      const value = String(url || "");
      const detailMatch = value.match(/offer\/(\d+)\.html/);
      if (detailMatch) return detailMatch[1];
      const queryMatch = value.match(/[?&]offerId=(\d+)/);
      return queryMatch ? queryMatch[1] : null;
    };
    const seen = new Set();
    const cards = [];
    for (const link of Array.from(document.querySelectorAll("a[href]"))) {
      const href = link.href;
      const offer_id = offerIdFromUrl(href);
      if (!offer_id || seen.has(offer_id)) continue;
      seen.add(offer_id);
      const card = link.closest("[class*='offer'], [class*='item'], [class*='card'], li, div") || link;
      const img = card.querySelector("img") || link.querySelector("img");
      const rect = card.getBoundingClientRect();
      cards.push({
        offer_id,
        href,
        detail_url: `https://detail.1688.com/offer/${offer_id}.html?offerId=${offer_id}&forcePC=1`,
        text: normalizeSpaces(card.innerText || link.innerText || "").slice(0, 600),
        image: img ? img.currentSrc || img.src : null,
        box: {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
        },
      });
    }
    return cards.slice(0, 60);
  });
}

async function extractDetail(page, offerId) {
  const body = await page.evaluate(() => document.body.innerText.replace(/\s+/g, " ").trim());
  const snippets = [];
  for (const pattern of [
    /1.?[件个双套].{0,8}起批/g,
    /运费.{0,22}/g,
    /包邮/g,
    /送至.{0,40}/g,
    /库存.{0,18}/g,
    /颜色.{0,100}/g,
    /适合身高.{0,100}/g,
  ]) {
    for (const match of body.matchAll(pattern)) snippets.push(match[0]);
  }
  const imgs = await page.evaluate(() => Array.from(document.images).slice(0, 50).map((img) => ({
    src: img.currentSrc || img.src,
    alt: img.alt || "",
    box: {
      x: Math.round(img.getBoundingClientRect().x),
      y: Math.round(img.getBoundingClientRect().y),
      w: Math.round(img.getBoundingClientRect().width),
      h: Math.round(img.getBoundingClientRect().height),
    },
  })));
  return {
    offer_id: offerId,
    url: page.url(),
    title: await page.title(),
    body_sample: body.slice(0, 6000),
    evidence_snippets: Array.from(new Set(snippets)).slice(0, 40),
    images: imgs,
  };
}

async function runOne(page, outDir, item) {
  const imagePath = path.resolve(root, item.ozon_image);
  const caseDir = path.join(outDir, item.seed_id);
  fs.mkdirSync(caseDir, { recursive: true });

  const result = {
    ...item,
    ozon_image_abs: imagePath,
    started_at: new Date().toISOString(),
    status: "started",
  };

  if (!fs.existsSync(imagePath)) {
    result.status = "missing_ozon_image";
    result.finished_at = new Date().toISOString();
    return result;
  }

  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForSelector("#img-search-upload", { timeout: 60000 });
  await page.setInputFiles("#img-search-upload", imagePath);
  const imageId = await waitForHomepageImageId(page);
  if (!imageId) {
    await page.screenshot({ path: path.join(caseDir, "homepage-upload-no-image-id.png"), fullPage: false });
    result.status = "homepage_upload_no_image_id";
    result.finished_at = new Date().toISOString();
    return result;
  }

  const searchUrl = `https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch&imageId=${imageId}&imageIdList=${imageId}&spm=a260k.home2025.imageUpload.search`;
  await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(14000);
  await page.screenshot({ path: path.join(caseDir, "1688-image-search.png"), fullPage: false });

  const cards = await extractSearchCards(page);
  const bodySample = normalizeSpaces(await page.textContent("body")).slice(0, 3500);
  fs.writeFileSync(path.join(caseDir, "search-cards.json"), JSON.stringify({ imageId, searchUrl, cards, body_sample: bodySample }, null, 2), "utf8");

  const selected = cards.find((card) => card.offer_id === item.expected_offer_id) || cards[0] || null;
  result.image_search = {
    image_id: imageId,
    url: searchUrl,
    screenshot: path.join(caseDir, "1688-image-search.png"),
    candidate_count: cards.length,
    expected_offer_found: cards.some((card) => card.offer_id === item.expected_offer_id),
    top_offer_ids: cards.slice(0, 10).map((card) => card.offer_id),
    selected_offer_id: selected ? selected.offer_id : null,
    selected_url: selected ? selected.detail_url : null,
  };

  if (!selected) {
    result.status = "no_search_candidate";
    result.finished_at = new Date().toISOString();
    return result;
  }

  await page.goto(detailUrl(selected.offer_id), { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(9000);
  await page.screenshot({ path: path.join(caseDir, `detail-${selected.offer_id}.png`), fullPage: false });
  const detail = await extractDetail(page, selected.offer_id);
  fs.writeFileSync(path.join(caseDir, `detail-${selected.offer_id}.json`), JSON.stringify(detail, null, 2), "utf8");

  result.detail = {
    offer_id: selected.offer_id,
    url: detail.url,
    title: detail.title,
    screenshot: path.join(caseDir, `detail-${selected.offer_id}.png`),
    evidence_snippets: detail.evidence_snippets,
    json: path.join(caseDir, `detail-${selected.offer_id}.json`),
  };
  result.status = "detail_collected";
  result.finished_at = new Date().toISOString();
  return result;
}

async function main() {
  const { chromium } = loadPlaywright();
  const outDir = path.resolve("runtime", "direct-playwright", "five-product-test");
  fs.mkdirSync(outDir, { recursive: true });

  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "profile"), {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });

  const page = context.pages()[0] || await context.newPage();
  const run = {
    captured_at: new Date().toISOString(),
    network_rule: "direct_no_proxy",
    entry_rule: "1688_homepage_upload_then_homepage_image_id",
    homepage_url: "https://www.1688.com/",
    results: [],
  };

  for (const item of cases) {
    const result = await runOne(page, outDir, item);
    run.results.push(result);
    fs.writeFileSync(path.join(outDir, "summary.json"), JSON.stringify(run, null, 2), "utf8");
    console.log(JSON.stringify(result, null, 2));
  }

  await context.close();
  fs.writeFileSync(path.join(outDir, "summary.json"), JSON.stringify(run, null, 2), "utf8");
  console.log(JSON.stringify(run, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
