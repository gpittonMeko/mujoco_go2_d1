#!/bin/bash
# Avvia / riavvia dashboard D1 integrata (5056).
set -e
cd /home/unitree/go2_visual_dashboard || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_d1_jog_env.sh"
# Il keeper deve possedere il writer prima che venga terminato Flask. Se non è
# disponibile, non riavviare la dashboard e non creare una finestra senza hold.
bash scripts/nx_start_d1_hold_daemon.sh
# Esponi tutti i nodi Orbbec (video6 = RGB color) dopo reboot USB.
if [ "${D1_ORBBEC_RELOAD_UVC:-1}" = "1" ]; then
  echo "${GO2_NX_PASSWORD:-123}" | sudo -S sh -c 'modprobe -r uvcvideo 2>/dev/null; modprobe uvcvideo' 2>/dev/null || true
  sleep 2
fi
# Solo dashboard D1 (5056): la 5052 operator non deve restare attiva (libera le camere).
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
python3 -c "import json,os,urllib.request; p=os.environ.get('D1_JOG_PORT','5056'); d=json.load(urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health', timeout=15)); h=d.get('command_daemon') or {}; assert d.get('ok') is True, d; assert h.get('external') is True, d; assert h.get('alive') is True and h.get('hold_active') is True, d; assert (d.get('startup_arm_stabilization') or {}).get('ok') is True, d; print('D1_JOG_HEALTH_OK_EXTERNAL_HOLD', p, h.get('publisher_pid'))" || {
  echo D1_JOG_HEALTH_FAIL
  tail -30 d1_jog_run.log 2>/dev/null || true
  tail -15 d1_jog_supervise.log 2>/dev/null || true
  exit 1
}
