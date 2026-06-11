#!/bin/bash
# Supervisore: rilancia la dashboard Flask sulla NX (default: monolite modular per lab localDogTest).
# L'avvio ufficiale su NX passa da qui (nx_start_dashboard.sh / boot wrapper), non più solo nohup diretto.
set +e
cd "$(dirname "$0")/.." || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_dashboard_env.sh"
# Traceback su abort/segfault utile in dashboard_run.log
export PYTHONFAULTHANDLER="${PYTHONFAULTHANDLER:-1}"
RESTART_SEC="${GO2_DASHBOARD_RESTART_DELAY_S:-15}"
echo "$(date -Is) nx_dashboard_supervise pid=$$ restart_delay_s=${RESTART_SEC}"
SERVE="${GO2_DASHBOARD_SERVE:-modular}"
if [ "$SERVE" = "lite" ] || [ "$SERVE" = "operator" ]; then
  SERVE_SCRIPT="scripts/serve_dashboard_lite.py"
else
  SERVE_SCRIPT="scripts/serve_dashboard_modular.py"
fi
while true; do
  echo "$(date -Is) exec ${SERVE_SCRIPT} (GO2_DASHBOARD_SERVE=${SERVE})"
  python3 "${SERVE_SCRIPT}" >> dashboard_run.log 2>&1
  ex=$?
  echo "$(date -Is) ${SERVE_SCRIPT} exited code=${ex} — sleep ${RESTART_SEC}s"
  sleep "$RESTART_SEC"
done
