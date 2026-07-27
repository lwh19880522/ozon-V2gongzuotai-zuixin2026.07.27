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
  const outDir = path.resolve("runtime", "direct-playwright", "upload-network-debug");
  fs.mkdirSync(outDir, { recursive: true });
  const imagePath = path.resolve("ozon-v2-run-671115c119b7-seed-0149-product-1892738436-main-image.jpg");
  const events = [];
  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "profile"), {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });
  const page = context.pages()[0] || await context.newPage();
  page.on("request", (req) => {
    const url = req.url();
    if (/1688|alicdn|image|upload|search|air/.test(url)) {
      events.push({ type: "request", method: req.method(), url: url.slice(0, 500), at: Date.now() });
    }
  });
  page.on("response", (res) => {
    const url = res.url();
    if (/1688|alicdn|image|upload|search|air/.test(url)) {
      events.push({ type: "response", status: res.status(), url: url.slice(0, 500), at: Date.now() });
    }
  });
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForSelector("#img-search-upload", { timeout: 45000 });
  const chooserPromise = page.waitForEvent("filechooser", { timeout: 15000 }).catch(() => null);
  await page.locator(".image-input-button, .image-upload-button-container, #img-search-upload").first().click({ force: true });
  const chooser = await chooserPromise;
  if (chooser) await chooser.setFiles(imagePath);
  else await page.setInputFiles("#img-search-upload", imagePath);
  await page.waitForTimeout(30000);
  await page.locator(".copy-image-container .search-btn").click({ force: true });
  await page.waitForTimeout(30000);
  const result = {
    url: page.url(),
    title: await page.title(),
    body: (await page.textContent("body")).replace(/\s+/g, " ").slice(0, 2000),
    events,
  };
  fs.writeFileSync(path.join(outDir, "network.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
