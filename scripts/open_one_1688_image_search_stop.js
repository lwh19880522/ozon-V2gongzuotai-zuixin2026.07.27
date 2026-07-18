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

async function waitForHomepageImageId(page) {
  for (let i = 0; i < 30; i += 1) {
    const imageId = await page.evaluate(() => {
      const hosts = Array.from(document.querySelectorAll(".copy-image-container .search-btn, .image-input-button, body"));
      for (const host of hosts) {
        const fiberKey = Object.keys(host || {}).find((key) => key.startsWith("__reactFiber$"));
        let fiber = fiberKey ? host[fiberKey] : null;
        for (let depth = 0; fiber && depth < 24; depth += 1) {
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

async function extractCards(page) {
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
      const offerId = offerIdFromUrl(link.href);
      if (!offerId || seen.has(offerId)) continue;
      seen.add(offerId);
      const card = link.closest("[class*='offer'], [class*='item'], [class*='card'], li, div") || link;
      const img = card.querySelector("img") || link.querySelector("img");
      cards.push({
        offer_id: offerId,
        href: link.href,
        text: normalizeSpaces(card.innerText || link.innerText || "").slice(0, 500),
        image: img ? img.currentSrc || img.src : null,
      });
    }
    return cards.slice(0, 30);
  });
}

async function main() {
  const { chromium } = loadPlaywright();
  const outDir = path.resolve("runtime", "direct-playwright", "live-image-search-stop");
  fs.mkdirSync(outDir, { recursive: true });

  const item = {
    seed_id: "seed-0149",
    name_zh: "儿童火箭图案睡衣",
    ozon_image: path.resolve("ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg"),
  };

  if (!fs.existsSync(item.ozon_image)) {
    throw new Error(`Ozon image not found: ${item.ozon_image}`);
  }

  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "live-search-profile"), {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });

  const page = context.pages()[0] || await context.newPage();
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForSelector("#img-search-upload", { timeout: 60000 });
  await page.setInputFiles("#img-search-upload", item.ozon_image);

  const imageId = await waitForHomepageImageId(page);
  if (!imageId) {
    await page.screenshot({ path: path.join(outDir, "homepage-upload-no-image-id.png"), fullPage: false });
    throw new Error("Homepage upload did not expose imageId.");
  }

  const searchUrl = `https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch&imageId=${imageId}&imageIdList=${imageId}&spm=a260k.home2025.imageUpload.search`;
  await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(15000);

  const cards = await extractCards(page);
  const result = {
    item,
    image_id: imageId,
    search_url: searchUrl,
    current_url: page.url(),
    title: await page.title(),
    candidate_count: cards.length,
    top_offer_ids: cards.slice(0, 10).map((card) => card.offer_id),
    screenshot: path.join(outDir, "image-search-results.png"),
    stopped_at: "image_search_results",
  };

  await page.screenshot({ path: result.screenshot, fullPage: false });
  fs.writeFileSync(path.join(outDir, "cards.json"), JSON.stringify({ ...result, cards }, null, 2), "utf8");
  fs.writeFileSync(path.join(outDir, "result.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  console.log("STOPPED_AT_IMAGE_SEARCH_RESULTS");

  await page.waitForTimeout(600000);
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
