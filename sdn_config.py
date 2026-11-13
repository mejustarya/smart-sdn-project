"""Shared constants for the Mininet topology, the Ryu controller and the experiment runner.

Topology (all ports are fixed so OpenFlow rules can name them):

    h1 h2 h3 (sync clients)  --\\
    h5 h6 h7 (background)    ---- s1 ==== SLOW link (5 Mbps, 50 ms) ==== s2 ---- h4 (time server)
                                    \\=== FAST link (20 Mbps, 5 ms) ====/
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- addressing ------------------------------------------------------------
SERVER_IP = "10.0.0.10"
SYNC_PORT = 5005          # UDP time-sync traffic
BG_PORT = 6000            # UDP background traffic
HOSTS = {"h1": "10.0.0.1", "h2": "10.0.0.2", "h3": "10.0.0.3", "h4": SERVER_IP,
         "h5": "10.0.0.50", "h6": "10.0.0.51", "h7": "10.0.0.52"}
CLIENT_HOSTS = ["h1", "h2", "h3"]
BG_HOSTS = ["h5", "h6", "h7"]

# ---- OpenFlow port numbers --------------------------------------------------
S1_HOST_PORTS = {"10.0.0.1": 1, "10.0.0.2": 2, "10.0.0.3": 3,
                 "10.0.0.50": 4, "10.0.0.51": 5, "10.0.0.52": 6}
S1_SLOW_PORT, S1_FAST_PORT = 7, 8
S2_SERVER_PORT, S2_SLOW_PORT, S2_FAST_PORT = 1, 2, 3
# path name -> {switch dpid: output port towards the other switch}
PATH_PORTS = {"SLOW": {1: S1_SLOW_PORT, 2: S2_SLOW_PORT},
              "FAST": {1: S1_FAST_PORT, 2: S2_FAST_PORT}}

# ---- link characteristics ---------------------------------------------------
SLOW_MBPS, SLOW_DELAY_MS = 5, 50
FAST_MBPS, FAST_DELAY_MS = 20, 5
QUEUE_PKTS = 60           # buffer per queue, in packets (about 100 ms at 5 Mbps)
DSCP_EF = 46              # marking the controller applies to sync packets

# ---- controller policy (environment variables override the defaults) -------
def _f(name, default):
    return float(os.environ.get(name, default))


HIGH_MBPS = _f("HIGH_THRESHOLD_MBPS", 2.5)
LOW_MBPS = _f("LOW_THRESHOLD_MBPS", 0.8)
HOLD_DOWN = _f("ROUTE_HOLD_DOWN", 4)
MONITOR_INTERVAL = _f("MONITOR_INTERVAL", 2)
DEFAULT_POLICY = os.environ.get("SYNC_POLICY", "dynamic")   # dynamic | static

# ---- files shared between controller and experiment runner -----------------
LOG_DIR = os.environ.get("SDN_LOG_DIR", os.path.join(HERE, "logs"))
CONTROL_FILE = os.path.join(LOG_DIR, "control.json")
ROUTE_EVENTS_CSV = os.path.join(LOG_DIR, "route_events.csv")
SAMPLES_CSV = os.path.join(LOG_DIR, "controller_samples.csv")
STATUS_JSON = os.path.join(LOG_DIR, "controller_status.json")
