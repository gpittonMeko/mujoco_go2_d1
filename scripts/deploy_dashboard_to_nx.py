#!/usr/bin/env python3
"""Push file dashboard verso Jetson e riavvio Flask. PC: ``python scripts/deploy_dashboard_to_nx.py``.

Imposta anche avvio automatico **@reboot** (cron) tramite ``nx_boot_dashboard_wrapper.sh``: parte in
background dopo ~45 s, errori solo su ``dashboard_boot.log``, non blocca il boot. Opzionale: unit
systemd user ``go2-visual-dashboard.service`` copiata in ``~/.config/systemd/user`` (non abilitata
dal deploy per evitare doppio avvio con cron).

**Preset ``data/vis_geometry_presets.json``:** se il file esiste già sulla NX, non viene sovrascritto
(così i preset salvati dalla UI restano). Prima installazione: viene copiato dal repo. Forzare:
``GO2_DEPLOY_OVERWRITE_PRESETS=1`` sul PC che lancia lo script.

Env: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD. Probe rapido da PC: ``python scripts/probe_nx_general.py``.
URL worker grasp sulla NX (generato in ``nx_dashboard_env.sh``): ``GO2_DEPLOY_ANYGRASP_WORKER_URL``
sul PC al deploy (default ``http://192.168.123.3:8765`` — verificare con ``python scripts/nx_scan_grasp_worker_port.py``;
sovrascrivere con ``GO2_DEPLOY_ANYGRASP_WORKER_URL`` se il PC worker ha un altro IP).
Per AWS: ``GO2_DEPLOY_ANYGRASP_WORKER_URL=https://...`` e opz. ``GO2_DEPLOY_GRASP_CLOUD_MODE=1``.
"""
import json
import os
import paramiko
import subprocess
import sys
from pathlib import Path


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


REMOTE_BASE = (os.environ.get("GO2_DEPLOY_REMOTE_BASE") or "/home/unitree/go2_visual_dashboard").strip()
DEPLOY_PORT = (os.environ.get("GO2_DEPLOY_PORT") or "5056").strip() or "5056"
DEPLOY_INSTANCE = (os.environ.get("GO2_DEPLOY_INSTANCE") or "main").strip() or "main"
SKIP_AUTOSTART = os.environ.get("GO2_DEPLOY_SKIP_AUTOSTART", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SKIP_SDK_BUILD = os.environ.get("GO2_DEPLOY_SKIP_SDK_BUILD", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEPLOY_OFFLINE = os.environ.get("GO2_DEPLOY_OFFLINE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
REPO_ROOT = Path(__file__).resolve().parent.parent


def _supervise_script() -> str:
    return "nx_dashboard_dev_supervise.sh" if DEPLOY_INSTANCE == "dev" else "nx_dashboard_supervise.sh"


def _start_script() -> str:
    return "nx_start_dashboard_dev.sh" if DEPLOY_INSTANCE == "dev" else "nx_start_dashboard.sh"

REMOTE_PUSH_FILES = [
    "diagnostics_dashboard.py",
    "msg/ArmString_.hpp",
    "msg/PubServoInfo_.hpp",
    "msg/ArmString_.cpp",
    "msg/PubServoInfo_.cpp",
    "scripts/build_d1_arm_helpers.sh",
    "scripts/build_d1_sdk.sh",
    "scripts/d1_arm_dds_helper.cpp",
    "scripts/d1_arm_feedback_helper.cpp",
    "scripts/d1_arm_servo_read_python.py",
    "scripts/d1_arm_servo_stream_ndjson.py",
    "go2_dashboard/__init__.py",
    "go2_dashboard/app.py",
    "go2_dashboard/lite_app.py",
    "go2_dashboard/focus_app.py",
    "go2_dashboard/motor_health_app.py",
    "go2_dashboard/go2_motor_event_log.py",
    "go2_dashboard/go2_motor_health.py",
    "go2_dashboard/go2_motor_sport.py",
    "go2_dashboard/go2_thermal_protect.py",
    "go2_dashboard/go2_thermal_runtime.py",
    "go2_dashboard/go2_thermal_settings.py",
    "go2_dashboard/scene_meshes.py",
    "go2_dashboard/tag5_calibration_lite.py",
    "go2_dashboard/legacy_mount.py",
    "go2_dashboard/cameras.py",
    "go2_dashboard/paths.py",
    "go2_dashboard/debug_agent_log.py",
    "go2_dashboard/sdk_backend.py",
    "go2_dashboard/d1_servo_feedback.py",
    "go2_dashboard/d1_arm_publish_lite.py",
    "go2_dashboard/d1_arm_motion.py",
    "go2_dashboard/operator_arm_motion.py",
    "go2_dashboard/sport_lane.py",
    "go2_dashboard/go2_voice_playback.py",
    "go2_dashboard/go2_voice_webrtc.py",
    "go2_dashboard/operator_stack.py",
    "go2_dashboard/operator_plan_cache.py",
    "go2_dashboard/operator_session_memory.py",
    "go2_dashboard/hermes_agent.py",
    "go2_dashboard/hermes/__init__.py",
    "go2_dashboard/hermes/app.py",
    "go2_dashboard/hermes/routes.py",
    "go2_dashboard/hermes/agent.py",
    "go2_dashboard/hermes/local_agent.py",
    "go2_dashboard/hermes/context.py",
    "go2_dashboard/hermes/speech.py",
    "go2_dashboard/hermes/sdk_bridge.py",
    "go2_dashboard/hermes/vision.py",
    "go2_dashboard/hermes/actions.py",
    "go2_dashboard/hermes/phrases.py",
    "go2_dashboard/hermes/tts_local.py",
    "go2_dashboard/hermes/interaction.py",
    "go2_dashboard/hermes/locomotion.py",
    "go2_dashboard/grasp_coach_memory.py",
    "go2_dashboard/grasp_coach_agent.py",
    "go2_dashboard/operator_scene.py",
    "go2_dashboard/grasp_assessment.py",
    "go2_dashboard/grasp_ec2_control.py",
    "go2_dashboard/grasp_rgbd_embed.py",
    "go2_dashboard/grasp_phased_execute.py",
    "go2_dashboard/grasp_close_verify.py",
    "go2_dashboard/grasp_detect_debug.py",
    "go2_dashboard/grasp_teach_calib.py",
    "go2_dashboard/grasp_full_sequence.py",
    "go2_dashboard/grasp_side_approach.py",
    "go2_dashboard/grasp_autonomous_loop.py",
    "go2_dashboard/grasp_collection_mission.py",
    "go2_dashboard/grasp_teach_flow.py",
    "go2_dashboard/grasp_visual_servo.py",
    "go2_dashboard/grasp_online_calib.py",
    "go2_dashboard/orbbec_wrist_grasp.py",
    "go2_dashboard/realsense_pyrs.py",
    "go2_dashboard/orbbec_lock.py",
    "go2_dashboard/blueprints/__init__.py",
    "go2_dashboard/blueprints/meta.py",
    "go2_dashboard/blueprints/grasp.py",
    "go2_dashboard/blueprints/d1_pick_teach.py",
    "go2_dashboard/blueprints/operator_api/__init__.py",
    "go2_dashboard/blueprints/operator_api/routes.py",
    "go2_dashboard/blueprints/operator_api/helpers_hermes.py",
    "go2_dashboard/blueprints/operator_api/helpers_camera.py",
    "go2_dashboard/blueprints/operator_api/helpers_arm_http.py",
    "go2_dashboard/blueprints/operator_api/helpers_mission.py",
    "go2_dashboard/blueprints/operator_api/helpers_timing.py",
    "scripts/box_grasp_planner.py",
    "scripts/box_object_detector.py",
    "scripts/arm_kinematics_d1_template.py",
    "scripts/go2_accompany.py",
    "scripts/dds_motion_ping_once.py",
    "scripts/sport_accompany_once.py",
    "scripts/nx_print_cyclone_diag.sh",
    "scripts/nx_go2_sta_and_dds_troubleshoot.txt",
    "scripts/pc_go2_webrtc_crouch.py",
    "scripts/pc_go2_webrtc_play_mp3.py",
    "scripts/pc_go2_webrtc_speak.py",
    "scripts/install_hermes_piper_binary.py",
    "scripts/install_hermes_piper_model.py",
    "scripts/install_gemma_from_pc_to_nx.py",
    "scripts/install_llama_gemma_nx.sh",
    "scripts/nx_llama_supervise.sh",
    "scripts/requirements-hermes.txt",
    "scripts/verify_gemma_local_http.py",
    "scripts/nx_install_go2_audio_deps.sh",
    "scripts/d1_drag_follow_experimental.py",
    "scripts/sync_d1_meshes_from_package.py",
    "scripts/fetch_d1_550_from_jeewantha_github.py",
    "scripts/serve_dashboard_modular.py",
    "scripts/serve_dashboard_lite.py",
    "scripts/serve_focus_dashboard.py",
    "scripts/serve_go2_motor_health.py",
    "scripts/deploy_integrated_stack_to_nx.py",
    "scripts/deploy_d1_jog_to_nx.py",
    "scripts/deploy_hermes_to_nx.py",
    "scripts/nx_serve_foreground.sh",
    "scripts/nx_dashboard_supervise.sh",
    "scripts/nx_start_focus_dashboard.sh",
    "scripts/nx_focus_dashboard_supervise.sh",
    "scripts/nx_machine_diag.sh",
    "scripts/nx_peripheral_probe.sh",
    "scripts/nx_webrtc_reset.sh",
    "scripts/probe_nx_dds_servo.py",
    "scripts/verify_go2_lab.py",
    "scripts/verify_dashboard_http.py",
    "scripts/verify_pick_snapshot_http.py",
    "scripts/verify_d1_arm_small_move_http.py",
    "scripts/verify_grasp_coach_http.py",
    "scripts/verify_grasp_orbbec_local_http.py",
    "scripts/verify_grasp_teach_http.py",
    "scripts/verify_anygrasp_worker_http.py",
    "scripts/verify_aws_vla_worker.py",
    "scripts/pair_nx_aws_vla.py",
    "scripts/nx_fix_restart_lite.py",
    "scripts/nx_test_grasp_plan_proxy.py",
    "scripts/test_grasp_pipeline_e2e.py",
    "docs/GRASP_AWS_PIPELINE.md",
    "scripts/aws_vla_ec2_control.py",
    "data/go2_vla_ec2_state.json",
    "data/color_profiles.json",
    "scripts/go2_vla_connect_nx.ps1",
    "scripts/go2_vla_full_setup.ps1",
    "scripts/verify_go2_voice_playback.py",
    "scripts/probe_grasp_worker_network_on_nx.py",
    "scripts/nx_scan_grasp_worker_port.py",
    "scripts/nx_set_grasp_worker_url.py",
    "scripts/lab/README.md",
    "scripts/lab/lab_mission_status.py",
    "scripts/lab/lab_box_pick_cycle.py",
    "scripts/verify_hermes_smoke.py",
    "scripts/verify_hermes_integration.py",
    "scripts/setup_rtx_grasp_worker.ps1",
    "scripts/udev/99-go2-realsense-dashboard.rules",
    "scripts/go2-visual-dashboard.service",
    "templates/dashboard.html",
    "templates/dashboard_operators.html",
    "templates/focus_dashboard.html",
    "templates/focus_teach.html",
    "templates/d1_jog_dashboard.html",
    "templates/hermes.html",
    "static/hermes/hermes.css",
    "static/hermes/hermes.js",
    "templates/d1_joint_jog_modal.html",
    "templates/d1_tcp_jog_modal.html",
    "static/d1_common.js",
    "static/d1_common.css",
    "static/focus/focus_dashboard.css",
    "static/focus/focus_dashboard.js",
    "static/focus/focus_teach.js",
    "templates/_always_cam_strip.html",
    "templates/_calibration_panel.html",
    "static/css/operators.css",
    "static/js/operators_core.js",
    "static/js/operators_cameras.js",
    "static/js/operators_grasp.js",
    "static/js/operators_mission.js",
    "static/js/operators.js",
    "static/js/operators_agent.js",
    "static/js/operators_grasp_coach.js",
    "static/js/operators_movement.js",
    "static/js/operators_arm_joints.js",
    "static/js/operators_scene3d.js",
    "static/js/operators_calibration.js",
    "docs/OPERATORS_DASHBOARD_NX.md",
    "docs/HERMES_NOUS_INTEGRATION.md",
    "external/openvla_worker/README.md",
    "external/openvla_worker/requirements.txt",
    "external/openvla_worker/requirements-openvla.txt",
    "external/openvla_worker/app.py",
    "external/openvla_worker/planner_runtime.py",
    "external/openvla_worker/graspgen_runtime.py",
    "external/openvla_worker/rgbd_pointcloud.py",
    "external/openvla_worker/openvla_runtime.py",
    "external/openvla_worker/Dockerfile",
    "external/openvla_worker/aws/Dockerfile",
    "external/openvla_worker/aws/docker-compose.yml",
    "external/openvla_worker/aws/.env.example",
    "external/openvla_worker/aws/entrypoint.sh",
    "external/openvla_worker/aws/ec2-setup.sh",
    "external/openvla_worker/aws/README.md",
    "external/openvla_worker/aws/bootstrap-ec2.sh",
    "external/openvla_worker/aws/QUICKSTART.md",
    "external/openvla_worker/aws/provision-ec2.ps1",
    "external/openvla_worker/aws/bootstrap-cloudshell.sh",
    "external/openvla_worker/aws/CLOUDSHELL.md",
    "external/openvla_worker/bootstrap_worker_host.sh",
    "external/openvla_worker/bootstrap_worker_host.ps1",
    "external/openvla_worker/setup_windows_worker.py",
    "data/hermes_skills/README.md",
    "data/hermes_skills/00_go2_base_lexicon_it.md",
    "data/hermes_skills/example_lab/SKILL.md",
]
# ``data/vis_geometry_presets.json`` è gestito a parte: non sovrascrivere i preset salvati in laboratorio
# sulla NX (vedi blocco ``main()``). Primo deploy: copia dal repo se il file remoto non esiste.

# Hermes offline (Piper TTS + Gemma via llama-server locale). Attivato se GO2_DEPLOY_OFFLINE=1 (default on).
NX_OFFLINE_EXPORTS = r"""
# --- Hermes offline (Piper + Gemma 2 1B locale) ---
export GO2_HERMES_OFFLINE=1
export GO2_HERMES_OPENAI_BASE_URL=http://127.0.0.1:8080/v1
export GO2_HERMES_MODEL=gemma-2-2b-it
export OPENAI_API_KEY=local-offline
export GO2_HERMES_TTS_ENGINE=piper
export HERMES_TTS_ENGINE=piper
export HERMES_PIPER_VOICE=it_IT-paola-medium
export HERMES_PIPER_DIR=/home/unitree/go2_visual_dashboard/go2_dashboard/hermes/piper
export HERMES_PIPER_BIN_DIR=/home/unitree/go2_visual_dashboard/bin/piper
export GO2_LLAMA_REPO=/home/unitree/llama.cpp
export GO2_GEMMA_MODEL_DIR=/home/unitree/go2_visual_dashboard/models/gemma
export GO2_LLAMA_PORT=8080
# Coach/visione cloud disabilitati in modalità offline
export GO2_ENABLE_GRASP_COACH=0
export GO2_HERMES_SKILLS_DISABLE=1
"""

# Variabili condivise tra avvio manuale, boot @reboot e (opzionale) systemd --user.
NX_EXPORTS = r"""
export GO2_LOCAL=1
export GO2_ENABLE_REAL_ARM=1
# Movimento braccio da piano lite (FK giunti + IK verso punto 3D in cache).
export GO2_ENABLE_ARM_PLAN_EXECUTE=1
# POST /api/arm/emergency_hold (dashboard operator): pkill d1_arm_command + hold posa letta. 0=disattiva.
export GO2_ENABLE_ARM_ESTOP_HTTP=1
export GO2_ENABLE_BASE_MOTION=1
# Sport/Crouch/Stand: DDS Cyclone verso il cane — NON è l'IP HTTP della dashboard.
# Interfaccia Ethernet Jetson→subnet Unitree (di solito 192.168.123.x). Se usi WiFi verso l'AP del cane: wlan0.
export GO2_DDS_DOMAIN=0
export GO2_DDS_INTERFACE=eth0
# Do NOT hard-pin GO2_VIDEO_INDEX_0/6 by default: let USB auto-map + runtime recovery
# keep D456 (log.0) and D435i (log.6) separated even when /dev/videoN ordering shifts.
# export GO2_VIDEO_INDEX_0=2
# export GO2_VIDEO_INDEX_6=10
# Cyclone + iceoryx: i binari D1 sulla NX sono compatibili con il backup SDK che esporta
# ``free_iox_chunk``; le libddsc piu' nuove in /usr/local/lib non lo esportano.
_D1_SDK_BACKUP="$HOME/sdk_reinstall_backup_19700225_160102"
if [ -d "$_D1_SDK_BACKUP" ]; then
  export LD_LIBRARY_PATH="$_D1_SDK_BACKUP:/usr/local/lib:/usr/local/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
else
  export LD_LIBRARY_PATH="/usr/local/lib:/usr/local/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
_UT="${UNITREE_SDK2:-/usr/local}"
if [ "$_UT" != "/usr/local" ] && [ -d "$_UT/lib" ]; then
  export LD_LIBRARY_PATH="$_UT/lib:${LD_LIBRARY_PATH}"
elif [ "$_UT" != "/usr/local" ] && [ -d "$_UT/lib64" ]; then
  export LD_LIBRARY_PATH="$_UT/lib64:${LD_LIBRARY_PATH}"
fi
if [ -d "$HOME/cyclonedds/install/lib" ]; then
  export CYCLONEDDS_HOME="$HOME/cyclonedds/install"
  export CMAKE_PREFIX_PATH="$HOME/cyclonedds/install${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
  export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+${LD_LIBRARY_PATH}:}$HOME/cyclonedds/install/lib"
fi
# --- CycloneDDS / unitree_sdk2py (Jetson: segfault noti con wheel miste) ---
# Guida: scripts/nx_go2_sta_and_dds_troubleshoot.txt | Diagnosi: bash scripts/nx_print_cyclone_diag.sh
# Un solo albero SDK (directory che contiene unitree_sdk2py/):
# export UNITREE_SDK2_PYTHON=/home/unitree/unitree_sdk2_python
# Se la riga ``if`` sopra non basta, reinstalla binding contro la stessa build:
#   cd ~/unitree_sdk2_python && pip3 install --user -e .
# Se sulla NX gira ROS 2 e interferisce col domain DDS 0 dell'SDK:
# export ROS_DOMAIN_ID=1
# Se StandDown/StandUp non hanno effetto: abilita sport_mode dall'app Go2; poi opzionale MotionSwitcher:
# export GO2_SPORT_MOTION_PREPARE=1
# export GO2_SPORT_SELECT_MODE=normal
# Crouch/Stand: 202 subito + esito in GET /api/base/sport_last (evita router/browser che chiudono la TCP sulla RPC DDS lunga).
export GO2_SPORT_ASYNC_STAND_MODES=1
# StandDown/StandUp in subprocess (segfault Cyclone non uccide Flask).
export GO2_SPORT_SUBPROCESS_STAND_MODES=1
export GO2_GRASP_EXECUTE_ARM=1
# Presa (locale/VLA/coach): TUTTO il movimento braccio passa dallo stack del tab «Braccio D1 · giunti»
# (couple + begin_live_session + publish_goto_servo_deg7). Evita i percorsi one-shot che facevano cadere il braccio.
export GO2_GRASP_USE_OPERATOR_ARM_MOTION=1
# All'avvio sessione presa: NON uccidere/riavviare il daemon DDS (gap senza publisher → il
# braccio CADE). Il tab «Braccio D1 · giunti» non lo fa mai. Tieni 0 salvo debug specifico.
export GO2_GRASP_RECONCILE_DDS=0
export GO2_DASHBOARD_HOST=0.0.0.0
export GO2_DASHBOARD_PORT=5052
# Log tempi richiesta→risposta API operator (0 in lab = meno I/O su Jetson).
export GO2_HTTP_TIMING_LOG=0
# Dashboard operator: non avviare thread LiDAR se non serve (default 1).
export GO2_LITE_SKIP_LIDAR=1
# Hermes — riavvio dashboard non cancella questo file:
#   echo 'export OPENAI_API_KEY='"'"'(chiave)'"'" >> ~/go2_visual_dashboard/scripts/nx_secrets_dashboard.sh && chmod 600 ...
# poi in coda a nx_dashboard_env.sh (una volta): [[ -f .../nx_secrets_dashboard.sh ]] && source ...
# Oppure reinstalla dopo ogni deploy: il deploy può rigenerare nx_dashboard_env.sh.
export GO2_ENABLE_HERMES_AGENT=1
export GO2_HERMES_INTEGRATED=1
export GO2_HERMES_LOG_TURNS_TO_MEMORY=1
# Clamp Hermes arm_joint_delta (deg) per richiesta; default 45 se env unset (riduci in campo se serve).
# export GO2_HERMES_ARM_NUDGE_MAX_DELTA_DEG=20
# Hermes disk skills (Jetson): markdown / SKILL.md bundles → system prompt (vedi data/hermes_skills/README.md).
# Non è upload hosted OpenAI; equivale al «local path» degli Agent Skills.
# export GO2_HERMES_SKILLS_DIR=/home/unitree/go2_visual_dashboard/data/hermes_skills
# export GO2_HERMES_SKILLS_MAX_CHARS=14000
# export GO2_HERMES_SKILL_FILE_MAX_CHARS=4500
# export GO2_HERMES_SKILLS_DISABLE=1
# Snapshot Sport/stack nel messaggio Hermes (default on); disabilita con 0 se troppo verboso.
# export GO2_HERMES_APPEND_RUNTIME_CONTEXT=1
# export GO2_HERMES_RUNTIME_CONTEXT_MAX_CHARS=1800
# Chiave OpenAI (Hermes + TTS cloud): ``~/go2_visual_dashboard/scripts/nx_secrets_dashboard.sh`` (una tantum, fuori da git).
export GO2_HERMES_PLAY_ON_GO2=1
# Audio sul Go2 via WebRTC se RPC DDS voice fallisce (pip install unitree-webrtc-connect sulla NX).
export GO2_HERMES_PLAY_ON_GO2_WEBRTC=1
# Su Go2 EDU il RPC DDS ``voice`` spesso non risponde (3102); WebRTC è il percorso affidabile.
export GO2_HERMES_PREFER_WEBRTC=1
export GO2_WEBRTC_IP=192.168.123.161
# Jetson ARM: handshake WebRTC spesso >15s — timeout data channel (default libreria troppo corto).
export GO2_WEBRTC_DATACHANNEL_TIMEOUT_S=60
# Firmware Go2 ≥ 1.1.15: opzionale in nx_secrets_dashboard.sh → export UNITREE_AES_128_KEY=...
# Solo altoparlante Go2: niente NX né browser (silenzio finché DDS/WebRTC non funzionano).
# export GO2_HERMES_TTS_ROBOT_ONLY=1
# Firmware Go2 ≥ 1.1.15: export UNITREE_AES_128_KEY=$(unitree-fetch-aes-key --email ... --sn ... --quiet)
# HTTP POST /api/robot/voice_test (solo LAN lab): disabilitato in produzione lab presa.
# export GO2_VOICE_SELF_TEST_HTTP=1
# TTS sul cane: RPC SDK ``voice`` + ffmpeg (PCM); serve ffmpeg sulla NX. Regola sample rate: GO2_GO2_VOICE_SAMPLE_RATE.
# Go2: versione API RPC firmware‑dipendente (fallback: GO2_GO2_VOICE_API_VERSION_FALLBACKS=1.0.0.1,...)
# export GO2_GO2_VOICE_API_VERSION=1.0.0.0
# export GO2_GO2_VOICE_TRANSPORT=rpc
# Se serve solo DDS PCM (topic da verificare sul robot): export GO2_GO2_VOICE_TRANSPORT=dds
# export GO2_GO2_AUDIO_DDS_TOPIC=rt/audio
# export GO2_GO2_VOICE_TRANSPORT=auto
# Debug stderr: export GO2_GO2_VOICE_DEBUG=1
export GO2_HERMES_PLAY_ON_NX=1
# Fallback TTS sulla Jetson (mpg123/ffplay) se il servizio voice sul cane non risponde.
export GO2_HERMES_PERSONALITY=bender_meeting
# Tono VIP sarcastico trattenuto (override dalla UI Agent). Voce OpenAI default per questo preset: onyx (codice).
# Grasp Coach GPT (cloud): OFF in lab — presa via Orbbec metrico + IK (tab Presa).
export GO2_ENABLE_GRASP_COACH=0
export GO2_HERMES_SKILLS_DISABLE=1
export GO2_GRASP_EMBED_RGBD=0
export GO2_HERMES_COLLECT=0
export GO2_GRASP_COACH_LATERAL_METRIC_ONLY=1
export GO2_GRASP_AUTONOMOUS_MAX_CYCLES=12
export GO2_GRASP_AUTONOMOUS_POLL_S=0.55
export GO2_GRASP_REQUIRE_VALIDATED_EXECUTE=1
export GO2_DEPTH_SCALE_M_PER_UNIT=0.001
export GO2_GRASP_COACH_MODEL=gpt-5-nano
export GO2_GRASP_COACH_DEPTH_POLICY=alternate
export GO2_GRASP_COACH_MAX_TOKENS=420
export GO2_GRASP_COACH_TIMEOUT_S=22
export GO2_GRASP_COACH_DELAY_MS=650
export GO2_GRASP_COACH_MAX_APPROACH_BLEND=0.45
# Avanzamento di fase: se il TCP e' entro questa distanza dal target di uno stadio
# intermedio (pre_grasp/approach), il coach passa allo stadio piu' profondo -> il braccio
# SCENDE fino a `grasp` e l'auto-close della pinza scatta (prima planava 15cm sopra).
export GO2_GRASP_STAGE_ADVANCE_M=0.05
export GO2_GRASP_MIN_EXEC_TARGET_Z_BASE_LINK_M=0.05
export GO2_GRASP_GRAY_CYLINDER_MIN_Z_BASE_LINK_M=0.065
# Approach all'oggetto: lento e a sotto-step parziali (mai salto IK unico). START resta veloce.
export GO2_GRASP_COACH_APPROACH_SUBSTEPS=1
export GO2_GRASP_COACH_APPROACH_DELAY_MS=320
# Grounding metrico: coordinate target dalla DEPTH reale (RealSense polso + detect), non stima LLM.
export GO2_GRASP_COACH_METRIC_GROUNDING=1
# NO-GO zone: cono centrale ±gradi e tolleranza errore TCP (verifica cinematica).
export GO2_GRASP_COACH_NOGO_CONE_DEG=10
export GO2_GRASP_COACH_TCP_TOL_M=0.03
# Opzionale: prima di IK braccio, Sport ``balance_hold`` sul Go2 (richiede GO2_ENABLE_BASE_MOTION=1).
# export GO2_GRASP_COACH_BALANCE_HOLD_FIRST=1
# export GO2_HERMES_MODEL=gpt-4o-mini
# export GO2_HERMES_DEFAULT_CAMERA=6
# AnyGrasp / OpenVLA worker HTTP: URL effettivo aggiunto da _nx_dashboard_env_sh() (default PC lab).
# export GO2_ANYGRASP_CHECKPOINT=/path/to/checkpoint.tar  # solo SDK AnyGrasp ufficiale
# export GO2_DASHBOARD_RESTART_DELAY_S=15
export GO2_VIS_GEOMETRY_DEFAULT_PRESET=2
export GO2_CAMERA_AUTO_USB_MAP=1
# Polso RealSense D456 (log.0) + frontale D435i (log.6): mappa automatica da VID:PID USB.
# NON fissare GO2_VIDEO_INDEX_0=12 — su questa NX video12 può essere la frontale e collude con log.6.
# Se USB si rinumerano: GET /api/cameras/status → v4l_usb_auto_map, oppure frecce in tab Scene.
export GO2_WRIST_DEPTH_BACKEND=realsense
export GO2_WRIST_ORBBEC_ENABLE=0
export GO2_WRIST_REALSENSE_USB_PID=0b5c
export GO2_FRONT_REALSENSE_USB_PID=0b3a
export GO2_REALSENSE_COLOR_BACKEND=pyrealsense
export GO2_REALSENSE_STREAMS=color,depth
export GO2_REALSENSE_RGB_AUTO_EXPOSURE=1
export GO2_REALSENSE_CAPTURE_WARMUP_DISCARD=20
export GO2_REALSENSE_CAPTURE_SETTLE_S=0.5
export GO2_REALSENSE_CAPTURE_PAUSE_EXTRA_S=0.35
export GO2_REALSENSE_CAPTURE_WARMUP_DISCARD=4
export GO2_REALSENSE_CAPTURE_MIN_DEPTH_NONZERO=400
export GO2_REALSENSE_CAPTURE_TIMEOUT_MS=2200
export GO2_REALSENSE_CAPTURE_MAX_FRAMES=20
export GO2_REALSENSE_CAPTURE_MIN_PAUSE_S=0.35
export GO2_REALSENSE_CAPTURE_PAUSE_GUARD_S=0.8
# Override opzionali per scene sovraesposte / depth debole (vuoto = auto/default RealSense).
# export GO2_REALSENSE_RGB_AUTO_EXPOSURE=0
# export GO2_REALSENSE_RGB_EXPOSURE=80
# export GO2_REALSENSE_RGB_GAIN=32
# export GO2_REALSENSE_STEREO_AUTO_EXPOSURE=0
# export GO2_REALSENSE_STEREO_EXPOSURE=12000
# export GO2_REALSENSE_STEREO_GAIN=16
# export GO2_REALSENSE_LASER_POWER=360
# Presa teach: cattura veloce nel loop (dopo gate) + meno attesa tra cicli
export GO2_GRASP_TEACH_FAST=1
export GO2_GRASP_TEACH_GATES_FAST=0
export GO2_GRASP_LOOP_FAST_CAPTURE=1
export GO2_GRASP_FAST_POLL_S=0.25
export GO2_GRASP_COACH_SUPERVISOR=0
export GO2_REALSENSE_CAPTURE_FAST=1
export GO2_REALSENSE_CAPTURE_FAST_SETTLE_S=0.35
export GO2_REALSENSE_CAPTURE_FAST_PAUSE_EXTRA_S=0
export GO2_REALSENSE_CAPTURE_FAST_WARMUP_DISCARD=1
export GO2_REALSENSE_CAPTURE_FAST_MAX_FRAMES=6
export GO2_REALSENSE_CAPTURE_FAST_TIMEOUT_MS=900
# Gate pre-presa (cattura non-fast): più retry depth = target 3D più solido da distanza.
export GO2_ORBBEC_DEPTH_CAPTURE_RETRIES=2
export GO2_GRASP_TEACH_FAST_DELAY_MS=280
export GO2_WRIST_RGB_DEPTH_FALLBACK=1
export GO2_WRIST_DEPTH_PLAUSIBLE_MIN_M=0.18
export GO2_WRIST_DEPTH_PLAUSIBLE_MAX_M=1.25
export GO2_WRIST_RGB_DEPTH_REF_DIST_M=0.38
export GO2_WRIST_RGB_DEPTH_REF_AREA_RATIO=0.04
export GO2_WRIST_RGB_DEPTH_MIN_M=0.28
export GO2_WRIST_RGB_DEPTH_MAX_M=0.42
export GO2_WRIST_APPROACH_TARGET_AREA_RATIO=0.10
export GO2_WRIST_APPROACH_AREA_GAIN=1.6
export GO2_WRIST_APPROACH_SHOULDER_DEG=4.0
export GO2_WRIST_APPROACH_ELBOW_DEG=3.0
export GO2_WRIST_RGB_FALLBACK_APPROACH_BLEND=0.42
export GO2_ORBBEC_DEPTH_RING_INNER_FRAC=0.55
export GO2_ORBBEC_DEPTH_HALO_EXPAND=0.20
export GO2_ORBBEC_DEPTH_RING_MIN_SUPPORT=4
export GO2_ORBBEC_DEPTH_HALO_MIN_SUPPORT=6
export GO2_ORBBEC_DEPTH_MIN_SUPPORT=4
# Opzionale se due RealSense confondono l'auto-map: seriali da pyrealsense2 sulla NX.
# export GO2_WRIST_REALSENSE_SERIAL=...
# export GO2_FRONT_REALSENSE_SERIAL=...
export GO2_REALSENSE_V4L_DEFAULT=6
export GO2_REALSENSE_VIDEO_PROBE=1
export GO2_REALSENSE_PREFER_MJPEG=1
# Depth ROI presa metrica (alias legacy Orbbec)
export GO2_ORBBEC_DEPTH_ROI_SHRINK=0.12
# Legacy Orbbec polso (disabilitato — solo se GO2_WRIST_DEPTH_BACKEND=orbbec):
# export D1_ORBBEC_RGB_V4L_INDEX=6
# export GO2_ORBBEC_PREFER_MJPEG=1
# export GO2_ORBBEC_VIDEO_PROBE=1
# export GO2_ORBBEC_CAPTURE_SETTLE_S=0.45
# export GO2_ORBBEC_CAPTURE_TIMEOUT_MS=2200
# export GO2_ORBBEC_LOCK_TIMEOUT_S=18
# Pick teach integrato (tab Pick /api/pick/*): reset UVC + fuser -k prima di ogni foto uccide Flask
# (MJPEG Scene nello stesso processo) → browser «TypeError: Failed to fetch» su «Foto · riconoscimento».
# export D1_ORBBEC_RESET_BEFORE_CAPTURE=0
# export D1_ORBBEC_RESET_RELOAD_UVC=0
# export D1_ORBBEC_USE_OPERATOR_CACHE=1
# export D1_ORBBEC_V4L_DIRECT=0
# export D1_ORBBEC_RGB_ONLY=1
export D1_PICK_DETECT_BACKEND=color
export D1_PICK_COLOR_ONLY=1
export GO2_FOCUS_PICK_YOLO=0
# Probe RGB Orbbec: più passaggi (MJPEG/YUYV, soglia bordi default 0.26). IR puntini → alza GO2_ORBBEC_MAX_EDGE_DENSITY o GO2_ORBBEC_MAX_EDGE_DENSITY_RELAXED
# export GO2_ORBBEC_MAX_EDGE_DENSITY=0.32
# export GO2_ORBBEC_MAX_EDGE_DENSITY_RELAXED=0.48
# Traiettorie braccio lite: <1 = passi più fini (meno movimento a «scalini» su DDS). 1.0 = passi massimi come da env dedicati.
export D1_LITE_TRAJ_STEP_SCALE=0.5
# Override manuale se la board espone ordine V4L diverso: GET /api/cameras/status → v4l_usb_inventory
# export GO2_VIDEO_INDEX_0=6
# export GO2_DEPTH_VIDEO_INDEX_0=4
# Lab: JPEG da ogni /dev/videoN in inventario — disattiva in produzione
# export GO2_ALLOW_RAW_V4L_DEBUG=1
# OpenVLA: snapshot da un altro nodo V4L (es. secondo RGB Orbbec). Worker: WORKER_CAMERA_JPG_URL=http://<NX_IP>:5050/api/robot/vla_frame.jpg
# export GO2_VLA_SNAPSHOT_V4L_INDEX=10
export D1_SEARCH_MAX_CYCLES=10
export GO2_GRASP_USE_FUSED_PLAN_IK=1
export GO2_GRASP_FUSED_WITH_CENTER=1
export GO2_TRUST_WRIST_ABSOLUTE_IK=1
# Fold + START: senza goto START il braccio resta dove capita → piano/tag spesso assenti e grasp «non va».
export GO2_GRASP_START_FOLD=1
export GO2_GRASP_GOTO_SAVED_START=1
export GO2_GRASP_WAIT_TAG_BEFORE_START_POSE=0
# Ultima spiaggia se il polso non locka mai (solo se area libera / rischio accettabile in laboratorio).
export GO2_FRONT_CAMERA_FALLBACK_GRASP=1
# Grasp: meno punti fold→START (meno «cede»); yaw tag in immagine solo se GO2_GRASP_ORIENT_PREVIEW_IMAGE_AS_BASE_YAW=1.
export GO2_GRASP_FAST_START_ALIGN=1
export GO2_GRASP_START_PREHOLD_CAP=5
export D1_AUTO_COUPLE_ON_MOVE=1
export D1_GRASP_ENTRY_DELAY_MS=120
export D1_GRASP_ENTRY_TRAJ_STEP_SCALE=1.0
export D1_GRASP_ENTRY_POSTHOLD_REPEATS=4
export D1_GRASP_ZERO_MAX_STEP_DEG=6.5,3.2,2.8,4.0,6.5,6.8,10.0
export D1_GRASP_START_ALIGN_MAX_STEP_DEG=5.5,2.8,2.5,3.6,5.5,5.8,9.0
export D1_GRASP_FOLD_MAX_STEP_DEG=5.0,2.5,2.2,3.2,5.0,5.2,8.5
export GO2_ARM_MAX_REACH_M=0.58
export GO2_GRASP_ORIENT_PREVIEW_TO_TAG=1
# 0 (default): NON ruotare offset pre‑presa nel base XY con lo yaw del tag **in immagine** (errore con camera polso).
# 1 = sperimentale (può «allontanare» il braccio se ψ immagine ≠ rotazione base).
export GO2_GRASP_ORIENT_PREVIEW_IMAGE_AS_BASE_YAW=0
# 0 (default): asse grip da bordo reale AprilTag; 1 = vecchio asse AABB orizzontale (~0°).
# export GO2_GRASP_TAG_AXIS_USE_AABB=0
# Piano fuso stabile: 1 = IK dopo un solo frame «ready» (più reattivo; 2–3 se il piano jittera).
export GO2_GRASP_FUSED_CONFIRM_FRAMES=1
export D1_SEARCH_DELAY_MS=260
export D1_PLAN_DELAY_MS=420
export D1_START_ALIGN_DELAY_MS=120
export D1_FOLD_DELAY_MS=120
export D1_ZERO_TO_START_DELAY_MS=120
export D1_LITE_FILE_POSE_DELAY_MS=120
export D1_EDITOR_MOVE_DELAY_MS=340
export D1_ONE_JOINT_DELAY_MS=320
export D1_ONE_JOINT_MAX_STEP_DEG=1.4,0.7,0.6,0.9,1.4,1.6,4.0
export D1_MAX_STEP_DEG_SEARCH=2.0,1.0,1.7,2.5,2.5,3.0,5.0
export D1_MAX_STEP_DEG_GRASP=1.5,0.8,1.2,2.0,2.0,2.5,4.0
export GO2_GRASP_PHASE_HOLD_BETWEEN=1
export GO2_GRASP_PHASE_HOLD_REPEATS=14
export GO2_GRASP_PHASE_DELAY_MS=500
export GO2_GRASP_REQUIRE_START_BEFORE_PLAN=1
export GO2_START_POSE_MAX_ERROR_DEG=8
export GO2_GRASP_ENTRY_HOLD=0
export GO2_GRASP_ARM_WITH_POWER=1
export GO2_WRIST_GRASPGEN_FIRST=0
export GO2_GRASP_STAGED_MOTION=1
export GO2_GRASP_PARTIAL_FRACTIONS=0.18,0.38,0.62,0.88,1.0
export D1_GRASP_JOINT_STEP_DEG=1.5
export D1_PROG_POSITION_TOL_DEG=5.0
export D1_PROG_SOFT_TOL_DEG=8.0
export GO2_WRIST_DETECT_MIN_CY_RATIO=0.30
# color_blue_box: confidenza euristica (non YOLO) — 0.40 scartava detection valide ~0.35
export GO2_WRIST_DETECT_MIN_CONF=0.22
export GO2_WRIST_DETECT_MIN_CONF_COLOR=0.22
export GO2_WRIST_DETECT_MIN_AREA_RATIO=0.003
export GO2_WRIST_DETECT_MAX_AREA_RATIO=0.12
export GO2_WRIST_DETECT_MAX_BBOX_HEIGHT_RATIO=0.36
export GO2_WRIST_DETECT_MAX_BOTTOM_Y_RATIO=0.72
export D1_PICK_BOTTOM_CROP_FRAC=0.30
export D1_PICK_GRIPPER_EXCLUDE_BOTTOM_FRAC=0.20
export D1_PICK_GRIPPER_EXCLUDE_WIDTH_FRAC=0.62
export D1_COLOR_BOX_MAX_CY_FRAC=0.68
export D1_COLOR_BOX_MAX_BBOX_H_FRAC=0.45
export D1_COLOR_BOX_MAX_BOTTOM_Y_FRAC=0.72
export GO2_GRASP_FOLD_SETTLE_MS=500
export GO2_GRASP_START_SETTLE_MS=900
export GO2_GRASP_START_ALIGN_RETRY=1
export D1_START_ALIGN_MAX_STEP_DEG=3.0,1.4,1.2,1.8,3.0,3.2,5.5
export D1_FOLD_MAX_STEP_DEG=2.0,1.0,0.9,1.2,2.0,2.2,4.0
export D1_EDITOR_MAX_STEP_DEG=1.6,0.8,0.7,1.0,1.6,1.8,4.0
export D1_INTERP_EASE=linear
export D1_MOTION_REHOME_FEEDBACK=1
export D1_MOTION_STABLE_START=1
export D1_START_USE_MEDIAN=1
export D1_REHOME_USE_MEDIAN=1
export D1_FEEDBACK_SAMPLES=3
export D1_FEEDBACK_SAMPLE_GAP_S=0.035
export D1_FEEDBACK_MEDIAN_SAMPLES=3
export D1_FEEDBACK_MEDIAN_GAP_S=0.03
export D1_PATH_POINT_REPEAT=1
export D1_ABORTABLE_MOTION_CHUNKS=1
# Più messaggi per chunk = meno confini subprocess (meno «cede» tra uno step e l’altro).
export D1_ABORTABLE_CHUNK_MESSAGES=20
# Tra un chunk e l’altro: hold dalla posa feedback (anti-cedimento / ripetizione coppia posizione).
export GO2_D1_HOLD_BETWEEN_CHUNKS=1
export D1_INTER_CHUNK_HOLD_REPEATS=18
export D1_INTER_CHUNK_HOLD_DELAY_MS=58
# Hold generico (emergenza, publish_d1_hold_current default): più ripetizioni = meno cedimento tra comandi.
export D1_HOLD_REPEATS=22
export D1_HOLD_DELAY_MS=88
export D1_ZERO_TO_START_SPLIT=1
export D1_ZERO_TO_START_SETTLE_REPEATS=5
export D1_ZERO_TO_START_SETTLE_DELAY_MS=45
export D1_ZERO_TO_START_DELAY_MS=175
export D1_TRUE_ZERO_DELAY_MS=175
export D1_ZERO_TRANSITION_MAX_STEP_DEG=3.4,1.7,1.5,2.0,3.4,3.8,6.5
export D1_ZERO_TRANSITION_INTERP=smoothstep
export GO2_CAMERA_CACHE_FPS=20
export GO2_MJPEG_FRAME_PERIOD_S=0.05
export GO2_MJPEG_FIRST_FRAME_WAIT_S=1.8
export GO2_APRILTAG_MJPEG_PERIOD_S=0.12
export GO2_ANNOTATED_JPEG_QUALITY=72
export GO2_CLASSIC_BOX_FALLBACK=1
export GO2_YOLO_IMGSZ=640
export GO2_YOLO_CONF=0.30
export GO2_VISUAL_SERVO_STOP_ON_DIVERGE=1
export GO2_WRIST_CENTER_STEP_GAIN=0.22
export GO2_WRIST_CENTER_MAX_YAW_STEP_DEG=0.8
export GO2_WRIST_CENTER_MAX_SHOULDER_STEP_DEG=0.45
export GO2_WRIST_CENTER_MAX_WRIST_STEP_DEG=0.9
export GO2_WRIST_ONLY_CYCLE_SLEEP_S=0.18
export D1_LIVE_REPEAT=10
export D1_LIVE_DELAY_MS=26
# funcode 2 data.mode: 0 stream 10Hz-style (SDK), 1 trajectory smoothing (default live/traj = 1).
export D1_LIVE_ANGLE_MODE=1
export D1_TRAJ_ANGLE_MODE=1
# lettura servo: settle dopo primo campione (helper C++ default 80 ms; vedi D1_FEEDBACK_HELPER_SETTLE_MS)
export D1_LIVE_PREHOLD=1
export D1_LIVE_PREHOLD_REPEATS=10
export D1_LIVE_PREHOLD_DELAY_MS=48
export D1_LIVE_POSTHOLD_REPEATS=22
export D1_TRAJ_POSTHOLD_REPEATS=14
export D1_GOTO_PREHOLD=1
export D1_GOTO_PREHOLD_REPEATS=12
export D1_GOTO_PREHOLD_DELAY_MS=55
export D1_START_PREHOLD=1
export D1_START_PREHOLD_REPEATS=10
export D1_START_PREHOLD_DELAY_MS=55
export D1_POST_MOTION_HOLD_REPEATS=14
export D1_POST_MOTION_HOLD_DELAY_MS=60
""".strip()


def _worker_url_is_cloud(grasp_url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(grasp_url).hostname or "").strip()
    if not host or host in {"127.0.0.1", "localhost"}:
        return False
    if host.startswith("192.168.") or host.startswith("10."):
        return False
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                if 16 <= int(parts[1]) <= 31:
                    return False
            except ValueError:
                pass
    return True


def _default_grasp_worker_url() -> str:
    env_url = (os.environ.get("GO2_DEPLOY_ANYGRASP_WORKER_URL") or "").strip()
    if env_url:
        return env_url
    state_path = REPO_ROOT / "data" / "go2_vla_ec2_state.json"
    if state_path.is_file():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            w = (data.get("worker_url") or "").strip()
            if w:
                return w
        except (OSError, json.JSONDecodeError):
            pass
    return "http://192.168.123.3:8765"


def _nx_dashboard_env_sh() -> str:
    grasp_url = _default_grasp_worker_url()
    cloud_mode = (os.environ.get("GO2_DEPLOY_GRASP_CLOUD_MODE") or "0").strip()
    cloud_line = ""
    if cloud_mode.lower() in {"1", "true", "yes", "on"} or _worker_url_is_cloud(grasp_url):
        cloud_line = "export GO2_GRASP_CLOUD_MODE=1\n"
    extra = (
        "\n# --- Worker grasp HTTP (proxy Flask /api/grasp/*) ---\n"
        f"export GO2_ANYGRASP_WORKER_URL={grasp_url}\n"
        "export GO2_ANYGRASP_PROXY=1\n"
        f"{cloud_line}"
        "# GO2_WORKER_TOKEN → scripts/nx_secrets_dashboard.sh (non committare)\n"
    )
    secrets_hook = (
        "\n# --- Segreti locali NX (persistono ai deploy della dashboard): ---\n"
        f"#   printf 'export OPENAI_API_KEY=xxx\\\\n' > {REMOTE_BASE}/scripts/nx_secrets_dashboard.sh && chmod 600 ...\n"
        f"if [[ -f '{REMOTE_BASE}/scripts/nx_secrets_dashboard.sh' ]]; then\n"
        "  # shellcheck source=/dev/null\n"
        f"  source '{REMOTE_BASE}/scripts/nx_secrets_dashboard.sh'\n"
        "fi\n"
    )
    exports = NX_EXPORTS.replace(
        "export GO2_DASHBOARD_PORT=5052",
        f"export GO2_DASHBOARD_PORT={DEPLOY_PORT}",
    )
    exports += f"\nexport GO2_DASHBOARD_INSTANCE={DEPLOY_INSTANCE}\n"
    # Dev: nessun indice V4L fisso — auto-probe USB (Orbbec/RealSense si rinumerano spesso).
    if DEPLOY_INSTANCE == "dev":
        exports = exports.replace(
            "# export GO2_VIDEO_INDEX_0=6\n",
            "# dev: auto-probe Orbbec RGB (nessun indice V4L fisso)\n",
        )
        exports += f"\nexport GO2_FOCUS_PORT={DEPLOY_PORT}\n"
    offline_block = ""
    if DEPLOY_OFFLINE:
        offline = NX_OFFLINE_EXPORTS.replace(
            "/home/unitree/go2_visual_dashboard",
            REMOTE_BASE,
        )
        offline_block = "\n" + offline
    return (
        "#!/bin/bash\n# Sorgente unica env dashboard (deploy_dashboard_to_nx.py).\n"
        "# shellcheck disable=SC2034\n"
        + exports
        + offline_block
        + extra
        + secrets_hook
        + "\n"
    )


def _nx_supervise_sh() -> str:
    run_log = "dashboard_dev_run.log" if DEPLOY_INSTANCE == "dev" else "focus_dashboard.log"
    return f"""#!/bin/bash
# Supervisore istanza {DEPLOY_INSTANCE}. Default: focus dashboard su :5056.
set +e
cd {REMOTE_BASE} || exit 1
# shellcheck disable=SC1091
source "$PWD/scripts/nx_dashboard_env.sh"
export PYTHONFAULTHANDLER="${{PYTHONFAULTHANDLER:-1}}"
RESTART_SEC="${{GO2_DASHBOARD_RESTART_DELAY_S:-15}}"
SERVE="${{GO2_DASHBOARD_SERVE:-focus}}"
if [ "$SERVE" = "lite" ] || [ "$SERVE" = "operator" ]; then
  SERVE_SCRIPT="scripts/serve_dashboard_lite.py"
  HEALTH_PORT="${{GO2_DASHBOARD_PORT:-5052}}"
elif [ "$SERVE" = "modular" ]; then
  SERVE_SCRIPT="scripts/serve_dashboard_modular.py"
  HEALTH_PORT="${{GO2_DASHBOARD_PORT:-5051}}"
else
  export GO2_LOCAL="${{GO2_LOCAL:-1}}"
  export GO2_ENABLE_BASE_MOTION="${{GO2_ENABLE_BASE_MOTION:-1}}"
  export GO2_ENABLE_REAL_ARM="${{GO2_ENABLE_REAL_ARM:-1}}"
  export GO2_FOCUS_PORT="${{GO2_FOCUS_PORT:-{DEPLOY_PORT}}}"
  export GO2_DASHBOARD_PORT="$GO2_FOCUS_PORT"
  export HERMES_OPERATOR_URL="http://127.0.0.1:${{GO2_FOCUS_PORT}}"
  export GO2_HERMES_INTEGRATED=1
  SERVE_SCRIPT="scripts/serve_focus_dashboard.py"
  HEALTH_PORT="$GO2_FOCUS_PORT"
fi
echo "$(date -Is) nx_dashboard_supervise instance={DEPLOY_INSTANCE} pid=$$ serve=${{SERVE}} port=${{HEALTH_PORT}}"
while true; do
  echo "$(date -Is) exec $SERVE_SCRIPT"
  python3 "$SERVE_SCRIPT" >> {run_log} 2>&1
  ex=$?
  echo "$(date -Is) $SERVE_SCRIPT exited code=${{ex}} — sleep ${{RESTART_SEC}}s"
  sleep "$RESTART_SEC"
done
"""


def _nx_start_dashboard_sh(host: str) -> str:
    sup = _supervise_script()
    sup_log = "dashboard_dev_supervise.log" if DEPLOY_INSTANCE == "dev" else "dashboard_supervise.log"
    run_log = "dashboard_dev_run.log" if DEPLOY_INSTANCE == "dev" else "dashboard_run.log"
    return f"""#!/bin/bash
set -e
cd {REMOTE_BASE} || exit 1
source "{REMOTE_BASE}/scripts/nx_dashboard_env.sh"
# Never kill the Flask-owned DDS publisher while the D1 arm is coupled. Even a
# short writer gap drops torque. Maintenance can override only explicitly.
if [ "${{GO2_ALLOW_ARM_COUPLED_RESTART:-0}}" != "1" ]; then
  ARM_COUPLED="$(curl -fsS --max-time 2 "http://127.0.0.1:${{GO2_FOCUS_PORT:-{DEPLOY_PORT}}}/api/arm/status" 2>/dev/null | python3 -c 'import json,sys; print(1 if json.load(sys.stdin).get("arm_coupled") else 0)' 2>/dev/null || echo 0)"
  if [ "$ARM_COUPLED" = "1" ]; then
    echo "REFUSE_RESTART_ARM_COUPLED: DDS publisher/hold must stay alive" >&2
    exit 42
  fi
fi
# Solo questa istanza: libera la porta configurata e ferma il supervisore dedicato.
fuser -k "${{GO2_DASHBOARD_PORT}}"/tcp 2>/dev/null || true
pkill -f "{sup}" 2>/dev/null || true
{(':' if DEPLOY_INSTANCE == 'dev' else 'pkill -f "scripts/nx_focus_dashboard_supervise.sh" 2>/dev/null || true')}
{(':' if DEPLOY_INSTANCE == 'dev' else 'pkill -f "scripts/serve_focus_dashboard.py" 2>/dev/null || true')}
{(':' if DEPLOY_INSTANCE == 'dev' else 'pkill -f "scripts/serve_go2_motor_health.py" 2>/dev/null || true')}
sleep 1
python3 -c "import diagnostics_dashboard as d; print('legacy_module_ok', d.GO2_LOCAL, d.GO2_DASHBOARD_BIND, 'instance={DEPLOY_INSTANCE}', 'port={DEPLOY_PORT}')"
nohup bash scripts/{sup} >> {sup_log} 2>&1 &
echo $! > dashboard.pid
sleep 4
python3 -c "import os,urllib.request; p=os.environ.get('GO2_FOCUS_PORT') or os.environ.get('GO2_DASHBOARD_PORT','{DEPLOY_PORT}'); urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health', timeout=10); print('HTTP_HEALTH_OK')" || (
  echo HTTP_HEALTH_FAIL
  tail -40 {run_log}
  tail -20 {sup_log} 2>/dev/null || true
  exit 1
)
echo "Dashboard {DEPLOY_INSTANCE} OK — http://{host}:${{GO2_FOCUS_PORT:-{DEPLOY_PORT}}}"
echo "verify: python scripts/verify_dashboard_http.py http://{host}:${{GO2_FOCUS_PORT:-{DEPLOY_PORT}}}"
"""


def _nx_boot_dashboard_wrapper_sh() -> str:
    """Cron @reboot: tutto in background — errori non bloccano il boot."""
    log = f"{REMOTE_BASE}/dashboard_boot.log"
    return f"""#!/bin/bash
# Avvio automatico post-accensione (cron @reboot). Fallimenti solo su log, exit 0 immediato.
LOG="{log}"
{{
  echo "=== boot $(date -Is) nx_boot_dashboard_wrapper pid=$$ ==="
  # RTC Jetson spesso ancora 1970 al @reboot: attendi rete + orologio prima di avviare Flask/camera.
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    Y=$(date +%Y 2>/dev/null || echo 1970)
    if [ "$Y" -ge 2024 ]; then break; fi
    echo "wait_clock year=$Y sleep 15s ($i/12)"
    sleep 15
  done
  sleep 30
  set +e
  cd {REMOTE_BASE} || {{ echo "cd_fail"; exit 0; }}
  if ! source "{REMOTE_BASE}/scripts/nx_dashboard_env.sh"; then
    echo "env_source_fail"
    exit 0
  fi
  pkill -f nx_dashboard_supervise.sh 2>/dev/null || true
  pkill -f nx_focus_dashboard_supervise.sh 2>/dev/null || true
  pkill -f diagnostics_dashboard 2>/dev/null || true
  pkill -f serve_dashboard_modular.py 2>/dev/null || true
  pkill -f serve_dashboard_lite.py 2>/dev/null || true
  pkill -f serve_focus_dashboard.py 2>/dev/null || true
  sleep 2
  export GO2_DASHBOARD_SERVE="${{GO2_DASHBOARD_SERVE:-focus}}"
  nohup bash scripts/nx_dashboard_supervise.sh >> dashboard_supervise.log 2>&1 &
  echo $! > dashboard.pid || true
  OK=0
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sleep 6
    if python3 -c "import os,urllib.request; p=os.environ.get('GO2_FOCUS_PORT') or ('5056' if os.environ.get('GO2_DASHBOARD_SERVE','focus') == 'focus' else os.environ.get('GO2_DASHBOARD_PORT','5052')); urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health', timeout=12)" 2>/dev/null; then
      OK=1
      break
    fi
    echo "health_retry $i/15"
  done
  if [ "$OK" = "1" ]; then
    echo "HTTP_HEALTH_OK (boot)"
  else
    echo "WARN: health non risponde dopo boot (vedi dashboard_run.log e dashboard_supervise.log)"
  fi
  echo "=== fine boot $(date -Is) ==="
}} >> "$LOG" 2>&1 &
exit 0
"""


def _remote_install_crontab(ssh: paramiko.SSHClient) -> None:
    marker = "GO2_DASHBOARD_AUTOSTART"
    reboot_line = f"@reboot /bin/bash {REMOTE_BASE}/scripts/nx_boot_dashboard_wrapper.sh"
    script = f"""set +e
TMP=$(mktemp)
( crontab -l 2>/dev/null | grep -v '{marker}' | grep -v nx_boot_dashboard_wrapper.sh || true
  echo '# {marker}'
  echo '{reboot_line}'
) > "$TMP"
crontab "$TMP"
EC=$?
rm -f "$TMP"
echo "crontab: installed {marker} exit=$EC"
crontab -l 2>/dev/null | tail -n 8
"""
    stdin, stdout, stderr = ssh.exec_command(script)
    out = stdout.read().decode()
    err = stderr.read().decode()
    print(out.strip())
    if err.strip():
        print("crontab stderr:", err.strip())


def _remote_install_systemd_user_optional(ssh: paramiko.SSHClient) -> None:
    """Installa unit user (senza enable/restart): evita conflitto con pkill in nx_start / cron nohup."""
    script = f"""set +e
mkdir -p "$HOME/.config/systemd/user"
cp -f "{REMOTE_BASE}/scripts/go2-visual-dashboard.service" "$HOME/.config/systemd/user/go2-visual-dashboard.service"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if [ -S "$XDG_RUNTIME_DIR/bus" ]; then
  systemctl --user daemon-reload 2>/dev/null
  echo "SYSTEMD_USER: unit in ~/.config/systemd/user (opzionale: systemctl --user enable --now go2-visual-dashboard; prima disabilita cron se doppio avvio)"
else
  echo "SYSTEMD_USER: unit copiata; niente dbus session — usa cron @reboot o loginctl enable-linger"
fi
"""
    stdin, stdout, stderr = ssh.exec_command(script)
    print(stdout.read().decode().strip())
    err = stderr.read().decode().strip()
    if err:
        print("systemd user stderr:", err)


def _remote_install_go2_audio_deps(ssh: paramiko.SSHClient) -> None:
    """ffmpeg + portaudio (apt aarch64) e ``unitree-webrtc-connect`` (pip user) per TTS Hermes sul Go2."""
    pwd = nx_password()
    script = f"{REMOTE_BASE}/scripts/nx_install_go2_audio_deps.sh"
    apt_cmd = f"sudo -S bash {script}"
    print("[deploy] Audio Go2/Hermes (Jetson ARM): apt ffmpeg portaudio …")
    stdin, stdout, stderr = ssh.exec_command(apt_cmd, timeout=600)
    stdin.write(pwd + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if out:
        print(out)
    if code != 0:
        print(f"[deploy] AVVISO: nx_install_go2_audio_deps apt exit={code}")
        if err and "password" not in err.lower():
            print("audio apt stderr:", err[-1200:])
    else:
        print("[deploy] apt audio OK")

    pip_script = f"""set -e
pip3 install --user --upgrade 'pip>=21' wheel 2>&1 | tail -3
pip3 install --user unitree-webrtc-connect 2>&1 | tail -15
python3 -c "import importlib.util, shutil; w=bool(importlib.util.find_spec('unitree_webrtc_connect')); f=bool(shutil.which('ffmpeg')); print('verify_webrtc_pkg', w, 'verify_ffmpeg', f); assert w and f"
echo nx_pip_audio_deps_ok
"""
    print("[deploy] pip unitree-webrtc-connect (user, aarch64) …")
    pip_stdin, pip_stdout, pip_stderr = ssh.exec_command(pip_script, timeout=900)
    pip_code = pip_stdout.channel.recv_exit_status()
    pip_out = pip_stdout.read().decode(errors="replace").strip()
    pip_err = pip_stderr.read().decode(errors="replace").strip()
    if pip_out:
        print(pip_out)
    if pip_code != 0:
        print(f"[deploy] ERRORE: pip unitree-webrtc-connect exit={pip_code}")
        if pip_err:
            print("pip stderr:", pip_err[-1500:])
    else:
        print("[deploy] unitree-webrtc-connect import OK")


def _remote_install_piper_tts(ssh: paramiko.SSHClient) -> None:
    """Piper ONNX (arm64) + modello italiano — salta se già presente (di solito da deploy Hermes :5054)."""
    check = (
        f"test -x {REMOTE_BASE}/bin/piper/piper/piper && "
        f"test -f {REMOTE_BASE}/go2_dashboard/hermes/piper/it_IT-paola-medium.onnx && "
        "echo PIPER_ALREADY_OK"
    )
    _, so, _ = ssh.exec_command(check, timeout=30)
    if "PIPER_ALREADY_OK" in so.read().decode(errors="replace"):
        print("[deploy] Piper già installato sulla NX — skip reinstall")
        return
    pip_cmd = (
        f"cd {REMOTE_BASE} && "
        "python3 -m pip install -q -r scripts/requirements-hermes.txt 2>/dev/null || "
        "python3 -m pip install -q onnxruntime pydub 2>/dev/null; "
        "python3 -m pip show onnxruntime 2>/dev/null | grep -E '^Name:|^Version:' || true"
    )
    print("[deploy] pip onnxruntime (Hermes Piper) …")
    _, pip_out, _ = ssh.exec_command(pip_cmd, timeout=180)
    out = pip_out.read().decode(errors="replace").strip()
    if out:
        print(out)

    pwd = nx_password()
    print("[deploy] espeak-ng (fallback TTS) …")
    ssh.exec_command(
        "command -v espeak-ng >/dev/null 2>&1 || "
        f"(echo '{pwd}' | sudo -S apt-get install -y espeak-ng 2>/dev/null) || true",
        timeout=120,
    )

    print("[deploy] Piper binary arm64 …")
    _, so, _ = ssh.exec_command(
        f"cd {REMOTE_BASE} && python3 scripts/install_hermes_piper_binary.py 2>&1",
        timeout=300,
    )
    piper_bin = so.read().decode(errors="replace")
    if piper_bin.strip():
        print(piper_bin.strip()[-400:])
    so.channel.recv_exit_status()

    print("[deploy] Piper model (voce IT) …")
    _, so2, _ = ssh.exec_command(
        f"cd {REMOTE_BASE} && "
        f"export HERMES_PIPER_DIR={REMOTE_BASE}/go2_dashboard/hermes/piper && "
        f"export HERMES_PIPER_BIN_DIR={REMOTE_BASE}/bin/piper && "
        "python3 scripts/install_hermes_piper_model.py 2>&1",
        timeout=300,
    )
    piper_model = so2.read().decode(errors="replace")
    if piper_model.strip():
        print(piper_model.strip()[-500:])
    so2.channel.recv_exit_status()


def _remote_upload_llama_binary(ssh: paramiko.SSHClient) -> None:
    """Carica llama.cpp dal PC e compila llama-server sulla NX (no Internet GitHub)."""
    if not DEPLOY_OFFLINE:
        return
    check = "test -x $HOME/llama.cpp/build/bin/llama-server && echo LLAMA_BIN_OK"
    _, so, _ = ssh.exec_command(check, timeout=20)
    if "LLAMA_BIN_OK" in so.read().decode(errors="replace"):
        print("[deploy] llama-server gia presente sulla NX — skip build")
        _remote_start_llama_supervise(ssh)
        return
    script_path = REPO_ROOT / "scripts" / "install_gemma_from_pc_to_nx.py"
    if not script_path.is_file():
        print("[deploy] AVVISO: install_gemma_from_pc_to_nx.py mancante — llama-server non installato")
        return
    print("[deploy] llama-server: upload sorgenti + build CUDA sulla NX …")
    proc = subprocess.run(
        [sys.executable, str(script_path), "--binary-only"],
        cwd=str(REPO_ROOT),
        timeout=7200,
        check=False,
    )
    if proc.returncode != 0:
        print(f"[deploy] AVVISO: install llama binary exit={proc.returncode}")
    else:
        _remote_start_llama_supervise(ssh)


def _remote_start_llama_supervise(ssh: paramiko.SSHClient) -> None:
    """Avvia (o riavvia) llama-server Gemma in background se build+modello presenti."""
    if not DEPLOY_OFFLINE:
        return
    script = f"""set -e
chmod +x {REMOTE_BASE}/scripts/install_llama_gemma_nx.sh {REMOTE_BASE}/scripts/nx_llama_supervise.sh 2>/dev/null || true
mkdir -p {REMOTE_BASE}/logs
LLAMA="$HOME/llama.cpp/build/bin/llama-server"
MODEL="{REMOTE_BASE}/models/gemma/gemma-2-2b-it-Q4_K_M.gguf"
if [[ -x "$LLAMA" && -f "$MODEL" ]]; then
  pkill -f 'nx_llama_supervise.sh' 2>/dev/null || true
  pkill -f 'llama-server' 2>/dev/null || true
  sleep 1
  nohup bash {REMOTE_BASE}/scripts/nx_llama_supervise.sh >> {REMOTE_BASE}/logs/llama_supervise_boot.log 2>&1 &
  echo llama_supervise_started
else
  echo llama_skip_run_install_llama_gemma_nx_sh
fi
"""
    print("[deploy] llama-server Gemma (se già installato) …")
    _, stdout, stderr = ssh.exec_command(script, timeout=30)
    print(stdout.read().decode(errors="replace").strip())
    err = stderr.read().decode(errors="replace").strip()
    if err:
        print("llama stderr:", err[-600:])


def _remote_install_realsense_udev(ssh: paramiko.SSHClient) -> None:
    """Regola udev per /dev/video* RealSense (8086:0b3a) — richiede sudo sulla NX."""
    pwd = nx_password()
    src = f"{REMOTE_BASE}/scripts/udev/99-go2-realsense-dashboard.rules"
    dst = "/etc/udev/rules.d/99-go2-realsense-dashboard.rules"
    cmd = (
        f"sudo -S bash -c 'cp -f {src} {dst} && udevadm control --reload-rules "
        f"&& udevadm trigger --subsystem-match=video4linux'"
    )
    stdin, stdout, stderr = ssh.exec_command(cmd)
    stdin.write(pwd + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if code == 0:
        print("[deploy] udev RealSense: installata e trigger video4linux OK")
    else:
        print(f"[deploy] udev RealSense: sudo fallito (exit {code}) — copia manuale su NX:")
        print(f"  sudo cp {src} {dst} && sudo udevadm control --reload-rules && sudo udevadm trigger -c change -s video4linux")
    if out:
        print(out)
    if err and "password" not in err.lower():
        print("udev stderr:", err)


def _remote_run_probe(ssh: paramiko.SSHClient) -> None:
    stdin, stdout, stderr = ssh.exec_command(f"bash {REMOTE_BASE}/scripts/nx_peripheral_probe.sh")
    print("--- nx_peripheral_probe ---")
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("probe stderr:", err)


def _d1_jog_rel_paths() -> list[str]:
    d = REPO_ROOT / "go2_dashboard" / "d1_jog"
    if not d.is_dir():
        return []
    out: list[str] = []
    for p in sorted(d.rglob("*.py")):
        out.append(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    return out


def _d1_sdk_rel_paths() -> list[str]:
    """Sorgenti CMake SDK D1 (ramo d1-arm-control) per ``bin/d1_sdk_*``."""
    sdk = REPO_ROOT / "D1 550 Workspace" / "d1_sdk"
    if not sdk.is_dir():
        return []
    out: list[str] = []
    for p in sorted(sdk.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".cpp", ".hpp", ".txt", ".cmake"):
            out.append(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    return out


def _remote_build_d1_sdk(ssh: paramiko.SSHClient) -> None:
    """Compila ``bin/d1_sdk_command`` e ``bin/d1_sdk_feedback`` (protocollo ufficiale D1)."""
    sdk_cmake = REPO_ROOT / "D1 550 Workspace" / "d1_sdk" / "d1_sdk" / "CMakeLists.txt"
    if not sdk_cmake.is_file():
        print("[deploy] skip build_d1_sdk — manca D1 550 Workspace/d1_sdk (merge da origin/d1-arm-control)")
        return
    print("[deploy] Compilazione SDK D1 (bin/d1_sdk_command, d1_sdk_feedback) …")
    log = "/tmp/go2_d1_sdk_build.log"
    script = f"""set +e
cd "{REMOTE_BASE}"
mkdir -p bin
set -a
if [ -f scripts/nx_dashboard_env.sh ]; then
  # shellcheck disable=SC1091
  . scripts/nx_dashboard_env.sh
fi
set +a
bash scripts/build_d1_sdk.sh >{log} 2>&1
EC=$?
echo "EXIT_CODE=$EC" >>{log}
cat {log}
chmod +x bin/d1_sdk_command bin/d1_sdk_feedback bin/d1_sdk_get_angles 2>/dev/null || true
ls -la bin/d1_sdk_command bin/d1_sdk_feedback 2>/dev/null || true
exit $EC
"""
    stdin, stdout, stderr = ssh.exec_command(script)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.strip())
    if err.strip():
        print("build_d1_sdk stderr:", err.strip())
    if code != 0:
        print(
            f"[deploy] AVVISO: build_d1_sdk.sh exit={code} — "
            "la dashboard userà fallback d1_arm_command se presente."
        )
    else:
        print("[deploy] SDK D1 compilato OK (backend braccio preferito).")


def _remote_build_d1_arm_helpers(ssh: paramiko.SSHClient) -> None:
    """Compila ``bin/d1_arm_command`` e ``bin/d1_arm_feedback_helper`` sulla NX (g++ + Unitree SDK2)."""
    print("[deploy] Compilazione helper braccio D1 (bin/d1_arm_command) …")
    log = "/tmp/go2_d1_arm_helpers_build.log"
    script = f"""set +e
cd "{REMOTE_BASE}"
mkdir -p bin
rm -f bin/d1_arm_command bin/d1_arm_feedback_helper
set -a
if [ -f scripts/nx_dashboard_env.sh ]; then
  # shellcheck disable=SC1091
  . scripts/nx_dashboard_env.sh
fi
set +a
bash scripts/build_d1_arm_helpers.sh >{log} 2>&1
EC=$?
echo "EXIT_CODE=$EC" >>{log}
cat {log}
chmod +x bin/d1_arm_command bin/d1_arm_feedback_helper 2>/dev/null || true
ls -la bin/d1_arm_command bin/d1_arm_feedback_helper 2>/dev/null || true
exit $EC
"""
    stdin, stdout, stderr = ssh.exec_command(script)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.strip())
    if err.strip():
        print("build_d1_helpers stderr:", err.strip())
    if code != 0:
        print(
            f"[deploy] ERRORE: build_d1_arm_helpers.sh exit={code} — "
            "verifica g++, Unitree SDK2 e CYCLONEDDS_HOME sulla NX."
        )
    else:
        print("[deploy] Helper D1 compilati OK.")


def _mesh_rel_paths() -> list[str]:
    """Path relativi alla root repo per mesh Go2 (.obj) e D1 (.STL) servite dal viewer."""
    out: list[str] = []
    d1 = REPO_ROOT / "unitree_mujoco/unitree_robots/go2_d1/d1_550_description/meshes"
    if d1.is_dir():
        for p in sorted(d1.glob("*.STL")):
            out.append(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    ad = REPO_ROOT / "unitree_mujoco/unitree_robots/go2_d1/assets"
    if ad.is_dir():
        for p in sorted(ad.glob("*.obj")):
            out.append(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    return out


def _d1_urdf_rel_paths() -> list[str]:
    """URDF e sidecar da repo Jeewantha (stessi file usati dal parser in diagnostics_dashboard)."""
    d = REPO_ROOT / "unitree_mujoco/unitree_robots/go2_d1/d1_550_description/urdf"
    if not d.is_dir():
        return []
    out: list[str] = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in (".urdf", ".xacro", ".csv") and p.is_file():
            out.append(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    return out


def _scene_xml_rel_paths() -> list[str]:
    """XML MuJoCo per ``/api/mujoco/preview.png`` (NX deve avere gli include)."""
    rels = [
        "unitree_mujoco/unitree_robots/go2_d1/scene_d1_mesh.xml",
        "unitree_mujoco/unitree_robots/go2_d1/go2_d1_d1mesh.xml",
    ]
    return [r for r in rels if (REPO_ROOT / r).is_file()]


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Deploy dashboard lite sulla Jetson NX")
    ap.add_argument(
        "--skip-meshes",
        action="store_true",
        help="Non caricare mesh .obj/.STL (usa se già sulla NX; equivale a GO2_DEPLOY_SKIP_MESHES=1)",
    )
    args = ap.parse_args()
    if args.skip_meshes:
        os.environ["GO2_DEPLOY_SKIP_MESHES"] = "1"

    host = nx_host()
    skip_m = os.environ.get("GO2_DEPLOY_SKIP_MESHES", "").strip().lower() in ("1", "true", "yes", "on")
    inst_tag = f" instance={DEPLOY_INSTANCE} port={DEPLOY_PORT} dir={REMOTE_BASE}"
    print(f"[deploy] Connecting SSH {nx_user()}@{host} …{inst_tag}" + (" (skip meshes)" if skip_m else ""))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=nx_user(), password=nx_password(), timeout=45)
    stdin, stdout, stderr = ssh.exec_command(
        f"mkdir -p {REMOTE_BASE}/unitree_mujoco/unitree_robots/go2_d1 "
        f"{REMOTE_BASE}/unitree_mujoco/unitree_robots/go2_d1/assets "
        f"{REMOTE_BASE}/unitree_mujoco/unitree_robots/go2_d1/d1_550_description/meshes "
        f"{REMOTE_BASE}/unitree_mujoco/unitree_robots/go2_d1/d1_550_description/urdf "
        f"{REMOTE_BASE}/go2_dashboard/blueprints/operator_api "
        f"{REMOTE_BASE}/go2_dashboard/d1_jog "
        f"{REMOTE_BASE}/go2_dashboard/hermes "
        f'"{REMOTE_BASE}/D1 550 Workspace/d1_sdk" '
        f"{REMOTE_BASE}/msg "
        f"{REMOTE_BASE}/templates "
        f"{REMOTE_BASE}/static/css "
        f"{REMOTE_BASE}/static/js "
        f"{REMOTE_BASE}/static/focus "
        f"{REMOTE_BASE}/static/hermes "
        f"{REMOTE_BASE}/docs "
        f"{REMOTE_BASE}/external/openvla_worker "
        f"{REMOTE_BASE}/external/openvla_worker/aws "
        f"{REMOTE_BASE}/data "
        f"{REMOTE_BASE}/data/hermes_skills "
        f"{REMOTE_BASE}/data/hermes_skills/example_lab "
        f"{REMOTE_BASE}/bin "
        f"{REMOTE_BASE}/scripts/lab "
        f"{REMOTE_BASE}/scripts/udev"
    )
    stdout.channel.recv_exit_status()
    sftp = ssh.open_sftp()
    rel_presets = "data/vis_geometry_presets.json"
    rp_presets = f"{REMOTE_BASE}/{rel_presets}"
    force_presets = os.environ.get("GO2_DEPLOY_OVERWRITE_PRESETS", "").strip().lower() in ("1", "true", "yes", "on")
    try:
        sftp.stat(rp_presets)
        remote_presets_exist = True
    except Exception:
        remote_presets_exist = False
    for rel in REMOTE_PUSH_FILES:
        loc = REPO_ROOT / rel
        if not loc.is_file():
            print("skip missing", loc)
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed", rel)
    loc_pr = REPO_ROOT / rel_presets
    if loc_pr.is_file():
        if force_presets or not remote_presets_exist:
            sftp.put(str(loc_pr), rp_presets)
            print(
                "pushed",
                rel_presets,
                "(GO2_DEPLOY_OVERWRITE_PRESETS=1)" if force_presets and remote_presets_exist else "",
            )
        else:
            print(
                "[deploy] keep remote",
                rel_presets,
                "— preset salvati sulla NX non sovrascritti (imposta GO2_DEPLOY_OVERWRITE_PRESETS=1 per forzare)",
            )
    skip_meshes = os.environ.get("GO2_DEPLOY_SKIP_MESHES", "").strip().lower() in ("1", "true", "yes", "on")
    if skip_meshes:
        print("[deploy] GO2_DEPLOY_SKIP_MESHES=1 — salto upload mesh .obj/.STL (già sulla NX o non necessarie ora)")
    for rel in _mesh_rel_paths():
        if skip_meshes:
            break
        loc = REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed mesh", rel)
    for rel in _d1_urdf_rel_paths():
        loc = REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed urdf", rel)
    for rel in _scene_xml_rel_paths():
        loc = REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed scene_xml", rel)
    for rel in _d1_jog_rel_paths():
        loc = REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed d1_jog", rel)
    if DEPLOY_INSTANCE != "dev":
        for rel in _d1_sdk_rel_paths():
            loc = REPO_ROOT / rel
            if not loc.is_file():
                continue
            remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
            sftp.put(str(loc), remote_path)
            print("pushed d1_sdk_src", rel)
    else:
        print("[deploy] dev: skip upload sorgenti D1 SDK (usa bin/ symlink dalla tree principale)")

    generated_scripts: list[tuple[str, str, int]] = [
        (f"{REMOTE_BASE}/scripts/nx_dashboard_env.sh", _nx_dashboard_env_sh(), 0o644),
        (f"{REMOTE_BASE}/scripts/{_start_script()}", _nx_start_dashboard_sh(host), 0o755),
        (f"{REMOTE_BASE}/scripts/{_supervise_script()}", _nx_supervise_sh(), 0o755),
    ]
    if not SKIP_AUTOSTART:
        generated_scripts.append(
            (f"{REMOTE_BASE}/scripts/nx_boot_dashboard_wrapper.sh", _nx_boot_dashboard_wrapper_sh(), 0o755),
        )
    for path, content, mode in generated_scripts:
        with sftp.file(path, "wb") as rf:
            rf.write(content.encode("utf-8"))
        sftp.chmod(path, mode)
        print("wrote", path)

    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_serve_foreground.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_dashboard_supervise.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_machine_diag.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_peripheral_probe.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_print_cyclone_diag.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_install_go2_audio_deps.sh", 0o755)
    # Checkout Windows può lasciare CRLF negli .sh — bash sulla NX si rompe (set: +\r).
    strip_stdin, strip_stdout, strip_stderr = ssh.exec_command(
        f"bash -lc \"sed -i 's/\\\\r$//' {REMOTE_BASE}/scripts/*.sh {REMOTE_BASE}/external/openvla_worker/*.sh 2>/dev/null || true\""
    )
    strip_stdout.channel.recv_exit_status()
    sftp.close()

    skip_sdk_build = SKIP_SDK_BUILD or DEPLOY_INSTANCE == "dev"
    if DEPLOY_INSTANCE == "dev":
        main_tree = "/home/unitree/go2_visual_dashboard"
        stdin, stdout, stderr = ssh.exec_command(
            f"mkdir -p {REMOTE_BASE} && "
            f"rm -rf {REMOTE_BASE}/bin {REMOTE_BASE}/unitree_mujoco && "
            f"ln -sfn {main_tree}/bin {REMOTE_BASE}/bin && "
            f"ln -sfn {main_tree}/unitree_mujoco {REMOTE_BASE}/unitree_mujoco && "
            f"ls -la {REMOTE_BASE}/bin {REMOTE_BASE}/unitree_mujoco | head -6"
        )
        print(stdout.read().decode(errors="replace").strip())
        print("[deploy] dev: bin/ + unitree_mujoco/ symlink dalla tree principale")

    if not skip_sdk_build:
        _remote_build_d1_sdk(ssh)
        _remote_build_d1_arm_helpers(ssh)
    else:
        print("[deploy] SKIP build SDK/helpers (usa bin esistente)")

    if not SKIP_AUTOSTART:
        print("[deploy] Dipendenze audio Hermes / WebRTC Go2 (ARM) …")
        _remote_install_go2_audio_deps(ssh)
        if DEPLOY_OFFLINE:
            print("[deploy] Hermes offline: Piper TTS …")
            _remote_install_piper_tts(ssh)
            _remote_upload_llama_binary(ssh)
        print("[deploy] Install cron @reboot (non blocca boot; log in dashboard_boot.log) …")
        _remote_install_crontab(ssh)
        print("[deploy] Optional systemd --user unit …")
        _remote_install_systemd_user_optional(ssh)
        print("[deploy] udev RealSense (permessi /dev/video*) …")
        _remote_install_realsense_udev(ssh)
    else:
        print("[deploy] SKIP autostart/cron/udev (istanza dev — non tocca il setup del collega)")

    if DEPLOY_INSTANCE == "dev":
        main_secrets = "/home/unitree/go2_visual_dashboard/scripts/nx_secrets_dashboard.sh"
        dev_secrets = f"{REMOTE_BASE}/scripts/nx_secrets_dashboard.sh"
        stdin, stdout, stderr = ssh.exec_command(
            f"test -f '{dev_secrets}' || ( test -f '{main_secrets}' && cp '{main_secrets}' '{dev_secrets}' && chmod 600 '{dev_secrets}' && echo 'copied secrets from main' ) || echo 'no secrets file'"
        )
        print(stdout.read().decode(errors="replace").strip())

    print(f"[deploy] Riavvio dashboard {DEPLOY_INSTANCE} …")
    stdin, stdout, stderr = ssh.exec_command(f"bash {REMOTE_BASE}/scripts/{_start_script()}")
    print(stdout.read().decode())
    err = stderr.read().decode()
    if err.strip():
        print("stderr:", err)

    print("[deploy] Probe periferiche / rete …")
    _remote_run_probe(ssh)
    ssh.close()


if __name__ == "__main__":
    main()
