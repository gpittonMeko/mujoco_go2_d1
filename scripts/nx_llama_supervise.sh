#!/bin/bash
# Supervise llama-server (Gemma 2 1B) per Hermes offline — OpenAI-compat su 127.0.0.1:8080
set -u

REPO_BASE="${GO2_DEPLOY_REMOTE_BASE:-/home/unitree/go2_visual_dashboard}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/nx_dashboard_env.sh" ]]; then
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/nx_dashboard_env.sh"
fi

LLAMA_REPO="${GO2_LLAMA_REPO:-$HOME/llama.cpp}"
MODEL_DIR="${GO2_GEMMA_MODEL_DIR:-$REPO_BASE/models/gemma}"
MODEL_FILE="${GO2_GEMMA_MODEL_FILE:-gemma-2-2b-it-Q4_K_M.gguf}"
MODEL_PATH="$MODEL_DIR/$MODEL_FILE"
SERVER_BIN="${GO2_LLAMA_SERVER_BIN:-$LLAMA_REPO/build/bin/llama-server}"
LLAMA_HOST="${GO2_LLAMA_HOST:-127.0.0.1}"
LLAMA_PORT="${GO2_LLAMA_PORT:-8080}"
MODEL_ALIAS="${GO2_HERMES_MODEL:-gemma-2-2b-it}"
CTX="${GO2_LLAMA_CTX:-2048}"
NGL="${GO2_LLAMA_NGL:-0}"
export LD_LIBRARY_PATH="$(dirname "$SERVER_BIN")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
RESTART_S="${GO2_LLAMA_RESTART_S:-8}"
LOG="${GO2_LLAMA_LOG:-$REPO_BASE/logs/llama_server.log}"

mkdir -p "$(dirname "$LOG")"

if [[ ! -x "$SERVER_BIN" ]]; then
  echo "[llama-supervise] manca $SERVER_BIN — esegui: bash $REPO_BASE/scripts/install_llama_gemma_nx.sh" >&2
  exit 1
fi
if [[ ! -f "$MODEL_PATH" ]]; then
  echo "[llama-supervise] manca modello $MODEL_PATH" >&2
  exit 1
fi

echo "[llama-supervise] $SERVER_BIN alias=$MODEL_ALIAS port=$LLAMA_PORT log=$LOG"

while true; do
  echo "[llama-supervise] start $(date -Is)" >>"$LOG"
  "$SERVER_BIN" \
    -m "$MODEL_PATH" \
    --host "$LLAMA_HOST" \
    --port "$LLAMA_PORT" \
    -c "$CTX" \
    -ngl "$NGL" \
    --alias "$MODEL_ALIAS" \
    >>"$LOG" 2>&1 || true
  echo "[llama-supervise] exit $(date -Is) — restart in ${RESTART_S}s" >>"$LOG"
  sleep "$RESTART_S"
done
