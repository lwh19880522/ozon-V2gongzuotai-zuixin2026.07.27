const fs = require("fs");
const os = require("os");
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
  const outDir = path.resolve("runtime", "direct-playwright", "upload-controls");
  fs.mkdirSync(outDir, { recursive: true });
  const imagePath = path.resolve("ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg");
  const profileDir = path.join(os.tmpdir(), `ozonv2-1688-upload-controls-${Date.now()}`);
  const context = await chromium.launchPersistentContext(profileDir, {
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
  await page.waitForTimeout(5000);
  const info = await page.evaluate(() => Array.from(document.querySelectorAll("div,span,button,a,input")).map((el) => {
    const rect = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      text: (el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title") || "").replace(/\s+/g, " ").trim().slice(0, 200),
      id: el.id || null,
      cls: typeof el.className === "string" ? el.className.slice(0, 180) : null,
      type: el.getAttribute("type"),
      href: el.getAttribute("href"),
      visible: rect.width > 0 && rect.height > 0,
      box: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    };
  }).filter((item) => /搜索图片|已上传|以图搜|image|upload|camera|search|copy-image|img-search/.test(`${item.text} ${item.id} ${item.cls}`)).slice(0, 180));
  await page.screenshot({ path: path.join(outDir, "after-upload-controls.png"), fullPage: false });
  fs.writeFileSync(path.join(outDir, "controls.json"), JSON.stringify(info, null, 2), "utf8");
  console.log(JSON.stringify(info, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
