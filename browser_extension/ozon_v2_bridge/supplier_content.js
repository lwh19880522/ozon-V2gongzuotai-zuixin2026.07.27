(function () {
  "use strict";

  if (globalThis.__OzonV2SupplierBridgeLoaded) return;
  globalThis.__OzonV2SupplierBridgeLoaded = true;

  const BASE_URL = "http://127.0.0.1:8765";
  const EXTENSION_VERSION = chrome.runtime.getManifest().version;
  const SUPPLIER_CHANNEL_WAIT_ATTEMPTS = 60;
  const REFERENCE_UPLOAD_ATTEMPTS = 3;
  const REFERENCE_UPLOAD_INPUT_WAIT_MS = 5000;
  let activeRunPromise = null;
  let managedNavigationInstalled = false;
  let activeManagedBinding = null;
  let managedLifecycleInstalled = false;
  let managedReconcileTimer = null;
  let managedPanelObserver = null;
  let managedDragState = null;
  let managedDragListenersInstalled = false;
  let referencePreparationPromise = null;
  let referencePreparationKey = "";

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function api(path, options = {}) {
    const response = await fetch(`${BASE_URL}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    return await response.json();
  }

  async function heartbeat(payload = {}) {
    try {
      await api("/api/browser-bridge/heartbeat", {
        method: "POST",
        body: JSON.stringify({
          bridge_id: "ozon_v2_browser_extension",
          source: "supplier_content_script",
          extension_version: EXTENSION_VERSION,
          url: location.href,
          run_id: payload.run_id || null,
          task_type: "supplier_collection",
          stage: payload.stage || null,
          code: payload.code || null,
          message: payload.message || null,
          details: payload.details && typeof payload.details === "object" ? payload.details : null,
        }),
      });
    } catch (_) {
      // The local workbench may be restarting. The next poll will retry.
    }
  }

  async function currentTask() {
    const response = await chrome.runtime.sendMessage({ type: "ozon_v2_get_current_task" });
    return response && response.ok ? response.task : null;
  }

  async function currentSupplierChannelState() {
    const response = await chrome.runtime.sendMessage({ type: "ozon_v2_get_supplier_channel" });
    return {
      binding: response && response.ok && response.binding ? response.binding : null,
      diagnostics: response && response.diagnostics ? response.diagnostics : null,
    };
  }

  async function currentSupplierChannel() {
    return (await currentSupplierChannelState()).binding;
  }

  function normalizedText(value, limit = 600) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return limit > 0 ? text.slice(0, limit) : text;
  }

  function textOf(node, limit = 600) {
    return normalizedText(node && (node.innerText || node.textContent), limit);
  }

  function firstText(selectors, limit = 300) {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      const value = textOf(node, limit);
      if (value) return value;
    }
    return "";
  }

  function metaContent(selectors) {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      const value = normalizedText(node && node.getAttribute("content"), 500);
      if (value) return value;
    }
    return "";
  }

  function offerId(value) {
    const match = String(value || "").match(/\/offer\/(\d+)\.html/i);
    return match ? match[1] : "";
  }

  function is1688DetailPage(value = location.href) {
    try {
      const url = new URL(value);
      return /(^|\.)1688\.com$/i.test(url.hostname) && !!offerId(url.pathname);
    } catch (_) {
      return false;
    }
  }

  function is1688HomePage(value = location.href) {
    try {
      const url = new URL(value);
      return url.hostname.toLowerCase() === "www.1688.com" && /^\/?$/.test(url.pathname);
    } catch (_) {
      return false;
    }
  }

  function isLoginPage() {
    const host = location.hostname.toLowerCase();
    const body = textOf(document.body, 3000);
    return host === "login.taobao.com"
      || /(^|\.)login\.1688\.com$/i.test(host)
      || /登录|扫码登录|密码登录|手机验证码登录/.test(body);
  }

  function scriptEvidence(patterns) {
    const scripts = [...document.querySelectorAll("script")]
      .map((node) => String(node.textContent || ""))
      .filter(Boolean);
    for (const source of scripts) {
      for (const pattern of patterns) {
        const match = source.match(pattern);
        if (match && match[1]) return normalizedText(match[1].replace(/\\u([0-9a-f]{4})/gi, (_, hex) => String.fromCharCode(parseInt(hex, 16))), 300);
      }
    }
    return "";
  }

  function scriptSources() {
    return [...document.querySelectorAll("script")]
      .map((node) => String(node.textContent || ""))
      .filter(Boolean);
  }

  function extractJsonValue(source, key) {
    const keyPattern = new RegExp(`["']${key}["']\\s*:\\s*`, "g");
    let match = null;
    while ((match = keyPattern.exec(source))) {
      let start = match.index + match[0].length;
      while (/\s/.test(source[start] || "")) start += 1;
      const opener = source[start];
      if (opener !== "{" && opener !== "[") continue;
      const closer = opener === "{" ? "}" : "]";
      let depth = 0;
      let quote = "";
      let escaped = false;
      for (let index = start; index < source.length; index += 1) {
        const character = source[index];
        if (quote) {
          if (escaped) escaped = false;
          else if (character === "\\") escaped = true;
          else if (character === quote) quote = "";
          continue;
        }
        if (character === '"' || character === "'") {
          quote = character;
          continue;
        }
        if (character === opener) depth += 1;
        if (character === closer) depth -= 1;
        if (depth === 0) {
          try {
            return JSON.parse(source.slice(start, index + 1));
          } catch (_) {
            break;
          }
        }
      }
    }
    return null;
  }

  function normalizedImageUrl(value) {
    const url = String(value || "").trim().replace(/^\/\//, "https://");
    return /^https?:\/\//i.test(url) ? url : "";
  }

  function stableTextId(value) {
    const source = String(value || "");
    let hash = 0x811c9dc5;
    for (let index = 0; index < source.length; index += 1) {
      hash ^= source.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
  }

  function firstAttribute(node, names) {
    for (const name of names) {
      const value = normalizedText(node && node.getAttribute && node.getAttribute(name), 160);
      if (value) return value;
    }
    return "";
  }

  function optionImageUrl(node) {
    const image = node && node.querySelector && node.querySelector("img");
    const direct = [
      image && image.currentSrc,
      image && image.src,
      image && image.getAttribute && image.getAttribute("data-src"),
      image && image.getAttribute && image.getAttribute("data-lazy-src"),
      node && node.getAttribute && node.getAttribute("data-image"),
      node && node.getAttribute && node.getAttribute("data-image-url"),
    ].map(normalizedImageUrl).find(Boolean);
    if (direct) return direct;
    const inlineBackground = String(node && node.style && node.style.backgroundImage || "");
    const match = inlineBackground.match(/url\(["']?([^"')]+)["']?\)/i);
    return normalizedImageUrl(match && match[1]);
  }

  function collectVisibleSkuGroups() {
    const groupSelector = [
      "[data-yandex-role='sku-group']",
      ".sku-item-wrapper",
      ".sku-prop",
      "[class*='sku-item-wrapper']",
      "[class*='sku-prop']",
      "[class*='sku-wrapper']",
      "[class*='sku-list']",
      "[class*='skuList']",
      "[class*='sku-content']",
      "[class*='skuContent']",
    ].join(",");
    const optionSelector = [
      "[data-yandex-role='sku-option']",
      "[data-sku-id]",
      "[data-skuId]",
      ".sku-item",
      ".sku-prop-item",
      "[class*='sku-item']",
      "[class*='sku-option']",
      "[class*='sku-value']",
    ].join(",");
    const groups = [];
    const seenGroups = new Set();
    for (const [groupIndex, group] of [...document.querySelectorAll(groupSelector)].entries()) {
      const name = normalizedText(
        firstAttribute(group, ["data-property-name", "data-prop-name", "data-name"])
        || textOf(group.querySelector && group.querySelector(
          "[class*='sku-title'],[class*='prop-title'],[class*='property-title'],[class*='label']"
        ), 100)
        || `规格${groupIndex + 1}`,
        100,
      );
      const optionsByLabel = new Map();
      for (const [optionIndex, option] of [...(group.querySelectorAll && group.querySelectorAll(optionSelector) || [])].entries()) {
        const label = normalizedText(
          firstAttribute(option, ["title", "aria-label", "data-name", "data-value-name"])
          || textOf(option, 120),
          120,
        );
        if (!label || label.length > 80 || label === name || /^(?:\+|-|加|减|展开|收起)$/.test(label)) continue;
        const nativeId = firstAttribute(option, [
          "data-sku-id",
          "data-skuId",
          "data-id",
          "data-value-id",
          "data-vid",
          "data-value",
        ]);
        const imageUrl = optionImageUrl(option);
        const className = normalizedText(option.className, 300);
        const candidate = {
          label,
          supplier_sku_id: nativeId,
          image_url: imageUrl,
          disabled: Boolean(
            option.disabled
            || option.getAttribute && option.getAttribute("aria-disabled") === "true"
            || /\b(?:disabled|sold-out|out-of-stock)\b/i.test(className)
          ),
          selected: Boolean(
            option.getAttribute && option.getAttribute("aria-checked") === "true"
            || option.getAttribute && option.getAttribute("aria-selected") === "true"
            || /\b(?:active|selected|checked)\b/i.test(className)
          ),
          option_index: optionIndex,
        };
        const current = optionsByLabel.get(label);
        const score = Number(Boolean(nativeId)) * 2 + Number(Boolean(imageUrl));
        const currentScore = current
          ? Number(Boolean(current.supplier_sku_id)) * 2 + Number(Boolean(current.image_url))
          : -1;
        if (!current || score > currentScore) optionsByLabel.set(label, candidate);
      }
      const options = [...optionsByLabel.values()];
      if (!options.length) continue;
      const groupKey = `${name}|${options.map((option) => option.label).join("|")}`;
      if (seenGroups.has(groupKey)) continue;
      seenGroups.add(groupKey);
      groups.push({ name, options });
    }
    if (!groups.length) {
      const looseOptions = new Map();
      for (const [index, option] of [...document.querySelectorAll("[data-sku-id], [data-skuId]")].entries()) {
        const label = normalizedText(
          firstAttribute(option, ["title", "aria-label", "data-name", "data-value-name"])
          || textOf(option, 120),
          120,
        );
        if (!label || label.length > 80) continue;
        const supplierSkuId = firstAttribute(option, ["data-sku-id", "data-skuId"]);
        if (!supplierSkuId) continue;
        looseOptions.set(label, {
          label,
          supplier_sku_id: supplierSkuId,
          image_url: optionImageUrl(option),
          disabled: Boolean(option.disabled || option.getAttribute && option.getAttribute("aria-disabled") === "true"),
          selected: Boolean(option.getAttribute && option.getAttribute("aria-checked") === "true"),
          option_index: index,
        });
      }
      if (looseOptions.size) groups.push({ name: "规格", options: [...looseOptions.values()] });
    }
    return groups;
  }

  function visiblePriceAmount(price) {
    const match = String(price && price.visible_text || "").match(/\d+(?:\.\d+)?/);
    return match ? match[0] : "";
  }

  function skuComposition(label, quantity) {
    if (quantity > 1) {
      return /\d+\s*(?:支|件|个|只|套|枚|片|瓶|包|组)/.test(label)
        ? [label]
        : [`${quantity}件装`];
    }
    return [label || "单一商品"];
  }

  function collectDomSkuOptions(groups) {
    if (!Array.isArray(groups) || !groups.length) return [];
    const usableGroups = groups.filter((group) => Array.isArray(group && group.options) && group.options.length);
    if (usableGroups.length !== groups.length) return [];
    const varyingGroups = usableGroups.filter((group) => group.options.length > 1);
    if (varyingGroups.length > 1) return [];
    const group = varyingGroups[0] || usableGroups[0];
    const fixedGroups = usableGroups.filter((candidate) => candidate !== group);
    if (fixedGroups.some((candidate) => candidate.options.length !== 1)) return [];
    const price = collectPrice();
    const amount = visiblePriceAmount(price);
    return group.options.map((option, index) => {
      const label = normalizedText(option.label, 120);
      const fixedOptions = fixedGroups.map((candidate) => candidate.options[0]);
      const imageUrl = [option, ...fixedOptions]
        .map((candidate) => normalizedImageUrl(candidate.image_url))
        .find(Boolean);
      if (!label || !imageUrl) return null;
      const selectedOptions = { [group.name]: label };
      fixedGroups.forEach((candidate, fixedIndex) => {
        selectedOptions[candidate.name] = normalizedText(fixedOptions[fixedIndex].label, 120);
      });
      const rawValues = Object.values(selectedOptions).filter(Boolean);
      const combinationKey = Object.entries(selectedOptions)
        .map(([name, value]) => `${name}>${value}`)
        .join("|");
      const supplierSkuId = normalizedText(
        usableGroups.length === 1 && option.supplier_sku_id
        || `visible-${offerId(location.href)}-${stableTextId(`${combinationKey}|${index}`)}`,
        160,
      );
      const setQuantity = setQuantityFromValues(rawValues);
      return {
        supplier_sku_id: supplierSkuId,
        combination_key: combinationKey,
        raw_label: rawValues.join(" / "),
        selected_options: selectedOptions,
        set_quantity: setQuantity,
        set_composition: skuComposition(rawValues.join(" / "), setQuantity),
        price: { currency: "CNY", amount },
        stock: {
          status: [option, ...fixedOptions].some((candidate) => candidate.disabled) ? "out_of_stock" : "in_stock",
          quantity: null,
        },
        image_urls: [imageUrl],
        evidence_source: usableGroups.length === 1 ? "dom_single_group_sku" : "dom_single_axis_sku",
        complete: true,
        evidence: {
          group_names: usableGroups.map((candidate) => candidate.name),
          varying_group_name: group.name,
          fixed_group_names: fixedGroups.map((candidate) => candidate.name),
          option_index: option.option_index,
          native_supplier_sku_id: usableGroups.length === 1 && Boolean(option.supplier_sku_id),
          price_visible_text: price && price.visible_text || "",
          price_independent_sku_selection: true,
        },
      };
    }).filter(Boolean);
  }

  function collectSingleSkuOption(groups) {
    if (Array.isArray(groups) && groups.length) return [];
    const currentOfferId = offerId(location.href);
    const price = collectPrice();
    const amount = visiblePriceAmount(price);
    const images = collectImages();
    if (!currentOfferId || !images.length) return [];
    const title = collectTitle();
    const attributes = collectAttributes();
    const setQuantity = setQuantityFromValues([title, ...Object.values(attributes)]);
    const label = setQuantity > 1 ? `${setQuantity}件装` : "单一 SKU";
    return [{
      supplier_sku_id: currentOfferId,
      combination_key: label,
      raw_label: label,
      selected_options: { 规格: label },
      set_quantity: setQuantity,
      set_composition: skuComposition(label, setQuantity),
      price: { currency: "CNY", amount },
      stock: {
        status: /已下架|暂时缺货|无货|售罄/.test(textOf(document.body, 100000)) ? "out_of_stock" : "in_stock",
        quantity: null,
      },
      image_urls: [images[0]],
      evidence_source: "single_sku_detail_page",
      complete: true,
      evidence: {
        offer_id: currentOfferId,
        no_visible_variant_selector: true,
        price_visible_text: price && price.visible_text || "",
        price_independent_sku_selection: true,
      },
    }];
  }

  function collectTableAttributePairs() {
    const pairs = [];
    for (const row of document.querySelectorAll("tr")) {
      const cells = row.querySelectorAll("th,td");
      if (cells.length < 2) continue;
      const key = normalizedText(textOf(cells[0], 100), 100).replace(/[：:]$/, "");
      const value = normalizedText(textOf(cells[1], 300), 300);
      if (key && value && key !== value) pairs.push({ key, value });
    }
    return pairs;
  }

  function specificationDimension(value) {
    const match = normalizedText(value, 100).match(
      /^(全长|长度|宽度|高度|直径|尺寸)\s*(?:[（(]\s*(mm|cm|毫米|厘米)\s*[)）])?$/i,
    );
    if (!match) return null;
    const rawUnit = String(match[2] || "").toLowerCase();
    return {
      label: match[1],
      unit: rawUnit === "cm" || rawUnit === "厘米" ? "厘米" : "毫米",
    };
  }

  function specificationModelRow(key, value) {
    const keyMatch = normalizedText(key, 180).match(
      /^([a-z0-9][a-z0-9._/-]{1,31})\s*[（(]\s*(.{2,120}?)\s*[)）]$/i,
    );
    const valueMatch = normalizedText(value, 80).match(
      /^(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)?$/i,
    );
    if (!keyMatch || !valueMatch) return null;
    return {
      model: keyMatch[1],
      specification: normalizedText(keyMatch[2], 120),
      measurement: valueMatch[1],
      unit: String(valueMatch[2] || "").toLowerCase(),
    };
  }

  function collectSpecificationTable(pairs = null) {
    const sourcePairs = Array.isArray(pairs) ? pairs : collectTableAttributePairs();
    const header = sourcePairs.map((pair, index) => ({
      ...pair,
      index,
      dimension: /^(?:型号|款号|货号|产品规格)$/.test(pair.key)
        ? specificationDimension(pair.value)
        : null,
    })).find((pair) => pair.dimension);
    if (!header) return null;
    const rows = sourcePairs.map((pair, index) => ({
      ...pair,
      index,
      parsed: specificationModelRow(pair.key, pair.value),
    })).filter((pair) => pair.parsed && pair.index > header.index);
    if (rows.length < 2) return null;
    return {
      header_key: header.key,
      header_value: header.value,
      dimension_label: header.dimension.label,
      unit: header.dimension.unit,
      rows: rows.map((row) => ({ key: row.key, value: row.value, ...row.parsed })),
      consumed_keys: new Set([header.key, ...rows.map((row) => row.key)]),
    };
  }

  function collectSpecificationTableSkuOptions(specificationTable) {
    if (!specificationTable || !Array.isArray(specificationTable.rows)) return [];
    const currentOfferId = offerId(location.href);
    const price = collectPrice();
    const amount = visiblePriceAmount(price);
    const images = collectImages();
    if (!currentOfferId || !images.length) return [];
    const soldOut = /已下架|暂时缺货|无货|售罄/.test(textOf(document.body, 100000));
    return specificationTable.rows.map((row, index) => {
      const rawUnit = String(row.unit || "").toLowerCase();
      const unit = rawUnit === "cm" || rawUnit === "厘米" ? "厘米" : specificationTable.unit;
      const measurement = `${row.measurement}${unit}`;
      const selectedOptions = {
        型号: row.model,
        规格: row.specification,
        [specificationTable.dimension_label]: measurement,
      };
      const rawLabel = `${row.model} / ${row.specification} / ${measurement}`;
      return {
        supplier_sku_id: `spec-${currentOfferId}-${stableTextId(`${row.key}|${row.value}|${index}`)}`,
        combination_key: rawLabel,
        raw_label: rawLabel,
        selected_options: selectedOptions,
        set_quantity: 1,
        set_composition: [row.specification],
        price: { currency: "CNY", amount },
        stock: { status: soldOut ? "out_of_stock" : "in_stock", quantity: null },
        image_urls: [images[0]],
        evidence_source: "dom_specification_table",
        complete: true,
        evidence: {
          offer_id: currentOfferId,
          header_key: specificationTable.header_key,
          header_value: specificationTable.header_value,
          source_row_key: row.key,
          source_row_value: row.value,
        price_visible_text: price && price.visible_text || "",
        price_independent_sku_selection: true,
        },
      };
    });
  }

  function skuPropertyDefinitions(rawProps) {
    if (!Array.isArray(rawProps)) return [];
    return rawProps.map((raw) => {
      const name = normalizedText(raw.prop || raw.name || raw.propName || raw.attributeName, 100);
      const values = Array.isArray(raw.value) ? raw.value : (Array.isArray(raw.values) ? raw.values : []);
      const images = {};
      for (const item of values) {
        const value = normalizedText(item && (item.name || item.value || item.valueName), 120);
        const image = normalizedImageUrl(item && (item.imageUrl || item.image || item.imageURL));
        if (value && image) images[value] = image;
      }
      return { name, images };
    }).filter((item) => item.name);
  }

  function collectEmbeddedSkuGroups() {
    const groups = [];
    const seen = new Set();
    for (const source of scriptSources()) {
      const rawProps = extractJsonValue(source, "skuProps")
        || extractJsonValue(source, "skuPropertyList");
      if (!Array.isArray(rawProps)) continue;
      for (const rawGroup of rawProps) {
        const name = normalizedText(
          rawGroup && (rawGroup.prop || rawGroup.name || rawGroup.propName || rawGroup.attributeName),
          100,
        );
        const rawValues = rawGroup && (Array.isArray(rawGroup.value) ? rawGroup.value : rawGroup.values);
        if (!name || !Array.isArray(rawValues)) continue;
        const options = rawValues.map((rawOption, optionIndex) => {
          const option = rawOption && typeof rawOption === "object" ? rawOption : {};
          const label = normalizedText(option.name || option.value || option.valueName, 120);
          if (!label) return null;
          return {
            label,
            supplier_sku_id: normalizedText(
              option.vid || option.id || option.valueId || option.propValueId,
              160,
            ),
            image_url: normalizedImageUrl(option.imageUrl || option.image || option.imageURL),
            disabled: option.disabled === true || option.canBookCount === 0,
            selected: option.selected === true,
            option_index: optionIndex,
          };
        }).filter(Boolean);
        if (!options.length) continue;
        const key = `${name}|${options.map((option) => option.label).join("|")}`;
        if (seen.has(key)) continue;
        seen.add(key);
        groups.push({ name, options });
      }
    }
    return groups;
  }

  function splitSkuValues(rawValue) {
    return normalizedText(rawValue, 500)
      .replace(/&gt;/gi, ">")
      .split(/\s*(?:>|;|；|\|)\s*/)
      .map((value) => normalizedText(value, 120))
      .filter(Boolean);
  }

  function setQuantityFromValues(values) {
    for (const value of values) {
      const multipliers = [...String(value || "").matchAll(/(?:x|×|\*)\s*(\d+)\b/gi)]
        .map((match) => Number(match[1]))
        .filter((quantity) => Number.isFinite(quantity) && quantity > 0);
      if (multipliers.length) {
        return multipliers.reduce((total, quantity) => total + quantity, 0);
      }
      const documentedCounts = [...String(value || "").matchAll(/(\d+)\s*(?:支|件|个|只|套|枚|片|瓶|包|组)/g)]
        .map((match) => Number(match[1]))
        .filter((quantity) => Number.isFinite(quantity) && quantity > 0);
      if (documentedCounts.length) {
        return documentedCounts.reduce((total, quantity) => total + quantity, 0);
      }
    }
    return 1;
  }

  function collectTrustedSkuOptions(visibleGroups = null, specificationTable = null) {
    const options = [];
    const seen = new Set();
    for (const source of scriptSources()) {
      const skuMap = [
        extractJsonValue(source, "skuMap"),
        extractJsonValue(source, "skuInfoMap"),
      ].find((candidate) => candidate && !Array.isArray(candidate) && typeof candidate === "object");
      if (!skuMap) continue;
      const definitions = skuPropertyDefinitions(
        extractJsonValue(source, "skuProps") || extractJsonValue(source, "skuPropertyList")
      );
      for (const [mapKey, rawRow] of Object.entries(skuMap)) {
        const row = rawRow && typeof rawRow === "object" ? rawRow : {};
        const supplierSkuId = normalizedText(
          row.skuId || row.skuID || row.specId || row.specID || row.id || row.cargoNumber,
          160,
        );
        if (!supplierSkuId || seen.has(supplierSkuId)) continue;
        const rawValues = splitSkuValues(row.specAttrs || row.specAttr || mapKey);
        const selectedOptions = {};
        rawValues.forEach((value, index) => {
          selectedOptions[definitions[index] && definitions[index].name ? definitions[index].name : `规格${index + 1}`] = value;
        });
        const imageCandidates = [row.imageUrl, row.image, row.imageURL];
        rawValues.forEach((value, index) => {
          if (definitions[index] && definitions[index].images[value]) imageCandidates.push(definitions[index].images[value]);
        });
        const imageUrls = imageCandidates.map(normalizedImageUrl).filter((value, index, all) => value && all.indexOf(value) === index);
        const amount = normalizedText(
          row.discountPrice || row.price || row.salePrice || row.priceValue || row.priceText,
          80,
        ).replace(/^[¥￥]\s*/, "");
        const quantityValue = row.canBookCount ?? row.stock ?? row.quantity ?? row.amountOnSale;
        const quantity = Number(quantityValue);
        const setQuantity = setQuantityFromValues(rawValues);
        const setComposition = rawValues.filter((value) => setQuantityFromValues([value]) === setQuantity && setQuantity > 1);
        const complete = Boolean(
          supplierSkuId
          && Object.keys(selectedOptions).length
          && Number.isFinite(quantity)
          && imageUrls.length
        );
        options.push({
          supplier_sku_id: supplierSkuId,
          combination_key: normalizedText(row.specAttrs || row.specAttr || mapKey, 500),
          raw_label: rawValues.join(" / "),
          selected_options: selectedOptions,
          set_quantity: setQuantity,
          set_composition: setComposition.length ? setComposition : [rawValues.join(" / ") || "single item"],
          price: { currency: "CNY", amount },
          stock: {
            status: Number.isFinite(quantity) && quantity > 0 ? "in_stock" : "out_of_stock",
            quantity: Number.isFinite(quantity) ? quantity : null,
          },
          image_urls: imageUrls,
          evidence_source: "embedded_sku_map",
          complete,
          evidence: {
            sku_map_key: mapKey,
            price_independent_sku_selection: true,
          },
        });
        seen.add(supplierSkuId);
      }
    }
    if (options.length) return options;
    const groups = Array.isArray(visibleGroups) ? visibleGroups : collectVisibleSkuGroups();
    const domOptions = collectDomSkuOptions(groups);
    if (domOptions.length) return domOptions;
    const specificationOptions = collectSpecificationTableSkuOptions(specificationTable);
    return specificationOptions.length ? specificationOptions : collectSingleSkuOption(groups);
  }

  function collectTitle() {
    return firstText([
      "[data-testid='offer-title']",
      "[class*='offer-title']",
      "[class*='offerTitle']",
      "[class*='title-text']",
      "[class*='titleText']",
    ], 500)
      || metaContent(["meta[property='og:title']", "meta[name='title']"])
      || scriptEvidence([
        /"offerTitle"\s*:\s*"([^"]+)"/i,
        /"subject"\s*:\s*"([^"]+)"/i,
      ])
      || firstText(["main h1", "h1"], 500)
      || normalizedText(document.title, 500);
  }

  function collectSeller() {
    const shopName = firstText([
      "[class*='shop-company-name']",
      "[class*='company-name']",
      "[class*='companyName']",
      "[class*='shopName']",
      "[class*='sellerName']",
      "a[href*='company.1688.com']",
      "a[href*='winport.1688.com']",
    ], 300) || scriptEvidence([
      /"companyName"\s*:\s*"([^"]+)"/i,
      /"shopName"\s*:\s*"([^"]+)"/i,
      /"sellerLoginId"\s*:\s*"([^"]+)"/i,
    ]);
    const body = textOf(document.body, 100000);
    const companyMatch = body.match(/([\u4e00-\u9fffA-Za-z0-9（）()·&-]{2,60}(?:有限责任公司|有限公司|商行|经营部|工厂|厂))/);
    const resolvedName = shopName || (companyMatch ? normalizedText(companyMatch[1], 300) : "");
    return resolvedName ? { shop_name: resolvedName } : null;
  }

  function collectImages() {
    const productRegionSelector = [
      "[data-testid*='gallery']",
      "[class*='offer-img']",
      "[class*='offerImg']",
      "[class*='main-image']",
      "[class*='mainImage']",
      "[class*='image-list']",
      "[class*='imageList']",
      "[class*='detail-gallery']",
      "[class*='detailGallery']",
      "[class*='detail-content']",
      "[class*='detailContent']",
      "[class*='sku']",
    ].join(",");
    const values = [];
    const fallbackValues = [];
    const isTrustedProductImage = (value) =>
      /(?:cbu\d+\.alicdn\.com\/img\/ibank|img\.alicdn\.com\/imgextra)\//i.test(String(value || ""));
    const add = (value) => {
      if (!value) return;
      const url = String(value).replace(/^\/\//, "https://");
      if (!/^https?:\/\//i.test(url) || !/alicdn\.com|1688\.com/i.test(url)) return;
      if (/\.svg(?:[?#]|$)|-55-tps-|(?:^|[\/_-])(icon|logo|avatar|sprite)(?:[\/_-]|\.)/i.test(url)) return;
      if (/_sum\.(?:jpg|jpeg|png|webp)(?:[?#]|$)/i.test(url)) return;
      if (!values.includes(url)) values.push(url);
    };
    add(metaContent(["meta[property='og:image']"]));
    for (const image of document.querySelectorAll("img")) {
      const width = Number(image.naturalWidth || image.width || image.getAttribute("width") || 0);
      const height = Number(image.naturalHeight || image.height || image.getAttribute("height") || 0);
      if (width > 0 && height > 0 && (width < 300 || height < 300)) continue;
      const urls = [
        image.currentSrc,
        image.src,
        image.getAttribute("data-src"),
        image.getAttribute("data-lazyload-src"),
        image.getAttribute("data-lazy-src"),
      ].filter(Boolean);
      const inProductRegion = typeof image.closest !== "function" || image.closest(productRegionSelector);
      for (const url of urls) {
        if (inProductRegion) add(url);
        else if (isTrustedProductImage(url) && !fallbackValues.includes(url)) fallbackValues.push(url);
      }
    }
    if (!values.length) fallbackValues.forEach(add);
    if (!values.length) {
      for (const script of document.querySelectorAll("script")) {
        const source = String(script.textContent || "")
          .replace(/\\u002f/gi, "/")
          .replace(/\\\//g, "/");
        const matches = source.match(/(?:https?:)?\/\/(?:cbu\d+|img)\.alicdn\.com\/(?:img\/ibank|imgextra)\/[^"'\\\s<>{}]+?\.(?:jpe?g|png|webp)(?:_[^"'\\\s<>{}]*)?/gi) || [];
        matches.forEach(add);
      }
    }
    return values.slice(0, 30);
  }

  function collectPrice() {
    const meta = metaContent([
      "meta[property='product:price:amount']",
      "meta[itemprop='price']",
    ]);
    const body = textOf(document.body, 100000);
    const visible = firstText([
      "[class*='price']",
      "[class*='Price']",
      "[data-testid*='price']",
    ], 300);
    const pattern = /(?:¥|￥)\s*\d+(?:\s*\.\s*\d+)?(?:\s*[-~至]\s*(?:¥|￥)?\s*\d+(?:\s*\.\s*\d+)?)?/;
    const match = visible.match(pattern) || body.match(pattern);
    const raw = meta || (match ? match[0] : "");
    return raw
      ? { currency: "CNY", visible_text: normalizedText(raw, 120).replace(/\s+/g, "") }
      : null;
  }

  function collectShipping() {
    const body = textOf(document.body, 100000);
    const nodeText = firstText([
      "[class*='logistics']",
      "[class*='freight']",
      "[class*='delivery']",
      "[class*='shipping']",
    ], 600);
    const match = body.match(/送至.{0,180}?(?:包邮|运费\s*[¥￥]?\s*\d+(?:\.\d+)?(?:起)?)/)
      || body.match(/(?:^|\s)(?:包邮|运费\s*[¥￥]?\s*\d+(?:\.\d+)?(?:起)?)(?:\s|$)/);
    const visibleText = normalizedText(nodeText || (match && match[0]), 600);
    if (!visibleText) return null;
    const feeMatch = visibleText.match(/(?:运费|快递费|配送费)[^¥￥\d]{0,10}(?:¥|￥)?\s*(\d+(?:\.\d+)?)/);
    const free = /包邮|运费\s*[¥￥]?\s*0(?:\.0+)?/.test(visibleText);
    return {
      visible_text: visibleText,
      fee: free ? 0 : (feeMatch ? Number(feeMatch[1]) : null),
      free_shipping_visible: free,
    };
  }

  function collectAttributes(specificationTable = null, tablePairs = null) {
    const attributes = {};
    const add = (key, value) => {
      const cleanKey = normalizedText(key, 100).replace(/[：:]$/, "");
      const cleanValue = normalizedText(value, 300);
      if (specificationTable && specificationTable.consumed_keys.has(cleanKey)) return;
      if (
        specificationTable
        && /^(?:型号|款号|货号|产品规格)$/.test(cleanKey)
        && specificationDimension(cleanValue)
      ) return;
      if (specificationTable && /^[a-z0-9][a-z0-9._/-]{1,31}\s*[（(]/i.test(cleanKey)) return;
      if (specificationTable && /^(?:全部|全选|不限)/.test(cleanValue)) return;
      if (specificationTable && /(?:展开参数|收起参数)$/.test(cleanValue)) return;
      if (cleanKey && cleanValue && cleanKey !== cleanValue && !attributes[cleanKey]) attributes[cleanKey] = cleanValue;
    };
    const pairs = Array.isArray(tablePairs) ? tablePairs : collectTableAttributePairs();
    for (const pair of pairs) add(pair.key, pair.value);
    for (const node of document.querySelectorAll("[class*='attribute'], [class*='parameter'], [class*='property']")) {
      const text = textOf(node, 500);
      const split = text.match(/^([^：:]{1,40})[：:]\s*(.{1,300})$/);
      if (split) add(split[1], split[2]);
    }
    return attributes;
  }

  function collectSku(visibleGroups = null, skuOptions = null) {
    const groups = Array.isArray(visibleGroups) ? visibleGroups : collectVisibleSkuGroups();
    const labels = [];
    for (const group of groups) {
      for (const option of group.options) {
        if (option.label && !labels.includes(option.label)) labels.push(option.label);
      }
    }
    const specificationOptions = Array.isArray(skuOptions)
      ? skuOptions.filter((option) => option.evidence_source === "dom_specification_table")
      : [];
    if (!labels.length && specificationOptions.length) {
      for (const option of specificationOptions) labels.push(option.raw_label);
    }
    return {
      selected_options: {
        visible_sku_labels: labels.length ? labels.slice(0, 12) : ["单一 SKU（页面无可选规格）"],
      },
      evidence: specificationOptions.length
        ? "specification_table_sku_rows"
        : (labels.length ? "visible_selected_or_available_sku_labels" : "no_visible_variant_selector"),
      evidence_source: specificationOptions.length ? "dom_specification_table" : "dom_option_labels",
      complete: false,
    };
  }

  function collectProduct(item) {
    const visibleSkuGroups = collectVisibleSkuGroups();
    const skuGroups = visibleSkuGroups.length ? visibleSkuGroups : collectEmbeddedSkuGroups();
    const tablePairs = collectTableAttributePairs();
    const specificationTable = collectSpecificationTable(tablePairs);
    const skuOptions = collectTrustedSkuOptions(skuGroups, specificationTable);
    return {
      seed_id: String(item.seed_id || ""),
      supplier_url: String(item.supplier_url || ""),
      final_url: location.href,
      offer_id: offerId(location.href),
      title: collectTitle(),
      seller: collectSeller(),
      sku: collectSku(skuGroups, skuOptions),
      sku_groups: skuGroups,
      sku_options: skuOptions,
      images: collectImages(),
      price: collectPrice(),
      attributes: collectAttributes(specificationTable, tablePairs),
      domestic_shipping_evidence: collectShipping(),
    };
  }

  function missingRequired(product, { requireSkuOptions = true } = {}) {
    const requiredFields = ["title", "seller", "sku", "images", "domestic_shipping_evidence"];
    if (requireSkuOptions) requiredFields.splice(3, 0, "sku_options");
    return requiredFields.filter((field) => {
      const value = product[field];
      return value == null || value === "" || (Array.isArray(value) && !value.length);
    });
  }

  function detailUrlFromEvent(event) {
    const anchor = event && event.target && typeof event.target.closest === "function"
      ? event.target.closest("a[href]")
      : null;
    if (!anchor) return "";
    try {
      const target = new URL(anchor.href || anchor.getAttribute("href"), location.href);
      if (!/(^|\.)1688\.com$/i.test(target.hostname)) return "";
      if (!/^\/offer\/\d+\.html$/i.test(target.pathname)) return "";
      return target.href;
    } catch (_) {
      return "";
    }
  }

  function managedDetailUrl(event) {
    if (!event || event.defaultPrevented || event.button !== 0) return "";
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return "";
    return detailUrlFromEvent(event);
  }

  function installManagedSameTabNavigation() {
    if (managedNavigationInstalled) return;
    managedNavigationInstalled = true;
    document.addEventListener("pointerdown", (event) => {
      const detailUrl = detailUrlFromEvent(event);
      const explicitNewTab = event.button === 1
        || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey;
      if (!detailUrl || !explicitNewTab) return;
      chrome.runtime.sendMessage({
        type: "ozon_v2_supplier_native_new_tab_intent",
        url: detailUrl,
      }).catch(() => null);
    }, true);
    document.addEventListener("click", (event) => {
      const detailUrl = managedDetailUrl(event);
      if (!detailUrl) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      location.assign(detailUrl);
    }, true);
  }

  function referencePreparedStateKey(binding) {
    return `ozon_v2_reference_prepared_${binding.run_id}_${binding.seed_id}`;
  }

  function clampManagedPanelPosition(panel, position) {
    const maxLeft = Math.max(0, Number(globalThis.innerWidth || 0) - Number(panel.offsetWidth || 0));
    const maxTop = Math.max(0, Number(globalThis.innerHeight || 0) - Number(panel.offsetHeight || 0));
    return {
      left: Math.min(maxLeft, Math.max(0, Number(position && position.left) || 0)),
      top: Math.min(maxTop, Math.max(0, Number(position && position.top) || 0)),
    };
  }

  async function restoreManagedPanelPosition(panel) {
    const stored = await chrome.storage.local.get({ supplierPanelPosition: null });
    if (!stored.supplierPanelPosition || !document.getElementById(panel.id)) return;
    const position = clampManagedPanelPosition(panel, stored.supplierPanelPosition);
    panel.style.left = `${position.left}px`;
    panel.style.top = `${position.top}px`;
    panel.style.right = "auto";
    panel.style.bottom = "auto";
  }

  function installManagedPanelDrag(panel) {
    panel.addEventListener("pointerdown", (event) => {
      if (event.target && String(event.target.tagName || "").toLowerCase() === "button") return;
      const current = clampManagedPanelPosition(panel, {
        left: Number.parseFloat(panel.style.left) || Math.max(0, globalThis.innerWidth - panel.offsetWidth - 18),
        top: Number.parseFloat(panel.style.top) || Math.max(0, globalThis.innerHeight - panel.offsetHeight - 18),
      });
      managedDragState = {
        panel,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        left: current.left,
        top: current.top,
      };
      if (typeof panel.setPointerCapture === "function") panel.setPointerCapture(event.pointerId);
      if (typeof event.preventDefault === "function") event.preventDefault();
    });
    if (managedDragListenersInstalled) return;
    managedDragListenersInstalled = true;
    addEventListener("pointermove", (event) => {
      if (!managedDragState || event.pointerId !== managedDragState.pointerId) return;
      const position = clampManagedPanelPosition(managedDragState.panel, {
        left: managedDragState.left + event.clientX - managedDragState.startX,
        top: managedDragState.top + event.clientY - managedDragState.startY,
      });
      managedDragState.panel.style.left = `${position.left}px`;
      managedDragState.panel.style.top = `${position.top}px`;
      managedDragState.panel.style.right = "auto";
      managedDragState.panel.style.bottom = "auto";
    });
    addEventListener("pointerup", (event) => {
      if (!managedDragState || event.pointerId !== managedDragState.pointerId) return;
      const position = {
        left: Number.parseFloat(managedDragState.panel.style.left) || 0,
        top: Number.parseFloat(managedDragState.panel.style.top) || 0,
      };
      managedDragState = null;
      chrome.storage.local.set({ supplierPanelPosition: position }).catch(() => null);
    });
  }

  function createManagedPanel(binding) {
    const panel = document.createElement("section");
    panel.id = "ozon-v2-supplier-panel";
    Object.assign(panel.style, {
      position: "fixed",
      right: "18px",
      bottom: "18px",
      zIndex: "2147483647",
      width: "300px",
      padding: "14px",
      border: "1px solid #cbd5e1",
      borderRadius: "8px",
      background: "#ffffff",
      color: "#172033",
      boxShadow: "0 12px 32px rgba(15, 23, 42, 0.2)",
      font: "13px/1.45 system-ui, sans-serif",
    });
    const back = document.createElement("button");
    back.id = "ozon-v2-supplier-back";
    back.textContent = "← 返回选品";
    Object.assign(back.style, {
      minHeight: "30px",
      marginRight: "8px",
      border: "1px solid #cbd5e1",
      borderRadius: "6px",
      background: "#f8fafc",
      color: "#334155",
      cursor: "pointer",
    });
    const title = document.createElement("strong");
    title.setAttribute("data-role", "channel-title");
    title.textContent = `通道 ${Number(binding.channel_index) + 1} · ${binding.ozon_title || binding.seed_id}`;
    const status = document.createElement("div");
    status.id = "ozon-v2-supplier-status";
    status.textContent = is1688DetailPage()
      ? "详情页已就绪，请确认同款后采集 (Ready)"
      : "请用参考图搜索并打开同款详情页 (Open Detail)";
    Object.assign(status.style, { margin: "8px 0 10px", color: "#526179" });
    const collect = document.createElement("button");
    collect.id = "ozon-v2-collect-current-product";
    collect.textContent = "采集当前商品 (Collect Current Product)";
    Object.assign(collect.style, {
      width: "100%",
      minHeight: "38px",
      border: "0",
      borderRadius: "6px",
      background: "#2457d6",
      color: "white",
      cursor: "pointer",
      fontWeight: "600",
    });
    const reject = document.createElement("button");
    reject.id = "ozon-v2-no-supplier";
    reject.textContent = "无供应商 (No Supplier Found)";
    Object.assign(reject.style, {
      width: "100%",
      minHeight: "36px",
      marginTop: "8px",
      border: "1px solid #e7a5a5",
      borderRadius: "6px",
      background: "#fff7f7",
      color: "#b42318",
      cursor: "pointer",
    });
    back.onclick = async () => {
      const current = activeManagedBinding || binding;
      if (globalThis.sessionStorage) {
        globalThis.sessionStorage.removeItem(referencePreparedStateKey(current));
      }
      const response = await chrome.runtime.sendMessage({ type: "ozon_v2_supplier_channel_back" });
      if (!response || response.ok !== true) {
        status.textContent = "返回失败，请重试";
      }
      return response;
    };
    collect.onclick = async () => {
      if (!is1688DetailPage()) {
        status.textContent = "请先进入 1688 商品详情页 (Detail Page Required)";
        return;
      }
      collect.disabled = true;
      status.textContent = "正在读取公开商品数据 (Collecting)";
      const current = activeManagedBinding || binding;
      const item = { seed_id: current.seed_id, supplier_url: location.href };
      const product = collectProduct(item);
      const missing = missingRequired(product, { requireSkuOptions: false });
      if (missing.length) {
        collect.disabled = false;
        status.textContent = `采集不完整，请等待页面加载：${missing.join(", ")} (Incomplete)`;
        return;
      }
      const result = await api(current.capture_url, {
        method: "POST",
        body: JSON.stringify({
          channel_index: current.channel_index,
          seed_id: current.seed_id,
          ozon_product_id: current.ozon_product_id,
          supplier_product: product,
        }),
      });
      const skuMatrixDeferred = !(product.sku_options || []).length;
      status.textContent = result.ok
        ? skuMatrixDeferred
          ? "公开数据已采集；完整 SKU 待工具台确认 (Collected)"
          : "采集成功，已回传工具台 (Collected)"
        : `采集失败：${result.message || result.code || "unknown"} (Failed)`;
      collect.disabled = result.ok === true;
      if (result.ok === true) {
        await chrome.runtime.sendMessage({
          type: "ozon_v2_supplier_channel_terminal",
          state: "collected",
        });
      }
    };
    reject.onclick = async () => {
      reject.disabled = true;
      status.textContent = "正在剔除并补位 (Replacing)";
      const response = await chrome.runtime.sendMessage({ type: "ozon_v2_reject_supplier_channel" });
      status.textContent = response && response.ok
        ? "已加入黑名单，工具台正在补位 (Replaced)"
        : "提交失败，请重试 (Retry)";
      reject.disabled = !!(response && response.ok);
    };
    panel.appendChild(back);
    panel.appendChild(title);
    panel.appendChild(status);
    panel.appendChild(collect);
    panel.appendChild(reject);
    installManagedPanelDrag(panel);
    document.body.appendChild(panel);
    return panel;
  }

  async function prepareReferenceImageOnce(binding) {
    if (!is1688HomePage()) return false;
    const key = referencePreparedStateKey(binding);
    if (referencePreparationPromise && referencePreparationKey === key) {
      return await referencePreparationPromise;
    }
    referencePreparationKey = key;
    referencePreparationPromise = prepareReferenceImage(binding);
    try {
      return await referencePreparationPromise;
    } finally {
      referencePreparationPromise = null;
      referencePreparationKey = "";
    }
  }

  async function reconcileManagedPanel(binding = activeManagedBinding) {
    const resolved = binding || await currentSupplierChannel();
    if (!resolved) return null;
    activeManagedBinding = resolved;
    let panel = document.getElementById("ozon-v2-supplier-panel");
    const created = !panel;
    if (!panel) panel = createManagedPanel(resolved);
    panel.dataset.channelIndex = String(resolved.channel_index);
    const title = panel.querySelector("[data-role='channel-title']");
    if (title) title.textContent = `通道 ${Number(resolved.channel_index) + 1} · ${resolved.ozon_title || resolved.seed_id}`;
    const detailReady = is1688DetailPage();
    const collect = panel.querySelector("#ozon-v2-collect-current-product");
    const status = panel.querySelector("#ozon-v2-supplier-status");
    if (collect) collect.disabled = !detailReady;
    if (status) {
      status.textContent = detailReady
        ? "当前商品可以采集"
        : "请选择同款商品并进入详情页";
    }
    if (created) await restoreManagedPanelPosition(panel);
    if (is1688HomePage()) await prepareReferenceImageOnce(resolved);
    return panel;
  }

  function scheduleManagedPanelReconcile(delayMs = 50) {
    clearTimeout(managedReconcileTimer);
    managedReconcileTimer = setTimeout(() => {
      reconcileManagedPanel().catch(() => null);
    }, delayMs);
  }

  function installManagedLifecycleRecovery() {
    if (managedLifecycleInstalled) return;
    managedLifecycleInstalled = true;
    addEventListener("pageshow", () => scheduleManagedPanelReconcile());
    addEventListener("popstate", () => scheduleManagedPanelReconcile());
    for (const method of ["pushState", "replaceState"]) {
      if (!globalThis.history || typeof globalThis.history[method] !== "function") continue;
      const original = globalThis.history[method];
      globalThis.history[method] = function (...args) {
        const result = original.apply(this, args);
        scheduleManagedPanelReconcile();
        return result;
      };
    }
    if (typeof MutationObserver === "function" && document.documentElement) {
      managedPanelObserver = new MutationObserver(() => {
        if (!document.getElementById("ozon-v2-supplier-panel")) {
          scheduleManagedPanelReconcile(100);
        }
      });
      managedPanelObserver.observe(document.documentElement, { childList: true, subtree: true });
    }
  }

  function findUploadInput() {
    return document.querySelector(
      "input[type='file'][accept*='image'], input[type='file'][accept*='jpg'], .image-search-upload input[type='file']"
    );
  }

  function findImageSearchLauncher() {
    const configured = document.querySelector(
      "[data-yandex-role='image-search-launcher'], [aria-label*='以图搜款'], [title*='以图搜款'], [class*='image-search-entry'], [class*='search-by-image']"
    );
    if (configured) return configured;
    const exactText = /^(?:以图搜款|以图搜索|以图搜图|图片搜索|找相似)$/i;
    const candidate = [...document.querySelectorAll("button, a, [role='button'], div, span")]
      .find((node) => exactText.test(textOf(node, 80).replace(/\s+/g, "")));
    return candidate && candidate.closest
      ? candidate.closest("button, a, [role='button']") || candidate
      : candidate;
  }

  async function waitForUploadInput(timeoutMs = REFERENCE_UPLOAD_INPUT_WAIT_MS) {
    const deadline = Date.now() + timeoutMs;
    let input = findUploadInput();
    while (!input && Date.now() < deadline && is1688HomePage()) {
      await sleep(200);
      input = findUploadInput();
    }
    return input;
  }

  async function uploadReferenceImage(binding) {
    if (!is1688HomePage() || !binding.reference_image_url) {
      return { ok: false, reason: "not_home_or_missing_reference" };
    }
    let input = findUploadInput();
    if (!input) {
      const trigger = findImageSearchLauncher();
      if (trigger && typeof trigger.click === "function") trigger.click();
      input = await waitForUploadInput();
    }
    if (!input || typeof DataTransfer === "undefined" || typeof File === "undefined") {
      return { ok: false, reason: "upload_input_missing" };
    }
    const response = await chrome.runtime.sendMessage({
      type: "ozon_v2_fetch_reference_image",
      url: binding.reference_image_url,
    });
    if (!response || !response.ok || !Array.isArray(response.bytes)) {
      return { ok: false, reason: "reference_fetch_failed" };
    }
    const transfer = new DataTransfer();
    transfer.items.add(new File(
      [new Uint8Array(response.bytes)],
      `ozon-${binding.ozon_product_id || binding.seed_id}.jpg`,
      { type: response.contentType || "image/jpeg" },
    ));
    const navigationLease = await chrome.runtime.sendMessage({
      type: "ozon_v2_supplier_navigation_intent",
    }).catch((error) => ({
      ok: false,
      code: "supplier_selection.navigation_message_failed",
      error: error && error.message ? error.message : String(error),
    }));
    if (!navigationLease || navigationLease.ok !== true || navigationLease.granted === false) {
      return {
        ok: false,
        reason: navigationLease && (navigationLease.code || navigationLease.error)
          ? [navigationLease.code, navigationLease.error].filter(Boolean).join(": ")
          : "supplier_selection.navigation_lease_failed",
      };
    }
    input.files = transfer.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true };
  }

  async function prepareReferenceImage(binding) {
    if (!is1688HomePage() || !binding.reference_image_url) return false;
    const status = document.getElementById("ozon-v2-supplier-status");
    const preparedKey = `ozon_v2_reference_prepared_${binding.run_id}_${binding.seed_id}`;
    if (globalThis.sessionStorage && globalThis.sessionStorage.getItem(preparedKey) === "true") {
      if (status) status.textContent = "参考图已输入，请点击 1688 搜索图片 (Image Ready)";
      return true;
    }
    let lastReason = "";
    let attempt = 1;
    while (attempt <= REFERENCE_UPLOAD_ATTEMPTS && is1688HomePage()) {
      if (status) status.textContent = `正在输入参考图 ${attempt}/${REFERENCE_UPLOAD_ATTEMPTS} (Preparing Image)`;
      try {
        const result = await uploadReferenceImage(binding);
        if (result.ok) {
          if (globalThis.sessionStorage) globalThis.sessionStorage.setItem(preparedKey, "true");
          if (status) status.textContent = "参考图已输入，请点击 1688 搜索图片 (Image Ready)";
          return true;
        }
        lastReason = result.reason || "upload_failed";
        if (lastReason === "supplier_selection.navigation_busy") {
          if (status) status.textContent = "正在等待上一通道完成图片搜索 (Waiting for Previous Lane)";
          await sleep(500);
          continue;
        }
      } catch (error) {
        lastReason = error && error.message ? error.message : String(error);
      }
      attempt += 1;
      if (attempt <= REFERENCE_UPLOAD_ATTEMPTS) await sleep(1000);
    }
    if (status && is1688HomePage()) {
      status.textContent = "参考图输入失败，请保持本页并重试批次 (Image Upload Failed)";
    }
    await heartbeat({
      run_id: binding.run_id,
      stage: "supplier_reference_upload_failed",
      code: "browser_bridge.reference_upload_failed",
      message: "The managed 1688 channel could not prepare its assigned reference image.",
      details: { channel_index: binding.channel_index, seed_id: binding.seed_id, reason: lastReason },
    });
    return false;
  }

  async function initializeManagedChannel() {
    const task = await currentTask();
    if (!task || task.code !== "browser_task.supplier_selection_ready") return false;
    let binding = null;
    let bindingDiagnostics = null;
    for (let attempt = 0; attempt < SUPPLIER_CHANNEL_WAIT_ATTEMPTS && !binding; attempt += 1) {
      const state = await currentSupplierChannelState();
      binding = state.binding;
      bindingDiagnostics = state.diagnostics || bindingDiagnostics;
      if (!binding) await sleep(250);
    }
    if (!binding) {
      await heartbeat({
        stage: "supplier_channel_binding_missing",
        code: "browser_bridge.supplier_channel_binding_missing",
        message: "The managed 1688 tab did not receive its channel binding before the wait deadline.",
        details: bindingDiagnostics,
      });
      return true;
    }
    activeManagedBinding = binding;
    installManagedLifecycleRecovery();
    await reconcileManagedPanel(binding);
    installManagedSameTabNavigation();
    await heartbeat({
      run_id: binding.run_id,
      stage: "supplier_selection_waiting_user",
      code: "browser_bridge.supplier_selection_waiting_user",
      message: "Managed 1688 channel is waiting for the user to confirm an exact product.",
      details: { channel_index: binding.channel_index, seed_id: binding.seed_id },
    });
    return true;
  }

  async function waitForProduct(item, runId, timeoutMs = 45000) {
    const deadline = Date.now() + timeoutMs;
    let stableFingerprint = "";
    let stableCount = 0;
    let latest = null;
    let reportedFingerprint = "";
    while (Date.now() < deadline) {
      if (isLoginPage()) return { loginRequired: true };
      latest = collectProduct(item);
      const missing = missingRequired(latest);
      const fingerprint = JSON.stringify({
        title: latest.title,
        seller: latest.seller,
        images: latest.images.length,
        price: latest.price,
        shipping: latest.domestic_shipping_evidence,
      });
      stableCount = fingerprint === stableFingerprint ? stableCount + 1 : 0;
      stableFingerprint = fingerprint;
      if (fingerprint !== reportedFingerprint) {
        reportedFingerprint = fingerprint;
        await api(`/api/batches/${encodeURIComponent(runId)}/supplier-collection-progress`, {
          method: "POST",
          body: JSON.stringify({
            run_id: runId,
            seed_id: String(item.seed_id || ""),
            supplier_url: String(item.supplier_url || ""),
            partial_product: latest,
            missing_fields: missing,
          }),
        });
      }
      if (!missing.length && stableCount >= 2) return { product: latest };
      await heartbeat({
        run_id: runId,
        stage: "waiting_supplier_evidence",
        code: "browser_bridge.supplier_waiting_evidence",
        message: `Waiting for 1688 public fields: ${missing.join(", ") || "page stability"}.`,
        details: { missing_fields: missing },
      });
      await sleep(1000);
    }
    return { product: latest, timedOut: true };
  }

  function stateKey(runId) {
    return `ozon_v2_supplier_state_${runId}`;
  }

  async function loadState(runId) {
    const key = stateKey(runId);
    const stored = await chrome.storage.local.get({ [key]: null });
    return stored[key];
  }

  async function saveState(runId, state) {
    await chrome.storage.local.set({ [stateKey(runId)]: state });
  }

  async function clearState(runId) {
    if (chrome.storage.local.remove) await chrome.storage.local.remove(stateKey(runId));
    else await chrome.storage.local.set({ [stateKey(runId)]: null });
  }

  async function runSupplierTask(task) {
    const runId = task.data.run_id;
    const items = (task.data.contract && task.data.contract.items) || [];
    const state = await loadState(runId) || { itemIndex: 0, supplierProducts: [] };
    const item = items[state.itemIndex];
    if (!item) {
      const result = await api(task.data.ingest_url, {
        method: "POST",
        body: JSON.stringify({
          run_id: runId,
          worker: "workbench_browser_bridge",
          source: "1688_user_verified_link",
          network: { mode: "direct", proxy_disabled: true },
          supplier_products: state.supplierProducts,
        }),
      });
      if (result.ok) await clearState(runId);
      await heartbeat({
        run_id: runId,
        stage: result.ok ? "supplier_submitted" : "supplier_submit_failed",
        code: result.code,
        message: result.message,
      });
      return result;
    }

    if (isLoginPage()) {
      await heartbeat({
        run_id: runId,
        stage: "supplier_login_required",
        code: "browser_bridge.supplier_login_required",
        message: "1688 login is required in this Edge session. Complete login in the current tab; collection will resume automatically.",
      });
      return { ok: false, code: "browser_bridge.supplier_login_required" };
    }

    const targetOfferId = offerId(item.supplier_url);
    if (!is1688DetailPage() || offerId(location.href) !== targetOfferId) {
      await heartbeat({
        run_id: runId,
        stage: "supplier_navigating",
        code: "browser_bridge.supplier_navigating",
        message: `Opening user-verified 1688 product ${state.itemIndex + 1} of ${items.length}.`,
      });
      location.href = item.supplier_url;
      return { ok: true, code: "browser_bridge.supplier_navigating" };
    }

    const evidence = await waitForProduct(item, runId);
    if (evidence.loginRequired) {
      await heartbeat({
        run_id: runId,
        stage: "supplier_login_required",
        code: "browser_bridge.supplier_login_required",
        message: "1688 redirected to login. Complete login in this tab; collection will resume automatically.",
      });
      return { ok: false, code: "browser_bridge.supplier_login_required" };
    }
    const missing = missingRequired(evidence.product || {});
    if (missing.length) {
      await heartbeat({
        run_id: runId,
        stage: "supplier_evidence_incomplete",
        code: "browser_bridge.supplier_evidence_incomplete",
        message: `1688 page did not expose required public fields: ${missing.join(", ")}.`,
      });
      return { ok: false, code: "browser_bridge.supplier_evidence_incomplete", missing_fields: missing };
    }

    state.supplierProducts.push(evidence.product);
    state.itemIndex += 1;
    await saveState(runId, state);
    await heartbeat({
      run_id: runId,
      stage: "supplier_product_collected",
      code: "browser_bridge.supplier_product_collected",
      message: `Collected 1688 product ${state.itemIndex} of ${items.length}.`,
    });
    const next = items[state.itemIndex];
    if (next) {
      location.href = next.supplier_url;
      return { ok: true, code: "browser_bridge.supplier_next_product" };
    }
    return await runSupplierTask(task);
  }

  async function run() {
    const task = await currentTask();
    if (!task || task.code !== "browser_task.supplier_collection_ready") {
      return { ok: true, code: task ? task.code : "browser_task.none" };
    }
    return await runSupplierTask(task);
  }

  function triggerRun() {
    if (activeRunPromise) return activeRunPromise;
    activeRunPromise = run().finally(() => { activeRunPromise = null; });
    return activeRunPromise;
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message) return false;
    if (message.type === "ozon_v2_content_ping") {
      sendResponse({
        ok: true,
        bridge: "supplier",
        binding_present: !!activeManagedBinding,
        panel_present: !!document.getElementById("ozon-v2-supplier-panel"),
      });
      return false;
    }
    if (message.type === "ozon_v2_supplier_channel_refresh") {
      scheduleManagedPanelReconcile(0);
      sendResponse({
        ok: true,
        binding_present: !!activeManagedBinding,
        panel_present: !!document.getElementById("ozon-v2-supplier-panel"),
      });
      return false;
    }
    if (message.type === "ozon_v2_run_task") {
      triggerRun()
        .then((result) => sendResponse({ ok: true, result }))
        .catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    }
    return false;
  });

  initializeManagedChannel()
    .then((managed) => managed ? null : triggerRun())
    .catch(async (error) => {
      await heartbeat({
        stage: "supplier_script_error",
        code: "browser_bridge.supplier_script_error",
        message: error && error.message ? error.message : String(error),
      });
    });
})();
