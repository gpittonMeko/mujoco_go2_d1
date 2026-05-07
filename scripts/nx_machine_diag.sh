#!/bin/bash
# Diagnostica rapida Jetson: carico, RAM, disco, zombie, top processi, dashboard.
# I processi zombie (STAT Z) non si «uccidono» utilmente: sono già terminati; va ripulito il parent (PPID) che non fa wait.
set +e
echo "=== nx_machine_diag $(date -Is) host=$(hostname) ==="
echo ""
echo "=== uptime / load ==="
uptime 2>/dev/null || true
echo ""
echo "=== memory ==="
free -h 2>/dev/null || true
echo ""
echo "=== disk (/, /home) ==="
df -h / /home 2>/dev/null || df -h /
echo ""
echo "=== zombies (STAT contains Z) ==="
ps -eo pid,ppid,user,stat,cmd 2>/dev/null | awk '$4 ~ /Z/ {print}' | head -40
ZC=$(ps -eo stat 2>/dev/null | grep -c '^Z' || true)
echo "zombie_stat_Z_count: ${ZC}"
echo ""
echo "=== top CPU (15) ==="
ps aux --sort=-%cpu 2>/dev/null | head -16
echo ""
echo "=== top RSS memory (15) ==="
ps aux --sort=-rss 2>/dev/null | head -16
echo ""
echo "=== dashboard / supervise ==="
pgrep -af 'serve_dashboard_modular|diagnostics_dashboard|nx_dashboard_supervise' 2>/dev/null || echo "(none)"
echo ""
echo "=== python3 cmdlines (first 20) ==="
pgrep -af python3 2>/dev/null | head -20
echo ""
echo "=== tail dmesg (OOM hints) ==="
dmesg -T 2>/dev/null | tail -n 12 || echo "(dmesg non disponibile)"
echo ""
echo "=== end nx_machine_diag ==="
