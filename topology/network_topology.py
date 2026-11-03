#!/usr/bin/env python3
"""Single-VM Mininet topology and interactive experiment commands."""
import argparse
import json
import socket
import threading
import os
import shlex
import shutil
import time
from pathlib import Path

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

ROOT = Path(__file__).resolve().parents[1]
SERVER_IP = "10.0.0.10"
SERVER_MAC = "00:00:00:00:00:10"
SLOW_IFACE = "s1-eth5"      # slow path egress on s1 (shaped by Mininet TCLink)
SLOW_BASE_DELAY_MS = 50
TCP_BASE_PORT = 5201

CLIENTS = [
    ("h1", "10.0.0.1", "00:00:00:00:00:01", 1),
    ("h2", "10.0.0.2", "00:00:00:00:00:02", 2),
    ("h3", "10.0.0.3", "00:00:00:00:00:03", 3),
]
BACKGROUND = [
    ("h5", "10.0.0.50", "00:00:00:00:00:50", 4),
    ("h6", "10.0.0.51", "00:00:00:00:00:51", 7),
    ("h7", "10.0.0.52", "00:00:00:00:00:52", 8),
]
SCENARIOS = {
    "low":      (0.5, 0.5, 0.5),
    "moderate": (1.0, 1.0, 1.0),
    "high":     (2.0, 2.0, 2.0),
    "extreme":  (3.0, 3.0, 3.0),
}


class ExperimentCLI(CLI):
    def __init__(self, net, hosts, duration=120, **kwargs):
        self.hosts = hosts
        self.duration = duration
        self.bg_pids = {}
        self._lock = threading.RLock()
        self._start_dashboard_control()
        super().__init__(net, **kwargs)

    @staticmethod
    def _num(req, key, default, lo, hi, cast=float):
        value = cast(req.get(key, default))
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        return value

    def _handle_request(self, request):
        """Execute one dashboard command. Returns a JSON-serialisable dict."""
        action = request.get("action")
        num = self._num
        if action == "scenario":
            level = str(request.get("level", ""))
            seconds = num(request, "seconds", 120, 5, 3600, int)
            if level not in SCENARIOS:
                return {"ok": False, "message": "Invalid scenario"}
            self.do_scenario(f"{level} {seconds}")
            return {"ok": True, "message": f"{level.title()} UDP congestion started for {seconds}s"}
        if action == "stop":
            self.do_stoptraffic("")
            return {"ok": True, "message": "All background traffic stopped"}
        if action == "sync":
            rounds = num(request, "rounds", 40, 1, 1000, int)
            self.do_sync(str(rounds))
            return {"ok": True, "message": f"Synchronization started ({rounds} rounds)"}
        if action == "stopsync":
            self._stop_sync()
            return {"ok": True, "message": "Synchronization clients stopped"}
        if action == "tcp":
            seconds = num(request, "seconds", 60, 5, 3600, int)
            flows = num(request, "flows", 2, 1, 3, int)
            rate = num(request, "rate_mbps", 0, 0, 100, float)
            return self.start_tcp(seconds, flows, rate)
        if action == "burst":
            rate = num(request, "rate_mbps", 3.0, 0.5, 10, float)
            on = num(request, "on_seconds", 4, 1, 30, int)
            off = num(request, "off_seconds", 3, 1, 30, int)
            seconds = num(request, "seconds", 60, 5, 3600, int)
            return self.start_burst(rate, on, off, seconds)
        if action == "netem":
            delay = num(request, "delay_ms", SLOW_BASE_DELAY_MS, 0, 500, float)
            jitter = num(request, "jitter_ms", 0, 0, 200, float)
            loss = num(request, "loss_pct", 0, 0, 30, float)
            return self.set_netem(delay, jitter, loss)
        if action == "netem_reset":
            return self.set_netem(SLOW_BASE_DELAY_MS, 0, 0)
        return {"ok": False, "message": "Unknown action"}

    def _start_dashboard_control(self):
        """Accept dashboard commands on localhost only; Mininet remains the owner of hosts."""
        def serve():
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind(("127.0.0.1", 8765))
                server.listen(5)
                while True:
                    conn, _ = server.accept()
                    with conn:
                        try:
                            raw = conn.recv(4096).decode("utf-8").strip()
                            response = self._handle_request(json.loads(raw or "{}"))
                        except Exception as exc:
                            response = {"ok": False, "message": str(exc)}
                        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except OSError as exc:
                print(f"Dashboard control socket unavailable: {exc}")

        threading.Thread(target=serve, daemon=True, name="dashboard-control").start()

    def do_scenario(self, line):
        """Start a congestion scenario: scenario low|moderate|high|extreme [seconds]"""
        with self._lock:
            self._scenario(line)

    def _scenario(self, line):
        args = shlex.split(line)
        if not args or args[0] not in SCENARIOS:
            print("Usage: scenario low|moderate|high|extreme [seconds]")
            return
        seconds = int(args[1]) if len(args) > 1 else self.duration
        self._stop()
        rates = SCENARIOS[args[0]]
        binary = str(ROOT / "background_sender")
        if not Path(binary).exists():
            print("background_sender binary missing. Exit Mininet and run: make")
            return
        for (host_name, _ip, _mac, _port), rate in zip(BACKGROUND, rates):
            host = self.hosts[host_name]
            pidfile = f"/tmp/smart-sdn-{host_name}-bg.pid"
            logfile = str(ROOT / "logs" / f"{host_name}-{args[0]}.log")
            command = (
                f"{shlex.quote(binary)} {SERVER_IP} {rate} {seconds} 1200 "
                f"> {shlex.quote(logfile)} 2>&1 & echo $! > {pidfile}"
            )
            host.cmd(command)
            self.bg_pids[host_name] = pidfile
        print(f"Started {args[0]} congestion: rates={rates} Mbps per host, duration={seconds}s")

    def do_stoptraffic(self, _line):
        """Stop background traffic generators."""
        with self._lock:
            self._stop()

    def _stop(self):
        for host_name, pidfile in list(self.bg_pids.items()):
            self.hosts[host_name].cmd(
                f"if [ -f {pidfile} ]; then kill $(cat {pidfile}) 2>/dev/null; rm -f {pidfile}; fi"
            )
        self.bg_pids.clear()
        # Also stop iperf3 (TCP) and burst loops (their command line contains background_sender).
        self.hosts["h4"].cmd("pkill -x iperf3 2>/dev/null; pkill -f background_sender 2>/dev/null; true")
        print("Background traffic generators stopped.")

    def do_sync(self, line):
        """Start synchronization clients: sync [rounds]"""
        args = shlex.split(line)
        rounds = int(args[0]) if args else 40
        self._stop_sync()
        binary = str(ROOT / "time_client")
        if not Path(binary).exists():
            print("time_client binary missing. Exit Mininet and run: make")
            return
        offsets = [40, -25, 80]
        for (host_name, _ip, _mac, _port), client_id, offset in zip(CLIENTS, range(1, 4), offsets):
            logfile = str(ROOT / "logs" / f"{host_name}-sync.csv")
            self.hosts[host_name].cmd(
                f"{shlex.quote(binary)} {SERVER_IP} {client_id} {offset} {rounds} "
                f"> {shlex.quote(logfile)} 2>&1 &"
            )
        print(f"Started three UDP synchronization clients ({rounds} rounds). CSV files are in logs/.")


    def _stop_sync(self):
        with self._lock:
            self.hosts["h1"].cmd("pkill -x time_client 2>/dev/null; true")

    def start_tcp(self, seconds, flows, rate_mbps):
        """Competing TCP flows (iperf3) from h5..h7 to the server; they ride the slow path."""
        if shutil.which("iperf3") is None:
            return {"ok": False, "message": "iperf3 is not installed. Run: sudo apt install -y iperf3"}
        with self._lock:
            self._stop()
            h4 = self.hosts["h4"]
            names = [b[0] for b in BACKGROUND][:flows]
            for i, _name in enumerate(names):
                h4.cmd(f"iperf3 -s -D -p {TCP_BASE_PORT + i} > /dev/null 2>&1")
            time.sleep(0.5)
            bw = f"-b {rate_mbps:g}M" if rate_mbps > 0 else ""
            for i, name in enumerate(names):
                log = str(ROOT / "logs" / f"{name}-tcp.log")
                self.hosts[name].cmd(
                    f"iperf3 -c {SERVER_IP} -p {TCP_BASE_PORT + i} -t {seconds} {bw} "
                    f"> {shlex.quote(log)} 2>&1 &"
                )
        return {"ok": True, "message": f"{flows} TCP flow(s) started for {seconds}s"}

    def start_burst(self, rate, on, off, seconds):
        """On/off UDP bursts from h5..h7 (on seconds sending, off seconds idle)."""
        binary = str(ROOT / "background_sender")
        if not Path(binary).exists():
            return {"ok": False, "message": "background_sender binary missing. Run: make"}
        with self._lock:
            self._stop()
            loop = (f"end=$((SECONDS+{seconds})); while [ $SECONDS -lt $end ]; do "
                    f"{shlex.quote(binary)} {SERVER_IP} {rate:g} {on} 1200 >/dev/null 2>&1; "
                    f"sleep {off}; done")
            for name, *_ in BACKGROUND:
                self.hosts[name].cmd(f"bash -c {shlex.quote(loop)} > /dev/null 2>&1 &")
        return {"ok": True, "message": f"Burst traffic started ({rate:g} Mbps x3, {on}s on / {off}s off, {seconds}s)"}

    def set_netem(self, delay_ms, jitter_ms, loss_pct):
        """Change delay/jitter/loss on the slow path egress (s1-eth5)."""
        with self._lock:
            s1 = self.mn.get("s1")
            jitter = f" {jitter_ms:g}ms" if jitter_ms else ""
            out = s1.cmd(
                f"tc qdisc replace dev {SLOW_IFACE} parent 5:1 handle 10: "
                f"netem delay {delay_ms:g}ms{jitter} loss {loss_pct:g}% limit 100"
            ).strip()
        if out:
            return {"ok": False, "message": f"tc error: {out}"}
        return {"ok": True, "message": f"Slow link now: delay {delay_ms:g} ms, jitter {jitter_ms:g} ms, loss {loss_pct:g}%"}

    def do_tcp(self, line):
        """Start competing TCP flows: tcp [seconds] [flows]"""
        a = shlex.split(line)
        print(self.start_tcp(int(a[0]) if a else 60, int(a[1]) if len(a) > 1 else 2, 0)["message"])

    def do_burst(self, line):
        """Start burst traffic: burst [seconds]"""
        a = shlex.split(line)
        print(self.start_burst(3.0, 4, 3, int(a[0]) if a else 60)["message"])

    def do_netem(self, line):
        """Set slow-link impairments: netem DELAY_MS [LOSS_PCT]"""
        a = shlex.split(line)
        print(self.set_netem(float(a[0]) if a else SLOW_BASE_DELAY_MS, 0, float(a[1]) if len(a) > 1 else 0)["message"])

    def do_status(self, _line):
        """Show host addresses and current switch/controller status."""
        for name in ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]:
            host = self.hosts[name]
            print(f"{name}: {host.IP()}  {host.MAC()}")
        print("Controller: 127.0.0.1:6633")
        print("Slow path: s1:5 <-> s2:2 (5 Mbps, 50 ms)")
        print("Fast path: s1:6 <-> s2:3 (20 Mbps, 5 ms)")


def build(controller_ip="127.0.0.1", controller_port=6633, duration=120):
    net = Mininet(
        controller=None, switch=OVSSwitch, link=TCLink,
        autoSetMacs=False, autoStaticArp=False
    )
    c0 = net.addController(
        "c0", controller=RemoteController,
        ip=controller_ip, port=controller_port
    )

    s1 = net.addSwitch(
        "s1", dpid="0000000000000001", protocols="OpenFlow13",
        failMode="secure"
    )
    s2 = net.addSwitch(
        "s2", dpid="0000000000000002", protocols="OpenFlow13",
        failMode="secure"
    )

    hosts = {}
    for name, ip, mac, port in CLIENTS + BACKGROUND:
        host = net.addHost(name, ip=ip + "/24", mac=mac)
        hosts[name] = host
        net.addLink(host, s1, port1=1, port2=port)

    h4 = net.addHost("h4", ip=SERVER_IP + "/24", mac=SERVER_MAC)
    hosts["h4"] = h4
    net.addLink(h4, s2, port1=1, port2=1)

    # Parallel paths with explicitly assigned OpenFlow ports.
    net.addLink(s1, s2, port1=5, port2=2, cls=TCLink,
                bw=5, delay="50ms", max_queue_size=100)
    net.addLink(s1, s2, port1=6, port2=3, cls=TCLink,
                bw=20, delay="5ms", max_queue_size=200)

    net.build()
    c0.start()
    s1.start([c0])
    s2.start([c0])

    # Static ARP entries ensure unicast forwarding works without relying on
    # broadcast flooding across the two parallel links.
    for name, ip, mac, _port in CLIENTS + BACKGROUND:
        hosts[name].cmd(f"arp -s {SERVER_IP} {SERVER_MAC}")
        h4.cmd(f"arp -s {ip} {mac}")

    # Start the UDP time server inside h4's network namespace.
    server_binary = str(ROOT / "time_server")
    if Path(server_binary).exists():
        server_log = str(ROOT / "logs" / "server.log")
        h4.cmd(f"{shlex.quote(server_binary)} > {shlex.quote(server_log)} 2>&1 &")
        time.sleep(0.5)
        print("UDP time server started on h4 (UDP 5005; background receiver UDP 6000).")
    else:
        print("WARNING: time_server binary missing. Run 'make' before starting topology.")

    print("\nSingle-VM SDN topology is ready.")
    print("Hosts: h1-h3 synchronization clients; h4 time server; h5-h7 traffic generators.")
    print("Commands inside Mininet CLI:")
    print("  status                         - show topology details")
    print("  sync [rounds]                  - start all three synchronization clients")
    print("  scenario low|moderate|high|extreme [seconds]")
    print("  tcp [seconds] [flows]          - competing TCP flows (iperf3)")
    print("  burst [seconds]                - on/off UDP bursts")
    print("  netem DELAY_MS [LOSS_PCT]      - change slow-link delay/loss")
    print("  stoptraffic                    - stop background generators")
    print("  h1 ping -c 3 10.0.0.10        - test client/server connectivity")
    print("  exit                           - stop Mininet")
    ExperimentCLI(net, hosts=hosts, duration=duration)
    net.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6633)
    parser.add_argument("--duration", type=int, default=120)
    args = parser.parse_args()
    setLogLevel("info")
    build(args.controller_ip, args.controller_port, args.duration)
