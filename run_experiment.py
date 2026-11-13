"""Run the three experiments and compare synchronization accuracy.

  1. baseline             - no background traffic, FIFO link
  2. congestion + FIFO    - background traffic overloads the link, no priority
  3. congestion + priority- same load, sync packets get strict priority

Outputs (in ./results): raw_samples.csv, summary.csv, comparison.png
"""
import argparse
import csv
import os
import random
import statistics as st
import threading
import time

from bg_traffic import run_bg
from client import run_client
from emulator import Emulator
from server import run_server

SCENARIOS = [
    ("baseline (no congestion)", "fifo", False),
    ("congestion + FIFO", "fifo", True),
    ("congestion + priority", "priority", True),
]


def run_scenario(label, mode, congested, idx, a, skews):
    server_port, emu_port = a.base_port + 2 * idx, a.base_port + 2 * idx + 1
    stop, ready = threading.Event(), threading.Event()
    results, server_stats = [], {}

    threading.Thread(target=run_server, args=(server_port, stop, "127.0.0.1", ready),
                     daemon=True).start()
    ready.wait(2)
    emu = Emulator(emu_port, ("127.0.0.1", server_port), rate_bps=a.rate,
                   base_delay=a.delay / 1000, jitter=a.jitter / 1000,
                   queue_bytes=a.queue, mode=mode, seed=idx)
    emu.start()

    bg = None
    if congested:
        bg = threading.Thread(target=run_bg, args=(("127.0.0.1", emu_port), a.bg_pps, a.bg_size, stop),
                              daemon=True)
        bg.start()
        time.sleep(1.5)  # let the standing queue build up before probing

    clients = []
    for i, skew in enumerate(skews):
        t = threading.Thread(target=run_client, daemon=True,
                             args=(f"C{i+1}", ("127.0.0.1", emu_port), skew, a.samples,
                                   a.interval, a.timeout, a.window, results, label))
        clients.append(t)
        t.start()
        time.sleep(0.03)  # stagger clients slightly
    for t in clients:
        t.join()

    stop.set()
    time.sleep(0.3)
    emu.stop()
    return results, dict(emu.stats)


def summarize(label, rows, emu_stats, n_clients):
    sent = len(rows)
    ok = [r for r in rows if not r["lost"]]
    delays = [r["delay_ms"] for r in ok]
    raw = [r["raw_error_ms"] for r in ok]
    # error after the client has had time to converge: last 5 probes of every client
    final = []
    for c in sorted({r["client"] for r in rows}):
        final += [r["synced_error_ms"] for r in rows if r["client"] == c][-5:]
    p95 = lambda xs: sorted(xs)[int(0.95 * (len(xs) - 1))] if xs else float("nan")
    return {
        "scenario": label, "clients": n_clients, "probes": sent,
        "loss_%": round(100 * (sent - len(ok)) / sent, 1),
        "mean_delay_ms": round(st.mean(delays), 2) if delays else float("nan"),
        "mean_raw_err_ms": round(st.mean(raw), 2) if raw else float("nan"),
        "p95_raw_err_ms": round(p95(raw), 2),
        "max_raw_err_ms": round(max(raw), 2) if raw else float("nan"),
        "mean_synced_err_ms": round(st.mean(final), 2),
        "max_synced_err_ms": round(max(final), 2),
        "bg_dropped": emu_stats["bg_dropped"], "sync_dropped": emu_stats["sync_dropped"],
    }


def make_plot(all_rows, summaries, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not installed - skipping plot)")
        return
    labels = [s["scenario"] for s in summaries]
    fig, ax = plt.subplots(1, 3, figsize=(16, 4.5))
    ax[0].boxplot([[r["delay_ms"] for r in all_rows[l] if not r["lost"]] for l in labels],
                  showfliers=False)
    ax[0].set_xticklabels([l.replace(" + ", "\n+ ").replace(" (", "\n(") for l in labels], fontsize=8)
    ax[0].set_ylabel("round-trip delay (ms)"); ax[0].set_title("Measured RTT")
    ax[1].boxplot([[r["raw_error_ms"] for r in all_rows[l] if not r["lost"]] for l in labels],
                  showfliers=False)
    ax[1].set_xticklabels([l.replace(" + ", "\n+ ").replace(" (", "\n(") for l in labels], fontsize=8)
    ax[1].set_ylabel("|offset error| per sample (ms)"); ax[1].set_title("Raw sync error")
    for l in labels:
        rows = all_rows[l]
        n = max(r["seq"] for r in rows) + 1
        mean_by_seq = [st.mean(r["synced_error_ms"] for r in rows if r["seq"] == s) for s in range(n)]
        ax[2].plot(range(n), mean_by_seq, marker="o", ms=3, label=l)
    ax[2].set_yscale("log"); ax[2].set_xlabel("probe number")
    ax[2].set_ylabel("mean clock error after filter (ms)")
    ax[2].set_title("Convergence"); ax[2].legend(fontsize=8)
    fig.tight_layout(); fig.savefig(path, dpi=130)
    print("plot saved:", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=5)
    ap.add_argument("--samples", type=int, default=30)
    ap.add_argument("--interval", type=float, default=0.2, help="s between probes")
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--window", type=int, default=8, help="clock-filter window")
    ap.add_argument("--rate", type=float, default=1_000_000, help="bottleneck bit/s")
    ap.add_argument("--delay", type=float, default=20, help="one-way base delay ms")
    ap.add_argument("--jitter", type=float, default=5, help="max jitter ms")
    ap.add_argument("--queue", type=int, default=50_000, help="bottleneck buffer bytes")
    ap.add_argument("--bg-pps", type=float, default=200, help="background packets/s")
    ap.add_argument("--bg-size", type=int, default=1000, help="background payload bytes")
    ap.add_argument("--base-port", type=int, default=7000)
    ap.add_argument("--out", default="results")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    rng = random.Random(42)
    skews = [rng.uniform(-3, 3) for _ in range(a.clients)]
    print("initial clock errors (s):", [round(s, 3) for s in skews])

    all_rows, summaries = {}, []
    for i, (label, mode, congested) in enumerate(SCENARIOS):
        print(f"\n>>> running: {label} ...")
        rows, emu_stats = run_scenario(label, mode, congested, i, a, skews)
        all_rows[label] = rows
        summaries.append(summarize(label, rows, emu_stats, a.clients))

    with open(os.path.join(a.out, "raw_samples.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(next(iter(all_rows.values()))[0].keys()))
        w.writeheader()
        for rows in all_rows.values():
            w.writerows(rows)
    with open(os.path.join(a.out, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summaries[0].keys()))
        w.writeheader(); w.writerows(summaries)

    cols = ["scenario", "loss_%", "mean_delay_ms", "mean_raw_err_ms", "max_raw_err_ms",
            "mean_synced_err_ms", "max_synced_err_ms"]
    print("\n" + " | ".join(f"{c:>22}" if c != "scenario" else f"{c:<26}" for c in cols))
    for s in summaries:
        print(" | ".join(f"{s[c]:>22}" if c != "scenario" else f"{s[c]:<26}" for c in cols))
    make_plot(all_rows, summaries, os.path.join(a.out, "comparison.png"))


if __name__ == "__main__":
    main()
