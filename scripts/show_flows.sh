#!/usr/bin/env bash
# Show what the controller installed and what the switches are counting (good for a demo).
# Run while the topology is up.   sudo ./scripts/show_flows.sh
for sw in s1 s2; do
    echo "=============== $sw: OpenFlow rules (priority 20 = time-sync flow) ==============="
    ovs-ofctl -O OpenFlow13 dump-flows "$sw"
done
echo "=============== s1 port counters (s1-eth7 = SLOW link, s1-eth8 = FAST link) ==============="
ovs-ofctl -O OpenFlow13 dump-ports s1
echo "=============== controller status ==============="
cat "$(dirname "$0")/../logs/controller_status.json" 2>/dev/null || echo "(no status file yet)"
echo
echo "=============== route changes ==============="
cat "$(dirname "$0")/../logs/route_events.csv" 2>/dev/null
