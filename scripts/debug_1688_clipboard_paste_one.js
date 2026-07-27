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
  const outDir = path.resolve("runtime", "direct-playwright", "clipboard-paste-debug");
  fs.mkdirSync(outDir, { recursive: true });
  const imagePath = path.resolve("ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg");
  const imageBase64 = fs.readFileSync(imagePath).toString("base64");
  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "profile"), {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: "https://www.1688.com" });
  const page = context.pages()[0] || await context.newPage();
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForSelector("#alisearch-input", { timeout: 45000 });
  await page.evaluate(async ({ imageBase64 }) => {
    const bytes = Uint8Array.from(atob(imageBase64), (char) => char.charCodeAt(0));
    const blob = new Blob([bytes], { type: "image/jpeg" });
    await navigator.clipboard.write([new ClipboardItem({ "image/jpeg": blob })]);
  }, { imageBase64 });
  await page.locator("#alisearch-input").click({ force: true });
  await page.keyboard.press("Control+V");
  await page.waitForTimeout(8000);
  const hasSearchButton = await page.locator(".copy-image-container .search-btn").count();
  if (hasSearchButton) {
    await page.locator(".copy-image-container .search-btn").click({ force: true });
  }
  await page.waitForTimeout(25000);
  await page.screenshot({ path: path.join(outDir, "after-paste.png"), fullPage: false });
  const result = await page.evaluate(() => ({
    url: location.href,
    title: document.title,
    body: document.body.innerText.replace(/\s+/g, " ").slice(0, 3000),
    detailLinks: Array.from(document.querySelectorAll('a[href*="detail.1688.com/offer/"]')).slice(0, 20).map((a) => a.href),
  }));
  fs.writeFileSync(path.join(outDir, "after-paste.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
