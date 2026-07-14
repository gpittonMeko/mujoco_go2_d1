#!/bin/bash
# Offline install unitree_sdk2_python on Jetson (no PyPI; uses system CycloneDDS /usr/local).
set -euo pipefail
export CYCLONEDDS_HOME=/usr/local
export LD_LIBRARY_PATH=/usr/local/lib:${LD_LIBRARY_PATH:-}
export PATH="$HOME/.local/bin:$PATH"
export PIP_BREAK_SYSTEM_PACKAGES=1
OFFLINE="/home/unitree/h2_demo/offline"
DEMO="/home/unitree/h2_demo"

if [ ! -f "$OFFLINE/cyclonedds-0.10.2.tar.gz" ]; then
  echo "Missing $OFFLINE/cyclonedds-0.10.2.tar.gz — run on PC: python scripts/h2_bundle_offline_deps.py"
  exit 1
fi

echo "[install] cyclonedds python bindings (offline build)..."
pip3 install --user --no-build-isolation --no-index --no-deps \
  "$OFFLINE/cyclonedds-0.10.2.tar.gz"

echo "[install] unitree_sdk2py (editable, no deps)..."
cd "$DEMO/unitree_sdk2_python"
pip3 install --user --no-build-isolation --no-deps -e .

python3 -c "import cyclonedds, unitree_sdk2py; print('SDK OK', unitree_sdk2py.__file__)"
