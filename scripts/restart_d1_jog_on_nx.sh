#!/bin/bash
# Riavvio rapido dashboard jog D1 (5053) — eseguire sulla Jetson o via SSH.
set -e
cd /home/unitree/go2_visual_dashboard || { echo "cartella mancante"; exit 1; }
bash scripts/nx_start_d1_jog.sh
echo "URL: http://$(hostname -I | awk '{print $1}'):5053/"
echo "     http://192.168.123.18:5053/"
