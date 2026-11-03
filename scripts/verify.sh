#!/usr/bin/env bash
# Health check for the running Smart SDN stack. Run in a 4th terminal while Ryu, Mininet and the dashboard are up.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
echo "== 1. Ryu listening on OpenFlow port 6633 =="; ss -lnt | grep -q ':6633' && echo OK || echo "NOT LISTENING - start ./scripts/start_controller.sh"
echo "== 2. Mininet control socket (8765) =="; ss -lnt | grep -q ':8765' && echo OK || echo "NOT LISTENING - is the Mininet CLI running?"
echo "== 3. Dashboard (5000) =="; curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:5000/api/series || echo "dashboard not reachable"
echo "== 4. Controller status =="; cat logs/controller_status.json 2>/dev/null || echo "no status file yet"
echo; echo "== 5. Last METRIC lines =="; grep METRIC logs/controller.log 2>/dev/null | tail -5
echo "== 6. Routing decisions (ROUTE_CHANGE) =="; grep ROUTE_CHANGE logs/controller.log 2>/dev/null | tail -6 || true
echo "== 7. Slow link shaping on s1-eth5 =="; sudo tc qdisc show dev s1-eth5
echo "== 8. Sync clients (last rows of h1) =="; tail -4 logs/h1-sync.csv 2>/dev/null
echo "== 9. Flow rules on s1 =="; sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | cut -c1-170
