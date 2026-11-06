// Carmen's page drawn from real views in Node, no browser (Nov 6, refactor Phase 4).
// The `doneN` crash (Nov 3 to 6) threw while render() built its HTML, before any DOM work, and
// load() hid it. This runs page.html's script with a permissive stand-in DOM, then calls render()
// for every tab on each fixture view and reports what throws.
//
//   node tests/page_smoke.js <page.html> <views.json>     views.json: {"name": view, ...}
// Exit 1 and one line per failure if any render threw.
"use strict";
const fs = require("fs");
const vm = require("vm");

const [page, viewsFile] = process.argv.slice(2);
const html = fs.readFileSync(page, "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = scripts.sort((a, b) => b.length - a.length)[0];
const views = JSON.parse(fs.readFileSync(viewsFile, "utf8"));

// Anything the page asks of the DOM answers with something usable: a callable proxy that returns
// itself, strings for text, numbers for sizes, empty lists for queries.
// Tree walks end: parents, siblings, children and walkers answer null, so a loop that climbs
// or walks the DOM stops instead of running forever on stand-ins.
const ENDS = new Set(["parentElement", "parentNode", "nextSibling", "previousSibling", "firstChild", "lastChild",
  "nextElementSibling", "previousElementSibling", "firstElementChild", "lastElementChild", "offsetParent",
  "nextNode", "previousNode", "closest", "activeElement", "host"]);
function stub(name) {
  name = String(name).slice(0, 40);
  const fn = function () { return stub(name); };
  return new Proxy(fn, {
    get(t, k) {
      if (k === Symbol.toPrimitive) return h => (h === "number" ? 0 : "");
      if (k === "then") return undefined;   // never a thenable
      if (k === "length") return 0;
      if (["innerHTML", "textContent", "value", "className", "id", "href", "search", "pathname", "hash"].includes(k))
        return t[k] || "";
      if (["querySelectorAll", "getElementsByClassName", "getElementsByTagName"].includes(k)) return () => [];
      if (["offsetTop", "offsetHeight", "offsetWidth", "scrollTop", "scrollHeight", "clientHeight", "innerWidth", "innerHeight", "width", "height", "top", "left"].includes(k))
        return 1000;
      if (k === "dataset" || k === "style") return t[k] || (t[k] = {});
      if (k in t) return t[k];
      if (ENDS.has(k)) return ["closest", "nextNode", "previousNode"].includes(k) ? () => null : null;
      return stub(name + "." + String(k));
    },
    set(t, k, v) { t[k] = v; return true; },
    apply() { return stub(name); },
    construct() { return stub("new " + name); },
  });
}

const store = {};
const ctx = {
  console, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  requestAnimationFrame: () => 0, queueMicrotask() {}, Promise, JSON, Math, Date, RegExp, URL, URLSearchParams,
  encodeURIComponent, decodeURIComponent, Intl, Array, Object, String, Number, Set, Map, Symbol, Error,
  fetch: () => new Promise(() => {}),
  localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
  navigator: stub("navigator"), location: stub("location"), history: stub("history"),
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  MutationObserver: function () { return { observe() {}, disconnect() {} }; },
  getComputedStyle: () => stub("style"),
  addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
  scrollTo() {}, scrollBy() {}, open() {}, alert() {}, confirm: () => false, getSelection: () => stub("selection"),
  scrollY: 0, scrollX: 0, innerWidth: 1200, innerHeight: 800, devicePixelRatio: 2,
  visualViewport: stub("visualViewport"), performance: { now: () => 0 }, screen: stub("screen"),
  CSS: { escape: s => String(s) }, Event: function () {}, CustomEvent: function () {},
  FormData: function () { return stub("formdata"); }, Blob: function () {}, FileReader: function () { return stub("reader"); },
  AbortController: function () { return { signal: {}, abort() {} }; },
  Element: function Element() {}, HTMLElement: function HTMLElement() {}, Node: function Node() {},
  Text: function Text() {}, HTMLInputElement: function () {}, HTMLTextAreaElement: function () {},
  NodeFilter: { SHOW_TEXT: 4, SHOW_ELEMENT: 1, FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3 },
};
ctx.document = stub("document");
ctx.window = ctx;
ctx.globalThis = ctx;
ctx.self = ctx;
vm.createContext(ctx);

const fails = [];
try {
  vm.runInContext(code + `
;globalThis.__smoke = (v, t, test) => { view = v; tab = t; testMode = !!test; lastJson = null; render(); };`, ctx,
    { filename: "page.html" });
} catch (e) {
  fails.push(`loading the script: ${e && e.stack ? e.stack.split("\n").slice(0, 2).join(" | ") : e}`);
}
if (ctx.__smoke) {
  for (const [name, v] of Object.entries(views)) {
    for (const t of ["do", "today", "replies", "claude"]) {
      for (const test of [false, true]) {
        try { ctx.__smoke(JSON.parse(JSON.stringify(v)), t, test); }
        catch (e) { fails.push(`${name} / ${t}${test ? " / test mode" : ""}: ${e && e.stack ? e.stack.split("\n").slice(0, 2).join(" | ") : e}`); }
      }
    }
  }
}
if (fails.length) { console.log(fails.join("\n")); process.exit(1); }
console.log(`rendered ${Object.keys(views).length} views x 4 tabs x 2 modes`);
