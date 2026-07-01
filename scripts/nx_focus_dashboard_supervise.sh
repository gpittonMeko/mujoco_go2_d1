#!/usr/bin/env bash
set -u

cd "${GO2_DEPLOY_REMOTE_BASE:-/home/unitree/go2_visual_dashboard}" || exit 1

while true; do
  if [ -f scripts/nx_dashboard_env.sh ]; then
    # shellcheck disable=SC1091
    source scripts/nx_dashboard_env.sh
  fi
  export GO2_LOCAL="${GO2_LOCAL:-1}"
  export GO2_ENABLE_BASE_MOTION="${GO2_ENABLE_BASE_MOTION:-1}"
  export GO2_ENABLE_REAL_ARM="${GO2_ENABLE_REAL_ARM:-1}"
  export GO2_FOCUS_PORT="${GO2_FOCUS_PORT:-5056}"
  export GO2_DASHBOARD_PORT="$GO2_FOCUS_PORT"
  export HERMES_OPERATOR_URL="http://127.0.0.1:${GO2_FOCUS_PORT}"
  export GO2_HERMES_INTEGRATED=1
  if [ "${GO2_FOCUS_PICK_YOLO:-0}" = "1" ] && [ -f "${GO2_YOLO_MODEL:-models/yolov8n.pt}" ]; then
    export GO2_YOLO_MODEL="${GO2_YOLO_MODEL:-/home/unitree/go2_visual_dashboard/models/yolov8n.pt}"
    export D1_PICK_DETECT_BACKEND=yolo
    export D1_PICK_COLOR_ONLY=0
  fi
  date '+%Y-%m-%dT%H:%M:%S%z focus supervise start' >> focus_supervise.log
  python3 scripts/serve_focus_dashboard.py >> focus_dashboard.log 2>&1
  code=$?
  date '+%Y-%m-%dT%H:%M:%S%z focus exited code '"$code" >> focus_supervise.log
  sleep "${GO2_FOCUS_RESTART_DELAY_S:-2}"
done
