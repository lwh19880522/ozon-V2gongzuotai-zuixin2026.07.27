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

async function extractHomepageImageId(page) {
  return page.evaluate(() => {
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
}

async function waitForHomepageImageId(page) {
  for (let i = 0; i < 30; i += 1) {
    const imageId = await extractHomepageImageId(page);
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
      const offer_id = offerIdFromUrl(link.href);
      if (!offer_id || seen.has(offer_id)) continue;
      seen.add(offer_id);
      const card = link.closest("[class*='offer'], [class*='item'], [class*='card'], li, div") || link;
      const img = card.querySelector("img") || link.querySelector("img");
      cards.push({
        offer_id,
        href: link.href,
        detail_url: `https://detail.1688.com/offer/${offer_id}.html?offerId=${offer_id}&forcePC=1`,
        text: normalizeSpaces(card.innerText || link.innerText || "").slice(0, 500),
        image: img ? img.currentSrc || img.src : null,
      });
    }
    return cards.slice(0, 30);
  });
}

async function main() {
  const { chromium } = loadPlaywright();
  const outDir = path.resolve("runtime", "direct-playwright", "live-open-one");
  fs.mkdirSync(outDir, { recursive: true });

  const item = {
    seed_id: "seed-0149",
    name_zh: "儿童火箭图案睡衣",
    ozon_image: path.resolve("ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg"),
    expected_offer_id: "841777531690",
  };

  if (!fs.existsSync(item.ozon_image)) {
    throw new Error(`Ozon image not found: ${item.ozon_image}`);
  }

  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "live-profile"), {
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
    throw new Error("Homepage upload succeeded visually but did not expose imageId.");
  }

  const searchUrl = `https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch&imageId=${imageId}&imageIdList=${imageId}&spm=a260k.home2025.imageUpload.search`;
  await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(15000);
  await page.screenshot({ path: path.join(outDir, "image-search-results.png"), fullPage: false });

  const cards = await extractCards(page);
  fs.writeFileSync(path.join(outDir, "cards.json"), JSON.stringify({ item, imageId, searchUrl, cards }, null, 2), "utf8");

  const selected = cards.find((card) => card.offer_id === item.expected_offer_id) || cards[0];
  if (!selected) {
    throw new Error("No offerId found on 1688 image search results page.");
  }

  await page.goto(detailUrl(selected.offer_id), { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(9000);
  await page.screenshot({ path: path.join(outDir, `detail-${selected.offer_id}.png`), fullPage: false });

  const detail = await page.evaluate(() => {
    const body = document.body.innerText.replace(/\s+/g, " ").trim();
    return {
      url: location.href,
      title: document.title,
      body_sample: body.slice(0, 3000),
    };
  });

  const result = {
    item,
    imageId,
    searchUrl,
    selected_offer_id: selected.offer_id,
    selected_detail_url: detailUrl(selected.offer_id),
    selected_card: selected,
    detail,
    screenshots: {
      search: path.join(outDir, "image-search-results.png"),
      detail: path.join(outDir, `detail-${selected.offer_id}.png`),
    },
  };
  fs.writeFileSync(path.join(outDir, "result.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  console.log("LIVE_BROWSER_LEFT_OPEN");

  await page.waitForTimeout(600000);
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
