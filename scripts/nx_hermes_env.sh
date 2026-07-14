#!/bin/bash
# Compatibilità Hermes: in produzione è integrato nella dashboard 5056.
# shellcheck disable=SC1091
if [ -f "$(dirname "$0")/nx_dashboard_env.sh" ]; then
  . "$(dirname "$0")/nx_dashboard_env.sh"
fi
export HERMES_PORT=5056
export HERMES_BIND=0.0.0.0
export HERMES_OPERATOR_URL=http://127.0.0.1:5056
export HERMES_D1_JOG_URL=http://127.0.0.1:5056
export HERMES_REQUIRE_OPERATOR=0
export GO2_HOST="${GO2_HOST:-192.168.123.18}"
# IP signaling WebRTC del Go2 (bordo cane = 192.168.123.161 sulla LAN Unitree)
export GO2_WEBRTC_IP="${GO2_WEBRTC_IP:-192.168.123.161}"
export HERMES_USE_CURSOR_SDK="${HERMES_USE_CURSOR_SDK:-0}"
export HERMES_TTS_VOICE="${HERMES_TTS_VOICE:-it}"
export HERMES_SPEAK_MODE="${HERMES_SPEAK_MODE:-megaphone}"
# Vuoto = attesa automatica = durata WAV (non tagliare a metà). Non usare 3 fissi.
export HERMES_MEGAPHONE_PLAY_S="${HERMES_MEGAPHONE_PLAY_S:-}"
# Risposta rapida: azioni SDK prima; LLM generico solo con HERMES_USE_LLM=1
export HERMES_FAST="${HERMES_FAST:-1}"
export HERMES_USE_LLM="${HERMES_USE_LLM:-0}"
export HERMES_SPEAK_ASYNC="${HERMES_SPEAK_ASYNC:-1}"
export HERMES_INTERACTION_VOICE="${HERMES_INTERACTION_VOICE:-1}"
# Sport via DDS diretto nella dashboard integrata 5056. Direct DDS su NX può SIGSEGV — fallback HTTP se DIRECT fallisce.
export HERMES_SPORT_SYNC="${HERMES_SPORT_SYNC:-0}"
export HERMES_SPORT_DIRECT="${HERMES_SPORT_DIRECT:-1}"
export GO2_ENABLE_BASE_MOTION="${GO2_ENABLE_BASE_MOTION:-1}"
export GO2_LOCAL="${GO2_LOCAL:-1}"
# piper = voce italiana chiara (locale, ONNX Paola). fast = robotico (solo debug).
export HERMES_TTS_ENGINE="${HERMES_TTS_ENGINE:-piper}"
export HERMES_GO2_VOLUME="${HERMES_GO2_VOLUME:-5}"
export HERMES_PIPER_VOICE="${HERMES_PIPER_VOICE:-it_IT-paola-medium}"
# 0 = un solo WebRTC per visione (ack+descrizione insieme). 1 = ack subito + coda lunga.
export HERMES_FAST_ACK="${HERMES_FAST_ACK:-0}"
export HERMES_SPEAK_QUEUE_MAX="${HERMES_SPEAK_QUEUE_MAX:-1}"
export HERMES_SPEAK_CLEAR_BEFORE="${HERMES_SPEAK_CLEAR_BEFORE:-1}"
export HERMES_PIPER_DIR="${HERMES_PIPER_DIR:-/home/unitree/go2_visual_dashboard/go2_dashboard/hermes/piper}"
export HERMES_PIPER_BIN_DIR="${HERMES_PIPER_BIN_DIR:-/home/unitree/go2_visual_dashboard/bin/piper}"
export HERMES_ESPEAK_SPEED="${HERMES_ESPEAK_SPEED:-260}"
export HERMES_SPEAK_MAX_CHARS="${HERMES_SPEAK_MAX_CHARS:-220}"
export HERMES_VOICE_DESC_MAX_CHARS="${HERMES_VOICE_DESC_MAX_CHARS:-200}"
export HERMES_VOICE_MAX_SENTENCES="${HERMES_VOICE_MAX_SENTENCES:-3}"
export HERMES_VISION_VOICE_MODE="${HERMES_VISION_VOICE_MODE:-playlist}"
export HERMES_MEGAPHONE_PLAY_MAX_S="${HERMES_MEGAPHONE_PLAY_MAX_S:-45}"
export HERMES_VISION_SPEAK_DETAIL="${HERMES_VISION_SPEAK_DETAIL:-1}"
export HERMES_VISION_IMAGE_DETAIL="${HERMES_VISION_IMAGE_DETAIL:-high}"
export HERMES_SPORT_PAUSE_S="${HERMES_SPORT_PAUSE_S:-8}"
# Locomozione chat: velocità passo e durata (Sport Move)
export HERMES_MOVE_VX="${HERMES_MOVE_VX:-0.25}"
export HERMES_MOVE_VY="${HERMES_MOVE_VY:-0.22}"
export HERMES_MOVE_VYAW="${HERMES_MOVE_VYAW:-0.45}"
export HERMES_STEP_DURATION_S="${HERMES_STEP_DURATION_S:-0.45}"
export HERMES_TURN_STEP_DURATION_S="${HERMES_TURN_STEP_DURATION_S:-0.55}"
export HERMES_MOVE_DEFAULT_STEPS="${HERMES_MOVE_DEFAULT_STEPS:-2}"
export HERMES_SPEAK_TIMEOUT_S="${HERMES_SPEAK_TIMEOUT_S:-120}"
export HERMES_WEBRTC_CONNECT_RETRIES="${HERMES_WEBRTC_CONNECT_RETRIES:-3}"
export HERMES_WEBRTC_RETRY_DELAY_S="${HERMES_WEBRTC_RETRY_DELAY_S:-6}"
export HERMES_GO2_CAMERA="${HERMES_GO2_CAMERA:-6}"
export HERMES_LLM_TIMEOUT_S="${HERMES_LLM_TIMEOUT_S:-15}"
export HERMES_VISION_TIMEOUT_S="${HERMES_VISION_TIMEOUT_S:-18}"
export PYTHONFAULTHANDLER="${PYTHONFAULTHANDLER:-1}"
# Opzionale: HERMES_OPENAI_API_KEY per risposte LLM migliori
