#!/bin/bash
source /opt/ros/noetic/setup.bash 2>/dev/null || true
export PYTHONPATH="/opt/ros/noetic/lib/python3/dist-packages:${PYTHONPATH}"
cd /home/unitree/go2_visual_dashboard || exit 1
python3 scripts/_pyrs_rgb_test_inline.py
