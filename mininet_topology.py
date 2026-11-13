"""Mininet topology and link queues for the time-sync congestion project.

    h1 h2 h3  (sync clients)  ->  s1  ==SLOW 5 Mbps / 50 ms==  s2  <-  h4 (time server)
    h5 h6 h7  (background)    ->      ==FAST 20 Mbps / 5 ms==

* Switches are Open vSwitch speaking OpenFlow 1.3 to a remote Ryu controller.
* Inter-switch links are shaped with Linux tc: an HTB root (rate limit) with leaf
  queues of `netem delay` (propagation delay + finite buffer).
* Optional priority: two HTB classes. DSCP-EF packets (the controller marks sync
  traffic EF) go to a strict-priority class, everything else to a best-effort class.

Interactive use (needs root):  sudo python3 mininet_topology.py [--priority]
"""
import argparse
import re

from mininet.cli import CLI
from mininet.link import Link
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

import sdn_config as C


def build_network(controller_ip="127.0.0.1", controller_port=6653, datapath="kernel"):
    net = Mininet(controller=None, link=Link, autoSetMacs=True, build=False)
    net.addController("c0", controller=RemoteController, ip=controller_ip, port=controller_port)
    sw = {}
    for name, dpid in (("s1", "0000000000000001"), ("s2", "0000000000000002")):
        sw[name] = net.addSwitch(name, cls=OVSSwitch, dpid=dpid, protocols="OpenFlow13",
                                 datapath=datapath, failMode="secure")
    hosts = {n: net.addHost(n, ip=f"{ip}/24") for n, ip in C.HOSTS.items()}

    for ip, port in C.S1_HOST_PORTS.items():             # clients and background hosts on s1
        name = next(n for n, v in C.HOSTS.items() if v == ip)
        net.addLink(hosts[name], sw["s1"], port2=port)
    net.addLink(hosts["h4"], sw["s2"], port2=C.S2_SERVER_PORT)          # server on s2
    net.addLink(sw["s1"], sw["s2"], port1=C.S1_SLOW_PORT, port2=C.S2_SLOW_PORT)   # SLOW path
    net.addLink(sw["s1"], sw["s2"], port1=C.S1_FAST_PORT, port2=C.S2_FAST_PORT)   # FAST path
    return net


def disable_offloads(net):
    """Turn off checksum offload on every Mininet interface.

    With Open vSwitch's *userspace* datapath, a host's UDP/TCP packet still carries a
    "to be computed" checksum when OVS re-sends it, so the next host drops it (ping still
    works because ICMP checksums are done in software). Harmless with the kernel datapath.
    Needs the `ethtool` package.
    """
    missing = False
    for node in list(net.hosts) + list(net.switches):
        for intf in node.intfs.values():
            if intf.name == "lo":
                continue
            out = node.cmd(f"ethtool -K {intf.name} tx off rx off 2>&1")
            missing = missing or "not found" in out
    if missing:
        print("WARNING: ethtool is not installed (sudo apt install ethtool); with the userspace "
              "datapath UDP packets may be dropped")


def _tc(node, cmd, check=True):
    out = node.cmd("tc " + cmd)
    if check and ("rror" in out or "RTNETLINK" in out):
        raise RuntimeError(f"tc {cmd}\n{out.strip()}")
    return out


def setup_qos(net, priority=False, netem=True, queue_pkts=C.QUEUE_PKTS):
    """(Re)configure the queues on both directions of the SLOW and FAST links.

    priority=False: one FIFO queue shared by all traffic.
    priority=True : DSCP EF -> strict-priority class, rest -> best-effort class.
    netem=False   : skip the delay (for kernels without sch_netem); queues are pfifo.
    """
    spec = [("s1", C.S1_SLOW_PORT, C.SLOW_MBPS, C.SLOW_DELAY_MS),
            ("s2", C.S2_SLOW_PORT, C.SLOW_MBPS, C.SLOW_DELAY_MS),
            ("s1", C.S1_FAST_PORT, C.FAST_MBPS, C.FAST_DELAY_MS),
            ("s2", C.S2_FAST_PORT, C.FAST_MBPS, C.FAST_DELAY_MS)]
    for swname, port, rate, delay in spec:
        sw = net.get(swname)
        dev = sw.intfs[port].name

        def leaf(parent, handle, limit):
            kind = f"netem delay {delay}ms limit {limit}" if netem else f"pfifo limit {limit}"
            _tc(sw, f"qdisc add dev {dev} parent {parent} handle {handle} {kind}")

        _tc(sw, f"qdisc del dev {dev} root 2>/dev/null", check=False)
        _tc(sw, f"qdisc add dev {dev} root handle 1: htb default 20")
        _tc(sw, f"class add dev {dev} parent 1: classid 1:1 htb rate {rate}mbit ceil {rate}mbit")
        if priority:
            _tc(sw, f"class add dev {dev} parent 1:1 classid 1:10 htb rate {rate * 0.2:g}mbit "
                    f"ceil {rate}mbit prio 0")
            _tc(sw, f"class add dev {dev} parent 1:1 classid 1:20 htb rate {rate * 0.8:g}mbit "
                    f"ceil {rate}mbit prio 1")
            leaf("1:10", "10:", 1000)
            leaf("1:20", "20:", queue_pkts)
            # DSCP EF (46) -> TOS byte 0xb8; mask 0xfc ignores the ECN bits
            _tc(sw, f"filter add dev {dev} parent 1: protocol ip prio 1 u32 "
                    f"match ip tos 0xb8 0xfc flowid 1:10")
        else:
            _tc(sw, f"class add dev {dev} parent 1:1 classid 1:20 htb rate {rate}mbit ceil {rate}mbit")
            leaf("1:20", "20:", queue_pkts)


def tc_drops(net, swname="s1", port=C.S1_SLOW_PORT):
    """Total packets dropped by the queues of one interface (all qdisc levels summed)."""
    sw = net.get(swname)
    out = sw.cmd(f"tc -s qdisc show dev {sw.intfs[port].name}")
    return sum(int(x) for x in re.findall(r"dropped (\d+)", out))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Start the topology with a Mininet CLI")
    ap.add_argument("--controller", default="127.0.0.1:6653")
    ap.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    ap.add_argument("--priority", action="store_true", help="priority queue for DSCP-EF traffic")
    ap.add_argument("--no-netem", action="store_true", help="skip tc netem delay")
    a = ap.parse_args()
    setLogLevel("info")
    ip, port = a.controller.split(":")
    net = build_network(ip, int(port), a.datapath)
    net.start()
    net.staticArp()
    disable_offloads(net)
    net.waitConnected(timeout=30)
    setup_qos(net, priority=a.priority, netem=not a.no_netem)
    CLI(net)
    net.stop()
