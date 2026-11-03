#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HOME/ryu39-venv/bin/activate"
cd "$ROOT"
export SYNC_POLICY="${SYNC_POLICY:-dynamic}"
export HIGH_THRESHOLD_MBPS="${HIGH_THRESHOLD_MBPS:-2.5}"
export LOW_THRESHOLD_MBPS="${LOW_THRESHOLD_MBPS:-0.8}"
mkdir -p "$ROOT/logs"
ryu-manager --ofp-tcp-listen-port 6633 controller/sdn_controller.py 2>&1 | tee -a "$ROOT/logs/controller.log"
