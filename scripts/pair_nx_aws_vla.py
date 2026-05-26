#!/usr/bin/env python3
"""Collega la Jetson NX al worker VLA su AWS (URL + token + cloud mode).

Dal PC con SSH verso la NX (192.168.123.18):

  # Dopo bootstrap EC2 — copia il file pairing:
  scp ubuntu@<EC2_IP>:~/go2-vla-pairing.env .
  python scripts/pair_nx_aws_vla.py --pairing-file go2-vla-pairing.env

  # Oppure manuale:
  python scripts/pair_nx_aws_vla.py \\
    --worker-url http://<EC2_IP>:8765 \\
    --token <GO2_WORKER_TOKEN>

  # Verifica end-to-end dalla NX (via dashboard proxy):
  python scripts/pair_nx_aws_vla.py --pairing-file go2-vla-pairing.env --verify

Env NX SSH: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD (come deploy).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import paramiko

REMOTE_BASE = "/home/unitree/go2_visual_dashboard"
ENV_SH = f"{REMOTE_BASE}/scripts/nx_dashboard_env.sh"
SECRETS_SH = f"{REMOTE_BASE}/scripts/nx_secrets_dashboard.sh"


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


def _load_pairing(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _shell_escape(s: str) -> str:
    return s.replace("'", "'\"'\"'")


def _ssh_run(ssh: paramiko.SSHClient, cmd: str) -> tuple[int, str, str]:
    _, stdout, stderr = ssh.exec_command(cmd)
    code = stdout.channel.recv_exit_status()
    return code, stdout.read().decode("utf-8", errors="replace"), stderr.read().decode("utf-8", errors="replace")


def _patch_env_sh(ssh: paramiko.SSHClient, worker_url: str, cloud_mode: bool) -> None:
    esc_url = worker_url.replace("|", "\\|").replace("&", "\\&")
    cmds = [
        f"sed -i 's|^export GO2_ANYGRASP_WORKER_URL=.*|export GO2_ANYGRASP_WORKER_URL={esc_url}|' {ENV_SH}",
        f"grep -q '^export GO2_ANYGRASP_PROXY=' {ENV_SH} || echo 'export GO2_ANYGRASP_PROXY=1' >> {ENV_SH}",
    ]
    if cloud_mode:
        cmds.append(
            f"grep -q '^export GO2_GRASP_CLOUD_MODE=' {ENV_SH} && "
            f"sed -i 's|^export GO2_GRASP_CLOUD_MODE=.*|export GO2_GRASP_CLOUD_MODE=1|' {ENV_SH} || "
            f"echo 'export GO2_GRASP_CLOUD_MODE=1' >> {ENV_SH}"
        )
    for c in cmds:
        code, out, err = _ssh_run(ssh, c)
        if code != 0:
            raise RuntimeError(f"patch env failed: {err or out}")


def _write_secrets_token(ssh: paramiko.SSHClient, token: str) -> None:
    tok_esc = _shell_escape(token)
    script = f"""set -e
mkdir -p $(dirname {SECRETS_SH})
touch {SECRETS_SH}
chmod 600 {SECRETS_SH}
if grep -q '^export GO2_WORKER_TOKEN=' {SECRETS_SH}; then
  sed -i 's|^export GO2_WORKER_TOKEN=.*|export GO2_WORKER_TOKEN='"'"'{tok_esc}'"'"'|' {SECRETS_SH}
else
  echo 'export GO2_WORKER_TOKEN='"'"'{tok_esc}'"'"'' >> {SECRETS_SH}
fi
grep GO2_WORKER_TOKEN {SECRETS_SH} | sed 's/=.*/=***/'
"""
    code, out, err = _ssh_run(ssh, script)
    if code != 0:
        raise RuntimeError(f"secrets write failed: {err or out}")
    print(out.rstrip())


def _restart_dashboard(ssh: paramiko.SSHClient) -> int:
    code, out, err = _ssh_run(ssh, f"cd {REMOTE_BASE} && bash scripts/nx_start_dashboard.sh")
    print(out[-2000:].rstrip())
    if err.strip():
        print("stderr:", err[-600:], file=sys.stderr)
    return code


def _verify_nx_grasp(ssh: paramiko.SSHClient) -> None:
    cmd = (
        f"bash -lc 'source {ENV_SH}; "
        f"[[ -f {SECRETS_SH} ]] && source {SECRETS_SH}; "
        "curl -sf http://127.0.0.1:5052/api/grasp/health | python3 -m json.tool'"
    )
    code, out, err = _ssh_run(ssh, cmd)
    print("--- GET /api/grasp/health (via NX localhost) ---")
    print(out or err)
    if code != 0:
        raise RuntimeError("grasp health sulla NX fallito")


def _verify_worker_direct(worker_url: str, token: str) -> None:
    url = worker_url.rstrip("/") + "/health"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "X-Worker-Token": token})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    print("--- worker /health (direct) ---")
    print(json.dumps(body, indent=2, ensure_ascii=False)[:3000])


def main() -> int:
    ap = argparse.ArgumentParser(description="Pair Jetson NX with AWS VLA worker")
    ap.add_argument("--pairing-file", type=Path, help="go2-vla-pairing.env from EC2 bootstrap")
    ap.add_argument("--worker-url", default="", help="http://EC2_IP:8765")
    ap.add_argument("--token", default="", help="GO2_WORKER_TOKEN")
    ap.add_argument("--no-cloud-mode", action="store_true", help="Non forzare GO2_GRASP_CLOUD_MODE=1")
    ap.add_argument("--verify", action="store_true", help="Dopo pair, curl grasp/health sulla NX")
    ap.add_argument("--skip-restart", action="store_true")
    args = ap.parse_args()

    worker_url = (args.worker_url or "").strip().rstrip("/")
    token = (args.token or "").strip()

    if args.pairing_file:
        pf = args.pairing_file.expanduser().resolve()
        if not pf.is_file():
            print(f"File non trovato: {pf}", file=sys.stderr)
            return 2
        data = _load_pairing(pf)
        worker_url = worker_url or data.get("GO2_ANYGRASP_WORKER_URL", "").rstrip("/")
        token = token or data.get("GO2_WORKER_TOKEN", "")

    if not worker_url or "://" not in worker_url:
        print("Serve --worker-url o --pairing-file con GO2_ANYGRASP_WORKER_URL", file=sys.stderr)
        return 2
    if not token:
        print("Serve --token o pairing file con GO2_WORKER_TOKEN", file=sys.stderr)
        return 2

    cloud_mode = not args.no_cloud_mode

    print(f"[pair] worker={worker_url} cloud_mode={cloud_mode} nx={nx_user()}@{nx_host()}")

    try:
        _verify_worker_direct(worker_url, token)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"AVVISO: worker non raggiungibile dal PC ({exc}) — continuo pair NX comunque")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=45)

    _patch_env_sh(ssh, worker_url, cloud_mode)
    print(f"OK GO2_ANYGRASP_WORKER_URL={worker_url}")
    _write_secrets_token(ssh, token)

    if not args.skip_restart:
        rc = _restart_dashboard(ssh)
        if rc != 0:
            ssh.close()
            return rc

    if args.verify:
        _verify_nx_grasp(ssh)

    ssh.close()
    print("")
    print("PAIR_NX_AWS_VLA_OK")
    print(f"Dashboard: http://{nx_host()}:5052 → tab Presa → VLA AWS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
