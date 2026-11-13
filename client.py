"""Time-sync client (UDP).

Each exchange gives four timestamps:
    t1 client send, t2 server receive, t3 server send, t4 client receive

    round-trip delay   delta = (t4 - t1) - (t3 - t2)
    clock offset       theta = ((t2 - t1) + (t3 - t4)) / 2     (server - client)

theta is exact only if forward and return delays are equal; with asymmetric
delay the error is (d_forward - d_return) / 2.

Clock discipline: the client keeps a sliding window of recent samples and
steps its clock using the sample with the smallest delay (the same idea as
NTP's clock-filter algorithm - a low-delay sample is the least likely to have
been hit by queueing).
"""
import argparse
import socket
import time

from common import REQ, RESP, Clock, is_sync, mark_dscp, pack, unpack


def run_client(name, server_addr, skew, samples, interval, timeout, window,
               results, scenario="", stop=None, duration=None, on_probe=None,
               start_delay=0.0):
    """Run one client; append one dict per probe to `results`.

    skew: initial clock error in seconds. The server uses true time, so the
          true residual error of this client is simply abs(clock.offset).
    on_probe: optional callback called with each finished probe row (live streaming).
    start_delay: wait this long before the first probe (lets a viewer see the wrong clock).
    """
    clock = Clock(skew)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mark_dscp(sock)  # sync requests are marked EF
    best = []        # window of (delay, candidate_offset)

    if start_delay > 0:
        time.sleep(start_delay)
    started = time.time()
    seq = -1
    while True:
        seq += 1
        if duration is not None:                 # run for a fixed time...
            if time.time() - started >= duration:
                break
        elif seq >= samples:                     # ...or for a fixed number of probes
            break
        if stop is not None and stop.is_set():
            break
        t_wall = time.time()                     # real time of this probe (for plots)
        t1 = clock.now()
        sock.sendto(pack(REQ, seq, t1), server_addr)
        resp = None
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                continue
            t4 = clock.now()
            if not is_sync(data):
                continue
            kind, rseq, _, t2, t3 = unpack(data)
            if kind == RESP and rseq == seq:   # ignore stale replies
                resp = (t2, t3, t4)
                break

        row = {"scenario": scenario, "client": name, "seq": seq, "wall_time": t_wall,
               "initial_skew_ms": skew * 1000, "t1": t1,
               "offset_before_ms": clock.offset * 1000}      # signed error before this probe
        if resp is None:
            row.update(lost=1, delay_ms="", theta_ms="", raw_error_ms="",
                       t2="", t3="", t4="",
                       synced_error_ms=abs(clock.offset) * 1000,
                       error_signed_ms=clock.offset * 1000)
        else:
            t2, t3, t4 = resp
            delay = (t4 - t1) - (t3 - t2)
            theta = ((t2 - t1) + (t3 - t4)) / 2
            candidate = clock.offset + theta        # offset that would sync us
            best.append((delay, candidate))
            best = best[-window:]
            raw_error = abs(candidate)              # error if we trusted this sample alone
            clock.offset = min(best)[1]             # step to the min-delay sample
            row.update(lost=0, delay_ms=delay * 1000, theta_ms=theta * 1000,
                       raw_error_ms=raw_error * 1000, t2=t2, t3=t3, t4=t4,
                       synced_error_ms=abs(clock.offset) * 1000,
                       error_signed_ms=clock.offset * 1000)
        results.append(row)
        if on_probe is not None:
            on_probe(row)
        time.sleep(interval)
    sock.close()
    return clock


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="UDP time-sync client")
    ap.add_argument("--server", default="127.0.0.1:5000")
    ap.add_argument("--skew", type=float, default=1.0, help="initial clock error (s)")
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--window", type=int, default=8)
    ap.add_argument("--duration", type=float, default=None,
                    help="run for this many seconds instead of a fixed number of probes")
    ap.add_argument("--name", default="cli", help="client name written to the CSV")
    ap.add_argument("--csv", default=None, help="also write every probe to this CSV file")
    ap.add_argument("--jsonl", action="store_true",
                    help="print each probe as one JSON line (used by the dashboard) instead of the table")
    ap.add_argument("--start-delay", type=float, default=0.0,
                    help="wait this many seconds before the first probe")
    a = ap.parse_args()
    host, port = a.server.split(":")
    out = []
    emit = None
    if a.jsonl:
        import json

        def emit(row):
            print(json.dumps(row), flush=True)
    run_client(a.name, (host, int(port)), a.skew, a.samples, a.interval,
               a.timeout, a.window, out, duration=a.duration, on_probe=emit,
               start_delay=a.start_delay)
    if a.csv and out:
        import csv
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
            w.writeheader()
            w.writerows(out)
    if a.jsonl:
        raise SystemExit(0)
    print(f"{'seq':>3} {'delay(ms)':>10} {'offset(ms)':>11} {'raw err(ms)':>12} {'synced err(ms)':>15}")
    for r in out:
        if r["lost"]:
            print(f"{r['seq']:>3}   (lost)")
        else:
            print(f"{r['seq']:>3} {r['delay_ms']:>10.3f} {r['theta_ms']:>11.3f} "
                  f"{r['raw_error_ms']:>12.3f} {r['synced_error_ms']:>15.3f}")
