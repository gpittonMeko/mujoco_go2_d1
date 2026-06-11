#!/bin/bash
# Opzionale su EC2 g5: server GraspGen ZMQ sulla stessa macchina (porta 5556).
# Richiede clone NVlabs/GraspGen + GraspGenModels (checkpoints) — vedi client-server/README.md
set -euo pipefail
GRASP_GEN_ROOT="${GRASP_GEN_ROOT:-$HOME/GraspGen}"
MODELS_DIR="${GRASPGEN_MODELS_DIR:-$HOME/GraspGenModels}"
PORT="${GRASPGEN_ZMQ_PORT:-5556}"
if [[ ! -d "$GRASP_GEN_ROOT/client-server" ]]; then
  echo "Clone GraspGen: git clone https://github.com/NVlabs/GraspGen.git $GRASP_GEN_ROOT" >&2
  exit 2
fi
if [[ ! -d "$MODELS_DIR/checkpoints" ]]; then
  echo "Scarica modelli in $MODELS_DIR (vedi README GraspGen)" >&2
  exit 2
fi
cd "$GRASP_GEN_ROOT"
source .venv/bin/activate 2>/dev/null || true
pip install -q pyzmq msgpack msgpack-numpy 2>/dev/null || true
GRIPPER_YML="${GRASPGEN_GRIPPER_YML:-$MODELS_DIR/checkpoints/graspgen_robotiq_2f_140.yml}"
nohup python client-server/graspgen_server.py \
  --gripper_config "$GRIPPER_YML" \
  --port "$PORT" \
  > /var/log/graspgen-zmq.log 2>&1 &
echo "GraspGen ZMQ avviato su tcp://127.0.0.1:$PORT (log /var/log/graspgen-zmq.log)"
