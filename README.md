# Smart SDN Network Management & Adaptive Traffic Optimization System

A software-defined networking (SDN) project that keeps time synchronization accurate by **automatically rerouting traffic from a congested "slow" path to a "fast" path**. A Ryu controller monitors link utilisation, Mininet simulates the network and congestion, and a Flask dashboard lets you run and analyse experiments.

> Course project, Deliverable 2 | Team: **TODO: team name**

---

## 1. Objectives

1. Build an SDN testbed (Ryu + Mininet + Open vSwitch) on a single Ubuntu VM.
2. Run a time-synchronization application (UDP client/server written in C) across the network.
3. Generate different kinds of congestion (UDP levels, TCP, bursts, delay and packet loss).
4. Let the controller **detect congestion and reroute** time-sync traffic automatically, with a hold-down timer to avoid flapping.
5. Provide a **web dashboard** to control experiments, monitor the network live, and export results (history, CSV, PDF report).

---

## 2. Features

| Area | What it does |
|---|---|
| SDN controller (Ryu) | Reads switch statistics, measures slow-link egress rate, switches the sync path SLOW <-> FAST using thresholds (high 2.5 Mbps, low 0.8 Mbps) and a 4 s hold-down timer. Logs `METRIC` and `ROUTE_CHANGE` events. |
| Topology (Mininet) | 2 OpenFlow switches (s1, s2), 7 hosts, a slow link (bandwidth-limited, 50 ms delay via `tc` htb/netem) and a fast link. |
| Time sync (C) | `time_server` on h4 (UDP 5005 and 6000); `time_client` on h1, h2, h3 write RTT/offset to `logs/hX-sync.csv`. |
| Congestion generators | `background_sender` (UDP) on h5, h6, h7 with **Low / Moderate / High / Extreme** levels; **TCP flows** (iperf3); **burst traffic** (4 s on / 3 s off); configurable **delay, jitter and loss** on the slow link. |
| Dashboard (Flask) | Live topology (link colours green/amber/red, active path animated), path status card, slow-link throughput chart, RTT / jitter / loss / sync-accuracy cards, switch port counters, Ryu event log, Start/Stop sync, manual controls. |
| Experiment manager | One-click automatic experiment: Baseline, Low, Moderate, High, Extreme, Recovery (optional TCP and Burst stages). |
| Reports | Experiment history in SQLite (`results/experiments.db`), per-run CSV, all-runs CSV, and an auto-generated **PDF report** including "RTT slow -> fast" improvement after reroute. |

---

## 3. Architecture / Design
<img width="1600" height="1100" alt="image" src="https://github.com/user-attachments/assets/599e5cfc-312c-48ac-8507-205ffefa4288" />



**Network topology (inside Mininet)**
<img width="1600" height="1050" alt="image" src="https://github.com/user-attachments/assets/92625e6e-e3e7-415a-a900-9c751b73a7f5" />


**Control loop**

1. Time-sync clients send UDP packets to the server (default path: SLOW).
2. Background generators push traffic over the slow link.
3. Ryu measures slow-link egress rate. If it exceeds **2.5 Mbps**, it installs flows to move sync traffic to **FAST** (`ROUTE_CHANGE path=FAST`).
4. When the rate falls below **0.8 Mbps** (and the 4 s hold-down expires), traffic returns to **SLOW**.
5. The dashboard shows the path, rates, RTT before/after, and stores the run.

### Repository structure

```
smart-sdn-project/
├── controller/sdn_controller.py     # Ryu app (monitoring + rerouting policy)
├── topology/network_topology.py     # Mininet topology + control socket
├── dashboard/
│   ├── app.py                       # Flask backend, experiments, CSV/PDF
│   ├── templates/index.html
│   └── static/{style.css,dashboard.js}
├── scripts/
│   ├── start_controller.sh
│   ├── start_topology.sh
│   ├── start_dashboard.sh
│   └── verify.sh                    # end-to-end health check
├── server.c  client.c  background_sender.c   # C programs
├── Makefile
├── requirements.txt
├── docs/screenshots/                # result screenshots
├── results/                         # experiments.db, reports (generated)
└── logs/                            # generated at runtime
```

---

## 4. Setup & Run

**Requirements:** Ubuntu VM, Mininet, Open vSwitch, Python 3 venv with Ryu (`~/ryu39-venv`), `gcc`/`make`, `iperf3`.

```bash
# build C programs
cd smart-sdn-project
make
chmod +x time_server time_client background_sender

sudo apt install -y iperf3

# allow the dashboard to read port counters (replace YOUR_USER)
echo 'YOUR_USER ALL=(root) NOPASSWD: /usr/bin/ovs-ofctl' | sudo tee /etc/sudoers.d/smart-sdn
sudo chmod 440 /etc/sudoers.d/smart-sdn
```

Start three terminals **in this order**:

```bash
# Terminal 1: Ryu controller
source ~/ryu39-venv/bin/activate
cd smart-sdn-project && ./scripts/start_controller.sh

# Terminal 2: Mininet (keep the mininet> prompt open)
cd smart-sdn-project && sudo -E ./scripts/start_topology.sh

# Terminal 3: Dashboard
source ~/ryu39-venv/bin/activate
cd smart-sdn-project && ./scripts/start_dashboard.sh
```

Open **http://127.0.0.1:5000**.

**Quick test:** click *Start sync*, then *EXTREME*. After ~10 s the "Selected sync path" card changes SLOW -> FAST and Ryu logs `ROUTE_CHANGE path=FAST`. Click *Stop traffic* to return to SLOW. Run `./scripts/verify.sh` for a full health check.

**Cleanup between runs:** `sudo mn -c`

---

## 5. Experiments

| Test | Simulates | Shows |
|---|---|---|
| UDP levels (Low-Extreme) | Steady traffic of different strengths | Controller reacts when slow link gets busy |
| TCP flows (iperf3) | Real applications, e.g. downloads | Works with realistic traffic |
| Burst traffic | Spikes (4 s on / 3 s off) | Hold-down timer prevents path flapping |
| Delay + loss | Poor-quality link | Sync RTT is high on slow path; rerouting restores low RTT (~250 ms -> ~10 ms) |
| Full automatic experiment | All of the above in sequence | Generates history, CSV and PDF report |

**Known limitation:** the controller currently reroutes based on slow-link utilisation only, not on measured RTT or packet loss. Future work: RTT/loss-aware rerouting.

---

## 6. Screenshots

Place images in `docs/screenshots/` and keep these names:

| File | Shows |
|---|---|
| `01-dashboard-idle.png` | Dashboard before traffic (path SLOW, switches 2/2) |
| `02-dashboard-extreme-fast.png` | EXTREME running, path changed to FAST |
| `03-ryu-route-change.png` | Ryu terminal with `ROUTE_CHANGE path=FAST` |
| `04-verify-output.png` | `verify.sh` output |
| `05-throughput-chart.png` | Slow-link throughput chart |
| `06-rtt-chart.png` | RTT chart colored by path |
| `07-experiment-manager.png` | Automatic experiment in progress |
| `08-history-table.png` | Experiment history and reports |
| `09-pdf-report.png` | Generated PDF report |
| `10-delay-loss-test.png` | 200 ms / 10% loss test, RTT before vs after reroute |

![Dashboard idle](docs/screenshots/01-dashboard-idle.png)
![Reroute to FAST](docs/screenshots/02-dashboard-extreme-fast.png)
![Ryu log](docs/screenshots/03-ryu-route-change.png)
![PDF report](docs/screenshots/09-pdf-report.png)

---

## 7. Video Demo

TODO: add link (YouTube unlisted / Google Drive): `https://...`

Suggested flow (3 to 5 min): start the 3 components, show dashboard, Start sync, EXTREME, path flips to FAST, Stop traffic, run the automatic experiment, open the PDF report.

---

## 8. Team Contributions

| Member | Roll No. | Contributions |
|---|---|---|
| TODO: Name 1 | | e.g. Ryu controller logic, path selection policy, hold-down timer |
| TODO: Name 2 | | e.g. Mininet topology, slow/fast links, tc delay/loss, traffic generators |
| TODO: Name 3 | | e.g. C time-sync client/server, RTT/offset logging |
| TODO: Name 4 | | e.g. Flask dashboard, charts, experiment manager, PDF/CSV reports |
| All | | Testing, documentation, demo, presentation |

---

## 9. Tech Stack

Ryu (OpenFlow 1.3) · Mininet · Open vSwitch · C (UDP sockets) · Python / Flask · Chart.js · SQLite · reportlab · iperf3 · Linux `tc` (htb/netem)
