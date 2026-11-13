"""Network emulator: a UDP relay between clients and the server that
generates controlled delay and congestion.

    client --> [bottleneck link: rate limit, finite queue] --> +delay/jitter --> server
    client <-- [no bottleneck]                             <-- +delay/jitter <-- server

* Controlled delay : fixed base delay + uniform random jitter in both directions.
* Congestion       : the client->server direction passes through a bottleneck
                     link of `rate_bps` with a finite buffer. Background
                     traffic that overloads it builds a standing queue, which
                     delays (or drops) packets.
* Prioritisation   : mode="fifo"     -> one queue, sync and background share it.
                     mode="priority" -> sync packets go to a strict-priority
                     queue served before background packets (non-preemptive,
                     like DiffServ EF / 802.1p).

Congestion is deliberately only in the forward direction, so the delay is
asymmetric - the situation in which offset estimation is hurt the most.
"""
import argparse
import heapq
import itertools
import random
import selectors
import socket
import threading
import time
from collections import deque

from common import is_sync

IP_UDP_OVERHEAD = 28  # bytes added to each datagram on the wire


def precise_sleep(dt):
    end = time.perf_counter() + dt
    while True:
        rem = end - time.perf_counter()
        if rem <= 0:
            return
        if rem > 0.002:
            time.sleep(rem - 0.001)


class Emulator:
    def __init__(self, listen_port, server_addr, rate_bps=1_000_000,
                 base_delay=0.020, jitter=0.005, queue_bytes=50_000,
                 mode="fifo", loss=0.0, host="127.0.0.1", seed=None):
        assert mode in ("fifo", "priority")
        self.server_addr = server_addr
        self.rate = rate_bps
        self.base_delay, self.jitter = base_delay, jitter
        self.queue_bytes, self.mode, self.loss = queue_bytes, mode, loss
        self.rng = random.Random(seed)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, listen_port))
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sock, selectors.EVENT_READ, None)
        self.upstream = {}                       # client addr -> socket to server

        self.q_hi, self.q_lo = deque(), deque()  # bottleneck queues
        self.bytes_hi = self.bytes_lo = 0
        self.qcv = threading.Condition()

        self.heap, self.counter = [], itertools.count()   # delayed deliveries
        self.hcv = threading.Condition()

        self.stop_evt = threading.Event()
        self.stats = {"sync_fwd": 0, "bg_fwd": 0, "sync_dropped": 0, "bg_dropped": 0}
        self.threads = [threading.Thread(target=f, daemon=True)
                        for f in (self._rx, self._tx, self._delivery)]

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        for t in self.threads:
            t.start()

    def stop(self):
        self.stop_evt.set()
        with self.qcv:
            self.qcv.notify_all()
        with self.hcv:
            self.hcv.notify_all()
        for t in self.threads:
            t.join(timeout=1)
        for s in list(self.upstream.values()) + [self.sock]:
            s.close()

    # ---- receive side ----------------------------------------------------
    def _upstream_for(self, client):
        s = self.upstream.get(client)
        if s is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.upstream[client] = s
            self.sel.register(s, selectors.EVENT_READ, client)
        return s

    def _rx(self):
        while not self.stop_evt.is_set():
            for key, _ in self.sel.select(timeout=0.1):
                try:
                    data, addr = key.fileobj.recvfrom(4096)
                except OSError:
                    continue
                if key.data is None:
                    self._from_client(data, addr)
                else:                              # reply from server
                    self._schedule(self._deliver_rev, data, key.data)

    def _from_client(self, data, addr):
        self._upstream_for(addr)
        sync = is_sync(data)
        if self.rng.random() < self.loss:
            self.stats["sync_dropped" if sync else "bg_dropped"] += 1
            return
        size = len(data) + IP_UDP_OVERHEAD
        hi = sync and self.mode == "priority"
        with self.qcv:
            used = self.bytes_hi if hi else self.bytes_lo
            if used + size > self.queue_bytes:     # tail drop
                self.stats["sync_dropped" if sync else "bg_dropped"] += 1
                return
            (self.q_hi if hi else self.q_lo).append((data, addr, size, sync))
            if hi:
                self.bytes_hi += size
            else:
                self.bytes_lo += size
            self.qcv.notify()

    # ---- bottleneck link (forward direction) -----------------------------
    def _tx(self):
        while True:
            with self.qcv:
                while not (self.q_hi or self.q_lo):
                    if self.stop_evt.is_set():
                        return
                    self.qcv.wait(0.1)
                if self.q_hi:                      # strict priority
                    data, addr, size, sync = self.q_hi.popleft()
                    self.bytes_hi -= size
                else:
                    data, addr, size, sync = self.q_lo.popleft()
                    self.bytes_lo -= size
            precise_sleep(size * 8 / self.rate)    # serialisation time
            self.stats["sync_fwd" if sync else "bg_fwd"] += 1
            self._schedule(self._deliver_fwd, data, addr)

    # ---- propagation delay + jitter -------------------------------------
    def _schedule(self, fn, *args):
        delay = self.base_delay + self.rng.uniform(0, self.jitter)
        with self.hcv:
            heapq.heappush(self.heap, (time.perf_counter() + delay,
                                       next(self.counter), fn, args))
            self.hcv.notify()

    def _delivery(self):
        while True:
            with self.hcv:
                while True:
                    if self.stop_evt.is_set():
                        return
                    if self.heap:
                        wait = self.heap[0][0] - time.perf_counter()
                        if wait <= 0:
                            break
                        self.hcv.wait(wait)
                    else:
                        self.hcv.wait(0.1)
                _, _, fn, args = heapq.heappop(self.heap)
            try:
                fn(*args)
            except OSError:
                pass

    def _deliver_fwd(self, data, client):
        self.upstream[client].sendto(data, self.server_addr)

    def _deliver_rev(self, data, client):
        self.sock.sendto(data, client)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="UDP network emulator")
    ap.add_argument("--listen", type=int, default=6000)
    ap.add_argument("--server", default="127.0.0.1:5000")
    ap.add_argument("--rate", type=float, default=1_000_000, help="bottleneck bit/s")
    ap.add_argument("--delay", type=float, default=0.020, help="one-way base delay (s)")
    ap.add_argument("--jitter", type=float, default=0.005, help="max jitter (s)")
    ap.add_argument("--queue", type=int, default=50_000, help="buffer bytes")
    ap.add_argument("--mode", choices=["fifo", "priority"], default="fifo")
    ap.add_argument("--loss", type=float, default=0.0)
    a = ap.parse_args()
    h, p = a.server.split(":")
    emu = Emulator(a.listen, (h, int(p)), a.rate, a.delay, a.jitter,
                   a.queue, a.mode, a.loss)
    emu.start()
    print(f"emulator :{a.listen} -> {a.server}  mode={a.mode}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(2)
            print(emu.stats)
    except KeyboardInterrupt:
        emu.stop()
