#!/bin/bash
set -euo pipefail
cd /home/ubuntu/mujoco_go2_d1/external/openvla_worker
find aws -name '*.sh' -exec sed -i 's/\r$//' {} \;
cp -f aws/.env.example aws/.env
TOKEN="$(openssl rand -hex 32)"
sed -i "s|^GO2_WORKER_TOKEN=.*|GO2_WORKER_TOKEN=${TOKEN}|" aws/.env
sed -i 's|^OPENVLA_RUNTIME_STUB=.*|OPENVLA_RUNTIME_STUB=1|' aws/.env
sed -i 's|^OPENVLA_USE_HF=.*|OPENVLA_USE_HF=0|' aws/.env
sed -i 's|^GO2_GRASP_WORKER_BACKEND=.*|GO2_GRASP_WORKER_BACKEND=auto|' aws/.env || echo 'GO2_GRASP_WORKER_BACKEND=auto' >> aws/.env
grep -q '^GO2_GRASP_GEN_ZMQ=' aws/.env || echo 'GO2_GRASP_GEN_ZMQ=tcp://127.0.0.1:5556' >> aws/.env
grep -q '^GO2_DEPTH_SCALE_M_PER_UNIT=' aws/.env || echo 'GO2_DEPTH_SCALE_M_PER_UNIT=0.001' >> aws/.env
export GO2_EC2_REGION="${GO2_EC2_REGION:-eu-north-1}"
export GO2_EC2_IDLE_STOP_MIN="${GO2_EC2_IDLE_STOP_MIN:-20}"
bash aws/setup-auto-stop.sh || true
docker compose -f aws/docker-compose.yml up -d --build
PUBLIC_IP="$(curl -sf --connect-timeout 2 http://169.254.169.254/latest/meta-data/public-ipv4 || true)"
if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(curl -sf --connect-timeout 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
fi
cat > "$HOME/go2-vla-pairing.env" <<EOF
GO2_ANYGRASP_WORKER_URL=http://${PUBLIC_IP}:8765
GO2_WORKER_TOKEN=${TOKEN}
GO2_GRASP_CLOUD_MODE=1
GO2_ANYGRASP_PROXY=1
EOF
chmod 600 "$HOME/go2-vla-pairing.env"
echo "WORKER_URL=http://${PUBLIC_IP}:8765"
for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:8765/health" >/dev/null; then
    curl -s "http://127.0.0.1:8765/health" | python3 -m json.tool 2>/dev/null || curl -s "http://127.0.0.1:8765/health"
    exit 0
  fi
  sleep 3
done
echo "health timeout" >&2
docker ps -a
exit 1
