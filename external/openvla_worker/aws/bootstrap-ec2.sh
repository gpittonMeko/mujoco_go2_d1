#!/bin/bash
# Bootstrap completo worker VLA su EC2 g5.xlarge — un solo script dopo SSH.
#
# Opzione A — clone da git:
#   export GO2_REPO_URL=https://github.com/TUO_ORG/mujoco_go2_d1.git
#   curl -fsSL https://raw.githubusercontent.com/.../bootstrap-ec2.sh | bash
#   # oppure da repo già copiato:
#   bash external/openvla_worker/aws/bootstrap-ec2.sh
#
# Opzione B — repo già su EC2 (scp/rsync):
#   cd ~/mujoco_go2_d1/external/openvla_worker && bash aws/bootstrap-ec2.sh
#
# Env utili:
#   GO2_WORKER_STUB=1   (default) — test rete senza scaricare OpenVLA 7B
#   GO2_WORKER_STUB=0   — OpenVLA HF reale (lento al primo avvio)
#   GO2_INSTALL_DIR=~/mujoco_go2_d1
#   GO2_REPO_URL=...    — se la cartella non esiste

set -euo pipefail

GO2_INSTALL_DIR="${GO2_INSTALL_DIR:-$HOME/mujoco_go2_d1}"
GO2_REPO_URL="${GO2_REPO_URL:-}"
GO2_WORKER_STUB="${GO2_WORKER_STUB:-1}"

echo "[bootstrap] install dir: $GO2_INSTALL_DIR"

if [[ ! -f "$GO2_INSTALL_DIR/external/openvla_worker/aws/Dockerfile" ]]; then
  if [[ -n "$GO2_REPO_URL" ]]; then
    echo "[bootstrap] git clone $GO2_REPO_URL"
    git clone --depth 1 "$GO2_REPO_URL" "$GO2_INSTALL_DIR"
  else
    echo "ERRORE: repo non trovato in $GO2_INSTALL_DIR"
    echo "  export GO2_REPO_URL=https://... && bash aws/bootstrap-ec2.sh"
    echo "  oppure: scp -r mujoco_go2_d1 ubuntu@EC2:~/mujoco_go2_d1"
    exit 1
  fi
fi

WORKER_DIR="$GO2_INSTALL_DIR/external/openvla_worker"
cd "$WORKER_DIR"

echo "[bootstrap] Docker + NVIDIA toolkit…"
bash aws/ec2-setup.sh

TOKEN="$(openssl rand -hex 32)"
PUBLIC_IP="$(curl -sf --connect-timeout 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(curl -sf --connect-timeout 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
fi

cp -f aws/.env.example aws/.env
sed -i "s|^GO2_WORKER_TOKEN=.*|GO2_WORKER_TOKEN=${TOKEN}|" aws/.env

if [[ "$GO2_WORKER_STUB" == "1" ]]; then
  sed -i 's|^OPENVLA_RUNTIME_STUB=.*|OPENVLA_RUNTIME_STUB=1|' aws/.env
  sed -i 's|^OPENVLA_USE_HF=.*|OPENVLA_USE_HF=0|' aws/.env
  echo "[bootstrap] modalità STUB (Gate G2) — nessun download HF"
else
  sed -i 's|^OPENVLA_RUNTIME_STUB=.*|OPENVLA_RUNTIME_STUB=0|' aws/.env
  sed -i 's|^OPENVLA_USE_HF=.*|OPENVLA_USE_HF=1|' aws/.env
  echo "[bootstrap] modalità OpenVLA HF (Gate G3) — primo avvio lento"
fi

echo "[bootstrap] docker compose build + up…"
docker compose -f aws/docker-compose.yml up -d --build

PAIR_FILE="$HOME/go2-vla-pairing.env"
WORKER_URL="http://${PUBLIC_IP}:8765"
cat > "$PAIR_FILE" <<EOF
# Generato da bootstrap-ec2.sh — usa con: python scripts/pair_nx_aws_vla.py --pairing-file go2-vla-pairing.env
GO2_ANYGRASP_WORKER_URL=${WORKER_URL}
GO2_WORKER_TOKEN=${TOKEN}
GO2_GRASP_CLOUD_MODE=1
GO2_ANYGRASP_PROXY=1
EOF
chmod 600 "$PAIR_FILE"

echo ""
echo "=============================================="
echo " WORKER VLA PRONTO"
echo " URL:    ${WORKER_URL}"
echo " Token:  ${TOKEN}"
echo " Pair:   ${PAIR_FILE}"
echo "=============================================="
echo ""
echo "Security group EC2: apri inbound TCP 8765 (o metti ALB :443)."
echo ""
echo "Verifica su EC2:"
echo "  curl -s http://127.0.0.1:8765/health | python3 -m json.tool"
echo ""
echo "Dal PC (repo locale, NX raggiungibile):"
echo "  scp ubuntu@${PUBLIC_IP}:${PAIR_FILE} ."
echo "  python scripts/pair_nx_aws_vla.py --pairing-file go2-vla-pairing.env"
echo "  python scripts/verify_aws_vla_worker.py ${WORKER_URL} --token ${TOKEN}"
echo ""

for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:8765/health" >/dev/null 2>&1; then
    echo "[bootstrap] health OK"
    curl -s "http://127.0.0.1:8765/health" | python3 -m json.tool 2>/dev/null || curl -s "http://127.0.0.1:8765/health"
    exit 0
  fi
  sleep 2
done

echo "[bootstrap] AVVISO: health non ancora OK — controlla: docker logs \$(docker ps -q --filter ancestor=go2-vla-worker:aws | head -1)"
exit 0
