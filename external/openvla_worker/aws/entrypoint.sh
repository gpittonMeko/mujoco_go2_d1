#!/bin/bash
set -euo pipefail

echo "[go2-vla-worker] backend=${GO2_GRASP_WORKER_BACKEND:-openvla}"
echo "[go2-vla-worker] OPENVLA_USE_HF=${OPENVLA_USE_HF:-0} OPENVLA_RUNTIME_STUB=${OPENVLA_RUNTIME_STUB:-0}"
echo "[go2-vla-worker] model=${OPENVLA_HF_MODEL_ID:-openvla/openvla-7b}"

if [[ "${OPENVLA_WARMUP_ON_START:-1}" == "1" ]] && [[ "${OPENVLA_RUNTIME_STUB:-0}" != "1" ]] && [[ "${OPENVLA_USE_HF:-0}" == "1" ]]; then
  echo "[go2-vla-worker] warmup HF (download pesi al primo avvio, può richiedere molti minuti)..."
  python - <<'PY' || true
import os
os.environ.setdefault("OPENVLA_USE_HF", "1")
try:
    from openvla_runtime import _ensure_hf_vla
    err = _ensure_hf_vla()
    print("warmup_ok" if err is None else f"warmup_err: {err}")
except Exception as exc:
    print(f"warmup_exc: {exc!r}")
PY
fi

exec gunicorn -w 1 -b "${WORKER_BIND_HOST:-0.0.0.0}:${WORKER_PORT:-8765}" \
  --timeout "${GUNICORN_TIMEOUT_S:-180}" \
  --access-logfile - \
  app:app
