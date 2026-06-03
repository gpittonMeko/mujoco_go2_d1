#!/bin/bash
# Avvia / riavvia dashboard jog D1 (5053). Per RGB RealSense (pyrealsense2) libera la camera dalla 5052.
set -e
cd /home/unitree/go2_visual_dashboard || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_d1_jog_env.sh"
# La dashboard operator (5052) tiene /dev/video4 — blocca pyrealsense2. Con D1_JOG_RGB_EXCLUSIVE=1 la fermiamo.
if [ "${D1_JOG_RGB_EXCLUSIVE:-1}" = "1" ] && [ "${GO2_REALSENSE_COLOR_BACKEND:-}" = "pyrs" ]; then
  echo "D1 vision RGB: stop dashboard operator (5052) + supervise per liberare RealSense"
  pkill -f serve_dashboard_modular.py 2>/dev/null || true
  pkill -f nx_dashboard_supervise.sh 2>/dev/null || true
  sleep 2
fi
pkill -f nx_d1_jog_supervise.sh 2>/dev/null || true
pkill -f serve_d1_jog_dashboard.py 2>/dev/null || true
pkill -f serve_vision_dashboard.py 2>/dev/null || true
fuser -k /dev/video4 /dev/video2 /dev/video0 2>/dev/null || true
sleep 2
nohup bash scripts/nx_d1_jog_supervise.sh >> d1_jog_supervise.log 2>&1 &
echo $! > d1_jog.pid
sleep 4
python3 -c "import os,urllib.request; p=os.environ.get('D1_JOG_PORT','5053'); urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health', timeout=15); print('D1_JOG_HEALTH_OK', p)" || {
  echo D1_JOG_HEALTH_FAIL
  tail -30 d1_jog_run.log 2>/dev/null || true
  tail -15 d1_jog_supervise.log 2>/dev/null || true
  exit 1
}
