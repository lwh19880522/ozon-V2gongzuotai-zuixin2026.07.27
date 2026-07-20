"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const elements = new Map();
let clickCapture = null;
let pointerDownCapture = null;
let assignedUrl = "";
const runtimeMessages = [];

function node(text = "") {
  return {
    innerText: text,
    textContent: text,
    style: {},
    dataset: {},
    children: [],
    appendChild(child) {
      this.children.push(child);
      if (child.id) elements.set(child.id, child);
      return child;
    },
    addEventListener(type, handler) { this[`on${type}`] = handler; },
    setAttribute(name, value) { if (name === "data-role") this.dataset.role = String(value); },
    getAttribute() { return null; },
    querySelector(selector) {
      if (selector.startsWith("#")) return elements.get(selector.slice(1)) || null;
      const stack = [...this.children];
      while (stack.length) {
        const item = stack.shift();
        if (selector === "[data-role='channel-title']" && item.dataset && item.dataset.role === "channel-title") return item;
        stack.push(...(item.children || []));
      }
      return null;
    },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
}

const document = {
  title: "1688 image search",
  body: node("1688 image search"),
  documentElement: node(),
  createElement() { return node(); },
  getElementById(id) { return elements.get(id) || null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener(type, listener, capture) {
    if (type === "click" && capture === true) clickCapture = listener;
    if (type === "pointerdown" && capture === true) pointerDownCapture = listener;
  },
};

const binding = {
  run_id: "wb-managed",
  channel_index: 0,
  seed_id: "seed-1",
  ozon_product_id: "ozon-1",
  ozon_title: "Ozon test product",
  reference_image_url: "https://ir.ozone.ru/reference.jpg",
  capture_url: "/api/batches/wb-managed/supplier-selection/capture",
  reject_url: "/api/batches/wb-managed/supplier-review/reject",
};

const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    async sendMessage(message) {
      runtimeMessages.push(message);
      if (message.type === "ozon_v2_get_current_task") {
        return { ok: true, task: { code: "browser_task.supplier_selection_ready" } };
      }
      if (message.type === "ozon_v2_get_supplier_channel") return { ok: true, binding };
      return { ok: true };
    },
    onMessage: { addListener() {} },
  },
  storage: { local: { async get(defaults) { return defaults; }, async set() {}, async remove() {} } },
};

const location = {
  href: "https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch",
  hostname: "air.1688.com",
  assign(url) {
    assignedUrl = String(url);
    this.href = assignedUrl;
  },
};

const context = vm.createContext({
  chrome,
  console,
  document,
  location,
  fetch: async () => ({ json: async () => ({ ok: true }) }),
  addEventListener() {},
  history: { pushState() {}, replaceState() {} },
  innerWidth: 1280,
  innerHeight: 720,
  MutationObserver: class { observe() {} },
  setTimeout,
  clearTimeout,
  URL,
  Date,
  Promise,
  JSON,
});

const scriptPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "supplier_content.js");
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });

(async () => {
  const deadline = Date.now() + 1000;
  while (!clickCapture && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(clickCapture, "a managed 1688 page must capture detail-link clicks for same-tab navigation");
  assert.ok(pointerDownCapture, "a managed 1688 page must mark explicit native new-tab intent");

  const detailUrl = "https://detail.1688.com/offer/123456789012.html";
  function dispatch(listener, {
    href = detailUrl,
    button = 0,
    ctrlKey = false,
    metaKey = false,
    shiftKey = false,
    altKey = false,
  } = {}) {
    const anchor = {
      href,
      getAttribute(name) { return name === "href" ? href : null; },
      closest(selector) { return selector === "a[href]" ? this : null; },
    };
    let prevented = false;
    let stopped = false;
    listener({
      target: { closest() { return anchor; } },
      button,
      ctrlKey,
      metaKey,
      shiftKey,
      altKey,
      defaultPrevented: false,
      preventDefault() { prevented = true; },
      stopImmediatePropagation() { stopped = true; },
    });
    return { prevented, stopped };
  }

  const normal = dispatch(clickCapture);

  assert.equal(assignedUrl, detailUrl, "the exact 1688 detail URL must replace the current managed lane page");
  assert.deepEqual(normal, { prevented: true, stopped: true });

  assignedUrl = "";
  for (const explicit of [
    { ctrlKey: true },
    { metaKey: true },
    { shiftKey: true },
    { altKey: true },
    { button: 1 },
  ]) {
    assert.deepEqual(dispatch(clickCapture, explicit), { prevented: false, stopped: false });
    assert.equal(assignedUrl, "", "explicit new-tab clicks must keep native browser behavior");
    const before = runtimeMessages.length;
    assert.deepEqual(dispatch(pointerDownCapture, explicit), { prevented: false, stopped: false });
    assert.equal(runtimeMessages.length, before + 1);
    assert.deepEqual(runtimeMessages.at(-1), {
      type: "ozon_v2_supplier_native_new_tab_intent",
      url: detailUrl,
    });
  }

  const beforeNonDetail = runtimeMessages.length;
  assert.deepEqual(
    dispatch(clickCapture, { href: "https://www.1688.com/" }),
    { prevented: false, stopped: false },
  );
  dispatch(pointerDownCapture, { href: "https://www.1688.com/", ctrlKey: true });
  assert.equal(runtimeMessages.length, beforeNonDetail, "non-detail links must remain untouched");
  process.stdout.write("supplier same-tab detail navigation: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
