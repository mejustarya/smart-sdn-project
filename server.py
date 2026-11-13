"""Reference time server (UDP).

A single UDP socket serves any number of clients; each request is answered
independently, so the server is stateless per client.
"""
import argparse
import socket
import threading
import time
from collections import Counter

from common import REQ, RESP, Clock, is_sync, mark_dscp, pack, unpack


def run_server(port, stop, host="127.0.0.1", ready=None, stats=None):
    clock = Clock(0.0)  # the reference clock
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    mark_dscp(sock)     # replies are sync traffic -> EF
    sock.settimeout(0.2)
    stats = stats if stats is not None else Counter()
    if ready:
        ready.set()
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:  # e.g. ICMP "connection reset" on Windows
            continue
        t2 = clock.now()                 # receive timestamp
        if not is_sync(data):            # background traffic: just discard
            stats["bg_received"] += 1
            continue
        kind, seq, t1, _, _ = unpack(data)
        if kind != REQ:
            continue
        stats["sync_served"] += 1
        stats[f"client:{addr[1]}"] += 1
        t3 = clock.now()                 # transmit timestamp (as late as possible)
        sock.sendto(pack(RESP, seq, t1, t2, t3), addr)
    sock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="UDP reference time server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    stop = threading.Event()
    print(f"time server listening on {a.host}:{a.port}  (Ctrl+C to stop)")
    try:
        run_server(a.port, stop, a.host)
    except KeyboardInterrupt:
        stop.set()
