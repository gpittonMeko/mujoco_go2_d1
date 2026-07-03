#!/bin/bash
# Avvio automatico post-accensione della dashboard D1 jog (5056).
# Non blocca il boot: logga e termina sempre con exit 0.
# Marker crontab: GO2_D1_JOG_AUTOSTART
LOG="/home/unitree/go2_visual_dashboard/d1_jog_boot.log"
{
  echo "=== boot $(date -Is) nx_boot_d1_jog_wrapper pid=$$ ==="
  sleep 45
  set +e
  cd /home/unitree/go2_visual_dashboard || {
    echo "cd_fail"
    exit 0
  }
  if ! source "/home/unitree/go2_visual_dashboard/scripts/nx_d1_jog_env.sh"; then
    echo "env_source_fail"
    exit 0
  fi
  ATTEMPTS="${D1_JOG_BOOT_ATTEMPTS:-3}"
  DELAY_S="${D1_JOG_BOOT_RETRY_DELAY_S:-20}"
  ok=0
  i=1
  while [ "$i" -le "$ATTEMPTS" ]; do
    echo "--- attempt $i/$ATTEMPTS $(date -Is) ---"
    if bash scripts/nx_start_d1_jog.sh; then
      echo "D1_JOG_BOOT_OK attempt=$i"
      ok=1
      break
    fi
    echo "D1_JOG_BOOT_FAIL attempt=$i"
    sleep "$DELAY_S"
    i=$((i + 1))
  done
  if [ "$ok" -ne 1 ]; then
    echo "WARN: D1 jog boot failed after ${ATTEMPTS} attempts"
    tail -40 d1_jog_run.log 2>/dev/null || true
    tail -20 d1_jog_supervise.log 2>/dev/null || true
  fi
  echo "=== fine boot $(date -Is) ==="
} >> "$LOG" 2>&1 &
exit 0
