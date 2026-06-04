#!/bin/bash
# Env dedicato dashboard jog D1 (porta 5053) — non modifica nx_dashboard_env.sh della dashboard operator.
# shellcheck disable=SC1091
if [ -f "$(dirname "$0")/nx_dashboard_env.sh" ]; then
  . "$(dirname "$0")/nx_dashboard_env.sh"
fi
export GO2_LOCAL=1
export D1_JOG_ENABLE_REAL_ARM=1
export GO2_ENABLE_REAL_ARM=1
export D1_JOG_PORT=5053
export D1_JOG_BIND=0.0.0.0
# mode 1 = smoothing traiettoria (come movimento zero interno al controller)
export D1_JOG_MODE=1
export D1_JOG_STREAM_MODE=1
export D1_JOG_DAEMON_DELAY_MS="${D1_JOG_DAEMON_DELAY_MS:-0}"
export D1_JOG_CMD_DELAY_MS="${D1_JOG_CMD_DELAY_MS:-0}"
export D1_JOG_FEEDBACK_S=3
export D1_JOG_FEEDBACK_TIMEOUT_S=14
export D1_DDS_DOMAIN="${D1_DDS_DOMAIN:-${GO2_DDS_DOMAIN:-0}}"
export GO2_DDS_INTERFACE="${GO2_DDS_INTERFACE:-eth0}"
export D1_DDS_INTERFACE="${D1_DDS_INTERFACE:-$GO2_DDS_INTERFACE}"
# CycloneDDS: forza multicast sulla NIC verso subnet Unitree (192.168.123.x)
if [ -z "${CYCLONEDDS_URI:-}" ]; then
  export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" multicast="default" priority="default"/></Interfaces></General></Domain></CycloneDDS>'
fi
export D1_ARM_HOST="${D1_ARM_HOST:-192.168.123.100}"
export PYTHONFAULTHANDLER="${PYTHONFAULTHANDLER:-1}"
# Jog cartesiano interpolato (waypoint IK) — ritardi bassi per reattività
export D1_CART_SEGMENT_MM="${D1_CART_SEGMENT_MM:-10}"
export D1_CART_MAX_WAYPOINTS="${D1_CART_MAX_WAYPOINTS:-6}"
export D1_CART_MIN_WAYPOINTS="${D1_CART_MIN_WAYPOINTS:-2}"
export D1_CART_STEP_DELAY_MS="${D1_CART_STEP_DELAY_MS:-8}"
export D1_CART_FEEDBACK_S="${D1_CART_FEEDBACK_S:-1}"
export D1_CART_FEEDBACK_TIMEOUT_S="${D1_CART_FEEDBACK_TIMEOUT_S:-4}"
export D1_CART_INTERPOLATED="${D1_CART_INTERPOLATED:-1}"
# Jog continuo cartesiano (stile UR teach pendant)
export D1_JOG_MAX_SPEED_MM_S="${D1_JOG_MAX_SPEED_MM_S:-22}"
export D1_JOG_MIN_SPEED_MM_S="${D1_JOG_MIN_SPEED_MM_S:-0}"
export D1_JOG_STREAM_HZ="${D1_JOG_STREAM_HZ:-20}"
export D1_JOG_STREAM_DELAY_MS="${D1_JOG_STREAM_DELAY_MS:-8}"
export D1_JOG_TICK_MAX_MM="${D1_JOG_TICK_MAX_MM:-2.5}"
export D1_CART_MAX_DQ_RAD="${D1_CART_MAX_DQ_RAD:-0.035}"
export D1_JOG_ACCEL_MM_S2="${D1_JOG_ACCEL_MM_S2:-120}"
export D1_JOG_DECEL_MM_S2="${D1_JOG_DECEL_MM_S2:-150}"
export D1_JOG_MIN_MOVE_SPEED_MM_S="${D1_JOG_MIN_MOVE_SPEED_MM_S:-0}"
export D1_JOG_KICK_START_RATIO="${D1_JOG_KICK_START_RATIO:-0}"
export D1_JOG_FB_RESYNC_EVERY="${D1_JOG_FB_RESYNC_EVERY:-0}"
export D1_JOG_ENABLE_EVERY_TICKS="${D1_JOG_ENABLE_EVERY_TICKS:-4}"
export D1_JOG_COUPLE_ON_STREAM_START="${D1_JOG_COUPLE_ON_STREAM_START:-0}"
export D1_JOG_ENABLE_EVERY_TICKS="${D1_JOG_ENABLE_EVERY_TICKS:-0}"
export D1_JOG_HOLD_AFTER_MOTION="${D1_JOG_HOLD_AFTER_MOTION:-0}"
export D1_ZERO_SETTLE_S="${D1_ZERO_SETTLE_S:-4}"
export D1_ZERO_HOLD_REPEATS="${D1_ZERO_HOLD_REPEATS:-8}"
export D1_ZERO_HOLD_DELAY_MS="${D1_ZERO_HOLD_DELAY_MS:-55}"
export D1_JOG_HOLD_SETTLE_S="${D1_JOG_HOLD_SETTLE_S:-0.35}"
# Programma: range ±° sui giunti (non uguaglianza esatta col valore salvato)
export D1_PROG_POSITION_TOL_DEG="${D1_PROG_POSITION_TOL_DEG:-2.5}"
export D1_PROG_SOFT_TOL_DEG="${D1_PROG_SOFT_TOL_DEG:-4.5}"
export D1_PROG_GRIPPER_TOL_DEG="${D1_PROG_GRIPPER_TOL_DEG:-8}"
export D1_PROG_IGNORE_GRIPPER="${D1_PROG_IGNORE_GRIPPER:-1}"
export D1_PROG_PROCEED_ON_TIMEOUT="${D1_PROG_PROCEED_ON_TIMEOUT:-1}"
export D1_PROG_WAIT_TIMEOUT_S="${D1_PROG_WAIT_TIMEOUT_S:-30}"
export D1_PROG_MAX_POLLS="${D1_PROG_MAX_POLLS:-12}"
export D1_PROG_MOVE_DEG_PER_S="${D1_PROG_MOVE_DEG_PER_S:-12}"
export D1_PROG_POLL_GAP_S="${D1_PROG_POLL_GAP_S:-0.15}"
export D1_JOG_ALWAYS_COUPLED=1
export D1_JOG_AUTO_ENABLE=1
# Orbbec wrist: SOLO stream RGB su /dev/video6 (video2=IR video4 spesso IR/depth su 335Lg)
export D1_ORBBEC_RGB_V4L_INDEX="${D1_ORBBEC_RGB_V4L_INDEX:-6}"
export D1_ORBBEC_V4L_INDICES="${D1_ORBBEC_V4L_INDICES:-6}"
export D1_ORBBEC_RGB_V4L_PREFERRED="${D1_ORBBEC_RGB_V4L_PREFERRED:-6}"
export D1_ORBBEC_RGB_ONLY="${D1_ORBBEC_RGB_ONLY:-1}"
export D1_ORBBEC_V4L_DIRECT="${D1_ORBBEC_V4L_DIRECT:-1}"
export D1_ORBBEC_PREFER_V4L_DIRECT="${D1_ORBBEC_PREFER_V4L_DIRECT:-1}"
export D1_ORBBEC_USE_OPERATOR_HTTP="${D1_ORBBEC_USE_OPERATOR_HTTP:-0}"
export D1_ORBBEC_MIN_FRAME_CHROMA="${D1_ORBBEC_MIN_FRAME_CHROMA:-12}"
export D1_ORBBEC_REPROBE_EACH_CAPTURE="${D1_ORBBEC_REPROBE_EACH_CAPTURE:-1}"
# ROI visione: esclude bordo inferiore (chele pinza nel frame polso)
export D1_PICK_VISION_CROP_BOTTOM_FRAC="${D1_PICK_VISION_CROP_BOTTOM_FRAC:-0.30}"
export D1_PICK_PX_TO_J0_DEG="${D1_PICK_PX_TO_J0_DEG:-0.04}"
export D1_PICK_PX_TO_J1_DEG="${D1_PICK_PX_TO_J1_DEG:-0.035}"
export D1_PICK_PX_TO_J2_DEG="${D1_PICK_PX_TO_J2_DEG:-0.015}"
export D1_PICK_CALIB_COUPLE_SETTLE_S="${D1_PICK_CALIB_COUPLE_SETTLE_S:-0.8}"
export D1_ORBBEC_CAPTURE_RETRIES="${D1_ORBBEC_CAPTURE_RETRIES:-3}"
# Riconoscimento oggetto: stesso criterio del detector (nessun filtro area extra in Teach)
export GO2_BOX_MAX_AREA_RATIO="${GO2_BOX_MAX_AREA_RATIO:-1.0}"
# Modello YOLO opzionale (TensorRT/ONNX/.pt) — se presente migliora foto+riconoscimento:
if [[ -z "${GO2_YOLO_MODEL:-}" ]]; then
  for _yolo in \
    "${GO2_VIS_DIR:-/home/unitree/go2_visual_dashboard}/models/yolo11n.engine" \
    "${GO2_VIS_DIR:-/home/unitree/go2_visual_dashboard}/models/yolo11n.onnx" \
    "/home/unitree/go2_visual_dashboard/models/yolo11n.engine"; do
    if [[ -f "$_yolo" ]]; then
      export GO2_YOLO_MODEL="$_yolo"
      break
    fi
  done
  unset _yolo
fi
export GO2_ORBBEC_PREFER_MJPEG="${GO2_ORBBEC_PREFER_MJPEG:-1}"
