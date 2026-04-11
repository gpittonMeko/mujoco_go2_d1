#!/usr/bin/env bash
# Riavvio locale: Go2+Z1 (unitree_mujoco.py) + run_go2_d1_ball.py
#
# IMPORTANTE: eseguire da un terminale sul PC/robot **con finestra grafica**
# (monitor collegato o `ssh -X`). Senza DISPLAY valido MuJoCo termina subito con
# "could not initialize GLFW" e lo script palla resta su "Waiting for lowstate…".
#
# Uso:
#   chmod +x scripts/restart_go2_z1_ball.sh
#   ./scripts/restart_go2_z1_ball.sh
#
# Opzionale: export DISPLAY=:0   oppure   export DISPLAY=:1
# su Jetson/desktop se il default non funziona.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Interfaccia DDS (come config.py per sim locale)
INTERFACE_OVERRIDE="${INTERFACE_OVERRIDE:-lo}"

if [[ -z "${DISPLAY:-}" ]]; then
  export DISPLAY="${DISPLAY:-:0}"
  echo "[restart] DISPLAY non era impostato: uso DISPLAY=$DISPLAY"
fi

pkill -f 'unitree_mujoco_d1viz.py' 2>/dev/null || true
pkill -f 'unitree_mujoco.py' 2>/dev/null || true
pkill -f 'run_go2_d1_ball.py' 2>/dev/null || true
pkill -f 'go2_d1_joint_joystick.py' 2>/dev/null || true
sleep 1

echo "[restart] Avvio simulatore MuJoCo…"
cd "$ROOT/unitree_mujoco/simulate_python"
python3 unitree_mujoco.py &
SIM_PID=$!
sleep 6

echo "[restart] Avvio run_go2_d1_ball.py (interface=$INTERFACE_OVERRIDE)…"
cd "$ROOT"
python3 scripts/run_go2_d1_ball.py --interface "$INTERFACE_OVERRIDE" &
BALL_PID=$!

echo "[restart] OK — sim PID=$SIM_PID  ball PID=$BALL_PID"
echo "  Per fermare: kill $SIM_PID $BALL_PID"
