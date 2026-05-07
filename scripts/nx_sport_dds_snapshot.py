#!/usr/bin/env python3
"""
Da un PC con rete+SSH verso la Jetson: snapshot Sport/DDS **visto dalla NX**
(localhost HTTP + env GO2_* + righe recenti log).

Non muove il cane da solo: legge solo API di diagnostica e log.
Credenziali/host come ``deploy_dashboard_to_nx.py`` (GO2_NX_*).

Nota rete «PC del proprietario»:
- DDS Sport Jetson↔cane resta sulla LAN Unitree (es. 192.168.123.x). Il PC di casa su 192.168.1.x
  non partecipa al DDS; può solo HTTP/SSH verso la NX se c’è instradamento o VPN.
- Per diagnosticare da remoto: questo script (SSH) o ``python scripts/probe_nx_general.py``.
"""
from __future__ import annotations

import sys

import paramiko

from deploy_dashboard_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user

REMOTE_SCRIPT = f"""set +e
cd {REMOTE_BASE} || {{ echo "MISSING {REMOTE_BASE}"; exit 1; }}
# shellcheck disable=SC1091
if test -f scripts/nx_dashboard_env.sh; then . scripts/nx_dashboard_env.sh; fi
echo "=== GO2 env (DDS / Sport / motion) ==="
env 2>/dev/null | grep -E '^GO2_' | sort | grep -E 'GO2_LOCAL|GO2_ENABLE_BASE|GO2_DDS|GO2_SPORT|GO2_ALLOW_GET' || true
echo ""
echo "=== Cyclone / unitree (nx_print_cyclone_diag) ==="
bash scripts/nx_print_cyclone_diag.sh 2>&1 || echo "(nx_print_cyclone_diag failed)"
echo ""
echo "=== GET /api/base/sport_env (localhost) ==="
curl -sS --max-time 12 "http://127.0.0.1:5050/api/base/sport_env" 2>&1 || echo "(curl sport_env failed)"
echo ""
echo "=== GET /api/base/sport_connectivity (MotionSwitcher ping, no movimento) ==="
curl -sS --max-time 14 "http://127.0.0.1:5050/api/base/sport_connectivity" 2>&1 || echo "(curl sport_connectivity failed)"
echo ""
echo "=== GET /api/base/sport_last ==="
curl -sS --max-time 10 "http://127.0.0.1:5050/api/base/sport_last" 2>&1 || echo "(curl sport_last failed)"
echo ""
echo "=== GET /api/health ==="
curl -sS --max-time 8 "http://127.0.0.1:5050/api/health" 2>&1 || echo "(curl health failed)"
echo ""
echo "=== tail dashboard_run.log (Sport|DDS|Traceback|Error) max 50 righe filtrate ==="
if test -f dashboard_run.log; then
  grep -iE 'sport|standdown|stand_up|dds|cyclone|traceback|error|accompany|segfault|subprocess' dashboard_run.log 2>/dev/null | tail -n 50
  echo "--- dashboard_run.log ultime 35 righe (grezzo) ---"
  tail -n 35 dashboard_run.log
else
  echo "(no dashboard_run.log)"
fi
echo ""
echo "=== tail dashboard_supervise.log (riavvii Flask) ==="
if test -f dashboard_supervise.log; then tail -n 25 dashboard_supervise.log; else echo "(no dashboard_supervise.log)"; fi
echo ""
echo "=== tail dashboard_boot.log (@reboot) ==="
if test -f dashboard_boot.log; then tail -n 20 dashboard_boot.log; else echo "(no dashboard_boot.log)"; fi
echo ""
echo "=== dmesg tail (Jetson: OOM/thermal/usb — non è il LED del cane ma utile) ==="
dmesg 2>/dev/null | tail -n 22 || echo "(dmesg non disponibile)"
echo ""
echo "=== Nota LED rossi Go2 (corpo): la dashboard non li logga; vedi app Unitree / manuale ==="
echo "Spesso: lampeggio rosso = anomalia/calibrazione; surriscaldamento motori/BMS = limitazione coppia o stop."
echo "Se il cane va a terra + rosso dopo uso intenso: raffreddare, batteria/carica, app per codici errore."
echo ""
echo "=== fine snapshot ==="
"""


def main() -> int:
    host = nx_host()
    print(f"[nx_sport_dds_snapshot] SSH {nx_user()}@{host} …\n")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=nx_user(), password=nx_password(), timeout=45)
    except Exception as exc:
        print("SSH fallita (rete, host, password GO2_NX_PASSWORD?):", exc)
        return 2
    stdin, stdout, stderr = ssh.exec_command("bash -s")
    stdin.write(REMOTE_SCRIPT)
    stdin.flush()
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    ssh.close()
    print(out)
    if err.strip():
        print("--- stderr ---", file=sys.stderr)
        print(err, file=sys.stderr)
    return int(code != 0)


if __name__ == "__main__":
    raise SystemExit(main())
