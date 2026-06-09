#!/bin/bash
# Avvia Hermes sulla NX (5054). Richiede operator :5052 per il contesto.
set -e
cd /home/unitree/go2_visual_dashboard || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_hermes_env.sh"
# Backend operator (camere/Sport) — non è una dashboard da aprire; solo Hermes :5054
if ! curl -sf --max-time 3 "http://127.0.0.1:5052/api/health" >/dev/null 2>&1; then
  if [ -x scripts/nx_start_dashboard.sh ]; then
    nohup bash scripts/nx_start_dashboard.sh >> operator_for_hermes.log 2>&1 &
    sleep 4
  fi
fi
pkill -f nx_hermes_supervise.sh 2>/dev/null || true
pkill -f serve_hermes_dashboard.py 2>/dev/null || true
sleep 1
nohup bash scripts/nx_hermes_supervise.sh >> hermes_supervise.log 2>&1 &
echo $! > hermes.pid
sleep 3
python3 -c "import os,urllib.request; p=os.environ.get('HERMES_PORT','5054'); urllib.request.urlopen('http://127.0.0.1:'+p+'/api/hermes/health', timeout=12); print('HERMES_HEALTH_OK', p)" || {
  echo HERMES_HEALTH_FAIL
  tail -25 hermes_run.log 2>/dev/null || true
  exit 1
}
