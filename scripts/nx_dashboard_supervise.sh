#!/bin/bash
# Supervisore: rilancia serve_dashboard_lite.py (dashboard operator) se termina (crash, OOM, eccezioni).
# L'avvio ufficiale su NX passa da qui (nx_start_dashboard.sh / boot wrapper), non più solo nohup diretto.
set +e
cd /home/unitree/go2_visual_dashboard || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_dashboard_env.sh"
# Traceback su abort/segfault utile in dashboard_run.log
export PYTHONFAULTHANDLER="${PYTHONFAULTHANDLER:-1}"
RESTART_SEC="${GO2_DASHBOARD_RESTART_DELAY_S:-15}"
echo "$(date -Is) nx_dashboard_supervise pid=$$ restart_delay_s=${RESTART_SEC}"
while true; do
  echo "$(date -Is) exec serve_dashboard_lite.py"
  python3 scripts/serve_dashboard_lite.py >> dashboard_run.log 2>&1
  ex=$?
  echo "$(date -Is) serve_dashboard_lite.py exited code=${ex} — sleep ${RESTART_SEC}s"
  sleep "$RESTART_SEC"
done
