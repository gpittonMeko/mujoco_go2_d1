#!/bin/bash
# Supervisore Hermes (5054)
set +e
cd /home/unitree/go2_visual_dashboard || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_hermes_env.sh"
RESTART_SEC="${HERMES_RESTART_DELAY_S:-12}"
echo "$(date -Is) nx_hermes_supervise pid=$$ port=${HERMES_PORT:-5054}"
while true; do
  echo "$(date -Is) exec scripts/serve_hermes_dashboard.py"
  python3 scripts/serve_hermes_dashboard.py >> hermes_run.log 2>&1
  ex=$?
  echo "$(date -Is) serve_hermes_dashboard exited code=${ex} — sleep ${RESTART_SEC}s"
  sleep "$RESTART_SEC"
done
