const fs = require("fs");
const os = require("os");
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

function offerIdFromUrl(url) {
  const value = String(url || "");
  const detailMatch = value.match(/offer\/(\d+)\.html/);
  if (detailMatch) return detailMatch[1];
  const queryMatch = value.match(/[?&]offerId=(\d+)/);
  return queryMatch ? queryMatch[1] : null;
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
      const rect = card.getBoundingClientRect();
      cards.push({
        offer_id: offerId,
        href: link.href,
        detail_url: `https://detail.1688.com/offer/${offerId}.html?offerId=${offerId}&forcePC=1`,
        text: normalizeSpaces(card.innerText || link.innerText || "").slice(0, 500),
        image: img ? img.currentSrc || img.src : null,
        box: {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
        },
      });
    }
    return cards.slice(0, 40);
  });
}

async function main() {
  const { chromium } = loadPlaywright();
  const outDir = path.resolve("runtime", "direct-playwright", "live-click-parse-stop");
  fs.mkdirSync(outDir, { recursive: true });

  const item = {
    seed_id: "seed-0149",
    name_zh: "儿童火箭图案睡衣",
    ozon_image: path.resolve("ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg"),
  };

  if (!fs.existsSync(item.ozon_image)) {
    throw new Error(`Ozon image not found: ${item.ozon_image}`);
  }

  const profileDir = path.join(os.tmpdir(), `ozonv2-1688-click-profile-${Date.now()}`);
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });

  let page = context.pages()[0] || await context.newPage();
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForSelector("#img-search-upload", { timeout: 60000 });
  const chooserPromise = page.waitForEvent("filechooser", { timeout: 15000 }).catch(() => null);
  const uploadButton = page.locator(".image-input-button, .image-upload-button-container, #img-search-upload").first();
  await uploadButton.click({ force: true });
  const chooser = await chooserPromise;
  if (chooser) {
    await chooser.setFiles(item.ozon_image);
  } else {
    await page.setInputFiles("#img-search-upload", item.ozon_image);
  }
  await page.waitForTimeout(5000);
  await page.screenshot({ path: path.join(outDir, "after-upload-before-click.png"), fullPage: false });

  const searchButton = page.locator(".copy-image-container .search-btn").first();
  await searchButton.waitFor({ state: "visible", timeout: 30000 });
  const buttonBox = await searchButton.boundingBox();
  if (!buttonBox) throw new Error("Search image button has no clickable box.");

  let openedPage = null;
  const popupPromise = context.waitForEvent("page", { timeout: 20000 })
    .then((newPage) => {
      openedPage = newPage;
      return newPage;
    })
    .catch(() => null);
  const navPromise = page.waitForURL(/1688-search\/pc-image-search|imageSearch|imageId=/, { timeout: 20000 }).catch(() => null);

  await page.mouse.move(buttonBox.x + buttonBox.width / 2, buttonBox.y + buttonBox.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(150);
  await page.mouse.up();
  await Promise.race([popupPromise, navPromise, page.waitForTimeout(20000)]);

  if (openedPage) {
    page = openedPage;
    await page.waitForLoadState("domcontentloaded", { timeout: 30000 }).catch(() => null);
  }
  await page.waitForTimeout(12000);

  const cards = await extractCards(page);
  const result = {
    item,
    current_url: page.url(),
    title: await page.title(),
    parser_rule: "parse_offer_id_from_all_href_query_or_detail_url",
    candidate_count: cards.length,
    top_offer_ids: cards.slice(0, 12).map((card) => card.offer_id),
    first_cards: cards.slice(0, 6),
    screenshot: path.join(outDir, "image-search-results.png"),
    stopped_at: "image_search_results",
  };

  await page.screenshot({ path: result.screenshot, fullPage: false });
  fs.writeFileSync(path.join(outDir, "result.json"), JSON.stringify(result, null, 2), "utf8");
  fs.writeFileSync(path.join(outDir, "cards.json"), JSON.stringify(cards, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  console.log("STOPPED_AT_IMAGE_SEARCH_RESULTS");

  await page.waitForTimeout(600000);
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
