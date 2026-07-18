#!/bin/sh
for pid in $(grep -rl monitor_ng_m2m /proc/*/cmdline 2>/dev/null | sed 's|/proc/||;s|/cmdline||' | sort -u); do
    kill "$pid" 2>/dev/null || true
done
echo "NG monitor stopped"
