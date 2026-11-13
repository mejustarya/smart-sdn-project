#!/usr/bin/env bash
# One-time setup on Ubuntu (22.04 tested by your SDN README; the recipe below was verified with
# Python 3.9 + Ryu 4.34 + Open vSwitch 3.3 + Mininet 2.3).
#   bash scripts/setup_ubuntu.sh
set -e

sudo apt update
sudo apt install -y mininet openvswitch-switch iproute2 ethtool iputils-ping net-tools \
                    python3 python3-venv python3-pip python3-matplotlib build-essential
sudo systemctl enable --now openvswitch-switch 2>/dev/null || sudo /usr/share/openvswitch/scripts/ovs-ctl start

# Ryu lives in its own virtualenv (same path your SDN README uses). Python 3.9 is preferred;
# use `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.9 python3.9-venv` if you have it.
PY=$(command -v python3.9 || command -v python3)
"$PY" -m venv ~/ryu39-venv
# shellcheck disable=SC1090
source ~/ryu39-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install 'setuptools<66' wheel pbr
python -m pip install --no-build-isolation ryu 'eventlet==0.30.2'
ryu-manager --version

echo
echo "Checking the kernel pieces the experiment needs:"
sudo modprobe sch_htb  2>/dev/null && echo "  sch_htb   : ok" || echo "  sch_htb   : missing (priority queue will not work)"
sudo modprobe sch_netem 2>/dev/null && echo "  sch_netem : ok" || echo "  sch_netem : missing (run with --no-netem)"
sudo modprobe openvswitch 2>/dev/null && echo "  openvswitch kernel module: ok" || echo "  openvswitch kernel module: missing (run with --datapath user)"
echo
echo "Setup done. Next: see README.md, section 'Run it'."
