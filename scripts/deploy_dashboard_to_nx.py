#!/usr/bin/env python3
"""Push file dashboard verso Jetson e riavvio Flask. PC: ``python scripts/deploy_dashboard_to_nx.py``.

Imposta anche avvio automatico **@reboot** (cron) tramite ``nx_boot_dashboard_wrapper.sh``: parte in
background dopo ~45 s, errori solo su ``dashboard_boot.log``, non blocca il boot. Opzionale: unit
systemd user ``go2-visual-dashboard.service`` copiata in ``~/.config/systemd/user`` (non abilitata
dal deploy per evitare doppio avvio con cron).

**Preset ``data/vis_geometry_presets.json``:** se il file esiste già sulla NX, non viene sovrascritto
(così i preset salvati dalla UI restano). Prima installazione: viene copiato dal repo. Forzare:
``GO2_DEPLOY_OVERWRITE_PRESETS=1`` sul PC che lancia lo script.

Env: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD. Probe rapido da PC: ``python scripts/probe_nx_general.py``.
"""
import os
import paramiko
from pathlib import Path


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


REMOTE_BASE = "/home/unitree/go2_visual_dashboard"
REPO_ROOT = Path(__file__).resolve().parent.parent

REMOTE_PUSH_FILES = [
    "diagnostics_dashboard.py",
    "msg/ArmString_.hpp",
    "msg/PubServoInfo_.hpp",
    "msg/ArmString_.cpp",
    "msg/PubServoInfo_.cpp",
    "scripts/build_d1_arm_helpers.sh",
    "scripts/d1_arm_dds_helper.cpp",
    "scripts/d1_arm_feedback_helper.cpp",
    "scripts/d1_arm_servo_read_python.py",
    "go2_dashboard/__init__.py",
    "go2_dashboard/app.py",
    "go2_dashboard/legacy_mount.py",
    "go2_dashboard/cameras.py",
    "go2_dashboard/blueprints/__init__.py",
    "go2_dashboard/blueprints/meta.py",
    "scripts/box_grasp_planner.py",
    "scripts/box_object_detector.py",
    "scripts/arm_kinematics_d1_template.py",
    "scripts/go2_accompany.py",
    "scripts/dds_motion_ping_once.py",
    "scripts/sport_accompany_once.py",
    "scripts/nx_print_cyclone_diag.sh",
    "scripts/nx_go2_sta_and_dds_troubleshoot.txt",
    "scripts/pc_go2_webrtc_crouch.py",
    "scripts/d1_drag_follow_experimental.py",
    "scripts/sync_d1_meshes_from_package.py",
    "scripts/fetch_d1_550_from_jeewantha_github.py",
    "scripts/serve_dashboard_modular.py",
    "scripts/nx_serve_foreground.sh",
    "scripts/nx_dashboard_supervise.sh",
    "scripts/nx_machine_diag.sh",
    "scripts/nx_peripheral_probe.sh",
    "scripts/probe_nx_dds_servo.py",
    "scripts/udev/99-go2-realsense-dashboard.rules",
    "scripts/go2-visual-dashboard.service",
    "templates/dashboard.html",
    "templates/_always_cam_strip.html",
    "templates/_calibration_panel.html",
]
# ``data/vis_geometry_presets.json`` è gestito a parte: non sovrascrivere i preset salvati in laboratorio
# sulla NX (vedi blocco ``main()``). Primo deploy: copia dal repo se il file remoto non esiste.

# Variabili condivise tra avvio manuale, boot @reboot e (opzionale) systemd --user.
NX_EXPORTS = r"""
export GO2_LOCAL=1
export GO2_ENABLE_REAL_ARM=1
export GO2_ENABLE_BASE_MOTION=1
# Sport/Crouch/Stand: DDS Cyclone verso il cane — NON è l'IP HTTP della dashboard.
# Interfaccia Ethernet Jetson→subnet Unitree (di solito 192.168.123.x). Se usi WiFi verso l'AP del cane: wlan0.
export GO2_DDS_DOMAIN=0
export GO2_DDS_INTERFACE=eth0
# Cyclone + iceoryx: i binari C++ ``d1_arm_*`` (Unitree SDK) risolvono ``free_iox_chunk`` da
# ``/usr/local/lib`` (iceoryx accoppiato al libddsc di sistema). Mettere *solo*
# ``$HOME/cyclonedds/install/lib`` in testa rompeva il lookup (symbol undefined su ``d1_arm_command``).
export LD_LIBRARY_PATH="/usr/local/lib:/usr/local/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
_UT="${UNITREE_SDK2:-/usr/local}"
if [ -d "$_UT/lib" ]; then
  export LD_LIBRARY_PATH="$_UT/lib:${LD_LIBRARY_PATH}"
elif [ -d "$_UT/lib64" ]; then
  export LD_LIBRARY_PATH="$_UT/lib64:${LD_LIBRARY_PATH}"
fi
if [ -d "$HOME/cyclonedds/install/lib" ]; then
  export CYCLONEDDS_HOME="$HOME/cyclonedds/install"
  export CMAKE_PREFIX_PATH="$HOME/cyclonedds/install${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
  export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+${LD_LIBRARY_PATH}:}$HOME/cyclonedds/install/lib"
fi
# --- CycloneDDS / unitree_sdk2py (Jetson: segfault noti con wheel miste) ---
# Guida: scripts/nx_go2_sta_and_dds_troubleshoot.txt | Diagnosi: bash scripts/nx_print_cyclone_diag.sh
# Un solo albero SDK (directory che contiene unitree_sdk2py/):
# export UNITREE_SDK2_PYTHON=/home/unitree/unitree_sdk2_python
# Se la riga ``if`` sopra non basta, reinstalla binding contro la stessa build:
#   cd ~/unitree_sdk2_python && pip3 install --user -e .
# Se sulla NX gira ROS 2 e interferisce col domain DDS 0 dell'SDK:
# export ROS_DOMAIN_ID=1
# Se StandDown/StandUp non hanno effetto: abilita sport_mode dall'app Go2; poi opzionale MotionSwitcher:
# export GO2_SPORT_MOTION_PREPARE=1
# export GO2_SPORT_SELECT_MODE=normal
# Crouch/Stand: 202 subito + esito in GET /api/base/sport_last (evita router/browser che chiudono la TCP sulla RPC DDS lunga).
export GO2_SPORT_ASYNC_STAND_MODES=1
# StandDown/StandUp in subprocess (segfault Cyclone non uccide Flask).
export GO2_SPORT_SUBPROCESS_STAND_MODES=1
export GO2_GRASP_EXECUTE_ARM=1
export GO2_DASHBOARD_HOST=0.0.0.0
export GO2_DASHBOARD_PORT=5050
# Ritardo tra crash Flask e riavvio (loop nx_dashboard_supervise.sh)
# export GO2_DASHBOARD_RESTART_DELAY_S=15
export GO2_VIS_GEOMETRY_DEFAULT_PRESET=2
export GO2_CAMERA_AUTO_USB_MAP=1
export GO2_REALSENSE_V4L_DEFAULT=6
export GO2_REALSENSE_VIDEO_PROBE=1
export D1_SEARCH_MAX_CYCLES=10
export GO2_GRASP_USE_FUSED_PLAN_IK=1
export GO2_GRASP_FUSED_WITH_CENTER=1
export GO2_TRUST_WRIST_ABSOLUTE_IK=1
# Fold + START: senza goto START il braccio resta dove capita → piano/tag spesso assenti e grasp «non va».
export GO2_GRASP_START_FOLD=1
export GO2_GRASP_GOTO_SAVED_START=1
export GO2_GRASP_WAIT_TAG_BEFORE_START_POSE=0
# Ultima spiaggia se il polso non locka mai (solo se area libera / rischio accettabile in laboratorio).
export GO2_FRONT_CAMERA_FALLBACK_GRASP=1
# Grasp: meno punti fold→START (meno «cede»); yaw tag in immagine solo se GO2_GRASP_ORIENT_PREVIEW_IMAGE_AS_BASE_YAW=1.
export GO2_GRASP_FAST_START_ALIGN=1
export GO2_GRASP_START_PREHOLD_CAP=5
export D1_GRASP_START_ALIGN_MAX_STEP_DEG=4.8,2.5,2.3,3.4,4.8,5.0,8.5
export D1_GRASP_FOLD_MAX_STEP_DEG=4.5,2.3,2.1,3.0,4.5,4.8,8.0
export GO2_GRASP_ORIENT_PREVIEW_TO_TAG=1
# 0 (default): NON ruotare offset pre‑presa nel base XY con lo yaw del tag **in immagine** (errore con camera polso).
# 1 = sperimentale (può «allontanare» il braccio se ψ immagine ≠ rotazione base).
export GO2_GRASP_ORIENT_PREVIEW_IMAGE_AS_BASE_YAW=0
# 0 (default): asse grip da bordo reale AprilTag; 1 = vecchio asse AABB orizzontale (~0°).
# export GO2_GRASP_TAG_AXIS_USE_AABB=0
# Piano fuso stabile: 1 = IK dopo un solo frame «ready» (più reattivo; 2–3 se il piano jittera).
export GO2_GRASP_FUSED_CONFIRM_FRAMES=1
export D1_SEARCH_DELAY_MS=260
export D1_PLAN_DELAY_MS=420
export D1_START_ALIGN_DELAY_MS=260
export D1_FOLD_DELAY_MS=620
export D1_ZERO_TO_START_DELAY_MS=520
export D1_EDITOR_MOVE_DELAY_MS=340
export D1_ONE_JOINT_DELAY_MS=320
export D1_ONE_JOINT_MAX_STEP_DEG=1.4,0.7,0.6,0.9,1.4,1.6,4.0
export D1_MAX_STEP_DEG_SEARCH=2.0,1.0,1.7,2.5,2.5,3.0,5.0
export D1_MAX_STEP_DEG_GRASP=1.5,0.8,1.2,2.0,2.0,2.5,4.0
export D1_START_ALIGN_MAX_STEP_DEG=3.0,1.4,1.2,1.8,3.0,3.2,5.5
export D1_FOLD_MAX_STEP_DEG=2.0,1.0,0.9,1.2,2.0,2.2,4.0
export D1_EDITOR_MAX_STEP_DEG=1.6,0.8,0.7,1.0,1.6,1.8,4.0
export D1_INTERP_EASE=linear
export D1_MOTION_REHOME_FEEDBACK=1
export D1_MOTION_STABLE_START=1
export D1_START_USE_MEDIAN=1
export D1_REHOME_USE_MEDIAN=1
export D1_FEEDBACK_SAMPLES=3
export D1_FEEDBACK_SAMPLE_GAP_S=0.035
export D1_FEEDBACK_MEDIAN_SAMPLES=3
export D1_FEEDBACK_MEDIAN_GAP_S=0.03
export D1_PATH_POINT_REPEAT=1
export D1_ABORTABLE_MOTION_CHUNKS=1
# Più messaggi per chunk = meno confini subprocess (meno «cede» tra uno step e l’altro).
export D1_ABORTABLE_CHUNK_MESSAGES=20
# Tra un chunk e l’altro: hold dalla posa feedback (anti-cedimento / ripetizione coppia posizione).
export GO2_D1_HOLD_BETWEEN_CHUNKS=1
export D1_INTER_CHUNK_HOLD_REPEATS=18
export D1_INTER_CHUNK_HOLD_DELAY_MS=58
# Hold generico (emergenza, publish_d1_hold_current default): più ripetizioni = meno cedimento tra comandi.
export D1_HOLD_REPEATS=22
export D1_HOLD_DELAY_MS=88
export D1_ZERO_TO_START_SPLIT=1
export D1_ZERO_TO_START_SETTLE_REPEATS=5
export D1_ZERO_TO_START_SETTLE_DELAY_MS=45
export D1_ZERO_TO_START_DELAY_MS=175
export D1_TRUE_ZERO_DELAY_MS=175
export D1_ZERO_TRANSITION_MAX_STEP_DEG=3.4,1.7,1.5,2.0,3.4,3.8,6.5
export D1_ZERO_TRANSITION_INTERP=smoothstep
export GO2_CAMERA_CACHE_FPS=20
export GO2_MJPEG_FRAME_PERIOD_S=0.05
export GO2_MJPEG_FIRST_FRAME_WAIT_S=1.8
export GO2_APRILTAG_MJPEG_PERIOD_S=0.12
export GO2_ANNOTATED_JPEG_QUALITY=72
export GO2_CLASSIC_BOX_FALLBACK=1
export GO2_YOLO_IMGSZ=640
export GO2_YOLO_CONF=0.30
export GO2_VISUAL_SERVO_STOP_ON_DIVERGE=1
export GO2_WRIST_CENTER_STEP_GAIN=0.22
export GO2_WRIST_CENTER_MAX_YAW_STEP_DEG=0.8
export GO2_WRIST_CENTER_MAX_SHOULDER_STEP_DEG=0.45
export GO2_WRIST_CENTER_MAX_WRIST_STEP_DEG=0.9
export GO2_WRIST_ONLY_CYCLE_SLEEP_S=0.18
export D1_LIVE_REPEAT=5
export D1_LIVE_DELAY_MS=26
export D1_GOTO_PREHOLD=1
export D1_GOTO_PREHOLD_REPEATS=12
export D1_GOTO_PREHOLD_DELAY_MS=55
export D1_START_PREHOLD=1
export D1_START_PREHOLD_REPEATS=10
export D1_START_PREHOLD_DELAY_MS=55
export D1_POST_MOTION_HOLD_REPEATS=14
export D1_POST_MOTION_HOLD_DELAY_MS=60
""".strip()


def _nx_dashboard_env_sh() -> str:
    return (
        "#!/bin/bash\n# Sorgente unica env dashboard (deploy_dashboard_to_nx.py).\n"
        "# shellcheck disable=SC2034\n"
        + NX_EXPORTS
        + "\n"
    )


def _nx_start_dashboard_sh(host: str) -> str:
    return f"""#!/bin/bash
set -e
cd {REMOTE_BASE} || exit 1
source "{REMOTE_BASE}/scripts/nx_dashboard_env.sh"
pkill -f nx_dashboard_supervise.sh 2>/dev/null || true
pkill -f diagnostics_dashboard 2>/dev/null || true
pkill -f serve_dashboard_modular 2>/dev/null || true
sleep 1
python3 -c "import diagnostics_dashboard as d; print('legacy_module_ok', d.GO2_LOCAL, d.GO2_DASHBOARD_BIND)"
nohup bash scripts/nx_dashboard_supervise.sh >> dashboard_supervise.log 2>&1 &
echo $! > dashboard.pid
sleep 4
python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/api/health', timeout=10); print('HTTP_HEALTH_OK')" || (
  echo HTTP_HEALTH_FAIL
  tail -40 dashboard_run.log
  tail -20 dashboard_supervise.log 2>/dev/null || true
  exit 1
)
echo "Remote checks done (full smoke: run on PC: python scripts/test_dashboard_smoke.py)"
echo "From your laptop on LAN: python scripts/verify_dashboard_http.py http://{host}:5050"
"""


def _nx_boot_dashboard_wrapper_sh() -> str:
    """Cron @reboot: tutto in background — errori non bloccano il boot."""
    log = f"{REMOTE_BASE}/dashboard_boot.log"
    return f"""#!/bin/bash
# Avvio automatico post-accensione (cron @reboot). Fallimenti solo su log, exit 0 immediato.
LOG="{log}"
{{
  echo "=== boot $(date -Is) nx_boot_dashboard_wrapper pid=$$ ==="
  sleep 45
  set +e
  cd {REMOTE_BASE} || {{ echo "cd_fail"; exit 0; }}
  if ! source "{REMOTE_BASE}/scripts/nx_dashboard_env.sh"; then
    echo "env_source_fail"
    exit 0
  fi
  pkill -f nx_dashboard_supervise.sh 2>/dev/null || true
  pkill -f diagnostics_dashboard 2>/dev/null || true
  pkill -f serve_dashboard_modular.py 2>/dev/null || true
  sleep 2
  nohup bash scripts/nx_dashboard_supervise.sh >> dashboard_supervise.log 2>&1 &
  echo $! > dashboard.pid || true
  sleep 8
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/api/health', timeout=20)" 2>/dev/null; then
    echo "HTTP_HEALTH_OK (boot)"
  else
    echo "WARN: health non risponde subito dopo boot (vedi dashboard_run.log e dashboard_supervise.log)"
  fi
  echo "=== fine boot $(date -Is) ==="
}} >> "$LOG" 2>&1 &
exit 0
"""


def _remote_install_crontab(ssh: paramiko.SSHClient) -> None:
    marker = "GO2_DASHBOARD_AUTOSTART"
    reboot_line = f"@reboot /bin/bash {REMOTE_BASE}/scripts/nx_boot_dashboard_wrapper.sh"
    script = f"""set +e
TMP=$(mktemp)
( crontab -l 2>/dev/null | grep -v '{marker}' | grep -v nx_boot_dashboard_wrapper.sh || true
  echo '# {marker}'
  echo '{reboot_line}'
) > "$TMP"
crontab "$TMP"
EC=$?
rm -f "$TMP"
echo "crontab: installed {marker} exit=$EC"
crontab -l 2>/dev/null | tail -n 8
"""
    stdin, stdout, stderr = ssh.exec_command(script)
    out = stdout.read().decode()
    err = stderr.read().decode()
    print(out.strip())
    if err.strip():
        print("crontab stderr:", err.strip())


def _remote_install_systemd_user_optional(ssh: paramiko.SSHClient) -> None:
    """Installa unit user (senza enable/restart): evita conflitto con pkill in nx_start / cron nohup."""
    script = f"""set +e
mkdir -p "$HOME/.config/systemd/user"
cp -f "{REMOTE_BASE}/scripts/go2-visual-dashboard.service" "$HOME/.config/systemd/user/go2-visual-dashboard.service"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if [ -S "$XDG_RUNTIME_DIR/bus" ]; then
  systemctl --user daemon-reload 2>/dev/null
  echo "SYSTEMD_USER: unit in ~/.config/systemd/user (opzionale: systemctl --user enable --now go2-visual-dashboard; prima disabilita cron se doppio avvio)"
else
  echo "SYSTEMD_USER: unit copiata; niente dbus session — usa cron @reboot o loginctl enable-linger"
fi
"""
    stdin, stdout, stderr = ssh.exec_command(script)
    print(stdout.read().decode().strip())
    err = stderr.read().decode().strip()
    if err:
        print("systemd user stderr:", err)


def _remote_install_realsense_udev(ssh: paramiko.SSHClient) -> None:
    """Regola udev per /dev/video* RealSense (8086:0b3a) — richiede sudo sulla NX."""
    pwd = nx_password()
    src = f"{REMOTE_BASE}/scripts/udev/99-go2-realsense-dashboard.rules"
    dst = "/etc/udev/rules.d/99-go2-realsense-dashboard.rules"
    cmd = (
        f"sudo -S bash -c 'cp -f {src} {dst} && udevadm control --reload-rules "
        f"&& udevadm trigger --subsystem-match=video4linux'"
    )
    stdin, stdout, stderr = ssh.exec_command(cmd)
    stdin.write(pwd + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if code == 0:
        print("[deploy] udev RealSense: installata e trigger video4linux OK")
    else:
        print(f"[deploy] udev RealSense: sudo fallito (exit {code}) — copia manuale su NX:")
        print(f"  sudo cp {src} {dst} && sudo udevadm control --reload-rules && sudo udevadm trigger -c change -s video4linux")
    if out:
        print(out)
    if err and "password" not in err.lower():
        print("udev stderr:", err)


def _remote_run_probe(ssh: paramiko.SSHClient) -> None:
    stdin, stdout, stderr = ssh.exec_command(f"bash {REMOTE_BASE}/scripts/nx_peripheral_probe.sh")
    print("--- nx_peripheral_probe ---")
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("probe stderr:", err)


def _remote_build_d1_arm_helpers(ssh: paramiko.SSHClient) -> None:
    """Compila ``bin/d1_arm_command`` e ``bin/d1_arm_feedback_helper`` sulla NX (g++ + Unitree SDK2)."""
    print("[deploy] Compilazione helper braccio D1 (bin/d1_arm_command) …")
    log = "/tmp/go2_d1_arm_helpers_build.log"
    script = f"""set +e
cd "{REMOTE_BASE}"
mkdir -p bin
rm -f bin/d1_arm_command bin/d1_arm_feedback_helper
set -a
if [ -f scripts/nx_dashboard_env.sh ]; then
  # shellcheck disable=SC1091
  . scripts/nx_dashboard_env.sh
fi
set +a
bash scripts/build_d1_arm_helpers.sh >{log} 2>&1
EC=$?
echo "EXIT_CODE=$EC" >>{log}
cat {log}
chmod +x bin/d1_arm_command bin/d1_arm_feedback_helper 2>/dev/null || true
ls -la bin/d1_arm_command bin/d1_arm_feedback_helper 2>/dev/null || true
exit $EC
"""
    stdin, stdout, stderr = ssh.exec_command(script)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.strip())
    if err.strip():
        print("build_d1_helpers stderr:", err.strip())
    if code != 0:
        print(
            f"[deploy] ERRORE: build_d1_arm_helpers.sh exit={code} — "
            "verifica g++, Unitree SDK2 e CYCLONEDDS_HOME sulla NX."
        )
    else:
        print("[deploy] Helper D1 compilati OK.")


def _mesh_rel_paths() -> list[str]:
    """Path relativi alla root repo per mesh Go2 (.obj) e D1 (.STL) servite dal viewer."""
    out: list[str] = []
    d1 = REPO_ROOT / "unitree_mujoco/unitree_robots/go2_d1/d1_550_description/meshes"
    if d1.is_dir():
        for p in sorted(d1.glob("*.STL")):
            out.append(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    ad = REPO_ROOT / "unitree_mujoco/unitree_robots/go2_d1/assets"
    if ad.is_dir():
        for p in sorted(ad.glob("*.obj")):
            out.append(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    return out


def _d1_urdf_rel_paths() -> list[str]:
    """URDF e sidecar da repo Jeewantha (stessi file usati dal parser in diagnostics_dashboard)."""
    d = REPO_ROOT / "unitree_mujoco/unitree_robots/go2_d1/d1_550_description/urdf"
    if not d.is_dir():
        return []
    out: list[str] = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in (".urdf", ".xacro", ".csv") and p.is_file():
            out.append(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    return out


def _scene_xml_rel_paths() -> list[str]:
    """XML MuJoCo per ``/api/mujoco/preview.png`` (NX deve avere gli include)."""
    rels = [
        "unitree_mujoco/unitree_robots/go2_d1/scene_d1_mesh.xml",
        "unitree_mujoco/unitree_robots/go2_d1/go2_d1_d1mesh.xml",
    ]
    return [r for r in rels if (REPO_ROOT / r).is_file()]


def main() -> None:
    host = nx_host()
    print(f"[deploy] Connecting SSH {nx_user()}@{host} …")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=nx_user(), password=nx_password(), timeout=45)
    stdin, stdout, stderr = ssh.exec_command(
        f"mkdir -p {REMOTE_BASE}/unitree_mujoco/unitree_robots/go2_d1 "
        f"{REMOTE_BASE}/unitree_mujoco/unitree_robots/go2_d1/assets "
        f"{REMOTE_BASE}/unitree_mujoco/unitree_robots/go2_d1/d1_550_description/meshes "
        f"{REMOTE_BASE}/unitree_mujoco/unitree_robots/go2_d1/d1_550_description/urdf "
        f"{REMOTE_BASE}/go2_dashboard/blueprints "
        f"{REMOTE_BASE}/msg "
        f"{REMOTE_BASE}/templates "
        f"{REMOTE_BASE}/data "
        f"{REMOTE_BASE}/bin "
        f"{REMOTE_BASE}/scripts/udev"
    )
    stdout.channel.recv_exit_status()
    sftp = ssh.open_sftp()
    rel_presets = "data/vis_geometry_presets.json"
    rp_presets = f"{REMOTE_BASE}/{rel_presets}"
    force_presets = os.environ.get("GO2_DEPLOY_OVERWRITE_PRESETS", "").strip().lower() in ("1", "true", "yes", "on")
    try:
        sftp.stat(rp_presets)
        remote_presets_exist = True
    except Exception:
        remote_presets_exist = False
    for rel in REMOTE_PUSH_FILES:
        loc = REPO_ROOT / rel
        if not loc.is_file():
            print("skip missing", loc)
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed", rel)
    loc_pr = REPO_ROOT / rel_presets
    if loc_pr.is_file():
        if force_presets or not remote_presets_exist:
            sftp.put(str(loc_pr), rp_presets)
            print(
                "pushed",
                rel_presets,
                "(GO2_DEPLOY_OVERWRITE_PRESETS=1)" if force_presets and remote_presets_exist else "",
            )
        else:
            print(
                "[deploy] keep remote",
                rel_presets,
                "— preset salvati sulla NX non sovrascritti (imposta GO2_DEPLOY_OVERWRITE_PRESETS=1 per forzare)",
            )
    for rel in _mesh_rel_paths():
        loc = REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed mesh", rel)
    for rel in _d1_urdf_rel_paths():
        loc = REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed urdf", rel)
    for rel in _scene_xml_rel_paths():
        loc = REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed scene_xml", rel)

    for path, content, mode in (
        (f"{REMOTE_BASE}/scripts/nx_dashboard_env.sh", _nx_dashboard_env_sh(), 0o644),
        (f"{REMOTE_BASE}/scripts/nx_start_dashboard.sh", _nx_start_dashboard_sh(host), 0o755),
        (f"{REMOTE_BASE}/scripts/nx_boot_dashboard_wrapper.sh", _nx_boot_dashboard_wrapper_sh(), 0o755),
    ):
        with sftp.file(path, "wb") as rf:
            rf.write(content.encode("utf-8"))
        sftp.chmod(path, mode)
        print("wrote", path)

    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_serve_foreground.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_dashboard_supervise.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_machine_diag.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_peripheral_probe.sh", 0o755)
    sftp.chmod(f"{REMOTE_BASE}/scripts/nx_print_cyclone_diag.sh", 0o755)
    # Checkout Windows può lasciare CRLF negli .sh — bash sulla NX si rompe (set: +\r).
    strip_stdin, strip_stdout, strip_stderr = ssh.exec_command(
        f"bash -lc \"sed -i 's/\\\\r$//' {REMOTE_BASE}/scripts/*.sh 2>/dev/null || true\""
    )
    strip_stdout.channel.recv_exit_status()
    sftp.close()

    _remote_build_d1_arm_helpers(ssh)

    print("[deploy] Install cron @reboot (non blocca boot; log in dashboard_boot.log) …")
    _remote_install_crontab(ssh)
    print("[deploy] Optional systemd --user unit …")
    _remote_install_systemd_user_optional(ssh)
    print("[deploy] udev RealSense (permessi /dev/video*) …")
    _remote_install_realsense_udev(ssh)

    print("[deploy] Riavvio dashboard ora …")
    stdin, stdout, stderr = ssh.exec_command(f"bash {REMOTE_BASE}/scripts/nx_start_dashboard.sh")
    print(stdout.read().decode())
    err = stderr.read().decode()
    if err.strip():
        print("stderr:", err)

    print("[deploy] Probe periferiche / rete …")
    _remote_run_probe(ssh)
    ssh.close()


if __name__ == "__main__":
    main()
