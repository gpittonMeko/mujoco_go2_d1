#!/usr/bin/env bash
# Un solo file da lanciare sul PC worker (stessa rete della Jetson, es. 192.168.123.x).
# Uso:  bash bootstrap_worker_host.sh
#       bash bootstrap_worker_host.sh --skip-install   # solo avvio (venv già pronto)
#       bash bootstrap_worker_host.sh --with-openvla   # clone repo openvla in ~/source/openvla (solo git)
#       bash bootstrap_worker_host.sh --install-openvla-hf   # pip HF (con --skip-install: solo HF deps)
set -euo pipefail

WITH_OPENVLA=0
SKIP_INSTALL=0
INSTALL_OPENVLA_HF=0
for arg in "$@"; do
  if [[ "$arg" == "--skip-install" ]]; then SKIP_INSTALL=1; fi
  if [[ "$arg" == "--with-openvla" ]]; then WITH_OPENVLA=1; fi
  if [[ "$arg" == "--install-openvla-hf" ]]; then INSTALL_OPENVLA_HF=1; fi
done

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

if [[ "$WITH_OPENVLA" == "1" ]]; then
  OV_DEST="${HOME}/source/openvla"
  mkdir -p "$(dirname "$OV_DEST")"
  if [[ ! -d "$OV_DEST/.git" ]]; then
    echo ">>> --with-openvla: git clone openvla -> $OV_DEST"
    git clone "https://github.com/openvla/openvla.git" "$OV_DEST"
  else
    echo ">>> --with-openvla: git pull in $OV_DEST"
    (cd "$OV_DEST" && git pull)
  fi
  export OPENVLA_REPO_ROOT="$OV_DEST"
  echo "export OPENVLA_REPO_ROOT=$OV_DEST"
fi

if [[ "$SKIP_INSTALL" != "1" ]]; then
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
  if [[ "$INSTALL_OPENVLA_HF" == "1" ]]; then
    echo ">>> pip install -r requirements-openvla.txt (Hugging Face OpenVLA)"
    pip install -r "${ROOT}/requirements-openvla.txt"
  fi
else
  # shellcheck source=/dev/null
  source "$VENV/bin/activate"
  if [[ "$INSTALL_OPENVLA_HF" == "1" ]]; then
    echo ">>> pip install -r requirements-openvla.txt (solo HF, --skip-install)"
    pip install -r "${ROOT}/requirements-openvla.txt"
  fi
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
