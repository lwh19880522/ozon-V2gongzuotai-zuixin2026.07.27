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
  const outDir = path.resolve("runtime", "direct-playwright", "react-state");
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
    const safe = (value, depth = 0) => {
      if (depth > 3) return "[depth]";
      if (value == null) return value;
      if (typeof value === "function") return `[function ${value.name || "anonymous"}]`;
      if (typeof value !== "object") {
        const text = String(value);
        return text.length > 400 ? text.slice(0, 400) + "..." : text;
      }
      if (value instanceof HTMLElement) return `[HTMLElement ${value.tagName}.${value.className}]`;
      if (Array.isArray(value)) return value.slice(0, 10).map((item) => safe(item, depth + 1));
      const out = {};
      for (const [key, item] of Object.entries(value).slice(0, 30)) {
        if (/alternate|child|sibling|return|stateNode|dependencies|deletions|updateQueue/.test(key)) continue;
        out[key] = safe(item, depth + 1);
      }
      return out;
    };
    const btn = document.querySelector(".copy-image-container .search-btn");
    const fiberKey = Object.keys(btn).find((key) => key.startsWith("__reactFiber$"));
    const rows = [];
    let fiber = btn?.[fiberKey] || null;
    let depth = 0;
    while (fiber && depth < 20) {
      rows.push({
        depth,
        elementType: typeof fiber.elementType === "function" ? fiber.elementType.name : String(fiber.elementType),
        type: typeof fiber.type === "function" ? fiber.type.name : String(fiber.type),
        memoizedProps: safe(fiber.memoizedProps),
        memoizedState: safe(fiber.memoizedState),
      });
      fiber = fiber.return;
      depth += 1;
    }
    return {
      url: location.href,
      rows,
    };
  });
  fs.writeFileSync(path.join(outDir, "state-after-upload.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
