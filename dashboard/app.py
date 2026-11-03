#!/usr/bin/env python3
"""Smart SDN dashboard backend (Flask)."""
import re
import subprocess
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

import reports
from collector import Collector
from experiments import ExperimentRunner
from mnctl import control
from store import Store

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
app = Flask(__name__)
store = Store(ROOT / "results" / "experiments.db")
collector = Collector(ROOT)
runner = ExperimentRunner(store, collector, control)
ACTIONS = {"scenario", "stop", "sync", "stopsync", "tcp", "burst", "netem", "netem_reset"}


def run_cmd(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=2)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def port_stats(bridge):
    """Parse `ovs-ofctl dump-ports` (OpenFlow 1.3), e.g.
      port "s1-eth5": rx pkts=10, bytes=840, drop=0, ...
                      tx pkts=5, bytes=410, drop=0, ...
    """
    output = run_cmd(["sudo", "-n", "ovs-ofctl", "-O", "OpenFlow13", "dump-ports", bridge])
    ports, current = [], None
    for line in output.splitlines():
        m = re.match(r'\s*port\s+("[^"]+"|\S+?):', line)
        if m:
            current = {"name": m.group(1).strip('"'), "rx_bytes": 0, "tx_bytes": 0,
                       "rx_packets": 0, "tx_packets": 0}
            ports.append(current)
        if current is not None:
            for direction in ("rx", "tx"):
                mm = re.search(rf'\b{direction} pkts=(\d+), bytes=(\d+)', line)
                if mm:
                    current[f"{direction}_packets"] = int(mm.group(1))
                    current[f"{direction}_bytes"] = int(mm.group(2))
    return ports


def tail(path, lines=35):
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except Exception:
        return ""


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    state = collector.read_status() or {
        "policy": "unknown", "path": "UNKNOWN", "slow_link_mbps": 0,
        "high_threshold_mbps": 2.5, "low_threshold_mbps": 0.8,
        "connected_switches": [], "updated_at": "Waiting for controller telemetry"}
    state["switches"] = {"s1": port_stats("s1"), "s2": port_stats("s2")}
    state["controller_log"] = tail(LOG_DIR / "controller.log")
    state["latest"] = collector.latest
    state["experiment"] = runner.get_state()
    state["server_time"] = time.strftime("%H:%M:%S")
    return jsonify(state)


@app.get("/api/series")
def api_series():
    return jsonify(list(collector.series)[-90:])


@app.post("/api/action")
def api_action():
    data = request.get_json(silent=True) or {}
    if data.get("action") not in ACTIONS:
        return jsonify({"ok": False, "message": "Unsupported action"}), 400
    result = control(data)
    return jsonify(result), (200 if result.get("ok") else 503)


@app.post("/api/experiment/start")
def api_experiment_start():
    result = runner.start(request.get_json(silent=True) or {})
    return jsonify(result), (200 if result.get("ok") else 400)


@app.post("/api/experiment/stop")
def api_experiment_stop():
    return jsonify(runner.stop())


@app.get("/api/runs")
def api_runs():
    return jsonify([{k: v for k, v in store.summary(r["id"]).items() if k not in ("phases", "events", "config")}
                    for r in store.list_runs()])


@app.get("/api/runs/<int:run_id>/summary")
def api_run_summary(run_id):
    s = store.summary(run_id)
    return (jsonify(s), 200) if s else (jsonify({"ok": False, "message": "Run not found"}), 404)


@app.get("/api/runs/<int:run_id>.csv")
def api_run_csv(run_id):
    if not store.get_run(run_id):
        return jsonify({"ok": False, "message": "Run not found"}), 404
    return Response(reports.run_csv(store, run_id), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=run-{run_id}.csv"})


@app.get("/api/runs/<int:run_id>/report.pdf")
def api_run_pdf(run_id):
    if not store.get_run(run_id):
        return jsonify({"ok": False, "message": "Run not found"}), 404
    high = (collector.read_status() or {}).get("high_threshold_mbps", 2.5)
    return Response(reports.run_pdf(store, run_id, high), mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=smart-sdn-run-{run_id}.pdf"})


@app.get("/api/runs.csv")
def api_runs_csv():
    return Response(reports.runs_csv(store), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=smart-sdn-runs.csv"})


@app.post("/api/runs/<int:run_id>/delete")
def api_run_delete(run_id):
    st = runner.get_state()
    if st.get("active") and st.get("run_id") == run_id:
        return jsonify({"ok": False, "message": "Cannot delete the running experiment"}), 409
    store.delete_run(run_id)
    return jsonify({"ok": True})


if __name__ == "__main__":
    collector.start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
