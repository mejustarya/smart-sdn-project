#!/usr/bin/env bash
# Start the Ryu controller (Terminal 1). Ctrl+C to stop.
#   ./scripts/start_controller.sh
# Policy knobs (environment variables, defaults shown):
#   SYNC_POLICY=dynamic HIGH_THRESHOLD_MBPS=2.5 LOW_THRESHOLD_MBPS=0.8 ROUTE_HOLD_DOWN=4 MONITOR_INTERVAL=2
# NOTE: `sudo mn -c` kills ryu-manager. Run any Mininet cleanup BEFORE starting the controller.
cd "$(dirname "$0")/.." || exit 1
if [ -f "$HOME/ryu39-venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$HOME/ryu39-venv/bin/activate"
fi
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
export SYNC_POLICY="${SYNC_POLICY:-dynamic}"
export HIGH_THRESHOLD_MBPS="${HIGH_THRESHOLD_MBPS:-2.5}"
export LOW_THRESHOLD_MBPS="${LOW_THRESHOLD_MBPS:-0.8}"
export ROUTE_HOLD_DOWN="${ROUTE_HOLD_DOWN:-4}"
export MONITOR_INTERVAL="${MONITOR_INTERVAL:-2}"
mkdir -p logs
ryu-manager --ofp-tcp-listen-port 6653 ryu_controller.py 2>&1 | tee logs/controller.log
