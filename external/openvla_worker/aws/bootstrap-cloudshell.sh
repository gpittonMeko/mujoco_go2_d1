#!/bin/bash
# =============================================================================
# GO2 VLA — ONE-SHOT da AWS CloudShell (eu-north-1)
#
# NON serve caricare LLM_14.pem in CloudShell: bootstrap via user-data EC2.
# La chiave .pem sul PC serve SOLO se vuoi entrare in SSH manualmente.
#
# COMANDO UNICO:
#
#   export AWS_REGION=eu-north-1
#   export GO2_REPO_URL="https://github.com/gpittonMeko/mujoco_go2_d1.git"
#   git clone --depth 1 "$GO2_REPO_URL" ~/mujoco_go2_d1
#   bash ~/mujoco_go2_d1/external/openvla_worker/aws/bootstrap-cloudshell.sh
#
# Repo privato:
#   export GITHUB_TOKEN="ghp_..."
# =============================================================================

_go2_bootstrap_self() {
  local target="${GO2_INSTALL_DIR:-$HOME/mujoco_go2_d1}/external/openvla_worker/aws/Dockerfile"
  [[ -f "$target" ]] && return 0

  local dir="${GO2_INSTALL_DIR:-$HOME/mujoco_go2_d1}"
  local url="${GO2_REPO_URL:-https://github.com/gpittonMeko/mujoco_go2_d1.git}"

  echo "[cloudshell] repo assente → preparo $dir"

  if [[ -f "$HOME/mujoco_go2_d1.zip" ]]; then
    rm -rf "$dir"
    unzip -q -o "$HOME/mujoco_go2_d1.zip" -d "$HOME"
  elif [[ -n "$url" ]]; then
    rm -rf "$dir"
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
      local slug="${url#https://github.com/}"
      slug="${slug#http://github.com/}"
      slug="${slug%.git}"
      git clone --depth 1 "https://${GITHUB_TOKEN}@github.com/${slug}.git" "$dir"
    else
      git clone --depth 1 "$url" "$dir"
    fi
  else
    echo "ERRORE: export GO2_REPO_URL=... oppure carica ~/mujoco_go2_d1.zip"
    exit 1
  fi

  [[ -f "$target" ]] || { echo "ERRORE: worker non trovato dopo clone"; exit 1; }
  exec bash "$dir/external/openvla_worker/aws/bootstrap-cloudshell.sh" "$@"
}
_go2_bootstrap_self "$@"

set -euo pipefail

AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-north-1}}"
GO2_REPO_URL="${GO2_REPO_URL:-https://github.com/gpittonMeko/mujoco_go2_d1.git}"
GO2_INSTALL_DIR="${GO2_INSTALL_DIR:-$HOME/mujoco_go2_d1}"
KEY_NAME="${KEY_NAME:-LLM_14}"
KEY_PATH="${KEY_PATH:-$HOME/LLM_14.pem}"
INSTANCE_TYPE="${INSTANCE_TYPE:-g5.xlarge}"
VOLUME_GB="${VOLUME_GB:-100}"
INSTANCE_NAME="${INSTANCE_NAME:-go2-vla-worker}"
GO2_WORKER_STUB="${GO2_WORKER_STUB:-1}"
REUSE_INSTANCE_ID="${REUSE_INSTANCE_ID:-}"
SG_NAME="${SG_NAME:-go2-vla-worker-sg}"
# user-data = niente SSH/pem; ssh = vecchio modo se GO2_BOOT_MODE=ssh
GO2_BOOT_MODE="${GO2_BOOT_MODE:-userdata}"

STATE_FILE="$HOME/go2-vla-ec2-state.json"
PAIR_FILE="$HOME/go2-vla-pairing.env"
LOG_FILE="$HOME/go2-vla-cloudshell.log"
TINY_JPEG_B64="/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAv/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAADw/wD/2Q=="

exec > >(tee -a "$LOG_FILE") 2>&1

aws_cmd() { aws --region "$AWS_REGION" "$@"; }

write_pairing_file() {
  local url="$1" token="$2"
  cat > "$PAIR_FILE" <<EOF
# Generato da bootstrap-cloudshell.sh
GO2_ANYGRASP_WORKER_URL=${url}
GO2_WORKER_TOKEN=${token}
GO2_GRASP_CLOUD_MODE=1
GO2_ANYGRASP_PROXY=1
EOF
  chmod 600 "$PAIR_FILE"
}

verify_vla_worker() {
  local url="$1" token="$2"
  echo ""
  echo "[verify] === smoke test VLA worker ==="

  echo "[verify] attendo GET /health (max ~20 min)..."
  local health_ok=0
  for _ in $(seq 1 120); do
    if curl -sf --connect-timeout 5 "${url}/health" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); raise SystemExit(0 if d.get('ok') else 1)" 2>/dev/null; then
      health_ok=1
      break
    fi
    sleep 10
  done
  if [[ "$health_ok" -ne 1 ]]; then
    echo "FAIL: /health non risponde OK"
    if [[ -n "${INSTANCE_ID:-}" ]]; then
      echo "[debug] ultimi log console EC2:"
      aws_cmd ec2 get-console-output --instance-id "$INSTANCE_ID" --latest \
        --query Output --output text 2>/dev/null | tail -40 || true
    fi
    return 1
  fi

  curl -sf "${url}/health" | python3 -m json.tool
  echo "[verify] POST /plan..."
  python3 - "$url" "$token" "$TINY_JPEG_B64" <<'PY'
import json, sys, urllib.request
base, token, b64 = sys.argv[1], sys.argv[2], sys.argv[3]
body = {
    "instruction": "afferra l'oggetto davanti al braccio",
    "logical_camera_device": 0,
    "image_url": "embedded://cloudshell/smoke",
    "jpeg_base64": b64,
    "jpeg_base64_front": b64,
}
data = json.dumps(body).encode()
req = urllib.request.Request(
    base.rstrip("/") + "/plan",
    data=data,
    headers={"Content-Type": "application/json", "X-Worker-Token": token},
    method="POST",
)
with urllib.request.urlopen(req, timeout=120) as resp:
    out = json.loads(resp.read().decode())
print(json.dumps(out, indent=2, ensure_ascii=False)[:4000])
if not out.get("ok"):
    raise SystemExit("plan not ok")
print("VERIFY_AWS_VLA_WORKER_OK")
PY
}

_build_user_data_b64() {
  local token="$1"
  local repo="$2"
  local stub="$3"
  local gh="${GITHUB_TOKEN:-}"

  python3 - "$token" "$repo" "$stub" "$gh" <<'PY'
import base64, sys
token, repo, stub, gh = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
if gh:
    slug = repo.replace("https://github.com/", "").replace("http://github.com/", "").removesuffix(".git")
    clone = f"git clone --depth 1 https://{gh}@github.com/{slug}.git /home/ubuntu/mujoco_go2_d1"
else:
    clone = f"git clone --depth 1 {repo} /home/ubuntu/mujoco_go2_d1"
script = f"""#!/bin/bash
exec > /var/log/go2-vla-userdata.log 2>&1
set -ex
export DEBIAN_FRONTEND=noninteractive
export GO2_WORKER_TOKEN={token}
export GO2_WORKER_STUB={stub}
export GO2_INSTALL_DIR=/home/ubuntu/mujoco_go2_d1

for i in $(seq 1 60); do curl -sf --connect-timeout 2 https://github.com && break; sleep 5; done
apt-get update
apt-get install -y git
rm -rf /home/ubuntu/mujoco_go2_d1
sudo -u ubuntu {clone}
chown -R ubuntu:ubuntu /home/ubuntu/mujoco_go2_d1
chmod +x /home/ubuntu/mujoco_go2_d1/external/openvla_worker/aws/*.sh
sudo -u ubuntu bash /home/ubuntu/mujoco_go2_d1/external/openvla_worker/aws/bootstrap-ec2.sh
echo GO2_VLA_USERDATA_DONE >> /var/log/go2-vla-userdata.log
"""
print(base64.b64encode(script.encode()).decode())
PY
}

bootstrap_via_ssh() {
  local worker_dir="$1"
  [[ -f "$KEY_PATH" ]] || { echo "ERRORE: GO2_BOOT_MODE=ssh ma manca $KEY_PATH"; exit 1; }
  chmod 600 "$KEY_PATH"

  echo "[cloudshell] attendo SSH..."
  local deadline=$((SECONDS + 600))
  until ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no -o ConnectTimeout=8 "ubuntu@$PUBLIC_IP" "echo ok" 2>/dev/null; do
    (( SECONDS > deadline )) && { echo "ERRORE: SSH timeout"; exit 1; }
    sleep 8
  done

  ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no "ubuntu@$PUBLIC_IP" \
    "mkdir -p /home/ubuntu/mujoco_go2_d1/external/openvla_worker"
  tar -C "$worker_dir" -cf - . | \
    ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no "ubuntu@$PUBLIC_IP" \
    "cd /home/ubuntu/mujoco_go2_d1/external/openvla_worker && tar -xf -"

  ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no "ubuntu@$PUBLIC_IP" bash -s <<REMOTE
set -euo pipefail
cd /home/ubuntu/mujoco_go2_d1/external/openvla_worker
chmod +x aws/*.sh
export GO2_INSTALL_DIR=/home/ubuntu/mujoco_go2_d1
export GO2_WORKER_STUB=${GO2_WORKER_STUB}
export GO2_WORKER_TOKEN=${TOKEN}
bash aws/bootstrap-ec2.sh
REMOTE
}

echo "=============================================="
echo " GO2 VLA one-shot CloudShell"
echo " region=$AWS_REGION mode=$GO2_BOOT_MODE"
echo " log=$LOG_FILE"
echo "=============================================="
echo "NOTA: non serve LLM_14.pem in CloudShell (mode=userdata)"
echo ""

aws_cmd sts get-caller-identity --output table

TOKEN="${GO2_WORKER_TOKEN:-$(openssl rand -hex 32)}"
echo "[cloudshell] worker token generato in CloudShell (salvato in pairing)"

INSTANCE_ID="$REUSE_INSTANCE_ID"
if [[ -z "$INSTANCE_ID" && -f "$STATE_FILE" ]]; then
  INSTANCE_ID="$(python3 -c "import json;print(json.load(open('$STATE_FILE')).get('instance_id',''))" 2>/dev/null || true)"
  [[ -n "$INSTANCE_ID" ]] && echo "[cloudshell] riuso instance: $INSTANCE_ID"
  if [[ -f "$PAIR_FILE" ]]; then
    old="$(grep '^GO2_WORKER_TOKEN=' "$PAIR_FILE" | cut -d= -f2- || true)"
    [[ -n "$old" ]] && TOKEN="$old"
  fi
fi

if [[ -n "$INSTANCE_ID" ]]; then
  STATE="$(aws_cmd ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo missing)"
  if [[ "$STATE" == "stopped" ]]; then
    aws_cmd ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
    aws_cmd ec2 wait instance-running --instance-ids "$INSTANCE_ID"
  elif [[ "$STATE" == "missing" || "$STATE" == "terminated" ]]; then
    INSTANCE_ID=""
  fi
fi

CREATED_NEW=0
if [[ -z "$INSTANCE_ID" ]]; then
  CREATED_NEW=1
  AMI="$(aws_cmd ec2 describe-images \
    --owners amazon \
    --filters "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*" "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
  [[ -n "$AMI" && "$AMI" != "None" ]] || { echo "ERRORE: AMI GPU non trovata in $AWS_REGION"; exit 1; }

  VPC_ID="$(aws_cmd ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)"

  SG_ID="$(aws_cmd ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
  if [[ -z "$SG_ID" || "$SG_ID" == "None" ]]; then
    SG_ID="$(aws_cmd ec2 create-security-group \
      --group-name "$SG_NAME" \
      --description "Go2 VLA worker 8765" \
      --vpc-id "$VPC_ID" \
      --query GroupId --output text)"
    aws_cmd ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 8765 --cidr 0.0.0.0/0 >/dev/null || true
    aws_cmd ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 --cidr 0.0.0.0/0 >/dev/null || true
  fi

  RUN_ARGS=(
    --image-id "$AMI"
    --instance-type "$INSTANCE_TYPE"
    --security-group-ids "$SG_ID"
    --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":${VOLUME_GB},\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]"
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}}]"
  )

  # Key pair opzionale: solo se esiste in AWS (per SSH dal PC col .pem)
  if aws_cmd ec2 describe-key-pairs --key-names "$KEY_NAME" --query 'KeyPairs[0].KeyName' --output text 2>/dev/null | grep -q "$KEY_NAME"; then
    RUN_ARGS+=(--key-name "$KEY_NAME")
    echo "[cloudshell] key pair $KEY_NAME collegato (SSH opzionale dal PC)"
  else
    echo "[cloudshell] key pair $KEY_NAME assente — OK, user-data non usa SSH"
  fi

  if [[ "$GO2_BOOT_MODE" == "userdata" ]]; then
    UD="$(_build_user_data_b64 "$TOKEN" "$GO2_REPO_URL" "$GO2_WORKER_STUB")"
    RUN_ARGS+=(--user-data "$UD")
    echo "[cloudshell] launch $INSTANCE_TYPE + user-data bootstrap..."
  else
    echo "[cloudshell] launch $INSTANCE_TYPE (modalità SSH)..."
  fi

  INSTANCE_ID="$(aws_cmd ec2 run-instances "${RUN_ARGS[@]}" \
    --query 'Instances[0].InstanceId' --output text)"
  aws_cmd ec2 wait instance-running --instance-ids "$INSTANCE_ID"
fi

PUBLIC_IP="$(aws_cmd ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
WORKER_URL="http://${PUBLIC_IP}:8765"
echo "[cloudshell] instance=$INSTANCE_ID ip=$PUBLIC_IP"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$CREATED_NEW" -eq 1 && "$GO2_BOOT_MODE" == "ssh" ]]; then
  bootstrap_via_ssh "$WORKER_DIR"
elif [[ "$CREATED_NEW" -eq 1 && "$GO2_BOOT_MODE" == "userdata" ]]; then
  echo "[cloudshell] bootstrap su EC2 via user-data (~10-25 min)..."
else
  echo "[cloudshell] istanza esistente — salto bootstrap"
fi

write_pairing_file "$WORKER_URL" "$TOKEN"

python3 - <<PY
import json, pathlib
pathlib.Path("$STATE_FILE").write_text(json.dumps({
    "instance_id": "$INSTANCE_ID",
    "region": "$AWS_REGION",
    "instance_type": "$INSTANCE_TYPE",
    "public_ip": "$PUBLIC_IP",
    "worker_url": "$WORKER_URL",
    "worker_token": "$TOKEN",
    "key_name": "$KEY_NAME",
    "pairing_file": "$PAIR_FILE",
}, indent=2), encoding="utf-8")
PY

verify_vla_worker "$WORKER_URL" "$TOKEN"

echo ""
echo "=============================================="
echo " FATTO — EC2 VLA pronta e verificata"
echo " URL: $WORKER_URL"
echo "=============================================="
echo ""
cat "$PAIR_FILE"
echo ""
echo "Copia pairing sopra → scrivi a Cursor: sono pronto"
