#!/usr/bin/env python3
"""Diagnostica stream Vision sulla NX."""
import os
import paramiko

host = os.environ.get("GO2_NX_HOST", "192.168.123.18")
pwd = os.environ.get("GO2_NX_PASSWORD", "123")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username="unitree", password=pwd, timeout=25)

cmds = [
    "curl -s -o /dev/null -w 'health %{http_code}\\n' http://127.0.0.1:5053/api/vision/health",
    "curl -s http://127.0.0.1:5053/api/vision/camera/status 2>/dev/null | head -c 1200",
    "curl -s http://127.0.0.1:5053/api/vision/streams/status 2>/dev/null | head -c 600",
    "curl -s -o /tmp/vsnap.jpg -w 'snapshot %{http_code} size=%{size_download}\\n' http://127.0.0.1:5053/api/vision/camera/snapshot.jpg",
    "file /tmp/vsnap.jpg 2>/dev/null; ls -la /tmp/vsnap.jpg 2>/dev/null",
    "timeout 3 curl -s -o /tmp/detect.bin -w 'detect_mjpg %{http_code} bytes=%{size_download}\\n' http://127.0.0.1:5053/stream/vision/detect.mjpg 2>/dev/null; ls -la /tmp/detect.bin 2>/dev/null",
    "timeout 3 curl -s -o /tmp/color.bin -w 'color_mjpg %{http_code} bytes=%{size_download}\\n' http://127.0.0.1:5053/stream/vision/color.mjpg 2>/dev/null; ls -la /tmp/color.bin 2>/dev/null",
    "pgrep -af 'serve_d1_jog|5053' | head -5",
    "tail -30 /tmp/d1_jog.log 2>/dev/null || tail -30 /home/unitree/go2_visual_dashboard/logs/d1_jog.log 2>/dev/null || echo 'no log'",
    "python3 -c \"from go2_dashboard import realsense_pyrs as r; r.start(); b=r.read_bundle(); print('pyrs', r.status(), 'color', None if b is None else b.color.shape if b.color is not None else None)\" 2>&1 | tail -5",
]
for c in cmds:
    print("\n===", c[:70], "===")
    _, o, e = ssh.exec_command(c, timeout=45)
    print(o.read().decode(errors="replace"))
    err = e.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err[:400])
ssh.close()
