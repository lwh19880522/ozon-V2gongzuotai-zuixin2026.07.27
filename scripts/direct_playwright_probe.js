const fs = require("fs");
const path = require("path");

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (error) {
    const fallback = path.join(
      process.env.LOCALAPPDATA || "",
      "npm-cache",
      "_npx",
      "9833c18b2d85bc59",
      "node_modules",
      "playwright"
    );
    return require(fallback);
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
  const page = await browser.newPage();
  await page.goto("https://api64.ipify.org?format=json", { waitUntil: "domcontentloaded" });
  const body = await page.textContent("body");
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
  const title = await page.title();
  await page.screenshot({ path: path.join(outDir, "1688-homepage-probe.png"), fullPage: false });
  await browser.close();

  const result = {
    captured_at: new Date().toISOString(),
    ipify_body: body,
    homepage_url: "https://www.1688.com/",
    homepage_title: title,
    screenshot: path.join(outDir, "1688-homepage-probe.png"),
    proxy_args: ["--no-proxy-server"],
  };
  fs.writeFileSync(path.join(outDir, "probe-result.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
