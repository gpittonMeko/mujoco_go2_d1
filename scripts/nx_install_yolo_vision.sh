#!/usr/bin/env bash
# Installa Ultralytics + scarica yolo11n.pt per Vision D1 (5053). Idempotente.
set -euo pipefail
BASE="${GO2_VIS_DASH_ROOT:-/home/unitree/go2_visual_dashboard}"
MODEL_DIR="$BASE/models"
# yolov8n = compatibile con ultralytics 8.0.x su Jetson (yolo11n.pt richiede 8.3+ / modulo C3k2)
MODEL_PT="$MODEL_DIR/yolov8n.pt"
export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$MODEL_DIR"

if ! python3 -c "import ultralytics" 2>/dev/null; then
  echo "[yolo] install ultralytics (Python 3.8 Jetson)…"
  pip3 install --user "numpy<2" "opencv-python-headless>=4.5" pyyaml requests pillow tqdm 2>/dev/null || true
  pip3 install --user "ultralytics==8.0.196" || pip3 install --user "ultralytics==8.1.0"
fi
python3 -c "import ultralytics; print('[yolo] ultralytics', ultralytics.__version__)"

if [ ! -f "$MODEL_PT" ]; then
  echo "[yolo] download yolov8n.pt (COCO, compatibile NX)…"
  wget -q -O "$MODEL_PT" "https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov8n.pt" \
    || curl -fsSL -o "$MODEL_PT" "https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov8n.pt"
fi
# Test inferenza
python3 -c "
from ultralytics import YOLO
import numpy as np
m = YOLO('$MODEL_PT')
r = m.predict(np.zeros((480,640,3), dtype=np.uint8), verbose=False)
print('[yolo] smoke OK', len(r))
" || echo "[yolo] WARN smoke test failed"
ls -la "$MODEL_PT"
echo "[yolo] OK model=$MODEL_PT"
