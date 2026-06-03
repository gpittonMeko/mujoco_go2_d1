#!/bin/bash
# Supervisore solo dashboard jog D1 (5053) — indipendente da nx_dashboard_supervise.sh (5052).
set +e
cd /home/unitree/go2_visual_dashboard || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_d1_jog_env.sh"
export PYTHONFAULTHANDLER="${PYTHONFAULTHANDLER:-1}"
RESTART_SEC="${D1_JOG_RESTART_DELAY_S:-12}"
echo "$(date -Is) nx_d1_jog_supervise pid=$$ port=${D1_JOG_PORT:-5053}"
while true; do
  echo "$(date -Is) exec scripts/serve_d1_jog_dashboard.py"
  python3 scripts/serve_d1_jog_dashboard.py >> d1_jog_run.log 2>&1
  ex=$?
  echo "$(date -Is) serve_d1_jog_dashboard exited code=${ex} — sleep ${RESTART_SEC}s"
  sleep "$RESTART_SEC"
done
