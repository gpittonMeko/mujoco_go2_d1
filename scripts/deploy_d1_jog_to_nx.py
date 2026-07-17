#!/usr/bin/env python3
"""Deploy dashboard D1 integrata sulla Jetson, porta 5056.

SICUREZZA BRACCIO: di default copia SOLO i file (nessun restart).
Un restart Flask/hold puo' far cedere il braccio: va fatto solo a freddo,
con braccio sostenuto, e flag espliciti.

Uso files-only (sicuro):
  python scripts/deploy_d1_jog_to_nx.py

Uso con restart (PERICOLOSO — sostieni il braccio):
  set GO2_D1_JOG_RESTART=1
  set GO2_D1_CONFIRM_ARM_SUPPORTED=1
  python scripts/deploy_d1_jog_to_nx.py

Env: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD.

URL: http://192.168.123.18:5056/
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import paramiko

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_BASE = "/home/unitree/go2_visual_dashboard"
SDK_TREE = REPO_ROOT / "D1 550 Workspace" / "d1_sdk" / "d1_sdk"

PUSH_FILES = [
    "go2_dashboard/__init__.py",
    "go2_dashboard/paths.py",
    "go2_dashboard/d1_hold_client.py",
    "go2_dashboard/motor_health_app.py",
    "go2_dashboard/go2_motor_event_log.py",
    "go2_dashboard/go2_motor_health.py",
    "go2_dashboard/go2_motor_sport.py",
    "go2_dashboard/go2_thermal_protect.py",
    "go2_dashboard/go2_thermal_runtime.py",
    "go2_dashboard/go2_thermal_settings.py",
    "go2_dashboard/d1_jog/__init__.py",
    "go2_dashboard/d1_jog/app.py",
    "go2_dashboard/d1_jog/service.py",
    "go2_dashboard/d1_jog/motion_guard.py",
    "go2_dashboard/d1_jog/motion_profile.py",
    "go2_dashboard/d1_jog/program_store.py",
    "go2_dashboard/d1_jog/program_runner.py",
    "go2_dashboard/d1_jog/tcp_motion.py",
    "go2_dashboard/d1_jog/jog_stream.py",
    "go2_dashboard/d1_jog/cartesian.py",
    "go2_dashboard/d1_jog/grasp6d.py",
    "go2_dashboard/d1_jog/wrist_rgbd.py",
    "go2_dashboard/d1_jog/orbbec_capture.py",
    "go2_dashboard/d1_jog/pick_preset.py",
    "go2_dashboard/d1_jog/pick_teach_model.py",
    "go2_dashboard/d1_jog/pick_vision.py",
    "go2_dashboard/d1_jog/pick_vision_crop.py",
    "go2_dashboard/hermes/__init__.py",
    "go2_dashboard/hermes/app.py",
    "go2_dashboard/hermes/routes.py",
    "go2_dashboard/hermes/agent.py",
    "go2_dashboard/hermes/local_agent.py",
    "go2_dashboard/hermes/context.py",
    "go2_dashboard/hermes/speech.py",
    "go2_dashboard/hermes/sdk_bridge.py",
    "go2_dashboard/hermes/vision.py",
    "go2_dashboard/hermes/actions.py",
    "go2_dashboard/hermes/phrases.py",
    "go2_dashboard/hermes/tts_local.py",
    "go2_dashboard/hermes/interaction.py",
    "go2_dashboard/hermes/locomotion.py",
    "scripts/box_object_detector.py",
    "D1 550 Workspace/OLD/scripts/arm_kinematics_d1_template.py",
    "D1 550 Workspace/OLD/scripts/d1_arm_servo_read_python.py",
    "scripts/serve_d1_jog_dashboard.py",
    "scripts/d1_hold_daemon.py",
    "scripts/nx_d1_hold_supervise.sh",
    "scripts/nx_start_d1_hold_daemon.sh",
    "scripts/verify_d1_arm_stack.py",
    "scripts/build_d1_sdk.sh",
    "scripts/go2-d1-jog-dashboard.service",
    "scripts/nx_boot_d1_jog_wrapper.sh",
    "scripts/nx_d1_jog_env.sh",
    "scripts/nx_serve_foreground_d1_jog.sh",
    "scripts/nx_d1_jog_supervise.sh",
    "scripts/nx_start_d1_jog.sh",
    "scripts/nx_stop_operator_dashboard.sh",
    "scripts/orbbec_reset_camera.sh",
    "scripts/udev/99-go2-realsense-dashboard.rules",
    "templates/d1_jog_dashboard.html",
    "templates/hermes.html",
    "templates/d1_joint_jog_modal.html",
    "templates/d1_program_editor.html",
    "templates/d1_tcp_jog_modal.html",
    "static/d1_common.js",
    "static/d1_common.css",
    "static/hermes/hermes.css",
    "static/hermes/hermes.js",
    "data/unitree_robot_main.png",
    "output/pdf/d1_handeye_aruco_4x4_50_id0_60mm.pdf",
]


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


def _sdk_files() -> list[Path]:
    if not SDK_TREE.is_dir():
        return []
    out: list[Path] = []
    for p in SDK_TREE.rglob("*"):
        if p.is_file():
            out.append(p)
    return out


def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    if not remote_dir or remote_dir == "/":
        return
    parts = [p for p in remote_dir.split("/") if p]
    cur = "/" if remote_dir.startswith("/") else ""
    for part in parts:
        cur = f"{cur}/{part}" if cur else part
        try:
            sftp.stat(cur)
        except OSError:
            try:
                sftp.mkdir(cur)
            except OSError:
                pass


def _put_file(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    parent = remote.rsplit("/", 1)[0]
    if parent:
        _ensure_remote_dir(sftp, parent)
    data = local.read_bytes()
    if remote.endswith(".sh") or local.suffix == ".sh":
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    from io import BytesIO

    sftp.putfo(BytesIO(data), remote)


def _remote_build_d1_sdk(ssh: paramiko.SSHClient) -> int:
    print("[d1-jog deploy] Compilazione bin/d1_sdk_* …")
    log = "/tmp/go2_d1_sdk_build.log"
    script = f"""set +e
cd "{REMOTE_BASE}"
mkdir -p bin
set -a
if [ -f scripts/nx_d1_jog_env.sh ]; then
  . scripts/nx_d1_jog_env.sh
elif [ -f scripts/nx_dashboard_env.sh ]; then
  . scripts/nx_dashboard_env.sh
fi
set +a
bash scripts/build_d1_sdk.sh >{log} 2>&1
EC=$?
echo EXIT_CODE=$EC >>{log}
cat {log}
ls -la bin/d1_sdk_command bin/d1_sdk_feedback bin/d1_sdk_get_angles 2>/dev/null || true
exit $EC
"""
    _, stdout, stderr = ssh.exec_command(script, timeout=300)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.strip()[-6000:])
    if err.strip():
        print("build stderr:", err.strip()[-800:])
    if code == 0:
        print("[d1-jog deploy] build_d1_sdk OK")
    else:
        print(f"[d1-jog deploy] ERRORE build_d1_sdk exit={code}")
    return code


def _remote_reload_hold_daemon(ssh: paramiko.SSHClient) -> None:
    """Riavvia SOLO il hold daemon con restore stato (writer-thread fix).

    Sequenza: freeze pose -> stop daemon -> start -> assert hold_active.
    """
    print("[d1-jog deploy] Reload HOLD daemon (writer-thread fix) …")
    script = f"""set -e
cd {REMOTE_BASE}
# Freeze sulla posa corrente prima del reload.
python3 - <<'PY'
import json, time, urllib.request
from go2_dashboard.d1_hold_client import publish, status
try:
    req=urllib.request.Request(
        'http://127.0.0.1:5056/api/joints/hold_now',
        data=b'{{"reason":"pre_hold_daemon_reload"}}',
        method='POST',
        headers={{'Content-Type':'application/json'}},
    )
    json.load(urllib.request.urlopen(req, timeout=20))
except Exception as e:
    print('HOLD_API_SKIP', e)
h=status()
pose=h.get('hold_target_servo_deg')
if isinstance(pose, list) and len(pose)>=7:
    s=int(time.time()*1000)%100000
    data={{'mode':1}}
    for i,v in enumerate(pose[:7]): data[f'angle{{i}}']=float(v)
    publish([
      {{'seq':s,'address':1,'funcode':6,'data':{{'power':1}}}},
      {{'seq':s+1,'address':1,'funcode':5,'data':{{'mode':1}}}},
      {{'seq':s+2,'address':1,'funcode':2,'data':data}},
    ], delay_ms=5)
print('PRE_RELOAD_HOLD', status().get('hold_active'), status().get('heartbeat_age_ms'))
PY
# Stop solo il daemon (state.json resta per restore same-boot).
pkill -f '[n]x_d1_hold_supervise.sh' 2>/dev/null || true
pkill -f '[d]1_hold_daemon.py' 2>/dev/null || true
sleep 0.35
rm -f /tmp/go2_d1_hold.sock /tmp/go2_d1_hold.lock
bash scripts/nx_start_d1_hold_daemon.sh
python3 - <<'PY'
import time
from go2_dashboard.d1_hold_client import status
deadline=time.time()+4.0
last={{}}
while time.time()<deadline:
    last=status()
    if last.get('ok') and last.get('hold_active') and last.get('publisher_alive'):
        print('HOLD_DAEMON_RELOAD_OK', last.get('publisher_pid'), last.get('heartbeat_period_ms'), last.get('writer_queue_depth'))
        raise SystemExit(0)
    time.sleep(0.05)
print('HOLD_DAEMON_RELOAD_FAIL', last)
raise SystemExit(2)
PY
"""
    _, stdout, stderr = ssh.exec_command(script, timeout=60)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.strip())
    if err.strip():
        print("hold reload stderr:", err.strip())
    if code != 0:
        raise SystemExit("REFUSE_DEPLOY_HOLD_DAEMON_RELOAD_FAILED")


def _remote_start_jog(ssh: paramiko.SSHClient, host: str) -> None:
    """Riavvio Flask hold-safe. Richiede conferma esplicita: rischia cedimento braccio."""
    print("[d1-jog deploy] HOLD-SAFE restart 5056 (no uvc reload, freeze pose prima del pkill) …")
    script = f"""set -e
cd {REMOTE_BASE}
# Freeze posa misurata sul keeper PRIMA di toccare Flask.
python3 - <<'PY'
import json, urllib.request
from go2_dashboard.d1_hold_client import publish, status
import time
d={{}}
try:
    req=urllib.request.Request(
        'http://127.0.0.1:5056/api/joints/hold_now',
        data=b'{{"reason":"deploy_pre_restart"}}',
        method='POST',
        headers={{'Content-Type':'application/json'}},
    )
    d=json.load(urllib.request.urlopen(req, timeout=20))
except Exception:
    d={{}}
h=status()
pose=None
if isinstance((d or {{}}).get('feedback'), dict):
    pose=(d.get('feedback') or {{}}).get('servo_deg')
if not (isinstance(pose, list) and len(pose)>=7):
    try:
        fb=json.load(urllib.request.urlopen('http://127.0.0.1:5056/api/joints/feedback', timeout=10))
        pose=fb.get('servo_deg')
    except Exception:
        pose=h.get('hold_target_servo_deg')
if isinstance(pose, list) and len(pose)>=7:
    s=int(time.time()*1000)%100000
    data={{'mode':1}}
    for i,v in enumerate(pose[:7]): data[f'angle{{i}}']=float(v)
    publish([
      {{'seq':s,'address':1,'funcode':6,'data':{{'power':1}}}},
      {{'seq':s+1,'address':1,'funcode':5,'data':{{'mode':1}}}},
      {{'seq':s+2,'address':1,'funcode':2,'data':data}},
    ], delay_ms=10)
print('PRE_RESTART_HOLD', status().get('hold_active'), status().get('heartbeat_age_ms'))
PY
export D1_ORBBEC_RELOAD_UVC=0
bash scripts/nx_start_d1_jog.sh
"""
    _, stdout, stderr = ssh.exec_command(script, timeout=90)
    code = stdout.channel.recv_exit_status()
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("start stderr:", err.strip())
    if code != 0:
        raise SystemExit(code)
    port = os.environ.get("D1_JOG_PORT", "5056")
    print(f"[d1-jog deploy] OK — apri http://{host}:{port}/")


def _remote_prepare_external_hold(ssh: paramiko.SSHClient) -> None:
    """Assicura il keeper esterno. MAI uccidere un hold già attivo.

    Bug storico: se Flask/health non rispondeva, il deploy faceva pkill del
    daemon hold e il braccio cadeva. Ora: se il socket hold e' vivo, non toccare.
    """
    script = f"""set -e
cd {REMOTE_BASE}
# 1) Verifica DIRETTA del keeper (non dipendere da Flask).
HOLD_OK=$(python3 - <<'PY'
from go2_dashboard.d1_hold_client import status
try:
    h=status()
    print('1' if h.get('ok') and h.get('hold_active') and h.get('publisher_alive') else '0')
except Exception:
    print('0')
PY
)
if [ "$HOLD_OK" = "1" ]; then
  echo EXTERNAL_HOLD_ALREADY_ACTIVE
  exit 0
fi
# 2) Flask health: se dice external+hold, non toccare comunque.
LEGACY=$(python3 - <<'PY'
import json, urllib.request
try:
    d=json.load(urllib.request.urlopen('http://127.0.0.1:5056/api/health', timeout=5))
    h=d.get('command_daemon') or {{}}
    print('0' if h.get('external') and h.get('hold_active') else '1')
except Exception:
    print('1')
PY
)
if [ "$LEGACY" = "0" ]; then
  echo EXTERNAL_HOLD_ALREADY_ACTIVE
  exit 0
fi
# 3) Solo se NON c'e' hold attivo: avvia keeper. Mai pkill di un daemon vivo.
if pgrep -f '[d]1_hold_daemon.py' >/dev/null 2>&1; then
  echo EXTERNAL_HOLD_PROCESS_ALIVE_REFUSE_KILL
  echo "REFUSE: hold daemon process exists but status not ok — non uccido il processo (rischio caduta)." >&2
  exit 2
fi
POSE=$(python3 - <<'PY'
import json, urllib.request
d=json.load(urllib.request.urlopen('http://127.0.0.1:5056/api/joints/feedback', timeout=10))
p=d.get('servo_deg')
assert isinstance(p,list) and len(p)>=7, d
print(','.join(str(float(x)) for x in p[:7]))
PY
)
bash scripts/nx_start_d1_hold_daemon.sh
POSE="$POSE" python3 - <<'PY'
import os, time
from go2_dashboard.d1_hold_client import publish, status
p=[float(x) for x in os.environ['POSE'].split(',')]
s=int(time.time()*1000)%100000
data={{'mode':1}}
for i,v in enumerate(p): data[f'angle{{i}}']=v
msgs=[
 {{'seq':s,'address':1,'funcode':6,'data':{{'power':1}}}},
 {{'seq':s+1,'address':1,'funcode':5,'data':{{'mode':1}}}},
 {{'seq':s+2,'address':1,'funcode':2,'data':data}},
]
r=publish(msgs, delay_ms=20)
assert r.get('ok'), r
time.sleep(.35)
h=status()
assert h.get('publisher_alive') and h.get('hold_active'), h
print('EXTERNAL_HOLD_HANDOFF_OK', h.get('publisher_pid'), h.get('heartbeat_count'))
PY
"""
    _, stdout, stderr = ssh.exec_command(script, timeout=40)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.strip())
    if err.strip():
        print("hold handoff stderr:", err.strip())
    if code != 0:
        raise SystemExit("REFUSE_DEPLOY_EXTERNAL_HOLD_HANDOFF_FAILED")


def _remote_install_d1_crontab(ssh: paramiko.SSHClient) -> None:
    marker = "GO2_D1_JOG_AUTOSTART"
    reboot_line = f"@reboot /bin/bash {REMOTE_BASE}/scripts/nx_boot_d1_jog_wrapper.sh"
    script = f"""set +e
TMP=$(mktemp)
( crontab -l 2>/dev/null \
  | grep -v '{marker}' \
  | grep -v nx_boot_d1_jog_wrapper.sh \
  | grep -v 'GO2_DASHBOARD_AUTOSTART' \
  | grep -v nx_boot_dashboard_wrapper.sh \
  || true
  echo '# {marker}'
  echo '{reboot_line}'
) > "$TMP"
crontab "$TMP"
EC=$?
rm -f "$TMP"
echo "crontab: installed {marker} exit=$EC"
crontab -l 2>/dev/null | tail -n 8
"""
    _, stdout, stderr = ssh.exec_command(script)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.strip())
    if err.strip():
        print("crontab stderr:", err.strip())


def _remote_remove_legacy_5052_autostart(ssh: paramiko.SSHClient) -> None:
    script = f"""set +e
TMP=$(mktemp)
( crontab -l 2>/dev/null \
  | grep -v 'GO2_DASHBOARD_AUTOSTART' \
  | grep -v nx_boot_dashboard_wrapper.sh \
  || true
) > "$TMP"
crontab "$TMP"
EC=$?
rm -f "$TMP"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if [ -S "$XDG_RUNTIME_DIR/bus" ]; then
  systemctl --user disable --now go2-visual-dashboard >/dev/null 2>&1 || true
  systemctl --user reset-failed go2-visual-dashboard >/dev/null 2>&1 || true
fi
rm -f "$HOME/.config/systemd/user/go2-visual-dashboard.service"
echo "legacy_5052_cleanup exit=$EC"
crontab -l 2>/dev/null | tail -n 8
"""
    _, stdout, stderr = ssh.exec_command(script)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if out:
        print(out)
    if err:
        print("legacy cleanup stderr:", err)


def _remote_install_d1_systemd_user_optional(ssh: paramiko.SSHClient) -> None:
    script = f"""set +e
mkdir -p "$HOME/.config/systemd/user"
cp -f "{REMOTE_BASE}/scripts/go2-d1-jog-dashboard.service" "$HOME/.config/systemd/user/go2-d1-jog-dashboard.service"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if [ -S "$XDG_RUNTIME_DIR/bus" ]; then
  systemctl --user daemon-reload 2>/dev/null
  echo "SYSTEMD_USER: unit D1 in ~/.config/systemd/user (opzionale: systemctl --user enable --now go2-d1-jog-dashboard; prima disabilita cron se doppio avvio)"
else
  echo "SYSTEMD_USER: unit D1 copiata; niente dbus session â€” usa cron @reboot o loginctl enable-linger"
fi
"""
    _, stdout, stderr = ssh.exec_command(script)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if out:
        print(out)
    if err:
        print("systemd user stderr:", err)


def _remote_install_realsense_udev(ssh: paramiko.SSHClient) -> None:
    src = f"{REMOTE_BASE}/scripts/udev/99-go2-realsense-dashboard.rules"
    dst = "/etc/udev/rules.d/99-go2-realsense-dashboard.rules"
    password = nx_password().replace("'", "'\\''")
    command = (
        f"printf '%s\\n' '{password}' | sudo -S cp -f '{src}' '{dst}' && "
        "printf '%s\\n' '" + password + "' | sudo -S udevadm control --reload-rules && "
        "printf '%s\\n' '" + password + "' | sudo -S udevadm trigger --subsystem-match=video4linux"
    )
    _, stdout, stderr = ssh.exec_command(command, timeout=30)
    code = stdout.channel.recv_exit_status()
    if code != 0:
        print("[d1-jog deploy] WARN udev RealSense non installata:", stderr.read().decode(errors="replace")[-500:])
    else:
        print("[d1-jog deploy] udev RealSense D435i/D456 installata")


def main() -> None:
    import subprocess
    import sys

    gate = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "test_d1_rgb_and_motor_ui.py")],
        cwd=str(REPO_ROOT),
        check=False,
    )
    if gate.returncode != 0:
        raise SystemExit("REFUSE_DEPLOY_RGB_MOTOR_UI_TEST_FAILED")
    host = nx_host()
    print(f"[d1-jog deploy] NX {nx_user()}@{host} (non tocca dashboard 5052)")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=nx_user(), password=nx_password(), timeout=25)

    sftp = ssh.open_sftp()
    try:
        sftp.stat(REMOTE_BASE)
    except OSError:
        print(f"ERROR: {REMOTE_BASE} non esiste — esegui prima deploy_dashboard_to_nx.py")
        sftp.close()
        ssh.close()
        raise SystemExit(2)

    for rel in PUSH_FILES:
        loc = REPO_ROOT / rel
        if not loc.is_file():
            print("skip missing", rel)
            continue
        remote = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        _put_file(sftp, loc, remote)
        print("pushed", rel)

    remote_sdk_root = f"{REMOTE_BASE}/D1 550 Workspace/d1_sdk/d1_sdk"
    for loc in _sdk_files():
        rel = loc.relative_to(SDK_TREE)
        remote = f"{remote_sdk_root}/{rel.as_posix()}"
        _put_file(sftp, loc, remote)
    print(f"pushed d1_sdk tree ({len(_sdk_files())} files)")

    for sh in (
        "scripts/nx_boot_d1_jog_wrapper.sh",
        "scripts/nx_d1_jog_env.sh",
        "scripts/nx_serve_foreground_d1_jog.sh",
        "scripts/nx_d1_jog_supervise.sh",
        "scripts/nx_start_d1_jog.sh",
        "scripts/nx_d1_hold_supervise.sh",
        "scripts/nx_start_d1_hold_daemon.sh",
        "scripts/nx_stop_operator_dashboard.sh",
        "scripts/orbbec_reset_camera.sh",
        "scripts/build_d1_sdk.sh",
    ):
        try:
            sftp.chmod(f"{REMOTE_BASE}/{sh}", 0o755)
        except OSError:
            pass

    strip_cmd = (
        f"bash -lc \"sed -i 's/\\\\r$//' {REMOTE_BASE}/scripts/nx_d1_jog*.sh "
        f"{REMOTE_BASE}/scripts/nx_serve_foreground_d1_jog.sh "
        f"{REMOTE_BASE}/scripts/nx_boot_d1_jog_wrapper.sh "
        f"{REMOTE_BASE}/scripts/nx_start_d1_jog.sh {REMOTE_BASE}/scripts/nx_stop_operator_dashboard.sh "
        f"{REMOTE_BASE}/scripts/build_d1_sdk.sh 2>/dev/null || true\""
    )
    _, so, _ = ssh.exec_command(strip_cmd)
    so.channel.recv_exit_status()
    sftp.close()

    skip_sdk = os.environ.get("D1_JOG_SKIP_SDK_BUILD", "0").strip().lower() in {"1", "true", "yes", "on"}
    # Default: SOLO copia file. Il restart Flask/hold fa cadere il braccio in lab.
    restart = os.environ.get("GO2_D1_JOG_RESTART", "0").strip().lower() in {"1", "true", "yes", "on"}
    confirm_arm = os.environ.get("GO2_D1_CONFIRM_ARM_SUPPORTED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    build_code = 0 if skip_sdk else _remote_build_d1_sdk(ssh)
    if skip_sdk:
        print("[d1-jog deploy] SKIP SDK build — usa binari esistenti")
    if build_code != 0:
        print("[d1-jog deploy] WARN build SDK fallito — file comunque copiati")

    if not restart:
        print("[d1-jog deploy] FILES-ONLY: nessun restart Flask/hold (sicurezza braccio).")
        print("[d1-jog deploy] Per riavviare: braccio SOSTENUTO +")
        print("  set GO2_D1_JOG_RESTART=1")
        print("  set GO2_D1_CONFIRM_ARM_SUPPORTED=1")
        print("  python scripts/deploy_d1_jog_to_nx.py")
        ssh.close()
        return

    if not confirm_arm:
        ssh.close()
        raise SystemExit(
            "REFUSE_RESTART_WITHOUT_ARM_SUPPORT_CONFIRM: "
            "imposta GO2_D1_CONFIRM_ARM_SUPPORTED=1 solo se stai sostenendo il braccio."
        )

    print("[d1-jog deploy] ATTENZIONE: restart richiesto — sostieni il braccio.")
    print("[d1-jog deploy] Install cron @reboot per 5056 …")
    print("[d1-jog deploy] Remove legacy 5052 autostart ...")
    _remote_prepare_external_hold(ssh)
    reload_hold = os.environ.get("GO2_D1_RELOAD_HOLD_DAEMON", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if reload_hold:
        _remote_reload_hold_daemon(ssh)
    _remote_remove_legacy_5052_autostart(ssh)
    _remote_install_d1_crontab(ssh)
    print("[d1-jog deploy] Optional systemd --user unit per 5056 …")
    _remote_install_d1_systemd_user_optional(ssh)
    _remote_install_realsense_udev(ssh)
    _remote_start_jog(ssh, host)
    ssh.close()
    print("[d1-jog deploy] Verifica stabilità daemon/feedback/hold …")
    verify = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "verify_d1_arm_stack.py"),
            "--base",
            f"http://{host}:{os.environ.get('D1_JOG_PORT', '5056')}",
        ],
        cwd=str(REPO_ROOT),
        check=False,
    )
    if verify.returncode != 0:
        raise SystemExit("REFUSE_DEPLOY_ARM_STACK_STABILITY_FAILED")


if __name__ == "__main__":
    main()
