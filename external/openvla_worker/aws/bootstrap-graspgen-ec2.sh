#!/bin/bash
# Bootstrap GraspGen ZMQ server su EC2 g5 (host, porta 5556). Worker Docker usa tcp://172.17.0.1:5556
set -euo pipefail
GRASP_GEN_ROOT="${GRASP_GEN_ROOT:-$HOME/GraspGen}"
MODELS_DIR="${GRASPGEN_MODELS_DIR:-$HOME/GraspGenModels}"
PORT="${GRASPGEN_ZMQ_PORT:-5556}"
LOG="/var/log/graspgen-zmq.log"

if [[ ! -d "$GRASP_GEN_ROOT/.git" ]]; then
  echo "Cloning NVlabs/GraspGen..."
  git clone --depth 1 https://github.com/NVlabs/GraspGen.git "$GRASP_GEN_ROOT"
fi
cd "$GRASP_GEN_ROOT"
if [[ ! -d .venv ]]; then
  python3.10 -m venv .venv || python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q pyzmq msgpack msgpack-numpy numpy trimesh 2>/dev/null || true
if [[ -f install_pointnet.sh ]]; then
  bash install_pointnet.sh || echo "WARN: install_pointnet.sh failed — continua se già installato"
fi
pip install -q -e . || { echo "FAIL pip install GraspGen"; exit 2; }

GRIPPER_YML="${GRASPGEN_GRIPPER_YML:-$MODELS_DIR/checkpoints/graspgen_robotiq_2f_140.yml}"
if [[ ! -f "$GRIPPER_YML" ]]; then
  echo "MANCA gripper config: $GRIPPER_YML"
  echo "Scarica GraspGenModels (checkpoints) in $MODELS_DIR — vedi README GraspGen."
  echo "Worker userà fallback planner RGB-D finché GraspGen non è pronto."
  exit 3
fi
pkill -f graspgen_server.py 2>/dev/null || true
sleep 1
nohup python client-server/graspgen_server.py --gripper_config "$GRIPPER_YML" --port "$PORT" >>"$LOG" 2>&1 &
echo "GraspGen ZMQ pid=$! port=$PORT log=$LOG"
sleep 5
if ss -ltn | grep -q ":${PORT} "; then
  echo "GRASPGEN_ZMQ_LISTEN_OK"
else
  tail -30 "$LOG" || true
  exit 4
fi
