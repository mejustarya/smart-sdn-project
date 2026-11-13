"""Two-path network with an SDN-style controller (all in software).

Ideas borrowed from the Mininet/Ryu project:

    clients --> [ SLOW link: 5 Mbps, 50 ms ] --> server
            \\-> [ FAST link: 20 Mbps, 5 ms ] -/

* Sync traffic (marked UDP "TS") is steered onto one path at a time.
* Background traffic ("BG") is pinned to the SLOW path, so it is what
  congests the slow link.
* A controller thread samples the SLOW link's transmitted bytes every
  `monitor_interval` seconds (like OpenFlow port statistics), converts them to
  Mbps, and - when policy == "dynamic":
      rate >= high_mbps  -> move sync traffic to FAST
      rate <= low_mbps   -> move it back to SLOW
  Two thresholds give hysteresis; `hold_down` is the minimum time between two
  route changes. policy == "static" never changes the route (always SLOW).

Idea added from the first project: `priority=True` gives sync packets a strict
priority queue on both links (like DSCP EF), so they never wait behind
background packets except for the one already being transmitted.

Replies from the server return over whichever path is currently selected
(delay only; no queue in the reverse direction).
"""
import heapq
import itertools
import random
import selectors
import socket
import threading
import time
from collections import deque

from common import is_sync

OVERHEAD = 28  # IP + UDP header bytes counted on the wire


class Link:
    """One direction of a link: finite queue(s), serialisation, propagation delay."""

    def __init__(self, name, rate_bps, delay, jitter, queue_bytes, priority,
                 on_sent, rng, loss=0.0, extra_delay=0.0):
        self.name, self.rate = name, rate_bps
        self.delay, self.jitter, self.extra_delay = delay, jitter, extra_delay
        self.queue_bytes, self.priority, self.loss = queue_bytes, priority, loss
        self.on_sent, self.rng = on_sent, rng
        self.q_hi, self.q_lo = deque(), deque()
        self.b_hi = self.b_lo = 0
        self.cv = threading.Condition()
        self.tx_bytes = 0                       # like OpenFlow port tx_bytes
        self.stats = {"sync_tx": 0, "bg_tx": 0, "sync_drop": 0, "bg_drop": 0}
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_evt.set()
        with self.cv:
            self.cv.notify_all()
        self.thread.join(timeout=1)

    def prop_delay(self):
        return self.delay + self.extra_delay + self.rng.uniform(0, self.jitter)

    def enqueue(self, data, addr, sync):
        size = len(data) + OVERHEAD
        kind = "sync" if sync else "bg"
        if self.loss and self.rng.random() < self.loss:
            self.stats[kind + "_drop"] += 1
            return
        hi = sync and self.priority
        with self.cv:
            used = self.b_hi if hi else self.b_lo
            if used + size > self.queue_bytes:  # tail drop
                self.stats[kind + "_drop"] += 1
                return
            (self.q_hi if hi else self.q_lo).append((data, addr, size, sync))
            if hi:
                self.b_hi += size
            else:
                self.b_lo += size
            self.cv.notify()

    def _run(self):
        free_at = 0.0  # virtual time at which the link finishes its current packet
        while True:
            with self.cv:
                while not (self.q_hi or self.q_lo):
                    if self.stop_evt.is_set():
                        return
                    self.cv.wait(0.1)
                if self.q_hi:                    # strict priority (if enabled)
                    data, addr, size, sync = self.q_hi.popleft()
                    self.b_hi -= size
                else:
                    data, addr, size, sync = self.q_lo.popleft()
                    self.b_lo -= size
            finish = max(time.perf_counter(), free_at) + size * 8 / self.rate
            free_at = finish
            rem = finish - time.perf_counter()
            if rem > 0:
                time.sleep(rem)
            self.tx_bytes += size
            self.stats["sync_tx" if sync else "bg_tx"] += 1
            self.on_sent(self, data, addr)


class SdnNetwork:
    def __init__(self, listen_port, server_addr, policy="dynamic", priority=False,
                 slow_rate=5e6, slow_delay=0.050, slow_jitter=0.002,
                 fast_rate=20e6, fast_delay=0.005, fast_jitter=0.001,
                 queue_bytes=60_000, high_mbps=2.5, low_mbps=0.8,
                 hold_down=4.0, monitor_interval=2.0,
                 slow_loss=0.0, slow_extra_delay=0.0, seed=0, host="127.0.0.1"):
        assert policy in ("static", "dynamic")
        self.server_addr, self.policy, self.priority = server_addr, policy, priority
        self.high, self.low = high_mbps, low_mbps
        self.hold_down, self.monitor_interval = hold_down, monitor_interval
        self.rng = random.Random(seed)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, listen_port))
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sock, selectors.EVENT_READ, None)
        self.upstream = {}

        self.links = {
            "SLOW": Link("SLOW", slow_rate, slow_delay, slow_jitter, queue_bytes, priority,
                         self._on_sent, self.rng, loss=slow_loss, extra_delay=slow_extra_delay),
            "FAST": Link("FAST", fast_rate, fast_delay, fast_jitter, queue_bytes, priority,
                         self._on_sent, self.rng),
        }
        self.path = "SLOW"                  # default route, as in the SDN project
        self.route_events = []              # [{"t", "path", "rate_mbps"}]
        self.samples = []                   # [{"t", "slow_mbps", "path"}]

        self.heap, self.counter = [], itertools.count()
        self.hcv = threading.Condition()
        self.stop_evt = threading.Event()
        self.t0 = time.time()
        self.threads = [threading.Thread(target=f, daemon=True)
                        for f in (self._rx, self._delivery, self._controller)]

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self.t0 = time.time()
        for l in self.links.values():
            l.start()
        for t in self.threads:
            t.start()

    def snapshot(self):
        return {"route_events": list(self.route_events), "samples": list(self.samples),
                "slow": dict(self.links["SLOW"].stats), "fast": dict(self.links["FAST"].stats),
                "slow_tx_bytes": self.links["SLOW"].tx_bytes}

    def stop(self):
        self.stop_evt.set()
        with self.hcv:
            self.hcv.notify_all()
        for l in self.links.values():
            l.close()
        for t in self.threads:
            t.join(timeout=1)
        for s in list(self.upstream.values()) + [self.sock]:
            s.close()

    # ---- data plane --------------------------------------------------------
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
                if key.data is None:                       # from a client
                    self._upstream_for(addr)
                    sync = is_sync(data)
                    link = self.links[self.path] if sync else self.links["SLOW"]
                    link.enqueue(data, addr, sync)
                else:                                      # reply from the server
                    link = self.links[self.path]
                    self._schedule(link.prop_delay(), self._to_client, data, key.data)

    def _on_sent(self, link, data, addr):
        self._schedule(link.prop_delay(), self._to_server, data, addr)

    def _schedule(self, delay, fn, *args):
        with self.hcv:
            heapq.heappush(self.heap, (time.perf_counter() + delay, next(self.counter), fn, args))
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

    def _to_server(self, data, client):
        self.upstream[client].sendto(data, self.server_addr)

    def _to_client(self, data, client):
        self.sock.sendto(data, client)

    # ---- control plane ----------------------------------------------------
    def _controller(self):
        prev_bytes, prev_t, last_change = 0, time.time(), -1e9
        while not self.stop_evt.wait(self.monitor_interval):
            now = time.time()
            b = self.links["SLOW"].tx_bytes
            rate = (b - prev_bytes) * 8 / max(now - prev_t, 1e-6) / 1e6   # Mbps
            prev_bytes, prev_t = b, now
            if self.policy == "dynamic" and now - last_change >= self.hold_down:
                new = None
                if self.path == "SLOW" and rate >= self.high:
                    new = "FAST"
                elif self.path == "FAST" and rate <= self.low:
                    new = "SLOW"
                if new:
                    self.path, last_change = new, now
                    self.route_events.append({"t": now - self.t0, "path": new, "rate_mbps": rate})
            self.samples.append({"t": now - self.t0, "slow_mbps": rate, "path": self.path})
