#!/bin/bash
# Avvio in primo piano per systemd --user (ExecStart). Default: focus dashboard :5056.
cd /home/unitree/go2_visual_dashboard || exit 1
set -a
# shellcheck disable=SC1091
source /home/unitree/go2_visual_dashboard/scripts/nx_dashboard_env.sh
set +a
export GO2_LOCAL="${GO2_LOCAL:-1}"
export GO2_ENABLE_BASE_MOTION="${GO2_ENABLE_BASE_MOTION:-1}"
export GO2_ENABLE_REAL_ARM="${GO2_ENABLE_REAL_ARM:-1}"
export GO2_FOCUS_PORT="${GO2_FOCUS_PORT:-5056}"
export GO2_DASHBOARD_PORT="$GO2_FOCUS_PORT"
export HERMES_OPERATOR_URL="http://127.0.0.1:${GO2_FOCUS_PORT}"
export GO2_HERMES_INTEGRATED=1
exec /usr/bin/python3 /home/unitree/go2_visual_dashboard/scripts/serve_focus_dashboard.py
