#!/usr/bin/env python3
"""
Single-VM adaptive SDN controller.
OpenFlow 1.3; DPID 1 = client-side switch, DPID 2 = server-side switch.
"""
import os
import time
import json
from pathlib import Path

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub


class TimeSyncController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    SERVER_IP = "10.0.0.10"
    SYNC_PORT = 5005
    BG_PORT = 6000

    # Physical/logical OpenFlow ports on s1.
    SLOW_S1 = 5
    FAST_S1 = 6

    # Corresponding ports on s2.
    SLOW_S2 = 2
    FAST_S2 = 3
    SERVER_PORT = 1

    # Host-facing ports on s1. Background hosts are included for completeness.
    HOST_PORTS = {
        "10.0.0.1": 1,
        "10.0.0.2": 2,
        "10.0.0.3": 3,
        "10.0.0.50": 4,
        "10.0.0.51": 7,
        "10.0.0.52": 8,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.policy = os.getenv("SYNC_POLICY", "dynamic").lower()
        if self.policy not in ("dynamic", "static"):
            self.logger.warning("Unknown SYNC_POLICY=%s; using dynamic", self.policy)
            self.policy = "dynamic"

        self.fast = False
        self.prev_slow_bytes = None
        self.prev_sample_time = None
        self.last_switch_time = 0.0
        self.high_threshold_mbps = float(os.getenv("HIGH_THRESHOLD_MBPS", "2.5"))
        self.low_threshold_mbps = float(os.getenv("LOW_THRESHOLD_MBPS", "0.8"))
        self.hold_down_seconds = float(os.getenv("ROUTE_HOLD_DOWN", "4"))
        self.monitor_interval = float(os.getenv("MONITOR_INTERVAL", "2"))
        self.slow_link_mbps = 0.0
        self.status_file = Path(__file__).resolve().parents[1] / "logs" / "controller_status.json"
        self._write_status()
        self.monitor_thread = hub.spawn(self._monitor)

        self.logger.info(
            "TimeSyncController started: policy=%s, server=%s, "
            "high_threshold=%.2f Mbps, low_threshold=%.2f Mbps",
            self.policy, self.SERVER_IP, self.high_threshold_mbps,
            self.low_threshold_mbps
        )

    def _write_status(self):
        """Write controller telemetry for the local dashboard."""
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "policy": self.policy,
                "path": "FAST" if self.fast else "SLOW",
                "slow_link_mbps": round(float(self.slow_link_mbps), 3),
                "high_threshold_mbps": self.high_threshold_mbps,
                "low_threshold_mbps": self.low_threshold_mbps,
                "connected_switches": sorted(int(x) for x in self.datapaths.keys()),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp = self.status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self.status_file)
        except Exception as exc:
            self.logger.warning("Could not update dashboard status: %s", exc)

    def _add_flow(self, dp, priority, match_fields, out_port):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        match = parser.OFPMatch(**match_fields)
        actions = [parser.OFPActionOutput(out_port)]
        instructions = [parser.OFPInstructionActions(
            ofp.OFPIT_APPLY_ACTIONS, actions
        )]
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, priority=priority, match=match,
            instructions=instructions
        ))

    def _replace_flow(self, dp, priority, match_fields, out_port):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        match = parser.OFPMatch(**match_fields)
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            priority=priority, match=match
        ))
        self._add_flow(dp, priority, match_fields, out_port)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info("Switch connected: dpid=%s", dp.id)
            self._write_status()
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            self.logger.warning("Switch disconnected: dpid=%s", dp.id)
            self._write_status()

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        ofp = dp.ofproto

        # Table-miss: send unknown packets to controller. Known traffic is
        # handled by the explicit rules below; static ARP entries are set
        # by the Mininet topology to avoid ARP flooding across parallel links.
        self._add_flow(dp, 0, {}, ofp.OFPP_CONTROLLER)

        if dp.id == 1:
            # Background UDP traffic is deliberately pinned to the slow path.
            self._add_flow(dp, 400, {
                "eth_type": 0x0800, "ip_proto": 17,
                "ipv4_dst": self.SERVER_IP, "udp_dst": self.BG_PORT
            }, self.SLOW_S1)

            # Synchronization requests: slow by default, changed dynamically.
            self._install_sync_s1(dp)

            # Other client-to-server IPv4 traffic uses the slow path.
            self._add_flow(dp, 100, {
                "eth_type": 0x0800, "ipv4_dst": self.SERVER_IP
            }, self.SLOW_S1)

            # Return traffic from server is delivered to its originating host.
            for ip, port in self.HOST_PORTS.items():
                self._add_flow(dp, 250, {
                    "eth_type": 0x0800, "ipv4_dst": ip
                }, port)

        elif dp.id == 2:
            # Client-to-server traffic arriving on either inter-switch link.
            self._add_flow(dp, 200, {
                "eth_type": 0x0800, "ipv4_dst": self.SERVER_IP
            }, self.SERVER_PORT)

            # Synchronization replies use the same selected path in reverse.
            self._install_sync_s2(dp)

            # Any other server-originated IPv4 traffic returns over slow path.
            self._add_flow(dp, 100, {
                "eth_type": 0x0800, "ipv4_src": self.SERVER_IP
            }, self.SLOW_S2)
        else:
            self.logger.warning("Unexpected switch DPID=%s; only DPIDs 1 and 2 are configured", dp.id)

    def _install_sync_s1(self, dp):
        out_port = self.FAST_S1 if self.fast else self.SLOW_S1
        self._replace_flow(dp, 500, {
            "eth_type": 0x0800, "ip_proto": 17,
            "ipv4_dst": self.SERVER_IP, "udp_dst": self.SYNC_PORT
        }, out_port)

    def _install_sync_s2(self, dp):
        out_port = self.FAST_S2 if self.fast else self.SLOW_S2
        self._replace_flow(dp, 500, {
            "eth_type": 0x0800, "ip_proto": 17,
            "ipv4_src": self.SERVER_IP, "udp_src": self.SYNC_PORT
        }, out_port)

    def _set_path(self, use_fast, reason):
        if self.fast == use_fast:
            return
        now = time.time()
        if now - self.last_switch_time < self.hold_down_seconds:
            return

        self.fast = use_fast
        self.last_switch_time = now
        self._write_status()
        for dpid, installer in ((1, self._install_sync_s1),
                                (2, self._install_sync_s2)):
            dp = self.datapaths.get(dpid)
            if dp is not None:
                installer(dp)

        self.logger.warning(
            "ROUTE_CHANGE path=%s reason=%s",
            "FAST (20 Mbps / 5 ms)" if use_fast else "SLOW (5 Mbps / 50 ms)",
            reason
        )

    def _monitor(self):
        while True:
            dp = self.datapaths.get(1)
            if self.policy == "dynamic" and dp is not None:
                parser = dp.ofproto_parser
                request = parser.OFPPortStatsRequest(
                    dp, 0, self.SLOW_S1
                )
                dp.send_msg(request)
            hub.sleep(self.monitor_interval)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats(self, ev):
        if self.policy != "dynamic" or ev.msg.datapath.id != 1:
            return

        now = time.time()
        for stat in ev.msg.body:
            if stat.port_no != self.SLOW_S1:
                continue

            if self.prev_slow_bytes is not None and self.prev_sample_time is not None:
                elapsed = now - self.prev_sample_time
                byte_delta = max(0, stat.tx_bytes - self.prev_slow_bytes)
                rate_mbps = (byte_delta * 8.0 / elapsed / 1_000_000.0
                             if elapsed > 0 else 0.0)
                self.slow_link_mbps = rate_mbps
                self._write_status()
                self.logger.info(
                    "METRIC slow_link_egress_mbps=%.3f path=%s",
                    rate_mbps, "FAST" if self.fast else "SLOW"
                )

                if not self.fast and rate_mbps >= self.high_threshold_mbps:
                    self._set_path(True, "slow-link utilization exceeded threshold")
                elif self.fast and rate_mbps <= self.low_threshold_mbps:
                    self._set_path(False, "slow-link utilization returned below threshold")

            self.prev_slow_bytes = stat.tx_bytes
            self.prev_sample_time = now
