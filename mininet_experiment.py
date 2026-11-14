#!/usr/bin/env python3
"""Run the policy x congestion experiment matrix on Mininet + Open vSwitch + Ryu.

Needs root and a running Ryu controller (start it in another terminal with
scripts/start_controller.sh), or pass --ryu-manager PATH to have this script start it.

    sudo /usr/bin/python3 mininet_experiment.py                 # all 24 runs, ~10 min
    sudo /usr/bin/python3 mininet_experiment.py --scenarios extreme,burst --policies static,dynamic+prio

Policies
    static        sync stays on the SLOW path; one shared FIFO queue
    dynamic       Ryu moves sync to the FAST path when the SLOW link is busy
    static+prio   SLOW path, but sync (DSCP EF) has a strict-priority queue
    dynamic+prio  both
Scenarios: baseline, low, moderate, high, extreme, burst.

Outputs (results_mininet/): summary.csv, raw_samples.csv, route_events.csv,
controller_samples.csv, overview.png, timeline.png, final_report.pdf
"""
import argparse
import csv
import json
import os
import random
import subprocess
import sys
import time
from argparse import Namespace

from mininet.log import setLogLevel

import sdn_config as C
from mininet_topology import build_network, disable_offloads, setup_qos, tc_drops
from run_sdn_experiment import ALL_SCENARIOS, POLICIES, make_plots, pname, summarize, write_csv
from traffic import LEVELS, PAYLOAD, pps

PY = sys.executable
DEVNULL = subprocess.DEVNULL


def set_control(policy, token):
    """Tell the controller to switch policy and reset the route (picked up within ~0.4 s)."""
    os.makedirs(C.LOG_DIR, exist_ok=True)
    tmp = C.CONTROL_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"policy": policy, "token": token}, f)
    os.replace(tmp, C.CONTROL_FILE)


def read_csv(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def read_tx_bytes(dev):
    with open(f"/sys/class/net/{dev}/statistics/tx_bytes") as f:
        return int(f.read())


def num(v):
    return float(v) if v not in ("", None) else ""


def run_one(net, policy, prio, scenario, idx, a, skews, tmpdir):
    set_control(policy, f"{time.time()}-{idx}")
    time.sleep(1.2)                                   # controller applies the reset
    setup_qos(net, priority=prio, netem=not a.no_netem, queue_pkts=a.queue_pkts)
    time.sleep(1.0)                                   # fresh queues, settled flows
    dev = net.get("s1").intfs[C.S1_SLOW_PORT].name
    tx0, drops0 = read_tx_bytes(dev), tc_drops(net)

    t_start = time.time()
    bg = []
    if scenario != "baseline":
        level = "high" if scenario == "burst" else scenario
        rate = pps(LEVELS[level])
        extra = ["--on", str(a.burst_on), "--off", str(a.burst_off)] if scenario == "burst" else []
        for h in C.BG_HOSTS:
            bg.append(net.get(h).popen(
                [PY, "bg_traffic.py", "--target", f"{C.SERVER_IP}:{C.BG_PORT}", "--rate", str(rate),
                 "--size", str(PAYLOAD)] + extra, stdout=DEVNULL, stderr=DEVNULL))
    clients = []
    for i, (h, skew) in enumerate(zip(C.CLIENT_HOSTS[:a.clients], skews)):
        clients.append(net.get(h).popen(
            [PY, "client.py", "--server", f"{C.SERVER_IP}:{C.SYNC_PORT}", "--skew", str(skew),
             "--duration", str(a.duration), "--interval", str(a.interval), "--timeout", str(a.timeout),
             "--window", str(a.window), "--name", f"C{i+1}", "--csv", os.path.join(tmpdir, f"C{i+1}.csv")],
            stdout=DEVNULL, stderr=DEVNULL))
    for p in clients:
        p.wait(timeout=a.duration + 20)
    t_end = time.time()
    for p in bg:
        p.terminate()
    for p in bg:
        p.wait()
    tx1, drops1 = read_tx_bytes(dev), tc_drops(net)

    label = pname(policy, prio)
    rows = []
    for i in range(a.clients):
        for r in read_csv(os.path.join(tmpdir, f"C{i+1}.csv")):
            row = {"policy": label, "scenario": scenario, "client": r["client"], "seq": int(r["seq"]),
                   "t": round(float(r["wall_time"]) - t_start, 3), "initial_skew_ms": num(r["initial_skew_ms"]),
                   "lost": int(r["lost"]), "delay_ms": num(r["delay_ms"]), "theta_ms": num(r["theta_ms"]),
                   "raw_error_ms": num(r["raw_error_ms"]), "synced_error_ms": float(r["synced_error_ms"])}
            rows.append(row)
    events = [{"t": float(e["t"]) - t_start, "path": e["path"], "rate_mbps": float(e["rate_mbps"])}
              for e in read_csv(C.ROUTE_EVENTS_CSV) if t_start <= float(e["t"]) <= t_end]
    samples = [{"t": float(s["t"]) - t_start, "slow_mbps": float(s["slow_mbps"]), "path": s["path"]}
               for s in read_csv(C.SAMPLES_CSV) if t_start <= float(s["t"]) <= t_end]
    lost = sum(r["lost"] for r in rows)
    snap = {"route_events": events, "samples": samples, "slow_tx_bytes": tx1 - tx0,
            "slow": {"bg_drop": drops1 - drops0, "sync_drop": lost},   # qdisc drops / lost probes
            "fast": {"sync_drop": 0}}
    return rows, snap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=16, help="seconds per run")
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--window", type=int, default=8)
    ap.add_argument("--clients", type=int, default=3, choices=[1, 2, 3])
    ap.add_argument("--queue-pkts", type=int, default=C.QUEUE_PKTS)
    ap.add_argument("--burst-on", type=float, default=4.0)
    ap.add_argument("--burst-off", type=float, default=3.0)
    ap.add_argument("--steady-from", type=float, default=6.0)
    ap.add_argument("--policies", default="static,dynamic,static+prio,dynamic+prio")
    ap.add_argument("--scenarios", default=",".join(ALL_SCENARIOS))
    ap.add_argument("--controller", default="127.0.0.1:6653")
    ap.add_argument("--ryu-manager", default=None, help="path to ryu-manager: start the controller from here")
    ap.add_argument("--datapath", choices=["kernel", "user"], default="kernel",
                    help="OVS datapath (use 'user' if the kernel has no openvswitch module)")
    ap.add_argument("--no-netem", action="store_true", help="skip tc netem delay (kernel without sch_netem)")
    ap.add_argument("--out", default="results_mininet")
    ap.add_argument("--no-report", action="store_true", help="do not build final_report.pdf at the end")
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root (sudo): Mininet needs it")

    os.chdir(C.HERE)
    os.makedirs(a.out, exist_ok=True)
    tmpdir = os.path.join(a.out, "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    setLogLevel("warning")

    ryu = None
    if a.ryu_manager:
        env = dict(os.environ, PYTHONPATH=C.HERE + os.pathsep + os.environ.get("PYTHONPATH", ""))
        ryu = subprocess.Popen([a.ryu_manager, "--ofp-tcp-listen-port", a.controller.split(":")[1],
                                "ryu_controller.py"], cwd=C.HERE, env=env,
                               stdout=open(os.path.join(a.out, "ryu.log"), "w"), stderr=subprocess.STDOUT)
        time.sleep(4)

    ip, port = a.controller.split(":")
    net = build_network(ip, int(port), a.datapath)
    net.start()
    servers = []
    try:
        net.staticArp()
        disable_offloads(net)
        if not net.waitConnected(timeout=30):
            sys.exit("switches did not connect to the controller - is Ryu running on " + a.controller + "?")
        time.sleep(1.5)                                  # let the controller install flows
        setup_qos(net, priority=False, netem=not a.no_netem, queue_pkts=a.queue_pkts)
        loss = net.ping([net.get("h1"), net.get("h4")], timeout="3")
        if loss > 0:
            sys.exit(f"connectivity check failed ({loss}% loss): see {a.out}/ryu.log")
        h4 = net.get("h4")
        servers = [h4.popen([PY, "server.py", "--host", C.SERVER_IP, "--port", str(C.SYNC_PORT)],
                            stdout=DEVNULL, stderr=DEVNULL),
                   h4.popen([PY, "bg_sink.py", "--port", str(C.BG_PORT)], stdout=DEVNULL, stderr=DEVNULL)]
        time.sleep(1.0)

        rng = random.Random(42)
        skews = [rng.uniform(-3, 3) for _ in range(a.clients)]
        want_p, want_s = a.policies.split(","), a.scenarios.split(",")
        todo = [(p, pr, s) for s in want_s for (p, pr) in POLICIES if pname(p, pr) in want_p]
        print(f"{len(todo)} runs x {a.duration:g}s on Mininet/OVS/Ryu; clock errors (s): "
              f"{[round(s, 2) for s in skews]}")
        sa = Namespace(duration=a.duration, steady_from=a.steady_from, high=C.HIGH_MBPS, low=C.LOW_MBPS)

        summaries, all_rows, events, ctrl, data = [], [], [], [], {}
        for i, (policy, prio, scen) in enumerate(todo):
            rows, snap = run_one(net, policy, prio, scen, i, a, skews, tmpdir)
            label = pname(policy, prio)
            if not rows:
                print(f"[{i+1}/{len(todo)}] {scen} {label}: no client output - skipped")
                continue
            data[(label, scen)] = {"rows": rows, "snap": snap}
            s = summarize(label, scen, rows, snap, sa)
            summaries.append(s)
            all_rows += rows
            events += [{"policy": label, "scenario": scen, **e} for e in snap["route_events"]]
            ctrl += [{"policy": label, "scenario": scen, **x} for x in snap["samples"]]
            print(f"[{i+1}/{len(todo)}] {scen:<9} {label:<13} rtt={s['mean_rtt_ms']:>7} ms  "
                  f"steady err={s['steady_synced_err_ms']:>6} ms  loss={s['loss_%']}%  "
                  f"reroutes={s['route_changes']}", flush=True)

        write_csv(os.path.join(a.out, "summary.csv"), summaries)
        write_csv(os.path.join(a.out, "raw_samples.csv"), all_rows)
        write_csv(os.path.join(a.out, "route_events.csv"), events)
        write_csv(os.path.join(a.out, "controller_samples.csv"), ctrl)
        make_plots(summaries, data, sa, a.out)
        if not a.no_report and summaries:
            try:
                from report import build_report
                pdf = build_report(a.out, info={
                    "Run length": f"{a.duration:g} s per run, probe every {a.interval:g} s",
                    "Network": f"OVS datapath: {a.datapath}; link delay (netem): {'off' if a.no_netem else 'on'}",
                    "Initial clock errors": ", ".join(f"{k:+.2f} s" for k in skews)},
                    high=C.HIGH_MBPS, low=C.LOW_MBPS)
                print("final report ->", pdf)
            except Exception as e:                      # never lose the CSVs because of the report
                print("(final report skipped:", e, ")")
        print("\ndone ->", a.out)
    finally:
        for p in servers:
            p.terminate()
        net.stop()
        if ryu:
            ryu.terminate()


if __name__ == "__main__":
    main()
