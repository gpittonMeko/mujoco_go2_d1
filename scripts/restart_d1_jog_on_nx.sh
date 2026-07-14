#!/bin/bash
# Riavvio rapido dashboard jog D1 (5056) — eseguire sulla Jetson o via SSH.
set -e
cd /home/unitree/go2_visual_dashboard || { echo "cartella mancante"; exit 1; }
bash scripts/nx_start_d1_jog.sh
PORT="${D1_JOG_PORT:-5056}"
echo "URL: http://$(hostname -I | awk '{print $1}'):${PORT}/"
echo "     http://192.168.123.18:${PORT}/"
