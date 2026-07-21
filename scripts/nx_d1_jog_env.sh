#!/bin/bash
# Env dedicato dashboard jog D1 (porta 5056) — non modifica nx_dashboard_env.sh della dashboard operator.
# shellcheck disable=SC1091
if [ -f "$(dirname "$0")/nx_dashboard_env.sh" ]; then
  . "$(dirname "$0")/nx_dashboard_env.sh"
fi
export GO2_LOCAL=1
export D1_JOG_ENABLE_REAL_ARM=1
export GO2_ENABLE_REAL_ARM=1
export D1_JOG_PORT=5056
export D1_JOG_BIND=0.0.0.0
# mode 1 = smoothing TRAIETTORIA (solo jog/waypoint in moto).
# mode 0 = HOLD / dati a ~10Hz (doc Unitree D1 Arm services).
# NON usare mode1 per heartbeat hold: flood continuo → servo caldi → braccio
# smette di rispondere (report Caltech SURF 2025 + lab).
export D1_JOG_MODE=1
export D1_JOG_STREAM_MODE=1
export D1_HOLD_MODE="${D1_HOLD_MODE:-0}"
export D1_JOG_DAEMON_DELAY_MS="${D1_JOG_DAEMON_DELAY_MS:-0}"
export D1_JOG_CMD_DELAY_MS="${D1_JOG_CMD_DELAY_MS:-0}"
export D1_JOG_FEEDBACK_S=3
export D1_JOG_FEEDBACK_TIMEOUT_S=14
export D1_DDS_DOMAIN="${D1_DDS_DOMAIN:-${GO2_DDS_DOMAIN:-0}}"
export GO2_DDS_INTERFACE="${GO2_DDS_INTERFACE:-eth0}"
export D1_DDS_INTERFACE="${D1_DDS_INTERFACE:-$GO2_DDS_INTERFACE}"
# Runtime DDS del controller D1. Le lib standard /usr/local (Cyclone 0.10.5)
# non riconoscono il nodo Unitree <SharedMemory> e fanno abortire il publisher.
# Questo runtime è stato verificato live con lo stesso d1_sdk_command.
_D1_DDS_RUNTIME="${D1_DDS_LIB_DIR:-/home/unitree/sdk_reinstall_backup_19700225_160102}"
if [ -f "$_D1_DDS_RUNTIME/libddsc.so.0" ] && [ -f "$_D1_DDS_RUNTIME/libddscxx.so.0" ]; then
  export LD_LIBRARY_PATH="$_D1_DDS_RUNTIME:/usr/local/lib:/usr/local/lib/aarch64-linux-gnu"
  export D1_DDS_RUNTIME_OK=1
else
  export LD_LIBRARY_PATH="/usr/local/lib:/usr/local/lib/aarch64-linux-gnu"
  export D1_DDS_RUNTIME_OK=0
fi
# Lo shim introdotto successivamente mascherava l'ABI errata ma non rendeva
# Cyclone compatibile. Il runtime Unitree corretto esporta già i simboli richiesti.
unset LD_PRELOAD
# CycloneDDS: forza multicast sulla NIC verso subnet Unitree (192.168.123.x)
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" multicast="default" priority="default"/></Interfaces></General></Domain></CycloneDDS>'
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
# Stream jog <= ciclo ufficiale ~10Hz (cap soft anche in motion_profile).
export D1_JOG_STREAM_HZ="${D1_JOG_STREAM_HZ:-10}"
export D1_JOG_STREAM_DELAY_MS="${D1_JOG_STREAM_DELAY_MS:-8}"
export D1_JOG_TICK_MAX_MM="${D1_JOG_TICK_MAX_MM:-2.5}"
export D1_CART_MAX_DQ_RAD="${D1_CART_MAX_DQ_RAD:-0.035}"
export D1_JOG_ACCEL_MM_S2="${D1_JOG_ACCEL_MM_S2:-120}"
export D1_JOG_DECEL_MM_S2="${D1_JOG_DECEL_MM_S2:-150}"
export D1_JOG_MIN_MOVE_SPEED_MM_S="${D1_JOG_MIN_MOVE_SPEED_MM_S:-0}"
export D1_JOG_KICK_START_RATIO="${D1_JOG_KICK_START_RATIO:-0}"
export D1_JOG_FB_RESYNC_EVERY="${D1_JOG_FB_RESYNC_EVERY:-0}"
export D1_JOG_ENABLE_EVERY_TICKS=0
export D1_JOG_COUPLE_ON_STREAM_START="${D1_JOG_COUPLE_ON_STREAM_START:-0}"
export D1_JOG_HOLD_AFTER_MOTION="${D1_JOG_HOLD_AFTER_MOTION:-0}"
export D1_INFER_COUPLED_ON_FEEDBACK="${D1_INFER_COUPLED_ON_FEEDBACK:-0}"
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
# AUTO-calibrazione 6D: stile chen37058 Grasp-with-D1 —
# mode1 per moto (trajectory), sleep/delay tra comandi, hold idle mode0,
# niente re-couple ogni step. mode0 sui waypoint = scattoso.
export D1_GRASP6D_AUTO_MOTION_ENABLE="${D1_GRASP6D_AUTO_MOTION_ENABLE:-1}"
export D1_GRASP6D_AUTO_TRACKING_MAX_ERR_DEG="${D1_GRASP6D_AUTO_TRACKING_MAX_ERR_DEG:-15}"
export D1_GRASP6D_AUTO_TRACKING_MAX_VIOLATIONS="${D1_GRASP6D_AUTO_TRACKING_MAX_VIOLATIONS:-3}"
export D1_GRASP6D_AUTO_SETTLE_S="${D1_GRASP6D_AUTO_SETTLE_S:-1.2}"
export D1_GRASP6D_AUTO_REST_S="${D1_GRASP6D_AUTO_REST_S:-0.5}"
export D1_GRASP6D_AUTO_MOVE_DEG_PER_S="${D1_GRASP6D_AUTO_MOVE_DEG_PER_S:-5}"
export D1_GRASP6D_AUTO_MOVE_MODE="${D1_GRASP6D_AUTO_MOVE_MODE:-1}"
export D1_GRASP6D_AUTO_JOINT_STEP_DEG="${D1_GRASP6D_AUTO_JOINT_STEP_DEG:-1.5}"
export D1_GRASP6D_AUTO_WAYPOINT_DELAY_MS="${D1_GRASP6D_AUTO_WAYPOINT_DELAY_MS:-220}"
export D1_GRASP6D_AUTO_MAX_DELTA_DEG="${D1_GRASP6D_AUTO_MAX_DELTA_DEG:-22}"
# Ampiezza rotazione polso (J3/J4/J5) per la diversita' d'asse hand-eye.
# 20 deg: compromesso tra due vincoli opposti sulla camera da polso ravvicinata:
#  - troppo piccolo (14) -> griglia quasi FRONTALE -> posa planare AMBIGUA -> scartata;
#  - troppo grande (24)  -> griglia ESCE dal campo -> pochi tag.
# 20 da' abbastanza inclinazione per rompere l'ambiguita' tenendo >=12 tag.
export D1_GRASP6D_AUTO_ROT_AMPL_DEG="${D1_GRASP6D_AUTO_ROT_AMPL_DEG:-20}"
export D1_GRASP6D_AUTO_STUCK_OFFSET_SCALE="${D1_GRASP6D_AUTO_STUCK_OFFSET_SCALE:-1.2}"
# Fase SEARCH: prima di orbitare, il braccio prova alcune viste molto diverse
# (yaw ampio, piu' alto, polso meno puntato in basso) e sceglie quella con piu' tag.
export D1_GRASP6D_AUTO_SEARCH_ENABLE="${D1_GRASP6D_AUTO_SEARCH_ENABLE:-1}"
export D1_GRASP6D_AUTO_SEARCH_MAX_DELTA_DEG="${D1_GRASP6D_AUTO_SEARCH_MAX_DELTA_DEG:-16}"
export D1_GRASP6D_AUTO_SEARCH_GOOD_TAGS="${D1_GRASP6D_AUTO_SEARCH_GOOD_TAGS:-18}"
export D1_GRASP6D_AUTO_SEARCH_SETTLE_S="${D1_GRASP6D_AUTO_SEARCH_SETTLE_S:-0.8}"
# Se AUTO non aggiunge sample per N step (bloccato su pochi tag/residuo alto),
# rifa' la search per spostare il braccio su viste nuove. NON azzerare i sample
# sullo stuck: con offset piccoli la griglia resta visibile e conviene accumulare
# e potare, non ripartire (i reset causavano i loop di ricerca a 0 tag).
export D1_GRASP6D_AUTO_RESEARCH_AFTER_STUCK="${D1_GRASP6D_AUTO_RESEARCH_AFTER_STUCK:-8}"
export D1_GRASP6D_AUTO_RESET_ON_STUCK="${D1_GRASP6D_AUTO_RESET_ON_STUCK:-0}"
# Pool: accumula almeno questo numero di viste diverse prima di potare verso i
# migliori (con 6=min non si possono scartare gli outlier).
export D1_GRASP6D_AUTO_COLLECT_TARGET="${D1_GRASP6D_AUTO_COLLECT_TARGET:-12}"
# Riferimenti manuali: pose validate (errore sotto soglia) salvate e ripetute
# dall'AUTO. Delta ampio consentito perche' sono pose gia' raggiunte e valide.
export D1_GRASP6D_REF_MAX_TRANS_ERR_M="${D1_GRASP6D_REF_MAX_TRANS_ERR_M:-0.03}"
export D1_GRASP6D_REF_MAX_ROT_ERR_DEG="${D1_GRASP6D_REF_MAX_ROT_ERR_DEG:-8.0}"
export D1_GRASP6D_AUTO_REF_MAX_DELTA_DEG="${D1_GRASP6D_AUTO_REF_MAX_DELTA_DEG:-70}"
# 8 tag = 32 corner: PnP gia' stabile. Le viste INCLINATE (non ambigue) mostrano
# meno tag perche' parte della griglia esce dal campo; a 12 venivano scartate
# proprio le viste buone per la hand-eye. Reproiezione + anti-ambiguita' restano
# come garanzie di qualita'.
export D1_GRASP6D_AUTO_MIN_VISIBLE_TAGS="${D1_GRASP6D_AUTO_MIN_VISIBLE_TAGS:-8}"
export D1_GRASP6D_AUTO_MAX_REPROJ_PX="${D1_GRASP6D_AUTO_MAX_REPROJ_PX:-1.15}"
export D1_GRASP6D_AUTO_MEDIAN_FRAMES="${D1_GRASP6D_AUTO_MEDIAN_FRAMES:-2}"
# Hand-eye: soglie strette per pick sub-cm; NON alzarle per "far passare" sample mediocri.
export D1_GRASP6D_CALIB_MIN_SAMPLES="${D1_GRASP6D_CALIB_MIN_SAMPLES:-6}"
export D1_GRASP6D_CALIB_MAX_RMS_M="${D1_GRASP6D_CALIB_MAX_RMS_M:-0.025}"
export D1_GRASP6D_CALIB_MAX_RMS_DEG="${D1_GRASP6D_CALIB_MAX_RMS_DEG:-6.0}"
export D1_GRASP6D_CALIB_TARGET_ROTATION_SPAN_DEG="${D1_GRASP6D_CALIB_TARGET_ROTATION_SPAN_DEG:-30}"
export D1_GRASP6D_CALIB_MIN_NEW_TRANSLATION_M="${D1_GRASP6D_CALIB_MIN_NEW_TRANSLATION_M:-0.04}"
export D1_GRASP6D_CALIB_MIN_NEW_ROTATION_DEG="${D1_GRASP6D_CALIB_MIN_NEW_ROTATION_DEG:-12.0}"
export D1_GRASP6D_CALIB_SOFT_NEW_TRANSLATION_M="${D1_GRASP6D_CALIB_SOFT_NEW_TRANSLATION_M:-0.025}"
export D1_GRASP6D_CALIB_SOFT_NEW_ROTATION_DEG="${D1_GRASP6D_CALIB_SOFT_NEW_ROTATION_DEG:-8.0}"
# Se lontano da scan SX: offset sulla posa corrente (mai fold/safe-transit automatico).
export D1_GRASP6D_AUTO_MAX_START_DELTA_DEG="${D1_GRASP6D_AUTO_MAX_START_DELTA_DEG:-40}"
export D1_JOG_ALWAYS_COUPLED=1
export D1_JOG_AUTO_ENABLE=1
# Unico owner DDS del braccio: daemon esterno con heartbeat funcode 2 mode0.
# Flask è solo client e può riavviarsi senza interrompere l'hold.
export D1_HOLD_DAEMON_EXTERNAL=1
export D1_HOLD_SOCKET="${D1_HOLD_SOCKET:-/tmp/go2_d1_hold.sock}"
# Keepalive hold = ciclo ufficiale D1 ~10Hz. 50ms (20Hz) era troppo aggressivo
# e, con mode1, duplicava lo stream di motion → flood servo.
# Abort motion = soft hold (pose mode0); HOLD UI = hard couple. Mai snap
# measured→target (causa tipica dello "strattone" di recupero).
# Richiede reload hold daemon a freddo (braccio sostenuto) per applicare.
export D1_HOLD_HEARTBEAT_MS="${D1_HOLD_HEARTBEAT_MS:-100}"
export D1_ZERO_TRANSIT_J1_DEG="${D1_ZERO_TRANSIT_J1_DEG:--90}"
export D1_ZERO_TRANSIT_J2_DEG="${D1_ZERO_TRANSIT_J2_DEG:-90}"
export GO2_THERMAL_PROTECT="${GO2_THERMAL_PROTECT:-1}"
export GO2_THERMAL_POLL_S="${GO2_THERMAL_POLL_S:-1.0}"
export GO2_THERMAL_COOLDOWN_S="${GO2_THERMAL_COOLDOWN_S:-30}"
# Batteria critica: stop braccio → true-zero → crouch + lock motori/programmi.
# Crit 10% (non 3%): sotto ~5% Sport/braccio spesso non completano prima dello spegnimento.
export GO2_BATTERY_PROTECT="${GO2_BATTERY_PROTECT:-1}"
export GO2_BATTERY_CRIT_SOC="${GO2_BATTERY_CRIT_SOC:-10}"
export GO2_BATTERY_CLEAR_SOC="${GO2_BATTERY_CLEAR_SOC:-18}"
export GO2_BATTERY_WARN_SOC="${GO2_BATTERY_WARN_SOC:-20}"
export GO2_BATTERY_POLL_S="${GO2_BATTERY_POLL_S:-1.0}"
export GO2_BATTERY_ACTION_COOLDOWN_S="${GO2_BATTERY_ACTION_COOLDOWN_S:-20}"
# Due RealSense, solo interfacce COLOR (UVC :1.3, stream index 0).
# D456 polso: /dev/video4. D435i frontale: /dev/video10.
# app.py verifica anche VID:PID + interfaccia, quindi regge la rinumerazione USB.
export D1_WRIST_V4L_INDEX="${D1_WRIST_V4L_INDEX:-4}"
export D1_FRONT_V4L_INDEX="${D1_FRONT_V4L_INDEX:-10}"
# Presa metrica 6D: D456 polso selezionata dal product-id, mai dal numero /dev/video.
export D1_GRASP6D_ENABLED="${D1_GRASP6D_ENABLED:-1}"
export D1_WRIST_RS_PRODUCT_ID="${D1_WRIST_RS_PRODUCT_ID:-0b5c}"
export D1_WRIST_RS_SERIAL="${D1_WRIST_RS_SERIAL:-}"
export D1_WRIST_RGBD_WIDTH="${D1_WRIST_RGBD_WIDTH:-640}"
export D1_WRIST_RGBD_HEIGHT="${D1_WRIST_RGBD_HEIGHT:-480}"
export D1_WRIST_RGBD_FPS="${D1_WRIST_RGBD_FPS:-15}"
export D1_WRIST_RGBD_WARMUP="${D1_WRIST_RGBD_WARMUP:-10}"
export D1_WRIST_RS_VISUAL_PRESET="${D1_WRIST_RS_VISUAL_PRESET:-1}"
export D1_WRIST_RS_EMITTER_ENABLED="${D1_WRIST_RS_EMITTER_ENABLED:-1}"
export D1_WRIST_RS_LASER_POWER="${D1_WRIST_RS_LASER_POWER:-360}"
export D1_GRASP6D_MAX_DEPTH_M="${D1_GRASP6D_MAX_DEPTH_M:-1.2}"
# Oggetti lucidi/scuri danno pochi pixel depth: usa tutti i pixel validi per il cuboide 6D.
export D1_GRASP6D_DEPTH_STRIDE="${D1_GRASP6D_DEPTH_STRIDE:-1}"
export D1_GRASP6D_MIN_CLUSTER_POINTS="${D1_GRASP6D_MIN_CLUSTER_POINTS:-35}"
export D1_GRASP6D_PREGRASP_M="${D1_GRASP6D_PREGRASP_M:-0.10}"
export D1_GRASP6D_LIFT_M="${D1_GRASP6D_LIFT_M:-0.09}"
export D1_GRIPPER_MAX_APERTURE_M="${D1_GRIPPER_MAX_APERTURE_M:-0.085}"
export D1_ORBBEC_RGB_V4L_INDEX="${D1_ORBBEC_RGB_V4L_INDEX:-4}"
export D1_ORBBEC_LIVE_V4L_INDEX="${D1_ORBBEC_LIVE_V4L_INDEX:-4}"
export D1_PICK_ALLOW_GENERIC_RGB_FALLBACK=1
export D1_ORBBEC_RGB_ONLY=1
export D1_ORBBEC_AUTO_DISCOVERY="${D1_ORBBEC_AUTO_DISCOVERY:-0}"
export D1_ORBBEC_PREFERRED_UVC_INDEX="${D1_ORBBEC_PREFERRED_UVC_INDEX:-2}"
export D1_ORBBEC_REPROBE_EACH_CAPTURE="${D1_ORBBEC_REPROBE_EACH_CAPTURE:-0}"
export D1_ORBBEC_RELOAD_UVC="${D1_ORBBEC_RELOAD_UVC:-0}"
export D1_ORBBEC_RESET_BEFORE_CAPTURE="${D1_ORBBEC_RESET_BEFORE_CAPTURE:-0}"
export D1_ORBBEC_RESET_RELOAD_UVC="${D1_ORBBEC_RESET_RELOAD_UVC:-1}"
export D1_ORBBEC_RESET_SETTLE_S="${D1_ORBBEC_RESET_SETTLE_S:-2.5}"
export D1_ORBBEC_RESET_TIMEOUT_S="${D1_ORBBEC_RESET_TIMEOUT_S:-25}"
export D1_ORBBEC_CAPTURE_RETRIES="${D1_ORBBEC_CAPTURE_RETRIES:-6}"
export D1_ORBBEC_CAPTURE_RETRY_DELAY_S="${D1_ORBBEC_CAPTURE_RETRY_DELAY_S:-0.8}"
export D1_ORBBEC_DENY_UVC_INDEX="${D1_ORBBEC_DENY_UVC_INDEX:-1,3}"
export GO2_NX_PASSWORD="${GO2_NX_PASSWORD:-123}"
export D1_ORBBEC_V4L_INDICES="${D1_ORBBEC_V4L_INDICES:-}"
export D1_ORBBEC_V4L_DENY="${D1_ORBBEC_V4L_DENY:-0,2}"
export D1_ORBBEC_MIN_TRUE_COLOR_SPREAD="${D1_ORBBEC_MIN_TRUE_COLOR_SPREAD:-0.8}"
export D1_ORBBEC_FFMPEG="${D1_ORBBEC_FFMPEG:-1}"
export D1_ORBBEC_FFMPEG_INPUT_FORMAT="${D1_ORBBEC_FFMPEG_INPUT_FORMAT:-mjpeg}"
export D1_ORBBEC_FFMPEG_SIZE="${D1_ORBBEC_FFMPEG_SIZE:-640x480}"
export D1_ORBBEC_MIN_JPEG_CHROMA="${D1_ORBBEC_MIN_JPEG_CHROMA:-8}"
export D1_ORBBEC_JPEG_QUALITY="${D1_ORBBEC_JPEG_QUALITY:-98}"
export D1_ORBBEC_MIN_CHANNEL_SPREAD="${D1_ORBBEC_MIN_CHANNEL_SPREAD:-8}"
export D1_ORBBEC_AUTO_MIN_CHANNEL_SPREAD="${D1_ORBBEC_AUTO_MIN_CHANNEL_SPREAD:-0.05}"
export D1_ORBBEC_PINNED_MIN_CHANNEL_SPREAD="${D1_ORBBEC_PINNED_MIN_CHANNEL_SPREAD:-0.05}"
export D1_ORBBEC_MIN_SPREAD_FLOOR="${D1_ORBBEC_MIN_SPREAD_FLOOR:-0.04}"
export D1_PICK_ALLOW_GENERIC_RGB_FALLBACK="${D1_PICK_ALLOW_GENERIC_RGB_FALLBACK:-1}"
export D1_ORBBEC_V4L_DIRECT="${D1_ORBBEC_V4L_DIRECT:-1}"
export D1_ORBBEC_PREFER_V4L_DIRECT="${D1_ORBBEC_PREFER_V4L_DIRECT:-1}"
export D1_ORBBEC_USE_OPERATOR_HTTP="${D1_ORBBEC_USE_OPERATOR_HTTP:-0}"
export D1_ORBBEC_HTTP_FALLBACK="${D1_ORBBEC_HTTP_FALLBACK:-0}"
export D1_ORBBEC_MIN_FRAME_CHROMA="${D1_ORBBEC_MIN_FRAME_CHROMA:-14}"
# ROI visione: esclude bordo inferiore (chele pinza nel frame polso)
export D1_PICK_VISION_CROP_BOTTOM_FRAC="${D1_PICK_VISION_CROP_BOTTOM_FRAC:-0.30}"
export D1_PICK_PX_TO_J0_DEG="${D1_PICK_PX_TO_J0_DEG:-0.04}"
export D1_PICK_PX_TO_J1_DEG="${D1_PICK_PX_TO_J1_DEG:-0.035}"
export D1_PICK_PX_TO_J2_DEG="${D1_PICK_PX_TO_J2_DEG:-0.015}"
# Rotazione pezzo → ultimo giunto braccio (J5) prima della pinza
export D1_PICK_ORIENT_ENABLED="${D1_PICK_ORIENT_ENABLED:-1}"
export D1_PICK_ORIENT_JOINT_INDEX="${D1_PICK_ORIENT_JOINT_INDEX:-5}"
export D1_PICK_ORIENT_GAIN="${D1_PICK_ORIENT_GAIN:-1.0}"
export D1_PICK_ORIENT_SIGN="${D1_PICK_ORIENT_SIGN:-1}"
export D1_PICK_ORIENT_PERP_OFFSET_DEG="${D1_PICK_ORIENT_PERP_OFFSET_DEG:-90}"
export D1_PICK_ORIENT_MAX_DELTA_DEG="${D1_PICK_ORIENT_MAX_DELTA_DEG:-12}"
export D1_PICK_ORIENT_SMOOTH_ALPHA="${D1_PICK_ORIENT_SMOOTH_ALPHA:-0.4}"
export D1_PICK_CALIB_COUPLE_SETTLE_S="${D1_PICK_CALIB_COUPLE_SETTLE_S:-0.8}"
# Teach multiplo: sotto questa distanza posizione (solo norm/px) usa l'esempio insegnato intero
export D1_PICK_TEACH_NN_MAX="${D1_PICK_TEACH_NN_MAX:-7.5}"
# J5: solo teach entro posizione+margin (rotazione = offset teach + Δ pezzo vs quel teach)
export D1_PICK_TEACH_J5_POS_MARGIN="${D1_PICK_TEACH_J5_POS_MARGIN:-2.5}"
# Presa/foto: riferimento «Punto SCANSIONE 90»; pulsanti vanno ai due waypoint in programma
export D1_PICK_SCAN_REFERENCE="${D1_PICK_SCAN_REFERENCE:-j90}"
# Pinza J6 (D1: aperta ≈ 49.7°, chiusa ≈ 5° — NON usare J6 waypoint scansione come aperta)
export D1_GRIPPER_OPEN_DEG="${D1_GRIPPER_OPEN_DEG:-49.7}"
export D1_GRIPPER_CLOSED_DEG="${D1_GRIPPER_CLOSED_DEG:-5}"
# Riconoscimento presa D1: SOLO scatoletta blu HSV + orientamento (mai YOLO su 5056)
export D1_PICK_DETECT_BACKEND="${D1_PICK_DETECT_BACKEND:-color}"
export D1_PICK_COLOR_ONLY="${D1_PICK_COLOR_ONLY:-1}"
export D1_COLOR_BOX_H_MIN="${D1_COLOR_BOX_H_MIN:-95}"
export D1_COLOR_BOX_H_MAX="${D1_COLOR_BOX_H_MAX:-130}"
export D1_COLOR_BOX_S_MIN="${D1_COLOR_BOX_S_MIN:-45}"
export D1_COLOR_BOX_V_MIN="${D1_COLOR_BOX_V_MIN:-35}"
export D1_COLOR_BOX_MIN_AREA_FRAC="${D1_COLOR_BOX_MIN_AREA_FRAC:-0.012}"
export D1_COLOR_BOX_Y_TARGET_FRAC="${D1_COLOR_BOX_Y_TARGET_FRAC:-0.62}"
export GO2_BOX_MAX_AREA_RATIO="${GO2_BOX_MAX_AREA_RATIO:-1.0}"
unset GO2_YOLO_MODEL
export GO2_ORBBEC_PREFER_MJPEG="${GO2_ORBBEC_PREFER_MJPEG:-1}"
