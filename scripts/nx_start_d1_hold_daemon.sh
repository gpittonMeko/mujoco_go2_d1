#!/usr/bin/env bash
set -euo pipefail

cd "${GO2_DEPLOY_REMOTE_BASE:-/home/unitree/go2_visual_dashboard}"
if [ -f scripts/nx_dashboard_env.sh ]; then
  # shellcheck disable=SC1091
  source scripts/nx_dashboard_env.sh
fi

if python3 -c 'from go2_dashboard.d1_hold_client import status; raise SystemExit(0 if status().get("ok") else 1)' 2>/dev/null; then
  echo D1_HOLD_DAEMON_OK
  exit 0
fi
if ! pgrep -f '[n]x_d1_hold_supervise.sh' >/dev/null 2>&1; then
  nohup bash scripts/nx_d1_hold_supervise.sh >> d1_hold_supervise.log 2>&1 &
  echo $! > d1_hold_supervise.pid
fi
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 0.25
  if python3 -c 'from go2_dashboard.d1_hold_client import status; raise SystemExit(0 if status().get("ok") else 1)' 2>/dev/null; then
    echo D1_HOLD_DAEMON_STARTED
    exit 0
  fi
done
echo D1_HOLD_DAEMON_START_FAILED >&2
exit 1

