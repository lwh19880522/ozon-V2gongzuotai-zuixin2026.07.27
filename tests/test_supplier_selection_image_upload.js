"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const elements = new Map();
let triggerClicks = 0;
let uploadChanges = 0;
let uploadedFile = null;
let inputVisible = false;
let navigationIntents = 0;
let navigationResponse = { ok: true, granted: true };
let navigationReleases = 0;
const requestedImageUrls = [];
const sessionValues = new Map();

function node(text = "") {
  return {
    innerText: text,
    textContent: text,
    style: {},
    dataset: {},
    children: [],
    appendChild(child) { this.children.push(child); if (child.id) elements.set(child.id, child); return child; },
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
  };
}

const input = {
  files: [],
  dispatchEvent(event) {
    if (event.type === "change") uploadChanges += 1;
  },
};
const trigger = {
  innerText: "以图搜款",
  textContent: "以图搜款",
  click() { triggerClicks += 1; inputVisible = true; },
};
const body = node("1688 首页");
const document = {
  title: "1688 首页",
  body,
  documentElement: node(),
  createElement() { return node(); },
  addEventListener() {},
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) {
    if (selector.includes("input[type='file']")) return inputVisible ? input : null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector.includes("button") || selector.includes("role='button'")) return [trigger];
    if (selector === "script" || selector === "img") return [];
    return [];
  },
};

class FakeFile {
  constructor(parts, name, options) {
    this.parts = parts;
    this.name = name;
    this.type = options.type;
  }
}
class FakeDataTransfer {
  constructor() {
    this.files = [];
    this.items = {
      add: (file) => {
        uploadedFile = file;
        this.files.push(file);
      },
    };
  }
}

const binding = {
  run_id: "wb-managed",
  channel_index: 0,
  seed_id: "seed-1",
  ozon_product_id: "ozon-1",
  ozon_title: "Ozon test product",
  reference_image_url: "https://ir.ozone.ru/reference.jpg",
  reference_image_urls: [
    "https://ir.ozone.ru/reference.jpg",
    "https://ir.ozone.ru/fallback.jpg",
  ],
  capture_url: "/api/batches/wb-managed/supplier-selection/capture",
  reject_url: "/api/batches/wb-managed/supplier-review/reject",
};
const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    async sendMessage(message) {
      if (message.type === "ozon_v2_get_current_task") {
        return { ok: true, task: { code: "browser_task.supplier_selection_ready" } };
      }
      if (message.type === "ozon_v2_get_supplier_channel") return { ok: true, binding };
      if (message.type === "ozon_v2_supplier_navigation_intent") {
        navigationIntents += 1;
        return navigationResponse;
      }
      if (message.type === "ozon_v2_supplier_navigation_release") {
        navigationReleases += 1;
        return { ok: true, released: true };
      }
      if (message.type === "ozon_v2_fetch_reference_image") {
        requestedImageUrls.push(message.url);
        return { ok: true, bytes: [1, 2, 3], contentType: "image/jpeg" };
      }
      return { ok: true };
    },
    onMessage: { addListener() {} },
  },
  storage: { local: { async get(defaults) { return defaults; }, async set() {}, async remove() {} } },
};

const context = vm.createContext({
  chrome,
  console,
  document,
  location: { href: "https://www.1688.com/", hostname: "www.1688.com" },
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
  Uint8Array,
  File: FakeFile,
  DataTransfer: FakeDataTransfer,
  Event: class { constructor(type) { this.type = type; } },
  sessionStorage: {
    getItem(key) { return sessionValues.has(key) ? sessionValues.get(key) : null; },
    setItem(key, value) { sessionValues.set(key, String(value)); },
    removeItem(key) { sessionValues.delete(key); },
  },
});

const scriptPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "supplier_content.js");
const source = fs.readFileSync(scriptPath, "utf8").replace(
  "  initializeManagedChannel()",
  "  globalThis.__testUploadReferenceImage = uploadReferenceImage;\n  globalThis.__testHandleReferenceImageRejection = handleReferenceImageRejection;\n  initializeManagedChannel()",
);
vm.runInContext(source, context, { filename: scriptPath });

setTimeout(async () => {
  assert.equal(triggerClicks, 1, "the visible 1688 image-search control must be activated");
  assert.equal(uploadChanges, 1, "the Ozon reference image must be submitted to the 1688 file input");
  assert.equal(navigationIntents, 0, "all managed lanes must be able to preload their reference image without taking the navigation lease");
  assert.ok(uploadedFile, "a browser File must be created for the reference image");
  assert.equal(uploadedFile.name, "ozon-ozon-1.jpg");
  navigationResponse = { ok: false, granted: false, code: "supplier_selection.navigation_busy" };
  uploadChanges = 0;
  const blocked = await context.__testUploadReferenceImage(binding);
  assert.equal(blocked.ok, true);
  assert.equal(uploadChanges, 1, "a busy search-navigation lease must not prevent another lane from preloading its image");
  assert.equal(navigationIntents, 0, "preloading a reference image must never acquire a search-navigation lease");
  uploadChanges = 0;
  await context.__testHandleReferenceImageRejection(binding);
  assert.equal(navigationReleases, 1, "1688 recognition rejection must release the search-navigation lease immediately");
  assert.equal(uploadChanges, 1, "the next distinct Ozon SKU image must be preloaded after recognition rejection");
  assert.equal(requestedImageUrls.at(-1), "https://ir.ozone.ru/fallback.jpg");
  process.stdout.write("supplier managed reference upload: OK\n");
}, 700);
