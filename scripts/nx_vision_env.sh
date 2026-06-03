#!/bin/bash
# Env Vision Workspace — porta 5054, solo camera Intel RealSense (logico 6)
if [ -f "$(dirname "$0")/nx_dashboard_env.sh" ]; then
  # shellcheck disable=SC1091
  . "$(dirname "$0")/nx_dashboard_env.sh"
fi
export GO2_LOCAL=1
export VISION_PORT=5054
export VISION_BIND=0.0.0.0
export VISION_CAMERA_LOGICAL=6
export GO2_CAMERA_CACHE_FPS=20
export GO2_REALSENSE_PREFER_MJPEG=1
export VISION_MJPEG_PERIOD_S=0.05
