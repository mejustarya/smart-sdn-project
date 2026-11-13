"""Lab backend for the dashboard.

Owns everything that needs root: the Ryu controller process, the Mininet network,
the time server, and the experiment runs. All Mininet calls happen on ONE worker
thread (Mininet is not thread-safe); other threads only read /sys counters and run
`tc -s` from the root namespace, which is where the switches live.

Everything the browser shows travels through the `Bus` as small JSON events:
    lab        lab state changed (offline / starting / ready / running / error)
    log        a line for the event feed
    run_start  a run begins (policy, scenario, clock skews, t0 ...)
    probe      one finished sync probe from one client (t1..t4, delay, offset, error)
    net        slow/fast link throughput, queue backlog, controller reading (every 0.5 s)
    route      the controller moved sync traffic to another path
    run_end    run finished (summary metrics)
    matrix     progress / completion of the full comparison
"""
import csv
import json
import math
import os
import pwd
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from argparse import Namespace
from collections import deque
from itertools import islice

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sdn_config as C                                               # noqa: E402
from run_sdn_experiment import POLICIES, pname, summarize, write_csv  # noqa: E402

PY = sys.executable
RESULTS_DIR = os.path.join(ROOT, "results_dashboard")
SAMPLE_SUMMARY = os.path.join(ROOT, "results_mininet_sandbox", "summary.csv")
SCENARIOS = ["baseline", "low", "moderate", "high", "extreme", "burst"]
LEVEL_OF = {"low": "low", "moderate": "moderate", "high": "high", "extreme": "extreme", "burst": "high"}
MBPS_PER_HOST = {"low": 0.5, "moderate": 1.0, "high": 2.0, "extreme": 3.0}
PAYLOAD = 1000


def clean(o):
    """Make a structure JSON-safe: NaN/inf become None."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


class Bus:
    """Append-only event log with blocking readers (used for Server-Sent Events)."""

    def __init__(self, maxlen=60000):
        self.events = deque(maxlen=maxlen)
        self.cv = threading.Condition()
        self.next_id = 1

    def emit(self, type_, **data):
        with self.cv:
            ev = clean({"id": self.next_id, "type": type_, **data})
            self.next_id += 1
            self.events.append(ev)
            self.cv.notify_all()
            return ev["id"]

    @property
    def last_id(self):
        with self.cv:
            return self.next_id - 1

    def since(self, last_id, timeout=15.0):
        end = time.time() + timeout
        with self.cv:
            while True:
                if self.events and self.events[-1]["id"] > last_id:
                    first = self.events[0]["id"]
                    start = max(0, last_id + 1 - first)
                    return list(islice(self.events, start, None))
                rem = end - time.time()
                if rem <= 0:
                    return []
                self.cv.wait(rem)


def port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_ryu():
    cands = [os.environ.get("RYU_MANAGER", "")]
    homes = [os.path.expanduser("~")]
    if os.environ.get("SUDO_USER"):
        try:
            homes.append(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        except KeyError:
            pass
    cands += [os.path.join(h, "ryu39-venv", "bin", "ryu-manager") for h in homes]
    cands.append(shutil.which("ryu-manager") or "")
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def read_csv_rows(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return 0


def tc_stats(dev):
    """(backlog_bytes, backlog_pkts, dropped_pkts) of an interface's queues."""
    try:
        out = subprocess.run(["tc", "-s", "qdisc", "show", "dev", dev], capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return 0, 0, 0
    bl = re.findall(r"backlog (\d+)b (\d+)p", out)
    return (max((int(b) for b, _ in bl), default=0), max((int(p) for _, p in bl), default=0),
            sum(int(x) for x in re.findall(r"dropped (\d+)", out)))


def probe_netem():
    """Does this kernel support tc netem? (tested on a throw-away veth pair)"""
    try:
        subprocess.run(["ip", "link", "add", "ntmtest0", "type", "veth", "peer", "name", "ntmtest1"],
                       capture_output=True, timeout=5)
        r = subprocess.run(["tc", "qdisc", "add", "dev", "ntmtest0", "root", "netem", "delay", "5ms"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        subprocess.run(["ip", "link", "del", "ntmtest0"], capture_output=True)


def kernel_ovs_available():
    if os.path.isdir("/sys/module/openvswitch"):
        return True
    try:
        subprocess.run(["modprobe", "openvswitch"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.isdir("/sys/module/openvswitch")


class Lab:
    def __init__(self, bus, datapath="auto", netem="auto", queue_pkts=C.QUEUE_PKTS):
        self.bus = bus
        self.datapath_opt, self.netem_opt, self.queue_pkts = datapath, netem, queue_pkts
        self.datapath, self.netem = None, None
        self.state, self.message = "offline", "Lab is not started"
        self.lock = threading.Lock()
        self.cmds = queue.Queue()
        self.abort = threading.Event()
        self.net, self.ryu, self.servers = None, None, []
        self.run_meta, self.run_start_id = None, 0
        self.matrix_state = None
        self.history, self.matrix_result = [], None
        self.last_rows = []
        os.makedirs(RESULTS_DIR, exist_ok=True)
        self._load_history()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------ plumbing
    def log(self, text, level="info"):
        self.bus.emit("log", t=time.time(), level=level, text=text)

    def set_state(self, state, message=""):
        with self.lock:
            self.state, self.message = state, message
        self.bus.emit("lab", state=state, message=message, datapath=self.datapath, netem=self.netem)

    def status(self):
        with self.lock:
            st, msg = self.state, self.message
        return {"lab": {"state": st, "message": msg, "datapath": self.datapath, "netem": self.netem,
                        "root": os.geteuid() == 0},
                "run": self.run_meta, "run_start_id": self.run_start_id, "last_id": self.bus.last_id,
                "matrix": self.matrix_state, "server_time": time.time(),
                "scenarios": SCENARIOS, "policies": [pname(p, pr) for p, pr in POLICIES],
                "limits": {"high": C.HIGH_MBPS, "low": C.LOW_MBPS, "slow_mbps": C.SLOW_MBPS,
                           "fast_mbps": C.FAST_MBPS, "hold_down": C.HOLD_DOWN,
                           "monitor_interval": C.MONITOR_INTERVAL, "queue_pkts": self.queue_pkts}}

    def submit(self, cmd, arg=None):
        """Queue a command. Returns (ok, message)."""
        with self.lock:
            st = self.state
        if cmd == "start" and st not in ("offline", "error"):
            return False, f"Lab is already {st}"
        if cmd in ("run", "matrix") and st != "ready":
            return False, "Start the lab first" if st in ("offline", "error") else f"Lab is busy ({st})"
        if cmd == "stop" and st in ("offline", "starting"):
            return False, f"Nothing to stop ({st})"
        if cmd == "start" and os.geteuid() != 0:
            return False, "Mininet needs root: restart the dashboard with sudo"
        if cmd in ("start", "stop", "run", "matrix"):
            self.abort.clear()
        self.cmds.put((cmd, arg))
        return True, "ok"

    def request_abort(self):
        self.abort.set()

    def _worker(self):
        while True:
            cmd, arg = self.cmds.get()
            try:
                if cmd == "start":
                    self._start()
                elif cmd == "stop":
                    self._stop()
                elif cmd == "run":
                    self._run_single(arg)
                elif cmd == "matrix":
                    self._run_matrix(arg)
                elif cmd == "quit":
                    self._stop()
                    return
            except (Exception, SystemExit) as e:  # Mininet calls exit() on some errors; keep the worker alive
                traceback.print_exc()
                self.log(f"{cmd} failed: {e}", "error")
                if cmd == "start":
                    self._teardown()
                    self.set_state("error", str(e))
                elif self.net is not None:
                    self.set_state("ready", "Last action failed: " + str(e))
                else:
                    self.set_state("error", str(e))

    # -------------------------------------------------------- start / stop
    def _start(self):
        self.set_state("starting", "Checking the system")
        try:
            from mininet.log import setLogLevel
            from mininet_topology import build_network, disable_offloads, setup_qos
        except ImportError as e:
            raise RuntimeError("Mininet is not installed for this Python (run with /usr/bin/python3): %s" % e)
        setLogLevel("warning")
        os.chdir(ROOT)

        self.netem = probe_netem() if self.netem_opt == "auto" else bool(self.netem_opt)
        if self.datapath_opt == "auto":
            self.datapath = "kernel" if kernel_ovs_available() else "user"
        else:
            self.datapath = self.datapath_opt
        self.log(f"Open vSwitch datapath: {self.datapath}"
                 + ("" if self.datapath == "kernel" else " (kernel module not available)"),
                 "info" if self.datapath == "kernel" else "warn")
        self.log("Link delay (netem): " + ("on, 50 ms slow / 5 ms fast" if self.netem else
                                           "not available on this kernel, links have rate limits only"),
                 "info" if self.netem else "warn")

        if port_open(6653):
            self.log("Using the Ryu controller that is already running on port 6653")
        else:
            self.set_state("starting", "Cleaning up old Mininet state")
            subprocess.run(["mn", "-c"], capture_output=True, timeout=60)   # kills ryu-manager: do it first
            self._launch_ryu()
        self._tail_controller_log()

        self._ensure_ovs()
        self.set_state("starting", "Building the network")
        self.net = build_network("127.0.0.1", 6653, self.datapath)
        self.net.start()
        self.net.staticArp()
        disable_offloads(self.net)
        self.set_state("starting", "Waiting for the switches to reach Ryu")
        if not self.net.waitConnected(timeout=30):
            raise RuntimeError("the switches did not connect to the Ryu controller")
        time.sleep(1.5)
        setup_qos(self.net, priority=False, netem=self.netem, queue_pkts=self.queue_pkts)
        loss = self.net.ping([self.net.get("h1"), self.net.get("h4")], timeout="3")
        if loss > 0:
            raise RuntimeError("connectivity check failed (h1 cannot reach h4); see logs/controller.log")
        h4 = self.net.get("h4")
        self.servers = [
            h4.popen([PY, "server.py", "--host", C.SERVER_IP, "--port", str(C.SYNC_PORT)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            h4.popen([PY, "bg_sink.py", "--port", str(C.BG_PORT)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
        time.sleep(0.8)
        self.log("Network is up: 2 switches, 7 hosts, time server on h4", "ok")
        self.set_state("ready", "Ready to run")

    def _ensure_ovs(self):
        """Make sure the Open vSwitch database/daemon is running, starting it if we can."""
        def ok():
            try:
                return subprocess.run(["ovs-vsctl", "--timeout=5", "show"], capture_output=True,
                                      timeout=15).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False

        if ok():
            return
        self.set_state("starting", "Starting Open vSwitch")
        for cmd in (["systemctl", "start", "openvswitch-switch"],
                    ["/usr/share/openvswitch/scripts/ovs-ctl", "start"]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                continue
            if ok():
                self.log("Open vSwitch started", "ok")
                return
        raise RuntimeError("Open vSwitch is not running. Start it with: sudo systemctl start openvswitch-switch")

    def _launch_ryu(self):
        exe = find_ryu()
        if not exe:
            raise RuntimeError("ryu-manager not found. Install Ryu (scripts/setup_ubuntu.sh) or set RYU_MANAGER")
        self.set_state("starting", "Starting the Ryu controller")
        env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
        os.makedirs(C.LOG_DIR, exist_ok=True)
        logf = open(os.path.join(C.LOG_DIR, "controller.log"), "w")
        self.ryu = subprocess.Popen([exe, "--ofp-tcp-listen-port", "6653", "ryu_controller.py"], cwd=ROOT,
                                    env=env, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(60):
            if port_open(6653):
                self.log("Ryu controller started", "ok")
                return
            if self.ryu.poll() is not None:
                raise RuntimeError("Ryu exited immediately; see logs/controller.log")
            time.sleep(0.5)
        raise RuntimeError("Ryu did not open port 6653 in time")

    def _tail_controller_log(self):
        path = os.path.join(C.LOG_DIR, "controller.log")
        if getattr(self, "_tail_thread", None) and self._tail_thread.is_alive():
            return

        def tail():
            try:
                f = open(path)
            except OSError:
                return
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.3)
                    continue
                if any(k in line for k in ("ROUTE CHANGE", "connected", "control:")):
                    self.log("Ryu: " + line.split("]")[-1].strip() if "]" in line else "Ryu: " + line.strip())

        self._tail_thread = threading.Thread(target=tail, daemon=True)
        self._tail_thread.start()

    def _teardown(self):
        for p in self.servers:
            try:
                p.terminate()
            except OSError:
                pass
        self.servers = []
        if self.net is not None:
            try:
                self.net.stop()
            except Exception:
                pass
            self.net = None
        if self.ryu is not None:
            try:
                self.ryu.terminate()
                self.ryu.wait(timeout=5)
            except Exception:
                pass
            self.ryu = None

    def _stop(self):
        self.set_state("offline", "Stopping")
        self._teardown()
        self.log("Lab stopped")
        self.set_state("offline", "Lab is not started")

    def shutdown(self):
        self.abort.set()
        self.cmds.put(("quit", None))
        self.worker.join(timeout=40)

    # ----------------------------------------------------------------- runs
    def _set_control(self, policy, token):
        os.makedirs(C.LOG_DIR, exist_ok=True)
        tmp = C.CONTROL_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"policy": policy, "token": token}, f)
        os.replace(tmp, C.CONTROL_FILE)

    def _run_single(self, p):
        self.set_state("running", "Preparing")
        summary = self._do_run(p, matrix=None)
        if summary:
            self._remember(summary)
        self.set_state("ready", "Ready to run")

    def _run_matrix(self, p):
        pols = p.get("policies") or [pname(a, b) for a, b in POLICIES]
        scens = p.get("scenarios") or SCENARIOS
        todo = [(a, b, s) for s in scens for (a, b) in POLICIES if pname(a, b) in pols]
        n, results, rows_all = len(todo), [], []
        self.matrix_state = {"i": 0, "n": n, "running": True}
        self.set_state("running", f"Comparison 0 of {n}")
        self.bus.emit("matrix", phase="start", n=n)
        for i, (policy, prio, scen) in enumerate(todo):
            if self.abort.is_set():
                break
            self.matrix_state = {"i": i + 1, "n": n, "running": True}
            self.set_state("running", f"Comparison {i + 1} of {n}")
            self.bus.emit("matrix", phase="progress", i=i + 1, n=n, label=pname(policy, prio), scenario=scen)
            q = dict(p, policy=policy, prio=prio, scenario=scen, matrix_i=i + 1, matrix_n=n)
            summary = self._do_run(q, matrix=(i + 1, n))
            if summary:
                results.append(summary)
                rows_all += self.last_rows
                self._remember(summary)
        self.matrix_state = None
        if results:
            self.matrix_result = {"rows": results, "source": "live", "when": time.time()}
            write_csv(os.path.join(RESULTS_DIR, "summary.csv"), results)
            write_csv(os.path.join(RESULTS_DIR, "raw_samples.csv"), rows_all)
            with open(os.path.join(RESULTS_DIR, "last_matrix.json"), "w") as f:
                json.dump(clean(self.matrix_result), f)
        self.bus.emit("matrix", phase="done", n=n, done=len(results),
                      aborted=self.abort.is_set(), result=self.matrix_result)
        self.set_state("ready", "Ready to run")

    def _remember(self, summary):
        self.history.append(summary)
        self.history = self.history[-200:]
        try:
            with open(os.path.join(RESULTS_DIR, "history.json"), "w") as f:
                json.dump(clean(self.history), f)
        except OSError:
            pass

    def _load_history(self):
        for name, attr in (("history.json", "history"), ("last_matrix.json", "matrix_result")):
            try:
                with open(os.path.join(RESULTS_DIR, name)) as f:
                    setattr(self, attr, json.load(f))
            except (OSError, ValueError):
                pass

    def results(self):
        m = self.matrix_result
        if not m:
            rows = []
            for r in read_csv_rows(SAMPLE_SUMMARY):
                row = {}
                for k, v in r.items():
                    try:
                        row[k] = float(v) if k not in ("policy", "scenario") and v != "" else v
                    except ValueError:
                        row[k] = v
                rows.append(row)
            m = {"rows": rows, "source": "sample", "when": None} if rows else None
        return {"matrix": m, "history": self.history}

    def _do_run(self, p, matrix):
        from mininet_topology import setup_qos
        net = self.net
        policy, prio, scen = p["policy"], bool(p.get("prio")), p["scenario"]
        if "+prio" in policy:                       # accept "dynamic+prio" style names too
            policy, prio = policy.split("+")[0], True
        label = pname(policy, prio)
        n_clients = max(1, min(3, int(p.get("clients", 3))))
        duration = float(p.get("duration", 20))
        start_delay = float(p.get("start_delay", 4))
        traffic_at = float(p.get("traffic_at", start_delay + 3))
        interval = float(p.get("interval", 0.2))
        timeout, window = 1.0, 8
        rng = random.Random(p.get("seed"))
        skews = p.get("skews") or [rng.choice([-1, 1]) * rng.uniform(0.8, 3.0) for _ in range(n_clients)]
        total = start_delay + duration

        self.bus.emit("log", t=time.time(), level="info", text=f"Run: {scen} traffic, policy {label}")
        self._set_control(policy, f"{time.time()}-{label}-{scen}")
        time.sleep(1.2)                                           # Ryu applies the reset
        setup_qos(net, priority=prio, netem=self.netem, queue_pkts=self.queue_pkts)
        time.sleep(0.8)
        if self.abort.is_set():
            return None

        dev_slow = net.get("s1").intfs[C.S1_SLOW_PORT].name
        dev_fast = net.get("s1").intfs[C.S1_FAST_PORT].name
        tx0 = read_int(f"/sys/class/net/{dev_slow}/statistics/tx_bytes")
        drops0 = tc_stats(dev_slow)[2]
        routes0 = len(read_csv_rows(C.ROUTE_EVENTS_CSV))
        t0 = time.time() + 0.3
        meta = {"policy": label, "base_policy": policy, "prio": prio, "scenario": scen,
                "clients": n_clients, "skews": skews, "duration": duration, "start_delay": start_delay,
                "traffic_at": traffic_at, "total": total, "t0": t0, "interval": interval,
                "netem": self.netem, "matrix": list(matrix) if matrix else None}
        self.run_meta = meta
        self.run_start_id = self.bus.emit("run_start", run=meta)
        rows, rows_lock = [], threading.Lock()
        done = threading.Event()

        # --- sampler: link throughput, queue backlog, controller view -----
        def sampler():
            prev = (time.time(), read_int(f"/sys/class/net/{dev_slow}/statistics/tx_bytes"),
                    read_int(f"/sys/class/net/{dev_fast}/statistics/tx_bytes"))
            n_routes, tick, path, bl = routes0, 0, "SLOW", (0, 0, 0)
            while not done.is_set():
                time.sleep(0.5)
                now = time.time()
                b_slow = read_int(f"/sys/class/net/{dev_slow}/statistics/tx_bytes")
                b_fast = read_int(f"/sys/class/net/{dev_fast}/statistics/tx_bytes")
                dt = max(now - prev[0], 1e-3)
                slow_mbps = (b_slow - prev[1]) * 8 / dt / 1e6
                fast_mbps = (b_fast - prev[2]) * 8 / dt / 1e6
                prev = (now, b_slow, b_fast)
                tick += 1
                if tick % 2 == 0:
                    bl = tc_stats(dev_slow)
                ev = read_csv_rows(C.ROUTE_EVENTS_CSV)
                for e in ev[n_routes:]:
                    path = e["path"]
                    self.bus.emit("route", t=float(e["t"]) - t0, path=path, rate_mbps=float(e["rate_mbps"]))
                    self.log(f"Ryu moved sync traffic to the {path} path (slow link at "
                             f"{float(e['rate_mbps']):.1f} Mbps)", "warn" if path == "FAST" else "ok")
                n_routes = len(ev)
                ctl = {}
                try:
                    with open(C.STATUS_JSON) as f:
                        ctl = json.load(f)
                except (OSError, ValueError):
                    pass
                self.bus.emit("net", t=now - t0, now=now, slow_mbps=slow_mbps, fast_mbps=fast_mbps,
                              backlog_bytes=bl[0], backlog_pkts=bl[1], drops=bl[2] - drops0,
                              path=path, ctl_mbps=ctl.get("slow_mbps"))

        threading.Thread(target=sampler, daemon=True).start()

        # --- clients (live JSON lines) -------------------------------------
        procs = []
        for i in range(n_clients):
            h = C.CLIENT_HOSTS[i]
            pr = net.get(h).popen(
                [PY, "client.py", "--server", f"{C.SERVER_IP}:{C.SYNC_PORT}", "--skew", str(skews[i]),
                 "--duration", str(duration), "--interval", str(interval), "--timeout", str(timeout),
                 "--window", str(window), "--name", f"C{i + 1}", "--jsonl",
                 "--start-delay", str(start_delay + max(0.0, t0 - time.time()))],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            procs.append(pr)

        def reader(pr):
            for raw in iter(pr.stdout.readline, b""):
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                row["t"] = row["wall_time"] - t0
                row["policy"], row["scenario"] = label, scen
                with rows_lock:
                    rows.append(row)
                self.bus.emit("probe", row=row)

        readers = [threading.Thread(target=reader, args=(pr,), daemon=True) for pr in procs]
        for r in readers:
            r.start()

        # --- background traffic starts after the clocks have synchronised ---
        bg, bg_started = [], False
        aborted = False
        while True:
            el = time.time() - t0
            if self.abort.is_set():
                aborted = True
                break
            if scen != "baseline" and not bg_started and el >= traffic_at:
                bg_started = True
                rate = MBPS_PER_HOST[LEVEL_OF[scen]] * 1e6 / 8 / PAYLOAD
                extra = (["--on", str(p.get("burst_on", 4.0)), "--off", str(p.get("burst_off", 3.0))]
                         if scen == "burst" else [])
                for h in C.BG_HOSTS:
                    bg.append(net.get(h).popen(
                        [PY, "bg_traffic.py", "--target", f"{C.SERVER_IP}:{C.BG_PORT}", "--rate", str(rate),
                         "--size", str(PAYLOAD)] + extra,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                self.log(f"Background traffic started: {3 * MBPS_PER_HOST[LEVEL_OF[scen]]:g} Mbps "
                         f"offered to a {C.SLOW_MBPS} Mbps link", "warn")
                self.bus.emit("traffic", t=el, on=True, offered_mbps=3 * MBPS_PER_HOST[LEVEL_OF[scen]])
            if all(pr.poll() is not None for pr in procs) and el > start_delay:
                break
            time.sleep(0.1)

        for pr in procs:
            if pr.poll() is None:
                pr.terminate()
        for r in readers:
            r.join(timeout=3)
        for pr in bg:
            pr.terminate()
        for pr in bg:
            try:
                pr.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pr.kill()
        time.sleep(0.6)
        done.set()

        tx1 = read_int(f"/sys/class/net/{dev_slow}/statistics/tx_bytes")
        drops1 = tc_stats(dev_slow)[2]
        events = [{"t": float(e["t"]) - t0, "path": e["path"], "rate_mbps": float(e["rate_mbps"])}
                  for e in read_csv_rows(C.ROUTE_EVENTS_CSV)[routes0:]]
        with rows_lock:
            rows_copy = sorted(rows, key=lambda r: r["t"])
        self.last_rows = rows_copy
        summary = None
        if rows_copy:
            lost = sum(r["lost"] for r in rows_copy)
            snap = {"route_events": events, "samples": [], "slow_tx_bytes": tx1 - tx0,
                    "slow": {"bg_drop": drops1 - drops0, "sync_drop": lost}, "fast": {"sync_drop": 0}}
            sa = Namespace(duration=total, steady_from=min(start_delay + 6.0, total - 2), high=C.HIGH_MBPS,
                           low=C.LOW_MBPS)
            summary = summarize(label, scen, rows_copy, snap, sa)
            summary.update(when=time.time(), duration=duration, clients=n_clients, aborted=aborted,
                           netem=self.netem, skews=skews)
        self.bus.emit("run_end", summary=summary, aborted=aborted)
        self.log("Run stopped" if aborted else
                 (f"Run finished: mean RTT {summary['mean_rtt_ms']} ms, loss {summary['loss_%']}%, "
                  f"clock error {summary['steady_synced_err_ms']} ms" if summary else "Run finished"),
                 "info" if aborted else "ok")
        return None if aborted else summary
