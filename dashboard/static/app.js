"use strict";
/* Time Sync Lab front end. Plain JS + Chart.js, no build step. */
(() => {
const $ = (s, r = document) => r.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
};
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const tryStore = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };

const SCEN = [
  ["baseline", "None", "no background traffic"], ["low", "Low", "1.5 Mbps"], ["moderate", "Moderate", "3 Mbps"],
  ["high", "High", "6 Mbps"], ["extreme", "Extreme", "9 Mbps"], ["burst", "Bursty", "on and off"],
];
const POL = [
  ["static", "Static", "slow path only"], ["dynamic", "Dynamic", "Ryu reroutes"],
  ["static+prio", "Static + priority", "sync goes first"], ["dynamic+prio", "Dynamic + priority", "both"],
];
const POL_LABEL = Object.fromEntries(POL.map((p) => [p[0], p[1]]));
const CLIENTS = ["C1", "C2", "C3"];

const S = {
  lab: { state: "offline", message: "", root: true, netem: null, datapath: "" },
  limits: { high: 2.5, low: 0.8, slow_mbps: 5, fast_mbps: 20, queue_pkts: 60 },
  scen: tryStore("tsl.scen") || "high", pol: tryStore("tsl.pol") || "dynamic",
  run: null, runActive: false, matrix: null,
  probes: [], nets: [], routes: [], trafficAt: null, tNow: 0,
  offset: { C1: 0, C2: 0, C3: 0 }, probeCount: { C1: 0, C2: 0, C3: 0 }, lastOk: {},
  ladderClient: "C1", lastEventId: 0, dirty: false, tableDirty: false,
  serverSkew: 0, results: null, replayUntil: 0,
};

/* ------------------------------------------------------------ formatting */
const pad = (n, w = 2) => String(n).padStart(w, "0");
function hms(ms) { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; }
function hmsm(sec) { const d = new Date(sec * 1000); return `${hms(sec * 1000)}.${pad(d.getMilliseconds(), 3)}`; }
function fmtMs(ms, signed = false) {
  if (ms == null || isNaN(ms)) return "–";
  const a = Math.abs(ms), s = signed ? (ms < 0 ? "−" : "+") : (ms < 0 ? "−" : "");
  if (a >= 1000) return `${s}${(a / 1000).toFixed(2)} s`;
  if (a >= 100) return `${s}${a.toFixed(0)} ms`;
  if (a >= 1) return `${s}${a.toFixed(2)} ms`;
  if (a >= 0.001) return `${s}${a.toFixed(3)} ms`;
  return "0 ms";
}
const num = (v, d = 2) => (v == null || v === "" || isNaN(v) ? "–" : Number(v).toFixed(d));
function tone(absMs) { return absMs < 1 ? "good" : absMs < 50 ? "warn" : absMs < 500 ? "serious" : "bad"; }
function toast(msg, ms = 4200) {
  const t = $("#toast"); $("#toastText").textContent = msg; t.classList.add("show");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), ms);
}
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}
const clientColor = (i) => css(`--c${i + 1}`);

/* ----------------------------------------------------------------- theme */
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
}
applyTheme(tryStore("tsl.theme"));
$("#themeBtn").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme") ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const nx = cur === "dark" ? "light" : "dark";
  applyTheme(nx); tryStore("tsl.theme", nx); restyleCharts();
});

/* ------------------------------------------------------------------ tabs */
const tabs = ["live", "cmp", "how"];
tabs.forEach((t) => $("#tab-" + t).addEventListener("click", () => selectTab(t)));
function selectTab(t) {
  tabs.forEach((x) => {
    $("#tab-" + x).setAttribute("aria-selected", String(x === t));
    $("#p-" + x).hidden = x !== t;
  });
  if (t === "cmp") { loadResults(); }
  setTimeout(() => Object.values(charts).forEach((c) => c.resize()), 30);
}

/* -------------------------------------------------------------- controls */
function seg(host, items, getter, setter) {
  host.innerHTML = "";
  items.forEach(([v, name, sub]) => {
    const b = document.createElement("button");
    b.type = "button"; b.dataset.v = v; b.innerHTML = `${name}<small>${sub}</small>`;
    b.addEventListener("click", () => { setter(v); refreshSeg(); });
    host.appendChild(b);
  });
  host._get = getter;
}
function refreshSeg() {
  [["#scen"], ["#pol"]].forEach(([s]) => {
    const h = $(s);
    h.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === h._get())));
  });
  const sc = SCEN.find((s) => s[0] === S.scen), po = POL.find((p) => p[0] === S.pol);
  $("#hint").textContent = `Next run: ${sc[1].toLowerCase()} background traffic (${sc[2]}) with the ${po[1].toLowerCase()} policy. The slow link carries 5 Mbps.`;
}
seg($("#scen"), SCEN, () => S.scen, (v) => { S.scen = v; tryStore("tsl.scen", v); });
seg($("#pol"), POL, () => S.pol, (v) => { S.pol = v; tryStore("tsl.pol", v); });
refreshSeg();

$("#btnLab").addEventListener("click", async () => {
  const r = await api(S.lab.state === "ready" ? "/api/lab/stop" : "/api/lab/start", {});
  if (!r.ok) toast(r.message);
});
$("#btnRun").addEventListener("click", async () => {
  const r = await api("/api/run", { policy: S.pol, scenario: S.scen, duration: +$("#dur").value });
  if (!r.ok) toast(r.message);
});
$("#btnMatrix").addEventListener("click", async () => {
  const d = +$("#dur").value, mins = Math.round((24 * (d + 4 + 5)) / 60);
  if (!confirm(`This runs all 24 combinations (4 policies × 6 traffic levels) and takes about ${mins} minutes. Start?`)) return;
  const r = await api("/api/matrix", { duration: d });
  if (!r.ok) toast(r.message); else selectTab("live");
});
$("#btnAbort").addEventListener("click", () => api("/api/abort", {}));

function renderControls() {
  const st = S.lab.state, busy = st === "running";
  const pill = $("#labPill"); pill.dataset.state = st;
  const names = { offline: "Lab is off", starting: "Starting lab", ready: "Lab is ready", running: "Run in progress", error: "Lab error", stopping: "Stopping lab" };
  $("#labText").textContent = names[st] || st;
  const b = $("#btnLab");
  b.textContent = st === "ready" ? "Stop lab" : st === "starting" ? "Starting…" : st === "stopping" ? "Stopping…" : "Start lab";
  b.disabled = !(st === "offline" || st === "error" || st === "ready");
  $("#btnRun").disabled = st !== "ready"; $("#btnMatrix").disabled = st !== "ready";
  $("#btnAbort").hidden = !busy;
  ["#scen", "#pol"].forEach((s) => $(s).querySelectorAll("button").forEach((x) => (x.disabled = busy)));
  $("#dur").disabled = busy;
  const bn = $("#banner"), txt = $("#bannerText");
  bn.classList.remove("err");
  if (!S.lab.root) { bn.classList.add("show", "err"); txt.textContent = "The server is not running as root, so Mininet cannot start. Run it with: sudo /usr/bin/python3 dashboard/server.py"; $("#bannerBar").parentElement.style.display = "none"; }
  else if (st === "error") { bn.classList.add("show", "err"); txt.textContent = S.lab.message || "The lab hit an error."; $("#bannerBar").parentElement.style.display = "none"; }
  else if (st === "starting") { bn.classList.add("show"); txt.textContent = S.lab.message || "Starting Mininet, Open vSwitch and Ryu…"; $("#bannerBar").parentElement.style.display = "none"; }
  else if (busy) { bn.classList.add("show"); $("#bannerBar").parentElement.style.display = ""; }
  else bn.classList.remove("show");
}
function renderRunBanner() {
  if (S.lab.state !== "running" || !S.run) return;
  const m = S.run.matrix ? `Comparison ${S.run.matrix[0]} of ${S.run.matrix[1]}: ` : "";
  const left = Math.max(0, Math.ceil(S.run.total - S.tNow));
  $("#bannerText").textContent = `${m}${POL_LABEL[S.run.policy] || S.run.policy}, ${S.run.scenario} traffic. ${left} s left`;
  $("#bannerBar").style.width = Math.max(0, Math.min(100, (S.tNow / S.run.total) * 100)) + "%";
}

/* ---------------------------------------------------------------- clocks */
const clockEls = {};
function buildClocks() {
  const wall = $("#wall"); wall.innerHTML = "";
  [["ref", "Server", "var(--ink)"], ...CLIENTS.map((c, i) => [c, "Client " + (i + 1), `var(--c${i + 1})`])].forEach(([id, name, color]) => {
    const d = document.createElement("div"); d.className = "clock" + (id === "ref" ? " ref" : "");
    d.innerHTML = `<div class="who"><i style="background:${color}"></i>${name}</div>
      <svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="46" fill="var(--panel)" stroke="var(--line)" stroke-width="2"/></svg>
      <div class="digital"><span class="d-main">--:--:--</span><small class="d-ms">.000</small></div>
      <span class="chip" data-tone="${id === "ref" ? "good" : "idle"}"><span class="ctext">${id === "ref" ? "True time" : "Waiting"}</span></span>`;
    wall.appendChild(d);
    const svg = $("svg", d);
    for (let i = 0; i < 60; i++) {
      const a = (i * 6 * Math.PI) / 180, big = i % 5 === 0, r1 = big ? 38 : 42, r2 = 45;
      el("line", { x1: 50 + r1 * Math.sin(a), y1: 50 - r1 * Math.cos(a), x2: 50 + r2 * Math.sin(a), y2: 50 - r2 * Math.cos(a), stroke: "var(--ink-3)", "stroke-width": big ? 1.6 : 0.7, "stroke-linecap": "round" }, svg);
    }
    const hand = (w, len, stroke) => el("line", { x1: 50, y1: 50, x2: 50, y2: 50 - len, stroke, "stroke-width": w, "stroke-linecap": "round" }, svg);
    const hh = hand(3.4, 22, "var(--ink)"), mh = hand(2.4, 33, "var(--ink)"), sh = hand(1.3, 39, color);
    el("circle", { cx: 50, cy: 50, r: 2.6, fill: color }, svg);
    clockEls[id] = { root: d, hh, mh, sh, main: $(".d-main", d), ms: $(".d-ms", d), chip: $(".chip", d), ctext: $(".ctext", d) };
  });
}
function paintClocks() {
  const now = Date.now() + S.serverSkew;
  for (const id in clockEls) {
    const e = clockEls[id], off = id === "ref" ? 0 : S.offset[id] || 0;
    const t = new Date(now + off), ms = t.getMilliseconds(), s = t.getSeconds() + ms / 1000;
    const m = t.getMinutes() + s / 60, h = (t.getHours() % 12) + m / 60;
    e.sh.setAttribute("transform", `rotate(${s * 6} 50 50)`);
    e.mh.setAttribute("transform", `rotate(${m * 6} 50 50)`);
    e.hh.setAttribute("transform", `rotate(${h * 30} 50 50)`);
    e.main.textContent = hms(t.getTime()); e.ms.textContent = "." + pad(ms, 3);
  }
  requestAnimationFrame(paintClocks);
}
function snapFlash(id) {
  const r = clockEls[id].root; r.classList.remove("snap"); void r.offsetWidth; r.classList.add("snap");
}
function setOffset(c, ms, flash) {
  const prev = S.offset[c] || 0;
  S.offset[c] = ms;
  if (flash && Math.abs(prev - ms) > 20 && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    snapFlash(c); clockEls[c].root.style.color = `var(--c${CLIENTS.indexOf(c) + 1})`;
  }
}
function renderChips() {
  let maxAbs = 0, started = 0;
  CLIENTS.forEach((c) => {
    const e = clockEls[c], off = S.offset[c] || 0, a = Math.abs(off);
    maxAbs = Math.max(maxAbs, a); if (S.probeCount[c]) started++;
    if (!S.run) { e.chip.dataset.tone = "idle"; e.ctext.textContent = "Waiting"; return; }
    e.chip.dataset.tone = tone(a);
    e.ctext.textContent = a < 0.001 ? "In sync" : `${fmtMs(a)} ${off > 0 ? "ahead" : "behind"}`;
  });
  const st = $("#syncStatus"), b = $("#syncBadge"), tx = $("#syncText");
  if (!S.run) { st.dataset.tone = "idle"; b.textContent = "Idle"; tx.textContent = S.lab.state === "ready" ? "Choose traffic and a policy, then run an experiment." : "Start the lab, then run an experiment."; return; }
  if (!started) { st.dataset.tone = "bad"; b.textContent = "Clocks are wrong"; tx.textContent = `Sync starts in ${Math.max(0, Math.ceil(S.run.start_delay - S.tNow))} s`; return; }
  if (maxAbs < 1) { st.dataset.tone = "good"; b.textContent = "Synchronised"; tx.textContent = `All clients within ${fmtMs(maxAbs)} of the server`; }
  else { st.dataset.tone = "warn"; b.textContent = "Synchronising"; tx.textContent = `Largest error is ${fmtMs(maxAbs)}`; }
}

/* ---------------------------------------------------------------- ladder */
function buildLadderTabs() {
  const h = $("#ladderTabs"); h.innerHTML = "";
  CLIENTS.forEach((c, i) => {
    const b = document.createElement("button"); b.type = "button"; b.setAttribute("aria-pressed", String(c === S.ladderClient));
    b.innerHTML = `<i style="background:var(--c${i + 1})"></i>${c}`;
    b.addEventListener("click", () => { S.ladderClient = c; buildLadderTabs(); renderLadder(true); });
    h.appendChild(b);
  });
}
function renderLadder(redraw) {
  const svg = $("#ladder"), r = S.lastOk[S.ladderClient], i = CLIENTS.indexOf(S.ladderClient);
  const color = `var(--c${i + 1})`;
  if (!redraw && r && (svg._seq === r.seq + S.ladderClient || (svg._seq && Date.now() - (svg._t || 0) < 1600))) return;
  svg._t = Date.now(); svg.innerHTML = ""; svg._seq = r ? r.seq + S.ladderClient : null;
  const X1 = 125, X2 = 275, Y = [58, 100, 142, 184];
  el("text", { x: X1, y: 18, "text-anchor": "middle", "font-weight": 600 }, svg).textContent = S.ladderClient;
  el("text", { x: X1, y: 32, "text-anchor": "middle", class: "muted" }, svg).textContent = "client clock";
  el("text", { x: X2, y: 18, "text-anchor": "middle", "font-weight": 600 }, svg).textContent = "Server";
  el("text", { x: X2, y: 32, "text-anchor": "middle", class: "muted" }, svg).textContent = "true time";
  el("line", { x1: X1, y1: 40, x2: X1, y2: 214, class: "life" }, svg);
  el("line", { x1: X2, y1: 40, x2: X2, y2: 214, class: "life" }, svg);
  const labels = ["t1", "t2", "t3", "t4"];
  labels.forEach((l, k) => {
    const left = k === 0 || k === 3;
    el("circle", { cx: left ? X1 : X2, cy: Y[k], r: 4.5, fill: left ? color : "var(--ink)" }, svg);
    el("text", { x: left ? X1 - 12 : X2 + 12, y: Y[k] + 4, "text-anchor": left ? "end" : "start", "font-weight": 600 }, svg).textContent = l;
  });
  if (!r) {
    el("text", { x: 200, y: 124, "text-anchor": "middle", class: "muted" }, svg).textContent = "Waiting for the first reply";
    $("#maths").innerHTML = "";
    return;
  }
  const arrow = (x1, y1, x2, y2, cls) => {
    const p = el("line", { x1, y1, x2, y2, class: "arrow draw " + cls, stroke: color }, svg);
    const a = Math.atan2(y2 - y1, x2 - x1), hx = x2 - 9 * Math.cos(a), hy = y2 - 9 * Math.sin(a);
    el("path", { d: `M${x2} ${y2} L${hx - 4.5 * Math.sin(a)} ${hy + 4.5 * Math.cos(a)} L${hx + 4.5 * Math.sin(a)} ${hy - 4.5 * Math.cos(a)}Z`, fill: color, class: "draw2" }, svg);
  };
  arrow(X1 + 6, Y[0], X2 - 6, Y[1], "");
  arrow(X2 - 6, Y[2], X1 + 6, Y[3], "draw2");
  const vals = [r.t1, r.t2, r.t3, r.t4];
  vals.forEach((v, k) => {
    const left = k === 0 || k === 3;
    el("text", { x: left ? X1 - 12 : X2 + 12, y: Y[k] + 18, "text-anchor": left ? "end" : "start", class: "muted" }, svg).textContent = hmsm(v);
  });
  el("text", { x: 200, y: 66, "text-anchor": "middle", class: "muted" }, svg).textContent = "request";
  el("text", { x: 200, y: 168, "text-anchor": "middle", class: "muted" }, svg).textContent = "reply";
  const m = $("#maths");
  m.innerHTML = `
    <li><span class="f">Delay = (t4 − t1) − (t3 − t2)</span><b>Round trip on the wire</b><span class="v">${fmtMs(r.delay_ms)}</span></li>
    <li><span class="f">Offset = ((t2 − t1) + (t3 − t4)) / 2</span><b>Estimated clock offset</b><span class="v">${fmtMs(r.theta_ms, true)}</span></li>
    <li><span class="f">Client adds the offset from its best recent sample</span><b>Error after the step</b><span class="v">${fmtMs(Math.abs(r.error_signed_ms))}</span></li>`;
}

/* ---------------------------------------------------------------- charts */
const charts = {};
const overlayPlugin = {
  id: "overlay",
  beforeDatasetsDraw(chart) {
    const o = chart.options.plugins.overlay; if (!o || !o.on) return;
    const { ctx, chartArea: a, scales: { x } } = chart; if (!x) return;
    ctx.save(); ctx.beginPath(); ctx.rect(a.left, a.top, a.width, a.height); ctx.clip();
    ctx.font = '500 11px "Plex", sans-serif'; ctx.textBaseline = "top";
    // FAST path bands
    const bands = []; let start = null;
    S.routes.forEach((r) => { if (r.path === "fast" && start === null) start = r.t; if (r.path === "slow" && start !== null) { bands.push([start, r.t]); start = null; } });
    if (start !== null) bands.push([start, Math.max(S.tNow, start)]);
    ctx.fillStyle = css("--shade");
    bands.forEach(([s, e]) => { const x1 = x.getPixelForValue(s), x2 = x.getPixelForValue(e); ctx.fillRect(x1, a.top, Math.max(2, x2 - x1), a.height); });
    ctx.fillStyle = css("--ink-3");
    bands.forEach(([s]) => ctx.fillText("fast path", x.getPixelForValue(s) + 5, a.top + 4));
    // traffic start
    ctx.strokeStyle = css("--ink-3"); ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
    if (S.trafficAt != null) {
      const px = x.getPixelForValue(S.trafficAt); ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
      ctx.fillStyle = css("--ink-2"); ctx.fillText("traffic starts", px + 5, a.bottom - 16);
    }
    // route changes
    ctx.setLineDash([]); ctx.strokeStyle = css("--ink-2"); ctx.lineWidth = 1.5;
    S.routes.forEach((r) => { const px = x.getPixelForValue(r.t); ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke(); });
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const o = chart.options.plugins.overlay; if (!o || !o.lost) return;
    const { ctx, chartArea: a, scales: { x } } = chart;
    ctx.save(); ctx.lineWidth = 1.6; ctx.lineCap = "round";
    S.probes.forEach((p) => {
      if (!p.lost) return;
      ctx.strokeStyle = clientColor(CLIENTS.indexOf(p.client)); const px = x.getPixelForValue(p.t), py = a.bottom - 7;
      ctx.beginPath(); ctx.moveTo(px - 3.5, py - 3.5); ctx.lineTo(px + 3.5, py + 3.5); ctx.moveTo(px + 3.5, py - 3.5); ctx.lineTo(px - 3.5, py + 3.5); ctx.stroke();
    });
    ctx.restore();
  },
};
Chart.register(overlayPlugin);

function baseOpts(ytitle, extra = {}) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false, normalized: true, parsing: false,
    interaction: { mode: "nearest", axis: "x", intersect: false },
    plugins: { legend: { display: false }, overlay: { on: false }, tooltip: { backgroundColor: css("--ink"), titleColor: css("--panel"), bodyColor: css("--panel"), padding: 10, cornerRadius: 8, displayColors: true, boxPadding: 4 } },
    scales: {
      x: { type: "linear", min: 0, max: 24, title: { display: true, text: "Seconds into the run", color: css("--ink-3") }, ticks: { color: css("--ink-3"), maxTicksLimit: 9 }, grid: { color: css("--line-2") }, border: { color: css("--line") } },
      y: { title: { display: true, text: ytitle, color: css("--ink-3") }, ticks: { color: css("--ink-3") }, grid: { color: css("--line-2") }, border: { display: false }, ...extra },
    },
  };
}
const lineDs = (label, color, dash) => ({ label, data: [], borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0, borderDash: dash || [], spanGaps: true });

function buildCharts() {
  const ds = (suffix) => CLIENTS.map((c, i) => lineDs(c + (suffix || ""), clientColor(i)));
  const logTicks = { callback: (v) => ([0.01, 0.1, 1, 10, 100, 1000, 10000].includes(v) ? (v >= 1000 ? v / 1000 + " s" : v + " ms") : "") };
  charts.err = new Chart($("#chErr"), { type: "line", data: { datasets: ds() },
    options: Object.assign(baseOpts("Clock error", { type: "logarithmic", min: 0.01, max: 10000, ticks: { color: css("--ink-3"), ...logTicks } }),
      { plugins: { legend: { display: false }, overlay: { on: true }, tooltip: tip((c) => `${c.dataset.label}: ${fmtMs(c.parsed.y)}`) } }) });
  charts.rtt = new Chart($("#chRtt"), { type: "line", data: { datasets: ds() },
    options: Object.assign(baseOpts("Round trip (ms)", { beginAtZero: true }), { plugins: { legend: { display: false }, overlay: { on: true, lost: true }, tooltip: tip((c) => `${c.dataset.label}: ${c.parsed.y.toFixed(1)} ms`) } }) });
  charts.thr = new Chart($("#chThr"), { type: "line", data: { datasets: [
    lineDs("Slow link", css("--serious")), lineDs("Fast link", css("--c1")),
    lineDs("Move to fast above", css("--ink-3"), [6, 5]), lineDs("Move back below", css("--ink-3"), [2, 4]) ] },
    options: Object.assign(baseOpts("Mbps", { beginAtZero: true, suggestedMax: 6 }), { plugins: { legend: { display: false }, overlay: { on: true }, tooltip: tip((c) => `${c.dataset.label}: ${c.parsed.y.toFixed(2)} Mbps`) } }) });
  charts.thr.data.datasets[2].borderWidth = 1.2; charts.thr.data.datasets[3].borderWidth = 1.2;
  legend("#legErr", CLIENTS.map((c, i) => [c, `var(--c${i + 1})`]));
  legend("#legRtt", CLIENTS.map((c, i) => [c, `var(--c${i + 1})`]));
  legend("#legThr", [["Slow link (5 Mbps, 50 ms)", "var(--serious)"], ["Fast link (20 Mbps, 5 ms)", "var(--c1)"], ["Controller thresholds", "var(--ink-3)"]]);
}
function tip(fn) {
  return { backgroundColor: css("--ink"), titleColor: css("--panel"), bodyColor: css("--panel"), padding: 10, cornerRadius: 8, boxPadding: 4,
    callbacks: { title: (it) => `${it[0].parsed.x.toFixed(1)} s`, label: fn } };
}
function legend(sel, items) {
  $(sel).innerHTML = items.map(([n, c]) => `<span><i style="background:${c}"></i>${n}</span>`).join("");
}
function restyleCharts() {
  CLIENTS.forEach((c, i) => { [charts.err, charts.rtt].forEach((ch) => { const d = ch.data.datasets[i]; d.borderColor = d.backgroundColor = clientColor(i); }); });
  const t = charts.thr.data.datasets; t[0].borderColor = t[0].backgroundColor = css("--serious"); t[1].borderColor = t[1].backgroundColor = css("--c1");
  t[2].borderColor = t[3].borderColor = css("--ink-3");
  Object.values(charts).forEach((c) => {
    for (const k of ["x", "y"]) { const s = c.options.scales[k]; if (!s) continue; s.ticks.color = css("--ink-3"); s.grid.color = css("--line-2"); if (s.title) s.title.color = css("--ink-3"); }
    const tp = c.options.plugins.tooltip; tp.backgroundColor = css("--ink"); tp.titleColor = tp.bodyColor = css("--panel");
    c.update("none");
  });
  if (cmp.err) renderCompare();
}

/* ---------------------------------------------------------------- topology */
const topo = {};
function buildTopo() {
  const svg = $("#topo"); svg.innerHTML = "";
  const slowD = "M150 118 C190 38 250 38 290 118", fastD = "M150 118 C190 188 250 188 290 118";
  topo.slow = el("path", { d: slowD, class: "link", "stroke-width": 7 }, svg);
  topo.fast = el("path", { d: fastD, class: "link", "stroke-width": 7 }, svg);
  topo.slowFlow = el("path", { d: slowD, class: "flow", stroke: css("--c1"), "stroke-width": 3 }, svg);
  topo.fastFlow = el("path", { d: fastD, class: "flow", stroke: css("--c1"), "stroke-width": 3 }, svg);
  topo.bgSlow = el("path", { d: slowD, class: "flow", stroke: css("--serious"), "stroke-width": 3, transform: "translate(0 5)" }, svg);
  const node = (x, y, w, h, l1, l2, cls = "node") => {
    el("rect", { x, y, width: w, height: h, rx: 9, class: cls }, svg);
    el("text", { x: x + w / 2, y: y + h / 2 - (l2 ? 2 : -4), "text-anchor": "middle", "font-weight": 600 }, svg).textContent = l1;
    if (l2) el("text", { x: x + w / 2, y: y + h / 2 + 12, "text-anchor": "middle", class: "muted" }, svg).textContent = l2;
  };
  node(8, 56, 82, 44, "Clients", "h1 h2 h3"); node(8, 138, 82, 44, "Background", "h5 h6 h7"); node(334, 94, 80, 48, "Server", "h4");
  el("line", { x1: 90, y1: 78, x2: 130, y2: 112, stroke: "var(--line)", "stroke-width": 3 }, svg);
  el("line", { x1: 90, y1: 160, x2: 130, y2: 124, stroke: "var(--line)", "stroke-width": 3 }, svg);
  el("line", { x1: 310, y1: 118, x2: 334, y2: 118, stroke: "var(--line)", "stroke-width": 3 }, svg);
  [[150, "s1"], [290, "s2"]].forEach(([x, n]) => { el("circle", { cx: x, cy: 118, r: 19, class: "sw" }, svg); el("text", { x, y: 122, "text-anchor": "middle", "font-weight": 600 }, svg).textContent = n; });
  el("text", { x: 220, y: 52, "text-anchor": "middle", "font-weight": 600 }, svg).textContent = "Slow path";
  el("text", { x: 220, y: 66, "text-anchor": "middle", class: "muted" }, svg).textContent = "5 Mbps, 50 ms";
  el("text", { x: 220, y: 182, "text-anchor": "middle", "font-weight": 600 }, svg).textContent = "Fast path";
  el("text", { x: 220, y: 196, "text-anchor": "middle", class: "muted" }, svg).textContent = "20 Mbps, 5 ms";
  el("line", { x1: 150, y1: 137, x2: 190, y2: 228, class: "ctl" }, svg); el("line", { x1: 290, y1: 137, x2: 250, y2: 228, class: "ctl" }, svg);
  el("rect", { x: 160, y: 228, width: 120, height: 26, rx: 8, class: "node" }, svg);
  el("text", { x: 220, y: 245, "text-anchor": "middle", "font-weight": 600 }, svg).textContent = "Ryu controller";
}
function renderTopo() {
  const n = S.nets[S.nets.length - 1], L = S.limits;
  const path = n ? n.path : "slow", sl = n ? n.slow_mbps : 0, fa = n ? n.fast_mbps : 0;
  const util = Math.min(1, sl / L.slow_mbps);
  topo.slow.setAttribute("stroke", util > 0.8 ? css("--serious") : util > 0.5 ? css("--warn") : css("--line"));
  topo.slow.setAttribute("stroke-opacity", util > 0.5 ? 0.55 : 1);
  topo.fast.setAttribute("stroke", fa > 0.5 ? css("--c1") : css("--line")); topo.fast.setAttribute("stroke-opacity", fa > 0.5 ? 0.35 : 1);
  const active = S.runActive;
  topo.slowFlow.classList.toggle("on", active && path === "slow"); topo.fastFlow.classList.toggle("on", active && path === "fast");
  topo.bgSlow.classList.toggle("on", active && sl > 0.3);
  topo.slowFlow.style.animationDuration = "1.4s"; topo.fastFlow.style.animationDuration = "0.7s"; topo.bgSlow.style.animationDuration = "1s";
  $("#topoSub").textContent = `Sync traffic is on the ${path} path`;
  $("#gLoad").style.width = Math.min(100, (sl / L.slow_mbps) * 100) + "%";
  $("#gLoad").style.background = util > 0.8 ? css("--serious") : util > 0.5 ? css("--warn") : css("--ink");
  $("#gLoadTxt").textContent = `${sl.toFixed(1)} of ${L.slow_mbps} Mbps`;
  const q = n ? n.backlog_pkts : 0;
  $("#gQ").style.width = Math.min(100, (q / L.queue_pkts) * 100) + "%";
  $("#gQ").style.background = q > L.queue_pkts * 0.8 ? css("--serious") : css("--ink");
  $("#gQTxt").textContent = `${q} of ${L.queue_pkts} packets`;
}

/* ---------------------------------------------------------------- KPIs */
function renderKpis() {
  const ok = S.probes.filter((p) => !p.lost), lost = S.probes.length - ok.length;
  const mean = ok.length ? ok.reduce((a, p) => a + p.delay_ms, 0) / ok.length : null;
  const maxErr = Math.max(0, ...CLIENTS.map((c) => Math.abs(S.offset[c] || 0)));
  const n = S.nets[S.nets.length - 1], L = S.limits;
  const items = [
    ["Mean round trip", mean == null ? "–" : mean.toFixed(1), "ms", mean == null ? "" : `${ok.length} replies`],
    ["Largest clock error", S.run ? fmtMs(maxErr).replace(" ms", "").replace(" s", "") : "–", S.run ? (maxErr >= 1000 ? "s" : "ms") : "", S.run ? "right now" : ""],
    ["Probes lost", S.probes.length ? (100 * lost / S.probes.length).toFixed(1) : "–", "%", `${lost} of ${S.probes.length}`],
    ["Slow link load", n ? n.slow_mbps.toFixed(1) : "–", "Mbps", `limit ${L.slow_mbps} Mbps`],
    ["Packets queued", n ? n.backlog_pkts : "–", "pkts", n ? `${n.drops} dropped` : ""],
    ["Route changes", S.routes.length, "", n ? `on ${n.path} path` : ""],
  ];
  $("#kpis").innerHTML = items.map(([k, v, u, nn]) => `<div class="kpi"><div class="k">${k}</div><div class="v">${v} <small>${u}</small></div><div class="n">${nn || "&nbsp;"}</div></div>`).join("");
}

/* ---------------------------------------------------------------- feed + table */
function addFeed(text, level = "info", tSec) {
  const f = $("#feed");
  if (f.firstElementChild && f.firstElementChild.dataset.empty) f.innerHTML = "";
  const li = document.createElement("li"); li.dataset.l = level;
  li.innerHTML = `<span class="t">${hms(tSec ? tSec * 1000 : Date.now())}</span><span class="mk"></span><span></span>`;
  li.lastChild.textContent = text; f.prepend(li);
  while (f.children.length > 60) f.lastChild.remove();
}
$("#feed").firstElementChild.dataset.empty = "1";
function renderTable() {
  const tb = $("#probeTable tbody"), rows = S.probes.slice(-14).reverse();
  tb.innerHTML = rows.length ? rows.map((p) => `<tr><td>${p.t.toFixed(1)} s</td><td><span class="sw-dot" style="background:var(--c${CLIENTS.indexOf(p.client) + 1})"></span>${p.client}</td>` +
    (p.lost ? `<td class="num" colspan="3">no reply</td>` : `<td class="num">${p.delay_ms.toFixed(2)}</td><td class="num">${p.theta_ms.toFixed(2)}</td><td class="num">${Math.abs(p.error_signed_ms).toFixed(3)}</td>`) + "</tr>").join("")
    : `<tr><td colspan="5" class="empty">Run an experiment to see probes here.</td></tr>`;
}

/* ---------------------------------------------------------------- event handling */
function resetRun(run) {
  S.run = run; S.runActive = true; S.probes = []; S.nets = []; S.routes = []; S.trafficAt = null; S.tNow = 0; S.lastOk = {};
  S.probeCount = { C1: 0, C2: 0, C3: 0 };
  CLIENTS.forEach((c, i) => { S.offset[c] = 0; });
  run.skews.forEach((s, i) => { setOffset(CLIENTS[i], s * 1000, false); });
  [charts.err, charts.rtt, charts.thr].forEach((c) => c.data.datasets.forEach((d) => (d.data = [])));
  charts.err.data.datasets.forEach((d, i) => { if (run.skews[i] != null) d.data.push({ x: 0, y: Math.max(0.01, Math.abs(run.skews[i] * 1000)) }); });
  const T = Math.ceil(run.total);
  [charts.err, charts.rtt, charts.thr].forEach((c) => (c.options.scales.x.max = T));
  const L = S.limits; charts.thr.data.datasets[2].data = [{ x: 0, y: L.high }, { x: T, y: L.high }]; charts.thr.data.datasets[3].data = [{ x: 0, y: L.low }, { x: T, y: L.low }];
  $("#feed").innerHTML = ""; renderLadder(true);
  addFeed(`Run started: ${run.scenario} traffic, ${POL_LABEL[run.policy] || run.policy}`, "info");
  addFeed(`Client clocks start off by ${run.skews.map((s) => (s > 0 ? "+" : "−") + Math.abs(s).toFixed(2) + " s").join(", ")}`, "warn");
  S.dirty = S.tableDirty = true;
}
function onProbe(p) {
  const ci = CLIENTS.indexOf(p.client); if (ci < 0) return;
  S.probes.push(p); S.probeCount[p.client]++;
  if (!p.lost) charts.rtt.data.datasets[ci].data.push({ x: p.t, y: p.delay_ms });
  const e = Math.max(0.01, Math.abs(p.error_signed_ms));
  charts.err.data.datasets[ci].data.push({ x: p.t, y: e });
  if (!p.lost) S.lastOk[p.client] = p;
  const first = S.probeCount[p.client] === 1;
  setOffset(p.client, p.error_signed_ms, first || Math.abs(p.error_signed_ms - p.offset_before_ms) > 20);
  if (first) addFeed(`${p.client} sent its first probe and corrected its clock`, "ok", p.wall_time);
  S.dirty = S.tableDirty = true;
}
function onNet(n) {
  S.tNow = n.t; S.nets.push(n);
  const d = charts.thr.data.datasets;
  d[0].data.push({ x: n.t, y: n.slow_mbps }); d[1].data.push({ x: n.t, y: n.fast_mbps });
  S.dirty = true;
}
function onRoute(r) {
  const path = String(r.path).toLowerCase(); S.routes.push({ t: r.t, path });
  S.dirty = true;
}
function onTraffic(t) { S.trafficAt = t.t; S.dirty = true; }
function onRunEnd(ev) {
  S.runActive = false; S.dirty = true;
  const s = ev.summary;
  if (!ev.aborted && ev.id > S.replayUntil) toast("Run finished");
  loadResults();
}
function handle(ev) {
  switch (ev.type) {
    case "lab": Object.assign(S.lab, { state: ev.state, message: ev.message, datapath: ev.datapath, netem: ev.netem }); renderControls(); break;
    case "log": addFeed(ev.text, ev.level === "ok" ? "ok" : ev.level === "error" ? "error" : ev.level === "warn" ? "warn" : "info", ev.t); break;
    case "run_start": resetRun(ev.run); renderControls(); break;
    case "probe": onProbe(ev.row); break;
    case "net": onNet(ev); break;
    case "route": onRoute(ev); break;
    case "traffic": onTraffic(ev); break;
    case "run_end": onRunEnd(ev); break;
    case "matrix": if (ev.phase === "done") { if (ev.id > S.replayUntil) toast(ev.aborted ? "Comparison stopped" : "Comparison finished"); loadResults(); } break;
  }
}

/* ---------------------------------------------------------------- streaming */
let es = null;
function connect(since) {
  if (es) es.close();
  es = new EventSource("/api/events?since=" + since);
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data); S.lastEventId = ev.id; ev.id = ev.id; handle(ev);
  };
  es.onerror = () => { es.close(); setTimeout(() => connect(S.lastEventId), 1500); };
}
function flush() {
  if (S.dirty) {
    S.dirty = false;
    [charts.err, charts.rtt, charts.thr].forEach((c) => c.update("none"));
    renderChips(); renderKpis(); renderTopo(); renderRunBanner(); renderLadder(false);
  }
  if (S.tableDirty) { S.tableDirty = false; renderTable(); }
}
setInterval(flush, 120);
setInterval(() => { if (S.runActive) { renderChips(); renderRunBanner(); } }, 500);

/* ---------------------------------------------------------------- compare */
const cmp = {};
const SC_ORDER = ["baseline", "low", "moderate", "high", "extreme", "burst"];
const POL_ORDER = ["static", "dynamic", "static+prio", "dynamic+prio"];
const polColor = (i) => css(`--c${i + 1}`);
async function loadResults() {
  try { S.results = await api("/api/results"); } catch (e) { return; }
  renderCompare();
}
function renderCompare() {
  const R = S.results; if (!R) return;
  const m = R.matrix, rows = m ? m.rows : [];
  $("#cmpSource").textContent = !m ? "No results yet. Run a comparison to fill this in." : m.source === "live" ? `Your comparison from ${new Date(m.when * 1000).toLocaleString()}` : "Showing the sample run that ships with the project. Run your own comparison to replace it.";
  legend("#legCmp", POL_ORDER.map((p, i) => [POL_LABEL[p], polColor(i)]));
  const get = (p, s, k) => { const r = rows.find((x) => x.policy === p && x.scenario === s); return r && r[k] !== "" ? r[k] : null; };
  const mk = (key, canvas, k, opts) => {
    const data = { labels: SC_ORDER.map((s) => SCEN.find((x) => x[0] === s)[1]), datasets: POL_ORDER.map((p, i) => ({ label: POL_LABEL[p], data: SC_ORDER.map((s) => get(p, s, k)), backgroundColor: polColor(i), borderRadius: 3, borderSkipped: "bottom", maxBarThickness: 18 })) };
    if (cmp[key]) { cmp[key].data = data; cmp[key].options.scales.y.ticks.color = css("--ink-3"); cmp[key].options.scales.x.ticks.color = css("--ink-3"); cmp[key].options.scales.y.grid.color = css("--line-2"); cmp[key].update("none"); return; }
    cmp[key] = charts[key] = new Chart($(canvas), { type: "bar", data, options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 400 }, datasets: { bar: { categoryPercentage: 0.8, barPercentage: 0.92 } },
      plugins: { legend: { display: false }, tooltip: { backgroundColor: css("--ink"), titleColor: css("--panel"), bodyColor: css("--panel"), padding: 10, cornerRadius: 8, callbacks: { label: (c) => `${c.dataset.label}: ${opts.fmt(c.parsed.y)}` } } },
      scales: { x: { ticks: { color: css("--ink-3") }, grid: { display: false }, border: { color: css("--line") } }, y: Object.assign({ ticks: { color: css("--ink-3") }, grid: { color: css("--line-2") }, border: { display: false } }, opts.y) } } });
  };
  mk("cmpErr", "#chCmpErr", "steady_synced_err_ms", { fmt: (v) => fmtMs(v), y: { type: "logarithmic", min: 0.01, max: 10000, ticks: { color: css("--ink-3"), callback: (v) => ([0.01, 0.1, 1, 10, 100, 1000, 10000].includes(v) ? (v >= 1000 ? v / 1000 + " s" : v + " ms") : "") } } });
  mk("cmpRtt", "#chCmpRtt", "mean_rtt_ms", { fmt: (v) => v.toFixed(1) + " ms", y: { beginAtZero: true, title: { display: true, text: "ms", color: css("--ink-3") } } });
  const cols = [["scenario", "Traffic"], ["policy", "Policy"], ["mean_rtt_ms", "Mean RTT (ms)"], ["p95_rtt_ms", "p95 RTT (ms)"], ["steady_synced_err_ms", "Clock error (ms)"], ["loss_%", "Lost (%)"], ["route_changes", "Route changes"], ["first_reroute_s", "First reroute (s)"], ["slow_link_mbps", "Slow link (Mbps)"]];
  const tbl = (rs, withWhen) => `<thead><tr>${cols.map(([k, n], i) => `<th class="${i > 1 ? "num" : ""}">${n}</th>`).join("")}</tr></thead><tbody>${rs.map((r) => `<tr>${cols.map(([k], i) => `<td class="${i > 1 ? "num" : ""}">${i < 2 ? (i === 1 ? (POL_LABEL[r[k]] || r[k]) : r[k]) : num(r[k], k === "route_changes" ? 0 : 2)}</td>`).join("")}</tr>`).join("")}</tbody>`;
  const sorted = [...rows].sort((a, b) => SC_ORDER.indexOf(a.scenario) - SC_ORDER.indexOf(b.scenario) || POL_ORDER.indexOf(a.policy) - POL_ORDER.indexOf(b.policy));
  $("#cmpTable").innerHTML = rows.length ? tbl(sorted) : `<tbody><tr><td class="empty">Nothing to show yet.</td></tr></tbody>`;
  const h = [...(R.history || [])].reverse();
  $("#histTable").innerHTML = h.length ? tbl(h) : `<tbody><tr><td class="empty">Runs you do in this session appear here.</td></tr></tbody>`;
}

/* ---------------------------------------------------------------- calculator */
function calc() {
  const err = +$("#sErr").value, f = +$("#sFwd").value, r = +$("#sRet").value, proc = 1;
  $("#oErr").textContent = (err > 0 ? "+" : "") + err.toFixed(1) + " s"; $("#oFwd").textContent = f + " ms"; $("#oRet").textContent = r + " ms";
  const t1 = err * 1000, t2 = f, t3 = f + proc, t4 = f + proc + r + err * 1000;
  const delay = (t4 - t1) - (t3 - t2), theta = ((t2 - t1) + (t3 - t4)) / 2, need = -err * 1000, resid = theta - need;
  $("#calc").innerHTML = `
    <li><span class="f">Delay = (t4 − t1) − (t3 − t2)</span><b>Round trip</b><span class="v">${delay.toFixed(1)} ms</span></li>
    <li><span class="f">Offset = ((t2 − t1) + (t3 − t4)) / 2</span><b>Estimated correction</b><span class="v">${fmtMs(theta, true)}</span></li>
    <li><span class="f">The correction the client really needs</span><b>True correction</b><span class="v">${fmtMs(need, true)}</span></li>
    <li><span class="f">(request delay − reply delay) / 2</span><b>Error left after sync</b><span class="v result-big">${fmtMs(Math.abs(resid))}</span></li>`;
  $("#calcNote").textContent = Math.abs(f - r) < 0.5 ? "Equal delays: the estimate is exact, whatever the delay." : "The two directions differ, so the estimate is off by half the difference. This is what a congested queue does.";
}
["#sErr", "#sFwd", "#sRet"].forEach((s) => $(s).addEventListener("input", calc));

/* ---------------------------------------------------------------- boot */
async function boot() {
  buildClocks(); buildLadderTabs(); buildCharts(); buildTopo(); renderTable(); renderKpis(); renderLadder(true); calc();
  requestAnimationFrame(paintClocks);
  let st = null;
  try { st = await api("/api/state"); } catch (e) { $("#banner").classList.add("show", "err"); $("#bannerText").textContent = "Cannot reach the dashboard server."; }
  let since = 0;
  if (st) {
    S.serverSkew = st.server_time * 1000 - Date.now(); Object.assign(S.limits, st.limits); Object.assign(S.lab, st.lab);
    S.lastEventId = st.last_id; S.replayUntil = st.last_id; since = st.run_start_id ? st.run_start_id - 1 : st.last_id;
    renderControls();
  }
  connect(since); loadResults();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) S.dirty = true; });
}
boot();
})();
