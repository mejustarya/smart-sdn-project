"""Policy x congestion experiment matrix.

Policies (how sync traffic is protected):
    static        sync always on SLOW path, shared FIFO queue      (SDN project, static mode)
    dynamic       controller moves sync to FAST above 2.5 Mbps      (SDN project, dynamic mode)
    static+prio   always SLOW, but sync has a strict-priority queue (first project's idea)
    dynamic+prio  reroute AND priority queue                        (combined)

Scenarios: baseline, low, moderate, high, extreme, burst.

Outputs in ./results_sdn: summary.csv, raw_samples.csv, route_events.csv,
controller_samples.csv, overview.png, timeline.png
"""
import argparse
import csv
import os
import random
import statistics as st
import threading
import time

from client import run_client
from sdn_emulator import SdnNetwork
from server import run_server
from traffic import start_generators

POLICIES = [("static", False), ("dynamic", False), ("static", True), ("dynamic", True)]
ALL_SCENARIOS = ["baseline", "low", "moderate", "high", "extreme", "burst"]
COLORS = {"static": "#7f7f7f", "dynamic": "#1f77b4", "static+prio": "#ff7f0e", "dynamic+prio": "#2ca02c"}


def pname(policy, prio):
    return policy + ("+prio" if prio else "")


def run_one(policy, prio, scenario, idx, a, skews):
    sport, nport = a.base_port + 2 * idx, a.base_port + 2 * idx + 1
    stop, ready = threading.Event(), threading.Event()
    threading.Thread(target=run_server, args=(sport, stop, "127.0.0.1", ready), daemon=True).start()
    ready.wait(2)
    net = SdnNetwork(nport, ("127.0.0.1", sport), policy=policy, priority=prio,
                     high_mbps=a.high, low_mbps=a.low, hold_down=a.hold_down,
                     monitor_interval=a.monitor, queue_bytes=a.queue,
                     slow_loss=a.slow_loss, slow_extra_delay=a.slow_extra_delay / 1000, seed=idx)
    net.start()
    if scenario != "baseline":
        level = "high" if scenario == "burst" else scenario
        start_generators(level, ("127.0.0.1", nport), stop, burst=(scenario == "burst"),
                         on=a.burst_on, off=a.burst_off)
    rows = []
    threads = [threading.Thread(target=run_client, daemon=True,
                                args=(f"C{i+1}", ("127.0.0.1", nport), sk, None, a.interval,
                                      a.timeout, a.window, rows, ""),
                                kwargs={"duration": a.duration})
               for i, sk in enumerate(skews)]
    for t in threads:
        t.start()
        time.sleep(0.02)
    for t in threads:
        t.join()
    snap = net.snapshot()
    t0 = net.t0
    stop.set()
    time.sleep(0.2)
    net.stop()
    for r in rows:
        r["policy"], r["scenario"] = pname(policy, prio), scenario
        r["t"] = round(r.pop("wall_time") - t0, 3)
    return rows, snap


def fast_intervals(events, end):
    out, start = [], None
    for e in events:
        if e["path"] == "FAST" and start is None:
            start = e["t"]
        elif e["path"] == "SLOW" and start is not None:
            out.append((start, e["t"]))
            start = None
    if start is not None:
        out.append((start, end))
    return out


def p95(xs):
    return sorted(xs)[int(0.95 * (len(xs) - 1))] if xs else float("nan")


def summarize(label, scenario, rows, snap, a):
    ok = [r for r in rows if not r["lost"]]
    delays = [r["delay_ms"] for r in ok]
    jit = []
    for c in sorted({r["client"] for r in ok}):
        d = [r["delay_ms"] for r in sorted((x for x in ok if x["client"] == c), key=lambda x: x["t"])]
        jit += [abs(b - x) for x, b in zip(d, d[1:])]
    steady = [r["synced_error_ms"] for r in rows if r["t"] >= a.steady_from]
    ev = snap["route_events"]
    r2 = lambda v: round(v, 2) if v == v else v
    return {
        "policy": label, "scenario": scenario, "probes": len(rows),
        "loss_%": round(100 * (len(rows) - len(ok)) / max(len(rows), 1), 1),
        "mean_rtt_ms": r2(st.mean(delays)) if delays else float("nan"),
        "p95_rtt_ms": r2(p95(delays)),
        "jitter_ms": r2(st.mean(jit)) if jit else float("nan"),
        "mean_raw_err_ms": r2(st.mean(r["raw_error_ms"] for r in ok)) if ok else float("nan"),
        "mean_synced_err_ms": r2(st.mean(r["synced_error_ms"] for r in rows)),
        "steady_synced_err_ms": r2(st.mean(steady)) if steady else float("nan"),
        "max_synced_err_ms": r2(max(r["synced_error_ms"] for r in rows)),
        "route_changes": len(ev),
        "first_reroute_s": round(ev[0]["t"], 1) if ev else "",
        "time_on_fast_s": round(sum(e - s for s, e in fast_intervals(ev, a.duration)), 1),
        "slow_link_mbps": round(snap["slow_tx_bytes"] * 8 / a.duration / 1e6, 2),
        "bg_dropped": snap["slow"]["bg_drop"], "sync_dropped": snap["slow"]["sync_drop"] + snap["fast"]["sync_drop"],
    }


def make_plots(summaries, data, a, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("(matplotlib missing - skipping plots)")
        return
    pols = [p for p in COLORS if any(s["policy"] == p for s in summaries)]
    scens = [s for s in ALL_SCENARIOS if any(x["scenario"] == s for x in summaries)]
    get = lambda p, s, k: next((x[k] for x in summaries if x["policy"] == p and x["scenario"] == s), np.nan)

    fig, ax = plt.subplots(1, 2, figsize=(14, 4.6))
    w = 0.8 / max(len(pols), 1)
    for ai, (key, title, ylab, log) in enumerate([
            ("steady_synced_err_ms", f"Clock error after sync (t >= {a.steady_from:g}s)", "error (ms, log scale)", True),
            ("mean_rtt_ms", "Mean round-trip delay", "RTT (ms)", False)]):
        for i, p in enumerate(pols):
            ax[ai].bar(np.arange(len(scens)) + i * w, [max(get(p, s, key), 0.01) if log else get(p, s, key) for s in scens],
                       w, label=p, color=COLORS[p])
        ax[ai].set_xticks(np.arange(len(scens)) + w * (len(pols) - 1) / 2)
        ax[ai].set_xticklabels(scens)
        ax[ai].set_title(title); ax[ai].set_ylabel(ylab)
        if log:
            ax[ai].set_yscale("log")
        ax[ai].spines[["top", "right"]].set_visible(False)
    ax[0].legend(fontsize=8, frameon=False)
    fig.tight_layout(); fig.savefig(os.path.join(out, "overview.png"), dpi=130); plt.close(fig)

    tl = [s for s in ("extreme", "burst") if ("dynamic", s) in data]
    if not tl:
        return
    fig, ax = plt.subplots(2, len(tl), figsize=(7 * len(tl), 6.2), squeeze=False, sharex=True)
    for j, s in enumerate(tl):
        d = data[("dynamic", s)]
        # a controller sample at time t is the average rate over the interval BEFORE t
        smp = d["snap"]["samples"]
        ts = [0] + [x["t"] for x in smp]
        rs = [smp[0]["slow_mbps"]] + [x["slow_mbps"] for x in smp]
        ax[0][j].step(ts, rs, where="pre", color="#1f77b4", label="slow-link rate")
        ax[0][j].set_ylim(0, a.high * 2.6)
        ax[0][j].axhline(a.high, color="#d62728", ls="--", lw=1, label=f"high threshold {a.high:g}")
        ax[0][j].axhline(a.low, color="#2ca02c", ls="--", lw=1, label=f"low threshold {a.low:g}")
        for k, (x0, x1) in enumerate(fast_intervals(d["snap"]["route_events"], a.duration)):
            ax[0][j].axvspan(x0, x1, color="#1f77b4", alpha=0.12, label="sync on FAST path" if k == 0 else None)
        ax[0][j].set_title(f"{s}: controller view (dynamic policy)"); ax[0][j].set_ylabel("Mbps")
        ax[0][j].legend(fontsize=7, frameon=False, loc="upper right")
        for p in ("static", "dynamic"):
            if (p, s) in data:
                rr = [r for r in data[(p, s)]["rows"] if not r["lost"]]
                ax[1][j].scatter([r["t"] for r in rr], [r["delay_ms"] for r in rr], s=7, alpha=0.6,
                                 color=COLORS[p], label=p)
        for x0, x1 in fast_intervals(d["snap"]["route_events"], a.duration):
            ax[1][j].axvspan(x0, x1, color="#1f77b4", alpha=0.12)
        ax[1][j].set_xlabel("time (s)"); ax[1][j].set_ylabel("measured RTT (ms)")
        ax[1][j].legend(fontsize=7, frameon=False, markerscale=2)
        for r in (0, 1):
            ax[r][j].spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(os.path.join(out, "timeline.png"), dpi=130); plt.close(fig)


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)


def replot(a):
    """Rebuild the plots from the CSVs of an earlier run (no experiments are re-run)."""
    def load(name):
        with open(os.path.join(a.out, name), newline="") as f:
            return list(csv.DictReader(f))
    num = lambda v: float(v) if v not in ("", None) else float("nan")
    summaries = load("summary.csv")
    for s in summaries:
        for k in ("steady_synced_err_ms", "mean_rtt_ms"):
            s[k] = num(s[k])
    data = {}
    for s in summaries:
        data[(s["policy"], s["scenario"])] = {"rows": [], "snap": {"samples": [], "route_events": []}}
    for r in load("raw_samples.csv"):
        data[(r["policy"], r["scenario"])]["rows"].append(
            {"t": num(r["t"]), "lost": int(r["lost"]), "delay_ms": num(r["delay_ms"])})
    for e in load("route_events.csv"):
        data[(e["policy"], e["scenario"])]["snap"]["route_events"].append({"t": num(e["t"]), "path": e["path"]})
    for c in load("controller_samples.csv"):
        data[(c["policy"], c["scenario"])]["snap"]["samples"].append(
            {"t": num(c["t"]), "slow_mbps": num(c["slow_mbps"])})
    make_plots(summaries, data, a, a.out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replot", action="store_true", help="rebuild plots from existing CSVs")
    ap.add_argument("--clients", type=int, default=3)
    ap.add_argument("--duration", type=float, default=16, help="seconds per run")
    ap.add_argument("--interval", type=float, default=0.2, help="gap between probes (s)")
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--window", type=int, default=8)
    ap.add_argument("--high", type=float, default=2.5, help="high threshold Mbps")
    ap.add_argument("--low", type=float, default=0.8, help="low threshold Mbps")
    ap.add_argument("--hold-down", type=float, default=4.0)
    ap.add_argument("--monitor", type=float, default=2.0, help="controller interval (s)")
    ap.add_argument("--queue", type=int, default=60_000, help="per-link buffer (bytes)")
    ap.add_argument("--burst-on", type=float, default=4.0)
    ap.add_argument("--burst-off", type=float, default=3.0)
    ap.add_argument("--steady-from", type=float, default=6.0, help="ignore first N s in 'steady' error")
    ap.add_argument("--slow-loss", type=float, default=0.0, help="extra loss prob on SLOW path")
    ap.add_argument("--slow-extra-delay", type=float, default=0.0, help="extra ms on SLOW path")
    ap.add_argument("--policies", default="static,dynamic,static+prio,dynamic+prio")
    ap.add_argument("--scenarios", default=",".join(ALL_SCENARIOS))
    ap.add_argument("--base-port", type=int, default=7100)
    ap.add_argument("--out", default="results_sdn")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    if a.replot:
        replot(a)
        print("plots rebuilt in", a.out)
        return
    rng = random.Random(42)
    skews = [rng.uniform(-3, 3) for _ in range(a.clients)]
    want_p = a.policies.split(",")
    want_s = a.scenarios.split(",")
    todo = [(p, pr, s) for s in want_s for (p, pr) in POLICIES if pname(p, pr) in want_p]
    print(f"{len(todo)} runs x {a.duration:g}s; initial clock errors (s): {[round(s, 2) for s in skews]}")

    summaries, all_rows, events, ctrl, data = [], [], [], [], {}
    for i, (policy, prio, scen) in enumerate(todo):
        label = pname(policy, prio)
        rows, snap = run_one(policy, prio, scen, i, a, skews)
        data[(label, scen)] = {"rows": rows, "snap": snap}
        s = summarize(label, scen, rows, snap, a)
        summaries.append(s); all_rows += rows
        events += [{"policy": label, "scenario": scen, **e} for e in snap["route_events"]]
        ctrl += [{"policy": label, "scenario": scen, **x} for x in snap["samples"]]
        print(f"[{i+1}/{len(todo)}] {scen:<9} {label:<13} rtt={s['mean_rtt_ms']:>7} ms  "
              f"steady err={s['steady_synced_err_ms']:>6} ms  loss={s['loss_%']}%  "
              f"reroutes={s['route_changes']}", flush=True)

    write_csv(os.path.join(a.out, "summary.csv"), summaries)
    write_csv(os.path.join(a.out, "raw_samples.csv"), all_rows)
    write_csv(os.path.join(a.out, "route_events.csv"), events)
    write_csv(os.path.join(a.out, "controller_samples.csv"), ctrl)
    make_plots(summaries, data, a, a.out)
    print("\ndone ->", a.out)


if __name__ == "__main__":
    main()
