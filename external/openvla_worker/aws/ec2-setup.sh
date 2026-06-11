#!/bin/bash
# Setup one-shot su EC2 Ubuntu 22.04 con GPU (g5.xlarge).
# Uso: bash aws/ec2-setup.sh
set -euo pipefail

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "ATTENZIONE: nvidia-smi non trovato. Usa AMI Deep Learning GPU o installa driver NVIDIA."
fi

sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git

if ! docker info 2>/dev/null | grep -qi nvidia; then
  echo "Installazione NVIDIA Container Toolkit..."
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt-get update
  sudo apt-get install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
fi

echo "OK: docker $(docker --version)"
nvidia-smi || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
  cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
  echo "Creato aws/.env da .env.example — modifica GO2_WORKER_TOKEN prima del compose up."
fi

echo ""
echo "Prossimi passi:"
echo "  cd external/openvla_worker"
echo "  docker compose -f aws/docker-compose.yml up -d --build"
echo "  curl -s http://127.0.0.1:8765/health"
