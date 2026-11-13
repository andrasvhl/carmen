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
;globalThis.__smoke = (v, t, test) => { view = v; tab = t; testMode = !!test; lastJson = null; toolsOpen = !!test; render(); };`, ctx,
    { filename: "page.html" });
} catch (e) {
  fails.push(`loading the script: ${e && e.stack ? e.stack.split("\n").slice(0, 2).join(" | ") : e}`);
}
if (ctx.__smoke) {
  for (const [name, v] of Object.entries(views)) {
    if (name.startsWith("__")) continue;   // not a view: a parity table (below)
    for (const t of ["do", "today", "replies", "claude", "train"]) {   // Train under Tools (Nov 8); test mode also draws the open Tools menu
      for (const test of [false, true]) {
        try { ctx.__smoke(JSON.parse(JSON.stringify(v)), t, test); }
        catch (e) { fails.push(`${name} / ${t}${test ? " / test mode" : ""}: ${e && e.stack ? e.stack.split("\n").slice(0, 2).join(" | ") : e}`); }
      }
    }
  }
}
// The keyboard rule (Nov 9): the sizing functions run without throwing, the keyboard is read from
// the visual viewport (never from focus alone), and an Edit draft round-trips.
if (ctx.__smoke) {
  try {
    vm.runInContext(`
      if (kbdUp()) throw new Error("kbdUp true with the viewport at full height");
      visualViewport = {height: 400, offsetTop: 0, addEventListener() {}};
      if (!kbdUp()) throw new Error("kbdUp false with the viewport shrunk by a keyboard");
      syncKbd(true); syncKbd(false); fitLayer(); fitGeneral(); fitChat(true);
      const f = {}; const pop = {querySelector: k => k.startsWith(".f-") ? (f[k] = f[k] || {value: "x" + k}) : null, querySelectorAll: () => []};
      const v = editValues(pop); f[".f-note"].value = ""; editRestore(pop, v);
      if (f[".f-note"].value !== "x.f-note") throw new Error("editRestore did not put the draft back");
    `, ctx);
  } catch (e) { fails.push(`keyboard rule: ${e && e.message ? e.message : e}`); }
}
// The screen never moves under a finger (Nov 9): a data redraw waits while a finger is down, for
// 1.5 s after a touch, and while scrolling; scrollend ends the scroll; then it draws, once.
if (ctx.__smoke) {
  try {
    vm.runInContext(`
      const realRender = render; let drew = 0; render = () => { drew++; };
      try {
        touchOn(); drawSoon(); if (drew) throw new Error("drew with a finger down");
        touchOff(); drawSoon(); if (drew) throw new Error("drew within 1.5 s of a touch");
        touchAt = Date.now() - 2000; scrolling = true; scrollAt = Date.now() - 2000; drawSoon();
        if (drew) throw new Error("drew while scrolling");
        scrollDone(); drawSoon(); if (drew !== 1) throw new Error("did not draw once the hold ended");
        if (FLIP_MS !== 10000) throw new Error("Undo is not up for 10 s");
      } finally { render = realRender; }
    `, ctx);
  } catch (e) { fails.push(`hold: ${e && e.message ? e.message : e}`); }
}
// Chat state survives navigation (Nov 9): back on the Chat tab, or on a past chat, the thread is
// fetched again; a redraw while he stays on the tab does not fetch it again.
if (ctx.__smoke) {
  try {
    vm.runInContext(`
      const realPoll = pollChat, realTab = tab; let polls = 0; pollChat = () => { polls++; };
      try {
        tab = "chat"; gui.chat = null; chat = null; gui.away = false;
        mountGeneral(); if (polls !== 1) throw new Error("the first visit did not fetch the thread");
        mountGeneral(); if (polls !== 1) throw new Error("a redraw on the tab fetched the thread again");
        gui.away = true; mountGeneral(); if (polls !== 2) throw new Error("coming back to Chat did not fetch the thread");
        gSwitch("g-smoke"); if (polls !== 3 || chat.id !== "g-smoke") throw new Error("a past chat opened without fetching it");
      } finally { pollChat = realPoll; tab = realTab; chat = null; }
    `, ctx);
  } catch (e) { fails.push(`chat state: ${e && e.message ? e.message : e}`); }
}
const hasViews = Object.keys(views).some(k => !k.startsWith("__"));   // a parity-only run draws nothing
// The computed step order (flag step_order, Nov 11): Plan follows the server's order, a gated step is
// not free, the piece is the server's, optional steps are greyed under "If there's time" and never
// counted in the part's minutes; with the flag off the note's order stands.
if (ctx.__smoke && hasViews) {
  try {
    vm.runInContext(`
      const vsave = view;
      try {
        const r = {id: "so-t", title: "Trip prep", est: "20m", next: "Pick the hotel once she sends them (20 min)",
          detail: {next_id: "a", blocked: ["a"], steps: ["Get the blazer (optional) (2.1 h)", "Send the ticket to Holiday Craft (10 min)",
            "Fill in the e-Arrival card from Nov 17 (45 min)", "Save the doctor (optional) (10 min)"], step_ids: ["b", "c", "d", "e"],
            order: {plan: ["c", "e", "b"], piece: ["c", "e"], optional: ["e", "b"], held: {d: "from Nov 17"}, why: {c: "starts Holiday Craft's clock"}}}};
        view = Object.assign({}, vsave || {}, {flags: {step_order: true}});
        if (taskStepIds(r).join() !== "c,e,b") throw new Error("Plan did not follow the computed order: " + taskStepIds(r).join());
        if (suggestChunk(r) !== 2) throw new Error("the piece is not the server's: " + suggestChunk(r));
        if (chunkMin(r, "today") !== 10) throw new Error("optional minutes were counted: " + chunkMin(r, "today"));
        const h = planCard(r, "today");
        if (!/If there's time/.test(h) || !/pl-opt/.test(h)) throw new Error("no greyed If there's time on the Plan card");
        if (/\(optional\)/.test(h.replace(/title="[^"]*"/g, ""))) throw new Error("the card shows the raw (optional) word");
        const r2 = {id: "so-m1", title: "M1", est: "6h", next: "Do the the e-bike classroom (360 min)",
          detail: {next_id: "x", steps: ["Remind the group (10 min)"], step_ids: ["y"], order: {plan: ["x", "y"], piece: ["x", "y"], optional: [], held: {}, why: {}, parts: {x: 60}}}};
        if (chunkMin(r2, "today") !== 70) throw new Error("a long step's part was not counted as its part: " + chunkMin(r2, "today"));
        if (!/60 min of it/.test(planCard(r2, "today"))) throw new Error("the Plan card does not say it is a part");
        view = Object.assign({}, vsave || {}, {flags: {}});
        if (taskStepIds(r).join() !== "b,c,d,e") throw new Error("flag off: not the note's order: " + taskStepIds(r).join());
      } finally { view = vsave; }
    `, ctx);
  } catch (e) { fails.push(`step order: ${e && e.message ? e.message : e}`); }
}
// Do matches what Plan booked (Nov 11, M1): a step added after the plan is said on the card, each
// step of a part booked in several blocks shows its time, and the card's date is today's own step's.
if (ctx.__smoke && hasViews) {
  try {
    vm.runInContext(`
      const r = {id: "dm", title: "M1", next: "Order the gear (30 min) (due Nov 11)", blk: {s: 780, f: 825},
        chunk: {ids: ["a", "b", "c"], steps: [], added_after: ["Order the gear (30 min)"],
          plan: [{id: "a", text: "Order the gear (30 min) (due Nov 11)", state: "done", blk: {s: 780, f: 825}},
                 {id: "c", text: "the e-bike part 1 (2 h) (due Nov 13)", state: "open", blk: {s: 915, f: 975}}]}};
      const h = doItem(r);
      if (!/Added after your plan: Order the gear/.test(h)) throw new Error("no Added after your plan line");
      if (!/pc-at">15:15</.test(h) || !/pc-at">13:00</.test(h)) throw new Error("steps do not show their booked times");
      if (!/Due [A-Z][a-z]{2} Nov 13/.test(h)) throw new Error("the card's date is not today's open step's");
    `, ctx);
  } catch (e) { fails.push(`do matches plan: ${e && e.message ? e.message : e}`); }
}
// Step text as the card shows it (Nov 11, the M1 card): an estimate never twice, in minutes or
// hours, and his "(the owner" tag never shown, wherever it sits on the line.
if (ctx.__smoke) {
  try {
    vm.runInContext(`
      const shown = t => stepParts(t).map(p => p.main + " | " + p.detail).join(" // ");
      const cases = [
        ["the e-bike online classroom, part 2 of 3: next 2 h (due Nov 13) (2 h)", /\\(2 h\\)/],
        ["Get gear (due Nov 12) (2.1 h)", /2\\.1 h/],
        ["Send the ticket (due Nov 12) (10 min)", /10 min/],
        ["Order the gear: boots, gloves (owner decision, Nov 11) (due Nov 11) (30 min)", /the owner/],
      ];
      for (const [t, bad] of cases) if (bad.test(shown(t))) throw new Error(JSON.stringify(t) + " shows " + JSON.stringify(shown(t)));
      if (/the owner/.test(clean("Book it (owner decision, Nov 1)"))) throw new Error("tag at the end shown");
      if (!/San Mateo deadline/.test(shown("Do it (San Mateo deadline) (due Nov 13) (2 h)"))) throw new Error("other brackets dropped");
      if (!/^the e-bike/.test(stepParts("the e-bike part 1 (due Nov 13) (2 h)")[0].main)) throw new Error("the e-bike was recased");
      if (!NOMIN.test("x (1.5 h)") || !NOMIN.test("x (timebox 20 min)")) throw new Error("NOMIN misses a form");
    `, ctx);
  } catch (e) { fails.push(`step text: ${e && e.message ? e.message : e}`); }
}
// Start on a task that is his to write (Nov 11, ECON): the panel opens at once, Carmen's question
// with two quick replies is the chat's first entry, nothing is sent or logged until he chooses.
if (ctx.__smoke) {
  try {
    vm.runInContext(`
      const vsave16 = view, oc16 = openChat, ap16 = api; let opened = null, calls = [];
      try {
        const r = {id: "refl", title: "Write the ECON reflection paper", next: "Draft the reflection (60 min)", detail: {next_id: "s1"}};
        view = {do: {rows: [r]}, flags: {actuals: true}, chats: {}, today: {}, tomorrow: {}};
        openChat = c => { opened = c; }; api = (u, b) => { calls.push(b); return Promise.resolve({}); };
        if (!isReflective(r)) throw new Error("the ECON reflection is not recognised as his to write");
        openTaskPanel("refl", "ask");
        if (!opened || !opened.ask || opened.send) throw new Error("Start did not open the panel with the question: " + JSON.stringify(opened));
        if (calls.length) throw new Error("step_start was logged before he chose");
        const h = startAskHtml();
        if (!/data-askmode="draft"/.test(h) || !/data-askmode="prompts"/.test(h)) throw new Error("the two quick replies are missing");
        if (typeof askFirstChoice !== "undefined") throw new Error("the inline menu on the Do card still exists");
      } finally { view = vsave16; openChat = oc16; api = ap16; }
    `, ctx);
  } catch (e) { fails.push(`start ask: ${e && e.message ? e.message : e}`); }
}
// Parity with Python (audit A2, A3): views.json's "__parity" is [[page expression of x, input, Python's
// output], ...]; the page's own function must give Python's answer.
if (ctx.__smoke && views.__parity) {
  for (const [expr, x, want] of views.__parity) {
    try {
      ctx.__x = x;
      const got = vm.runInContext(`(x => ${expr})(globalThis.__x)`, ctx);
      if (JSON.stringify(got) !== JSON.stringify(want)) fails.push(`parity ${expr} on ${JSON.stringify(x)}: page ${JSON.stringify(got)}, python ${JSON.stringify(want)}`);
    } catch (e) { fails.push(`parity ${expr}: ${e && e.message ? e.message : e}`); }
  }
}
if (fails.length) { console.log(fails.join("\n")); process.exit(1); }
console.log(`rendered ${Object.keys(views).length} views x 5 tabs x 2 modes`);
