#!/bin/bash
# Spegni questa EC2 se il worker non riceve traffico da N minuti.
# Richiede IAM instance profile con ec2:StopInstances sulla propria istanza.
set -euo pipefail

IDLE_MIN="${GO2_EC2_IDLE_STOP_MIN:-20}"
STAMP_FILE="${GO2_EC2_ACTIVITY_STAMP:-/var/lib/go2-worker/last_plan_unix}"
REGION="${GO2_EC2_REGION:-eu-north-1}"
LOG="${GO2_IDLE_SHUTDOWN_LOG:-/var/log/go2-idle-shutdown.log}"

mkdir -p "$(dirname "$STAMP_FILE")"
now="$(date +%s)"

if [[ ! -f "$STAMP_FILE" ]]; then
  echo "$(date -Is) init stamp (no activity yet)" >>"$LOG"
  echo "$now" >"$STAMP_FILE"
  exit 0
fi

last="$(cat "$STAMP_FILE" 2>/dev/null || echo 0)"
idle_sec=$((now - last))
limit_sec=$((IDLE_MIN * 60))

if ((idle_sec < limit_sec)); then
  exit 0
fi

iid="$(curl -sf --connect-timeout 2 http://169.254.169.254/latest/meta-data/instance-id || true)"
if [[ -z "$iid" ]]; then
  echo "$(date -Is) no instance-id metadata" >>"$LOG"
  exit 1
fi

echo "$(date -Is) idle ${idle_sec}s >= ${limit_sec}s — stopping $iid" >>"$LOG"
/usr/local/bin/aws ec2 stop-instances --region "$REGION" --instance-ids "$iid" >>"$LOG" 2>&1 || \
  /usr/bin/aws ec2 stop-instances --region "$REGION" --instance-ids "$iid" >>"$LOG" 2>&1
