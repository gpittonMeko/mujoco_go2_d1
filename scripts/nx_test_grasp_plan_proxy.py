#!/usr/bin/env python3
"""POST /api/grasp/plan sulla NX (proxy AWS) con JPEG sintetico."""
from __future__ import annotations

import base64
import json
import os
import sys

import cv2
import numpy as np
import paramiko

REMOTE = "/home/unitree/go2_visual_dashboard"


def main() -> int:
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.rectangle(img, (200, 150), (440, 330), (80, 200, 255), -1)
    _, jpg = cv2.imencode(".jpg", img)
    body = json.dumps(
        {
            "instruction": "afferra la scatola",
            "logical_camera_device": 0,
            "jpeg_base64": base64.standard_b64encode(jpg.tobytes()).decode(),
        }
    )
    host = os.environ.get("GO2_NX_HOST", "192.168.123.18")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username="unitree", password=os.environ.get("GO2_NX_PASSWORD", "123"), timeout=45)
    sftp = ssh.open_sftp()
    with sftp.file("/tmp/grasp_plan_test.json", "w") as f:
        f.write(body)
    sftp.close()
    cmd = (
        f"bash -lc 'source {REMOTE}/scripts/nx_dashboard_env.sh; "
        f"[[ -f {REMOTE}/scripts/nx_secrets_dashboard.sh ]] && source {REMOTE}/scripts/nx_secrets_dashboard.sh; "
        "curl -sf -X POST http://127.0.0.1:5052/api/grasp/plan "
        "-H Content-Type:application/json --data-binary @/tmp/grasp_plan_test.json'"
    )
    _, o, e = ssh.exec_command(cmd, timeout=180)
    raw = o.read().decode()
    err = e.read().decode()
    if not raw.strip():
        print("FAIL:", err, file=sys.stderr)
        ssh.close()
        return 1
    plan = json.loads(raw)
    print(json.dumps({k: plan.get(k) for k in ("ok", "backend", "grasp_display_base_link_m", "grasp_assessment")}, indent=2))
    ass = plan.get("grasp_assessment") or {}
    print("assessment tier:", ass.get("tier"), "execution_allowed:", ass.get("execution_allowed"))
    ssh.close()
    if not plan.get("ok"):
        return 1
    print("NX_TEST_GRASP_PLAN_PROXY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
