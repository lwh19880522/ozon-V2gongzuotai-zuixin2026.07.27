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

function node(text = "") {
  return {
    innerText: text,
    textContent: text,
    style: {},
    children: [],
    appendChild(child) { this.children.push(child); if (child.id) elements.set(child.id, child); return child; },
    getAttribute() { return null; },
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
      if (message.type === "ozon_v2_fetch_reference_image") {
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
});

const scriptPath = path.join(__dirname, "..", "browser_extension", "ozon_v2_bridge", "supplier_content.js");
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });

setTimeout(() => {
  assert.equal(triggerClicks, 1, "the visible 1688 image-search control must be activated");
  assert.equal(uploadChanges, 1, "the Ozon reference image must be submitted to the 1688 file input");
  assert.ok(uploadedFile, "a browser File must be created for the reference image");
  assert.equal(uploadedFile.name, "ozon-ozon-1.jpg");
  process.stdout.write("supplier managed reference upload: OK\n");
}, 700);
