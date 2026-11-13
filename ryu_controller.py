"""Ryu OpenFlow 1.3 controller: congestion-aware path selection for time-sync traffic.

Run with:   PYTHONPATH=$PWD ryu-manager --ofp-tcp-listen-port 6653 ryu_controller.py

What it does
  * Installs forwarding rules proactively (static ARP is used in the topology).
  * Time-sync packets (UDP port 5005) get their own high-priority flow on s1 (requests)
    and s2 (replies). The flow marks DSCP EF and outputs on the currently selected
    path, SLOW (default) or FAST. Everything else, including background traffic
    (UDP 6000), is pinned to the SLOW path.
  * Every MONITOR_INTERVAL seconds it asks s1 for port statistics of the SLOW-link
    port, converts tx_bytes into Mbps and applies RoutePolicy (thresholds,
    hysteresis, hold-down). A route change rewrites the sync flows on both switches.
  * Logs samples and route changes to CSV files, and the current state to a JSON file.
  * logs/control.json lets the experiment runner change policy or reset the route
    without restarting Ryu: {"policy": "dynamic" | "static", "token": <any new value>}.
"""
import csv
import json
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

import sdn_config as C
from route_policy import RoutePolicy

ETH_IP, PROTO_UDP = 0x0800, 17
PRIO_SYNC, PRIO_HOST, PRIO_DEFAULT = 20, 10, 5


class SyncCongestionController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}                                  # dpid -> datapath
        self.policy = RoutePolicy(C.HIGH_MBPS, C.LOW_MBPS, C.HOLD_DOWN, C.DEFAULT_POLICY)
        self.prev = None                               # (tx_bytes, time) of last sample
        self.skip_next = False                         # drop the sample straddling a reset
        self.token = None
        os.makedirs(C.LOG_DIR, exist_ok=True)
        self._init_csv(C.ROUTE_EVENTS_CSV, ["t", "path", "rate_mbps"])
        self._init_csv(C.SAMPLES_CSV, ["t", "slow_mbps", "path"])
        hub.spawn(self._monitor_loop)
        hub.spawn(self._control_loop)
        self.logger.info("policy=%s high=%.2f low=%.2f hold-down=%.1fs interval=%.1fs",
                         C.DEFAULT_POLICY, C.HIGH_MBPS, C.LOW_MBPS, C.HOLD_DOWN, C.MONITOR_INTERVAL)

    # ------------------------------------------------------------ logging --
    @staticmethod
    def _init_csv(path, header):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)

    @staticmethod
    def _append_csv(path, row):
        with open(path, "a", newline="") as f:
            csv.writer(f).writerow(row)

    def _write_status(self, rate):
        status = {"policy": self.policy.policy, "path": self.policy.path,
                  "slow_mbps": round(rate, 3), "route_changes": self.policy.changes,
                  "high_mbps": C.HIGH_MBPS, "low_mbps": C.LOW_MBPS,
                  "hold_down_s": C.HOLD_DOWN, "updated": time.time()}
        tmp = C.STATUS_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, C.STATUS_JSON)

    # -------------------------------------------------------- flow rules ---
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _on_switch(self, ev):
        dp = ev.msg.datapath
        self.dps[dp.id] = dp
        self.logger.info("switch s%d connected", dp.id)
        self._delete_all(dp)
        self._install_base_flows(dp)
        self._install_sync_flow(dp, self.policy.path)

    @staticmethod
    def _delete_all(dp):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=parser.OFPMatch()))

    @staticmethod
    def _add_flow(dp, priority, match, actions):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst))

    def _install_base_flows(self, dp):
        parser = dp.ofproto_parser
        if dp.id == 1:                                              # client-side switch
            for ip, port in C.S1_HOST_PORTS.items():                # deliver to local hosts
                self._add_flow(dp, PRIO_HOST, parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=ip),
                               [parser.OFPActionOutput(port)])
            self._add_flow(dp, PRIO_HOST,                           # all other traffic to the
                           parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=C.SERVER_IP),   # server: SLOW
                           [parser.OFPActionOutput(C.S1_SLOW_PORT)])
        elif dp.id == 2:                                            # server-side switch
            self._add_flow(dp, PRIO_HOST, parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=C.SERVER_IP),
                           [parser.OFPActionOutput(C.S2_SERVER_PORT)])
            self._add_flow(dp, PRIO_DEFAULT,                        # everything else: back over SLOW
                           parser.OFPMatch(eth_type=ETH_IP, ipv4_dst=("10.0.0.0", "255.255.255.0")),
                           [parser.OFPActionOutput(C.S2_SLOW_PORT)])

    def _install_sync_flow(self, dp, path):
        """(Re)write the sync flow: ADD with the same match replaces the old actions."""
        parser = dp.ofproto_parser
        out_port = C.PATH_PORTS[path][dp.id]
        if dp.id == 1:       # requests: client -> server, UDP dst port 5005
            match = parser.OFPMatch(eth_type=ETH_IP, ip_proto=PROTO_UDP,
                                    ipv4_dst=C.SERVER_IP, udp_dst=C.SYNC_PORT)
        elif dp.id == 2:     # replies: server -> client, UDP src port 5005
            match = parser.OFPMatch(eth_type=ETH_IP, ip_proto=PROTO_UDP,
                                    ipv4_src=C.SERVER_IP, udp_src=C.SYNC_PORT)
        else:
            return
        actions = [parser.OFPActionSetField(ip_dscp=C.DSCP_EF), parser.OFPActionOutput(out_port)]
        self._add_flow(dp, PRIO_SYNC, match, actions)

    def _apply_route(self, path):
        for dp in self.dps.values():
            self._install_sync_flow(dp, path)

    # --------------------------------------------------- congestion monitor --
    def _monitor_loop(self):
        while True:
            hub.sleep(C.MONITOR_INTERVAL)
            dp = self.dps.get(1)
            if dp is not None:
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, C.S1_SLOW_PORT))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _on_port_stats(self, ev):
        now = time.time()
        for st in ev.msg.body:
            if st.port_no != C.S1_SLOW_PORT:
                continue
            previous, self.prev = self.prev, (st.tx_bytes, now)
            if previous is None:
                continue
            if self.skip_next:                 # interval overlapped a reset: not meaningful
                self.skip_next = False
                continue
            rate = (st.tx_bytes - previous[0]) * 8 / max(now - previous[1], 1e-6) / 1e6
            new_path = self.policy.update(rate, now)
            if new_path:
                self._apply_route(new_path)
                self._append_csv(C.ROUTE_EVENTS_CSV, [now, new_path, round(rate, 4)])
                self.logger.info("ROUTE CHANGE -> %s (slow link %.2f Mbps)", new_path, rate)
            self._append_csv(C.SAMPLES_CSV, [now, round(rate, 4), self.policy.path])
            self._write_status(rate)
            self.logger.info("slow link %.2f Mbps, sync on %s", rate, self.policy.path)

    # ------------------------------------------------ experiment control ----
    def _control_loop(self):
        while True:
            hub.sleep(0.4)
            try:
                with open(C.CONTROL_FILE) as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                continue
            if cfg.get("token") == self.token:
                continue
            self.token = cfg.get("token")
            self.policy.reset(cfg.get("policy", self.policy.policy))
            self.skip_next = True
            self._apply_route("SLOW")
            self.logger.info("control: policy=%s, route reset to SLOW", self.policy.policy)
