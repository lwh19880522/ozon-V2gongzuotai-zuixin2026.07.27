const fs = require("fs");
const path = require("path");

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (_) {
    return require(path.join(process.env.LOCALAPPDATA || "", "npm-cache", "_npx", "9833c18b2d85bc59", "node_modules", "playwright"));
  }
}

async function main() {
  const { chromium } = loadPlaywright();
  const outDir = path.resolve("runtime", "direct-playwright", "first-item-imageid");
  const offerId = "841777531690";
  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "profile"), {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });
  const page = context.pages()[0] || await context.newPage();
  await page.goto(`https://detail.1688.com/offer/${offerId}.html?offerId=${offerId}&forcePC=1`, {
    waitUntil: "domcontentloaded",
    timeout: 60000,
  });
  await page.waitForTimeout(12000);
  await page.screenshot({ path: path.join(outDir, `detail-${offerId}-current.png`), fullPage: false });
  const detail = await page.evaluate((offerId) => {
    const body = document.body.innerText.replace(/\s+/g, " ").trim();
    const matches = [];
    for (const re of [/1.?[件个双套].{0,8}起批/g, /运费.{0,20}/g, /包邮/g, /送至.{0,35}/g, /库存.{0,14}/g, /颜色.{0,80}/g, /适合身高.{0,80}/g]) {
      for (const match of body.matchAll(re)) matches.push(match[0]);
    }
    const imgs = Array.from(document.images).slice(0, 60).map((img) => {
      const r = img.getBoundingClientRect();
      return {
        src: img.currentSrc || img.src,
        alt: img.alt || "",
        box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      };
    });
    return {
      offer_id: offerId,
      url: location.href,
      title: document.title,
      body_sample: body.slice(0, 6000),
      evidence_snippets: Array.from(new Set(matches)).slice(0, 40),
      images: imgs,
    };
  }, offerId);
  fs.writeFileSync(path.join(outDir, `detail-${offerId}-current.json`), JSON.stringify(detail, null, 2), "utf8");
  console.log(JSON.stringify(detail, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

