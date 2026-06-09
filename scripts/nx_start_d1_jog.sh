#!/bin/bash
# Avvia / riavvia dashboard jog D1 (5053). Per RGB RealSense (pyrealsense2) libera la camera dalla 5052.
set -e
cd /home/unitree/go2_visual_dashboard || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_d1_jog_env.sh"
# Esponi tutti i nodi Orbbec (video6 = RGB color) dopo reboot USB.
if [ "${D1_ORBBEC_RELOAD_UVC:-1}" = "1" ]; then
  echo "${GO2_NX_PASSWORD:-123}" | sudo -S sh -c 'modprobe -r uvcvideo 2>/dev/null; modprobe uvcvideo' 2>/dev/null || true
  sleep 2
fi
# Solo dashboard D1 (5053): la 5052 operator non deve restare attiva (libera Orbbec + RealSense).
if [ "${D1_JOG_STOP_OPERATOR_DASH:-1}" = "1" ]; then
  bash scripts/nx_stop_operator_dashboard.sh || true
fi
pkill -f nx_d1_jog_supervise.sh 2>/dev/null || true
pkill -f serve_d1_jog_dashboard.py 2>/dev/null || true
pkill -f serve_vision_dashboard.py 2>/dev/null || true
fuser -k /dev/video0 /dev/video1 /dev/video2 /dev/video3 /dev/video4 /dev/video5 /dev/video6 /dev/video7 /dev/video12 2>/dev/null || true
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
