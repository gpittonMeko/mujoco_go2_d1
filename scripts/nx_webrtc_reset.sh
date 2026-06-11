#!/usr/bin/env bash
# Sulla NX: pulisce WebRTC zombie e opzionalmente riavvia la dashboard.
#   bash scripts/nx_webrtc_reset.sh          # solo pkill
#   bash scripts/nx_webrtc_reset.sh restart  # pkill + riavvio dashboard
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pkill -f 'pc_go2_webrtc_play_mp3.py' 2>/dev/null || true
sleep 1
echo "[nx_webrtc_reset] webrtc play procs:"
ps aux | grep -E 'pc_go2_webrtc_play_mp3' | grep -v grep || echo "  (none)"
if [[ "${1:-}" == "restart" ]]; then
  echo "[nx_webrtc_reset] riavvio dashboard …"
  bash "$ROOT/scripts/nx_start_dashboard.sh"
fi
echo "[nx_webrtc_reset] ok"
