const fs = require("fs");
const path = require("path");

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (_) {
    return require(path.join(process.env.LOCALAPPDATA || "", "npm-cache", "_npx", "9833c18b2d85bc59", "node_modules", "playwright"));
  }
}

function offerIdFromUrl(url) {
  const match = String(url || "").match(/offer\/(\d+)\.html/);
  return match ? match[1] : null;
}

async function extractCards(page) {
  return page.evaluate(() => {
    const offerIdFromUrl = (url) => {
      const match = String(url || "").match(/offer\/(\d+)\.html/);
      return match ? match[1] : null;
    };
    const seen = new Set();
    return Array.from(document.querySelectorAll('a[href*="detail.1688.com/offer/"]')).map((link) => {
      const href = link.href;
      const offer_id = offerIdFromUrl(href);
      const card = link.closest("[class]") || link;
      const img = card.querySelector("img") || link.querySelector("img");
      return {
        offer_id,
        href,
        text: (card.innerText || link.innerText || "").replace(/\s+/g, " ").trim().slice(0, 500),
        image: img ? img.currentSrc || img.src : null,
      };
    }).filter((item) => item.offer_id && !seen.has(item.offer_id) && seen.add(item.offer_id)).slice(0, 40);
  });
}

async function extractImageId(page) {
  return page.evaluate(() => {
    const btn = document.querySelector(".copy-image-container .search-btn");
    const fiberKey = Object.keys(btn || {}).find((key) => key.startsWith("__reactFiber$"));
    let fiber = btn?.[fiberKey] || null;
    for (let depth = 0; fiber && depth < 12; depth += 1) {
      const info = fiber.memoizedProps?.uploadedImagesListInfo;
      if (info?.list?.length) return String(info.list[0]);
      fiber = fiber.return;
    }
    return null;
  });
}

async function main() {
  const { chromium } = loadPlaywright();
  const outDir = path.resolve("runtime", "direct-playwright", "first-item-imageid");
  fs.mkdirSync(outDir, { recursive: true });
  const item = {
    seed_id: "seed-0149",
    name_zh: "儿童火箭图案睡衣",
    ozon_image: path.resolve("ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg"),
    expected_offer_id: "841777531690",
  };

  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "profile"), {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });
  const page = context.pages()[0] || await context.newPage();
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForSelector("#img-search-upload", { timeout: 45000 });
  const chooserPromise = page.waitForEvent("filechooser", { timeout: 15000 }).catch(() => null);
  await page.locator(".image-input-button, .image-upload-button-container, #img-search-upload").first().click({ force: true });
  const chooser = await chooserPromise;
  if (chooser) await chooser.setFiles(item.ozon_image);
  else await page.setInputFiles("#img-search-upload", item.ozon_image);
  await page.waitForTimeout(6000);
  const imageId = await extractImageId(page);
  if (!imageId) throw new Error("Homepage upload did not expose imageId");

  const searchUrl = `https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch&imageId=${imageId}&imageIdList=${imageId}&spm=a260k.home2025.imageUpload.search`;
  await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(15000);
  await page.screenshot({ path: path.join(outDir, "search-results.png"), fullPage: false });
  const cards = await extractCards(page);
  fs.writeFileSync(path.join(outDir, "search-cards.json"), JSON.stringify({ imageId, searchUrl, cards }, null, 2), "utf8");

  const selected = cards.find((card) => card.offer_id === item.expected_offer_id) || cards[0] || null;
  const result = {
    ...item,
    imageId,
    searchUrl,
    searchScreenshot: path.join(outDir, "search-results.png"),
    candidateCount: cards.length,
    expectedFound: cards.some((card) => card.offer_id === item.expected_offer_id),
    topOfferIds: cards.slice(0, 10).map((card) => card.offer_id),
    selectedOfferId: selected?.offer_id || null,
    selectedUrl: selected?.href || null,
  };

  if (selected) {
    await page.goto(selected.href, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(10000);
    await page.screenshot({ path: path.join(outDir, `detail-${selected.offer_id}.png`), fullPage: false });
    const body = await page.evaluate(() => document.body.innerText.replace(/\s+/g, " ").trim());
    const snippets = [];
    for (const re of [/1.?[件个双套].{0,8}起批/g, /运费.{0,20}/g, /包邮/g, /送至.{0,35}/g, /库存.{0,14}/g]) {
      for (const match of body.matchAll(re)) snippets.push(match[0]);
    }
    result.detail = {
      url: page.url(),
      title: await page.title(),
      screenshot: path.join(outDir, `detail-${selected.offer_id}.png`),
      evidenceSnippets: Array.from(new Set(snippets)).slice(0, 30),
      bodySample: body.slice(0, 4000),
    };
  }
  fs.writeFileSync(path.join(outDir, "result.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
