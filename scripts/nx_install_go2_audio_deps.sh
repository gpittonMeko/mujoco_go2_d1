#!/usr/bin/env bash
# Dipendenze audio Hermes / Go2 su Jetson aarch64 (ARM).
# Uso remoto dal deploy: sudo -S bash scripts/nx_install_go2_audio_deps.sh
# Uso manuale sulla NX: sudo bash scripts/nx_install_go2_audio_deps.sh && pip3 install --user unitree-webrtc-connect
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "[nx_install_go2_audio_deps] apt: ffmpeg, mpg123, portaudio, PyAV build headers …"
apt-get update -qq
apt-get install -y -qq \
  ffmpeg \
  mpg123 \
  portaudio19-dev \
  python3-pyaudio \
  python3-dev \
  python3-pip \
  pkg-config \
  libavformat-dev \
  libavcodec-dev \
  libavdevice-dev \
  libavutil-dev \
  libswscale-dev \
  libswresample-dev \
  libavfilter-dev

echo "[nx_install_go2_audio_deps] apt OK"
