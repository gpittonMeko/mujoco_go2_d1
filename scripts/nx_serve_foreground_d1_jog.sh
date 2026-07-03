#!/bin/bash
# Avvio in primo piano per systemd --user della dashboard D1 jog (5056).
cd /home/unitree/go2_visual_dashboard || exit 1
set -a
# shellcheck disable=SC1091
source /home/unitree/go2_visual_dashboard/scripts/nx_d1_jog_env.sh
set +a
exec /usr/bin/python3 /home/unitree/go2_visual_dashboard/scripts/serve_d1_jog_dashboard.py
