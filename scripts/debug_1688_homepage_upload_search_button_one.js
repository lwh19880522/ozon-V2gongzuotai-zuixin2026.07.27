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
  const outDir = path.resolve("runtime", "direct-playwright", "upload-search-button-debug");
  fs.mkdirSync(outDir, { recursive: true });
  const imagePath = path.resolve("ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg");
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
  if (chooser) await chooser.setFiles(imagePath);
  else await page.setInputFiles("#img-search-upload", imagePath);
  await page.waitForSelector(".copy-image-container .search-btn", { timeout: 30000 });
  await page.locator(".copy-image-container .search-btn").click({ force: true });
  await page.waitForTimeout(25000);
  await page.screenshot({ path: path.join(outDir, "after-search-button.png"), fullPage: false });
  const result = await page.evaluate(() => ({
    url: location.href,
    title: document.title,
    body: document.body.innerText.replace(/\s+/g, " ").slice(0, 3000),
    detailLinks: Array.from(document.querySelectorAll('a[href*="detail.1688.com/offer/"]')).slice(0, 20).map((a) => a.href),
  }));
  fs.writeFileSync(path.join(outDir, "after-search-button.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
