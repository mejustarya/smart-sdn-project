const $ = id => document.getElementById(id);
const num = (v, d = 1) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);
const axis = {ticks:{color:"#71839d",maxTicksLimit:7},grid:{color:"#1b293b"}};
const common = {responsive:true,maintainAspectRatio:false,animation:false,
  plugins:{legend:{display:true,labels:{color:"#8293aa",boxWidth:10,font:{size:10}}}},
  scales:{x:axis,y:{beginAtZero:true,ticks:{color:"#71839d"},grid:{color:"#1b293b"}}}};

const HAS_CHART = typeof Chart !== "undefined";
const mk = (el, cfg) => HAS_CHART ? new Chart(el, cfg) : null;
const rateChart = mk($("rateChart"), {type:"line", data:{labels:[], datasets:[
  {label:"Slow link Mbps", data:[], borderColor:"#43d9e8", backgroundColor:"rgba(67,217,232,.10)", fill:true, tension:.3, pointRadius:2, borderWidth:2},
  {label:"Reroute threshold", data:[], borderColor:"#ff657a", borderDash:[6,5], pointRadius:0, borderWidth:1.2}]}, options:common});
const rttChart = mk($("rttChart"), {type:"line", data:{labels:[], datasets:[
  {label:"On SLOW path", data:[], borderColor:"#ffb454", backgroundColor:"#ffb454", tension:.25, pointRadius:3, borderWidth:2, spanGaps:false},
  {label:"On FAST path", data:[], borderColor:"#43d9e8", backgroundColor:"#43d9e8", tension:.25, pointRadius:3, borderWidth:2, spanGaps:false}]}, options:common});

function fmtBytes(n){ if(n<1024) return n+" B"; if(n<1048576) return (n/1024).toFixed(1)+" KB"; return (n/1048576).toFixed(2)+" MB"; }

function renderPorts(switches){
  let html = "";
  for (const [sw, ports] of Object.entries(switches || {})) {
    for (const p of ports || []) {
      let name = p.name;
      if ((sw==="s1" && name.endsWith("eth5")) || (sw==="s2" && name.endsWith("eth2"))) name += " · SLOW";
      if ((sw==="s1" && name.endsWith("eth6")) || (sw==="s2" && name.endsWith("eth3"))) name += " · FAST";
      html += `<div class="port-row"><b>${sw} ${name}</b><span>TX ${fmtBytes(p.tx_bytes)}</span><span>RX ${fmtBytes(p.rx_bytes)}</span></div>`;
    }
  }
  $("ports").innerHTML = html || "OVS statistics unavailable. Check passwordless sudo for ovs-ofctl.";
}

function renderTopology(d){
  const slowMbps = Number(d.slow_link_mbps || 0), fast = d.path === "FAST";
  const util = slowMbps / 5.0;                       // slow link capacity is 5 Mbps
  const cls = util < 0.3 ? "u-ok" : util < 0.6 ? "u-warn" : "u-hot";
  const slow = $("slowLink"), fastL = $("fastLink");
  slow.setAttribute("class", `lk ${cls}` + ((slowMbps > 0.05 || !fast) ? " active" : ""));
  fastL.setAttribute("class", fast ? "lk u-fast active" : "lk idle");
  $("slowActive").style.display = fast ? "none" : "";
  $("fastActive").style.display = fast ? "" : "none";
  $("topoTag").textContent = fast ? "REROUTED → FAST" : (util >= 0.6 ? "CONGESTED" : "LIVE VIEW");
}

function renderExperiment(e){
  const active = !!e.active;
  $("expTag").textContent = active ? "RUNNING" : "IDLE";
  $("expStart").disabled = active; $("expStop").disabled = !active;
  const phases = e.plan || [];
  $("phases").innerHTML = phases.map((p, i) => {
    const c = active ? (i < e.phase_index ? "done" : i === e.phase_index ? "now" : "") : (e.status === "completed" ? "done" : "");
    return `<span class="phase ${c}">${p.name} · ${p.seconds}s</span>`;
  }).join("");
  if (active) {
    $("expBar").style.width = Math.min(100, 100 * (e.elapsed || 0) / (e.total || 1)) + "%";
    $("expMessage").textContent = `${e.message} — ${Math.round(e.elapsed||0)}s of ${e.total}s`;
  } else if (e.status) {
    $("expBar").style.width = e.status === "completed" ? "100%" : $("expBar").style.width;
    $("expMessage").textContent = e.message || "";
  }
}

async function refresh(){
  try {
    const [r, rs] = await Promise.all([fetch("/api/status",{cache:"no-store"}), fetch("/api/series",{cache:"no-store"})]);
    const d = await r.json(), series = await rs.json();
    const connected = (d.connected_switches || []).length;
    $("switchCount").textContent = connected + " / 2";
    $("connection").textContent = connected === 2 ? "Connected" : "Waiting for switches";
    $("path").textContent = d.path || "UNKNOWN"; $("path").className = "path " + (d.path || "").toLowerCase();
    $("pathDesc").textContent = d.path === "FAST" ? "20 Mbps · 5 ms" : "5 Mbps · 50 ms";
    $("rate").textContent = Number(d.slow_link_mbps || 0).toFixed(3);
    $("policy").textContent = (d.policy || "unknown").toUpperCase();
    $("highThreshold").textContent = d.high_threshold_mbps ?? "2.5";
    $("clock").textContent = d.server_time || "--:--:--";

    // last sample that actually contained sync data
    const withRtt = [...series].reverse().find(s => s.rtt_ms !== null);
    const recent = series.slice(-3);
    const lastLoss = [...recent].reverse().find(s => s.loss_pct !== null);
    $("mRtt").textContent = num(withRtt && withRtt.rtt_ms, 1);
    const withJit = [...series].reverse().find(s => s.jitter_ms !== null);
    $("mJitter").textContent = num(withJit && withJit.jitter_ms, 2);
    $("mLoss").textContent = num(lastLoss && lastLoss.loss_pct, 1);
    const withErr = [...series].reverse().find(s => s.sync_err_ms !== null);
    $("mErr").textContent = num(withErr && withErr.sync_err_ms, 3);

    const labels = series.map(s => s.label), thr = d.high_threshold_mbps ?? 2.5;
    if (HAS_CHART) {
      rateChart.data.labels = labels;
      rateChart.data.datasets[0].data = series.map(s => s.slow_mbps);
      rateChart.data.datasets[1].data = series.map(() => thr);
      rateChart.update("none");
      rttChart.data.labels = labels;
      rttChart.data.datasets[0].data = series.map(s => s.path === "SLOW" ? s.rtt_ms : null);
      rttChart.data.datasets[1].data = series.map(s => s.path === "FAST" ? s.rtt_ms : null);
      rttChart.update("none");
    } else {
      document.querySelectorAll(".chart-wrap").forEach(w => w.textContent = "Charts need internet access (Chart.js CDN is unreachable).");
    }

    renderTopology(d); renderPorts(d.switches); renderExperiment(d.experiment || {});
    $("logs").textContent = d.controller_log || "Waiting for controller log…";
  } catch (e) { $("connection").textContent = "Dashboard API error"; }
}

async function post(url, payload){
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload || {})});
  return r.json();
}
async function action(payload){
  $("actionMessage").textContent = "Sending command…";
  try {
    const d = await post("/api/action", payload);
    $("actionMessage").textContent = d.message || "Command completed";
    $("actionMessage").style.color = d.ok ? "#43d6a0" : "#ff8a9a";
  } catch (e) { $("actionMessage").textContent = "Could not contact dashboard backend"; }
}
const dur = () => Number($("duration").value || 120);
document.querySelectorAll("[data-level]").forEach(b => b.addEventListener("click", () => action({action:"scenario", level:b.dataset.level, seconds:dur()})));
$("stopBtn").addEventListener("click", () => action({action:"stop"}));
$("syncBtn").addEventListener("click", () => action({action:"sync", rounds:40}));
$("tcpBtn").addEventListener("click", () => action({action:"tcp", seconds:dur(), flows:2}));
$("burstBtn").addEventListener("click", () => action({action:"burst", seconds:dur(), rate_mbps:3, on_seconds:4, off_seconds:3}));
$("netemBtn").addEventListener("click", () => action({action:"netem", delay_ms:Number($("nDelay").value||50), jitter_ms:Number($("nJitter").value||0), loss_pct:Number($("nLoss").value||0)}));
$("netemReset").addEventListener("click", () => { $("nDelay").value = 50; $("nJitter").value = 0; $("nLoss").value = 0; action({action:"netem_reset"}); });

$("expMode").addEventListener("change", () => {
  const single = $("expMode").value === "single";
  $("levelWrap").style.display = single ? "" : "none"; $("autoOpts").style.display = single ? "none" : "";
});
$("expStart").addEventListener("click", async () => {
  const d = await post("/api/experiment/start", {
    name:$("expName").value, mode:$("expMode").value, level:$("expLevel").value,
    stage_seconds:Number($("expStage").value || 40), include_tcp:$("expTcp").checked, include_burst:$("expBurst").checked});
  $("expMessage").textContent = d.message; $("expMessage").style.color = d.ok ? "#43d6a0" : "#ff8a9a";
  loadRuns(); refresh();
});
$("expStop").addEventListener("click", async () => { const d = await post("/api/experiment/stop"); $("expMessage").textContent = d.message; });

function fmtTime(t){ return t ? new Date(t * 1000).toLocaleString() : ""; }
async function loadRuns(){
  try {
    const runs = await (await fetch("/api/runs", {cache:"no-store"})).json();
    const body = document.querySelector("#runsTable tbody");
    if (!runs.length) { body.innerHTML = '<tr><td colspan="9" class="empty">No experiments yet.</td></tr>'; return; }
    body.innerHTML = runs.map(r => `<tr>
      <td>${r.id}</td><td>${r.name}</td><td>${fmtTime(r.started)}</td><td>${Math.round(r.duration_s)} s</td>
      <td class="st-${r.status}">${r.status}</td><td>${r.route_changes}</td>
      <td>${num(r.rtt_slow_ms,1)} → ${num(r.rtt_fast_ms,1)} ms${r.rtt_improvement_pct!=null ? ` (−${num(r.rtt_improvement_pct,0)}%)` : ""}</td>
      <td>${r.loss_pct==null ? "—" : num(r.loss_pct,1)+" %"}</td>
      <td><a href="/api/runs/${r.id}.csv">CSV</a><a href="/api/runs/${r.id}/report.pdf" target="_blank">PDF</a><a class="del" data-del="${r.id}">Delete</a></td></tr>`).join("");
    body.querySelectorAll("[data-del]").forEach(a => a.addEventListener("click", async () => {
      if (confirm("Delete this run and its data?")) { await post(`/api/runs/${a.dataset.del}/delete`); loadRuns(); }
    }));
  } catch (e) {}
}

refresh(); loadRuns();
setInterval(refresh, 2000); setInterval(loadRuns, 6000);
