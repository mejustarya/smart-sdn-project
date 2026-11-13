#!/usr/bin/env bash
# Start the Time Sync Lab dashboard (needs root: it controls Mininet).
# Usage: ./scripts/start_dashboard.sh [--port 8000] [--autostart] [--datapath user]
cd "$(dirname "$0")/.." || exit 1
if [ "$(id -u)" -ne 0 ]; then exec sudo -E env "PATH=$PATH" /usr/bin/python3 dashboard/server.py "$@"; fi
exec /usr/bin/python3 dashboard/server.py "$@"
