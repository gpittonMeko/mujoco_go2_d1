#!/bin/bash
# Avvio in primo piano per systemd --user (ExecStart). Stessi env di nx_start_dashboard.
cd /home/unitree/go2_visual_dashboard || exit 1
set -a
# shellcheck disable=SC1091
source /home/unitree/go2_visual_dashboard/scripts/nx_dashboard_env.sh
set +a
exec /usr/bin/python3 /home/unitree/go2_visual_dashboard/scripts/serve_dashboard_modular.py
