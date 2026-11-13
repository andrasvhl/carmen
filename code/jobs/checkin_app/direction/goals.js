// Carmen's Goals tab (owner decision, Nov 5). Loaded after page.html's script and uses its globals
// (api, head, banner, render, toast, tab, testMode). Data: /api/goals (jobs/checkin_app/goals.py).
// The goals live in the vault note Areas/Goals/Current goals.md; every edit here writes it and
// adds one line to its change log. Doc: _system/checkin-app.md § Goals tab.
(function () {
  const css = `
  .g-goals { display: grid; gap: 8px; margin: 4px 0 14px; }
  .g-goal { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 10px 6px; display: flex;
    flex-direction: column; align-items: center; gap: 5px; color: var(--ink); text-align: center; min-width: 0; }
  .g-goal.on { border-color: var(--ink); box-shadow: 0 0 0 1px var(--ink) inset; }
  .g-goal .n { font-size: 12px; font-weight: 600; line-height: 1.2; }
  .g-goal .d { font-size: 10.5px; color: var(--muted); }
  .g-hero { display: grid; grid-template-columns: 124px minmax(0,1fr); gap: 14px; align-items: center; }
  .g-why { font-size: 13.5px; color: var(--muted); border-left: 2px solid var(--line); padding-left: 10px; margin: 12px 0 2px; }
  .g-row { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 3px 10px; padding: 11px 0; border-top: 1px solid var(--line); }
  .g-row:first-child { border-top: 0; }
  .g-row .nm { font-weight: 600; } .g-row .vl { font-weight: 600; font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
  .g-row .dt { grid-column: 1 / -1; font-size: 12.5px; color: var(--muted); }
  .g-src { font-size: 10.5px; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); opacity: .75; font-weight: 600; margin-left: 6px; }
  .g-pill { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; font-weight: 600; padding: 2px 8px; border-radius: 99px; margin-left: 8px; vertical-align: 1px; }
  .g-pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .g-good { color: #1F9D63; background: #E5F5EC; } .g-warn { color: #A86F00; background: #FBF1DC; }
  .g-bad { color: #C8392E; background: #FBE6E4; } .g-later { color: var(--muted); background: var(--soft); }
  @media (prefers-color-scheme: dark) { .g-good { color: #3CC487; background: #16301F; } .g-warn { color: #E9AE3A; background: #352812; }
    .g-bad { color: #F06A5E; background: #3A1C19; } }
  .g-move { background: var(--surface); border-radius: 16px; padding: 13px 14px; display: flex; flex-direction: column; gap: 7px; margin-bottom: 8px; }
  .g-move .gp { font-size: 12.5px; color: var(--muted); } .g-move .wt { font-weight: 600; font-size: 15px; }
  .g-acts { display: flex; gap: 8px; flex-wrap: wrap; }
  .g-btn { height: 34px; padding: 0 14px; border-radius: 99px; font-weight: 600; font-size: 13.5px; background: var(--ink); color: var(--surface); }
  .g-btn.ghost { background: transparent; color: var(--muted); border: 1px solid var(--line); }
  .g-unm { font-size: 12.5px; color: var(--muted); display: flex; flex-direction: column; gap: 3px; }
  .g-sheet { position: fixed; inset: 0; z-index: 55; background: var(--ground); overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: calc(14px + env(safe-area-inset-top)) 16px calc(40px + env(safe-area-inset-bottom)); }
  .g-sheet .in { max-width: 560px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }
  .g-card { background: var(--surface); border-radius: 16px; padding: 12px 14px; display: flex; flex-direction: column; gap: 9px; }
  .g-card.paused { opacity: .6; }
  .g-card input, .g-card select { width: 100%; min-width: 0; border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px;
    background: var(--surface); color: var(--ink); font: inherit; }
  .g-t { display: grid; grid-template-columns: minmax(0,1.3fr) 82px minmax(0,1.2fr) 30px; gap: 6px; align-items: center; }
  .g-x { background: none; color: var(--muted); font-size: 18px; height: 30px; }
  .g-lbl { font-size: 11.5px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .g-log div { font-size: 13px; color: var(--muted); padding: 7px 0; border-top: 1px solid var(--line); }
  .g-log div:first-child { border-top: 0; }
  .g-help { font-size: 12.5px; color: var(--muted); }
  `;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  const X = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  let gv = null, gweek = "next", gsel = null, gload = false, gerr = "", sheet = false;
  const LBL = {good: "On track", warn: "Close", bad: "Gap", later: "Later"};
  const COL = {good: "#1F9D63", warn: "#C98A0B", bad: "#D2473C"};

  async function fetchGoals() {
    if (gload) return; gload = true;
    try { gv = await api("api/goals?week=" + gweek); gerr = ""; }
    catch (e) { gerr = e.message; }
    gload = false;
    if (tab === "goals") render();
    if (sheet) drawSheet();
  }
  async function post(body, msg) {
    try {
      const j = await api("api/goals", {...body, week: gweek});
      if (j && j.goals) gv = j;
      if (msg) toast((testMode ? "Test mode, nothing updated: " : "") + msg);
      if (tab === "goals") render();
      if (sheet) drawSheet();
      return j;
    } catch (e) { toast("Failed: " + e.message); if (sheet) drawSheet(); return null; }
  }

  function ring(on, of, size) {
    const r = size / 2 - (size > 80 ? 9 : 5), c = 2 * Math.PI * r, f = of ? on / of : 0;
    const col = !of ? "var(--line)" : f >= .67 ? COL.good : f >= .34 ? COL.warn : COL.bad, w = size > 80 ? 11 : 6;
    const txt = size > 80
      ? `<text x="50%" y="50%" text-anchor="middle" dominant-baseline="central" style="fill:var(--ink);font-size:32px;font-weight:700">${on}<tspan style="fill:var(--muted);font-size:19px;font-weight:600">/${of}</tspan></text>`
      : `<text x="50%" y="50%" text-anchor="middle" dominant-baseline="central" style="fill:var(--ink);font-size:12px;font-weight:700">${on}/${of}</text>`;
    return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" role="img" aria-label="${on} of ${of} on track">
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--line)" stroke-width="${w}"/>
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${col}" stroke-width="${w}" stroke-linecap="round"
        stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - f)}" transform="rotate(-90 ${size / 2} ${size / 2})"/>${txt}</svg>`;
  }
  function spark(trend) {
    const pts = trend.filter(t => t.pct !== null);
    if (pts.length < 2) return `<div class="g-help">Trend starts after two weeks of data.</div>`;
    const W = 220, H = 58, p = 6, n = trend.length, x = i => p + i * (W - 2 * p) / (n - 1), y = v => H - 16 - (v / 100) * (H - 26);
    let d = "", first = true;
    trend.forEach((t, i) => { if (t.pct === null) return; d += `${first ? "M" : "L"}${x(i).toFixed(1)},${y(t.pct).toFixed(1)} `; first = false; });
    const li = trend.map(t => t.pct !== null).lastIndexOf(true);
    return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Targets on track per week">
      <line x1="${p}" x2="${W - p}" y1="${H - 16}" y2="${H - 16}" stroke="var(--line)"/>
      <path d="${d}" fill="none" stroke="var(--ink)" stroke-width="2" stroke-linejoin="round"/>
      <circle cx="${x(li)}" cy="${y(trend[li].pct)}" r="4" fill="var(--ink)"/>
      ${trend.map((t, i) => `<text x="${x(i)}" y="${H - 2}" font-size="10" fill="var(--muted)" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${X(t.wk)}</text>`).join("")}</svg>`;
  }

  window.goalsHtml = function () {
    const edit = `<button class="pill" data-g="edit">Edit goals</button>`;
    if (!gv) { fetchGoals(); return head(gerr ? "Could not load" : "Loading…", "Goals", edit) + (gerr ? `<div class="empty">${X(gerr)}</div>` : ""); }
    const act = gv.goals.filter(g => g.active);
    if (!act.length) return head("No active goals", "Goals", edit) + `<div class="empty">Add a goal with Edit goals.</div>`;
    if (!gsel || !act.some(g => g.name === gsel)) gsel = act.some(g => g.name === gv.direction) ? gv.direction : act[0].name;
    const g = act.find(x => x.name === gsel);
    let h = head(`${gweek === "next" ? "Next week" : "This week"} · ${X(gv.label)}${gv.calendar_ok ? "" : " · calendar unreadable"}`, "Goals", edit);
    h += `<div class="seg" role="tablist"><button data-g="week" data-w="next" class="${gweek === "next" ? "on" : ""}">Next week</button>
      <button data-g="week" data-w="this" class="${gweek === "this" ? "on" : ""}">This week</button></div>`;
    h += `<div class="g-goals" style="grid-template-columns:repeat(${Math.min(act.length, 4)},minmax(0,1fr))">${act.map(x =>
      `<button class="g-goal${x.name === gsel ? " on" : ""}" data-g="sel" data-n="${X(x.name)}">${ring(x.on, x.of, 50)}
        <span class="n">${X(x.name)}</span><span class="d">${x.name === gv.direction ? "Direction" : "&nbsp;"}</span></button>`).join("")}</div>`;
    h += `<div class="card"><div class="g-hero">${ring(g.on, g.of, 124)}<div style="display:flex;flex-direction:column;gap:5px;min-width:0">
      <div style="font-weight:600">targets on track ${gweek === "next" ? "for next week, as booked" : "this week"}</div>
      <div class="g-help">Share of targets on track, past 4 weeks and this one</div>${spark(g.trend)}</div></div>
      ${g.why ? `<div class="g-why">${X(g.why)}</div>` : ""}
      ${g.name !== gv.direction ? `<div style="margin-top:10px"><button class="g-btn ghost" data-g="dir" data-n="${X(g.name)}">Make this my direction</button></div>` : ""}</div>`;
    h += `<h2>What adds up to it</h2><div class="card">${g.rows.length ? g.rows.map(r => `<div class="g-row">
      <div class="nm">${X(r.name)}<span class="g-pill g-${r.status}">${LBL[r.status] || ""}</span></div><div class="vl">${X(r.text)}</div>
      <div class="dt">${X(r.detail.slice(0, 6).join(" · "))}<span class="g-src">${X(r.src)}</span></div></div>`).join("")
      : `<div class="g-help">No target is counted yet. Edit goals to add one.</div>`}</div>`;
    if (g.name === gv.direction) {
      h += `<h2>Moves <span>for your direction, at most three</span></h2>`;
      h += g.moves.length ? g.moves.map(m => `<div class="g-move"><div class="gp">${X(m.gap)}</div><div class="wt">${X(m.what)}</div>
        <div class="g-acts">${m.task ? `<button class="g-btn" data-g="task" data-t="${X(m.what)}" data-d="${X(m.date)}">Add as task</button>` : ""}
        <button class="g-btn ghost" data-g="dismiss" data-s="${X(m.sig)}">Not this week</button></div></div>`).join("")
        : `<div class="card g-help">Nothing to suggest: every counted target is on track or has no free slot left.</div>`;
    } else {
      h += `<div class="g-help" style="margin:14px 4px">Moves show for your direction (${X(gv.direction || "none set")}). Make this one your direction to see its moves.</div>`;
    }
    if (g.unmeasured.length) h += `<h2>Not measured yet</h2><div class="card g-unm">${g.unmeasured.map(u => `<span>○ ${X(u)}</span>`).join("")}</div>`;
    return h;
  };

  // --- the edit sheet: outside #app, so the page's redraws never wipe what he is typing
  const metricOpts = () => Object.keys(gv.metrics);
  function byInput(gi, ti, val) {
    return `<input data-ge="by" data-gi="${gi}" data-ti="${ti}" value="${X(val)}" list="g-metrics" aria-label="Counted by">`;
  }
  function drawSheet() {
    let el = document.getElementById("g-sheet");
    if (!sheet) { if (el) el.remove(); return; }
    if (!el) { el = document.createElement("div"); el.id = "g-sheet"; el.className = "g-sheet"; document.body.appendChild(el); }
    if (!gv) { el.innerHTML = `<div class="in">Loading…</div>`; return; }
    const keepScroll = el.scrollTop, n = gv.goals.filter(g => g.active).length;
    // Keep the field he moved to (a save redraws the sheet as he tabs on).
    const fa = document.activeElement && el.contains(document.activeElement) && document.activeElement.dataset.ge
      ? `[data-ge="${document.activeElement.dataset.ge}"][data-gi="${document.activeElement.dataset.gi || ""}"]${document.activeElement.dataset.ti !== undefined ? `[data-ti="${document.activeElement.dataset.ti}"]` : ""}` : "";
    el.innerHTML = `<div class="in">
      <div style="display:flex;justify-content:space-between;align-items:center"><h1 style="margin:0">Your goals</h1><button class="g-btn" data-g="close">Done</button></div>
      <div class="g-help">Every change is written to <a href="${X(gv.note)}">Current goals</a> with a dated line in its change log. Changes save when you leave a field.${testMode ? " <b>Test mode: nothing is saved.</b>" : ""}</div>
      ${n > 3 ? `<div class="g-move"><div class="gp">${n} active goals. Your 2024 note: "Too many goals make progress tracking impossible." Pause one, or keep all ${n}.</div></div>` : ""}
      <datalist id="g-metrics">${metricOpts().map(k => `<option value="${X(k)}">`).join("")}</datalist>
      ${gv.goals.map((g, gi) => `<div class="g-card${g.active ? "" : " paused"}">
        <div style="display:flex;gap:8px"><input data-ge="name" data-gi="${gi}" value="${X(g.name)}" aria-label="Goal name" style="font-weight:600">
          <button class="g-btn ghost" data-g="pause" data-gi="${gi}">${g.active ? "Pause" : "Resume"}</button></div>
        <input data-ge="why" data-gi="${gi}" value="${X(g.why)}" placeholder="Why it matters, one line" aria-label="Why">
        <div class="g-t g-lbl"><span>Target</span><span>Goal</span><span>Counted by</span><span></span></div>
        ${g.targets.map((t, ti) => `<div class="g-t">
          <input data-ge="tname" data-gi="${gi}" data-ti="${ti}" value="${X(t.name)}" aria-label="Target">
          <input data-ge="value" data-gi="${gi}" data-ti="${ti}" value="${X(t.goal)}" aria-label="Goal number, or max N">
          ${byInput(gi, ti, t.by)}
          <button class="g-x" data-g="rmt" data-gi="${gi}" data-ti="${ti}" aria-label="Remove ${X(t.name)}">×</button></div>`).join("")}
        <div><button class="g-btn ghost" data-g="addt" data-gi="${gi}">+ Target</button></div></div>`).join("")}
      <div class="g-card"><div class="g-lbl">Add a goal</div>
        <input id="g-new-name" placeholder="Goal name"><input id="g-new-why" placeholder="Why it matters, one line">
        <div><button class="g-btn" data-g="addg">Add goal</button></div></div>
      <div class="g-card"><div class="g-lbl">Close friends (for 1-1s)</div>
        <input data-ge="close" value="${X(gv.close.join(", "))}" placeholder="Names, comma-separated" aria-label="Close friends"></div>
      <div class="g-card"><div class="g-lbl">What Counted by can say</div>
        <div class="g-help">${Object.entries(gv.metrics).map(([k, v]) => `<b>${X(k)}</b>: ${X(v)}`).join("<br>")}<br>
        Goal is a number, or <b>max N</b> for an upper limit. A target with <b>manual</b> shows under Not measured yet.</div></div>
      <div class="g-card"><div class="g-lbl">Change log</div><div class="g-log">${gv.log.map(l => `<div>${X(l)}</div>`).join("") || "<div>No changes yet.</div>"}</div></div>
    </div>`;
    el.scrollTop = keepScroll;
    if (fa) { const f = el.querySelector(fa); if (f) f.focus({preventScroll: true}); }
  }

  document.addEventListener("change", (e) => {
    const t = e.target; if (!t.dataset || !t.dataset.ge || !gv) return;
    const gi = +t.dataset.gi, ti = +t.dataset.ti, g = gv.goals[gi], v = t.value.trim();
    const k = t.dataset.ge;
    if (k === "close") return post({op: "close", names: v}, "Close friends updated");
    if (!g) return;
    if (k === "name" && v && v !== g.name) return post({op: "rename", goal: g.name, name: v}, "Goal updated");
    if (k === "why" && v !== g.why) return post({op: "why", goal: g.name, why: v}, "Goal updated");
    const tg = g.targets[ti]; if (!tg) return;
    if (k === "tname" && v && v !== tg.name) return post({op: "target", goal: g.name, index: ti, name: v}, "Target updated");
    if (k === "value" && v && v !== tg.goal) return post({op: "target", goal: g.name, index: ti, value: v}, "Target updated");
    if (k === "by" && v && v !== tg.by) return post({op: "target", goal: g.name, index: ti, by: v}, "Target updated");
  });
  document.addEventListener("click", async (e) => {
    const b = e.target.closest && e.target.closest("[data-g]"); if (!b) return;
    e.preventDefault(); e.stopPropagation();
    const a = b.dataset.g, g = gv && gv.goals[+b.dataset.gi];
    if (a === "edit") { sheet = true; drawSheet(); if (!gv) fetchGoals(); return; }
    if (a === "close") { sheet = false; drawSheet(); render(); return; }
    if (a === "week") { gweek = b.dataset.w; gv = null; render(); return; }
    if (a === "sel") { gsel = b.dataset.n; render(); return; }
    if (a === "dir") { gsel = b.dataset.n; return post({op: "direction", goal: b.dataset.n}, "Direction updated"); }
    if (a === "dismiss") { return post({op: "dismiss", sig: b.dataset.s}); }
    if (a === "task") {
      b.disabled = true;
      try { await api("api/goals", {op: "task", title: b.dataset.t, date: b.dataset.d}); toast(testMode ? "Test mode: would add the task, nothing updated" : "Task added, see Plan"); }
      catch (err) { toast("Failed: " + err.message); b.disabled = false; }
      return;
    }
    if (a === "pause" && g) return post({op: "pause", goal: g.name}, g.active ? "Goal paused, updated" : "Goal resumed, updated");
    if (a === "addt" && g) {
      let name = "New target", i = 2; while (g.targets.some(t => t.name === name)) name = "New target " + i++;
      return post({op: "add_target", goal: g.name, name, value: "1", by: "manual"}, "Target added, updated");
    }
    if (a === "rmt" && g) {
      const t = g.targets[+b.dataset.ti];
      if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = "Sure?"; b.style.fontSize = "11px"; return; }
      return post({op: "remove_target", goal: g.name, index: +b.dataset.ti}, `Removed ${t ? t.name : "target"}, updated`);
    }
    if (a === "addg") {
      const name = document.getElementById("g-new-name").value.trim(), why = document.getElementById("g-new-why").value.trim();
      if (!name) return;
      return post({op: "add_goal", name, why}, "Goal added, updated");
    }
  }, true);
  // Fresh numbers when he comes back to the tab.
  document.addEventListener("visibilitychange", () => { if (!document.hidden && tab === "goals") fetchGoals(); });
  if (tab === "goals") render();
})();
