/* Browser regression tests without external DOM dependencies. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class FakeElement {
  constructor(tag = "div", id = "") {
    this.tag = tag;
    this.id = id;
    this._value = "";
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.style = {};
    this.classList = {
      add() {},
      remove() {},
      toggle() {}
    };
    this.textContent = "";
    this.disabled = false;
    this.files = [];
    this.options = this.children;
  }
  get value() { return this._value; }
  set value(v) { this._value = String(v ?? ""); }
  get childNodes() { return this.children; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { for (const child of children) this.appendChild(child); }
  replaceChildren(...children) {
    this.children = [...children];
    this.options = this.children;
  }
  addEventListener(event, cb) {
    if (!this.listeners.has(event)) this.listeners.set(event, []);
    this.listeners.get(event).push(cb);
  }
  dispatchEvent(event) {
    for (const callback of this.listeners.get(event.type) || []) callback(event);
  }
  setAttribute() {}
  querySelector() { return new FakeElement("span"); }
  focus() {}
  get innerHTML() { return this._html || ""; }
  set innerHTML(text) { this._html = text; this.children = []; }
}
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, new FakeElement("div", id));
  return elements.get(id);
}
const tabs = ["chat", "video"].map(mode => {
  const element = new FakeElement("button");
  element.dataset.mode = mode;
  return element;
});
const windowHandlers = new Map();
const storage = new Map();
storage.set("muse-ai-web-chat-v1", JSON.stringify({
  activeId: "a1",
  threads: [
    { id: "a1", accountId: "A", sessionId: "s1", name: "A chat 1", messages: [] },
    { id: "b1", accountId: "B", sessionId: "s2", name: "B chat 1", messages: [] },
    { id: "a2", accountId: "A", sessionId: "s3", name: "A chat 2", messages: [] },
    { id: "old", sessionId: "primary", name: "Legacy unassigned", messages: [] }
  ]
}));
const windowObject = {
  addEventListener(name, cb) {
    if (!windowHandlers.has(name)) windowHandlers.set(name, []);
    windowHandlers.get(name).push(cb);
  },
  dispatchEvent(event) {
    for (const cb of windowHandlers.get(event.type) || []) cb(event);
  }
};
const context = vm.createContext({
  document: {
    getElementById: el,
    createElement: tag => new FakeElement(tag),
    querySelectorAll: selector => selector === ".mode-tab" ? tabs : []
  },
  window: windowObject,
  localStorage: {
    getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value)
  },
  CustomEvent: class {
    constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
  },
  Event: class { constructor(type) { this.type = type; } },
  crypto: { randomUUID: () => "generated-id" },
  fetch: async url => ({
    ok: true,
    json: async () => url === "/api/auth/status"
      ? { authenticated: false, ready_accounts: 0 }
      : { jobs: [] }
  }),
  setInterval: () => 1,
  clearInterval() {},
  setTimeout: () => 1,
  console
});
el("taskCount").value = "1";
for (const script of ["app.js", "chat.js"]) {
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, "../muse_ai/web/static", script), "utf8"),
    context,
    { filename: script }
  );
}
const accounts = [
  { id: "A", label: "Account A", enabled: true, status: "ready", busy: false },
  { id: "B", label: "Account B", enabled: true, status: "ready", busy: false },
  { id: "C", label: "Account C", enabled: true, status: "ready", busy: false }
];
(async function() {
  windowObject.dispatchEvent(new context.CustomEvent("muse-accounts-updated", { detail: accounts }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(el("generate").disabled, false, "3 free accounts must enable generation");
  assert.equal(el("chatAccount").value, "A");
  assert.deepEqual(
    el("chatThreads").children.map(option => option.textContent),
    ["A chat 1", "A chat 2"],
    "Only conversations for selected A should be listed"
  );
  el("chatAccount").value = "B";
  el("chatAccount").dispatchEvent({ type: "change" });
  assert.deepEqual(
    el("chatThreads").children.map(option => option.textContent),
    ["B chat 1"],
    "Switching to B must hide all A and legacy conversations"
  );
  windowObject.dispatchEvent(new context.CustomEvent("muse-accounts-updated", { detail: accounts }));
  assert.equal(el("chatAccount").value, "B", "pool refresh must preserve explicit account");
  assert.deepEqual(el("chatThreads").children.map(option => option.textContent), ["B chat 1"]);
  el("taskCount").value = "4";
  el("taskCount").dispatchEvent({ type: "input" });
  assert.equal(el("generate").disabled, true, "N above idle accounts must be disabled");
  el("taskCount").value = "2";
  el("taskCount").dispatchEvent({ type: "input" });
  assert.equal(el("generate").disabled, false);
  const busyPool = accounts.map(a => ({ ...a, busy: a.id !== "A" }));
  windowObject.dispatchEvent(new context.CustomEvent("muse-accounts-updated", { detail: busyPool }));
  assert.equal(el("generate").disabled, true, "busy accounts must not be counted");
  console.log("Frontend regression: pool availability, account-filtered chat, refresh, task limits passed");
})().catch(err => { console.error(err); process.exitCode = 1; });
