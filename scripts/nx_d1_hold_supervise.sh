#!/usr/bin/env bash
set -u

cd "${GO2_DEPLOY_REMOTE_BASE:-/home/unitree/go2_visual_dashboard}" || exit 1
while true; do
  if [ -f scripts/nx_dashboard_env.sh ]; then
    # shellcheck disable=SC1091
    source scripts/nx_dashboard_env.sh
  fi
  date '+%Y-%m-%dT%H:%M:%S%z d1 hold daemon start' >> d1_hold_supervise.log
  python3 scripts/d1_hold_daemon.py >> d1_hold_daemon.log 2>&1
  code=$?
  date '+%Y-%m-%dT%H:%M:%S%z d1 hold daemon exited code '"$code" >> d1_hold_supervise.log
  sleep "${D1_HOLD_RESTART_DELAY_S:-0.2}"
done

