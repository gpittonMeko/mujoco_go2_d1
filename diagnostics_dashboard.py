#!/usr/bin/env python3
"""
Read-only diagnostics dashboard for a Unitree Go2 lab setup.

The dashboard deliberately avoids publishing low-level commands or moving the
robot. Tests are limited to ping/TCP checks, SSH inventory commands, local USB
enumeration, local webcam probing, and an optional DDS lowstate subscription.
"""

from __future__ import annotations

import json
import math
import os
import platform
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template_string

try:
    import cv2
except Exception:  # pragma: no cover - optional runtime dependency
    cv2 = None

try:
    import paramiko
except Exception:  # pragma: no cover - optional runtime dependency
    paramiko = None


PROJECT_ROOT = Path(__file__).resolve().parent
GO2_HOST = os.environ.get("GO2_HOST", "192.168.123.18")
GO2_INTERNAL_HOST = os.environ.get("GO2_INTERNAL_HOST", "192.168.123.222")
GO2_USER = os.environ.get("GO2_USER", "unitree")
GO2_PASSWORD = os.environ.get("GO2_PASSWORD", "123")
GO2_DDS_INTERFACE = os.environ.get("GO2_DDS_INTERFACE", "")
GO2_DDS_DOMAIN = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
GO2_LOCAL = os.environ.get("GO2_LOCAL", "0").lower() in {"1", "true", "yes"}
XT16_HOST = os.environ.get("XT16_HOST", "192.168.123.20")
SERVO_ARM_HOST = os.environ.get("SERVO_ARM_HOST", "192.168.123.161")
ETHERNET_CANDIDATES = [
    host.strip()
    for host in os.environ.get("GO2_ETHERNET_CANDIDATES", f"{XT16_HOST},{SERVO_ARM_HOST},192.168.123.100").split(",")
    if host.strip()
]

APP = Flask(__name__)
STATUS_LOCK = threading.Lock()
STATUS: dict[str, Any] = {
    "updated_at": None,
    "running": False,
    "summary": "No diagnostics run yet.",
    "tests": {},
}

CAMERA_DEVICES = {
    0: "Sonix HD 1080P PC-Camera (arm/external USB)",
    6: "Intel RealSense D435i RGB stream",
}
# Smaller steps on shoulder/elbow/wrist for search — avoids slamming and visible stepping.
D1_MAX_STEP_DEG = [3.0, 1.5, 2.5, 4.0, 4.0, 5.0, 8.0]
D1_SEARCH_COMMAND_DELAY_MS = int(os.environ.get("D1_SEARCH_DELAY_MS", "480"))
D1_SEARCH_MAX_CYCLES = int(os.environ.get("D1_SEARCH_MAX_CYCLES", "5"))
# Nominal scan pose (servo degrees): wrist strongly pitched so the wrist camera looks toward the floor/workspace ahead.
D1_SEARCH_SHOULDER_NOM_DEG = float(os.environ.get("D1_SEARCH_SHOULDER_NOM_DEG", "-52"))
D1_SEARCH_ELBOW_NOM_DEG = float(os.environ.get("D1_SEARCH_ELBOW_NOM_DEG", "48"))
D1_SEARCH_WRIST_NOM_DEG = float(os.environ.get("D1_SEARCH_WRIST_NOM_DEG", "-74"))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class CameraCache:
    def __init__(self, devices: dict[int, str], fps: float = 8.0, jpeg_quality: int = 68):
        self.devices = devices
        self.period = 1.0 / max(fps, 1.0)
        self.jpeg_quality = jpeg_quality
        self.frames: dict[int, dict[str, Any]] = {}
        self.errors: dict[int, str] = {}
        self._stop = threading.Event()
        self._started_devices: set[int] = set()
        self._lock = threading.Lock()

    def start(self, device: int | None = None) -> None:
        if cv2 is None:
            return
        devices = [device] if device is not None else list(self.devices)
        for dev in devices:
            if dev not in self.devices or dev in self._started_devices:
                continue
            self._started_devices.add(dev)
            threading.Thread(target=self._loop, args=(dev,), daemon=True).start()

    def _loop(self, device: int) -> None:
        cap = None
        while not self._stop.is_set():
            start = time.perf_counter()
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = cv2.VideoCapture(device)
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    cap.set(cv2.CAP_PROP_FPS, 15)
                else:
                    with self._lock:
                        self.errors[device] = "open failed"
                    time.sleep(1.0)
                    continue

            ok, frame = (False, None)
            try:
                ok, frame = cap.read()
            except Exception as exc:
                with self._lock:
                    self.errors[device] = f"read failed: {exc!r}"
                cap.release()
                cap = None
                time.sleep(0.5)
                continue

            if ok and frame is not None:
                enc_ok, jpg = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
                )
                if enc_ok:
                    with self._lock:
                        self.frames[device] = {
                            "jpg": jpg.tobytes(),
                            "ts": time.time(),
                            "shape": list(frame.shape),
                            "label": self.devices[device],
                        }
                        self.errors.pop(device, None)
            else:
                with self._lock:
                    self.errors[device] = "read returned no frame"
                cap.release()
                cap = None
                time.sleep(0.5)
                continue
            delay = self.period - (time.perf_counter() - start)
            if delay > 0:
                time.sleep(delay)
        if cap is not None:
            cap.release()

    def get_jpeg(self, device: int, wait_s: float = 1.2) -> bytes | None:
        self.start(device)
        deadline = time.time() + wait_s
        while True:
            with self._lock:
                item = self.frames.get(device)
                if item is not None and time.time() - item["ts"] < 3.0:
                    return item["jpg"]
            if time.time() >= deadline:
                return None
            time.sleep(0.04)

    def stats(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            return {
                str(device): {
                    "label": self.devices[device],
                    "available": device in self.frames,
                    "age_ms": None if device not in self.frames else round((now - self.frames[device]["ts"]) * 1000, 1),
                    "shape": None if device not in self.frames else self.frames[device]["shape"],
                    "error": self.errors.get(device),
                }
                for device in self.devices
            }


CAMERA_CACHE = CameraCache(CAMERA_DEVICES)


def warmup_realtime_feeds() -> None:
    if GO2_LOCAL:
        CAMERA_CACHE.start()
        LIDAR_CACHE.start()


def decode_xt16_packet(data: bytes) -> list[list[float | int]]:
    if len(data) != 568 or data[:4] != b"\xee\xff\x06\x01":
        return []
    channel_num = data[6] if data[6] else 16
    block_num = data[7] if data[7] else 8
    distance_unit = (data[9] if len(data) > 9 and data[9] else 4) / 1000.0
    body = 12
    points: list[list[float | int]] = []
    for block_idx in range(min(block_num, 8)):
        off = body + block_idx * 66
        if off + 66 > len(data):
            break
        azimuth = int.from_bytes(data[off:off + 2], "little") / 100.0
        for channel in range(min(channel_num, 16)):
            pos = off + 2 + channel * 4
            distance = int.from_bytes(data[pos:pos + 2], "little") * distance_unit
            reflectivity = data[pos + 2]
            if 0.05 <= distance <= 120:
                points.append([round(azimuth, 2), round(distance, 3), int(reflectivity), channel])
    return points


def lidar_stats(points: list[list[float | int]]) -> dict[str, Any]:
    distances = [float(p[1]) for p in points]
    per_channel = {str(i): 0 for i in range(16)}
    for point in points:
        per_channel[str(int(point[3]))] += 1
    return {
        "visible_points": len(points),
        "total_points_analyzed": len(points),
        "min_m": round(min(distances), 3) if distances else None,
        "max_m": round(max(distances), 3) if distances else None,
        "avg_m": round(sum(distances) / len(distances), 3) if distances else None,
        "per_channel": per_channel,
    }


class LidarCache:
    def __init__(self, port: int = 2368, max_points: int = 25000):
        self.port = port
        self.max_points = max_points
        self.points: list[list[float | int]] = []
        self.sources: dict[str, int] = {}
        self.packet_count = 0
        self.error: str | None = None
        self.last_ts = 0.0
        self._stop = threading.Event()
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                sock.bind(("0.0.0.0", self.port))
                sock.settimeout(0.5)
                with self._lock:
                    self.error = None
                while not self._stop.is_set():
                    try:
                        data, addr = sock.recvfrom(2048)
                    except socket.timeout:
                        continue
                    pts = decode_xt16_packet(data)
                    if not pts:
                        continue
                    with self._lock:
                        self.packet_count += 1
                        self.sources[addr[0]] = self.sources.get(addr[0], 0) + 1
                        self.points.extend(pts)
                        if len(self.points) > self.max_points:
                            self.points = self.points[-self.max_points:]
                        self.last_ts = time.time()
            except Exception as exc:
                with self._lock:
                    self.error = repr(exc)
                time.sleep(1.0)
            finally:
                if sock is not None:
                    sock.close()

    def frame(self, limit: int = 1800) -> dict[str, Any]:
        self.start()
        with self._lock:
            points = list(self.points[-limit:])
            packet_count = self.packet_count
            sources = dict(self.sources)
            age_ms = None if not self.last_ts else round((time.time() - self.last_ts) * 1000, 1)
        return {
            "ok": bool(points),
            "host": XT16_HOST,
            "port": self.port,
            "packets": packet_count,
            "sources": sources,
            "points": points,
            "stats": lidar_stats(points),
            "age_ms": age_ms,
            "mode": "local-cache",
            "error": self.error,
        }


LIDAR_CACHE = LidarCache()


def run_local(command: list[str] | str, timeout: float = 8.0) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=isinstance(command, str),
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "stdout": "", "stderr": ""}


def ping_host(host: str, count: int = 2, timeout_ms: int = 1000) -> dict[str, Any]:
    if platform.system().lower() == "windows":
        cmd = ["ping", "-n", str(count), "-w", str(timeout_ms), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(max(1, timeout_ms // 1000)), host]
    result = run_local(cmd, timeout=max(4.0, count * (timeout_ms / 1000.0 + 1.0)))
    result["host"] = host
    return result


def tcp_port(host: str, port: int, timeout: float = 0.8) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "ok": True,
                "host": host,
                "port": port,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
    except Exception as exc:
        return {"ok": False, "host": host, "port": port, "error": str(exc)}


def local_usb_inventory() -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return run_local(["bash", "-lc", "lsusb; ls -l /dev/video* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true"])

    ps = (
        "Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.Class -in @('Camera','Image','Media','USB','Ports','Net','Sensor','USBDevice','SoftwareDevice') "
        "-or $_.FriendlyName -match 'RealSense|Depth|Lidar|LiDAR|XY|YDLIDAR|CH340|CP210|USB Serial|Serial|Camera|WebCam|UVC' "
        "-or $_.InstanceId -match 'VID_8086|VID_10C4|VID_1A86|VID_0403' } | "
        "Sort-Object Class,FriendlyName | "
        "Select-Object Class,Status,FriendlyName,InstanceId | ConvertTo-Json -Depth 3"
    )
    result = run_local(["powershell", "-NoProfile", "-Command", ps], timeout=12)
    devices: list[dict[str, Any]] = []
    if result.get("stdout"):
        try:
            parsed = json.loads(result["stdout"])
            devices = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            devices = []
    result["devices"] = devices
    result["detected"] = classify_devices(result.get("stdout", ""))
    result["ok"] = bool(devices)
    return result


def classify_devices(text: str) -> dict[str, bool]:
    lowered = text.lower()
    return {
        "realsense": "realsense" in lowered or "depth camera 435" in lowered or "8086:0b3a" in lowered,
        "webcam": any(token in lowered for token in ["webcam", "uvc", "pc-camera", "camera"]),
        "lidar_serial": any(token in lowered for token in ["lidar", "ydlidar", "cp210", "ch340", "ttyusb", "usb serial"]),
        "servo_arm_usb": bool(re.search(r"\b(servo|d1[-_ ]?arm|unitree d1)\b", lowered)),
    }


def probe_local_webcams(max_index: int = 5) -> dict[str, Any]:
    if cv2 is None:
        return {"ok": False, "error": "cv2 is not installed"}

    found = []
    for index in range(max_index):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if platform.system().lower() == "windows" else 0)
        try:
            opened = bool(cap.isOpened())
            frame_ok = False
            shape = None
            if opened:
                frame_ok, frame = cap.read()
                if frame_ok and frame is not None:
                    shape = list(frame.shape)
            if opened or frame_ok:
                found.append({"index": index, "opened": opened, "frame_ok": bool(frame_ok), "shape": shape})
        finally:
            cap.release()
    return {"ok": bool(found), "cameras": found}


def run_robot_shell(command: str, timeout: float = 10.0) -> dict[str, Any]:
    if GO2_LOCAL:
        return run_local(["bash", "-lc", command], timeout=timeout)
    return ssh_run(GO2_HOST, command, timeout=timeout)


def ssh_run(host: str, command: str, timeout: float = 10.0) -> dict[str, Any]:
    if paramiko is None:
        return {"ok": False, "error": "paramiko is not installed"}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host,
            username=GO2_USER,
            password=GO2_PASSWORD,
            timeout=5,
            banner_timeout=5,
            auth_timeout=5,
        )
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        return {"ok": True, "host": host, "command": command, "stdout": out, "stderr": err}
    except Exception as exc:
        return {"ok": False, "host": host, "command": command, "error": repr(exc)}
    finally:
        client.close()


def ssh_run_bytes(host: str, command: str, timeout: float = 10.0) -> dict[str, Any]:
    if paramiko is None:
        return {"ok": False, "error": "paramiko is not installed"}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host,
            username=GO2_USER,
            password=GO2_PASSWORD,
            timeout=5,
            banner_timeout=5,
            auth_timeout=5,
        )
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read()
        err = stderr.read().decode(errors="replace").strip()
        return {"ok": True, "stdout": out, "stderr": err}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "stdout": b"", "stderr": ""}
    finally:
        client.close()


def robot_camera_jpeg(device: int) -> bytes | None:
    if GO2_LOCAL and cv2 is not None:
        return CAMERA_CACHE.get_jpeg(device)

    command = f"""
python3 - <<'PY'
import base64, cv2, sys
dev={int(device)}
cap=cv2.VideoCapture(dev)
ok=False
if cap.isOpened():
    for _ in range(3):
        ok, frame = cap.read()
        if ok:
            break
cap.release()
if not ok:
    raise SystemExit(2)
ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
if not ok:
    raise SystemExit(3)
sys.stdout.buffer.write(base64.b64encode(buf.tobytes()))
PY
"""
    result = ssh_run_bytes(GO2_HOST, command, timeout=8)
    if not result.get("ok") or not result.get("stdout"):
        return None
    import base64

    try:
        return base64.b64decode(result["stdout"], validate=False)
    except Exception:
        return None


def remote_robot_inventory() -> dict[str, Any]:
    command = r"""
set -o pipefail
echo '## host'; hostname; uname -a
echo '## ip'; ip -br addr
echo '## usb'; lsusb || true
echo '## video_serial'; ls -l /dev/video* /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/* 2>/dev/null || true
echo '## v4l2'; v4l2-ctl --list-devices 2>/dev/null || true
echo '## realsense'; rs-enumerate-devices -s 2>/dev/null || true
echo '## services'; (systemctl --no-pager --type=service --state=running 2>/dev/null | grep -Ei 'unitree|dds|cyclone|ros|realsense|camera|lidar|livox|ydlidar|hesai|xy') || true
echo '## internal_ping'; ping -c 1 -W 1 192.168.123.222 || true
"""
    result = run_robot_shell(command, timeout=15)
    text = "\n".join([result.get("stdout", ""), result.get("stderr", "")])
    result["detected"] = classify_devices(text)
    result["detected"]["realsense"] = "Intel(R) RealSense(TM) Depth Camera 435i".lower() in text.lower() or result["detected"]["realsense"]
    result["internal_pc_reachable_from_robot"] = "1 received" in text or "bytes from 192.168.123.222" in text
    result["ok"] = bool(result.get("ok")) and bool(result.get("stdout"))
    return result


def ethernet_device_scan() -> dict[str, Any]:
    ports = [22, 23, 80, 443, 502, 554, 8000, 8080, 8081, 8888, 10001, 20001, 2368, 8308, 10110]
    hosts: dict[str, Any] = {}
    for host in ETHERNET_CANDIDATES:
        host_result = {
            "ping": ping_host(host, count=1, timeout_ms=700),
            "ports": [tcp_port(host, port, timeout=0.35) for port in ports],
        }
        host_result["open_ports"] = [p["port"] for p in host_result["ports"] if p["ok"]]
        hosts[host] = host_result

    return {
        "ok": any(item["ping"].get("ok") or item["open_ports"] for item in hosts.values()),
        "xt16_host": XT16_HOST,
        "servo_arm_host": SERVO_ARM_HOST,
        "hosts": hosts,
    }


def remote_udp_listener(duration_s: int = 4) -> dict[str, Any]:
    ports = [2368, 2369, 10110, 8308, 10001]
    ports_literal = ",".join(str(port) for port in ports)
    command = f"""
python3 - <<'PY'
import socket, select, time
ports=[{ports_literal}]
socks=[]
for p in ports:
    try:
        s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', p))
        socks.append((p,s))
        print(f'LISTEN_UDP {{p}} OK')
    except Exception as e:
        print(f'LISTEN_UDP {{p}} FAIL {{e}}')
deadline=time.time()+{duration_s}
counts={{p:0 for p,_ in socks}}
last={{}}
while time.time()<deadline and socks:
    readable,_,_=select.select([s for _,s in socks],[],[],0.5)
    for s in readable:
        p=next(p for p,ss in socks if ss is s)
        data,addr=s.recvfrom(65535)
        counts[p]+=1
        last[p]=(addr[0], addr[1], len(data), data[:8].hex())
for p in ports:
    print('UDP_RESULT', p, counts.get(p,0), last.get(p))
PY
"""
    result = run_robot_shell(command, timeout=duration_s + 6)
    text = result.get("stdout", "")
    udp_results: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.startswith("UDP_RESULT "):
            continue
        parts = line.split(" ", 3)
        if len(parts) >= 3:
            udp_results[parts[1]] = {"count": int(parts[2]), "last": parts[3] if len(parts) > 3 else None}

    result["udp_results"] = udp_results
    result["xt16_packets_seen"] = udp_results.get("2368", {}).get("count", 0) > 0 or udp_results.get("10110", {}).get("count", 0) > 0
    result["ok"] = bool(result.get("ok")) and result["xt16_packets_seen"]
    return result


def xt16_lidar_frame(duration_s: float = 0.45, max_packets: int = 80) -> dict[str, Any]:
    if GO2_LOCAL:
        return LIDAR_CACHE.frame()

    # Hesai PandarXT-16: 568-byte UDP payload, pre-header EE FF 06 01,
    # 8 blocks per packet, 16 channels per block, distance unit in header.
    command = f"""
python3 - <<'PY'
import json, socket, select, time
duration={float(duration_s)}
max_packets={int(max_packets)}
sock=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('0.0.0.0', 2368))
sock.setblocking(False)
deadline=time.time()+duration
points=[]
packets=0
sources={{}}
total_seen=0
while time.time()<deadline and packets<max_packets:
    r,_,_=select.select([sock], [], [], 0.05)
    if not r:
        continue
    data, addr=sock.recvfrom(2048)
    packets += 1
    sources[addr[0]] = sources.get(addr[0], 0) + 1
    if len(data) != 568 or data[:4] != b'\\xee\\xff\\x06\\x01':
        continue
    channel_num=data[6] if data[6] else 16
    block_num=data[7] if data[7] else 8
    distance_unit=(data[9] if len(data)>9 and data[9] else 4) / 1000.0
    body=12
    for block_idx in range(min(block_num, 8)):
        off=body + block_idx*66
        if off + 66 > len(data):
            break
        az=int.from_bytes(data[off:off+2], 'little') / 100.0
        for ch in range(min(channel_num, 16)):
            pos=off + 2 + ch*4
            dist=int.from_bytes(data[pos:pos+2], 'little') * distance_unit
            refl=data[pos+2]
            if 0.05 <= dist <= 120:
                total_seen += 1
                points.append([round(az, 2), round(dist, 3), int(refl), ch])
            if len(points) >= 1400:
                break
        if len(points) >= 1400:
            break
sock.close()
dists=[p[1] for p in points]
per_channel={{str(i):0 for i in range(16)}}
for p in points:
    per_channel[str(p[3])] += 1
stats={{
    'visible_points': len(points),
    'total_points_analyzed': total_seen,
    'min_m': round(min(dists), 3) if dists else None,
    'max_m': round(max(dists), 3) if dists else None,
    'avg_m': round(sum(dists)/len(dists), 3) if dists else None,
    'per_channel': per_channel,
}}
print(json.dumps({{'ok': bool(points), 'packets': packets, 'sources': sources, 'points': points, 'stats': stats}}))
PY
"""
    result = run_robot_shell(command, timeout=duration_s + 5)
    try:
        payload = json.loads(result.get("stdout", "{}"))
    except Exception:
        payload = {"ok": False, "error": "failed to parse lidar JSON", "raw": result.get("stdout", "")}
    payload["host"] = XT16_HOST
    payload["port"] = 2368
    return payload


def sport_mode_info() -> dict[str, Any]:
    sdk_path = PROJECT_ROOT / "unitree_sdk2_python" / "unitree_sdk2py" / "go2" / "sport" / "sport_client.py"
    return {
        "ok": sdk_path.exists(),
        "service": "sport",
        "transport": "Unitree SDK2 DDS/RPC (not a fixed TCP command port)",
        "domain": GO2_DDS_DOMAIN,
        "interface": GO2_DDS_INTERFACE,
        "safe_note": "Dashboard is read-only: it does not call Move/StandUp/StopMove.",
        "common_apis": {
            "StopMove": 1003,
            "StandUp": 1004,
            "StandDown": 1005,
            "RecoveryStand": 1006,
            "Move(vx, vy, vyaw)": 1008,
            "BalanceStand": 1002,
        },
    }


def command_stack_status() -> dict[str, Any]:
    try:
        import importlib.util

        modules = {}
        for name in ("cyclonedds", "cyclonedds.idl", "unitree_sdk2py"):
            try:
                spec = importlib.util.find_spec(name)
                modules[name] = {"ok": bool(spec), "origin": None if spec is None else spec.origin}
            except Exception as exc:
                modules[name] = {"ok": False, "error": repr(exc)}
        sdk_ok = modules.get("cyclonedds", {}).get("ok") and modules.get("unitree_sdk2py", {}).get("ok")
        return {
            "ok": bool(sdk_ok),
            "real_arm_enabled": os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"},
            "dds_domain": GO2_DDS_DOMAIN,
            "dds_interface": GO2_DDS_INTERFACE or "default",
            "d1_helper": str(PROJECT_ROOT / "bin" / "d1_arm_command"),
            "d1_helper_ok": (PROJECT_ROOT / "bin" / "d1_arm_command").exists(),
            "modules": modules,
            "safety": "Real arm execution requires GO2_ENABLE_REAL_ARM=1 and a valid IK plan.",
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def _run_d1_messages(messages: list[dict[str, Any]], delay_ms: int = 900) -> dict[str, Any]:
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.exists():
        return {"ok": False, "reason": f"D1 DDS helper missing: {helper}"}
    stdin = "\n".join(json.dumps(msg, separators=(",", ":")) for msg in messages) + "\n"
    result = subprocess.run(
        [str(helper), str(GO2_DDS_DOMAIN), str(delay_ms)],
        cwd=str(PROJECT_ROOT),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=max(12.0, (delay_ms / 1000.0 + 0.4) * len(messages)),
    )
    return {
        "ok": result.returncode == 0,
        "topic": "rt/arm_Command",
        "messages": messages,
        "helper_returncode": result.returncode,
        "helper_stdout": result.stdout[-4000:],
        "helper_stderr": result.stderr[-2000:],
    }


def _read_d1_servo_angles() -> list[float] | None:
    helper = PROJECT_ROOT / "d1_arm_feedback_helper"
    if not helper.exists():
        return None
    try:
        result = subprocess.run(
            [str(helper), str(GO2_DDS_DOMAIN), "2"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=4,
        )
    except Exception:
        return None
    latest = None
    for line in result.stdout.splitlines():
        if line.startswith("servo_angles "):
            parts = line.split()[1:]
            if len(parts) >= 7:
                try:
                    latest = [float(v) for v in parts[:7]]
                except ValueError:
                    latest = None
    return latest


def _interpolate_angles(start: list[float], target: list[float]) -> list[list[float]]:
    deltas = [abs(t - s) / step for s, t, step in zip(start, target, D1_MAX_STEP_DEG)]
    count = max(1, int(math.ceil(max(deltas, default=1.0))))

    def smoothstep(u: float) -> float:
        u = max(0.0, min(1.0, u))
        return u * u * (3.0 - 2.0 * u)

    return [
        [round(s + (t - s) * smoothstep(idx / count), 3) for s, t in zip(start, target)]
        for idx in range(1, count + 1)
    ]


def _stage_messages(stages: list[dict[str, Any]], *, close_gripper: bool) -> tuple[list[dict[str, Any]], list[str]]:
    messages = []
    sent = []
    seq = int(time.time()) % 100000
    messages.append({"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}})
    current = _read_d1_servo_angles()
    if current is None:
        raise RuntimeError("No D1 servo feedback; refusing motion without current arm pose")
    for offset, stage in enumerate(stages, start=1):
        joints = [float(v) for v in stage.get("joints_rad", [])]
        if len(joints) < 6:
            raise ValueError(f"Invalid joints in stage {stage.get('stage')}")
        target = [round(max(-135.0, min(135.0, math.degrees(v))), 3) for v in joints[:6]]
        if close_gripper:
            target.append(56.0 if stage.get("stage") in {"pre_grasp", "approach"} else 5.0)
        else:
            target.append(56.0)
        path = [target] if current is None else _interpolate_angles(current, target)
        for point in path:
            angles = {f"angle{idx}": point[idx] for idx in range(7)}
            angles["mode"] = 1
            messages.append({"seq": seq + len(messages), "address": 1, "funcode": 2, "data": angles})
        current = target
        sent.append(str(stage.get("stage")))
    return messages, sent


def _offset_stage(stage: dict[str, Any], name: str, offsets: list[float]) -> dict[str, Any]:
    out = dict(stage)
    joints = [float(v) for v in stage.get("joints_rad", [])]
    for idx, delta in enumerate(offsets):
        if idx < len(joints):
            joints[idx] += delta
    out["stage"] = name
    out["joints_rad"] = [round(float(v), 4) for v in joints]
    return out


def _limit_search_stage_to_current(stage: dict[str, Any], current_deg: list[float] | None) -> dict[str, Any]:
    if current_deg is None:
        return stage
    out = dict(stage)
    joints = [float(v) for v in stage.get("joints_rad", [])]
    target_deg = [math.degrees(v) for v in joints[:6]]
    # The shoulder/pitch pair is the dangerous part near the floor. During visual search,
    # advance it only a little from the measured current pose.
    target_deg[1] = max(current_deg[1] - 3.0, min(current_deg[1] + 6.0, target_deg[1]))
    target_deg[2] = max(current_deg[2] - 10.0, min(current_deg[2] + 10.0, target_deg[2]))
    out["joints_rad"] = [round(math.radians(v), 4) for v in target_deg]
    return out


def _front_camera_scan_hints(front_plan: dict[str, Any]) -> dict[str, Any]:
    """
    Map front RGB detections to coarse joint trims so the arm proportionally follows where the dog sees the box.
    Image coords: y grows downward — tags sitting lower in the frame usually need a stronger downward wrist tilt.
    """
    tags = (front_plan.get("tags") or {}).get("tags") or []
    poses = (front_plan.get("poses") or {}).get("poses") or []
    cx, cy = 320.0, 240.0
    w, h = 640.0, 480.0
    if not tags:
        return {"yaw_deg": 0.0, "wrist_trim_deg": 0.0, "shoulder_trim_deg": 0.0, "elbow_trim_deg": 0.0}
    centers = [tag.get("center_px", [cx, cy]) for tag in tags]
    mean_x = sum(float(c[0]) for c in centers) / len(centers)
    mean_y = sum(float(c[1]) for c in centers) / len(centers)
    nx = (mean_x - cx) / (w / 2)
    ny = (mean_y - cy) / (h / 2)
    # Horizontal: steer base yaw so the arm aligns laterally with the cluster (fine tuning reserved for wrist lock).
    yaw_deg = max(-22.0, min(22.0, nx * 24.0))
    # Vertical: tag lower in frame → pitch wrist further down (more negative servo angle here).
    wrist_trim_deg = max(-18.0, min(18.0, -ny * 20.0))
    shoulder_trim_deg = max(-8.0, min(8.0, -ny * 5.5))
    elbow_trim_deg = 0.0
    ranges = [float(p.get("range_m", 0.75)) for p in poses if p.get("range_m") is not None]
    if ranges:
        nearest = min(ranges)
        # Rough coupling: farther tags → slightly extend elbow; nearer → tuck slightly (within tight bounds).
        elbow_trim_deg = max(-10.0, min(10.0, (nearest - 0.72) * 38.0))
    return {
        "yaw_deg": yaw_deg,
        "wrist_trim_deg": wrist_trim_deg,
        "shoulder_trim_deg": shoulder_trim_deg,
        "elbow_trim_deg": elbow_trim_deg,
        "nx": nx,
        "ny": ny,
        "nearest_range_m": min(ranges) if ranges else None,
    }


def _manual_overhead_search_stages(
    front_plan: dict[str, Any],
    current_deg: list[float],
    cycle: int,
    *,
    hints: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """
    Search pose for wrist camera: converge smoothly toward a nominal look-down configuration instead of cycling
    alternating yaw targets (which felt like drop/recover). Front-camera hints shift trims each cycle.
    """
    hints = hints if hints is not None else _front_camera_scan_hints(front_plan)
    settle = min(1.0, 0.14 + cycle * 0.17)

    shoulder_nom = D1_SEARCH_SHOULDER_NOM_DEG + hints["shoulder_trim_deg"]
    elbow_nom = D1_SEARCH_ELBOW_NOM_DEG + hints["elbow_trim_deg"]
    wrist_nom = D1_SEARCH_WRIST_NOM_DEG + hints["wrist_trim_deg"]

    shoulder_tgt = current_deg[1] + settle * (shoulder_nom - current_deg[1])
    elbow_tgt = current_deg[2] + settle * (elbow_nom - current_deg[2])
    wrist_tgt = current_deg[4] + settle * (wrist_nom - current_deg[4])

    # Safety envelope while scanning (keep wrist pitched down into [-86,-44] deg band).
    shoulder_tgt = max(-88.0, min(-28.0, shoulder_tgt))
    elbow_tgt = max(12.0, min(88.0, elbow_tgt))
    wrist_tgt = max(-88.0, min(-44.0, wrist_tgt))

    yaw = max(-24.0, min(24.0, hints["yaw_deg"]))
    target_deg = [yaw, shoulder_tgt, elbow_tgt, current_deg[3], wrist_tgt, current_deg[5]]
    return [{
        "stage": f"overhead_wrist_search_{cycle}",
        "joints_rad": [round(math.radians(v), 4) for v in target_deg],
    }]


def publish_d1_arm_search(front_plan: dict[str, Any], cycle: int = 0) -> dict[str, Any]:
    current_deg = _read_d1_servo_angles()
    if current_deg is None:
        return {"ok": False, "attempted_motion": False, "reason": "No D1 servo feedback; refusing wrist search"}
    if not front_plan.get("ok"):
        return {"ok": False, "attempted_motion": False, "reason": "No valid front-camera coarse plan for wrist search"}
    hints = _front_camera_scan_hints(front_plan)
    stages = _manual_overhead_search_stages(front_plan, current_deg, cycle, hints=hints)
    try:
        messages, sent = _stage_messages(stages, close_gripper=False)
        result = _run_d1_messages(messages, delay_ms=D1_SEARCH_COMMAND_DELAY_MS)
        return {
            **result,
            "attempted_motion": bool(result.get("ok")),
            "mode": "wrist_camera_search",
            "sent_stages": sent,
            "source_camera": front_plan.get("camera_device"),
            "cycle": cycle,
            "scan_hints": hints,
            "search_delay_ms": D1_SEARCH_COMMAND_DELAY_MS,
        }
    except Exception as exc:
        return {"ok": False, "attempted_motion": False, "reason": repr(exc)}


def publish_d1_arm_plan(plan_payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": False, "attempted_motion": False, "reason": "GO2_ENABLE_REAL_ARM is not enabled"}

    selected = plan_payload.get("selected") or {}
    preview = selected.get("preview") or {}
    stages = preview.get("plan") or []
    if not plan_payload.get("ok") or not preview.get("ok") or not stages:
        return {"ok": False, "attempted_motion": False, "reason": "No valid IK plan to execute"}

    try:
        messages, sent = _stage_messages(stages, close_gripper=True)
        result = _run_d1_messages(messages, delay_ms=350)
        return {
            **result,
            "attempted_motion": bool(result.get("ok")),
            "sent_stages": sent if result.get("ok") else [],
            "selected_camera": plan_payload.get("selected_camera"),
        }
    except Exception as exc:
        return {"ok": False, "attempted_motion": False, "reason": repr(exc), "stack": command_stack_status()}


def _box_plan_snapshot() -> dict[str, Any]:
    return json.loads(api_box_plan().get_data(as_text=True))


def _camera_candidate(plan: dict[str, Any], device: int) -> dict[str, Any]:
    return (plan.get("candidates") or {}).get(str(device)) or {}


def _wrist_has_lock(plan: dict[str, Any]) -> bool:
    wrist = _camera_candidate(plan, 0)
    return bool(wrist.get("ok") and (wrist.get("tags") or {}).get("tags"))


def _wait_for_visible_plan(wait_s: float = 15.0) -> dict[str, Any]:
    deadline = time.time() + wait_s
    last = _box_plan_snapshot()
    while time.time() < deadline:
        plan = _box_plan_snapshot()
        if _wrist_has_lock(plan) or _camera_candidate(plan, 6).get("ok"):
            return plan
        last = plan
        time.sleep(0.35)
    return last


def run_wrist_guided_grasp_loop(max_cycles: int = D1_SEARCH_MAX_CYCLES) -> dict[str, Any]:
    log = []
    first_plan = _wait_for_visible_plan()
    last_front_plan = _camera_candidate(first_plan, 6) if _camera_candidate(first_plan, 6).get("ok") else None

    for cycle in range(max_cycles):
        plan = _box_plan_snapshot()
        wrist_plan = _camera_candidate(plan, 0)
        front_plan = _camera_candidate(plan, 6)
        if front_plan.get("ok"):
            last_front_plan = front_plan
        log.append({
            "cycle": cycle,
            "wrist_ok": bool(wrist_plan.get("ok")),
            "front_ok": bool(front_plan.get("ok")),
            "front_memory_ok": bool(last_front_plan),
            "selected_camera": plan.get("selected_camera"),
        })

        if _wrist_has_lock(plan):
            # Require the wrist camera to see the target twice, so we do not close on a single noisy frame.
            time.sleep(0.35)
            confirm = _box_plan_snapshot()
            if _wrist_has_lock(confirm):
                execution = publish_d1_arm_plan({
                    **confirm,
                    "ok": True,
                    "selected_camera": 0,
                    "selected": _camera_candidate(confirm, 0),
                })
                return {
                    **execution,
                    "grasp_policy": "continuous_wrist_lock",
                    "cycles": log,
                    "final_plan": confirm,
                    "dry_run_plan": first_plan,
                }
            log[-1]["wrist_confirm_lost"] = True

        if last_front_plan:
            search = publish_d1_arm_search(last_front_plan, cycle=cycle)
            log[-1]["search_execution"] = {
                "ok": search.get("ok"),
                "attempted_motion": search.get("attempted_motion"),
                "sent_stages": search.get("sent_stages"),
                "cycle": search.get("cycle"),
                "helper_returncode": search.get("helper_returncode"),
            }
            time.sleep(0.65)
        else:
            time.sleep(0.45)

    final_plan = _box_plan_snapshot()
    return {
        "ok": False,
        "attempted_motion": any((entry.get("search_execution") or {}).get("attempted_motion") for entry in log),
        "grasp_policy": "continuous_wrist_search_no_lock",
        "reason": "Search cycles completed, but wrist camera /dev/video0 never got a stable AprilTag lock; gripper was not closed.",
        "cycles": log,
        "final_plan": final_plan,
        "dry_run_plan": first_plan,
    }


def dds_lowstate_probe(duration_s: float = 4.0) -> dict[str, Any]:
    sys.path.insert(0, str(PROJECT_ROOT / "unitree_sdk2_python"))
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
    except Exception as exc:
        return {"ok": False, "available": False, "error": f"import failed: {exc!r}"}

    seen = {"count": 0, "first_quaternion": None}

    def callback(msg: Any) -> None:
        seen["count"] += 1
        if seen["first_quaternion"] is None:
            seen["first_quaternion"] = list(msg.imu_state.quaternion)

    try:
        if GO2_DDS_INTERFACE:
            ChannelFactoryInitialize(GO2_DDS_DOMAIN, GO2_DDS_INTERFACE)
        else:
            ChannelFactoryInitialize(GO2_DDS_DOMAIN)
        subscriber = ChannelSubscriber("rt/lowstate", LowState_)
        subscriber.Init(callback, 10)
        deadline = time.time() + duration_s
        while time.time() < deadline and seen["count"] == 0:
            time.sleep(0.1)
        return {
            "ok": seen["count"] > 0,
            "available": True,
            "domain": GO2_DDS_DOMAIN,
            "interface": GO2_DDS_INTERFACE,
            "topic": "rt/lowstate",
            **seen,
        }
    except Exception as exc:
        return {"ok": False, "available": True, "error": repr(exc)}


def run_all_tests() -> dict[str, Any]:
    tests: dict[str, Any] = {}
    tests["network_robot_ping"] = ping_host(GO2_HOST)
    tests["robot_ports"] = {
        "ok": False,
        "ports": [tcp_port(GO2_HOST, port) for port in (22, 80, 8080, 8081, 8888)],
    }
    tests["robot_ports"]["ok"] = any(p["ok"] for p in tests["robot_ports"]["ports"])
    tests["robot_ssh_inventory"] = remote_robot_inventory()
    tests["ethernet_devices"] = ethernet_device_scan()
    tests["xt16_lidar_udp_from_robot"] = remote_udp_listener()
    tests["sport_mode_api"] = sport_mode_info()
    tests["arm_command_stack"] = command_stack_status()
    tests["dds_lowstate"] = dds_lowstate_probe()

    summary_bits = []
    if tests["network_robot_ping"].get("ok"):
        summary_bits.append("Go2 reachable")
    if tests["robot_ssh_inventory"].get("detected", {}).get("realsense"):
        summary_bits.append("RealSense on robot")
    if tests["robot_ssh_inventory"].get("detected", {}).get("webcam"):
        summary_bits.append("USB webcam on robot")
    if tests["xt16_lidar_udp_from_robot"].get("xt16_packets_seen"):
        summary_bits.append(f"XT-16 LiDAR UDP active ({XT16_HOST})")
    else:
        summary_bits.append("XT-16 LiDAR UDP not seen")
    servo_ports = tests["ethernet_devices"].get("hosts", {}).get(SERVO_ARM_HOST, {}).get("open_ports", [])
    if servo_ports:
        summary_bits.append(f"Servo/Ethernet candidate {SERVO_ARM_HOST} ports {servo_ports}")
    else:
        summary_bits.append("Servo/Ethernet candidate not open")
    summary_bits.append("Sport Mode via DDS service 'sport'")

    return {
        "updated_at": now_iso(),
        "running": False,
        "summary": " | ".join(summary_bits),
        "tests": tests,
    }


def set_status(new_status: dict[str, Any]) -> None:
    with STATUS_LOCK:
        STATUS.clear()
        STATUS.update(new_status)


def get_status() -> dict[str, Any]:
    with STATUS_LOCK:
        return json.loads(json.dumps(STATUS, default=str))


def frame_from_camera(device: int) -> Any | None:
    if cv2 is None:
        return None
    jpg = robot_camera_jpeg(device)
    if jpg is None:
        return None
    import numpy as np

    arr = np.frombuffer(jpg, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def background_run() -> None:
    with STATUS_LOCK:
        STATUS["running"] = True
        STATUS["summary"] = "Diagnostics running..."
    try:
        set_status(run_all_tests())
    except Exception as exc:
        set_status({
            "updated_at": now_iso(),
            "running": False,
            "summary": f"Diagnostics failed: {exc!r}",
            "tests": {},
        })


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Go2 Diagnostics Dashboard</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, Segoe UI, Arial, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: radial-gradient(circle at top left, #172554, #020617 48%); color: #e5e7eb; }
    header { padding: 22px 26px; background: rgba(2,6,23,.72); border-bottom: 1px solid #334155; backdrop-filter: blur(10px); position: sticky; top: 0; z-index: 2; }
    h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: .2px; }
    .sub { color: #bfdbfe; }
    main { padding: 22px; display: grid; gap: 18px; }
    button { background: #2563eb; color: white; border: 0; border-radius: 10px; padding: 10px 14px; cursor: pointer; font-weight: 700; }
    button:hover { background: #1d4ed8; }
    .layout { display: grid; grid-template-columns: minmax(360px, 1.2fr) minmax(340px, .8fr); gap: 18px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; }
    .card { background: rgba(15,23,42,.9); border: 1px solid #334155; border-radius: 18px; padding: 16px; box-shadow: 0 18px 40px rgba(0,0,0,.24); }
    .card h2 { margin: 0 0 12px; font-size: 17px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .ok { color: #34d399; }
    .bad { color: #fb7185; }
    .warn { color: #fbbf24; }
    .muted { color: #94a3b8; }
    .pill { display: inline-block; padding: 4px 8px; border-radius: 999px; background: #1e293b; margin: 2px; font-size: 12px; }
    .metric { font-size: 28px; font-weight: 800; margin: 6px 0; }
    .small { color: #9ca3af; font-size: 13px; }
    pre { white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow: auto; background: #020617; padding: 10px; border-radius: 12px; border: 1px solid #1e293b; font-size: 12px; }
    canvas { width: 100%; height: 420px; background: radial-gradient(circle, #0f172a, #020617); border: 1px solid #334155; border-radius: 16px; }
    .cams { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 12px; }
    .cam { background: #020617; border: 1px solid #334155; border-radius: 14px; overflow: hidden; min-height: 150px; }
    .cam img { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; background: #020617; }
    .cam span { display: block; padding: 8px 10px; color: #cbd5e1; font-size: 12px; }
    @media (max-width: 980px) { .layout { grid-template-columns: 1fr; } .cams { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Go2 Diagnostics Dashboard</h1>
    <div class="sub">Robot {{ go2_host }} | XT-16 {{ xt16_host }} | Servo arm {{ servo_arm_host }} | Sport Mode: DDS service <b>sport</b></div>
  </header>
  <main>
    <section class="card">
      <button onclick="runAll()">Run All Tests</button>
      <span id="summary" class="small">Loading...</span>
    </section>
    <section class="layout">
      <article class="card">
        <h2>LiDAR XT-16 Live <span id="lidarStatus" class="warn">waiting</span></h2>
        <canvas id="lidarCanvas" width="900" height="620"></canvas>
        <div id="lidarMeta" class="small">Listening on robot UDP 2368...</div>
      </article>
      <article class="card">
        <h2>Live Cameras From Robot <span id="cameraStatus" class="warn">warming</span></h2>
        <div class="cams">
          <div class="cam"><img id="cam0" src="/api/robot/camera/0.jpg"><span id="cam0Status">/dev/video0 - Sonix HD 1080P PC-Camera (arm/external USB)</span></div>
          <div class="cam"><img id="cam6" src="/api/robot/camera/6.jpg"><span id="cam6Status">/dev/video6 - Intel RealSense D435i RGB stream</span></div>
        </div>
      </article>
    </section>
    <section class="layout">
      <article class="card">
        <h2>AprilTag Box Search <span id="boxStatus" class="warn">dry-run</span></h2>
        <div class="cams">
          <div class="cam"><img id="boxAnnotated0" src="/api/box/annotated/0.jpg"><span>Wrist/arm camera /dev/video0, tag25h9 IDs 0..3</span></div>
          <div class="cam"><img id="boxAnnotated6" src="/api/box/annotated/6.jpg"><span>Front RealSense RGB /dev/video6, tag25h9 IDs 0..3</span></div>
        </div>
      </article>
      <article class="card">
        <h2>IK / Gripper Preview <span class="small">dual camera, no real motion</span></h2>
        <button onclick="attemptGrasp()">Attempt Grasp (guarded)</button>
        <pre id="boxPlan">Loading dry-run plan...</pre>
      </article>
    </section>
    <section id="cards" class="grid"></section>
  </main>
  <script>
    function statusClass(ok) { return ok ? 'ok' : 'bad'; }
    function setText(id, value) { const el = document.getElementById(id); if (el) el.textContent = value; }
    function renderCard(name, data) {
      const ok = data && data.ok;
      const title = name.replaceAll('_', ' ');
      let details = '';
      if (data && data.detected) {
        details += Object.entries(data.detected).map(([k, v]) => `<span class="pill ${v ? 'ok' : 'warn'}">${k}: ${v}</span>`).join('');
      }
      if (data && data.ports) {
        details += '<div>' + data.ports.map(p => `<span class="pill ${p.ok ? 'ok' : 'bad'}">${p.host}:${p.port}</span>`).join('') + '</div>';
      }
      if (data && data.common_apis) {
        details += '<div>' + Object.entries(data.common_apis).map(([k,v]) => `<span class="pill ok">${k}: ${v}</span>`).join('') + '</div>';
      }
      return `<article class="card">
        <h2>${title} <span class="${statusClass(ok)}">${ok ? 'OK' : 'FAIL/WARN'}</span></h2>
        ${details}
        <pre>${JSON.stringify(data, null, 2)}</pre>
      </article>`;
    }
    async function loadStatus() {
      const res = await fetch('/api/status');
      const data = await res.json();
      document.getElementById('summary').textContent = `${data.running ? 'Running...' : 'Updated ' + data.updated_at}: ${data.summary}`;
      const tests = data.tests || {};
      document.getElementById('cards').innerHTML = Object.entries(tests).map(([name, value]) => renderCard(name, value)).join('');
    }
    async function runAll() {
      await fetch('/api/run/all', { method: 'POST' });
      await loadStatus();
    }
    function refreshCameras() {
      const t = Date.now();
      for (const dev of [0, 6]) {
        const img = document.getElementById(`cam${dev}`);
        if (img) img.src = `/api/robot/camera/${dev}.jpg?t=${t}`;
      }
      const img0 = document.getElementById('boxAnnotated0');
      const img6 = document.getElementById('boxAnnotated6');
      if (img0) img0.src = `/api/box/annotated/0.jpg?t=${t}`;
      if (img6) img6.src = `/api/box/annotated/6.jpg?t=${t}`;
    }
    async function refreshCameraStatus() {
      try {
        const res = await fetch('/api/cameras/status');
        const data = await res.json();
        let allOk = true;
        for (const dev of [0, 6]) {
          const c = (data.cameras || {})[String(dev)] || {};
          allOk = allOk && !!c.available;
          setText(`cam${dev}Status`, `/dev/video${dev} - ${c.label || 'camera'} | ${c.available ? 'OK' : 'warming'} | age=${c.age_ms ?? '-'}ms${c.error ? ' | ' + c.error : ''}`);
        }
        setText('cameraStatus', allOk ? 'streaming' : 'warming');
        document.getElementById('cameraStatus').className = allOk ? 'ok' : 'warn';
      } catch (e) {
        setText('cameraStatus', 'error');
        document.getElementById('cameraStatus').className = 'bad';
      }
    }
    async function refreshBoxPlan() {
      try {
        const res = await fetch('/api/box/plan');
        const data = await res.json();
        document.getElementById('boxPlan').textContent = JSON.stringify(data, null, 2);
        setText('boxStatus', data.ok ? 'target planned' : 'searching');
        document.getElementById('boxStatus').className = data.ok ? 'ok' : 'warn';
      } catch (e) {
        setText('boxStatus', 'error');
        document.getElementById('boxStatus').className = 'bad';
      }
    }
    async function attemptGrasp() {
      const res = await fetch('/api/arm/grasp_box/attempt', { method: 'POST' });
      const data = await res.json();
      document.getElementById('boxPlan').textContent = JSON.stringify(data, null, 2);
      setText('boxStatus', data.attempted_motion ? 'motion sent' : 'blocked');
      document.getElementById('boxStatus').className = data.attempted_motion ? 'ok' : 'warn';
    }
    function drawLidar(points) {
      const canvas = document.getElementById('lidarCanvas');
      const ctx = canvas.getContext('2d');
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0,0,w,h);
      const cx = w/2, cy = h/2;
      const maxR = Math.min(w,h)*0.46;
      ctx.strokeStyle = '#1e3a8a'; ctx.lineWidth = 1;
      for (let r=0.2; r<=1; r+=0.2) { ctx.beginPath(); ctx.arc(cx,cy,maxR*r,0,Math.PI*2); ctx.stroke(); }
      for (let a=0; a<360; a+=30) {
        const rad=(a-90)*Math.PI/180;
        ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx+Math.cos(rad)*maxR, cy+Math.sin(rad)*maxR); ctx.stroke();
      }
      ctx.fillStyle = '#94a3b8'; ctx.fillRect(cx-4, cy-4, 8, 8);
      for (const p of points || []) {
        const az = p[0], dist = p[1], refl = p[2];
        const rad = (az - 90) * Math.PI / 180;
        const rr = Math.min(dist / 30, 1) * maxR;
        const x = cx + Math.cos(rad) * rr;
        const y = cy + Math.sin(rad) * rr;
        const hue = Math.min(160, 35 + refl * .6);
        ctx.fillStyle = `hsl(${hue}, 90%, 58%)`;
        ctx.fillRect(x, y, 2.2, 2.2);
      }
    }
    async function refreshLidar() {
      try {
        const res = await fetch('/api/lidar/frame');
        const data = await res.json();
        drawLidar(data.points || []);
        const stats = data.stats || {};
        setText('lidarStatus', data.ok ? 'streaming' : 'no points');
        document.getElementById('lidarStatus').className = data.ok ? 'ok' : 'bad';
        setText('lidarMeta', `packets=${data.packets || 0} visible=${stats.visible_points || 0} analyzed=${stats.total_points_analyzed || 0} range=${stats.min_m ?? '-'}..${stats.max_m ?? '-'}m avg=${stats.avg_m ?? '-'}m source=${JSON.stringify(data.sources || {})}`);
      } catch (e) {
        setText('lidarStatus', 'error');
        document.getElementById('lidarStatus').className = 'bad';
      }
    }
    loadStatus();
    refreshCameras();
    refreshCameraStatus();
    refreshLidar();
    refreshBoxPlan();
    setInterval(loadStatus, 5000);
    setInterval(refreshCameras, 450);
    setInterval(refreshCameraStatus, 1200);
    setInterval(refreshLidar, 900);
    setInterval(refreshBoxPlan, 1600);
  </script>
</body>
</html>
"""


@APP.route("/")
def index() -> Response:
    html = render_template_string(
        HTML,
        go2_host=GO2_HOST,
        xt16_host=XT16_HOST,
        servo_arm_host=SERVO_ARM_HOST,
    )
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})


@APP.route("/api/status")
def api_status() -> Any:
    return jsonify(get_status())


@APP.route("/api/lidar/frame")
def api_lidar_frame() -> Any:
    return jsonify(xt16_lidar_frame())


@APP.route("/api/robot/camera/<int:device>.jpg")
def api_robot_camera(device: int) -> Response:
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)
    image = robot_camera_jpeg(device)
    if image is None:
        return Response("camera frame unavailable", status=503)
    return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@APP.route("/stream/robot/camera/<int:device>.mjpg")
def stream_robot_camera(device: int) -> Response:
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)

    def generate():
        while True:
            image = robot_camera_jpeg(device)
            if image is not None:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + image + b"\r\n"
                )
            time.sleep(0.08 if GO2_LOCAL else 0.35)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@APP.route("/api/cameras/status")
def api_cameras_status() -> Any:
    if GO2_LOCAL:
        CAMERA_CACHE.start()
    return jsonify({"ok": True, "mode": "local-cache" if GO2_LOCAL else "ssh-snapshot", "cameras": CAMERA_CACHE.stats()})


@APP.route("/api/cameras/warmup", methods=["POST"])
def api_cameras_warmup() -> Any:
    warmup_realtime_feeds()
    return jsonify({"ok": True, "cameras": CAMERA_CACHE.stats()})


@APP.route("/api/box/plan")
def api_box_plan() -> Any:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from box_grasp_planner import plan_from_frame

        candidates = {}
        for device in (0, 6):
            frame = frame_from_camera(device)
            if frame is None:
                candidates[str(device)] = {
                    "ok": False,
                    "error": f"camera /dev/video{device} unavailable",
                    "camera_label": CAMERA_DEVICES.get(device, "unknown"),
                }
                continue
            result = plan_from_frame(frame)
            result["camera_device"] = device
            result["camera_label"] = CAMERA_DEVICES.get(device, "unknown")
            candidates[str(device)] = result

        def score(item: dict[str, Any]) -> tuple[int, int, float]:
            tags = item.get("tags", {}).get("tags", [])
            poses = item.get("poses", {}).get("poses", [])
            nearest = min([p.get("range_m", 999.0) for p in poses], default=999.0)
            return (1 if item.get("ok") else 0, len(tags), -nearest)

        selected_key = None
        if candidates:
            selected_key = max(candidates, key=lambda k: score(candidates[k]))
        selected = candidates.get(selected_key) if selected_key is not None else None
        ok = bool(selected and selected.get("ok"))
        return jsonify({
            "ok": ok,
            "mode": "dual-camera-fusion",
            "selected_camera": None if selected is None else int(selected_key),
            "selected": selected,
            "candidates": candidates,
            "real_motion_enabled": os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"},
            "command_stack": command_stack_status(),
            "note": "Both wrist/external camera (/dev/video0) and front RealSense RGB (/dev/video6) are searched for tag25h9 IDs 0..3.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": repr(exc), "mode": "dual-camera-fusion"})


@APP.route("/api/box/annotated.jpg")
@APP.route("/api/box/annotated/<int:device>.jpg")
def api_box_annotated(device: int = 6) -> Response:
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)
    frame = frame_from_camera(device)
    if frame is None or cv2 is None:
        return Response("camera frame unavailable", status=503)
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from box_grasp_planner import detect_box_tags, draw_tags

        out = draw_tags(frame, detect_box_tags(frame))
        ok, jpg = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 76])
        if not ok:
            return Response("encode failed", status=500)
        return Response(jpg.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return Response(repr(exc), status=500)


@APP.route("/api/arm/grasp_box/attempt", methods=["POST"])
def api_arm_grasp_box_attempt() -> Any:
    execution = run_wrist_guided_grasp_loop()
    execution["command_stack"] = command_stack_status()
    return jsonify(execution)


@APP.route("/api/run/all", methods=["POST"])
def api_run_all() -> Any:
    if get_status().get("running"):
        return jsonify({"ok": False, "message": "Diagnostics already running"}), 409
    thread = threading.Thread(target=background_run, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Diagnostics started"})


@APP.route("/api/test/<name>", methods=["GET", "POST"])
def api_test(name: str) -> Any:
    tests = {
        "network": lambda: {"robot_ping": ping_host(GO2_HOST)},
        "operator_pc_usb": local_usb_inventory,
        "operator_pc_webcam": probe_local_webcams,
        "robot_usb": remote_robot_inventory,
        "ethernet": ethernet_device_scan,
        "xt16_udp": remote_udp_listener,
        "xt16_frame": xt16_lidar_frame,
        "sport_mode": sport_mode_info,
        "arm_command_stack": command_stack_status,
        "camera_status": lambda: {"ok": True, "mode": "local-cache" if GO2_LOCAL else "ssh-snapshot", "cameras": CAMERA_CACHE.stats()},
        "box_plan": lambda: json.loads(api_box_plan().get_data(as_text=True)),
        "dds": dds_lowstate_probe,
    }
    if name not in tests:
        return jsonify({"ok": False, "error": f"Unknown test {name!r}"}), 404
    return jsonify(tests[name]())


if __name__ == "__main__":
    port = int(os.environ.get("GO2_DASHBOARD_PORT", "5050"))
    host = os.environ.get("GO2_DASHBOARD_HOST", "127.0.0.1")
    print(f"Starting Go2 diagnostics dashboard on http://{host}:{port}")
    print("All tests are read-only; no lowcmd is published.")
    warmup_realtime_feeds()
    set_status({
        "updated_at": now_iso(),
        "running": True,
        "summary": "Dashboard online; warming cameras and running diagnostics in background...",
        "tests": {},
    })
    threading.Thread(target=background_run, daemon=True).start()
    APP.run(host=host, port=port, debug=False, threaded=True)
