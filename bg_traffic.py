"""Background (cross) traffic generator: constant-rate UDP packets used to
congest the emulator's bottleneck link."""
import argparse
import socket
import threading
import time

from common import MAGIC_BG


def run_bg(target, rate_pps, size, stop):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = MAGIC_BG + bytes(size - len(MAGIC_BG))
    start, sent = time.perf_counter(), 0
    while not stop.is_set():
        due = int((time.perf_counter() - start) * rate_pps) - sent
        for _ in range(due):
            sock.sendto(payload, target)
            sent += 1
        time.sleep(0.002)
    sock.close()
    return sent


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="UDP background traffic generator")
    ap.add_argument("--target", default="127.0.0.1:6000")
    ap.add_argument("--rate", type=float, default=200, help="packets/second")
    ap.add_argument("--size", type=int, default=1000, help="payload bytes")
    ap.add_argument("--on", type=float, default=0, help="burst mode: seconds sending (0 = continuous)")
    ap.add_argument("--off", type=float, default=0, help="burst mode: seconds idle")
    a = ap.parse_args()
    h, p = a.target.split(":")
    stop = threading.Event()
    print(f"sending {a.rate} pkt/s x {a.size} B to {a.target} (Ctrl+C to stop)")
    try:
        if a.on > 0:
            from traffic import _burst
            _burst((h, int(p)), a.rate, a.size, stop, a.on, a.off)
        else:
            run_bg((h, int(p)), a.rate, a.size, stop)
    except KeyboardInterrupt:
        stop.set()
