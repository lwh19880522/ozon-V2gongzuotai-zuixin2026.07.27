const fs = require("fs");
const path = require("path");

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (firstError) {
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

function offerIdFromUrl(value) {
  const match = String(value || "").match(/\/offer\/(\d+)\.html/i);
  return match ? match[1] : null;
}

function isValid1688DetailUrl(value) {
  try {
    const parsed = new URL(String(value || "").trim());
    const hostname = parsed.hostname.toLowerCase();
    const is1688Host = hostname === "1688.com" || hostname.endsWith(".1688.com");
    const isHttp = parsed.protocol === "https:" || parsed.protocol === "http:";
    return isHttp && is1688Host && /^\/offer\/\d+\.html\/?$/i.test(parsed.pathname);
  } catch (_error) {
    return false;
  }
}

async function collectProduct(page, item, artifactsDir, index) {
  const url = String(item.supplier_url || "");
  if (!isValid1688DetailUrl(url)) {
    throw new Error(`Invalid 1688 detail URL for ${item.seed_id || index}`);
  }
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(3500);
  const screenshot = path.join(artifactsDir, `${String(index + 1).padStart(2, "0")}-${item.seed_id || "supplier"}.png`);
  await page.screenshot({ path: screenshot, fullPage: false });

  const evidence = await page.evaluate(() => {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const firstText = (selectors) => {
      for (const selector of selectors) {
        const node = document.querySelector(selector);
        const value = clean(node && (node.textContent || node.getAttribute("content")));
        if (value) return value;
      }
      return "";
    };
    const meta = (name) => clean(document.querySelector(`meta[property="${name}"],meta[name="${name}"]`)?.content);
    const bodyText = clean(document.body?.innerText);
    const lines = String(document.body?.innerText || "").split(/\r?\n/).map(clean).filter(Boolean);
    const jsonLd = [];
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(node.textContent || "null");
        jsonLd.push(...(Array.isArray(parsed) ? parsed : [parsed]));
      } catch (_error) {}
    }
    const productSchema = jsonLd.find((entry) => entry && (entry["@type"] === "Product" || entry.name)) || {};
    const title = firstText(["h1", "[class*=title] h1", "[class*=offer-title]"]) || meta("og:title") || clean(productSchema.name) || clean(document.title);
    const sellerName = firstText([
      "[class*=shop-name]",
      "[class*=company-name]",
      "a[href*=company]",
      "a[href*=winport]",
      "[class*=seller] [class*=name]",
    ]) || clean(productSchema.brand?.name || productSchema.brand);
    const priceText = firstText([
      "[class*=price] [class*=value]",
      "[class*=price]",
      "[data-testid*=price]",
      "meta[itemprop=price]",
    ]) || clean(productSchema.offers?.price);
    const shippingText = lines.find((line) => /(?:运费|包邮|快递|物流).{0,80}/.test(line)) || "";
    const skuLabels = Array.from(document.querySelectorAll(
      "[class*=sku] button,[class*=sku] li,[class*=sku] label,[class*=spec] button,[class*=spec] li,[class*=prop] button"
    )).map((node) => clean(node.textContent)).filter((value) => value && value.length <= 80).slice(0, 80);
    const attributes = {};
    for (const row of document.querySelectorAll("table tr, [class*=attribute] li, [class*=parameter] li")) {
      const cells = Array.from(row.querySelectorAll("th,td,span,div")).map((node) => clean(node.textContent)).filter(Boolean);
      if (cells.length >= 2 && cells[0].length <= 60 && cells[1].length <= 160) attributes[cells[0]] = cells[1];
      if (Object.keys(attributes).length >= 40) break;
    }
    const images = Array.from(document.images)
      .map((image) => image.currentSrc || image.src || image.getAttribute("data-src"))
      .filter((value) => /^https?:\/\//.test(String(value || "")) && /alicdn|1688|cbu01/.test(value))
      .filter((value, index, all) => all.indexOf(value) === index)
      .slice(0, 40);
    const schemaImages = Array.isArray(productSchema.image) ? productSchema.image : [productSchema.image].filter(Boolean);
    for (const image of schemaImages) if (!images.includes(image)) images.unshift(image);
    const shippingFeeMatch = shippingText.match(/(?:运费|快递)\s*[￥¥]?\s*(\d+(?:\.\d+)?)/);
    const freeShipping = /包邮/.test(shippingText);
    return {
      title,
      seller: sellerName ? { shop_name: sellerName } : null,
      sku: skuLabels.length ? { selected_options: { visible_sku_labels: skuLabels } } : null,
      images,
      price: priceText ? { currency: "CNY", visible_text: priceText } : null,
      attributes,
      domestic_shipping_evidence: shippingText ? {
        visible_text: shippingText,
        fee: freeShipping ? "0" : (shippingFeeMatch ? shippingFeeMatch[1] : null),
        free_shipping_visible: freeShipping,
      } : null,
      page_text_sample: bodyText.slice(0, 2000),
    };
  });
  return {
    seed_id: item.seed_id,
    supplier_product_id: offerIdFromUrl(page.url()) || offerIdFromUrl(url),
    supplier_url: url,
    final_url: page.url(),
    title: evidence.title,
    seller: evidence.seller,
    sku: evidence.sku,
    images: evidence.images,
    price: evidence.price,
    attributes: evidence.attributes,
    domestic_shipping_evidence: evidence.domestic_shipping_evidence,
    screenshot,
    collected_at: new Date().toISOString(),
  };
}

async function main() {
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  if (!inputPath || !outputPath) throw new Error("Usage: node collect_1688_supplier_link.js input.json output.json");
  const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
  const artifactsDir = path.resolve(input.artifacts_dir || path.dirname(outputPath));
  fs.mkdirSync(artifactsDir, { recursive: true });
  if (!input.network || input.network.proxy_disabled !== true) throw new Error("Direct network contract is required.");
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch({
    channel: "msedge",
    headless: true,
    args: ["--no-proxy-server"],
  });
  const context = await browser.newContext({ locale: "zh-CN", viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const supplier_products = [];
  const errors = [];
  try {
    for (let index = 0; index < input.items.length; index += 1) {
      try {
        supplier_products.push(await collectProduct(page, input.items[index], artifactsDir, index));
      } catch (error) {
        errors.push(`${input.items[index].seed_id || index}: ${error.message}`);
      }
    }
  } finally {
    await browser.close();
  }
  const output = errors.length ? {
    ok: false,
    message: "One or more 1688 supplier pages could not be collected.",
    errors,
  } : {
    ok: true,
    payload: {
      run_id: input.run_id,
      worker: "local_playwright_direct",
      source: "1688_user_verified_link",
      network: { mode: "direct", proxy_disabled: true },
      supplier_products,
    },
  };
  fs.writeFileSync(outputPath, JSON.stringify(output, null, 2), "utf8");
  if (!output.ok) process.exitCode = 1;
}

module.exports = { isValid1688DetailUrl, offerIdFromUrl };

if (require.main === module) {
  main().catch((error) => {
    const outputPath = process.argv[3];
    if (outputPath) fs.writeFileSync(outputPath, JSON.stringify({ ok: false, message: error.message, errors: [error.stack || error.message] }, null, 2), "utf8");
    console.error(error);
    process.exitCode = 1;
  });
}
