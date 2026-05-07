#!/usr/bin/env bash
# Avvio dashboard sul Jetson per test presa: fluido, braccio abilitato, più cicli ricerca.
# Uso: dalla root del repo   bash scripts/start_dashboard_grasp_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export GO2_LOCAL="${GO2_LOCAL:-1}"
export GO2_DASHBOARD_HOST="${GO2_DASHBOARD_HOST:-0.0.0.0}"
export GO2_DASHBOARD_PORT="${GO2_DASHBOARD_PORT:-5050}"
export GO2_ENABLE_REAL_ARM="${GO2_ENABLE_REAL_ARM:-1}"
export GO2_GRASP_EXECUTE_ARM="${GO2_GRASP_EXECUTE_ARM:-1}"
export GO2_GRASP_USE_FUSED_PLAN_IK="${GO2_GRASP_USE_FUSED_PLAN_IK:-1}"
export GO2_GRASP_GOTO_SAVED_START="${GO2_GRASP_GOTO_SAVED_START:-1}"
export GO2_GRASP_START_FOLD="${GO2_GRASP_START_FOLD:-1}"
export GO2_FRONT_CAMERA_FALLBACK_GRASP="${GO2_FRONT_CAMERA_FALLBACK_GRASP:-1}"
export D1_SEARCH_MAX_CYCLES="${D1_SEARCH_MAX_CYCLES:-12}"
export D1_SEARCH_DELAY_MS="${D1_SEARCH_DELAY_MS:-560}"
export D1_PLAN_DELAY_MS="${D1_PLAN_DELAY_MS:-620}"
exec python3 scripts/serve_dashboard_modular.py
