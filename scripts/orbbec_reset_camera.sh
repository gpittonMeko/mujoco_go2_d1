#!/bin/bash
# Reset Orbbec Gemini: libera V4L e ricarica driver uvcvideo (esponde /dev/video6 RGB).
set -e
cd "$(dirname "$0")/.." || exit 1
if [ -f scripts/nx_d1_jog_env.sh ]; then
  # shellcheck disable=SC1091
  . scripts/nx_d1_jog_env.sh
fi
PIN="${D1_ORBBEC_RGB_V4L_INDEX:-6}"
for i in 0 1 2 3 4 5 6 7; do
  fuser -k "/dev/video${i}" 2>/dev/null || true
done
if [ "${D1_ORBBEC_RESET_RELOAD_UVC:-1}" = "1" ]; then
  echo "${GO2_NX_PASSWORD:-123}" | sudo -S sh -c 'modprobe -r uvcvideo 2>/dev/null; modprobe uvcvideo' 2>/dev/null || true
fi
sleep "${D1_ORBBEC_RESET_SETTLE_S:-2.5}"
if [ ! -e "/dev/video${PIN}" ]; then
  echo "orbbec_reset: missing /dev/video${PIN}" >&2
  exit 1
fi
echo "orbbec_reset_ok video${PIN}"
