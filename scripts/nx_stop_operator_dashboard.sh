#!/bin/bash
# Ferma dashboard operator (5052) e il supervisore — usiamo solo D1 jog (5053).
set +e
cd /home/unitree/go2_visual_dashboard 2>/dev/null || true
echo "$(date -Is) nx_stop_operator_dashboard: stop 5052"
pkill -f nx_dashboard_supervise.sh 2>/dev/null || true
pkill -f serve_dashboard_lite.py 2>/dev/null || true
pkill -f serve_dashboard_modular.py 2>/dev/null || true
pkill -f diagnostics_dashboard.py 2>/dev/null || true
sleep 1
fuser -k /dev/video0 /dev/video1 /dev/video2 /dev/video3 /dev/video4 /dev/video5 /dev/video6 /dev/video7 /dev/video12 2>/dev/null || true
sleep 1
if curl -sf --max-time 2 "http://127.0.0.1:5052/api/health" >/dev/null 2>&1; then
  echo "WARN: 5052 ancora attiva"
  exit 1
fi
echo "OK: dashboard operator 5052 spenta"
exit 0
