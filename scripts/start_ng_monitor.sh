#!/bin/sh
# Kill any existing monitor loop
for pid in $(grep -rl monitor_ng_m2m /proc/*/cmdline 2>/dev/null | sed 's|/proc/||;s|/cmdline||' | sort -u); do
    kill "$pid" 2>/dev/null || true
done
sleep 1
# Start fresh
python3 /app/scripts/monitor_ng_m2m.py --loop >> /tmp/ng_monitor.log 2>&1 &
echo "NG monitor started, pid=$!"
