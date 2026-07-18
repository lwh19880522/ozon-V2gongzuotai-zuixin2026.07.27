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
  const resultPath = path.resolve("runtime", "direct-playwright", "first-item-imageid", "result.json");
  const result = JSON.parse(fs.readFileSync(resultPath, "utf8"));
  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "profile"), {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });
  const page = context.pages()[0] || await context.newPage();
  await page.goto(result.searchUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(30000);
  const info = await page.evaluate(() => ({
    url: location.href,
    title: document.title,
    body: document.body.innerText.replace(/\s+/g, " ").slice(0, 6000),
    hrefs: Array.from(document.links).map((a) => ({ text: (a.innerText || "").replace(/\s+/g, " ").trim().slice(0, 200), href: a.href })).slice(0, 300),
    images: Array.from(document.images).map((img) => ({ src: img.currentSrc || img.src, alt: img.alt || "", box: (() => { const r = img.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; })() })).slice(0, 100),
  }));
  await page.screenshot({ path: path.resolve("runtime", "direct-playwright", "first-item-imageid", "inspect-result-page.png"), fullPage: false });
  fs.writeFileSync(path.resolve("runtime", "direct-playwright", "first-item-imageid", "inspect-result-page.json"), JSON.stringify(info, null, 2), "utf8");
  console.log(JSON.stringify(info, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
