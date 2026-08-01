"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const elements = new Map();
const windowListeners = {};
const runtimeMessages = [];
const storedLocal = { supplierPanelPosition: { left: 900, top: -50 } };
const storedSession = new Map();
let runtimeListener = null;
let mutationCallback = null;
let observerTarget = null;
let referenceFetches = 0;
let backResponse = { ok: true, mode: "history" };

function descendants(root) {
  const result = [];
  for (const child of root.children || []) {
    result.push(child, ...descendants(child));
  }
  return result;
}

function node(text = "") {
  const listeners = {};
  const value = {
    _id: "",
    innerText: text,
    textContent: text,
    style: {},
    dataset: {},
    children: [],
    parentNode: null,
    offsetWidth: 300,
    offsetHeight: 180,
    disabled: false,
    listeners,
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      if (child.id) elements.set(child.id, child);
      return child;
    },
    remove() {
      elements.delete(this.id);
      if (this.parentNode) {
        this.parentNode.children = this.parentNode.children.filter((item) => item !== this);
      }
      this.parentNode = null;
    },
    setAttribute(name, fieldValue) {
      if (name === "id") this.id = String(fieldValue);
      else if (name === "data-role") this.dataset.role = String(fieldValue);
      else this[name] = String(fieldValue);
    },
    getAttribute(name) {
      if (name === "id") return this.id;
      if (name === "data-role") return this.dataset.role || null;
      return null;
    },
    querySelector(selector) {
      if (selector.startsWith("#")) {
        const id = selector.slice(1);
        return [this, ...descendants(this)].find((item) => item.id === id) || null;
      }
      if (selector === "[data-role='channel-title']") {
        return [this, ...descendants(this)].find((item) => item.dataset.role === "channel-title") || null;
      }
      return null;
    },
    querySelectorAll(selector) {
      if (selector.startsWith("#")) {
        const found = this.querySelector(selector);
        return found ? [found] : [];
      }
      return [];
    },
    addEventListener(type, listener) { listeners[type] = listener; },
    setPointerCapture() {},
  };
  Object.defineProperty(value, "id", {
    get() { return this._id; },
    set(next) {
      if (this._id) elements.delete(this._id);
      this._id = String(next || "");
      if (this._id) elements.set(this._id, this);
    },
  });
  return value;
}

const documentElement = node();
let body = node("1688 image search");
documentElement.appendChild(body);
const uploadInput = {
  files: [],
  dispatchEvent() {},
};
const documentListeners = {};
const document = {
  title: "1688 image search",
  documentElement,
  get body() { return body; },
  set body(value) { body = value; },
  createElement() { return node(); },
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) {
    if (selector.includes("input[type='file']")) return uploadInput;
    return null;
  },
  querySelectorAll(selector) {
    if (selector.startsWith("#")) {
      const found = elements.get(selector.slice(1));
      return found ? [found] : [];
    }
    return [];
  },
  addEventListener(type, listener) { documentListeners[type] = listener; },
};

function replaceDocumentBody() {
  const oldBody = body;
  for (const item of [oldBody, ...descendants(oldBody)]) {
    if (item.id) elements.delete(item.id);
  }
  body = node("1688 replaced body");
  body.parentNode = documentElement;
  documentElement.children = [body];
}

class FakeMutationObserver {
  constructor(callback) { mutationCallback = callback; }
  observe(target) { observerTarget = target; }
}

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
    this.items = { add: (file) => this.files.push(file) };
  }
}

const binding = {
  run_id: "wb-managed",
  channel_index: 2,
  seed_id: "seed-3",
  ozon_product_id: "ozon-3",
  dispatch_token: "supplier-dispatch-current",
  ozon_title: "Ozon managed product",
  reference_image_url: "https://ir.ozone.ru/reference.jpg",
  capture_url: "/api/batches/wb-managed/supplier-selection/capture",
  reject_url: "/api/batches/wb-managed/supplier-review/reject",
};
let channelBinding = binding;

const chrome = {
  runtime: {
    getManifest() { return { version: "9.8.7" }; },
    async sendMessage(message) {
      runtimeMessages.push(message);
      if (message.type === "ozon_v2_get_current_task") {
        return { ok: true, task: { code: "browser_task.supplier_selection_ready" } };
      }
      if (message.type === "ozon_v2_get_supplier_channel") {
        return { ok: true, binding: channelBinding, diagnostics: null };
      }
      if (message.type === "ozon_v2_fetch_reference_image") {
        referenceFetches += 1;
        return { ok: true, bytes: [1, 2, 3], contentType: "image/jpeg" };
      }
      if (message.type === "ozon_v2_supplier_channel_back") return backResponse;
      return { ok: true };
    },
    onMessage: { addListener(listener) { runtimeListener = listener; } },
  },
  storage: { local: {
    async get(defaults) { return { ...defaults, ...storedLocal }; },
    async set(values) { Object.assign(storedLocal, values); },
    async remove(key) { delete storedLocal[key]; },
  } },
};

const location = {
  href: "https://air.1688.com/kapp/1688-search/pc-image-search/?tab=imageSearch",
  hostname: "air.1688.com",
  assign(url) { this.href = String(url); this.hostname = new URL(this.href).hostname; },
};

const sessionStorage = {
  getItem(key) { return storedSession.has(key) ? storedSession.get(key) : null; },
  setItem(key, value) { storedSession.set(key, String(value)); },
  removeItem(key) { storedSession.delete(key); },
};

function sendRuntimeMessage(message) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`runtime response timeout: ${message.type}`)), 1000);
    const keepOpen = runtimeListener(message, {}, (response) => {
      clearTimeout(timeout);
      resolve(response);
    });
    if (keepOpen === false) clearTimeout(timeout);
  });
}

function wait(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(check, timeoutMs = 1200) {
  const deadline = Date.now() + timeoutMs;
  while (!check() && Date.now() < deadline) await wait(10);
  assert.ok(check(), "timed out waiting for managed supplier lifecycle state");
}

const context = vm.createContext({
  chrome,
  console,
  document,
  location,
  history: { pushState() {}, replaceState() {} },
  sessionStorage,
  fetch: async () => ({ json: async () => ({ ok: true }) }),
  addEventListener(type, listener) { windowListeners[type] = listener; },
  dispatchEvent() {},
  innerWidth: 800,
  innerHeight: 600,
  MutationObserver: FakeMutationObserver,
  DataTransfer: FakeDataTransfer,
  File: FakeFile,
  Event: class { constructor(type) { this.type = type; } },
  CustomEvent: class { constructor(type) { this.type = type; } },
  Uint8Array,
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
  await waitFor(() => document.getElementById("ozon-v2-supplier-panel"));
  assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1);
  assert.equal(typeof windowListeners.popstate, "function", "popstate must reconcile a removed panel");
  assert.equal(typeof windowListeners.pageshow, "function", "pageshow must reconcile BFCache restoration");
  assert.equal(observerTarget, document.documentElement, "the observer must survive whole-body replacement");

  let panel = document.getElementById("ozon-v2-supplier-panel");
  assert.equal(panel.style.left, "500px", "stored horizontal position must be clamped to the viewport");
  assert.equal(panel.style.top, "0px", "stored vertical position must be clamped to the viewport");
  assert.equal(typeof panel.listeners.pointerdown, "function", "the integrated panel must be draggable");

  panel.remove();
  windowListeners.popstate();
  await wait(80);
  assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "popstate must restore one panel");

  document.getElementById("ozon-v2-supplier-panel").remove();
  windowListeners.pageshow({ persisted: true });
  await wait(80);
  assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "pageshow must restore one panel");

  replaceDocumentBody();
  mutationCallback();
  await wait(140);
  assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "whole-body replacement must restore one panel");

  await sendRuntimeMessage({ type: "ozon_v2_supplier_channel_refresh" });
  await wait(20);
  assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "refresh must remain idempotent");

  panel = document.getElementById("ozon-v2-supplier-panel");
  const backButton = document.getElementById("ozon-v2-supplier-back");
  assert.ok(backButton, "the integrated panel must contain a back button");
  const preparedKey = `ozon_v2_reference_prepared_${binding.run_id}_${binding.seed_id}`;
  sessionStorage.setItem(preparedKey, "true");
  await backButton.onclick();
  assert.equal(sessionStorage.getItem(preparedKey), null, "back must clear stale page-local reference state");
  assert.ok(runtimeMessages.some((item) => item.type === "ozon_v2_supplier_channel_back"));

  location.assign("https://www.1688.com/");
  windowListeners.popstate();
  await wait(120);
  assert.equal(referenceFetches, 1, "same-document home return must prepare the assigned reference once");
  await sendRuntimeMessage({ type: "ozon_v2_supplier_channel_refresh" });
  await wait(30);
  assert.equal(referenceFetches, 1, "refresh must not upload the reference twice");

  backResponse = { ok: false, code: "back_failed" };
  await document.getElementById("ozon-v2-supplier-back").onclick();
  assert.equal(document.getElementById("ozon-v2-supplier-status").textContent, "返回失败，请重试");
  assert.equal(document.querySelectorAll("#ozon-v2-supplier-panel").length, 1, "failed back must preserve the panel");

  panel = document.getElementById("ozon-v2-supplier-panel");
  panel.listeners.pointerdown({ clientX: 100, clientY: 100, pointerId: 1, target: panel, preventDefault() {} });
  windowListeners.pointermove({ clientX: 180, clientY: 160, pointerId: 1 });
  windowListeners.pointerup({ pointerId: 1 });
  await wait(10);
  assert.ok(storedLocal.supplierPanelPosition, "dragging must persist the panel position");

  const ping = await sendRuntimeMessage({ type: "ozon_v2_content_ping" });
  assert.equal(ping.binding_present, true);
  assert.equal(ping.panel_present, true);

  channelBinding = null;
  await sendRuntimeMessage({ type: "ozon_v2_supplier_channel_refresh" });
  await wait(40);
  const staleCollectButton = document.getElementById("ozon-v2-collect-current-product");
  assert.equal(staleCollectButton.disabled, true, "an unbound stale tab must not keep a usable capture button");
  const stalePing = await sendRuntimeMessage({ type: "ozon_v2_content_ping" });
  assert.equal(stalePing.binding_present, false, "a removed channel binding must invalidate page-local state");
  process.stdout.write("supplier managed panel lifecycle: OK\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
