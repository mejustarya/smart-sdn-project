#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ ! -x "$ROOT/time_server" || ! -x "$ROOT/time_client" || ! -x "$ROOT/background_sender" ]]; then
  make
fi
sudo -E python3 topology/network_topology.py "$@"
