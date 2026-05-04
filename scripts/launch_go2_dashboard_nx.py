#!/usr/bin/env python3
"""
Avvio / deploy dashboard sulla Jetson (NX) dal PC Windows, con log passo-passo.

Uso (dalla root repo):
  python scripts/launch_go2_dashboard_nx.py
  python scripts/launch_go2_dashboard_nx.py --skip-deploy
  python scripts/launch_go2_dashboard_nx.py --open-browser

Barra applicazioni: crea collegamento a ``launch_go2_dashboard_nx.bat`` in questa cartella.

Variabili (opzionali): GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD (stesso significato di deploy_dashboard_to_nx.py)
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_dashboard_to_nx.py"
LOG_DIR = REPO_ROOT / "logs"


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _emit(log_fp, msg: str, *, to_stdout: bool = True) -> None:
    line = f"[{_ts()}] {msg}"
    if to_stdout:
        print(line, flush=True)
    if log_fp:
        log_fp.write(line + "\n")
        log_fp.flush()


def _tcp_check(host: str, port: int = 22, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_health(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    try:
        import urllib.error
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", errors="replace")
            return True, f"HTTP {r.status} {body[:200]!r}"
    except Exception as exc:
        return False, repr(exc)


def _remote_snippet(host: str, user: str, password: str) -> str:
    try:
        import paramiko
    except ImportError:
        return "(paramiko non installato — salto riepilogo remoto)"
    out: list[str] = []
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username=user, password=password, timeout=20)
        base = "/home/unitree/go2_visual_dashboard"
        for cmd in (
            f"test -d {base} && ls -la {base} | head -n 15 || echo MISSING_DIR",
            f"test -f {base}/dashboard.pid && echo -n 'PID ' && cat {base}/dashboard.pid || echo NO_PID_FILE",
            f"test -f {base}/dashboard_run.log && tail -n 12 {base}/dashboard_run.log || echo NO_LOG",
        ):
            _, stdout, stderr = c.exec_command(cmd)
            out.append(f"$ {cmd}\n{stdout.read().decode(errors='replace')}")
            err = stderr.read().decode(errors="replace")
            if err.strip():
                out.append(f"stderr: {err}")
        c.close()
    except Exception as exc:
        out.append(f"SSH snippet error: {exc!r}")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Deploy + avvio dashboard su NX con log.")
    p.add_argument("--skip-deploy", action="store_true", help="Solo check TCP/HTTP e riepilogo remoto (niente push/restart).")
    p.add_argument("--open-browser", action="store_true", help="Apre il browser sulla dashboard se l'health risponde.")
    args = p.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"nx_dashboard_launch_{stamp}.log"
    latest = LOG_DIR / "nx_dashboard_launch_latest.log"

    import os

    host = (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"
    user = (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"
    pwd = os.environ.get("GO2_NX_PASSWORD") or "123"
    base_url = f"http://{host}:5050"

    with log_path.open("w", encoding="utf-8") as log_fp:
        _emit(log_fp, f"LOG_FILE = {log_path}")
        _emit(log_fp, f"Repository: {REPO_ROOT}")
        _emit(log_fp, f"Target NX: {user}@{host} (override con GO2_NX_HOST / GO2_NX_USER / GO2_NX_PASSWORD)")

        _emit(log_fp, "STEP 1 — Verifica porta SSH 22 …")
        if not _tcp_check(host, 22, timeout=4.0):
            _emit(log_fp, "ERRORE: nessuna connessione TCP verso la NX (controlla modem/LAN e IP).")
            try:
                latest.write_text(log_path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
            return 2
        _emit(log_fp, "OK: host raggiungibile sulla porta 22.")

        if not args.skip_deploy:
            _emit(log_fp, "STEP 2 — Deploy file + restart dashboard (deploy_dashboard_to_nx.py) …")
            if not DEPLOY_SCRIPT.is_file():
                _emit(log_fp, f"ERRORE: script mancante {DEPLOY_SCRIPT}")
                return 3
            proc = subprocess.Popen(
                [sys.executable, str(DEPLOY_SCRIPT)],
                cwd=str(REPO_ROOT),
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                _emit(log_fp, line.rstrip("\n"))
            proc.wait()
            if proc.returncode != 0:
                _emit(log_fp, f"ERRORE: deploy terminato con codice {proc.returncode}")
                try:
                    latest.write_text(log_path.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    pass
                return int(proc.returncode) if proc.returncode else 1
            _emit(log_fp, "OK: deploy script terminato con codice 0.")
        else:
            _emit(log_fp, "STEP 2 — Saltato (--skip-deploy).")

        _emit(log_fp, f"STEP 3 — Health HTTP {base_url}/api/health …")
        ok, detail = _http_health(f"{base_url}/api/health", timeout=10.0)
        if ok:
            _emit(log_fp, f"OK: {detail}")
        else:
            _emit(log_fp, f"ATTENZIONE (HTTP da questo PC): {detail}")
            _emit(log_fp, "Suggerimento: firewall sul PC o routing; sulla NX il servizio può essere comunque attivo.")

        _emit(log_fp, "STEP 4 — Riepilogo file su NX (ls, pid, tail log) …")
        snip = _remote_snippet(host, user, pwd)
        for line in snip.splitlines():
            _emit(log_fp, line)

        _emit(log_fp, f"Fine. Dashboard (LAN): {base_url}")
        try:
            latest.write_text(log_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
        _emit(log_fp, f"Copia rapida ultimo log: {latest}")

        if args.open_browser and ok:
            webbrowser.open(base_url)

    print(f"\nLog completo: {log_path}", flush=True)
    print(f"Ultimo log copiato in: {latest}", flush=True)
    # Deploy riuscito = successo operativo; HTTP da PC può fallire per routing ma la NX è ok.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
