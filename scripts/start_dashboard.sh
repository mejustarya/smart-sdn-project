#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HOME/ryu39-venv/bin/activate"
cd "$ROOT"
python -m pip install -r requirements.txt
echo "Dashboard: http://127.0.0.1:5000"
exec python dashboard/app.py
