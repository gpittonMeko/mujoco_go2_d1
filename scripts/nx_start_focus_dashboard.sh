#!/usr/bin/env bash
set -euo pipefail

cd "${GO2_DEPLOY_REMOTE_BASE:-/home/unitree/go2_visual_dashboard}"

if [ -f scripts/nx_dashboard_env.sh ]; then
  # shellcheck disable=SC1091
  source scripts/nx_dashboard_env.sh
fi

export GO2_LOCAL="${GO2_LOCAL:-1}"
export GO2_ENABLE_BASE_MOTION="${GO2_ENABLE_BASE_MOTION:-1}"
export GO2_ENABLE_REAL_ARM="${GO2_ENABLE_REAL_ARM:-1}"
export GO2_FOCUS_PORT="${GO2_FOCUS_PORT:-5056}"
export GO2_DASHBOARD_PORT="$GO2_FOCUS_PORT"
export GO2_FOCUS_PICK_YOLO="${GO2_FOCUS_PICK_YOLO:-0}"

# Non interrompere il writer DDS del D1 mentre il braccio è in coppia: il
# daemon è figlio di Flask e un restart crea una finestra senza hold.
if [ "${GO2_ALLOW_ARM_COUPLED_RESTART:-0}" != "1" ]; then
  ARM_COUPLED="$(curl -fsS --max-time 2 "http://127.0.0.1:${GO2_FOCUS_PORT}/api/arm/status" 2>/dev/null | python3 -c 'import json,sys; print(1 if json.load(sys.stdin).get("arm_coupled") else 0)' 2>/dev/null || echo 0)"
  if [ "$ARM_COUPLED" = "1" ]; then
    echo "REFUSE_RESTART_ARM_COUPLED: DDS publisher/hold must stay alive" >&2
    exit 42
  fi
fi

if [ "${GO2_FOCUS_STOP_OPERATOR:-1}" = "1" ]; then
  pkill -f '[s]cripts/nx_dashboard_supervise.sh' 2>/dev/null || true
  pkill -f '[s]cripts/serve_dashboard_lite.py' 2>/dev/null || true
fi
pkill -f '[s]cripts/nx_focus_dashboard_supervise.sh' 2>/dev/null || true
pkill -f '[s]cripts/serve_focus_dashboard.py' 2>/dev/null || true
pkill -f '[s]cripts/serve_go2_motor_health.py' 2>/dev/null || true

export HERMES_OPERATOR_URL="http://127.0.0.1:${GO2_FOCUS_PORT}"
export GO2_HERMES_INTEGRATED=1

nohup bash scripts/nx_focus_dashboard_supervise.sh >> focus_supervise.log 2>&1 &
echo $! > focus_dashboard.pid
sleep 1
python3 - <<'PY'
import os, urllib.request
port = os.environ.get("GO2_FOCUS_PORT", "5056")
url = f"http://127.0.0.1:{port}/api/health"
with urllib.request.urlopen(url, timeout=10) as r:
    print("FOCUS_HEALTH_OK", r.status)
PY
