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
  const context = await chromium.launchPersistentContext(userDataDir, {
    channel: "msedge",
    headless: false,
    viewport: { width: 1365, height: 900 },
    args: ["--no-proxy-server"],
  });
  const page = context.pages()[0] || await context.newPage();
  await page.goto("https://www.1688.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(8000);
  const info = await page.evaluate(() => {
    const brief = (el) => {
      const rect = el.getBoundingClientRect();
      return {
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title") || "").trim().replace(/\s+/g, " ").slice(0, 160),
        type: el.getAttribute("type"),
        name: el.getAttribute("name"),
        id: el.id || null,
        cls: typeof el.className === "string" ? el.className.slice(0, 180) : null,
        placeholder: el.getAttribute("placeholder"),
        href: el.getAttribute("href"),
        role: el.getAttribute("role"),
        accept: el.getAttribute("accept"),
        contenteditable: el.getAttribute("contenteditable"),
        box: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      };
    };
    const all = Array.from(document.querySelectorAll("input,textarea,button,a,[role=button],[contenteditable=true],div,span,i"));
    const interesting = all.filter((el) => {
      const raw = `${el.innerText || ""} ${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${el.getAttribute("placeholder") || ""} ${el.getAttribute("class") || ""} ${el.id || ""} ${el.getAttribute("href") || ""}`;
      return /搜|找货|图片|图|相机|camera|image|photo|upload|pic|search|拍照|上传/.test(raw);
    }).slice(0, 160).map(brief);
    return {
      url: location.href,
      title: document.title,
      bodyText: document.body.innerText.replace(/\s+/g, " ").slice(0, 3000),
      inputs: Array.from(document.querySelectorAll("input,textarea,[contenteditable=true]")).map(brief),
      fileInputs: Array.from(document.querySelectorAll('input[type="file"]')).map(brief),
      interesting,
    };
  });
  await page.screenshot({ path: path.join(root, "1688-homepage-profile-inspect.png"), fullPage: false });
  fs.writeFileSync(path.join(root, "homepage-profile-inspect.json"), JSON.stringify(info, null, 2), "utf8");
  console.log(JSON.stringify(info, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
