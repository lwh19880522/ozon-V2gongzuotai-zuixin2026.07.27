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
  const outDir = path.resolve("runtime", "direct-playwright", "home-js");
  fs.mkdirSync(outDir, { recursive: true });
  const urls = [
    "https://g.alicdn.com/code/npm/@ali/hyper-project-pc-home2025/1.1.97/js/p_index.js",
    "https://g.alicdn.com/code/npm/@ali/hyper-project-pc-home2025/1.1.97/js/966.js",
    "https://g.alicdn.com/code/npm/@ali/hyper-project-pc-home2025/1.1.97/js/706.js",
    "https://g.alicdn.com/code/npm/@ali/hyper-project-pc-home2025/1.1.97/js/main.js",
  ];
  const context = await chromium.launchPersistentContext(path.resolve("runtime", "direct-playwright", "profile"), {
    channel: "msedge",
    headless: true,
    args: ["--no-proxy-server"],
  });
  const page = context.pages()[0] || await context.newPage();
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
  const outputs = [];
  for (const url of urls) {
    const text = await page.evaluate(async (url) => {
      const response = await fetch(url, { credentials: "include" });
      return response.text();
    }, url);
    const file = path.join(outDir, path.basename(url).replace(/[^a-zA-Z0-9_.-]/g, "_"));
    fs.writeFileSync(file, text, "utf8");
    outputs.push({ url, file, length: text.length });
  }
  await context.close();
  console.log(JSON.stringify(outputs, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
