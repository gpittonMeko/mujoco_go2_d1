#!/bin/bash
# Supervisore: rilancia la dashboard Flask sulla NX.
# Default attuale: focus dashboard su :5056. La vecchia operator/lite :5052 resta solo
# per avvii manuali espliciti con GO2_DASHBOARD_SERVE=lite.
set +e
cd "$(dirname "$0")/.." || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_dashboard_env.sh"
# Traceback su abort/segfault utile in dashboard_run.log
export PYTHONFAULTHANDLER="${PYTHONFAULTHANDLER:-1}"
RESTART_SEC="${GO2_DASHBOARD_RESTART_DELAY_S:-15}"
echo "$(date -Is) nx_dashboard_supervise pid=$$ restart_delay_s=${RESTART_SEC}"
SERVE="${GO2_DASHBOARD_SERVE:-focus}"
if [ "$SERVE" = "lite" ] || [ "$SERVE" = "operator" ]; then
  SERVE_SCRIPT="scripts/serve_dashboard_lite.py"
elif [ "$SERVE" = "focus" ]; then
  export GO2_FOCUS_PORT="${GO2_FOCUS_PORT:-5056}"
  export GO2_DASHBOARD_PORT="$GO2_FOCUS_PORT"
  export HERMES_OPERATOR_URL="http://127.0.0.1:${GO2_FOCUS_PORT}"
  export GO2_HERMES_INTEGRATED=1
  SERVE_SCRIPT="scripts/serve_focus_dashboard.py"
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
