#!/bin/bash
# Avvia dashboard Vision (Intel camera MJPEG) sulla NX — porta 5054
set -e
cd /home/unitree/go2_visual_dashboard || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_vision_env.sh"
pkill -f serve_vision_dashboard.py 2>/dev/null || true
sleep 1
nohup python3 scripts/serve_vision_dashboard.py >> vision_run.log 2>&1 &
echo $! > vision.pid
sleep 3
python3 -c "import os,urllib.request; p=os.environ.get('VISION_PORT','5054'); urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health', timeout=15); print('VISION_HEALTH_OK', p)" || {
  echo VISION_HEALTH_FAIL
  tail -25 vision_run.log 2>/dev/null || true
  exit 1
}
