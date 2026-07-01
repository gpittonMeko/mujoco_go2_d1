#!/bin/bash
# Build llama.cpp (CUDA sm_87) sulla Jetson NX e scarica Gemma 2 1B IT (GGUF Q4_K_M).
# Uso: bash scripts/install_llama_gemma_nx.sh
# Env opzionali: GO2_LLAMA_REPO, GO2_GEMMA_GGUF_URL, GO2_GEMMA_MODEL_FILE, GO2_LLAMA_PORT
set -euo pipefail

REPO_BASE="${GO2_DEPLOY_REMOTE_BASE:-/home/unitree/go2_visual_dashboard}"
LLAMA_REPO="${GO2_LLAMA_REPO:-$HOME/llama.cpp}"
MODEL_DIR="${GO2_GEMMA_MODEL_DIR:-$REPO_BASE/models/gemma}"
MODEL_FILE="${GO2_GEMMA_MODEL_FILE:-gemma-2-1b-it-Q4_K_M.gguf}"
MODEL_PATH="$MODEL_DIR/$MODEL_FILE"
GGUF_URL="${GO2_GEMMA_GGUF_URL:-https://huggingface.co/bartowski/gemma-2-1b-it-GGUF/resolve/main/gemma-2-1b-it-Q4_K_M.gguf}"
LLAMA_PORT="${GO2_LLAMA_PORT:-8080}"
BUILD_DIR="$LLAMA_REPO/build"
SERVER_BIN="$BUILD_DIR/bin/llama-server"

echo "[llama-gemma] repo=$LLAMA_REPO model=$MODEL_PATH port=$LLAMA_PORT"

if ! command -v cmake >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
  echo "[llama-gemma] apt build deps…"
  sudo apt-get update -qq
  sudo apt-get install -y build-essential cmake git libcurl4-openssl-dev
fi

if [[ ! -d "$LLAMA_REPO/.git" ]]; then
  echo "[llama-gemma] clone llama.cpp…"
  git clone --depth 1 https://github.com/ggerganov/llama.cpp "$LLAMA_REPO"
fi

if [[ ! -x "$SERVER_BIN" ]]; then
  echo "[llama-gemma] cmake build (CUDA sm_87)…"
  cmake -S "$LLAMA_REPO" -B "$BUILD_DIR" \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=87 \
    -DLLAMA_CURL=ON
  cmake --build "$BUILD_DIR" --config Release -j"$(nproc)"
fi

mkdir -p "$MODEL_DIR"
if [[ ! -f "$MODEL_PATH" ]] || [[ "$(stat -c%s "$MODEL_PATH" 2>/dev/null || echo 0)" -lt 1000000 ]]; then
  echo "[llama-gemma] download $MODEL_FILE …"
  if command -v curl >/dev/null 2>&1; then
    curl -L --retry 3 -o "$MODEL_PATH" "$GGUF_URL"
  else
    wget -O "$MODEL_PATH" "$GGUF_URL"
  fi
fi

echo "[llama-gemma] OK server=$SERVER_BIN model=$MODEL_PATH ($(stat -c%s "$MODEL_PATH") bytes)"
echo "[llama-gemma] Avvio: bash $REPO_BASE/scripts/nx_llama_supervise.sh"
