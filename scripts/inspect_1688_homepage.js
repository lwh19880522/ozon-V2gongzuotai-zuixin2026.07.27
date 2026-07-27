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

async function main() {
  const { chromium } = loadPlaywright();
  const outDir = path.resolve("runtime", "direct-playwright");
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch({
    channel: "msedge",
    headless: true,
    args: ["--no-proxy-server"],
  });
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(3000);
  const info = await page.evaluate(() => {
    const pick = (el) => ({
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title") || "").trim().slice(0, 120),
      type: el.getAttribute("type"),
      name: el.getAttribute("name"),
      id: el.id || null,
      cls: el.className || null,
      placeholder: el.getAttribute("placeholder"),
      href: el.getAttribute("href"),
      role: el.getAttribute("role"),
      accept: el.getAttribute("accept"),
    });
    return {
      url: location.href,
      title: document.title,
      inputs: Array.from(document.querySelectorAll("input")).map(pick),
      buttons: Array.from(document.querySelectorAll("button,[role=button],a")).map(pick).filter((item) => {
        const text = `${item.text || ""} ${item.cls || ""} ${item.id || ""} ${item.href || ""}`.toLowerCase();
        return /图|image|img|camera|相机|搜|search|pic|photo|upload|上传/.test(text);
      }).slice(0, 80),
      fileInputs: Array.from(document.querySelectorAll('input[type="file"]')).map(pick),
    };
  });
  await page.screenshot({ path: path.join(outDir, "1688-homepage-inspect.png"), fullPage: false });
  await browser.close();
  fs.writeFileSync(path.join(outDir, "homepage-inspect.json"), JSON.stringify(info, null, 2), "utf8");
  console.log(JSON.stringify(info, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
