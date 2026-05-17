#!/usr/bin/env bash
# Un solo file da lanciare sul PC worker (stessa rete della Jetson, es. 192.168.123.x).
# Uso:  bash bootstrap_worker_host.sh
#       bash bootstrap_worker_host.sh --skip-install   # solo avvio (venv già pronto)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
VENV="${ROOT}/.venv"
PORT="${WORKER_PORT:-8765}"
BIND="${WORKER_BIND_HOST:-0.0.0.0}"

REPO_ROOT="$(cd "$ROOT/../.." && pwd)"
if [[ ! -f "$REPO_ROOT/scripts/box_grasp_planner.py" ]]; then
  echo "ERRORE: serve il clone completo del repo (manca $REPO_ROOT/scripts/box_grasp_planner.py)"
  exit 1
fi

echo "=== Grasp worker host (Flask; default backend=planner / box_grasp_planner) ==="
echo "Directory worker: $ROOT"
echo "Repo root:        $REPO_ROOT"

if [[ "${1:-}" != "--skip-install" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERRORE: installa python3 (es. sudo apt install -y python3 python3-venv python3-pip)"
    exit 1
  fi
  echo ">>> Creo venv in .venv (se manca)"
  if [[ ! -d "$VENV" ]]; then
    python3 -m venv "$VENV"
  fi
  # shellcheck source=/dev/null
  source "$VENV/bin/activate"
  echo ">>> pip install -r requirements.txt"
  pip install -U pip wheel
  pip install -r requirements.txt
else
  # shellcheck source=/dev/null
  source "$VENV/bin/activate"
fi

echo
echo ">>> Indirizzi sulla LAN (cerca 192.168.123.x per GO2_ANYGRASP_WORKER_URL sulla NX):"
if command -v ip >/dev/null 2>&1; then
  ip -br a 2>/dev/null || true
else
  hostname -I 2>/dev/null || true
fi
echo
echo "Sulla Jetson, dopo il deploy, l'URL deve essere tipo:"
echo "  export GO2_ANYGRASP_WORKER_URL=http://<IP_SOPRA>:${PORT}"
echo "(oppure da PC di deploy:  set GO2_DEPLOY_ANYGRASP_WORKER_URL=http://<IP>:${PORT}  poi deploy)"
echo
echo "Firewall (Ubuntu, solo se serve):"
echo "  sudo ufw allow from 192.168.123.0/24 to any port ${PORT} proto tcp"
echo
echo "Verifica locale (altro terminale):"
echo "  curl -sS http://127.0.0.1:${PORT}/health"
echo
echo ">>> Avvio server http://${BIND}:${PORT}  (Ctrl+C per uscire)"
export WORKER_BIND_HOST="$BIND"
export WORKER_PORT="$PORT"
export GO2_GRASP_WORKER_BACKEND="${GO2_GRASP_WORKER_BACKEND:-planner}"
export WORKER_CAMERA_JPG_URL="${WORKER_CAMERA_JPG_URL:-http://192.168.123.18:5052/api/robot/camera/0.jpg}"
echo "GO2_GRASP_WORKER_BACKEND=$GO2_GRASP_WORKER_BACKEND"
echo "WORKER_CAMERA_JPG_URL=$WORKER_CAMERA_JPG_URL"
exec python app.py
