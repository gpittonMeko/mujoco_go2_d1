#!/bin/bash
# Installa timer idle-shutdown + touch hook per attività worker (da bootstrap EC2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IDLE_MIN="${GO2_EC2_IDLE_STOP_MIN:-20}"
REGION="${GO2_EC2_REGION:-eu-north-1}"

sudo mkdir -p /var/lib/go2-worker
sudo cp -f "$SCRIPT_DIR/idle-shutdown.sh" /usr/local/bin/go2-idle-shutdown.sh
sudo chmod +x /usr/local/bin/go2-idle-shutdown.sh

sudo tee /etc/systemd/system/go2-idle-shutdown.service >/dev/null <<EOF
[Unit]
Description=Go2 EC2 idle auto-stop

[Service]
Type=oneshot
Environment=GO2_EC2_IDLE_STOP_MIN=${IDLE_MIN}
Environment=GO2_EC2_REGION=${REGION}
ExecStart=/usr/local/bin/go2-idle-shutdown.sh
EOF

sudo tee /etc/systemd/system/go2-idle-shutdown.timer >/dev/null <<'EOF'
[Unit]
Description=Go2 EC2 idle check every 5 minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now go2-idle-shutdown.timer
echo "[auto-stop] timer go2-idle-shutdown attivo (idle=${IDLE_MIN} min)"
