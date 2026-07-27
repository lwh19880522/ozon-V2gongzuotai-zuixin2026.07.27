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
  const root = path.resolve("runtime", "direct-playwright");
  const userDataDir = path.join(root, "profile");
  fs.mkdirSync(root, { recursive: true });

  const context = await chromium.launchPersistentContext(userDataDir, {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });
  const page = context.pages()[0] || await context.newPage();
  await page.goto("https://api64.ipify.org?format=json", { waitUntil: "domcontentloaded" });
  const ipifyBody = await page.textContent("body");
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 45000 });

  const deadline = Date.now() + 10 * 60 * 1000;
  let state = null;
  while (Date.now() < deadline) {
    await page.waitForTimeout(2000);
    state = await page.evaluate(() => ({
      url: location.href,
      title: document.title,
      hasFileInput: !!document.querySelector('input[type="file"]'),
      hasSearchInput: Array.from(document.querySelectorAll("input")).some((el) => {
        const text = `${el.placeholder || ""} ${el.name || ""} ${el.id || ""} ${el.className || ""}`;
        return /搜|search|keyword|q/.test(text);
      }),
      loginLike: /login|signin|member/.test(location.href) || !!document.querySelector("#fm-login-id"),
    }));
    fs.writeFileSync(path.join(root, "session-state.json"), JSON.stringify({
      captured_at: new Date().toISOString(),
      ipifyBody,
      ...state,
    }, null, 2), "utf8");
    if (!state.loginLike && (state.hasFileInput || state.hasSearchInput || /1688\.com\/?$/.test(state.url))) {
      break;
    }
  }

  await page.screenshot({ path: path.join(root, "1688-session-current.png"), fullPage: false });
  console.log(JSON.stringify({
    captured_at: new Date().toISOString(),
    ipifyBody,
    ...state,
    screenshot: path.join(root, "1688-session-current.png"),
    profile: userDataDir,
  }, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
