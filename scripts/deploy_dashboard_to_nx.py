#!/usr/bin/env python3
"""
Push dashboard/planner sources to the Jetson + restart Flask ON the NX.
Run from dev PC:  python scripts/deploy_dashboard_to_nx.py
"""
import paramiko
from pathlib import Path

HOST = "192.168.123.18"
USER = "unitree"
PWD = "123"
REMOTE_BASE = "/home/unitree/go2_visual_dashboard"
REPO_ROOT = Path(__file__).resolve().parent.parent

REMOTE_PUSH_FILES = [
    "diagnostics_dashboard.py",
    "scripts/box_grasp_planner.py",
    "scripts/box_object_detector.py",
    "scripts/arm_kinematics_d1_template.py",
    "scripts/go2_accompany.py",
    "scripts/d1_drag_follow_experimental.py",
]

NX_SCRIPT = """#!/bin/bash
set -e
cd %s || exit 1
export GO2_LOCAL=1
export GO2_ENABLE_REAL_ARM=1
export GO2_ENABLE_BASE_MOTION=1
# Presa: 1 = «Avvia grasp» consentito dopo preflight. 0 = solo pianificazione / sicura.
export GO2_GRASP_EXECUTE_ARM=1
export GO2_DASHBOARD_HOST=0.0.0.0
export GO2_DASHBOARD_PORT=5050
export D1_SEARCH_MAX_CYCLES=12
export GO2_GRASP_USE_FUSED_PLAN_IK=1
export GO2_TRUST_WRIST_ABSOLUTE_IK=1
export GO2_GRASP_START_FOLD=0
export GO2_GRASP_GOTO_SAVED_START=0
export GO2_GRASP_WAIT_TAG_BEFORE_START_POSE=0
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
export D1_ABORTABLE_CHUNK_MESSAGES=8
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
# Se l'overlay AprilTag frontale è nero ma i tag si vedono altrove: indice V4L RGB ≠ 6
# export GO2_VIDEO_INDEX_6=4
export D1_LIVE_REPEAT=5
export D1_LIVE_DELAY_MS=26
export D1_GOTO_PREHOLD=1
export D1_GOTO_PREHOLD_REPEATS=12
export D1_GOTO_PREHOLD_DELAY_MS=55
export D1_START_PREHOLD=1
export D1_START_PREHOLD_REPEATS=10
export D1_START_PREHOLD_DELAY_MS=55
export D1_POST_MOTION_HOLD_REPEATS=10
export D1_POST_MOTION_HOLD_DELAY_MS=55
pkill -f diagnostics_dashboard 2>/dev/null || true
sleep 1
python3 -c "import diagnostics_dashboard as d; print('GO2_LOCAL_active', d.GO2_LOCAL, 'bind', d.GO2_DASHBOARD_BIND)"
nohup python3 diagnostics_dashboard.py >> dashboard_run.log 2>&1 &
echo $! > dashboard.pid
sleep 4
python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/api/health', timeout=10); print('HTTP_HEALTH_OK')" || (
  echo HTTP_HEALTH_FAIL
  tail -40 dashboard_run.log
  exit 1
)
echo "Remote checks done (full smoke: run on PC: python scripts/test_dashboard_smoke.py)"
echo "From your laptop on LAN: python scripts/verify_dashboard_http.py http://192.168.123.18:5050"
""" % REMOTE_BASE


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PWD, timeout=45)
    sftp = ssh.open_sftp()
    for rel in REMOTE_PUSH_FILES:
        loc = REPO_ROOT / rel
        if not loc.is_file():
            print("skip missing", loc)
            continue
        remote_path = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed", rel)
    path = f"{REMOTE_BASE}/scripts/nx_start_dashboard.sh"
    with sftp.file(path, "wb") as rf:
        rf.write(NX_SCRIPT.encode("utf-8"))
    sftp.chmod(path, 0o755)
    sftp.close()

    stdin, stdout, stderr = ssh.exec_command(f"bash {path}")
    print(stdout.read().decode())
    err = stderr.read().decode()
    if err.strip():
        print("stderr:", err)
    ssh.close()


if __name__ == "__main__":
    main()
