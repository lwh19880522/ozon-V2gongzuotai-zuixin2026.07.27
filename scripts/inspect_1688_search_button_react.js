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
  const outDir = path.resolve("runtime", "direct-playwright", "react-inspect");
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
  await page.waitForTimeout(5000);
  const result = await page.evaluate(() => {
    const describe = (el) => {
      if (!el) return null;
      const keys = Object.keys(el);
      const reactKeys = keys.filter((key) => key.startsWith("__react"));
      const react = {};
      for (const key of reactKeys) {
        const value = el[key];
        if (!value || typeof value !== "object") {
          react[key] = String(value);
          continue;
        }
        const props = value.memoizedProps || value.pendingProps || value;
        react[key] = {
          propKeys: props && typeof props === "object" ? Object.keys(props).slice(0, 80) : [],
          className: props?.className,
          children: typeof props?.children === "string" ? props.children : Array.isArray(props?.children) ? "[array]" : typeof props?.children,
          onClick: props?.onClick ? String(props.onClick).slice(0, 2000) : null,
          onMouseDown: props?.onMouseDown ? String(props.onMouseDown).slice(0, 1000) : null,
        };
      }
      return {
        text: el.innerText,
        className: el.className,
        outerHTML: el.outerHTML.slice(0, 1000),
        react,
      };
    };
    const btn = document.querySelector(".copy-image-container .search-btn");
    const input = document.querySelector("#img-search-upload");
    const container = document.querySelector(".copy-image-container");
    return {
      url: location.href,
      btn: describe(btn),
      input: describe(input),
      container: describe(container),
      windowKeys: Object.keys(window).filter((key) => /image|upload|search|paste/i.test(key)).slice(0, 200),
      localStorage: Object.fromEntries(Object.entries(localStorage).filter(([key]) => /image|upload|search|paste/i.test(key)).slice(0, 50)),
      sessionStorage: Object.fromEntries(Object.entries(sessionStorage).filter(([key]) => /image|upload|search|paste/i.test(key)).slice(0, 50)),
    };
  });
  fs.writeFileSync(path.join(outDir, "react-button.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
