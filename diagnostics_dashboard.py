#!/usr/bin/env python3
"""
Diagnostics dashboard for a Unitree Go2 lab setup.

Most checks are read-only (ping, SSH inventory, cameras). Optional modes when
running on the robot: real arm DDS motion (`GO2_ENABLE_REAL_ARM`), Sport API
base poses Stand up / Crouch (`GO2_ENABLE_BASE_MOTION`, default 1 when `GO2_LOCAL=1`),
saved START alignment snapshots with `arm_at_start` (servo + `joints_rad` for IK). Grasp attempt: (optional) fold →
go to saved START → wait for AprilTag → … Wrist grasp: `GO2_GRASP_WRIST_POLICY` (default `center_then_grasp_on_loss`, IK from cached plan on tag loss; `legacy_double_lock` restores old behavior). Manual: `data/true_zero_pose.json` (Salva ZERO) and «ZERO → START» for a
smooth path from calibrated fold to `start_alignment.json`. D1 drag-teaching: see `docs/d1_arm_protocol_feasibility.md`; `POST /api/arm/teach_mode` is 501 until
the protocol is integrated. Experimental mirror/assist drag-follow loop: `scripts/d1_drag_follow_experimental.py`
and `POST /api/arm/drag_follow`. Sport RPC from `/api/base/accompany_mode` uses a thread timeout
(`GO2_SPORT_RPC_TIMEOUT_S`, default 45s) so the worker does not hang.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import platform
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template_string, request

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
# Sul Jetson: Sport Stand up / Crouch (e modalità avanzate nel modulo go2_accompany) abilitato di default.
if GO2_LOCAL:
    os.environ.setdefault("GO2_ENABLE_BASE_MOTION", "1")
# Sul NX serve bind su tutte le interfacce così il browser su LAN raggiunge il servizio.
GO2_DASHBOARD_BIND = os.environ.get(
    "GO2_DASHBOARD_HOST",
    "0.0.0.0" if GO2_LOCAL else "127.0.0.1",
)
XT16_HOST = os.environ.get("XT16_HOST", "192.168.123.20")
SERVO_ARM_HOST = os.environ.get("SERVO_ARM_HOST", "192.168.123.161")
# Snapshot memorizzato sul robot: scena AprilTag + piani camera al momento «START» (non odometria).
ALIGNMENT_START_PATH = PROJECT_ROOT / "data" / "start_alignment.json"
# Ripiegatura calibrata su NX (giunto2 ~82°, ecc.) — riferimento ZERO→START.
TRUE_ZERO_POSE_PATH = PROJECT_ROOT / "data" / "true_zero_pose.json"
# Pose braccio D1 (solo angoli servo letti da feedback — niente Sport).
ARM_POSE_SNAPSHOTS_PATH = PROJECT_ROOT / "data" / "arm_pose_snapshots.json"
ETHERNET_CANDIDATES = [
    host.strip()
    for host in os.environ.get("GO2_ETHERNET_CANDIDATES", f"{XT16_HOST},{SERVO_ARM_HOST},192.168.123.100").split(",")
    if host.strip()
]

APP = Flask(__name__)
# Riavvio: questo timestamp è all’import del modulo; utile vs mtime di diagnostics_dashboard.py su disco.
_DASHBOARD_SELF = Path(__file__).resolve()
PROCESS_STARTED_AT_EPOCH = time.time()
PROCESS_STARTED_AT = datetime.now().isoformat(timespec="seconds")


def _dashboard_py_mtime_epoch() -> float | None:
    try:
        return float(_DASHBOARD_SELF.stat().st_mtime)
    except OSError:
        return None


def _dashboard_py_mtime_iso() -> str | None:
    try:
        return datetime.fromtimestamp(_DASHBOARD_SELF.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


STATUS_LOCK = threading.Lock()
STATUS: dict[str, Any] = {
    "updated_at": None,
    "running": False,
    "summary": "No diagnostics run yet.",
    "tests": {},
}
# Process optional avviato da POST /api/arm/drag_follow (script sperimentale).
DRAG_FOLLOW_PROC: subprocess.Popen | None = None
# Meta quando drag_follow è avviato da questa istanza Flask (PID, durata, parametri).
DRAG_FOLLOW_META: dict[str, Any] | None = None
# Ultimo stop / uscita processo (feedback UI).
DRAG_FOLLOW_LAST_END: dict[str, Any] | None = None
# stdout/stderr dello script drag-follow (exit code, print ERROR…).
DRAG_FOLLOW_LOG_FP: Any = None
# Log mirror append-only (relativo a PROJECT_ROOT).
DRAG_FOLLOW_LOOP_LOG_RELPATH = "data/drag_follow_loop.log"
DRAG_FOLLOW_PROCESS_LOG_RELPATH = "data/drag_follow_process.log"
DRAG_FOLLOW_DIAG_JSONL_RELPATH = "data/drag_follow_diag.jsonl"


_RE_DRAG_LOOP_MAX_ERR = re.compile(r"max_err=([\d.]+)°@j(\d+)")
_RE_DRAG_LOOP_MEAN_ERR = re.compile(r"mean_err=([\d.]+)°")


def _tail_file_lines(path: Path, max_lines: int) -> tuple[list[str], str | None]:
    if max_lines < 1:
        return [], None
    if not path.is_file():
        return [], f"missing_file:{path.name}"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max_lines:], None
    except OSError as exc:
        return [], repr(exc)


def _analyze_drag_follow_process_lines(lines: list[str]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    hints: list[str] = []
    for ln in lines:
        s = ln.strip()
        if "ERROR:" in s or s.startswith("ERROR"):
            issues.append({"severity": "error", "source": "drag_follow_process.log", "detail": s[:800]})
        elif "WARN" in s and "WARN log-file" not in s:
            issues.append({"severity": "warn", "source": "drag_follow_process.log", "detail": s[:800]})
    if any("no servo_angles" in str(i.get("detail", "")).lower() for i in issues):
        hints.append(
            "Feedback DDS assente entro 15s — verifica `bin/d1_arm_feedback_helper`, dominio DDS (`GO2_DDS_DOMAIN`), cavo/arm acceso."
        )
    if any("missing bin/" in str(i.get("detail", "")) for i in issues):
        hints.append("Compila/deploy degli helper C++ (`bin/d1_arm_feedback_helper`, `bin/d1_arm_command`) sulla NX.")
    return {"issues": issues, "hints": hints}


def _analyze_drag_follow_loop_lines(lines: list[str]) -> dict[str, Any]:
    metrics: list[dict[str, Any]] = []
    hints: list[str] = []
    for ln in lines:
        m = _RE_DRAG_LOOP_MAX_ERR.search(ln)
        mm = _RE_DRAG_LOOP_MEAN_ERR.search(ln)
        if not m:
            continue
        rec: dict[str, Any] = {
            "max_err_deg": float(m.group(1)),
            "joint": int(m.group(2)),
            "line": ln.strip()[:240],
        }
        if mm:
            rec["mean_err_deg"] = float(mm.group(1))
        if "mirror" in ln:
            rec["mode_line"] = "mirror"
        elif "passthrough" in ln:
            rec["mode_line"] = "passthrough"
        elif "echo" in ln:
            rec["mode_line"] = "echo"
        metrics.append(rec)
    tail = metrics[-24:]
    if tail:
        w = max(tail, key=lambda x: x["max_err_deg"])
        if w["max_err_deg"] > 8.0:
            hints.append(
                f"Loop log: errore inseguimento fino a {w['max_err_deg']:.2f}° su j{w['joint']} — ritardi DDS, clipping mirror, o giunto che non segue il comando."
            )
        if w["joint"] in (0, 1, 2) and w["max_err_deg"] > 4:
            hints.append(
                "Spalla/gomito (j0–j2) con errore alto: tipico di giunti carichi + comando troppo lento o `clip_rate` mirror alto — prova ECHO o alza cap/step mirror."
            )
    mirror_clip_heavy = sum(
        1 for ln in lines[-160:] if "mirror" in ln and "clip_rate=" in ln
    )
    if mirror_clip_heavy >= 8:
        hints.append(
            "Molte righe mirror con clip_rate nel loop — aumentare mirror_max_step_deg / η oppure passare a ECHO."
        )
    return {"metrics_tail": tail, "hints": hints}


def _analyze_drag_follow_jsonl_lines(lines: list[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if r.get("event") != "tick_summary":
            continue
        rows.append(r)
    hints: list[str] = []
    if len(rows) < 2:
        return {
            "samples": len(rows),
            "per_joint_max_abs_dq_fb": None,
            "per_joint_max_abs_cmd_minus_fb": None,
            "hints": hints + (["Nessun campione JSONL — esegui almeno un drag dopo deploy (file data/drag_follow_diag.jsonl)."] if len(rows) == 0 else []),
        }
    dq_max = [0.0] * 7
    err_max = [0.0] * 7
    for r in rows:
        dq = r.get("dq_fb_deg")
        err = r.get("err_cmd_minus_fb_deg")
        if isinstance(dq, list) and len(dq) >= 7:
            for i in range(7):
                dq_max[i] = max(dq_max[i], abs(float(dq[i])))
        if isinstance(err, list) and len(err) >= 7:
            for i in range(7):
                err_max[i] = max(err_max[i], abs(float(err[i])))
    for i in range(7):
        if dq_max[i] < 0.015 and len(rows) >= 12:
            hints.append(
                f"JSONL: giunto {i} — Δfeedback quasi zero nel campione — DDS non aggiorna questo asse mentre muovi, oppure braccio non si muove davvero."
            )
        if err_max[i] > 1.25:
            hints.append(
                f"JSONL: giunto {i} — |cmd−fb| fino a {err_max[i]:.2f}° — quantizzazione comando vs lettura o latenza."
            )
    return {
        "samples": len(rows),
        "per_joint_max_abs_dq_fb": [round(x, 5) for x in dq_max],
        "per_joint_max_abs_cmd_minus_fb": [round(x, 5) for x in err_max],
        "last_sample_keys": sorted(rows[-1].keys()) if rows else [],
        "hints": hints,
    }


def _drag_follow_diagnostics_payload(*, lines_process: int, lines_loop: int, lines_jsonl: int, include_servo: bool) -> dict[str, Any]:
    _drag_follow_reap()
    proc_path = PROJECT_ROOT / DRAG_FOLLOW_PROCESS_LOG_RELPATH
    loop_path = PROJECT_ROOT / DRAG_FOLLOW_LOOP_LOG_RELPATH
    jsonl_path = PROJECT_ROOT / DRAG_FOLLOW_DIAG_JSONL_RELPATH
    pl, pe = _tail_file_lines(proc_path, lines_process)
    ll, le = _tail_file_lines(loop_path, lines_loop)
    jl, je = _tail_file_lines(jsonl_path, lines_jsonl)

    fb_bin = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
    cmd_bin = PROJECT_ROOT / "bin" / "d1_arm_command"
    bundle: dict[str, Any] = {
        "ok": True,
        "paths": {
            "process_log": str(proc_path),
            "loop_log": str(loop_path),
            "diag_jsonl": str(jsonl_path),
        },
        "binaries": {
            "d1_arm_feedback_helper": fb_bin.is_file(),
            "d1_arm_command": cmd_bin.is_file(),
        },
        "drag_follow_running": DRAG_FOLLOW_PROC is not None and DRAG_FOLLOW_PROC.poll() is None,
        "meta": DRAG_FOLLOW_META,
        "last_end": DRAG_FOLLOW_LAST_END,
        "read_errors": {"process": pe, "loop": le, "jsonl": je},
        "process_log_tail": "\n".join(pl),
        "loop_log_tail": "\n".join(ll),
        "jsonl_tail_raw": "\n".join(jl[-min(len(jl), 40) :]) if jl else "",
        "analysis": {
            "process": _analyze_drag_follow_process_lines(pl),
            "loop": _analyze_drag_follow_loop_lines(ll),
            "jsonl": _analyze_drag_follow_jsonl_lines(jl),
        },
    }
    merged_hints: list[str] = []
    merged_issues: list[dict[str, Any]] = []
    for part in bundle["analysis"].values():
        merged_hints.extend(part.get("hints") or [])
        merged_issues.extend(part.get("issues") or [])
    bundle["summary"] = {
        "hint_count": len(merged_hints),
        "issue_count": len(merged_issues),
        "hints": merged_hints[:24],
        "issues": merged_issues[:16],
    }
    if include_servo:
        snap = _read_d1_servo_angles()
        bundle["servo_deg_now"] = snap if snap is not None else None
        bundle["servo_feedback_ok"] = snap is not None
    return bundle


def _drag_follow_close_log_fp() -> None:
    global DRAG_FOLLOW_LOG_FP
    if DRAG_FOLLOW_LOG_FP is not None:
        try:
            DRAG_FOLLOW_LOG_FP.close()
        except Exception:
            pass
        DRAG_FOLLOW_LOG_FP = None


def _drag_follow_reap() -> None:
    """Se il subprocess è terminato, libera PID e meta."""
    global DRAG_FOLLOW_PROC, DRAG_FOLLOW_META, DRAG_FOLLOW_LAST_END
    if DRAG_FOLLOW_PROC is None:
        return
    if DRAG_FOLLOW_PROC.poll() is None:
        return
    code = DRAG_FOLLOW_PROC.returncode
    try:
        DRAG_FOLLOW_PROC.wait(timeout=0.05)
    except Exception:
        pass
    DRAG_FOLLOW_PROC = None
    DRAG_FOLLOW_META = None
    _drag_follow_close_log_fp()
    DRAG_FOLLOW_LAST_END = {
        "ended_at": time.time(),
        "reason": "process_exited",
        "exit_code": code,
    }


def _drag_follow_stop_if_running(*, hold_after_stop: bool = True) -> dict[str, Any]:
    """
    Termina il subprocess drag-follow avviato da questa istanza Flask.
    Se ``hold_after_stop`` e gli env consentono, invia un hold DDS (stesso comportamento di POST /api/arm/drag_follow con enable=false).
    """
    global DRAG_FOLLOW_PROC, DRAG_FOLLOW_META, DRAG_FOLLOW_LAST_END, DRAG_FOLLOW_LOG_FP
    _drag_follow_reap()
    if DRAG_FOLLOW_PROC is None:
        return {"drag_follow_stopped": False, "hold_after_stop": None}
    if DRAG_FOLLOW_PROC.poll() is not None:
        DRAG_FOLLOW_PROC = None
        DRAG_FOLLOW_META = None
        _drag_follow_close_log_fp()
        return {"drag_follow_stopped": False, "hold_after_stop": None}
    proc = DRAG_FOLLOW_PROC
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    DRAG_FOLLOW_PROC = None
    DRAG_FOLLOW_META = None
    _drag_follow_close_log_fp()
    DRAG_FOLLOW_LAST_END = {"ended_at": time.time(), "reason": "user_stop", "exit_code": None}
    hold_after: dict[str, Any] | None = None
    if (
        hold_after_stop
        and os.environ.get("GO2_DRAG_HOLD_AFTER_STOP", "1").lower() in {"1", "true", "yes"}
        and os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"}
    ):
        hold_after = publish_d1_hold_current()
    return {"drag_follow_stopped": True, "hold_after_stop": hold_after}


@APP.route("/api/health", methods=["GET"])
def api_health() -> Any:
    """Smoke test minimo — niente camere né DDS."""
    mt = _dashboard_py_mtime_epoch()
    reload_recommended = bool(mt is not None and mt > PROCESS_STARTED_AT_EPOCH)
    return jsonify(
        {
            "ok": True,
            "service": "go2_dashboard",
            "pid": os.getpid(),
            "process_started_at": PROCESS_STARTED_AT,
            "dashboard_py_mtime": _dashboard_py_mtime_iso(),
            "reload_recommended": reload_recommended,
            "reload_hint": (
                "Il file diagnostics_dashboard.py sul disco è più nuovo di questo processo: riavvia Flask "
                "(su NX: python scripts/deploy_dashboard_to_nx.py oppure kill PID e rilancia)."
                if reload_recommended
                else None
            ),
        }
    )

CAMERA_DEVICES = {
    0: "Sonix HD 1080P PC-Camera (arm/external USB)",
    6: "Intel RealSense D435i RGB stream",
}


def _v4l_index_for_logical_camera(logical: int) -> int:
    """
    Indice V4L2 reale (ls -l /dev/video*). Sul Jetson il RGB RealSense spesso non è video6:
    prova es. GO2_VIDEO_INDEX_6=4 o 2 dopo rs-enumerate-devices / test manuali.
    """
    key = f"GO2_VIDEO_INDEX_{logical}"
    if key in os.environ:
        try:
            return int(str(os.environ[key]).strip())
        except ValueError:
            pass
    return int(logical)


def _cv_videocapture(v4l_index: int) -> Any:
    """Apertura V4L2 esplicita su Linux — alcuni device RealSense non aprono col backend default."""
    if cv2 is None:
        raise RuntimeError("cv2 unavailable")
    if platform.system().lower() == "linux":
        try:
            cap = cv2.VideoCapture(v4l_index, cv2.CAP_V4L2)
            if cap.isOpened():
                return cap
            cap.release()
        except Exception:
            pass
    return cv2.VideoCapture(v4l_index)


def _parse_step_deg_list(raw: str | None, default: list[float]) -> list[float]:
    if not raw:
        return default[:]
    parts = [float(x.strip()) for x in raw.split(",")]
    return parts if len(parts) >= 7 else default[:]


# Search vs grasp use different step caps — grasp is slower/smoother by default.
_DEFAULT_SEARCH = [2.5, 1.2, 2.2, 3.5, 3.5, 4.5, 8.0]
_DEFAULT_GRASP = [2.0, 1.0, 1.8, 3.0, 3.0, 3.5, 8.0]
D1_MAX_STEP_DEG_SEARCH = _parse_step_deg_list(os.environ.get("D1_MAX_STEP_DEG_SEARCH"), _DEFAULT_SEARCH)
D1_MAX_STEP_DEG_GRASP = _parse_step_deg_list(os.environ.get("D1_MAX_STEP_DEG_GRASP"), _DEFAULT_GRASP)
D1_SEARCH_COMMAND_DELAY_MS = int(os.environ.get("D1_SEARCH_DELAY_MS", "560"))
D1_PLAN_COMMAND_DELAY_MS = int(os.environ.get("D1_PLAN_DELAY_MS", "620"))
D1_SEARCH_MAX_CYCLES = int(os.environ.get("D1_SEARCH_MAX_CYCLES", "10"))
# Nominal scan pose (servo degrees): wrist strongly pitched so the wrist camera looks toward the floor/workspace ahead.
D1_SEARCH_SHOULDER_NOM_DEG = float(os.environ.get("D1_SEARCH_SHOULDER_NOM_DEG", "-52"))
D1_SEARCH_ELBOW_NOM_DEG = float(os.environ.get("D1_SEARCH_ELBOW_NOM_DEG", "48"))
D1_SEARCH_WRIST_NOM_DEG = float(os.environ.get("D1_SEARCH_WRIST_NOM_DEG", "-74"))
# Passi servo più conservativi per riallinearsi alla posizione «Salva START» prima della ricerca AprilTag.
# Indici: 0..5 braccio, 6 gripper. Giunti 2–3 (terzo/quarto) passi ridotti per meno cedimento in traiettoria.
_DEFAULT_START_ALIGN_STEPS = [2.2, 1.0, 0.9, 1.2, 2.2, 2.4, 4.0]
D1_START_ALIGN_MAX_STEP_DEG = _parse_step_deg_list(
    os.environ.get("D1_START_ALIGN_MAX_STEP_DEG"), _DEFAULT_START_ALIGN_STEPS
)
# Fold compatto (template cinematica D1) — stesso profilo passi della START align salvo override env.
D1_FOLD_MAX_STEP_DEG = _parse_step_deg_list(
    os.environ.get("D1_FOLD_MAX_STEP_DEG"), _DEFAULT_START_ALIGN_STEPS
)
# Movimenti da editor UI / taratura manuale (passi stretti su giunti 2–4).
_DEFAULT_EDITOR_STEPS = [1.6, 0.8, 0.7, 1.0, 1.6, 1.8, 4.0]
D1_EDITOR_MAX_STEP_DEG = _parse_step_deg_list(os.environ.get("D1_EDITOR_MAX_STEP_DEG"), _DEFAULT_EDITOR_STEPS)
# ZERO ↔ START / goto ZERO: passi più ampi + delay più basso (meno punti interpolati = più veloce e fluido con ease dedicata).
_DEFAULT_ZERO_TRANSITION_STEPS = [3.4, 1.7, 1.5, 2.0, 3.4, 3.8, 6.5]
D1_ZERO_TRANSITION_MAX_STEP_DEG = _parse_step_deg_list(
    os.environ.get("D1_ZERO_TRANSITION_MAX_STEP_DEG"), _DEFAULT_ZERO_TRANSITION_STEPS
)
# Micro-mosse centratura tag su camera polso (passi stretti).
_DEFAULT_WRIST_CENTER_STEPS = [1.2, 0.65, 0.55, 0.75, 1.15, 1.15, 4.0]
D1_WRIST_CENTER_MAX_STEP_DEG = _parse_step_deg_list(
    os.environ.get("D1_WRIST_CENTER_MAX_STEP_DEG"), _DEFAULT_WRIST_CENTER_STEPS
)


def _zero_transition_interp_profile() -> str:
    return (os.environ.get("D1_ZERO_TRANSITION_INTERP", "smoothstep") or "smoothstep").strip().lower()


def _zero_transition_command_delay_ms(*, for_true_zero_only: bool) -> int:
    """Delay helper DDS per percorsi ZERO↔START (default più bassi della ricerca tag)."""
    if for_true_zero_only:
        raw = (
            os.environ.get("D1_TRUE_ZERO_DELAY_MS")
            or os.environ.get("D1_ZERO_TRANSITION_DELAY_MS")
            or "175"
        )
        cap = int(os.environ.get("D1_ZERO_TRANSITION_DELAY_CAP_MS", "270"))
    else:
        raw = (
            os.environ.get("D1_ZERO_TO_START_DELAY_MS")
            or os.environ.get("D1_ZERO_TRANSITION_DELAY_MS")
            or "175"
        )
        cap = int(os.environ.get("D1_ZERO_TO_START_DELAY_CAP_MS", "250"))
    delay_ms = int(float(raw))
    return max(80, min(delay_ms, cap))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class CameraCache:
    def __init__(self, devices: dict[int, str], fps: float = 20.0, jpeg_quality: int = 68):
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
                v4l_idx = _v4l_index_for_logical_camera(device)
                cap = _cv_videocapture(v4l_idx)
                if cap.isOpened():
                    try:
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        pass
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    cap.set(cv2.CAP_PROP_FPS, 15)
                else:
                    with self._lock:
                        self.errors[device] = f"open failed (V4L /dev/video{v4l_idx}, logical {device})"
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
                # RealSense: aprire il nodo sbagliato dà frame tutti neri; non sovrascrivere la cache.
                if frame.size and float(frame.max()) < 4.0:
                    with self._lock:
                        self.errors[device] = (
                            f"frame nero su V4L — verifica GO2_VIDEO_INDEX_{device} "
                            f"(reale /dev/video{_v4l_index_for_logical_camera(device)}, logico {device})"
                        )
                    time.sleep(0.25)
                    continue
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

    def peek_jpeg(self, device: int) -> bytes | None:
        """Ultimo frame in cache senza attesa (per MJPEG: niente blocchi lunghi sul generator)."""
        self.start(device)
        with self._lock:
            item = self.frames.get(device)
            if item is None:
                return None
            return item["jpg"]

    def stats(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            return {
                str(device): {
                    "label": self.devices[device],
                    "available": device in self.frames and (now - self.frames[device]["ts"]) < 5.0,
                    "started": device in self._started_devices,
                    "age_ms": None if device not in self.frames else round((now - self.frames[device]["ts"]) * 1000, 1),
                    "shape": None if device not in self.frames else self.frames[device]["shape"],
                    "error": self.errors.get(device),
                }
                for device in self.devices
            }


CAMERA_CACHE = CameraCache(
    CAMERA_DEVICES,
    fps=float(os.environ.get("GO2_CAMERA_CACHE_FPS", "20")),
)

ARM_GRASP_ABORT = threading.Event()
ARM_OPERATION_LOCK = threading.RLock()
LAST_ARM_JOB: dict[str, Any] = {"status": "idle", "updated_at": None, "detail": {}}
ARM_GRASP_EVENTS: list[dict[str, Any]] = []
ARM_GRASP_EVENTS_MAX = 80
# Override da dashboard (slider): usati dal grasp loop al posto di env per la sessione.
ARM_UI_TUNING: dict[str, Any] = {}
ARM_UI_TUNING_LOCK = threading.Lock()
# Flag grasp (bool) e stringhe opzionali — stessa istanza Flask sulla NX; reset esplicito da UI.
# Chiavi: trust_wrist_absolute_ik, use_fused_plan_ik, fused_with_center, front_camera_fallback_grasp,
# prefer_tag_grip, grasp_execute_arm (opzionale; se assente si usa solo env).
ARM_GRASP_SESSION: dict[str, Any] = {}
ARM_GRASP_SESSION_LOCK = threading.Lock()


def _ui_tuning_get(key: str) -> Any | None:
    with ARM_UI_TUNING_LOCK:
        return ARM_UI_TUNING.get(key)


def _tune_float(key: str, env_key: str, default: float) -> float:
    v = _ui_tuning_get(key)
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    try:
        return float(os.environ.get(env_key, str(default)))
    except ValueError:
        return float(default)


def _tune_int(key: str, env_key: str, default: int) -> int:
    return int(round(_tune_float(key, env_key, float(default))))


def _env_truthy(val: str | None, *, default: str = "0") -> bool:
    return (val if val is not None else default).lower() in {"1", "true", "yes"}


def _session_grasp_get(key: str) -> Any | None:
    with ARM_GRASP_SESSION_LOCK:
        return ARM_GRASP_SESSION.get(key)


def _effective_grasp_bool(session_key: str, env_key: str, *, default_env: str = "0") -> bool:
    """Override dashboard (sessione) ha priorità su os.environ per il flag grasp."""
    v = _session_grasp_get(session_key)
    if v is not None:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(int(v))
        if isinstance(v, str):
            return _env_truthy(v, default="0")
        return bool(v)
    return _env_truthy(os.environ.get(env_key), default=default_env)


def _effective_search_delay_ms() -> int:
    return _tune_int("search_delay_ms", "D1_SEARCH_DELAY_MS", D1_SEARCH_COMMAND_DELAY_MS)


def _effective_plan_delay_ms() -> int:
    return _tune_int("plan_delay_ms", "D1_PLAN_DELAY_MS", D1_PLAN_COMMAND_DELAY_MS)


def _grasp_execute_enabled() -> bool:
    v = _session_grasp_get("grasp_execute_arm")
    if v is not None:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(int(v))
        if isinstance(v, str):
            return _env_truthy(v, default="0")
        return bool(v)
    return _env_truthy(os.environ.get("GO2_GRASP_EXECUTE_ARM"), default="0")


def grasp_session_effective_flags() -> dict[str, Any]:
    """Valori effettivi (sessione > env) per UI e debug."""
    return {
        "trust_wrist_absolute_ik": _effective_grasp_bool("trust_wrist_absolute_ik", "GO2_TRUST_WRIST_ABSOLUTE_IK"),
        "use_fused_plan_ik": _effective_grasp_bool("use_fused_plan_ik", "GO2_GRASP_USE_FUSED_PLAN_IK"),
        "fused_with_center": _effective_grasp_bool("fused_with_center", "GO2_GRASP_FUSED_WITH_CENTER"),
        "front_camera_fallback_grasp": _effective_grasp_bool(
            "front_camera_fallback_grasp", "GO2_FRONT_CAMERA_FALLBACK_GRASP"
        ),
        "prefer_tag_grip": _effective_grasp_bool("prefer_tag_grip", "GO2_GRASP_PREFER_TAG_GRIP"),
        "grasp_execute_arm": _grasp_execute_enabled(),
    }


BOX_TAG_IDS_IK = frozenset({0, 1, 2, 3})


def _candidate_ik_detail(c: dict[str, Any]) -> dict[str, Any]:
    """Scomposizione per-camera: tag → target → preview IK (per debug UI)."""
    tags_obj = c.get("tags") or {}
    tag_list = tags_obj.get("tags") or []
    ids = [int(t.get("id", -1)) for t in tag_list]
    box_ids = [i for i in ids if i in BOX_TAG_IDS_IK]
    target = c.get("target") or {}
    preview = c.get("preview") or {}
    grip = c.get("grip_point") or {}
    obj = c.get("object_detection") or {}
    return {
        "camera_label": c.get("camera_label"),
        "camera_error": c.get("error"),
        "tag_ids_seen": ids,
        "box_tag_ids": box_ids,
        "object_detection_ok": bool(obj.get("ok")),
        "object_detection_backend": obj.get("backend"),
        "object_detection_confidence": obj.get("confidence"),
        "grip_point_ok": bool(grip.get("ok")),
        "grip_point_source": grip.get("source"),
        "grip_center_px": grip.get("grip_center_px"),
        "grip_axis_px": grip.get("grip_axis_px"),
        "box_area_px": grip.get("box_area_px"),
        "approach_error_px": grip.get("approach_error_px"),
        "absolute_ik_safe": bool(c.get("absolute_ik_safe")),
        "absolute_ik_note": c.get("absolute_ik_note"),
        "has_box_tags_for_ik": len(box_ids) > 0,
        "target_ok": bool(target.get("ok")),
        "target_error": target.get("error"),
        "preview_ok": bool(preview.get("ok")),
        "preview_error": preview.get("error"),
        "ik_failed_stage": preview.get("failed_stage"),
        "plan_stage_count": len(preview.get("plan") or []),
        "candidate_top_ok": bool(c.get("ok")),
    }


def _plan_ready_for_fused_ik(plan: dict[str, Any]) -> bool:
    """True se il piano «dual-camera» ha una traiettoria IK eseguibile (camera selezionata dal punteggio)."""
    if not plan.get("ok"):
        return False
    sel = plan.get("selected")
    if not isinstance(sel, dict):
        return False
    if not sel.get("absolute_ik_safe", True):
        return False
    preview = sel.get("preview") or {}
    return bool(preview.get("ok") and (preview.get("plan") or []))


def _plan_has_grip_detection(plan: dict[str, Any]) -> bool:
    for cand in (plan.get("candidates") or {}).values():
        if ((cand or {}).get("grip_point") or {}).get("ok"):
            return True
    return False


def grasp_pipeline_status() -> dict[str, Any]:
    """
    Stato end-to-end: visione → IK → prerequisiti motion — per UI e debug senza avviare il grasp.
    """
    plan = _box_plan_snapshot()
    diag = arm_diagnose_motion()
    cands_raw = plan.get("candidates") or {}
    per_dev: dict[str, Any] = {str(d): _candidate_ik_detail(cands_raw.get(str(d)) or {}) for d in (0, 6)}
    story: list[str] = []
    real = bool(diag.get("real_arm_env"))
    if not real:
        story.append(
            "① Movimento braccio disattivato: sulla Jetson esporta GO2_ENABLE_REAL_ARM=1 e riavvia il processo dashboard."
        )
    if not diag.get("go2_local"):
        story.append(
            "② GO2_LOCAL=0: questa istanza non legge le camere in locale — il piano può restare vuoto o ritardato."
        )
    if not diag.get("servo_feedback_ok"):
        story.append(
            "③ Nessun feedback servo DDS: fold, ricerca e IK non partono (verifica bin/d1_arm_feedback_helper e dominio DDS)."
        )
    if not diag.get("start_alignment_json"):
        story.append("④ Manca data/start_alignment.json — salva START dal tab presa prima di una corsa operativa.")
    if not TRUE_ZERO_POSE_PATH.is_file():
        story.append(
            "④b (opz.) Manca data/true_zero_pose.json — «Salva ZERO (corrente)» quando il braccio è in ripiegatura calibrata per ZERO→START."
        )

    fusion_ok = bool(plan.get("ok"))
    sel = plan.get("selected_camera")
    grip_any = _plan_has_grip_detection(plan)
    if fusion_ok:
        safe_note = ""
        selected_raw = plan.get("selected") if isinstance(plan.get("selected"), dict) else {}
        if selected_raw and not selected_raw.get("absolute_ik_safe", True):
            safe_note = " Nota: la camera scelta è polso e l'IK assoluta è marcata non sicura; verrà usata solo per visual-servo."
        story.append(
            f"⑤ Piano IK globale OK (camera scelta: {sel}). Esecuzione: dopo doppio lock tag sul polso, "
            "oppure — se abiliti GO2_GRASP_USE_FUSED_PLAN_IK=1 — due snapshot consecutivi con piano ok." + safe_note
        )
    elif grip_any:
        story.append(
            "⑤ Punto presa 2D disponibile (tag o detector), ma IK globale non pronta: il visual servo può centrare/avvicinare prima dell'IK."
        )
    else:
        story.append(
            "⑤ Piano IK globale non pronto: serve tag scatola 0–3 oppure detector box (YOLO/TensorRT o fallback) con punto presa."
        )
        for dev in (0, 6):
            d = per_dev[str(dev)]
            if d.get("camera_error"):
                story.append(f"   · /dev/video{dev}: frame — {d['camera_error']}")
                continue
            if d.get("object_detection_ok"):
                story.append(
                    f"   · /dev/video{dev}: detector {d.get('object_detection_backend')} vede grip point, ma target/IK non ancora pronto."
                )
                continue
            if not d.get("tag_ids_seen"):
                story.append(f"   · /dev/video{dev}: nessun AprilTag e nessuna box detection utilizzabile.")
                continue
            if not d.get("has_box_tags_for_ik"):
                story.append(
                    f"   · /dev/video{dev}: vedi solo landmark (es. id5); per IK presa servono tag scatola 0–3 nel frame."
                )
                continue
            if not d.get("target_ok"):
                story.append(f"   · /dev/video{dev}: tag box ok ma target base no — {d.get('target_error')}")
                continue
            if not d.get("preview_ok"):
                fe = d.get("ik_failed_stage") or d.get("preview_error") or "?"
                story.append(f"   · /dev/video{dev}: target ok ma IK fallita (stage/err: {fe}).")

    fused_env = _effective_grasp_bool("use_fused_plan_ik", "GO2_GRASP_USE_FUSED_PLAN_IK")
    if not fused_env and fusion_ok and not per_dev["0"].get("candidate_top_ok") and per_dev["6"].get("candidate_top_ok"):
        story.append(
            "Suggerimento: il RealSense ha piano ok ma il polso no — il loop resta in ricerca finché video0 non locka. "
            "Per eseguire IK dalla camera migliore senza lock polso: GO2_GRASP_USE_FUSED_PLAN_IK=1 (solo se sicuro)."
        )

    fusion_ready_exec = _plan_ready_for_fused_ik(plan)
    wrist_c0 = cands_raw.get("0") or {}
    wd0 = _candidate_ik_detail(wrist_c0)
    wrist_sees_box_tags = bool(wd0.get("has_box_tags_for_ik"))
    wrist_preview_ok = bool(wd0.get("preview_ok"))
    eff = grasp_session_effective_flags()
    with ARM_GRASP_SESSION_LOCK:
        sess_over = dict(ARM_GRASP_SESSION)
    base = {
        "ok": True,
        "updated_at": now_iso(),
        "true_zero_json": TRUE_ZERO_POSE_PATH.is_file(),
        "environment": {
            "GO2_LOCAL": bool(diag.get("go2_local")),
            "GO2_ENABLE_REAL_ARM": real,
            "GO2_GRASP_EXECUTE_ARM": "1" if _grasp_execute_enabled() else "0",
            "GO2_GRASP_USE_FUSED_PLAN_IK": "1" if fused_env else "0",
            "GO2_FRONT_CAMERA_FALLBACK_GRASP": "1"
            if _effective_grasp_bool("front_camera_fallback_grasp", "GO2_FRONT_CAMERA_FALLBACK_GRASP")
            else "0",
            "GO2_TRUST_WRIST_ABSOLUTE_IK": "1"
            if _effective_grasp_bool("trust_wrist_absolute_ik", "GO2_TRUST_WRIST_ABSOLUTE_IK")
            else "0",
            "GO2_GRASP_FUSED_WITH_CENTER": "1"
            if _effective_grasp_bool("fused_with_center", "GO2_GRASP_FUSED_WITH_CENTER")
            else "0",
            "GO2_GRASP_PREFER_TAG_GRIP": "1"
            if _effective_grasp_bool("prefer_tag_grip", "GO2_GRASP_PREFER_TAG_GRIP")
            else "0",
        },
        "grasp_session_overrides": sess_over,
        "effective_grasp_flags": eff,
        "grasp_trigger_params": {
            "GO2_WRIST_GRASP_DIAGONAL_MIN_PX": float(os.environ.get("GO2_WRIST_GRASP_DIAGONAL_MIN_PX", "420")),
            "GO2_GRASP_LOSS_DEBOUNCE_FRAMES": int(os.environ.get("GO2_GRASP_LOSS_DEBOUNCE_FRAMES", "2")),
        },
        "fusion_plan_ok": fusion_ok,
        "fusion_ready_for_execute": fusion_ready_exec,
        "grip_detection_any": grip_any,
        "wrist_sees_box_tags": wrist_sees_box_tags,
        "wrist_preview_ok": wrist_preview_ok,
        "selected_camera": sel,
        "candidates": per_dev,
        "diagnose_hints": diag.get("hints") or [],
        "narrative_it": story,
    }
    start_ok, start_why = _grasp_preflight_allows_sequence_start(
        {
            "fusion_ready_for_execute": fusion_ready_exec,
            "wrist_sees_box_tags": wrist_sees_box_tags,
            "wrist_preview_ok": wrist_preview_ok,
        }
    )
    base["sequence_start_ready"] = start_ok
    base["sequence_start_block_reason"] = None if start_ok else start_why
    base["preflight_strict"] = os.environ.get("GO2_GRASP_PREFLIGHT_STRICT", "0")
    base["relaxed_hardware_ready"] = _grasp_relaxed_hardware_ready()[0]
    return base


def _grasp_relaxed_hardware_ready() -> tuple[bool, str]:
    """Condizioni minime per avviare la sequenza senza attendere tag/IK già perfetti nel planner."""
    if not _grasp_execute_enabled():
        return False, "GO2_GRASP_EXECUTE_ARM=0"
    d = arm_diagnose_motion()
    if not d.get("real_arm_env"):
        return False, "GO2_ENABLE_REAL_ARM=0"
    if not d.get("servo_feedback_ok"):
        return False, "no_servo_feedback"
    if not d.get("start_alignment_json"):
        return False, "no_start_alignment_json"
    if not GO2_LOCAL:
        return False, "GO2_LOCAL=0"
    return True, ""


def _grasp_preflight_allows_sequence_start(preflight: dict[str, Any]) -> tuple[bool, str | None]:
    """
    - strict=1: richiede piano fuso OPPURE (tag scatola sul polso + preview IK polso + hardware).
    - strict=0: basta hardware DDS/START, oppure fusion, oppure (wrist tag + hardware) se hardware da solo fosse troppo debole.
    In pratica: se vedi il QR/tag sulla camera polso e l'IK polso è ok, non serve la frontale.
    """
    strict = os.environ.get("GO2_GRASP_PREFLIGHT_STRICT", "0").lower() in {"1", "true", "yes"}
    fusion_ok = bool(preflight.get("fusion_ready_for_execute"))
    wrist_tags = bool(preflight.get("wrist_sees_box_tags"))
    wrist_preview = bool(preflight.get("wrist_preview_ok"))

    ok_hw, why_hw = _grasp_relaxed_hardware_ready()

    if strict:
        if fusion_ok:
            return True, None
        if wrist_tags and wrist_preview:
            if ok_hw:
                return True, None
            return False, why_hw
        return False, "preflight_strict_need_fusion_or_wrist_box_tag_with_ik"

    if fusion_ok:
        return True, None
    if ok_hw:
        return True, None
    if wrist_tags and wrist_preview and ok_hw:
        return True, None
    if wrist_tags and (not wrist_preview):
        return False, "wrist_sees_tag_but_ik_preview_failed"
    return False, why_hw or "preflight_relaxed_failed"


def arm_diagnose_motion() -> dict[str, Any]:
    """Perché il braccio potrebbe non muoversi: checklist operativa per la UI."""
    real = os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"}
    hints: list[str] = []
    if not GO2_LOCAL:
        hints.append(
            "GO2_LOCAL=0: questa istanza Flask non è sulla Jetson — feed camere/LiDAR possono essere assenti o via SSH."
        )
    if not real:
        hints.append(
            "GO2_ENABLE_REAL_ARM disattivo: nessun comando reale al braccio (solo pianificazione / dry-run)."
        )
    fb_bin = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
    cmd_bin = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not fb_bin.is_file():
        hints.append(f"Manca {fb_bin.name} — senza feedback il loop rifiuta ricerca e IK.")
    if not cmd_bin.is_file():
        hints.append(f"Manca {cmd_bin.name} — non si possono inviare movimenti DDS.")
    cur = _read_d1_servo_angles()
    if cur is None:
        hints.append(
            "Nessun feedback servo da DDS (helper non risponde, dominio DDS errato, braccio spento o cavo)."
        )
    else:
        hints.append(f"Feedback servo DDS: OK ({len(cur)} canali letti).")
    if not ALIGNMENT_START_PATH.is_file():
        hints.append("Nessun data/start_alignment.json — «Salva START» prima della sequenza operativa.")
    dj = LAST_ARM_JOB.get("detail") or {}
    res = dj.get("result")
    if isinstance(res, dict) and res.get("reason"):
        hints.append(f"Ultimo esito grasp: {res.get('reason')}")
    if isinstance(res, dict) and res.get("helper_stderr"):
        tail = str(res.get("helper_stderr", ""))[-280:]
        if tail.strip():
            hints.append("Stderr helper (ultimi caratteri): " + tail.replace("\n", " "))
    return {
        "ok": True,
        "hints": hints,
        "go2_local": GO2_LOCAL,
        "real_arm_env": real,
        "servo_feedback_ok": cur is not None,
        "last_job_status": LAST_ARM_JOB.get("status"),
        "command_stack": command_stack_status(),
        "start_alignment_json": ALIGNMENT_START_PATH.is_file(),
        "true_zero_json": TRUE_ZERO_POSE_PATH.is_file(),
        "camera_devices": list(CAMERA_DEVICES.keys()),
    }


def _arm_job_update(status: str, detail: dict[str, Any] | None = None) -> None:
    with ARM_OPERATION_LOCK:
        LAST_ARM_JOB["status"] = status
        LAST_ARM_JOB["updated_at"] = now_iso()
        LAST_ARM_JOB["detail"] = dict(detail or {})


def _arm_event(kind: str, message: str, **extra: Any) -> None:
    event = {
        "t": now_iso(),
        "kind": kind,
        "message": message,
        **{k: v for k, v in extra.items() if v is not None},
    }
    with ARM_OPERATION_LOCK:
        ARM_GRASP_EVENTS.append(event)
        del ARM_GRASP_EVENTS[:-ARM_GRASP_EVENTS_MAX]


def _grasp_live_phase(label_it: str, **extra: Any) -> None:
    """Aggiorna messaggio passo-passo per GET /api/arm/job_status (solo job grasp running)."""
    with ARM_OPERATION_LOCK:
        if LAST_ARM_JOB.get("status") != "running":
            return
    detail: dict[str, Any] = {
        "phase": "wrist_guided_grasp",
        "phase_label_it": label_it,
        **extra,
    }
    detail = {k: v for k, v in detail.items() if v is not None}
    _arm_job_update("running", detail)
    _arm_event("phase", label_it, **extra)


def _sleep_abortable(total_s: float, chunk_s: float = 0.05) -> bool:
    """Sleep in short slices; returns False if ARM_GRASP_ABORT was set."""
    end = time.time() + total_s
    while time.time() < end:
        if ARM_GRASP_ABORT.is_set():
            return False
        time.sleep(min(chunk_s, max(0.0, end - time.time())))
    return True


def warmup_realtime_feeds() -> None:
    if GO2_LOCAL:
        CAMERA_CACHE.start()
        LIDAR_CACHE.start()


def nx_stack_status() -> dict[str, Any]:
    """Stato operativo sul robot (solo significativo con GO2_LOCAL=1)."""
    cs = command_stack_status()
    detector = object_detector_stack_status()
    return {
        "go2_local": GO2_LOCAL,
        "dashboard_bind": GO2_DASHBOARD_BIND,
        "pid": os.getpid(),
        "hostname": platform.node(),
        "cameras": CAMERA_CACHE.stats() if GO2_LOCAL else {},
        "command_stack": cs,
        "object_detector": detector,
        "real_arm_env": os.environ.get("GO2_ENABLE_REAL_ARM", "0"),
        "base_motion_env": os.environ.get("GO2_ENABLE_BASE_MOTION", "0"),
    }


def object_detector_stack_status() -> dict[str, Any]:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from box_object_detector import detector_status

        return detector_status()
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


@APP.route("/api/nx/stack/status", methods=["GET"])
def api_nx_stack_status() -> Any:
    return jsonify({"ok": True, **nx_stack_status()})


@APP.route("/api/nx/stack/start", methods=["POST"])
def api_nx_stack_start() -> Any:
    """
    Avvio esplicito camere + LiDAR sulla NX (thread locali). Da pulsante dashboard.
    """
    if not GO2_LOCAL:
        return jsonify(
            {
                "ok": False,
                "reason": "GO2_LOCAL!=1 — questa istanza non è la dashboard sulla Jetson (sensori non locali).",
                **nx_stack_status(),
            }
        ), 400
    warmup_realtime_feeds()
    # Assicura capture esplicita per entrambe le camere usate dal grasp.
    CAMERA_CACHE.start(0)
    CAMERA_CACHE.start(6)
    return jsonify({"ok": True, "message": "Camere + LiDAR avviati sul robot.", **nx_stack_status()})


def _base_motion_allowed() -> tuple[bool, str | None]:
    if os.environ.get("GO2_ENABLE_BASE_MOTION", "0").lower() not in {"1", "true", "yes"}:
        return False, "GO2_ENABLE_BASE_MOTION is not enabled (refusing Sport RPC). Set GO2_ENABLE_BASE_MOTION=1 or rely on default when GO2_LOCAL=1."
    if not GO2_LOCAL:
        return False, "Dashboard must run on the robot with GO2_LOCAL=1 for Sport DDS."
    return True, None


@APP.route("/api/base/accompany_mode", methods=["POST"])
def api_base_accompany_mode() -> Any:
    ok_gate, reason = _base_motion_allowed()
    if not ok_gate:
        return jsonify({"ok": False, "reason": reason}), 403
    body = request.get_json(silent=True) or {}
    enable = bool(body.get("enable", True))
    stand_first = bool(body.get("stand_up_first", False))
    speed_raw = body.get("speed_level")
    speed_level = int(speed_raw) if speed_raw is not None else None
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from go2_accompany import sport_accompany

    iface = GO2_DDS_INTERFACE.strip() if GO2_DDS_INTERFACE else None
    mode = str(body.get("mode") or "joystick").strip().lower()

    def _sport_call() -> Any:
        return sport_accompany(
            project_root=PROJECT_ROOT,
            domain=GO2_DDS_DOMAIN,
            iface=iface,
            enable=enable,
            mode=mode,
            stand_up_first=stand_first,
            speed_level=speed_level,
        )

    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "45"))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_sport_call)
            result = fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": f"sport_rpc_timeout_after_{timeout_s}s",
                    "hint": "DDS Sport ha tardato troppo — robot impegnato o DDS non raggiungibile.",
                }
            ),
            504,
        )
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 502

    status = 200 if result.get("ok") else 502
    return jsonify(result), status


@APP.route("/api/alignment/start_pose", methods=["GET", "POST"])
def api_alignment_start_pose() -> Any:
    if request.method == "GET":
        if not ALIGNMENT_START_PATH.exists():
            return jsonify({"ok": False, "reason": "no_saved_start_pose"}), 404
        try:
            data = json.loads(ALIGNMENT_START_PATH.read_text(encoding="utf-8"))
            return jsonify({"ok": True, "path": str(ALIGNMENT_START_PATH), "start_pose": data})
        except Exception as exc:
            return jsonify({"ok": False, "reason": repr(exc)}), 500

    if not GO2_LOCAL:
        return jsonify({"ok": False, "reason": "GO2_LOCAL=1 required to snapshot cameras on the NX."}), 400
    body = request.get_json(silent=True) or {}
    override_sd = body.get("servo_deg")
    try:
        ALIGNMENT_START_PATH.parent.mkdir(parents=True, exist_ok=True)
        wait_s = float(os.environ.get("GO2_START_SAVE_WAIT_TAG_S", "5.0"))
        deadline = time.time() + max(0.0, wait_s)
        snap_box = json.loads(api_box_plan().get_data(as_text=True))
        while time.time() < deadline and not _plan_has_apriltag_detection(snap_box):
            time.sleep(0.25)
            snap_box = json.loads(api_box_plan().get_data(as_text=True))
        if isinstance(override_sd, list) and len(override_sd) >= 6:
            try:
                arm_at_start = _arm_snapshot_from_servo_deg([float(x) for x in override_sd])
            except (TypeError, ValueError):
                arm_at_start = _arm_at_start_snapshot()
        else:
            arm_at_start = _arm_at_start_snapshot()
        payload = {
            "label": "START",
            "saved_at": now_iso(),
            "note": (
                "Operational alignment pose — frontal object + AprilTags as visible at save time. "
                "Tag 5 is usually wrist-camera-only; this snapshot is not absolute odometry. "
                "arm_at_start carries current D1 servo pose for IK seed / repeatability."
            ),
            "box_plan": snap_box,
            "box_plan_has_apriltag": _plan_has_apriltag_detection(snap_box),
            "arm_at_start": arm_at_start,
            "nx_stack": nx_stack_status(),
        }
        ALIGNMENT_START_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return jsonify({"ok": True, "saved_to": str(ALIGNMENT_START_PATH), "start_pose": payload})
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 500


@APP.route("/api/arm/servo_snapshot", methods=["GET"])
def api_arm_servo_snapshot() -> Any:
    """Solo lettura angoli servo D1 (feedback DDS). Nessuna RPC Sport."""
    cur = _read_d1_servo_angles()
    if cur is None:
        return jsonify({"ok": False, "reason": "no_servo_feedback", "hint": "Verifica bin/d1_arm_feedback_helper e DDS sul robot."}), 503
    return jsonify(
        {
            "ok": True,
            "servo_deg": [round(float(v), 3) for v in cur[:7]],
            "saved_at": now_iso(),
        }
    )


@APP.route("/api/arm/save_pose_snapshot", methods=["POST"])
def api_arm_save_pose_snapshot() -> Any:
    """Append una riga in data/arm_pose_snapshots.json — memorizza pose braccio (servo), non la base."""
    if not GO2_LOCAL:
        return jsonify({"ok": False, "reason": "GO2_LOCAL=1 required on the robot to write pose files."}), 400
    cur = _read_d1_servo_angles()
    if cur is None:
        return jsonify({"ok": False, "reason": "no_servo_feedback"}), 503
    body = request.get_json(silent=True) or {}
    label = str(body.get("label") or "").strip() or "pose"
    ARM_POSE_SNAPSHOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries: list[Any] = []
    if ARM_POSE_SNAPSHOTS_PATH.exists():
        try:
            raw = json.loads(ARM_POSE_SNAPSHOTS_PATH.read_text(encoding="utf-8"))
            entries = raw if isinstance(raw, list) else []
        except Exception:
            entries = []
    entry = {
        "label": label,
        "saved_at": now_iso(),
        "servo_deg": [round(float(v), 3) for v in cur[:7]],
    }
    entries.append(entry)
    ARM_POSE_SNAPSHOTS_PATH.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "saved_to": str(ARM_POSE_SNAPSHOTS_PATH), "entry": entry})


@APP.route("/api/arm/hold_pose", methods=["POST"])
def api_arm_hold_pose() -> Any:
    """Ripeti posa servo corrente (riduce creep). Non è Sport — solo braccio D1."""
    hold = publish_d1_hold_current()
    code = 200 if hold.get("ok") else (503 if "REAL_ARM" in str(hold.get("reason", "")) else 502)
    return jsonify({"ok": bool(hold.get("ok")), **hold}), code


def _arm_grasp_worker_busy() -> bool:
    with ARM_OPERATION_LOCK:
        return LAST_ARM_JOB.get("status") in ("running", "starting")


def _clear_stale_abort_for_manual_motion() -> bool:
    """Non riarmare automaticamente dopo FERMA: serve restart/riarmo esplicito."""
    return False


def _d1_goto_servo_deg(
    servo_deg: list[float],
    *,
    stage_name: str = "goto_servo_deg",
    max_step_deg: list[float] | None = None,
    delay_ms: int | None = None,
    skip_prehold: bool = False,
) -> dict[str, Any]:
    """Movimento smooth verso 7 angoli (gradi) da feedback reale; gripper = canale 6 come nella UI."""
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    _clear_stale_abort_for_manual_motion()
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "skipped": False, "reason": "aborted_before_goto"}
    sd = [float(servo_deg[i]) for i in range(min(7, len(servo_deg)))]
    while len(sd) < 7:
        sd.append(sd[-1])
    if (
        not skip_prehold
        and os.environ.get("D1_GOTO_PREHOLD", "1").lower() in {"1", "true", "yes"}
    ):
        publish_d1_hold_current(
            repeats=max(4, int(os.environ.get("D1_GOTO_PREHOLD_REPEATS", "12"))),
            delay_ms=max(35, int(os.environ.get("D1_GOTO_PREHOLD_DELAY_MS", "55"))),
        )
    jr = [math.radians(sd[i]) for i in range(6)]
    steps = max_step_deg if max_step_deg is not None else D1_EDITOR_MAX_STEP_DEG
    dms = delay_ms if delay_ms is not None else int(os.environ.get("D1_EDITOR_MOVE_DELAY_MS", "420"))
    stages = [{"stage": stage_name, "joints_rad": jr}]
    messages, sent = _stage_messages(stages, close_gripper=False, max_step_deg=steps)
    result = _run_d1_messages(messages, delay_ms=max(70, int(dms)), post_hold=True)
    return {**result, "skipped": False, "sent_stages": sent, "target_servo_deg": [round(x, 3) for x in sd]}


def _d1_send_live_pose_deg(servo_deg: list[float]) -> dict[str, Any]:
    """
    Comandi posa diretti (no spline): piccola raffica DDS — per slider in tempo quasi reale.
    """
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    _clear_stale_abort_for_manual_motion()
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "skipped": False, "reason": "aborted_before_live"}
    sd_in = [float(servo_deg[i]) for i in range(min(7, len(servo_deg)))]
    while len(sd_in) < 7:
        sd_in.append(sd_in[-1])
    sd = [round(max(-135.0, min(135.0, sd_in[i])), 3) for i in range(7)]
    repeats = max(1, int(os.environ.get("D1_LIVE_REPEAT", "5")))
    delay_ms = max(10, int(os.environ.get("D1_LIVE_DELAY_MS", "26")))
    seq = int(time.time()) % 100000
    messages: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    angles = {f"angle{idx}": sd[idx] for idx in range(7)}
    angles["mode"] = 1
    for r in range(repeats):
        messages.append({"seq": seq + 1 + r, "address": 1, "funcode": 2, "data": dict(angles)})
    result = _run_d1_messages(messages, delay_ms=delay_ms)
    return {**result, "skipped": False, "mode": "live_direct", "target_servo_deg": sd}


def _d1_move_one_joint(joint_index: int, angle_deg: float) -> dict[str, Any]:
    """Muove un solo giunto: target = feedback corrente con un solo angolo sostituito."""
    if joint_index < 0 or joint_index > 6:
        return {"ok": False, "skipped": False, "reason": "joint_index must be 0..6"}
    fb = _read_d1_servo_angles_median()
    if fb is None:
        fb = _read_d1_servo_angles_stable()
    if fb is None:
        return {"ok": False, "skipped": False, "reason": "no_servo_feedback"}
    sd = [round(float(fb[i]), 3) for i in range(7)]
    sd[joint_index] = round(float(angle_deg), 3)
    narrow = _parse_step_deg_list(os.environ.get("D1_ONE_JOINT_MAX_STEP_DEG"), D1_EDITOR_MAX_STEP_DEG)
    dms = int(os.environ.get("D1_ONE_JOINT_DELAY_MS", os.environ.get("D1_EDITOR_MOVE_DELAY_MS", "420")))
    return _d1_goto_servo_deg(
        sd,
        stage_name=f"editor_one_j{joint_index}",
        max_step_deg=narrow,
        delay_ms=dms,
        skip_prehold=True,
    )


@APP.route("/api/arm/joints/goto_deg", methods=["POST"])
def api_arm_joints_goto_deg() -> Any:
    """POST JSON {\"servo_deg\":[7 floats], \"delay_ms\":optional, \"max_step_custom\":optional list str}."""
    if _arm_grasp_worker_busy():
        return (
            jsonify({"ok": False, "reason": "arm_job_running"}),
            409,
        )
    body = request.get_json(silent=True) or {}
    sd_raw = body.get("servo_deg")
    if not isinstance(sd_raw, list) or len(sd_raw) < 6:
        return jsonify({"ok": False, "reason": "servo_deg must be a list of at least 6 numbers (deg)"}), 400
    try:
        sd = [float(x) for x in sd_raw[:7]]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "servo_deg values must be numeric"}), 400
    while len(sd) < 7:
        sd.append(sd[-1])
    custom = body.get("max_step_deg")
    mstep: list[float] | None = None
    if isinstance(custom, str) and custom.strip():
        mstep = _parse_step_deg_list(custom, D1_EDITOR_MAX_STEP_DEG)
    dms = body.get("delay_ms")
    delay_ms = int(dms) if dms is not None else None
    try:
        out = _d1_goto_servo_deg(sd, stage_name="editor_goto", max_step_deg=mstep, delay_ms=delay_ms)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify({"ok": bool(out.get("ok")) or bool(out.get("skipped")), **out}), code
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 502


@APP.route("/api/arm/joints/live_deg", methods=["POST"])
def api_arm_joints_live_deg() -> Any:
    """POST {\"servo_deg\":[7 floats]} — invio rapido posa senza interpolazione (slider real-time)."""
    if _arm_grasp_worker_busy():
        return jsonify({"ok": False, "reason": "arm_job_running"}), 409
    body = request.get_json(silent=True) or {}
    sd_raw = body.get("servo_deg")
    if not isinstance(sd_raw, list) or len(sd_raw) < 6:
        return jsonify({"ok": False, "reason": "servo_deg must be list of 6–7 numbers"}), 400
    try:
        sd = [float(x) for x in sd_raw[:7]]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "servo_deg must be numeric"}), 400
    while len(sd) < 7:
        sd.append(sd[-1])
    try:
        out = _d1_send_live_pose_deg(sd)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify({"ok": bool(out.get("ok")) or bool(out.get("skipped")), **out}), code
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 502


@APP.route("/api/arm/joints/move_one", methods=["POST"])
def api_arm_joints_move_one() -> Any:
    """POST JSON {\"joint_index\":0-6, \"angle_deg\":float} — muove solo quel giunto mantenendo gli altri dal feedback."""
    if _arm_grasp_worker_busy():
        return jsonify({"ok": False, "reason": "arm_job_running"}), 409
    body = request.get_json(silent=True) or {}
    try:
        ji = int(body.get("joint_index"))
        ad = float(body.get("angle_deg"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "joint_index (int) and angle_deg (float) required"}), 400
    try:
        out = _d1_move_one_joint(ji, ad)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify({"ok": bool(out.get("ok")) or bool(out.get("skipped")), **out}), code
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 502


def _true_zero_motion_http_response(result: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """
    Normalizza risposta HTTP per goto_zero / goto_start.
    Se ``skipped`` (es. GO2_ENABLE_REAL_ARM off), l'API deve riportare ok=false altrimenti la UI sembra «successo» senza movimento.
    """
    if result.get("skipped"):
        payload = {
            **result,
            "ok": False,
            "hint": "Nessun comando DDS inviato al braccio. Su NX: avvia la dashboard con GO2_ENABLE_REAL_ARM=1 (vedi scripts/deploy_dashboard_to_nx.py).",
        }
        return payload, 503
    ok = bool(result.get("ok"))
    out = {**result, "ok": ok}
    return out, 200 if ok else 502


@APP.route("/api/arm/true_zero", methods=["GET", "POST"])
def api_arm_true_zero() -> Any:
    """
    Posa ZERO calibrata (data/true_zero_pose.json): salva, vai a ZERO, oppure percorso smooth ZERO → START.
    POST JSON: {"op":"save"|"goto_zero"|"goto_start"} — movimento richiede GO2_ENABLE_REAL_ARM=1 sulla NX.
    """
    if request.method == "GET":
        exists = TRUE_ZERO_POSE_PATH.is_file()
        out: dict[str, Any] = {"ok": True, "exists": exists, "path": str(TRUE_ZERO_POSE_PATH)}
        if exists:
            try:
                raw = json.loads(TRUE_ZERO_POSE_PATH.read_text(encoding="utf-8"))
                out["saved_at"] = raw.get("saved_at")
                out["label"] = raw.get("label")
                arm = raw.get("arm") or raw.get("arm_at_start") or {}
                if isinstance(arm.get("servo_deg"), list):
                    out["servo_deg"] = arm["servo_deg"]
            except Exception as exc:
                out["read_error"] = repr(exc)
        return jsonify(out)

    body = request.get_json(silent=True) or {}
    op = str(body.get("op") or "").strip().lower()
    if op == "save":
        if not GO2_LOCAL:
            return (
                jsonify({"ok": False, "reason": "GO2_LOCAL=1 required on the robot to write true_zero_pose.json."}),
                400,
            )
        try:
            body_sd = body.get("servo_deg")
            ovr = [float(x) for x in body_sd] if isinstance(body_sd, list) and len(body_sd) >= 6 else None
            payload = _save_true_zero_snapshot(servo_deg_override=ovr, angle2_deg=None)
            if not payload.get("ok"):
                return jsonify(payload), 400
            _arm_event("true_zero", "Salvato data/true_zero_pose.json (posa ZERO)")
            return jsonify(payload)
        except Exception as exc:
            return jsonify({"ok": False, "reason": repr(exc)}), 500

    if op not in {"goto_zero", "goto_start", "goto_saved_start"}:
        return jsonify({"ok": False, "reason": "unknown_op", "hint": "op: save | goto_zero | goto_start | goto_saved_start"}), 400

    if _arm_grasp_worker_busy():
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "arm_job_running",
                    "hint": "Sequenza presa in corso — FERMA (hold) prima di movimenti ZERO/START manuali.",
                }
            ),
            409,
        )

    if op == "goto_zero":
        _arm_event("true_zero", "Moto manuale: posa ZERO da file")
        result = _goto_true_zero_arm_pose()
        payload, code = _true_zero_motion_http_response(result)
        return jsonify(payload), code

    if op == "goto_saved_start":
        _arm_event("true_zero", "Moto manuale: vai direttamente a START salvato")
        result = _goto_saved_start_arm_pose(ignore_disable_env=True)
        payload, code = _true_zero_motion_http_response(result)
        return jsonify(payload), code

    _arm_event("true_zero", "Moto manuale: ZERO → START (file)")
    result = _goto_true_zero_then_saved_start()
    payload, code = _true_zero_motion_http_response(result)
    return jsonify(payload), code


@APP.route("/api/arm/teach_mode", methods=["POST"])
def api_arm_teach_mode() -> Any:
    """
    Drag-teaching / impedenza bassa sul D1: richiede tabella protocollo Unitree (non in-repo).
    Vedi docs/d1_arm_protocol_feasibility.md — finché non integrata, 501.
    """
    body = request.get_json(silent=True) or {}
    enable = bool(body.get("enable", True))
    return (
        jsonify(
            {
                "ok": False,
                "reason": "teach_drag_not_implemented",
                "enable_requested": enable,
                "feasibility_doc": "docs/d1_arm_protocol_feasibility.md",
                "hint": (
                    "Workaround: posiziona il braccio (app Unitree o comandi a piccoli step), "
                    "poi Salva START o leggi angoli servo — start_alignment.json include arm_at_start."
                ),
            }
        ),
        501,
    )


@APP.route("/api/arm/drag_follow/log", methods=["GET"])
def api_arm_drag_follow_log() -> Any:
    """Ultime righe di data/drag_follow_loop.log (max_err, clip_rate per giunto, period_ms)."""
    lines = min(max(int(request.args.get("lines", 100)), 1), 400)
    loop_path = PROJECT_ROOT / DRAG_FOLLOW_LOOP_LOG_RELPATH
    out: dict[str, Any] = {"ok": True, "path": str(loop_path)}
    if loop_path.is_file():
        try:
            text = loop_path.read_text(encoding="utf-8", errors="replace").splitlines()
            out["loop_log"] = "\n".join(text[-lines:])
        except OSError as exc:
            out["loop_log"] = None
            out["error"] = repr(exc)
    else:
        out["loop_log"] = None
        out["hint"] = "File assente — avvia mirror almeno una volta."
    return jsonify(out)


@APP.route("/api/arm/drag_follow/diagnostics", methods=["GET"])
def api_arm_drag_follow_diagnostics() -> Any:
    """
    Report unico: code dei log drag, parsing euristico, JSONL strutturato, snapshot servo opzionale.
    Query: lines_process, lines_loop, lines_jsonl (limiti), servo=1 per leggere angoli DDS ora.
    """
    lp = min(max(int(request.args.get("lines_process", 220)), 10), 600)
    ll = min(max(int(request.args.get("lines_loop", 120)), 10), 500)
    lj = min(max(int(request.args.get("lines_jsonl", 180)), 10), 600)
    servo = str(request.args.get("servo", "1")).lower() in {"1", "true", "yes"}
    return jsonify(_drag_follow_diagnostics_payload(lines_process=lp, lines_loop=ll, lines_jsonl=lj, include_servo=servo))


@APP.route("/api/arm/drag_follow", methods=["GET"])
def api_arm_drag_follow_status() -> Any:
    """
    Stato mirror/drag-follow: running, PID, tempo stimato, parametri (polling dalla UI).
    """
    global DRAG_FOLLOW_LAST_END
    _drag_follow_reap()
    now = time.time()
    if DRAG_FOLLOW_PROC is None:
        out: dict[str, Any] = {
            "ok": True,
            "running": False,
            "pid": None,
            "mode": None,
            "message": "FERMO — nessun drag-follow attivo.",
        }
        le = DRAG_FOLLOW_LAST_END
        if le and (now - float(le.get("ended_at", 0))) < 180.0:
            out["last_end"] = le
            reason = str(le.get("reason", ""))
            if reason == "user_stop":
                out["message"] = "FERMO — stop manuale (drag terminato)."
            elif reason == "process_exited":
                code = le.get("exit_code")
                out["message"] = (
                    f"FERMO — processo drag-follow terminato (exit code={code}). "
                    f"Apri «Diagnostica completa» o il file NX {DRAG_FOLLOW_PROCESS_LOG_RELPATH} "
                    f"(traceback Python / ERROR dai bin C++)."
                )
        return jsonify(out)

    meta = DRAG_FOLLOW_META or {}
    pid = DRAG_FOLLOW_PROC.pid if DRAG_FOLLOW_PROC else None
    started = float(meta.get("started_at", now))
    duration = float(meta.get("duration_s", 120))
    elapsed = max(0.0, now - started)
    remaining = max(0.0, duration - elapsed)
    mode = meta.get("mode", "echo")
    params = meta.get("params") or {}
    if mode == "mirror":
        mode_detail = f"η={params.get('track_eta')}"
    elif mode == "passthrough":
        mode_detail = f"α={params.get('passthrough_alpha')} cap°={params.get('passthrough_max_step_deg')}"
    elif mode == "echo":
        mode_detail = (
            f"{params.get('hz')} Hz · lead={params.get('echo_base_lead')} "
            f"n_heavy={params.get('echo_heavy_joint_count')}"
        )
    else:
        mode_detail = f"gain={params.get('gain')}"
    return jsonify(
        {
            "ok": True,
            "running": True,
            "pid": pid,
            "mode": mode,
            "elapsed_s": round(elapsed, 1),
            "remaining_s": round(remaining, 1),
            "joints_active": list(range(7)),
            "params": params,
            "message": (
                f"ATTIVO — {mode} su tutti e 7 giunti · PID {pid} · "
                f"~{remaining:.0f}s alla fine · {mode_detail}"
            ),
        }
    )


@APP.route("/api/arm/drag_follow", methods=["POST"])
def api_arm_drag_follow() -> Any:
    """
    Avvia o ferma lo script sperimentale velocity-follow (scripts/d1_drag_follow_experimental.py).
    Richiede GO2_LOCAL=1, GO2_ENABLE_REAL_ARM=1, bin/d1_arm_* presenti.
    """
    global DRAG_FOLLOW_PROC, DRAG_FOLLOW_META, DRAG_FOLLOW_LAST_END, DRAG_FOLLOW_LOG_FP
    _drag_follow_reap()
    body = request.get_json(silent=True) or {}
    enable = bool(body.get("enable", True))

    if not enable:
        skip_hold = str(body.get("hold_after_stop", "")).lower() in {"0", "false", "no"}
        drag_out = _drag_follow_stop_if_running(hold_after_stop=not skip_hold)
        hold_after = drag_out.get("hold_after_stop")
        return jsonify(
            {
                "ok": True,
                "stopped": True,
                "drag_follow_was_running": drag_out.get("drag_follow_stopped"),
                "message": (
                    "Drag-follow fermato."
                    if drag_out.get("drag_follow_stopped")
                    else "Nessun drag-follow attivo su questa sessione."
                ),
                "hold_after_stop": hold_after,
                "process_log_hint": (
                    f"Su NX leggi {DRAG_FOLLOW_PROCESS_LOG_RELPATH} se il mirror non parte "
                    "(ERROR feedback / bin mancanti)."
                ),
            }
        )

    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return jsonify({"ok": False, "reason": "GO2_ENABLE_REAL_ARM required"}), 403
    if not GO2_LOCAL:
        return jsonify({"ok": False, "reason": "GO2_LOCAL=1 required"}), 400

    fb_h = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
    cmd_h = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not fb_h.is_file() or not cmd_h.is_file():
        return jsonify({"ok": False, "reason": "missing bin/d1_arm_feedback_helper or d1_arm_command"}), 503

    script = PROJECT_ROOT / "scripts" / "d1_drag_follow_experimental.py"
    if not script.is_file():
        return jsonify({"ok": False, "reason": "scripts/d1_drag_follow_experimental.py missing"}), 500

    if DRAG_FOLLOW_PROC is not None and DRAG_FOLLOW_PROC.poll() is None:
        return jsonify({"ok": False, "reason": "drag_follow_already_running", "pid": DRAG_FOLLOW_PROC.pid}), 409

    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    _drag_follow_close_log_fp()

    gain = float(body.get("gain", 0.18))
    hz = float(body.get("hz", 14))
    seconds = float(body.get("seconds", 240))
    domain = int(body.get("domain", GO2_DDS_DOMAIN))
    mode = str(body.get("mode", "echo")).strip().lower()
    if mode not in ("mirror", "assist", "passthrough", "echo"):
        mode = "echo"
    track_eta = float(body.get("track_eta", 0.56))
    mirror_max = float(body.get("mirror_max_step_deg", 1.2))
    smooth = float(body.get("smooth", 0.55))
    max_step = float(body.get("max_step_deg", 0.55))
    deadband = float(body.get("deadband_deg", 0.04))
    cmd_delay = int(body.get("command_delay_ms", 30))
    gripper_mirror_scale = float(body.get("gripper_mirror_scale", 1.0))
    gripper_mirror_scale = max(0.2, min(1.5, gripper_mirror_scale))
    mirror_base_count = int(body.get("mirror_base_count", 3))
    mirror_base_count = max(0, min(7, mirror_base_count))
    mirror_base_eta_scale = float(body.get("mirror_base_eta_scale", 3.4))
    mirror_base_eta_scale = max(1.0, min(4.85, mirror_base_eta_scale))
    mirror_base_cap_scale = float(body.get("mirror_base_cap_scale", 1.85))
    mirror_base_cap_scale = max(1.0, min(3.2, mirror_base_cap_scale))
    passthrough_alpha = float(body.get("passthrough_alpha", 0.9))
    passthrough_alpha = max(0.08, min(1.0, passthrough_alpha))
    passthrough_max = float(body.get("passthrough_max_step_deg", 7.0))
    passthrough_max = max(0.4, min(25.0, passthrough_max))
    echo_base_lead = float(body.get("echo_base_lead", 0.55))
    echo_base_lead = max(0.0, min(3.0, echo_base_lead))
    echo_lead_cap = float(body.get("echo_lead_cap_deg", 5.0))
    echo_lead_cap = max(0.2, min(20.0, echo_lead_cap))
    echo_heavy_n = int(body.get("echo_heavy_joint_count", 4))
    echo_heavy_n = max(0, min(7, echo_heavy_n))
    echo_dec_h = int(body.get("echo_decimals_heavy", 5))
    echo_dec_h = max(2, min(8, echo_dec_h))
    echo_dec_r = int(body.get("echo_decimals_rest", 3))
    echo_dec_r = max(2, min(8, echo_dec_r))

    proc_log_path = PROJECT_ROOT / DRAG_FOLLOW_PROCESS_LOG_RELPATH
    log_fp = open(proc_log_path, "a", encoding="utf-8", buffering=1)
    params_preview = json.dumps(
        {
            "mode": mode,
            "track_eta": track_eta,
            "mirror_max_step_deg": mirror_max,
            "passthrough_alpha": passthrough_alpha,
            "passthrough_max_step_deg": passthrough_max,
            "echo_base_lead": echo_base_lead,
            "echo_lead_cap_deg": echo_lead_cap,
            "echo_heavy_joint_count": echo_heavy_n,
            "hz": hz,
            "cmd_delay_ms": cmd_delay,
            "mirror_base_eta_scale": mirror_base_eta_scale,
            "diag_jsonl": DRAG_FOLLOW_DIAG_JSONL_RELPATH,
        },
        separators=(",", ":"),
    )
    log_fp.write(f"\n# --- spawn {now_iso()} {params_preview} ---\n")

    try:
        DRAG_FOLLOW_PROC = subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--domain",
                str(domain),
                "--mode",
                mode,
                "--track-eta",
                str(track_eta),
                "--mirror-max-step-deg",
                str(mirror_max),
                "--gain",
                str(gain),
                "--hz",
                str(hz),
                "--seconds",
                str(seconds),
                "--smooth",
                str(smooth),
                "--max-step-deg",
                str(max_step),
                "--deadband-deg",
                str(deadband),
                "--command-delay-ms",
                str(cmd_delay),
                "--gripper-mirror-scale",
                str(gripper_mirror_scale),
                "--mirror-base-count",
                str(mirror_base_count),
                "--mirror-base-eta-scale",
                str(mirror_base_eta_scale),
                "--mirror-base-cap-scale",
                str(mirror_base_cap_scale),
                "--passthrough-alpha",
                str(passthrough_alpha),
                "--passthrough-max-step-deg",
                str(passthrough_max),
                "--echo-base-lead",
                str(echo_base_lead),
                "--echo-lead-cap-deg",
                str(echo_lead_cap),
                "--echo-heavy-count",
                str(echo_heavy_n),
                "--echo-decimals-heavy",
                str(echo_dec_h),
                "--echo-decimals-rest",
                str(echo_dec_r),
                "--log-file",
                DRAG_FOLLOW_LOOP_LOG_RELPATH,
                "--log-interval-s",
                "1",
                "--diag-jsonl",
                DRAG_FOLLOW_DIAG_JSONL_RELPATH,
            ],
            cwd=str(PROJECT_ROOT),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        DRAG_FOLLOW_LOG_FP = log_fp
        log_fp.write(f"# pid={DRAG_FOLLOW_PROC.pid}\n")
        log_fp.flush()
    except Exception as exc:
        try:
            log_fp.close()
        except Exception:
            pass
        return jsonify({"ok": False, "reason": repr(exc)}), 502

    params_out = {
        "mode": mode,
        "track_eta": track_eta,
        "mirror_max_step_deg": mirror_max,
        "gripper_mirror_scale": gripper_mirror_scale,
        "mirror_base_count": mirror_base_count,
        "mirror_base_eta_scale": mirror_base_eta_scale,
        "mirror_base_cap_scale": mirror_base_cap_scale,
        "passthrough_alpha": passthrough_alpha,
        "passthrough_max_step_deg": passthrough_max,
        "echo_base_lead": echo_base_lead,
        "echo_lead_cap_deg": echo_lead_cap,
        "echo_heavy_joint_count": echo_heavy_n,
        "echo_decimals_heavy": echo_dec_h,
        "echo_decimals_rest": echo_dec_r,
        "gain": gain,
        "hz": hz,
        "seconds": seconds,
        "smooth": smooth,
        "max_step_deg": max_step,
        "deadband_deg": deadband,
        "command_delay_ms": cmd_delay,
        "log_file": DRAG_FOLLOW_LOOP_LOG_RELPATH,
        "diag_jsonl": DRAG_FOLLOW_DIAG_JSONL_RELPATH,
    }
    DRAG_FOLLOW_META = {
        "started_at": time.time(),
        "duration_s": seconds,
        "mode": mode,
        "params": params_out,
    }
    DRAG_FOLLOW_LAST_END = None

    return jsonify(
        {
            "ok": True,
            "pid": DRAG_FOLLOW_PROC.pid,
            "warning": f"{mode} su 7 giunti — Stop drag per fermare",
            "script": str(script),
            "message": f"Drag avviato (PID {DRAG_FOLLOW_PROC.pid}, mode={mode}, {hz} Hz). Stato sotto.",
            "params": params_out,
            "process_log": str(proc_log_path),
        }
    )


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
        "safe_note": (
            "Enable GO2_ENABLE_BASE_MOTION=1 on the NX and POST /api/base/accompany_mode "
            "with mode stand_up or crouch (Sport StandUp/BalanceStand or StandDown)."
        ),
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
        # Python cyclonedds: optional on NX/Jetson (often unreliable); arm motion uses C++ helpers only.
        sdk_python_ok = modules.get("cyclonedds", {}).get("ok") and modules.get("unitree_sdk2py", {}).get("ok")
        helper_pub = PROJECT_ROOT / "bin" / "d1_arm_command"
        helper_fb = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
        d1_binaries_ok = helper_pub.exists() and helper_fb.exists()
        # On Jetson, compiled helpers suffice for arm; Python cyclonedds may be absent.
        stack_any_ok = bool(sdk_python_ok or d1_binaries_ok)
        return {
            "ok": stack_any_ok,
            "python_dds_sdk_ok": bool(sdk_python_ok),
            "python_cyclonedds_required_for_d1_arm": False,
            "arm_motion_note": "D1 arm commands use subprocess → bin/d1_arm_command (C++ Unitree SDK); pip cyclonedds not required.",
            "real_arm_enabled": os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"},
            "dds_domain": GO2_DDS_DOMAIN,
            "dds_interface": GO2_DDS_INTERFACE or "default",
            "d1_helper": str(helper_pub),
            "d1_feedback_helper": str(helper_fb),
            "d1_helper_ok": helper_pub.exists(),
            "d1_feedback_helper_ok": helper_fb.exists(),
            "d1_binaries_ok": d1_binaries_ok,
            "modules": modules,
            "safety": "Real arm execution requires GO2_ENABLE_REAL_ARM=1 and a valid IK plan. DDS probe/lowlevel Python SDK optional.",
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def _run_d1_messages(
    messages: list[dict[str, Any]],
    delay_ms: int = 900,
    *,
    ignore_abort: bool = False,
    post_hold: bool | None = None,
) -> dict[str, Any]:
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.exists():
        return {"ok": False, "reason": f"D1 DDS helper missing: {helper}"}
    chunk_size = int(os.environ.get("D1_ABORTABLE_CHUNK_MESSAGES", "8"))
    abortable = os.environ.get("D1_ABORTABLE_MOTION_CHUNKS", "1").lower() in {"1", "true", "yes"}
    chunks: list[list[dict[str, Any]]]
    if abortable and len(messages) > max(3, chunk_size):
        init = messages[:1]
        body = messages[1:]
        chunks = [init + body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
    else:
        chunks = [messages]
    outs: list[str] = []
    errs: list[str] = []
    for idx, chunk in enumerate(chunks):
        if ARM_GRASP_ABORT.is_set() and not ignore_abort:
            return {
                "ok": False,
                "aborted": True,
                "reason": "aborted_before_d1_chunk",
                "chunk_index": idx,
                "chunks_total": len(chunks),
                "helper_stdout": "\n".join(outs)[-4000:],
                "helper_stderr": "\n".join(errs)[-2000:],
            }
        stdin = "\n".join(json.dumps(msg, separators=(",", ":")) for msg in chunk) + "\n"
        result = subprocess.run(
            [str(helper), str(GO2_DDS_DOMAIN), str(delay_ms)],
            cwd=str(PROJECT_ROOT),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=max(12.0, (delay_ms / 1000.0 + 0.4) * len(chunk)),
        )
        outs.append(result.stdout)
        errs.append(result.stderr)
        if result.returncode != 0:
            break
    out = {
        "ok": result.returncode == 0,
        "topic": "rt/arm_Command",
        "messages": messages,
        "abortable_chunks": abortable,
        "chunks_total": len(chunks),
        "helper_returncode": result.returncode,
        "helper_stdout": "\n".join(outs)[-4000:],
        "helper_stderr": "\n".join(errs)[-2000:],
    }
    do_post = (
        (post_hold if post_hold is not None else False)
        and out["ok"]
        and not (ARM_GRASP_ABORT.is_set() and not ignore_abort)
    )
    if do_post:
        reps = int(os.environ.get("D1_POST_MOTION_HOLD_REPEATS", "8"))
        dms = int(os.environ.get("D1_POST_MOTION_HOLD_DELAY_MS", "55"))
        out["post_hold"] = publish_d1_hold_current(repeats=max(3, reps), delay_ms=max(35, dms))
    return out


def publish_d1_hold_current(*, repeats: int | None = None, delay_ms: int | None = None) -> dict[str, Any]:
    """
    Ripete la posa servo letta da feedback: riduce cedimenti/creep tra un comando e l'altro.
    """
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": False, "reason": "GO2_ENABLE_REAL_ARM is not enabled"}
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.exists():
        return {"ok": False, "reason": f"D1 DDS helper missing: {helper}"}
    rpt = repeats if repeats is not None else int(os.environ.get("D1_HOLD_REPEATS", "14"))
    dms = delay_ms if delay_ms is not None else int(os.environ.get("D1_HOLD_DELAY_MS", "95"))
    cur = _read_d1_servo_angles()
    if cur is None:
        return {"ok": False, "reason": "No D1 servo feedback; cannot hold"}
    seq = int(time.time()) % 100000
    messages: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    for i in range(max(3, rpt)):
        angles = {f"angle{idx}": round(float(cur[idx]), 3) for idx in range(7)}
        angles["mode"] = 1
        messages.append({"seq": seq + 1 + i, "address": 1, "funcode": 2, "data": angles})
    result = _run_d1_messages(messages, delay_ms=max(40, dms), ignore_abort=True)
    return {
        **result,
        "mode": "hold_current_pose",
        "hold_repeats": rpt,
        "hold_delay_ms": dms,
        "snapshot_deg": [round(float(v), 2) for v in cur[:7]],
    }


def _kill_d1_motion_helpers() -> dict[str, Any]:
    """
    Best-effort stop for an in-flight d1_arm_command subprocess.
    Emergency hold must be allowed to preempt a long chunk already publishing.
    """
    if os.name == "nt":
        return {"ok": True, "skipped": True, "reason": "pkill_unavailable_on_windows"}
    try:
        result = subprocess.run(
            ["pkill", "-f", "d1_arm_command"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        return {
            "ok": result.returncode in (0, 1),
            "returncode": result.returncode,
            "stdout": result.stdout[-500:],
            "stderr": result.stderr[-500:],
        }
    except Exception as exc:
        return {"ok": False, "reason": repr(exc)}


def _arm_hold_keepalive(reason: str) -> dict[str, Any]:
    """Pulse breve di hold durante attese/detection: evita drift o cedimenti tra due movimenti."""
    if os.environ.get("GO2_GRASP_HOLD_KEEPALIVE", "1").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_GRASP_HOLD_KEEPALIVE disabled"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off"}
    _arm_event("hold", f"Hold keepalive: {reason}")
    return publish_d1_hold_current(repeats=3, delay_ms=70)


def _read_d1_servo_angles_median(samples: int | None = None, delay_s: float | None = None) -> list[float] | None:
    """Mediana su più campioni: meno rumore del singolo read quando si riallinea la traiettoria."""
    n = int(samples if samples is not None else int(os.environ.get("D1_FEEDBACK_MEDIAN_SAMPLES", "5")))
    dt = float(delay_s if delay_s is not None else float(os.environ.get("D1_FEEDBACK_MEDIAN_GAP_S", "0.042")))
    rows: list[list[float]] = []
    for _ in range(max(3, n)):
        cur = _read_d1_servo_angles()
        if cur is not None and len(cur) >= 7:
            rows.append([float(cur[i]) for i in range(7)])
        time.sleep(max(0.012, dt))
    if len(rows) < 2:
        return rows[0] if rows else None
    out = []
    for i in range(7):
        out.append(statistics.median([r[i] for r in rows]))
    return out


def _read_d1_servo_angles_stable(samples: int | None = None, delay_s: float | None = None) -> list[float] | None:
    """Più letture DDS ravvicinate — riduce punto di partenza «fantasma» prima delle spline."""
    n = int(samples if samples is not None else int(os.environ.get("D1_FEEDBACK_SAMPLES", "5")))
    dt = float(delay_s if delay_s is not None else float(os.environ.get("D1_FEEDBACK_SAMPLE_GAP_S", "0.05")))
    last: list[float] | None = None
    for _ in range(max(2, n)):
        cur = _read_d1_servo_angles()
        if cur is not None and len(cur) >= 7:
            last = cur
        time.sleep(max(0.012, dt))
    return last


def _read_d1_servo_angles() -> list[float] | None:
    helper = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
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


def _arm_snapshot_from_servo_deg(servo_deg: list[float]) -> dict[str, Any]:
    """Schema identico a arm_at_start ma da angoli (gradi) scelti dall operatore (slider)."""
    sd = [round(float(servo_deg[i]), 3) for i in range(min(7, len(servo_deg)))]
    while len(sd) < 7:
        sd.append(sd[-1])
    joints_rad = [round(math.radians(sd[i]), 6) for i in range(6)]
    out: dict[str, Any] = {
        "feedback_ok": True,
        "servo_deg": sd,
        "joints_rad": joints_rad,
        "ik_seed_note": "servo_deg from UI sliders — verify against hardware.",
    }
    if len(sd) >= 7:
        out["gripper_deg"] = sd[6]
    return out


def _arm_at_start_snapshot() -> dict[str, Any]:
    """
    Pose braccio D1 per seed IK / ripetibilità insieme allo snapshot AprilTag (START).
    Angoli servo in gradi da feedback; joints_rad = primi 6 giunti in radianti per il template cinematica.
    """
    cur = _read_d1_servo_angles()
    if cur is None or len(cur) < 6:
        return {
            "feedback_ok": False,
            "servo_deg": None,
            "joints_rad": None,
            "gripper_deg": None,
            "note": "No D1 servo feedback (bin/d1_arm_feedback_helper or DDS).",
        }
    servo_deg = [round(float(cur[i]), 3) for i in range(min(7, len(cur)))]
    joints_rad = [round(math.radians(servo_deg[i]), 6) for i in range(6)]
    out: dict[str, Any] = {
        "feedback_ok": True,
        "servo_deg": servo_deg,
        "joints_rad": joints_rad,
        "ik_seed_note": "joints_rad: first 6 joints rad for arm_kinematics_d1_template; servo_deg[6] is gripper.",
    }
    if len(servo_deg) >= 7:
        out["gripper_deg"] = servo_deg[6]
    return out


def _goto_saved_start_arm_pose(*, ignore_disable_env: bool = False) -> dict[str, Any]:
    """
    Riporta il braccio alla posizione servo in ``data/start_alignment.json``.
    Nella sequenza grasp è disabilitabile con ``GO2_GRASP_GOTO_SAVED_START=0``;
    il pulsante manuale "Vai a START" invece deve ignorare quel flag.
    """
    if ARM_GRASP_ABORT.is_set():
        _clear_stale_abort_for_manual_motion()
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "skipped": False, "reason": "aborted_before_goto_start"}
    if (
        not ignore_disable_env
        and os.environ.get("GO2_GRASP_GOTO_SAVED_START", "1").lower() not in {"1", "true", "yes"}
    ):
        return {"ok": True, "skipped": True, "reason": "GO2_GRASP_GOTO_SAVED_START disabled"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    if not ALIGNMENT_START_PATH.is_file():
        return {"ok": True, "skipped": True, "reason": "no start_alignment.json — tap Salva START in pose operativa"}
    try:
        data = json.loads(ALIGNMENT_START_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": f"START json read failed: {exc!r}"}
    arm = data.get("arm_at_start") or {}
    if not arm.get("feedback_ok"):
        return {"ok": True, "skipped": True, "reason": "saved START has no arm feedback"}
    jr = arm.get("joints_rad")
    if not isinstance(jr, list) or len(jr) < 6:
        return {"ok": True, "skipped": True, "reason": "saved START missing joints_rad"}
    stages = [{"stage": "goto_saved_start_align", "joints_rad": [float(v) for v in jr[:6]]}]
    prehold = None
    if os.environ.get("D1_START_PREHOLD", "1").lower() in {"1", "true", "yes"}:
        prehold = publish_d1_hold_current(
            repeats=int(os.environ.get("D1_START_PREHOLD_REPEATS", "10")),
            delay_ms=int(os.environ.get("D1_START_PREHOLD_DELAY_MS", "55")),
        )
    try:
        delay_ms = int(os.environ.get("D1_START_ALIGN_DELAY_MS", str(D1_SEARCH_COMMAND_DELAY_MS)))
        messages, sent = _stage_messages(stages, close_gripper=False, max_step_deg=D1_START_ALIGN_MAX_STEP_DEG)
        result = _run_d1_messages(messages, delay_ms=max(120, delay_ms), post_hold=True)
        out = {
            **result,
            "skipped": False,
            "sent_stages": sent,
            "start_file": str(ALIGNMENT_START_PATH),
            "prehold": prehold,
        }
        if isinstance(data.get("saved_at"), str):
            out["saved_at"] = data["saved_at"]
        return out
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": repr(exc)}


def _goto_fold_arm_pose() -> dict[str, Any]:
    """
    Posizione iniziale «chiusa»: giunti come ARM_FOLD_POSE (template D1), gripper come negli altri movimenti non-presà.
    Disabilitabile con ``GO2_GRASP_START_FOLD=0``.
    """
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "skipped": False, "reason": "aborted_before_goto_fold"}
    if os.environ.get("GO2_GRASP_START_FOLD", "1").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_GRASP_START_FOLD disabled"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from arm_kinematics_d1_template import ARM_FOLD_POSE

        jr = [float(v) for v in ARM_FOLD_POSE[:6]]
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": f"fold_pose_import: {exc!r}"}
    stages = [{"stage": "goto_fold_compact", "joints_rad": jr}]
    try:
        delay_ms = int(os.environ.get("D1_FOLD_DELAY_MS", str(D1_SEARCH_COMMAND_DELAY_MS)))
        messages, sent = _stage_messages(stages, close_gripper=False, max_step_deg=D1_FOLD_MAX_STEP_DEG)
        result = _run_d1_messages(messages, delay_ms=max(120, delay_ms))
        return {**result, "skipped": False, "sent_stages": sent, "pose": "ARM_FOLD_POSE"}
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": repr(exc)}


def _joints_rad_from_arm_blob(blob: dict[str, Any]) -> list[float] | None:
    """Accetta dict tipo arm_at_start o JSON con servo_deg / joints_rad."""
    if not isinstance(blob, dict):
        return None
    jr = blob.get("joints_rad")
    if isinstance(jr, list) and len(jr) >= 6:
        try:
            return [float(jr[i]) for i in range(6)]
        except (TypeError, ValueError):
            pass
    sd = blob.get("servo_deg")
    if isinstance(sd, list) and len(sd) >= 6:
        try:
            return [math.radians(float(sd[i])) for i in range(6)]
        except (TypeError, ValueError):
            pass
    return None


def _load_true_zero_joints_rad() -> tuple[list[float] | None, dict[str, Any]]:
    if not TRUE_ZERO_POSE_PATH.is_file():
        return None, {}
    try:
        data = json.loads(TRUE_ZERO_POSE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    arm = data.get("arm") or data.get("arm_at_start")
    if isinstance(arm, dict):
        z = _joints_rad_from_arm_blob(arm)
        if z:
            return z, data
    z = _joints_rad_from_arm_blob(data)
    if z:
        return z, data
    return None, data


def _save_true_zero_snapshot(
    *,
    servo_deg_override: list[float] | None = None,
    angle2_deg: float | None = None,
) -> dict[str, Any]:
    """
    Posa ZERO su disco. Opzioni:
    - ``servo_deg_override``: 6–7 angoli espliciti (gradi) se il feedback al salvataggio non è attendibile.
    - ``angle2_deg``: forza solo il terzo giunto (angle2, indice 2) — es. 82° per ripiegatura chiusa.
    """
    arm: dict[str, Any]
    if servo_deg_override is not None and len(servo_deg_override) >= 6:
        sd = [round(float(servo_deg_override[i]), 3) for i in range(min(7, len(servo_deg_override)))]
        while len(sd) < 7:
            sd.append(sd[-1])
        joints_rad = [round(math.radians(sd[i]), 6) for i in range(6)]
        arm = {
            "feedback_ok": True,
            "servo_deg": sd,
            "joints_rad": joints_rad,
            "ik_seed_note": "servo_deg override from API/UI — verify on hardware.",
        }
        if len(sd) >= 7:
            arm["gripper_deg"] = sd[6]
    else:
        cur = _read_d1_servo_angles_stable()
        if cur is None or len(cur) < 6:
            return {
                "ok": False,
                "reason": "no_servo_feedback_for_true_zero",
                "hint": "Usa servo_deg override oppure verifica d1_arm_feedback_helper.",
            }
        sd = [round(float(cur[i]), 3) for i in range(min(7, len(cur)))]
        while len(sd) < 7:
            sd.append(sd[-1])
        if angle2_deg is not None:
            sd[2] = round(float(angle2_deg), 3)
        joints_rad = [round(math.radians(sd[i]), 6) for i in range(6)]
        arm = {
            "feedback_ok": True,
            "servo_deg": sd,
            "joints_rad": joints_rad,
            "ik_seed_note": "joints_rad: first 6 joints rad; servo_deg[6] gripper. angle2_deg applied if set.",
        }
        if angle2_deg is not None:
            arm["angle2_deg_forced"] = round(float(angle2_deg), 3)
        if len(sd) >= 7:
            arm["gripper_deg"] = sd[6]
    payload = {
        "label": "true_zero",
        "saved_at": now_iso(),
        "note": (
            "Calibrated folded zero. Trajectory uses measured joint feedback between segments (no «phantom» pose). "
            "Optional angle2_deg forces third joint (e.g. 82°)."
        ),
        "arm": arm,
    }
    TRUE_ZERO_POSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRUE_ZERO_POSE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "saved_to": str(TRUE_ZERO_POSE_PATH), "pose": payload}


def _maybe_true_zero_prehold() -> dict[str, Any] | None:
    if os.environ.get("GO2_TRUE_ZERO_PREHOLD", "1").lower() not in {"1", "true", "yes"}:
        return None
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return None
    rpt = int(os.environ.get("GO2_TRUE_ZERO_PREHOLD_REPEATS", "6"))
    dms = int(os.environ.get("GO2_TRUE_ZERO_PREHOLD_DELAY_MS", "90"))
    return publish_d1_hold_current(repeats=max(3, rpt), delay_ms=max(40, dms))


def _goto_true_zero_arm_pose() -> dict[str, Any]:
    if ARM_GRASP_ABORT.is_set():
        _clear_stale_abort_for_manual_motion()
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "skipped": False, "reason": "aborted_before_goto_true_zero"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    z, _ = _load_true_zero_joints_rad()
    if z is None:
        return {
            "ok": False,
            "skipped": False,
            "reason": "no_true_zero_json_or_invalid",
            "path": str(TRUE_ZERO_POSE_PATH),
        }
    stages = [{"stage": "goto_true_zero", "joints_rad": z}]
    try:
        delay_ms = _zero_transition_command_delay_ms(for_true_zero_only=True)
        ease = _zero_transition_interp_profile()
        messages, sent = _stage_messages(
            stages,
            close_gripper=False,
            max_step_deg=D1_ZERO_TRANSITION_MAX_STEP_DEG,
            ease_profile=ease,
        )
        prehold = publish_d1_hold_current(
            repeats=int(os.environ.get("D1_ZERO_PREHOLD_REPEATS", "8")),
            delay_ms=int(os.environ.get("D1_ZERO_PREHOLD_DELAY_MS", "55")),
        )
        result = _run_d1_messages(messages, delay_ms=max(75, delay_ms), post_hold=True)
        return {
            **result,
            "skipped": False,
            "sent_stages": sent,
            "prehold": prehold,
            "true_zero_file": str(TRUE_ZERO_POSE_PATH),
            "zero_transition_delay_ms": delay_ms,
        }
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": repr(exc)}


def _goto_true_zero_then_saved_start() -> dict[str, Any]:
    if ARM_GRASP_ABORT.is_set():
        _clear_stale_abort_for_manual_motion()
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "skipped": False, "reason": "aborted_before_zero_to_start"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    zz, _ = _load_true_zero_joints_rad()
    if zz is None:
        return {"ok": False, "skipped": False, "reason": "no_true_zero_json", "path": str(TRUE_ZERO_POSE_PATH)}
    if not ALIGNMENT_START_PATH.is_file():
        return {"ok": False, "skipped": False, "reason": "no start_alignment.json — Salva START prima"}
    try:
        data = json.loads(ALIGNMENT_START_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": f"START json read failed: {exc!r}"}
    arm = data.get("arm_at_start") or {}
    if not arm.get("feedback_ok"):
        return {"ok": False, "skipped": False, "reason": "saved START has no arm feedback"}
    sr = _joints_rad_from_arm_blob(arm)
    if sr is None:
        return {"ok": False, "skipped": False, "reason": "saved START missing joints_rad / servo_deg"}
    pre = _maybe_true_zero_prehold()
    delay_ms = _zero_transition_command_delay_ms(for_true_zero_only=False)
    ease = _zero_transition_interp_profile()
    split = os.environ.get("D1_ZERO_TO_START_SPLIT", "1").lower() in {"1", "true", "yes"}
    try:
        if split:
            stg_z = [{"stage": "goto_true_zero", "joints_rad": zz}]
            messages_z, sent_z = _stage_messages(
                stg_z, close_gripper=False, max_step_deg=D1_ZERO_TRANSITION_MAX_STEP_DEG, ease_profile=ease
            )
            rz = _run_d1_messages(messages_z, delay_ms=max(75, delay_ms), post_hold=True)
            settle_rep = int(os.environ.get("D1_ZERO_TO_START_SETTLE_REPEATS", "4"))
            settle_dms = int(os.environ.get("D1_ZERO_TO_START_SETTLE_DELAY_MS", "40"))
            settle = publish_d1_hold_current(repeats=max(4, settle_rep), delay_ms=max(35, settle_dms))
            stg_s = [{"stage": "goto_saved_start_align", "joints_rad": sr}]
            messages_s, sent_s = _stage_messages(
                stg_s, close_gripper=False, max_step_deg=D1_ZERO_TRANSITION_MAX_STEP_DEG, ease_profile=ease
            )
            rs = _run_d1_messages(messages_s, delay_ms=max(75, delay_ms), post_hold=True)
            ok = bool(rz.get("ok")) and bool(rs.get("ok"))
            out: dict[str, Any] = {
                "ok": ok,
                "skipped": False,
                "split_segments": True,
                "segment_to_zero": rz,
                "segment_to_start": rs,
                "sent_stages": [*sent_z, *sent_s],
                "settle_between": settle,
                "prehold": pre,
                "start_file": str(ALIGNMENT_START_PATH),
                "true_zero_file": str(TRUE_ZERO_POSE_PATH),
                "zero_transition_delay_ms": delay_ms,
            }
            if isinstance(data.get("saved_at"), str):
                out["start_saved_at"] = data["saved_at"]
            return out
        stages = [
            {"stage": "goto_true_zero", "joints_rad": zz},
            {"stage": "goto_saved_start_align", "joints_rad": sr},
        ]
        messages, sent = _stage_messages(
            stages, close_gripper=False, max_step_deg=D1_ZERO_TRANSITION_MAX_STEP_DEG, ease_profile=ease
        )
        result = _run_d1_messages(messages, delay_ms=max(75, delay_ms), post_hold=True)
        out = {
            **result,
            "skipped": False,
            "split_segments": False,
            "sent_stages": sent,
            "prehold": pre,
            "start_file": str(ALIGNMENT_START_PATH),
            "true_zero_file": str(TRUE_ZERO_POSE_PATH),
            "zero_transition_delay_ms": delay_ms,
        }
        if isinstance(data.get("saved_at"), str):
            out["start_saved_at"] = data["saved_at"]
        return out
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": repr(exc), "prehold": pre}


def _plan_has_apriltag_detection(plan: dict[str, Any]) -> bool:
    cands = plan.get("candidates") or {}
    for key in ("0", "6"):
        tags = ((cands.get(key) or {}).get("tags") or {}).get("tags") or []
        if tags:
            return True
    return False


def _wait_for_apriltag_detection() -> dict[str, Any]:
    """
    Attende almeno un AprilTag (famiglia planner: tag25h9 tracciati) su camera polso o RealSense.
    Disabilitabile con ``GO2_GRASP_WAIT_TAG_BEFORE_START_POSE=0``. Timeout: ``GO2_GRASP_TAG_WAIT_S`` (default 90).
    """
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry) — tag wait skipped"}
    if os.environ.get("GO2_GRASP_WAIT_TAG_BEFORE_START_POSE", "1").lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "GO2_GRASP_WAIT_TAG_BEFORE_START_POSE disabled"}
    wait_s = _tune_float("tag_wait_s", "GO2_GRASP_TAG_WAIT_S", 90.0)
    deadline = time.time() + wait_s
    last = _box_plan_snapshot()
    t_wait_start = time.time()
    _grasp_live_phase(f"Attesa primo AprilTag (max {int(wait_s)}s) — mostra «Camere & AprilTag»…")
    next_phase_ping = t_wait_start
    next_hold_ping = t_wait_start + 1.0
    while time.time() < deadline:
        if ARM_GRASP_ABORT.is_set():
            return {"ok": False, "reason": "aborted_while_waiting_tags", "last_plan": last}
        now = time.time()
        if now >= next_phase_ping:
            next_phase_ping = now + 2.8
            elapsed = int(now - t_wait_start)
            _grasp_live_phase(f"Attesa AprilTag… {elapsed}s / {int(wait_s)}s (polso o RealSense)")
        if now >= next_hold_ping:
            next_hold_ping = now + 1.8
            _arm_hold_keepalive("attesa AprilTag, nessun movimento richiesto")
        plan = _box_plan_snapshot()
        if _plan_has_apriltag_detection(plan):
            _grasp_live_phase("AprilTag rilevato — proseguo con ricerca/IK…")
            return {
                "ok": True,
                "wait_s_elapsed": round(time.time() - t_wait_start, 2),
                "plan_snapshot": plan,
            }
        last = plan
        if not _sleep_abortable(0.25):
            return {"ok": False, "reason": "aborted_while_waiting_tags", "last_plan": last}
    return {
        "ok": False,
        "reason": "apriltag_detection_timeout",
        "wait_s": wait_s,
        "last_plan": last,
    }


def _interpolate_angles(
    start: list[float],
    target: list[float],
    *,
    max_step_deg: list[float],
    ease_profile: str | None = None,
) -> list[list[float]]:
    deltas = [abs(t - s) / step for s, t, step in zip(start, target, max_step_deg)]
    count = max(1, int(math.ceil(max(deltas, default=1.0))))
    profile = (ease_profile if ease_profile is not None else os.environ.get("D1_INTERP_EASE", "linear")).strip().lower()

    def ease_u(u: float) -> float:
        u = max(0.0, min(1.0, u))
        if profile in {"smoothstep", "smooth", "s"}:
            return u * u * (3.0 - 2.0 * u)
        if profile in {"cosine", "cos"}:
            return 0.5 - 0.5 * math.cos(math.pi * u)
        return u

    return [
        [round(s + (t - s) * ease_u(idx / count), 3) for s, t in zip(start, target)]
        for idx in range(1, count + 1)
    ]


def _stage_messages(
    stages: list[dict[str, Any]],
    *,
    close_gripper: bool,
    max_step_deg: list[float],
    ease_profile: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    messages = []
    sent = []
    seq = int(time.time()) % 100000
    messages.append({"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}})
    rehome = os.environ.get("D1_MOTION_REHOME_FEEDBACK", "1").lower() in {"1", "true", "yes"}
    use_stable = os.environ.get("D1_MOTION_STABLE_START", "1").lower() in {"1", "true", "yes"}
    use_median_start = os.environ.get("D1_START_USE_MEDIAN", "1").lower() in {"1", "true", "yes"}
    use_median_rehome = os.environ.get("D1_REHOME_USE_MEDIAN", "1").lower() in {"1", "true", "yes"}
    read_pose = _read_d1_servo_angles_stable if use_stable else _read_d1_servo_angles
    if use_median_start:
        current_fb = _read_d1_servo_angles_median()
        if current_fb is None:
            current_fb = read_pose()
    else:
        current_fb = read_pose()
    current = current_fb
    if current is None:
        raise RuntimeError("No D1 servo feedback; refusing motion without current arm pose")
    current = [round(float(current[i]), 3) for i in range(7)]
    point_repeat = max(1, int(os.environ.get("D1_PATH_POINT_REPEAT", "1")))
    for offset, stage in enumerate(stages, start=1):
        joints = [float(v) for v in stage.get("joints_rad", [])]
        if len(joints) < 6:
            raise ValueError(f"Invalid joints in stage {stage.get('stage')}")
        target = [round(max(-135.0, min(135.0, math.degrees(v))), 3) for v in joints[:6]]
        if close_gripper:
            target.append(56.0 if stage.get("stage") in {"pre_grasp", "approach"} else 5.0)
        else:
            # Non forzare 56° sul gripper: mantiene l'angolo attuale — evita comandi spurî sui giunti braccio.
            target.append(round(float(current[6]), 3))
        path = _interpolate_angles(current, target, max_step_deg=max_step_deg, ease_profile=ease_profile)
        for point in path:
            for _ in range(point_repeat):
                angles = {f"angle{idx}": point[idx] for idx in range(7)}
                angles["mode"] = 1
                messages.append({"seq": seq + len(messages), "address": 1, "funcode": 2, "data": angles})
        if rehome:
            if use_median_rehome:
                fb = _read_d1_servo_angles_median()
            else:
                fb = _read_d1_servo_angles_stable()
            if fb is not None and len(fb) >= 7:
                current = [round(float(fb[i]), 3) for i in range(7)]
            else:
                current = [round(float(x), 3) for x in target]
        else:
            current = [round(float(x), 3) for x in target]
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
    _BOX_TAG_IDS_HINT = frozenset({0, 1, 2, 3})
    tags = (front_plan.get("tags") or {}).get("tags") or []
    poses = (front_plan.get("poses") or {}).get("poses") or []
    cx, cy = 320.0, 240.0
    w, h = 640.0, 480.0
    if not tags:
        return {
            "yaw_deg": 0.0,
            "wrist_trim_deg": 0.0,
            "shoulder_trim_deg": 0.0,
            "elbow_trim_deg": 0.0,
            "max_box_tag_diagonal_px": None,
        }
    box_diags = [
        float(t["diagonal_px"])
        for t in tags
        if int(t.get("id", -1)) in _BOX_TAG_IDS_HINT and t.get("diagonal_px") is not None
    ]
    max_box_diag_px = max(box_diags) if box_diags else None
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
        "max_box_tag_diagonal_px": max_box_diag_px,
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
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "attempted_motion": False, "reason": "aborted_before_search"}
    current_deg = _read_d1_servo_angles()
    if current_deg is None:
        return {"ok": False, "attempted_motion": False, "reason": "No D1 servo feedback; refusing wrist search"}
    if not front_plan.get("ok"):
        return {"ok": False, "attempted_motion": False, "reason": "No valid front-camera coarse plan for wrist search"}
    hints = _front_camera_scan_hints(front_plan)
    stages = _manual_overhead_search_stages(front_plan, current_deg, cycle, hints=hints)
    try:
        sdelay = _effective_search_delay_ms()
        messages, sent = _stage_messages(stages, close_gripper=False, max_step_deg=D1_MAX_STEP_DEG_SEARCH)
        result = _run_d1_messages(messages, delay_ms=sdelay)
        return {
            **result,
            "attempted_motion": bool(result.get("ok")),
            "mode": "wrist_camera_search",
            "sent_stages": sent,
            "source_camera": front_plan.get("camera_device"),
            "cycle": cycle,
            "scan_hints": hints,
            "search_delay_ms": sdelay,
        }
    except Exception as exc:
        return {"ok": False, "attempted_motion": False, "reason": repr(exc)}


def publish_d1_arm_plan(plan_payload: dict[str, Any]) -> dict[str, Any]:
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "attempted_motion": False, "reason": "aborted_before_plan_execute"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": False, "attempted_motion": False, "reason": "GO2_ENABLE_REAL_ARM is not enabled"}

    selected = plan_payload.get("selected") or {}
    preview = selected.get("preview") or {}
    stages = preview.get("plan") or []
    if not (selected.get("absolute_ik_safe", True)):
        return {
            "ok": False,
            "attempted_motion": False,
            "reason": "selected_camera_absolute_ik_not_safe",
            "hint": "La camera polso vede il target in frame polso: usa visual-servo o frontale/calibrazione, non IK assoluta base.",
        }
    if not plan_payload.get("ok") or not preview.get("ok") or not stages:
        return {"ok": False, "attempted_motion": False, "reason": "No valid IK plan to execute"}

    _grasp_live_phase("Esecuzione piano IK sul braccio (comandi DDS multi-step)…")
    try:
        pdelay = _effective_plan_delay_ms()
        messages, sent = _stage_messages(stages, close_gripper=True, max_step_deg=D1_MAX_STEP_DEG_GRASP)
        result = _run_d1_messages(messages, delay_ms=pdelay)
        return {
            **result,
            "attempted_motion": bool(result.get("ok")),
            "sent_stages": sent if result.get("ok") else [],
            "selected_camera": plan_payload.get("selected_camera"),
            "plan_delay_ms": pdelay,
        }
    except Exception as exc:
        return {"ok": False, "attempted_motion": False, "reason": repr(exc), "stack": command_stack_status()}


def _box_plan_snapshot() -> dict[str, Any]:
    # Background grasp worker runs outside Flask request context; api_box_plan uses jsonify.
    with APP.app_context():
        return json.loads(api_box_plan().get_data(as_text=True))


def _camera_candidate(plan: dict[str, Any], device: int) -> dict[str, Any]:
    return (plan.get("candidates") or {}).get(str(device)) or {}


def _wrist_has_lock(plan: dict[str, Any]) -> bool:
    wrist = _camera_candidate(plan, 0)
    return bool(wrist.get("ok") and (wrist.get("tags") or {}).get("tags"))


def _grasp_wrist_policy() -> str:
    raw = (os.environ.get("GO2_GRASP_WRIST_POLICY") or "center_then_grasp_on_loss").strip().lower()
    if raw in {"legacy", "legacy_double_lock", "double_lock"}:
        return "legacy_double_lock"
    return "center_then_grasp_on_loss"


def _grasp_fused_ik_allowed(wrist_policy: str) -> bool:
    if wrist_policy == "legacy_double_lock":
        return True
    # GO2_GRASP_USE_FUSED_PLAN_IK=1 deve bastare (prima richiedeva anche FUSED_WITH_CENTER e bloccava il deploy NX).
    if _effective_grasp_bool("use_fused_plan_ik", "GO2_GRASP_USE_FUSED_PLAN_IK"):
        return True
    return _effective_grasp_bool("fused_with_center", "GO2_GRASP_FUSED_WITH_CENTER")


def _frame_shape_hw_for_camera(plan: dict[str, Any], device: int) -> tuple[float, float]:
    """Ritorna (altezza, larghezza) in pixel come `frame.shape[:2]` OpenCV."""
    pipe = plan.get("april_tag_pipeline") or {}
    per = (pipe.get("per_camera") or {}).get(str(device)) or {}
    sh = per.get("frame_shape_hw")
    if sh and len(sh) >= 2:
        return float(sh[0]), float(sh[1])
    return 480.0, 640.0


def _wrist_box_tags_visible(wrist_plan: dict[str, Any]) -> bool:
    tags = (wrist_plan.get("tags") or {}).get("tags") or []
    for t in tags:
        if int(t.get("id", -1)) in BOX_TAG_IDS_IK:
            return True
    return False


def _grip_point_visible(candidate: dict[str, Any]) -> bool:
    return bool(((candidate or {}).get("grip_point") or {}).get("ok"))


def _wrist_plan_executable(wrist_plan: dict[str, Any]) -> bool:
    if not wrist_plan.get("absolute_ik_safe", False):
        return False
    pv = wrist_plan.get("preview") or {}
    return bool(wrist_plan.get("ok") and pv.get("ok") and (pv.get("plan") or []))


def _max_box_diagonal_px_wrist(wrist_plan: dict[str, Any]) -> float | None:
    tags = (wrist_plan.get("tags") or {}).get("tags") or []
    diags: list[float] = []
    for t in tags:
        if int(t.get("id", -1)) not in BOX_TAG_IDS_IK:
            continue
        d = t.get("diagonal_px")
        if d is not None:
            diags.append(float(d))
    return max(diags) if diags else None


def _wrist_camera_center_hints(wrist_plan: dict[str, Any], frame_hw: tuple[float, float]) -> dict[str, Any]:
    """Offset grip point vs centro immagine — camera polso. ``frame_hw``: (h, w)."""
    h, w = frame_hw[0], frame_hw[1]
    cx, cy = w / 2.0, h / 2.0
    grip = (wrist_plan.get("grip_point") or {})
    if grip.get("ok") and grip.get("grip_center_px"):
        gc = grip.get("grip_center_px") or [cx, cy]
        mean_x, mean_y = float(gc[0]), float(gc[1])
        dx, dy = mean_x - cx, mean_y - cy
        nx = dx / max(w / 2.0, 1.0)
        ny = dy / max(h / 2.0, 1.0)
        nx_s = float(os.environ.get("GO2_WRIST_CENTER_NX_EFFECT_SIGN", "1"))
        ny_s = float(os.environ.get("GO2_WRIST_CENTER_NY_EFFECT_SIGN", "1"))
        yaw_deg = max(-12.0, min(12.0, nx * 10.0 * nx_s))
        wrist_trim_deg = max(-12.0, min(12.0, -ny * 11.0 * ny_s))
        shoulder_trim_deg = max(-4.5, min(4.5, -ny * 3.5 * ny_s))
        return {
            "has_tags": _wrist_box_tags_visible(wrist_plan),
            "has_grip_point": True,
            "source": grip.get("source"),
            "yaw_deg": yaw_deg,
            "wrist_trim_deg": wrist_trim_deg,
            "shoulder_trim_deg": shoulder_trim_deg,
            "offset_px": (round(dx, 2), round(dy, 2)),
            "norm": (round(nx, 4), round(ny, 4)),
            "max_box_tag_diagonal_px": _max_box_diagonal_px_wrist(wrist_plan),
            "box_area_px": grip.get("box_area_px"),
        }
    tags = (wrist_plan.get("tags") or {}).get("tags") or []
    box_centers: list[tuple[float, float]] = []
    for t in tags:
        if int(t.get("id", -1)) not in BOX_TAG_IDS_IK:
            continue
        c = t.get("center_px")
        if c and len(c) >= 2:
            box_centers.append((float(c[0]), float(c[1])))
    if not box_centers:
        return {
            "has_tags": False,
            "has_grip_point": False,
            "source": "none",
            "yaw_deg": 0.0,
            "wrist_trim_deg": 0.0,
            "shoulder_trim_deg": 0.0,
            "offset_px": (0.0, 0.0),
            "norm": (0.0, 0.0),
            "max_box_tag_diagonal_px": None,
        }
    mean_x = sum(p[0] for p in box_centers) / len(box_centers)
    mean_y = sum(p[1] for p in box_centers) / len(box_centers)
    dx, dy = mean_x - cx, mean_y - cy
    nx = dx / max(w / 2.0, 1.0)
    ny = dy / max(h / 2.0, 1.0)
    nx_s = float(os.environ.get("GO2_WRIST_CENTER_NX_EFFECT_SIGN", "1"))
    ny_s = float(os.environ.get("GO2_WRIST_CENTER_NY_EFFECT_SIGN", "1"))
    yaw_deg = max(-12.0, min(12.0, nx * 10.0 * nx_s))
    wrist_trim_deg = max(-12.0, min(12.0, -ny * 11.0 * ny_s))
    shoulder_trim_deg = max(-4.5, min(4.5, -ny * 3.5 * ny_s))
    diags = [
        float(t["diagonal_px"])
        for t in tags
        if int(t.get("id", -1)) in BOX_TAG_IDS_IK and t.get("diagonal_px") is not None
    ]
    return {
        "has_tags": True,
        "has_grip_point": True,
        "source": "apriltag",
        "yaw_deg": yaw_deg,
        "wrist_trim_deg": wrist_trim_deg,
        "shoulder_trim_deg": shoulder_trim_deg,
        "offset_px": (round(dx, 2), round(dy, 2)),
        "norm": (round(nx, 4), round(ny, 4)),
        "max_box_tag_diagonal_px": max(diags) if diags else None,
    }


def _wrist_centering_stages(
    wrist_plan: dict[str, Any],
    current_deg: list[float],
    frame_hw: tuple[float, float],
    *,
    hints: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    hints = hints if hints is not None else _wrist_camera_center_hints(wrist_plan, frame_hw)
    if not hints.get("has_grip_point") and not hints.get("has_tags"):
        return []
    deadband = float(os.environ.get("GO2_WRIST_CENTER_DEADBAND_PX", "18"))
    dx, dy = hints["offset_px"]
    if math.hypot(dx, dy) < deadband:
        return []
    gain = float(os.environ.get("GO2_WRIST_CENTER_STEP_GAIN", "0.38"))
    # Visual servo from wrist camera must be incremental. Do not clamp to a
    # nominal search posture here: from START the arm may already be close, and
    # absolute clamps can create a large unexpected jump.
    max_yaw_step = float(os.environ.get("GO2_WRIST_CENTER_MAX_YAW_STEP_DEG", "1.2"))
    max_shoulder_step = float(os.environ.get("GO2_WRIST_CENTER_MAX_SHOULDER_STEP_DEG", "0.9"))
    max_wrist_step = float(os.environ.get("GO2_WRIST_CENTER_MAX_WRIST_STEP_DEG", "1.6"))
    yaw_delta = max(-max_yaw_step, min(max_yaw_step, gain * float(hints["yaw_deg"])))
    shoulder_delta = max(-max_shoulder_step, min(max_shoulder_step, gain * float(hints["shoulder_trim_deg"])))
    wrist_delta = max(-max_wrist_step, min(max_wrist_step, gain * float(hints["wrist_trim_deg"])))
    shoulder_tgt = max(-90.0, min(90.0, current_deg[1] + shoulder_delta))
    wrist_tgt = max(-90.0, min(90.0, current_deg[4] + wrist_delta))
    yaw = max(-135.0, min(135.0, current_deg[0] + yaw_delta))
    target_deg = [yaw, shoulder_tgt, current_deg[2], current_deg[3], wrist_tgt, current_deg[5]]
    return [
        {
            "stage": "wrist_tag_center_micro_step",
            "joints_rad": [round(math.radians(v), 4) for v in target_deg],
            "target_deg": [round(v, 3) for v in target_deg],
            "delta_deg": [round(yaw_delta, 3), round(shoulder_delta, 3), 0.0, 0.0, round(wrist_delta, 3), 0.0],
        }
    ]


def publish_d1_wrist_center_step(
    wrist_plan: dict[str, Any],
    plan_for_shape: dict[str, Any],
    *,
    frame_hw: tuple[float, float] | None = None,
) -> dict[str, Any]:
    if ARM_GRASP_ABORT.is_set():
        return {"ok": False, "attempted_motion": False, "reason": "aborted_before_wrist_center"}
    current_deg = _read_d1_servo_angles()
    if current_deg is None:
        return {"ok": False, "attempted_motion": False, "reason": "No D1 servo feedback; refusing wrist center"}
    fh = frame_hw if frame_hw is not None else _frame_shape_hw_for_camera(plan_for_shape, 0)
    hints = _wrist_camera_center_hints(wrist_plan, fh)
    stages = _wrist_centering_stages(wrist_plan, current_deg, fh, hints=hints)
    if not stages:
        return {
            "ok": True,
            "attempted_motion": False,
            "skipped": True,
            "reason": "within_deadband_or_no_grip_point",
            "center_hints": hints,
        }
    try:
        raw_c = os.environ.get("D1_WRIST_CENTER_DELAY_MS")
        if raw_c is not None and str(raw_c).strip():
            cdelay = int(float(raw_c))
        else:
            cdelay = int(os.environ.get("D1_SEARCH_DELAY_MS", "380"))
        cdelay = max(120, min(cdelay, 900))
        messages, sent = _stage_messages(stages, close_gripper=False, max_step_deg=D1_WRIST_CENTER_MAX_STEP_DEG)
        result = _run_d1_messages(messages, delay_ms=cdelay)
        return {
            **result,
            "attempted_motion": bool(result.get("ok")),
            "mode": "wrist_camera_center",
            "sent_stages": sent,
            "center_hints": hints,
            "center_delay_ms": cdelay,
        }
    except Exception as exc:
        return {"ok": False, "attempted_motion": False, "reason": repr(exc)}


def _visual_servo_metric(candidate: dict[str, Any], frame_hw: tuple[float, float]) -> dict[str, Any]:
    hints = _wrist_camera_center_hints(candidate, frame_hw)
    norm = hints.get("norm") or (0.0, 0.0)
    err = float(math.hypot(float(norm[0]), float(norm[1])))
    diag = _max_box_diagonal_px_wrist(candidate)
    area = hints.get("box_area_px")
    size = float(area or 0.0)
    if diag is not None:
        size = max(size, float(diag) * float(diag))
    return {
        "ok": bool(hints.get("has_grip_point") or hints.get("has_tags")),
        "error_norm": round(err, 5),
        "size_score": round(size, 3),
        "source": hints.get("source"),
        "hints": hints,
    }


def _visual_servo_progress(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if not before.get("ok") or not after.get("ok"):
        return {"ok": False, "reason": "missing_visual_metric", "before": before, "after": after}
    err_before = float(before.get("error_norm") or 0.0)
    err_after = float(after.get("error_norm") or 0.0)
    size_before = float(before.get("size_score") or 0.0)
    size_after = float(after.get("size_score") or 0.0)
    err_gain = err_before - err_after
    size_gain = size_after - size_before
    min_err = float(os.environ.get("GO2_VISUAL_SERVO_MIN_ERR_GAIN", "0.018"))
    min_size_ratio = float(os.environ.get("GO2_VISUAL_SERVO_MIN_SIZE_RATIO", "0.025"))
    size_ok = size_before <= 1.0 or size_gain >= size_before * min_size_ratio
    err_ok = err_gain >= min_err
    return {
        "ok": bool(size_ok or err_ok),
        "err_gain": round(err_gain, 5),
        "size_gain": round(size_gain, 3),
        "size_ratio": round((size_gain / size_before) if size_before > 1.0 else 0.0, 5),
        "before": before,
        "after": after,
    }


def _wait_for_visible_plan(wait_s: float | None = None) -> dict[str, Any]:
    if wait_s is None:
        wait_s = _tune_float("visible_plan_wait_s", "GO2_GRASP_VISIBLE_PLAN_WAIT_S", 15.0)
    deadline = time.time() + wait_s
    last = _box_plan_snapshot()
    next_hold_ping = time.time() + 1.2
    while time.time() < deadline:
        if ARM_GRASP_ABORT.is_set():
            return last
        now = time.time()
        if now >= next_hold_ping:
            next_hold_ping = now + 1.8
            _arm_hold_keepalive("attesa piano visibile")
        plan = _box_plan_snapshot()
        if _grip_point_visible(_camera_candidate(plan, 0)) or _camera_candidate(plan, 6).get("ok") or _grip_point_visible(_camera_candidate(plan, 6)):
            return plan
        last = plan
        if not _sleep_abortable(0.35):
            return last
    return last


def _grasp_abort_return(
    *,
    log: list[dict[str, Any]],
    first_plan: dict[str, Any],
    attempted_motion: bool,
    alignment_prelude: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_plan = _box_plan_snapshot()
    out: dict[str, Any] = {
        "ok": False,
        "attempted_motion": attempted_motion,
        "grasp_policy": "user_abort",
        "reason": "user_abort_during_grasp",
        "cycles": log,
        "final_plan": final_plan,
        "dry_run_plan": first_plan,
    }
    if alignment_prelude is not None:
        out["alignment_prelude"] = alignment_prelude
    return out


def run_wrist_guided_grasp_loop(max_cycles: int | None = None) -> dict[str, Any]:
    # 1) Eventuale fold; 2) START salvata; 3) attendi AprilTag dalla posa operativa; 4) lock/piano fuso → IK.
    mc = (
        int(max_cycles)
        if max_cycles is not None
        else _tune_int("search_max_cycles", "D1_SEARCH_MAX_CYCLES", D1_SEARCH_MAX_CYCLES)
    )
    if os.environ.get("GO2_GRASP_ENTRY_HOLD", "1").lower() in {"1", "true", "yes"}:
        if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"}:
            _grasp_live_phase("Hold sulla posa corrente (anti-cedimento prima di fold/START)…")
            er = int(os.environ.get("GO2_GRASP_ENTRY_HOLD_REPEATS", "20"))
            ed = int(os.environ.get("GO2_GRASP_ENTRY_HOLD_DELAY_MS", "90"))
            publish_d1_hold_current(repeats=max(8, er), delay_ms=max(45, ed))
    _grasp_live_phase("Fold braccio — posizione compatta")
    fold_raw = _goto_fold_arm_pose()
    _grasp_live_phase("Riallineamento braccio alla posa START salvata…")
    prelude_raw = _goto_saved_start_arm_pose()
    wait_tags = _wait_for_apriltag_detection()
    alignment_prelude: dict[str, Any] = {
        "goto_fold": fold_raw,
        "goto_saved_start": prelude_raw,
        "wait_apriltag_after_start": wait_tags,
    }

    if not fold_raw.get("ok") and not fold_raw.get("skipped"):
        return {
            "ok": False,
            "attempted_motion": bool(fold_raw.get("messages")),
            "grasp_policy": "fold_pose_failed",
            "reason": str(fold_raw.get("reason", "goto_fold failed")),
            "alignment_prelude": alignment_prelude,
            "cycles": [],
            "final_plan": _box_plan_snapshot(),
            "dry_run_plan": {},
        }

    if not wait_tags.get("ok") and not wait_tags.get("skipped"):
        return {
            "ok": False,
            "attempted_motion": bool(fold_raw.get("messages")),
            "grasp_policy": "apriltag_wait_failed",
            "reason": str(wait_tags.get("reason", "waiting for AprilTag")),
            "alignment_prelude": alignment_prelude,
            "cycles": [],
            "final_plan": wait_tags.get("last_plan") or _box_plan_snapshot(),
            "dry_run_plan": {},
        }

    if not prelude_raw.get("ok") and not prelude_raw.get("skipped"):
        return {
            "ok": False,
            "attempted_motion": bool(prelude_raw.get("messages")),
            "grasp_policy": "saved_start_align_failed",
            "reason": str(prelude_raw.get("reason", "goto_saved_start failed")),
            "alignment_prelude": alignment_prelude,
            "cycles": [],
            "final_plan": _box_plan_snapshot(),
            "dry_run_plan": {},
        }

    log: list[dict[str, Any]] = []
    _grasp_live_phase("Allineamento vista — attesa tag/plan utilizzabile sulle camere…")
    first_plan = _wait_for_visible_plan()
    last_front_plan = _camera_candidate(first_plan, 6) if _camera_candidate(first_plan, 6).get("ok") else None
    fused_confirm_count = 0
    fused_env = _effective_grasp_bool("use_fused_plan_ik", "GO2_GRASP_USE_FUSED_PLAN_IK")
    wrist_policy = _grasp_wrist_policy()
    last_valid_execute: dict[str, Any] | None = None
    loss_streak = 0
    d_thresh = float(os.environ.get("GO2_WRIST_GRASP_DIAGONAL_MIN_PX", "420"))
    loss_debounce = max(1, min(int(os.environ.get("GO2_GRASP_LOSS_DEBOUNCE_FRAMES", "2")), 8))

    def _attempted_from_log() -> bool:
        for entry in log:
            if (entry.get("search_execution") or {}).get("attempted_motion"):
                return True
            if (entry.get("wrist_center_execution") or {}).get("attempted_motion"):
                return True
        return False

    for cycle in range(mc):
        if ARM_GRASP_ABORT.is_set():
            return _grasp_abort_return(
                log=log,
                first_plan=first_plan,
                attempted_motion=_attempted_from_log(),
                alignment_prelude=alignment_prelude,
            )
        _grasp_live_phase(
            f"Avvicinamento / ricerca — ciclo {cycle + 1} di {mc} (muovo polso verso tag)",
            cycle=cycle + 1,
            max_cycles=mc,
        )
        plan = _box_plan_snapshot()
        wrist_plan = _camera_candidate(plan, 0)
        front_plan = _camera_candidate(plan, 6)
        frame_hw = _frame_shape_hw_for_camera(plan, 0)
        if front_plan.get("ok"):
            last_front_plan = front_plan

        grip_vis = _grip_point_visible(wrist_plan)
        if _wrist_plan_executable(wrist_plan) and grip_vis:
            last_valid_execute = {
                "ok": True,
                "selected_camera": 0,
                "selected": wrist_plan,
            }

        center_hints = _wrist_camera_center_hints(wrist_plan, frame_hw)
        md_px = _max_box_diagonal_px_wrist(wrist_plan)
        box_vis = _wrist_box_tags_visible(wrist_plan)
        servo_metric = _visual_servo_metric(wrist_plan, frame_hw)

        if wrist_policy == "center_then_grasp_on_loss":
            if grip_vis:
                loss_streak = 0
            else:
                loss_streak = loss_streak + 1 if last_valid_execute is not None else 0

        log.append({
            "cycle": cycle,
            "wrist_ok": bool(wrist_plan.get("ok")),
            "front_ok": bool(front_plan.get("ok")),
            "front_memory_ok": bool(last_front_plan),
            "selected_camera": plan.get("selected_camera"),
            "wrist_policy": wrist_policy,
            "wrist_box_tags_visible": box_vis,
            "wrist_grip_point_visible": grip_vis,
            "wrist_grip_source": ((wrist_plan.get("grip_point") or {}).get("source")),
            "wrist_center_norm": center_hints.get("norm"),
            "wrist_max_box_diagonal_px": md_px,
            "wrist_visual_servo_metric": servo_metric,
            "cached_wrist_ik_ok": last_valid_execute is not None,
            "loss_streak": loss_streak,
            "ik_gate": {
                "wrist_absolute_ik_safe": bool(wrist_plan.get("absolute_ik_safe")),
                "wrist_plan_executable": _wrist_plan_executable(wrist_plan),
                "last_valid_execute_cached_before_step": last_valid_execute is not None,
                "effective_trust_wrist": _effective_grasp_bool(
                    "trust_wrist_absolute_ik", "GO2_TRUST_WRIST_ABSOLUTE_IK"
                ),
                "effective_use_fused_plan_ik": _effective_grasp_bool(
                    "use_fused_plan_ik", "GO2_GRASP_USE_FUSED_PLAN_IK"
                ),
                "wrist_preview_ok": bool((wrist_plan.get("preview") or {}).get("ok")),
            },
        })

        if wrist_policy == "center_then_grasp_on_loss":
            if last_valid_execute is not None and d_thresh > 0 and md_px is not None and md_px >= d_thresh:
                if ARM_GRASP_ABORT.is_set():
                    return _grasp_abort_return(
                        log=log,
                        first_plan=first_plan,
                        attempted_motion=_attempted_from_log(),
                        alignment_prelude=alignment_prelude,
                    )
                _grasp_live_phase("Tag box molto vicino (diagonale) — eseguo IK da ultimo piano valido")
                execution = publish_d1_arm_plan(last_valid_execute)
                return {
                    **execution,
                    "grasp_policy": "center_then_grasp_on_diagonal",
                    "cycles": log,
                    "final_plan": plan,
                    "dry_run_plan": first_plan,
                    "alignment_prelude": alignment_prelude,
                    "executed_from_cached_plan": True,
                    "size_trigger": True,
                    "diagonal_threshold_px": d_thresh,
                }

            if last_valid_execute is not None and loss_streak >= loss_debounce and not grip_vis:
                if ARM_GRASP_ABORT.is_set():
                    return _grasp_abort_return(
                        log=log,
                        first_plan=first_plan,
                        attempted_motion=_attempted_from_log(),
                        alignment_prelude=alignment_prelude,
                    )
                _grasp_live_phase("Punto presa perso dal polso — eseguo IK (ultimo piano valido)")
                execution = publish_d1_arm_plan(last_valid_execute)
                return {
                    **execution,
                    "grasp_policy": "center_then_grasp_on_loss",
                    "cycles": log,
                    "final_plan": plan,
                    "dry_run_plan": first_plan,
                    "alignment_prelude": alignment_prelude,
                    "executed_from_cached_plan": True,
                    "loss_trigger": True,
                }

        elif wrist_policy == "legacy_double_lock":
            if _wrist_has_lock(plan):
                if not _sleep_abortable(0.35):
                    return _grasp_abort_return(
                        log=log,
                        first_plan=first_plan,
                        attempted_motion=_attempted_from_log(),
                        alignment_prelude=alignment_prelude,
                    )
                confirm = _box_plan_snapshot()
                if _wrist_has_lock(confirm):
                    if ARM_GRASP_ABORT.is_set():
                        return _grasp_abort_return(
                            log=log,
                            first_plan=first_plan,
                            attempted_motion=_attempted_from_log(),
                            alignment_prelude=alignment_prelude,
                        )
                    _grasp_live_phase("Lock AprilTag sul polso confermato — eseguo piano IK (approccio / presa)")
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
                        "alignment_prelude": alignment_prelude,
                    }
                log[-1]["wrist_confirm_lost"] = True

        if fused_env and _grasp_fused_ik_allowed(wrist_policy):
            if _plan_ready_for_fused_ik(plan) and not ARM_GRASP_ABORT.is_set():
                fused_confirm_count += 1
            else:
                fused_confirm_count = 0
            if fused_confirm_count >= 2:
                if not _sleep_abortable(0.15):
                    return _grasp_abort_return(
                        log=log,
                        first_plan=first_plan,
                        attempted_motion=_attempted_from_log(),
                        alignment_prelude=alignment_prelude,
                    )
                plan_exec = _box_plan_snapshot()
                if _plan_ready_for_fused_ik(plan_exec) and not ARM_GRASP_ABORT.is_set():
                    sc = plan_exec.get("selected_camera")
                    _grasp_live_phase(
                        f"IK dal piano fuso (camera {sc}) — variabile GO2_GRASP_USE_FUSED_PLAN_IK=1 attiva (lock polso non richiesto)."
                    )
                    execution = publish_d1_arm_plan({
                        **plan_exec,
                        "ok": True,
                        "selected_camera": sc,
                        "selected": plan_exec.get("selected") or {},
                    })
                    return {
                        **execution,
                        "grasp_policy": "fused_plan_ik_no_wrist_lock",
                        "cycles": log,
                        "final_plan": plan_exec,
                        "dry_run_plan": first_plan,
                        "alignment_prelude": alignment_prelude,
                        "warning": "IK eseguita sulla camera col punteggio migliore senza doppio lock AprilTag sul polso.",
                    }
                fused_confirm_count = 0

        if wrist_policy == "center_then_grasp_on_loss" and grip_vis:
            center_res = publish_d1_wrist_center_step(wrist_plan, plan, frame_hw=frame_hw)
            progress = None
            if center_res.get("attempted_motion") and center_res.get("ok"):
                if not _sleep_abortable(float(os.environ.get("GO2_VISUAL_SERVO_VERIFY_WAIT_S", "0.16"))):
                    return _grasp_abort_return(
                        log=log,
                        first_plan=first_plan,
                        attempted_motion=_attempted_from_log(),
                        alignment_prelude=alignment_prelude,
                    )
                verify_plan = _box_plan_snapshot()
                verify_wrist = _camera_candidate(verify_plan, 0)
                progress = _visual_servo_progress(servo_metric, _visual_servo_metric(verify_wrist, frame_hw))
                if not progress.get("ok") and os.environ.get("GO2_VISUAL_SERVO_STOP_ON_DIVERGE", "1").lower() in {"1", "true", "yes"}:
                    _arm_event("blocked", "Visual servo divergente: hold e stop", progress=progress)
                    hold = publish_d1_hold_current(repeats=6, delay_ms=55)
                    return {
                        "ok": False,
                        "attempted_motion": True,
                        "grasp_policy": "visual_servo_diverged",
                        "reason": "visual_servo_diverged_object_not_growing_or_centering",
                        "visual_servo_progress": progress,
                        "hold": hold,
                        "cycles": log,
                        "final_plan": verify_plan,
                        "dry_run_plan": first_plan,
                        "alignment_prelude": alignment_prelude,
                    }
            log[-1]["wrist_center_execution"] = {
                "ok": center_res.get("ok"),
                "attempted_motion": center_res.get("attempted_motion"),
                "skipped": center_res.get("skipped"),
                "sent_stages": center_res.get("sent_stages"),
                "center_hints": (center_res.get("center_hints") or {}),
                "visual_servo_progress": progress,
            }

        # If the wrist camera already sees the grip point, do not also run the
        # coarse front-camera search in the same cycle. That search can move
        # shoulder/elbow toward an overhead posture and feels like a surprise
        # jump after a small visual-servo correction.
        if last_front_plan and not grip_vis:
            search = publish_d1_arm_search(last_front_plan, cycle=cycle)
            log[-1]["search_execution"] = {
                "ok": search.get("ok"),
                "attempted_motion": search.get("attempted_motion"),
                "sent_stages": search.get("sent_stages"),
                "cycle": search.get("cycle"),
                "helper_returncode": search.get("helper_returncode"),
            }
            if not _sleep_abortable(0.82):
                return _grasp_abort_return(
                    log=log,
                    first_plan=first_plan,
                    attempted_motion=_attempted_from_log(),
                    alignment_prelude=alignment_prelude,
                )
        elif grip_vis:
            log[-1]["search_execution"] = {
                "skipped": True,
                "reason": "wrist_grip_visible_micro_servo_only",
            }
            if not _sleep_abortable(float(os.environ.get("GO2_WRIST_ONLY_CYCLE_SLEEP_S", "0.18"))):
                return _grasp_abort_return(
                    log=log,
                    first_plan=first_plan,
                    attempted_motion=_attempted_from_log(),
                    alignment_prelude=alignment_prelude,
                )
        else:
            if not _sleep_abortable(0.45):
                return _grasp_abort_return(
                    log=log,
                    first_plan=first_plan,
                    attempted_motion=_attempted_from_log(),
                    alignment_prelude=alignment_prelude,
                )

    final_plan = _box_plan_snapshot()
    fallback_ok = _effective_grasp_bool("front_camera_fallback_grasp", "GO2_FRONT_CAMERA_FALLBACK_GRASP")
    fp = None
    if last_front_plan and last_front_plan.get("ok"):
        fp = last_front_plan
    else:
        c6 = _camera_candidate(final_plan, 6)
        if c6.get("ok"):
            fp = c6
    if fallback_ok and fp and fp.get("ok"):
        if ARM_GRASP_ABORT.is_set():
            return _grasp_abort_return(
                log=log,
                first_plan=first_plan,
                attempted_motion=_attempted_from_log(),
                alignment_prelude=alignment_prelude,
            )
        _grasp_live_phase("Fallback presa da RealSense (camera 6) — esecuzione IK")
        execution = publish_d1_arm_plan({
            "ok": True,
            "selected_camera": 6,
            "selected": fp,
        })
        return {
            **execution,
            "grasp_policy": "front_camera_fallback_no_wrist_lock",
            "cycles": log,
            "final_plan": final_plan,
            "dry_run_plan": first_plan,
            "alignment_prelude": alignment_prelude,
            "warning": "Presa da IK solo RealSense (camera 6): il polso non ha confermato tag — solo se area libera e rischio accettabile.",
        }

    return {
        "ok": False,
        "attempted_motion": _attempted_from_log(),
        "grasp_policy": "continuous_wrist_search_no_lock",
        "reason": "Search cycles completed; wrist /dev/video0 never locked. Set GO2_FRONT_CAMERA_FALLBACK_GRASP=1 for RealSense-only grasp (risky).",
        "cycles": log,
        "final_plan": final_plan,
        "dry_run_plan": first_plan,
        "alignment_prelude": alignment_prelude,
    }


def _grasp_preflight_and_start(drain_s: float) -> None:
    """
    Esegue grasp_pipeline_status (lento) fuori dal thread HTTP, poi avvia il loop
    se il preflight passa. Evita che il browser resti su «Invio POST…» con job idle.
    """
    try:
        preflight = grasp_pipeline_status()
        if not _grasp_execute_enabled():
            _arm_event("blocked", "Avvio grasp bloccato: GO2_GRASP_EXECUTE_ARM=0 (modalità sicura)")
            with ARM_OPERATION_LOCK:
                if LAST_ARM_JOB.get("status") == "starting":
                    _arm_job_update(
                        "idle",
                        {
                            "phase_label_it": "Esecuzione disabilitata (GO2_GRASP_EXECUTE_ARM=0).",
                            "preflight": preflight,
                        },
                    )
            return
        allow, deny_reason = _grasp_preflight_allows_sequence_start(
            {
                "fusion_ready_for_execute": bool(preflight.get("fusion_ready_for_execute")),
                "wrist_sees_box_tags": bool(preflight.get("wrist_sees_box_tags")),
                "wrist_preview_ok": bool(preflight.get("wrist_preview_ok")),
            }
        )
        if not allow:
            _arm_event("blocked", "Avvio grasp bloccato: " + str(deny_reason))
            with ARM_OPERATION_LOCK:
                if LAST_ARM_JOB.get("status") == "starting":
                    _arm_job_update(
                        "idle",
                        {
                            "phase_label_it": "Preflight rifiutato: " + str(deny_reason),
                            "preflight_block": True,
                            "deny_reason": deny_reason,
                            "preflight_snapshot": preflight,
                        },
                    )
            return
        ARM_GRASP_ABORT.set()
        time.sleep(drain_s)
        ARM_GRASP_ABORT.clear()
        with ARM_OPERATION_LOCK:
            if LAST_ARM_JOB.get("status") != "starting":
                return
            ARM_GRASP_EVENTS.clear()
            _arm_event("start", "Avvio sequenza presa richiesto dalla UI/API")
            _arm_job_update(
                "running",
                {
                    "phase": "wrist_guided_grasp",
                    "phase_label_it": "Sequenza presa in corso…",
                },
            )
        threading.Thread(target=_grasp_background_worker, daemon=True, name="grasp-loop").start()
    except Exception as exc:
        with ARM_OPERATION_LOCK:
            if LAST_ARM_JOB.get("status") == "starting":
                _arm_job_update(
                    "error",
                    {
                        "reason": repr(exc),
                        "phase_label_it": "Errore preflight: " + repr(exc),
                    },
                )


def _grasp_background_worker() -> None:
    try:
        execution = run_wrist_guided_grasp_loop()
        execution["command_stack"] = command_stack_status()
        reason = execution.get("reason")
        ok = bool(execution.get("ok"))
        if reason == "user_abort_during_grasp":
            _arm_job_update(
                "idle",
                {
                    "result": execution,
                    "phase_label_it": "Sequenza interrotta (FERMA o abort). Puoi rilanciare.",
                },
            )
        elif ok:
            _arm_job_update("completed", {"result": execution})
        else:
            _arm_job_update("finished_no_ok", {"result": execution})
    except Exception as exc:
        _arm_job_update("error", {"reason": repr(exc)})
    finally:
        ARM_GRASP_ABORT.clear()


def _grasp_crouch_then_preflight_worker(drain_s: float, settle_s: float) -> None:
    """Esegue Sport crouch (base), attende, poi stesso preflight+grasp di ``_grasp_preflight_and_start``."""
    try:
        ok_gate, reason = _base_motion_allowed()
        if not ok_gate:
            _arm_event("blocked", "crouch_then_grasp: " + str(reason))
            with ARM_OPERATION_LOCK:
                if LAST_ARM_JOB.get("status") == "starting":
                    _arm_job_update(
                        "idle",
                        {"phase_label_it": "Crouch non eseguito: " + str(reason)},
                    )
            return
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from go2_accompany import sport_accompany

        iface = GO2_DDS_INTERFACE.strip() if GO2_DDS_INTERFACE else None
        timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "45"))

        def _crouch_call() -> Any:
            return sport_accompany(
                project_root=PROJECT_ROOT,
                domain=GO2_DDS_DOMAIN,
                iface=iface,
                enable=True,
                mode="crouch",
                stand_up_first=False,
                speed_level=None,
            )

        with ARM_OPERATION_LOCK:
            if LAST_ARM_JOB.get("status") == "starting":
                _arm_job_update(
                    "starting",
                    {"phase_label_it": "Sport StandDown (crouch) in corso…"},
                )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            cres = pool.submit(_crouch_call).result(timeout=timeout_s)
        if not cres.get("ok"):
            err = str(cres.get("reason", "?"))
            _arm_event("blocked", "crouch_sport_failed: " + err)
            with ARM_OPERATION_LOCK:
                if LAST_ARM_JOB.get("status") == "starting":
                    _arm_job_update("idle", {"phase_label_it": "Crouch Sport fallito: " + err})
            return
        with ARM_OPERATION_LOCK:
            if LAST_ARM_JOB.get("status") == "starting":
                _arm_job_update(
                    "starting",
                    {"phase_label_it": f"Crouch ok — attesa {settle_s:.1f}s poi preflight grasp…"},
                )
        _arm_event("phase", "crouch_ok_settling")
        if not _sleep_abortable(float(max(0.0, settle_s))):
            with ARM_OPERATION_LOCK:
                if LAST_ARM_JOB.get("status") == "starting":
                    _arm_job_update("idle", {"phase_label_it": "Interrotto durante attesa post-crouch."})
            return
        _grasp_preflight_and_start(drain_s)
    except concurrent.futures.TimeoutError:
        with ARM_OPERATION_LOCK:
            if LAST_ARM_JOB.get("status") == "starting":
                _arm_job_update("idle", {"phase_label_it": "Timeout Sport crouch (RPC)"})
    except Exception as exc:
        with ARM_OPERATION_LOCK:
            if LAST_ARM_JOB.get("status") == "starting":
                _arm_job_update("idle", {"phase_label_it": "Errore crouch+grasp: " + repr(exc)})


def dds_lowstate_probe(duration_s: float = 4.0) -> dict[str, Any]:
    sys.path.insert(0, str(PROJECT_ROOT / "unitree_sdk2_python"))
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
    except Exception as exc:
        return {"ok": False, "available": False, "error": f"import failed: {exc!r}"}

    seen: dict[str, Any] = {"count": 0, "first_quaternion": None, "bms": None}

    def callback(msg: Any) -> None:
        seen["count"] += 1
        if seen["first_quaternion"] is None:
            seen["first_quaternion"] = list(msg.imu_state.quaternion)
        try:
            b = msg.bms_state
            cells = [int(x) for x in b.cell_vol if int(x) > 0]
            seen["bms"] = {
                "soc_percent": int(b.soc),
                "status_byte": int(b.status),
                "current_ma": int(b.current),
                "cycle_count": int(b.cycle),
                "power_v": round(float(msg.power_v), 3),
                "power_a": round(float(msg.power_a), 3),
                "cell_voltage_min_mv": min(cells) if cells else None,
                "cell_voltage_max_mv": max(cells) if cells else None,
            }
        except Exception:
            pass

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
        battery_hint: str | None = None
        bms = seen.get("bms")
        if isinstance(bms, dict):
            soc = int(bms.get("soc_percent") or 0)
            pv = float(bms.get("power_v") or 0.0)
            if soc == 0 and pv >= 26.0:
                battery_hint = (
                    "BMS segnala SOC 0%% ma tensione bus ancora elevata: tipico di BMS/pacco difettoso, "
                    "connessione intermedia, o SOC non calibrato — il firmware potrebbe limitare Sport/moto. "
                    "Confronta con app Unitree; prova ciclo carica completa ufficiale; se persiste sostituzione/assistenza."
                )
        return {
            "ok": seen["count"] > 0,
            "available": True,
            "domain": GO2_DDS_DOMAIN,
            "interface": GO2_DDS_INTERFACE,
            "topic": "rt/lowstate",
            "battery_hint": battery_hint,
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


def frame_from_camera_peek(device: int) -> Any | None:
    """
    Ultimo frame in cache (come MJPEG) — nessun wait su get_jpeg. Per overlay AprilTag ad alta frequenza.
    ``/api/box/plan`` resta su ``frame_from_camera`` (lettura più robusta all’avvio).
    """
    if cv2 is None:
        return None
    if GO2_LOCAL:
        CAMERA_CACHE.start(device)
        jpg = CAMERA_CACHE.peek_jpeg(device)
    else:
        jpg = robot_camera_jpeg(device)
    if jpg is None:
        return None
    import numpy as np

    arr = np.frombuffer(jpg, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _apriltag_overlay_jpeg_bytes(device: int) -> bytes | None:
    """Frame cache → detect AprilTag → JPEG overlay (stream MJPEG + GET singolo)."""
    if device not in CAMERA_DEVICES:
        return None
    frame = frame_from_camera_peek(device)
    if frame is None or cv2 is None:
        return None
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from box_grasp_planner import detect_box_tags, draw_tags

        out = draw_tags(frame, detect_box_tags(frame))
        aq = int(os.environ.get("GO2_ANNOTATED_JPEG_QUALITY", "72"))
        aq = max(55, min(95, aq))
        ok, jpg = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), aq])
        return jpg.tobytes() if ok else None
    except Exception:
        return None


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
    button.green { background: #059669; }
    button.green:hover { background: #047857; }
    button.warn { background: #d97706; }
    button.warn:hover { background: #b45309; }
    button.emergency { background: #b91c1c; }
    button.emergency:hover { background: #991b1b; }
    button.emergency.always-visible {
      font-size: 15px;
      padding: 12px 18px;
      border: 2px solid #fecaca;
      box-shadow: 0 0 0 2px rgba(185, 28, 28, .25), 0 8px 20px rgba(0,0,0,.28);
    }
    .btn-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 10px; }
    .layout { display: grid; grid-template-columns: minmax(360px, 1.2fr) minmax(340px, .8fr); gap: 18px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; }
    .card { background: rgba(15,23,42,.9); border: 1px solid #334155; border-radius: 18px; padding: 16px; box-shadow: 0 18px 40px rgba(0,0,0,.24); }
    .card h2 { margin: 0 0 12px; font-size: 17px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .ok { color: #34d399; }
    .bad { color: #fb7185; }
    .warn { color: #fbbf24; }
    .muted { color: #94a3b8; }
    .pill { display: inline-block; padding: 4px 8px; border-radius: 999px; background: #1e293b; margin: 2px; font-size: 12px; border: 1px solid #475569; }
    .pill.ok { color: #a7f3d0; border-color: #059669; background: rgba(5, 150, 105, 0.2); }
    .pill.warn { color: #fde68a; border-color: #d97706; background: rgba(217, 119, 6, 0.2); }
    .pill.bad { color: #fecaca; border-color: #dc2626; background: rgba(220, 38, 38, 0.25); }
    .metric { font-size: 28px; font-weight: 800; margin: 6px 0; }
    .small { color: #9ca3af; font-size: 13px; }
    pre { white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow: auto; background: #020617; padding: 10px; border-radius: 12px; border: 1px solid #1e293b; font-size: 12px; }
    canvas { width: 100%; height: 420px; background: radial-gradient(circle, #0f172a, #020617); border: 1px solid #334155; border-radius: 16px; }
    .cams { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 12px; }
    .cam { background: #020617; border: 1px solid #334155; border-radius: 14px; overflow: hidden; min-height: 150px; }
    .cam img { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; background: #020617; }
    .cam span { display: block; padding: 8px 10px; color: #cbd5e1; font-size: 12px; }
    .flow-h { margin: 14px 0 8px; font-size: 14px; font-weight: 700; color: #e2e8f0; display: flex; align-items: center; gap: 10px; }
    .step-num { display: inline-flex; align-items: center; justify-content: center; min-width: 26px; height: 26px; border-radius: 8px; background: #334155; font-size: 13px; }
    pre.compact-pre { max-height: 180px; font-size: 11px; }
    details.diag-acc { background: rgba(15,23,42,.75); border: 1px solid #334155; border-radius: 14px; margin-bottom: 8px; overflow: hidden; }
    details.diag-acc summary { cursor: pointer; padding: 12px 14px; list-style: none; font-weight: 700; display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
    details.diag-acc summary::-webkit-details-marker { display: none; }
    details.diag-acc summary .muted { font-weight: 400; font-size: 11px; max-width: min(900px, 92vw); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    details.diag-acc pre { max-height: 260px; margin: 0 14px 14px; }
    .diag-stack { display: flex; flex-direction: column; gap: 0; }
    .banner-op { background: rgba(66,32,6,.92); border: 1px solid #d97706; border-radius: 14px; padding: 14px 18px; margin-bottom: 16px; font-size: 14px; line-height: 1.45; }
    .banner-op code { background: #1e293b; padding: 2px 8px; border-radius: 6px; font-size: 13px; }
    #dragFollowStatus { max-height: 200px; font-size: 12px; line-height: 1.5; border: 2px solid #334155; transition: border-color .25s, box-shadow .25s; }
    #dragFollowStatus.live { border-color: #22c55e; box-shadow: 0 0 12px rgba(34,197,94,.25); }
    #dragFollowBadge { font-weight: 800; font-size: 13px; display: inline-block; margin-bottom: 6px; }
    body.drag-follow-running .hide-when-drag-active { display: none !important; }
    body.drag-follow-running .drag-session-block {
      border: 1px solid rgba(34, 197, 94, 0.45);
      border-radius: 14px;
      padding: 14px 14px 10px;
      background: rgba(34, 197, 94, 0.07);
      margin-bottom: 4px;
    }
    /* Operazioni — tab */
    .op-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 14px 0 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid #334155;
    }
    .op-tab {
      background: #1e293b;
      color: #e2e8f0;
      border: 1px solid #475569;
      border-radius: 10px;
      padding: 10px 16px;
      cursor: pointer;
      font-weight: 700;
      font-size: 14px;
      transition: background .15s, border-color .15s, color .15s;
    }
    .op-tab:hover { background: #334155; border-color: #64748b; }
    .op-tab.is-active {
      background: #1d4ed8;
      border-color: #3b82f6;
      color: #fff;
      box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.35);
    }
    .op-panel { display: none; animation: opFade .22s ease-out; }
    .op-panel.is-active { display: block; }
    @keyframes opFade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
    .mission-hero {
      background: linear-gradient(135deg, rgba(30, 58, 138, 0.35), rgba(15, 23, 42, 0.9));
      border: 1px solid #334155;
      border-radius: 16px;
      padding: 18px 18px 16px;
      margin-bottom: 18px;
    }
    .mission-hero h3 { margin: 0 0 8px; font-size: 16px; color: #e2e8f0; }
    .mission-hero .lead { margin: 0 0 14px; color: #94a3b8; font-size: 13px; line-height: 1.5; }
    .mission-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .mission-actions .primary-go {
      font-size: 15px;
      padding: 12px 20px;
      border-radius: 12px;
      background: #059669;
      border: 0;
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }
    .mission-actions .primary-go:hover { background: #047857; }
    .mission-actions .secondary-go {
      background: #334155;
      color: #e2e8f0;
      border: 1px solid #475569;
      border-radius: 10px;
      padding: 10px 16px;
      font-weight: 600;
      cursor: pointer;
    }
    .mission-actions .secondary-go:hover { background: #475569; }
    .pulse-hint { font-size: 12px; color: #94a3b8; margin-left: 4px; }
    .flow-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 14px;
      margin-top: 8px;
    }
    .flow-card {
      background: rgba(15, 23, 42, 0.65);
      border: 1px solid #334155;
      border-radius: 14px;
      padding: 14px 14px 12px;
    }
    .flow-card h4 { margin: 0 0 8px; font-size: 13px; font-weight: 800; color: #cbd5e1; letter-spacing: .02em; }
    .flow-card .small { margin-top: 0; }
    .joint-editor-card { grid-column: 1 / -1; max-width: 100%; }
    .joint-sliders { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
    .joint-slider-row {
      display: grid;
      grid-template-columns: 36px 1fr 58px;
      align-items: center;
      gap: 8px 10px;
    }
    @media (max-width: 700px) {
      .joint-slider-row { grid-template-columns: 32px 1fr; }
      .joint-slider-row .joint-val { grid-column: 2; text-align: left; }
    }
    .joint-slider-row input[type="range"] { width: 100%; accent-color: #10b981; }
    .joint-lab { font-size: 12px; font-weight: 800; color: #94a3b8; }
    .joint-val { font-size: 12px; color: #e2e8f0; font-variant-numeric: tabular-nums; text-align: right; }
    .joint-mvbtn { padding: 6px 10px; font-size: 12px; border-radius: 8px; border: 1px solid #475569; background: #1e293b; color: #e2e8f0; cursor: pointer; white-space: nowrap; }
    .joint-mvbtn:hover { background: #334155; }
    @media (max-width: 980px) { .layout { grid-template-columns: 1fr; } .cams { grid-template-columns: 1fr; } }
    .always-cam-strip {
      position: sticky;
      top: 0;
      z-index: 30;
      border-color: #2563eb99;
      box-shadow: 0 6px 28px rgba(15, 23, 42, 0.55);
      margin-bottom: 16px;
      padding: 10px 12px;
    }
    .always-cam-strip h2 { margin-top: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
    .quick-op-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .quick-op-row .primary-go { background:#059669; padding:10px 14px; border-radius:10px; font-weight:900; }
    .quick-cams { display: flex; gap: 8px; align-items: center; margin-left: auto; flex-wrap: wrap; }
    .quick-cams img { width: 132px; max-width: 28vw; height: 74px; object-fit: contain; background:#020617; border:1px solid #334155; border-radius:8px; }
    .always-cam-more { display: none; margin-top: 12px; max-height: calc(100vh - 160px); overflow: auto; padding-right: 4px; }
    .always-cam-strip.expanded .always-cam-more { display: block; }
    .always-cam-strip.expanded .quick-cams img { width: 96px; height: 54px; }
    .always-cam-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 12px;
      margin-top: 10px;
    }
    @media (max-width: 900px) {
      .always-cam-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .quick-cams { width: 100%; margin-left: 0; }
      .quick-cams img { flex: 1; min-width: 120px; }
    }
    .always-cam-strip .cam-strip-tile img,
    .always-cam-strip .cam-strip-tile canvas {
      width: 100%;
      max-height: min(160px, 24vh);
      object-fit: contain;
      background: #0f172a;
      border-radius: 8px;
      border: 1px solid #334155;
      display: block;
    }
    .always-cam-strip.expanded .always-cam-grid { grid-template-columns: repeat(2, minmax(280px, 1fr)); }
    .always-cam-strip.expanded .cam-strip-tile img,
    .always-cam-strip.expanded .cam-strip-tile canvas {
      max-height: min(460px, 46vh);
      min-height: 260px;
    }
    .vision-large-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(320px, 1fr));
      gap: 14px;
      margin: 12px 0 14px;
    }
    @media (max-width: 900px) { .vision-large-grid { grid-template-columns: 1fr; } }
    .vision-large-tile { background:#020617; border:1px solid #334155; border-radius:14px; overflow:hidden; }
    .vision-large-tile img,
    .vision-large-tile canvas {
      width: 100%;
      height: min(520px, 52vh);
      object-fit: contain;
      display: block;
      background:#020617;
    }
    .vision-large-tile img.bad-frame,
    .vision-large-tile canvas.bad-frame {
      outline: 3px solid #dc2626;
      opacity: 0.92;
    }
    .vision-large-tile strong,
    .vision-large-tile span { display:block; padding:8px 10px; font-size:13px; }
    .vision-large-tile strong { color:#e2e8f0; padding-bottom:0; }
    .vision-large-tile span { color:#94a3b8; }
    .always-cam-strip .cam-strip-tile img.bad-frame,
    .always-cam-strip .cam-strip-tile canvas.bad-frame {
      outline: 2px solid #dc2626;
      opacity: .68;
    }
    .always-cam-strip .cam-strip-tile span {
      display: block;
      font-size: 11px;
      color: #94a3b8;
      margin-top: 6px;
      line-height: 1.35;
    }
    .tuning-controls .tune-row { margin-bottom: 12px; }
    .tuning-controls .tune-row label { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; font-size: 12px; color: #cbd5e1; flex-wrap: wrap; }
    .tuning-controls input[type="range"] { width: 100%; margin-top: 6px; }
    .grasp-status-grid { display:grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap:10px; margin:12px 0; }
    @media (max-width: 900px) { .grasp-status-grid { grid-template-columns: 1fr; } }
    .big-state { border:1px solid #334155; border-radius:14px; padding:12px; background:rgba(2,6,23,.55); }
    .big-state strong { display:block; font-size:18px; margin-bottom:5px; }
    .big-state.ok { border-color:#059669; background:rgba(5,150,105,.18); }
    .big-state.warn { border-color:#d97706; background:rgba(217,119,6,.16); }
    .big-state.bad { border-color:#dc2626; background:rgba(220,38,38,.18); }
  </style>
</head>
<body>
  <header>
    <h1>Go2 Diagnostics Dashboard</h1>
    <div class="sub">Robot {{ go2_host }} | XT-16 {{ xt16_host }} | Servo arm {{ servo_arm_host }} | Stack locale NX se GO2_LOCAL=1</div>
    <div id="serverBootBadge" class="small muted" style="margin-top:8px;line-height:1.45;">Stato processo dashboard…</div>
    <p class="small muted" style="margin-top:6px;">Listen Flask (lato server): <code>{{ dashboard_bind }}</code>:{{ dashboard_port }} · GO2_LOCAL={{ go2_local }}</p>
  </header>
  <noscript><div class="banner-op" style="margin:16px;">Abilita JavaScript: senza, la dashboard non aggiorna stato, camere e pulsanti restano statici.</div></noscript>
  <iframe name="graspPostTarget" style="display:none;width:0;height:0;border:0;" title="grasp POST target"></iframe>
  <main>
    {% if dashboard_bind == "127.0.0.1" %}
    <div class="banner-op" style="border-color:#dc2626;background:rgba(127,29,29,.38);">
      <strong>Flask è in ascolto solo su <code>127.0.0.1</code>:{{ dashboard_port }}</strong> — da un altro PC (es. il laptop) la pagina sembra «morta» / non si apre.
      Sulla macchina che ospita la dashboard esporta <code>GO2_LOCAL=1</code> e <code>GO2_DASHBOARD_HOST=0.0.0.0</code>, poi riavvia il processo (<code>python3 diagnostics_dashboard.py</code> o deploy).
    </div>
    {% endif %}
    <div class="banner-op" style="border-color:#2563eb;background:rgba(30,58,138,.25);">
      <strong>Verifica LAN (dal tuo PC, stessa rete del robot):</strong>
      <code>python scripts/verify_dashboard_http.py http://{{ go2_host }}:{{ dashboard_port }}</code>
      deve stampare OK per <code>/api/health</code> e per <code>/</code>. Se fallisce, il problema è rete/firewall/processo — non il browser.
    </div>
    <div class="banner-op">
      <strong>PC esterno (browser sul tuo laptop):</strong> usa l’IP del robot sulla LAN —
      <code>http://{{ go2_host }}:{{ dashboard_port }}</code>
      (porta <code>{{ dashboard_port }}</code> se diversa).
      <strong>Non</strong> usare <code>http://127.0.0.1:5050</code> qui: quel indirizzo è solo il «localhost» del PC dove hai il mouse, <strong>non</strong> il cane.
    </div>
    <section class="card always-cam-strip" id="alwaysCamStrip">
      <div class="quick-op-row">
        <strong>Camere &amp; AprilTag</strong>
        <span id="alwaysCameraStatus" class="pill warn">…</span>
        <span id="alwaysBoxStatus" class="pill warn">planner</span>
        <button type="button" class="emergency always-visible" onclick="emergencyHold()" title="Abort sequenza + kill helper movimento + hold DDS immediato">FERMA / HOLD</button>
        <form method="post" action="{{ script_root|default('') }}/api/arm/grasp_box/attempt" target="graspPostTarget" style="display:inline;margin:0;" onsubmit="setPresaSequenceStatus('POST diretto grasp inviato — guarda stato sotto…', false); setTimeout(refreshGraspJobPanel, 350); setTimeout(refreshGraspJobPanel, 1200);">
          <button type="submit" class="primary-go" title="POST diretto: non dipende dal fetch JavaScript">Avvia grasp</button>
        </form>
        <button type="button" class="green" onclick="saveTrueZeroPose()" title="Scrive data/true_zero_pose.json (usa editor giunti sotto per angoli esatti)">Salva ZERO</button>
        <button type="button" onclick="gotoTrueZeroPose()" title="Movimento lento verso file ZERO">Vai a ZERO</button>
        <button type="button" class="primary-go" style="background:#0f766e;border-color:#0d9488;" onclick="gotoSavedStartPose()" title="Vai direttamente alla posa START salvata (start_alignment.json)">Vai a START</button>
        <button type="button" onclick="openOpTab('graspseq')">Sequenza/debug</button>
        <button type="button" onclick="toggleAlwaysCamDetails()">Mostra camere GRANDI/debug</button>
        <div class="quick-cams">
          <img id="alwaysCam0" alt="cam0" src="/stream/robot/camera/0.mjpg">
          <img id="alwaysCam6" alt="cam6" src="/stream/robot/camera/6.mjpg">
        </div>
      </div>
      <div class="always-cam-more" id="alwaysCamMore">
        <p class="small muted" style="margin:0 0 8px;">
          Overlay tag = <strong>stesso MJPEG</strong> delle raw (flusso unico in &lt;img&gt;), nessun canvas nascosto.
        </p>
        <div class="always-cam-grid">
          <div class="cam-strip-tile">
            <img id="alwaysBox0" class="apriltag-mjpeg" alt="AprilTag polso" src="/stream/robot/camera/0/tags.mjpg" />
            <span>Overlay GRANDE polso · AprilTag</span>
          </div>
          <div class="cam-strip-tile">
            <img id="alwaysBox6" class="apriltag-mjpeg" alt="AprilTag frontale" src="/stream/robot/camera/6/tags.mjpg" />
            <span>Overlay GRANDE frontale · AprilTag</span>
          </div>
          <div class="cam-strip-tile">
            <img alt="cam0 detail" src="/stream/robot/camera/0.mjpg">
            <span id="alwaysCam0Status">Raw /dev/video0 — …</span>
          </div>
          <div class="cam-strip-tile">
            <img alt="cam6 detail" src="/stream/robot/camera/6.mjpg">
            <span id="alwaysCam6Status">Raw /dev/video6 — …</span>
          </div>
        </div>
        <div class="flow-grid" style="margin-top:14px;">
          <div class="flow-card">
            <h4>Stato tag — polso <code>video0</code></h4>
            <p id="alwaysTag0" class="small" style="margin:0;color:#94a3b8;">…</p>
          </div>
          <div class="flow-card">
            <h4>Stato tag — frontale <code>video6</code></h4>
            <p id="alwaysTag6" class="small" style="margin:0;color:#94a3b8;">…</p>
          </div>
        </div>
        <div class="flow-card" style="margin-top:14px;">
          <h4>Log detection (da <code>/api/box/plan</code>)</h4>
          <pre id="aprilTagLog" class="small compact-pre" style="max-height:140px;margin:0;">In attesa…</pre>
        </div>
        <div class="flow-card" style="margin-top:14px;">
          <h4>Perché il braccio non si muove?</h4>
          <p class="small muted" style="margin:0 0 6px;">Checklist server: GO2_LOCAL, arm reale, helper <code>bin/d1_arm_*</code>, feedback DDS, START salvato.</p>
          <pre id="armMotionDiagPre" class="small compact-pre" style="max-height:200px;margin:0;">Carico…</pre>
        </div>
        <div class="flow-card tuning-controls" style="margin-top:14px;">
          <h4>Parametri sequenza presa (sessione Flask)</h4>
          <p class="small muted" style="margin:0 0 10px;">
            Sovrascrive temporaneamente gli env. <button type="button" onclick="resetUiTuning()" style="margin-left:6px;">Ripristina default</button>
            <span id="uiTuningStatus" class="small muted"></span>
          </p>
          <div class="tune-row">
            <label>Attesa tag (s) <span id="tune_tag_wait_s_v" class="muted"></span></label>
            <input type="range" id="tune_tag_wait_s" min="5" max="300" step="1" />
          </div>
          <div class="tune-row">
            <label>Attesa piano visibile (s) <span id="tune_visible_plan_wait_s_v" class="muted"></span></label>
            <input type="range" id="tune_visible_plan_wait_s" min="0.5" max="120" step="0.5" />
          </div>
          <div class="tune-row">
            <label>Cicli ricerca max <span id="tune_search_max_cycles_v" class="muted"></span></label>
            <input type="range" id="tune_search_max_cycles" min="1" max="40" step="1" />
          </div>
          <div class="tune-row">
            <label>Ritardo tra comandi ricerca (ms) <span id="tune_search_delay_ms_v" class="muted"></span></label>
            <input type="range" id="tune_search_delay_ms" min="80" max="2500" step="10" />
          </div>
          <div class="tune-row">
            <label>Ritardo tra pianificazioni (ms) <span id="tune_plan_delay_ms_v" class="muted"></span></label>
            <input type="range" id="tune_plan_delay_ms" min="80" max="3200" step="10" />
          </div>
        </div>
      </div>
    </section>
    <section class="card" style="border-color:#1d4ed899;" id="opPanel">
      <h2>Operazioni <span id="nxModeBadge" class="pill warn">…</span></h2>
      <p class="small muted" style="margin-bottom:0;">
        <strong>Camere &amp; AprilTag</strong>: stream + overlay + «vedo i tag?» ·
        <strong>LiDAR</strong> separato ·
        <strong>Sequenza presa</strong>: fasi live (fold → tag → START → ricerca → IK), pulsanti e log ·
        poi Corpo Go2 e Drag.
      </p>

      <div class="op-tabs" role="tablist" aria-label="Sezioni operative">
        <button type="button" class="op-tab is-active" role="tab" data-op-tab="camtag" aria-selected="true">Camere &amp; AprilTag</button>
        <button type="button" class="op-tab" role="tab" data-op-tab="lidar" aria-selected="false">LiDAR XT-16</button>
        <button type="button" class="op-tab" role="tab" data-op-tab="graspseq" aria-selected="false">Sequenza presa</button>
        <button type="button" class="op-tab" role="tab" data-op-tab="go2" aria-selected="false">Corpo Go2</button>
        <button type="button" class="op-tab" role="tab" data-op-tab="drag" aria-selected="false">Drag mano</button>
      </div>

      <div id="op-camtag" class="op-panel is-active" role="tabpanel" data-op-panel="camtag">
        <p class="small muted" style="margin-bottom:12px;">
          Vista grande per controllare se i tag sono davvero in frame. Tag scatola <strong>0–3</strong>, landmark <strong>5</strong>.
        </p>
        <p class="small" style="margin-bottom:10px;">
          Stato camere (dup): <span id="cameraStatus" class="warn">warming</span> · Planner: <span id="boxStatus" class="warn">dry-run</span>
        </p>
        <div class="vision-large-grid">
          <div class="vision-large-tile">
            <canvas id="camtagBox0" width="640" height="480"></canvas>
            <strong>Overlay AprilTag — polso <code>/dev/video0</code></strong>
            <span>Copia in tempo reale dallo strip sopra (stesso stream).</span>
          </div>
          <div class="vision-large-tile">
            <canvas id="camtagBox6" width="640" height="480"></canvas>
            <strong>Overlay AprilTag — frontale RealSense <code>/dev/video6</code></strong>
            <span>Copia dallo strip · Se nera: <code>GO2_VIDEO_INDEX_6</code> sulla NX.</span>
          </div>
          <div class="vision-large-tile">
            <img alt="Raw grande polso" src="/stream/robot/camera/0.mjpg">
            <strong>Raw polso <code>/dev/video0</code></strong>
            <span>Stream MJPEG live senza overlay.</span>
          </div>
          <div class="vision-large-tile">
            <img alt="Raw grande frontale" src="/stream/robot/camera/6.mjpg">
            <strong>Raw frontale <code>/dev/video6</code></strong>
            <span>Stream MJPEG live senza overlay.</span>
          </div>
        </div>
        <div class="flow-grid" style="margin-bottom:14px;">
          <div class="flow-card">
            <h4>AprilTag — polso <code>/dev/video0</code></h4>
            <p id="tagSummary0" class="small" style="margin:0;color:#94a3b8;">Carico…</p>
          </div>
          <div class="flow-card">
            <h4>AprilTag — frontale <code>/dev/video6</code></h4>
            <p id="tagSummary6" class="small" style="margin:0;color:#94a3b8;">Carico…</p>
          </div>
        </div>
        <article class="card" style="margin-top:14px;">
          <h2>Anteprima IK · <code>/api/box/plan</code> <span class="small warn">aggiornamento ~1,6s</span></h2>
          <pre id="planSnapshot" class="small compact-pre">Loading dry-run plan...</pre>
        </article>
      </div>

      <div id="op-lidar" class="op-panel" role="tabpanel" data-op-panel="lidar">
        <p class="small muted" style="margin-bottom:12px;">Visualizzazione UDP locale sulla NX (<code>GO2_LOCAL=1</code>). Da PC remoto il LiDAR dipende dal routing verso la Jetson.</p>
        <article class="card">
          <h2>LiDAR XT-16 Live <span id="lidarStatus" class="warn">waiting</span></h2>
          <canvas id="lidarCanvas" width="900" height="620"></canvas>
          <div id="lidarMeta" class="small">Listening on robot UDP 2368...</div>
        </article>
      </div>

      <div id="op-graspseq" class="op-panel" role="tabpanel" data-op-panel="graspseq">
        <div class="hide-when-drag-active">
        <div class="mission-hero">
          <h3>Sequenza presa braccio</h3>
          <p class="lead">
            Avvio tentativo e stato <strong>fase per fase</strong> (vedi riga grande). Le camere sono nella <strong>striscia fissa sopra</strong> e in
            <strong>Camere &amp; AprilTag</strong>. Serve <code>GO2_ENABLE_REAL_ARM=1</code> e helper <code>bin/d1_arm_*</code>.
          </p>
          <p id="graspPhaseBig" class="metric" style="font-size:19px;margin:10px 0 14px;line-height:1.4;color:#e2e8f0;">
            <span class="muted">Fase sequenza: —</span>
          </p>
          <div class="grasp-status-grid">
            <div id="tagStateBig" class="big-state warn">
              <strong>VISIONE: …</strong>
              <span class="small">AprilTag 0–3 + box detector su polso/frontale</span>
            </div>
            <div id="gripStateBig" class="big-state warn">
              <strong>PRESA: …</strong>
              <span class="small">Centro presa + asse griffe est/ovest</span>
            </div>
            <div id="motionStateBig" class="big-state warn">
              <strong>MOTO: fermo</strong>
              <span class="small">Mostra quando il braccio sta ricevendo comandi</span>
            </div>
            <div id="ikStateBig" class="big-state warn">
              <strong>IK: …</strong>
              <span class="small">Target + traiettoria braccio</span>
            </div>
            <div id="detectorStateBig" class="big-state warn">
              <strong>DETECTOR: …</strong>
              <span class="small">YOLO26/YOLO11 TensorRT o fallback</span>
            </div>
          </div>
          <div class="mission-actions">
            <form method="post" action="{{ script_root|default('') }}/api/arm/grasp_box/attempt" target="graspPostTarget" style="display:inline;margin:0;" onsubmit="setPresaSequenceStatus('POST diretto grasp inviato — guarda stato sotto…', false); setTimeout(refreshGraspJobPanel, 350); setTimeout(refreshGraspJobPanel, 1200);">
              <button type="submit" class="primary-go" title="POST diretto: non dipende dal fetch JavaScript">Avvia sequenza presa (braccio)</button>
            </form>
            <button type="button" class="secondary-go" onclick="graspAfterCrouch()">Crouch cane → poi grasp</button>
            <button type="button" class="secondary-go" onclick="refreshDetectionNow()">Aggiorna detection ora</button>
            <span id="detectionPulse" class="pulse-hint"></span>
          </div>
          <div class="flow-card" style="margin-top:14px;border:1px solid #334155;">
            <h4 style="margin-top:0;">Flag grasp (sessione processo)</h4>
            <p class="small muted" style="margin:0 0 10px;">Sovrascrive gli env sulla NX fino a reset o riavvio Flask. API: <code>GET/POST /api/arm/grasp_session</code>.</p>
            <div class="tune-row" style="flex-wrap:wrap;gap:8px 16px;">
              <label class="small"><input type="checkbox" id="gs_trust_wrist" /> Fiducia IK polso</label>
              <label class="small"><input type="checkbox" id="gs_fused_ik" /> Piano fuso (USE_FUSED_PLAN_IK)</label>
              <label class="small"><input type="checkbox" id="gs_fused_center" /> FUSED_WITH_CENTER (extra)</label>
              <label class="small"><input type="checkbox" id="gs_front_fallback" /> Fallback IK frontale</label>
              <label class="small"><input type="checkbox" id="gs_prefer_tag" /> Grip solo AprilTag (no merge YOLO)</label>
              <label class="small"><input type="checkbox" id="gs_execute_arm" /> Esegui braccio (EXECUTE_ARM)</label>
            </div>
            <p style="margin:10px 0 0;">
              <button type="button" class="secondary-go" onclick="applyGraspSessionFromUi()">Applica flag</button>
              <button type="button" onclick="resetGraspSessionUi()">Reset sessione</button>
              <span id="graspSessionStatus" class="small muted"></span>
            </p>
          </div>
          <div class="flow-card" style="margin-top:12px;border:1px dashed #475569;">
            <h4 style="margin-top:0;">Fasi 1–3 (riferimento)</h4>
            <ul class="small muted" style="margin:0;padding-left:1.2em;line-height:1.55;">
              <li><strong>Fase 1</strong> — Allineamento: fold opz., posa START, attesa AprilTag e piano visibile (dual cam <code>/api/box/plan</code>).</li>
              <li><strong>Fase 2</strong> — Avvicinamento: ricerca polso da hint frontale + micro-step centratura grip; hold tra i comandi.</li>
              <li><strong>Fase 3</strong> — Presa: esecuzione IK multi-step + pinza al trigger (tag grande in frame o grip perso dopo debounce); nel JSON risultato ogni ciclo include <code>ik_gate</code> (debug gating).</li>
            </ul>
          </div>
          <p id="presaSequenceStatus" class="small" style="margin-top:12px;min-height:1.5em;line-height:1.45;color:#cbd5e1;">
            Pronto — dopo «Avvia» la fase si aggiorna qui sopra e nel JSON sotto.
          </p>
          <p class="small muted" style="margin:12px 0 0;">
            <button type="button" class="emergency" style="padding:8px 14px;font-size:13px;" onclick="emergencyHold()">FERMA — hold</button>
            · Job: <span id="graspJobHint" class="muted">—</span>
          </p>
        </div>

        <div class="flow-card" style="margin-top:14px;border:1px solid #475569;background:rgba(15,23,42,.55);">
          <h4 style="margin-top:0;">Flusso visione → punto presa → IK → moto braccio <span class="small muted">/api/arm/grasp_pipeline</span></h4>
          <p class="small muted" style="margin:0 0 8px;line-height:1.5;">
            Diagnostica aggiornata: dove si interrompe la catena (frame, detector/tag, punto presa, target, IK, DDS).
            Con <code>GO2_ENABLE_REAL_ARM=0</code> il braccio <strong>non</strong> riceve comandi. Se il RealSense ha piano IK valido ma il polso no,
            di default si resta in ricerca: su NX prova <code>GO2_GRASP_USE_FUSED_PLAN_IK=1</code> (due snapshot consecutivi col piano fuso ok) oppure
            <code>GO2_FRONT_CAMERA_FALLBACK_GRASP=1</code> dopo i cicli di ricerca.
          </p>
          <p id="graspEnvBadge" class="small" style="margin:0 0 8px;line-height:1.45;color:#e2e8f0;">Env: …</p>
          <pre id="graspEventLogPre" class="small compact-pre" style="max-height:180px;margin:0 0 10px;border-color:#475569;">Log eventi presa…</pre>
          <pre id="graspPipelinePre" class="small compact-pre" style="max-height:min(380px,42vh);margin:0;">Carico…</pre>
        </div>

        <details class="diag-acc" style="margin-top:12px;">
          <summary><strong>JSON job completo</strong> <span class="muted small">/api/arm/job_status</span></summary>
          <pre id="graspJobDetailPre" class="small compact-pre" style="margin-top:10px;max-height:340px;">—</pre>
        </details>

        <div class="flow-grid">
          <div class="flow-card">
            <h4>1 · Salva scena START</h4>
            <p class="small muted">Memorizza AprilTag + <code>arm_at_start</code> in <code>data/start_alignment.json</code>.</p>
            <div class="btn-row" style="margin-top:10px;">
              <button type="button" class="green" onclick="saveStartPose()">Salva START (AprilTag + arm)</button>
              <button type="button" onclick="loadStartPose()">Leggi START salvato</button>
            </div>
            <p class="small muted" style="margin-top:12px;line-height:1.45;">
              Posa <strong>ZERO</strong> operativa: usa l'<strong>editor giunti</strong> qui sotto per leggere/muovere/salvare — così il file coincide col braccio reale.
            </p>
            <div class="btn-row" style="margin-top:8px;">
              <button type="button" class="green" onclick="saveTrueZeroPose()">Salva ZERO (corrente)</button>
              <button type="button" onclick="gotoTrueZeroPose()">Vai a ZERO</button>
              <button type="button" class="primary-go" onclick="gotoSavedStartPose()">Vai a START</button>
              <button type="button" onclick="gotoStartFromTrueZero()" title="Percorso completo ZERO salvato → START; usa solo se sei davvero in ZERO.">ZERO → START</button>
            </div>
            <pre id="startOpsLog" class="small compact-pre" style="margin-top:10px;">Log salvataggio START…</pre>
          </div>
          <div class="flow-card">
            <h4>2 · Braccio D1 — strumenti</h4>
            <p class="small muted">Angoli, hold, pose extra (non muove il corpo del cane).</p>
            <div class="btn-row" style="margin-top:10px;">
              <button type="button" onclick="armServoSnapshot()">Leggi angoli servo</button>
              <button type="button" onclick="armSavePoseSnapshot()">Salva pose extra</button>
              <button type="button" onclick="armHoldPose()">Hold servo</button>
              <button type="button" onclick="armTeachStub()">Drag/accompagna (stato)</button>
            </div>
            <pre id="armPoseLog" class="small compact-pre" style="margin-top:10px;">Stato servo D1…</pre>
          </div>
          <div class="flow-card">
            <h4>3 · Stack sensori</h4>
            <p class="small muted">Avvia thread camere + LiDAR sulla NX prima di operare.</p>
            <div class="btn-row" style="margin-top:10px;">
              <button type="button" class="warn" onclick="nxStackStart()">Avvia camere + LiDAR</button>
              <button type="button" onclick="nxStackRefresh()">Aggiorna stato stack</button>
            </div>
            <pre id="nxStackBox" class="small compact-pre" style="margin-top:10px;">Carico stato…</pre>
          </div>
        </div>

        <div class="flow-grid">
          <div class="flow-card joint-editor-card">
            <h4>Controllo giunti (slider) — tempo reale + ZERO / START</h4>
            <p class="small muted" style="margin:0 0 8px;line-height:1.45;">
              Con <strong>Controllo live</strong> attivo, ogni spostamento cursore invia subito la posa al braccio (DDS rapido, senza spline lenta).
              <strong>Sposta tutti (smooth)</strong> = movimento interpolato (lento) con pre-hold antigravità — solo per salti grandi.
              Poi <strong>Salva ZERO</strong> / <strong>Salva START</strong> per memorizzare i gradi mostrati dagli slider.
            </p>
            <div class="btn-row" style="margin:0 0 10px;align-items:center;flex-wrap:wrap;gap:10px;">
              <label class="small" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
                <input type="checkbox" id="jointLiveEnabled" checked />
                <strong>Controllo live</strong> (cursore → robot)
              </label>
              <span id="jointLiveStatus" class="small muted" style="font-size:11px;">in attesa…</span>
            </div>
            <div class="joint-sliders" id="jointSlidersBlock">
              <div class="joint-slider-row"><span class="joint-lab">J0</span><input type="range" id="jointSlide0" min="-135" max="135" step="0.25" value="0" oninput="jointSliderLiveInput(0)" onchange="jointSliderLiveFlush()" /><span class="joint-val" id="jointSlideV0">0°</span></div>
              <div class="joint-slider-row"><span class="joint-lab">J1</span><input type="range" id="jointSlide1" min="-90" max="90" step="0.25" value="0" oninput="jointSliderLiveInput(1)" onchange="jointSliderLiveFlush()" /><span class="joint-val" id="jointSlideV1">0°</span></div>
              <div class="joint-slider-row"><span class="joint-lab">J2</span><input type="range" id="jointSlide2" min="-90" max="90" step="0.25" value="0" oninput="jointSliderLiveInput(2)" onchange="jointSliderLiveFlush()" /><span class="joint-val" id="jointSlideV2">0°</span></div>
              <div class="joint-slider-row"><span class="joint-lab">J3</span><input type="range" id="jointSlide3" min="-135" max="135" step="0.25" value="0" oninput="jointSliderLiveInput(3)" onchange="jointSliderLiveFlush()" /><span class="joint-val" id="jointSlideV3">0°</span></div>
              <div class="joint-slider-row"><span class="joint-lab">J4</span><input type="range" id="jointSlide4" min="-90" max="90" step="0.25" value="0" oninput="jointSliderLiveInput(4)" onchange="jointSliderLiveFlush()" /><span class="joint-val" id="jointSlideV4">0°</span></div>
              <div class="joint-slider-row"><span class="joint-lab">J5</span><input type="range" id="jointSlide5" min="-135" max="135" step="0.25" value="0" oninput="jointSliderLiveInput(5)" onchange="jointSliderLiveFlush()" /><span class="joint-val" id="jointSlideV5">0°</span></div>
              <div class="joint-slider-row"><span class="joint-lab">Gr</span><input type="range" id="jointSlide6" min="0" max="90" step="0.25" value="50" oninput="jointSliderLiveInput(6)" onchange="jointSliderLiveFlush()" /><span class="joint-val" id="jointSlideV6">50°</span></div>
            </div>
            <div class="btn-row" style="margin-top:14px;flex-wrap:wrap;">
              <button type="button" onclick="jointEditorLoad()">Leggi da robot → slider</button>
              <button type="button" class="primary-go" onclick="jointEditorGoto()">Sposta tutti (smooth)</button>
              <button type="button" class="green" onclick="jointEditorSaveZero()">Salva ZERO (slider)</button>
              <button type="button" class="green" onclick="jointEditorSaveStart()">Salva START (slider + scena)</button>
            </div>
            <pre id="jointEditorLog" class="small compact-pre" style="margin-top:10px;max-height:220px;">«Leggi da robot» per allineare gli slider al feedback; con Controllo live i cursori comandano subito il braccio.</pre>
          </div>
        </div>

        <p class="small muted" style="margin-top:16px;">
          Env: <code>GO2_GRASP_TAG_WAIT_S</code>, <code>GO2_GRASP_START_FOLD</code>. Ultimo POST / hold:
        </p>
        <pre id="armActionLog" class="small compact-pre" style="margin-top:8px;">Nessuna azione recente.</pre>
        </div>
      </div>

      <div id="op-go2" class="op-panel hide-when-drag-active" role="tabpanel" data-op-panel="go2">
        <p class="small muted">
          Comandi <strong>Sport</strong> sul quadrupede — <span class="warn">non il braccio D1</span>.
          Richiede <code>GO2_ENABLE_BASE_MOTION=1</code> sulla NX.
        </p>
        <div class="btn-row" style="margin-top:12px;">
          <button type="button" class="warn" onclick="basePose('crouch')" title="Sport StandDown">Abbassa il cane (crouch)</button>
          <button type="button" class="warn" onclick="basePose('stand_up')" title="StandUp + BalanceStand">Alza il cane (stand)</button>
        </div>
        <pre id="baseDogLog" class="small compact-pre" style="margin-top:12px; border-color:#7c2d12;">Risposta Sport…</pre>
        <details class="diag-acc" style="margin-top:10px;">
          <summary>Note Sport (timeout, errori)</summary>
          <p class="small muted" style="margin:8px 14px 14px;">
            Se vedi 403: abilita <code>GO2_ENABLE_BASE_MOTION=1</code> e <code>GO2_LOCAL=1</code>. Timeout: <code>GO2_SPORT_RPC_TIMEOUT_S</code>.
          </p>
        </details>
      </div>

      <div id="op-drag" class="op-panel" role="tabpanel" data-op-panel="drag">
        <div id="dragSessionBlock" class="drag-session-block">
          <h3 class="flow-h" style="margin-top:0;"><span class="step-num" style="background:#b45309;">✋</span> Drag — accompagna mano</h3>
          <p class="small muted">
            <strong>ECHO</strong> (default): ripete sul bus la posa letta <em>ogni ciclo</em> — «smollamento» via funcode 2.
            <strong>Stop drag</strong> → hold dove sei.
          </p>
          <div class="drag-tune-row" style="margin:12px 0 12px;">
            <label for="dragSweetSpot" class="small" style="display:block;font-weight:700;">Sweet spot — cursore (morbido ↔ reattivo)</label>
            <p class="small muted" style="margin:4px 0 8px;">
              Logging NX: <code>data/drag_follow_process.log</code>, <code>data/drag_follow_loop.log</code>, <code>data/drag_follow_diag.jsonl</code>.
            </p>
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
              <span class="small muted" style="white-space:nowrap;">ultra morbido</span>
              <input type="range" id="dragSweetSpot" min="0" max="100" value="50" step="1" style="flex:1;min-width:140px;" />
              <span class="small muted" style="white-space:nowrap;">reattivo</span>
            </div>
            <pre id="dragSweetSpotPreview" class="small compact-pre" style="margin-top:8px;max-height:168px;font-size:11px;">Muovi il cursore…</pre>
          </div>
          <div class="btn-row">
            <button type="button" class="warn" onclick="dragFollowStart()" title="mode=echo">Avvia drag — ECHO</button>
            <button type="button" onclick="dragFollowStartPassthrough()">Drag pass-through</button>
            <button type="button" onclick="dragFollowStartMirrorLegacy()">Mirror classico</button>
            <button type="button" onclick="dragFollowStop()">Stop drag (+ hold)</button>
            <button type="button" onclick="dragFollowFetchLog()">Leggi log mirror</button>
            <button type="button" onclick="dragFollowFetchDiagnostics()">Diagnostica completa</button>
          </div>
          <div id="dragFollowBadge" class="muted" style="margin-top:8px;">Stato drag-follow: —</div>
          <pre id="dragFollowStatus" class="compact-pre" style="margin-top:6px;">Carico stato…</pre>
          <pre id="dragFollowDiagLog" class="compact-pre" style="margin-top:8px; max-height:160px; font-size:11px;">Log diagnostico: —</pre>
          <pre id="dragFollowDiagBundle" class="compact-pre" style="margin-top:8px; max-height:280px; font-size:11px;">Bundle diagnostico: —</pre>
          <p class="small muted" style="margin-top:6px;">File NX: <code>data/drag_follow_loop.log</code>, <code>data/drag_follow_process.log</code></p>
        </div>
      </div>
    </section>

    <section class="card">
      <h2 style="flex-wrap:wrap;">Diagnostica &amp; test di rete <button type="button" style="margin-left:12px;font-size:13px;padding:7px 14px;" onclick="runAll()">Run All Tests</button> <span id="summary" class="small muted" style="font-weight:400;">Loading...</span></h2>
      <p class="small muted">Apri una riga per il JSON completo (compatto quando chiuso).</p>
      <div id="cards" class="diag-stack"></div>
    </section>
  </main>
  <script>
    /** Prefisso URL se dashboard dietro reverse-proxy (SCRIPT_NAME) o GO2_DASHBOARD_URL_PREFIX=/myprefix */
    var dashboardApi = (function() {
      var base = {{ script_root|default('')|tojson }};
      return function(path) {
        var p = path || '/';
        if (p.charAt(0) !== '/') p = '/' + p;
        if (!base) return p;
        return base + p;
      };
    })();
    function fetchHint_failedToFetch() {
      return ' Apri la dashboard dallo stesso indirizzo del server Flask (es. http://<host>:' + {{ dashboard_port|tojson }} + '), non da file locale. Se usi proxy/nginx con path, imposta GO2_DASHBOARD_URL_PREFIX.';
    }
    function statusClass(ok) { return ok ? 'ok' : 'bad'; }
    function setText(id, value) { const el = document.getElementById(id); if (el) el.textContent = value; }
    function setArmPoseLog(dataOrMsg) {
      const el = document.getElementById('armPoseLog');
      if (!el) return;
      el.textContent = typeof dataOrMsg === 'string' ? dataOrMsg : JSON.stringify(dataOrMsg, null, 2);
    }
    function truncatePreview(obj, max) {
      try {
        const s = JSON.stringify(obj);
        return s.length > max ? s.slice(0, max) + '…' : s;
      } catch (e) {
        return String(obj).slice(0, max);
      }
    }
    function renderCard(name, data) {
      const ok = data && data.ok;
      const title = name.replaceAll('_', ' ');
      let badges = '';
      if (data && data.detected) {
        badges += Object.entries(data.detected).map(([k, v]) => `<span class="pill ${v ? 'ok' : 'warn'}">${k}</span>`).join('');
      }
      if (data && data.ports) {
        badges += '<span class="pill">' + data.ports.filter(p => p.ok).length + '/' + data.ports.length + ' ports</span>';
      }
      if (data && data.common_apis) {
        badges += '<span class="pill ok">sport apis</span>';
      }
      const preview = truncatePreview(data, 220);
      const mark = ok ? '●' : '○';
      return `<details class="diag-acc">
        <summary><span class="${statusClass(ok)}">${mark}</span> <strong>${title}</strong> ${badges}<span class="muted">${preview}</span></summary>
        <pre>${JSON.stringify(data, null, 2)}</pre>
      </details>`;
    }
    async function refreshServerBoot() {
      const el = document.getElementById('serverBootBadge');
      if (!el) return;
      try {
        const res = await fetch(dashboardApi('/api/health?_=' + Date.now()), { cache: 'no-store' });
        const h = await res.json();
        let t =
          'Server dashboard · PID ' + (h.pid != null ? h.pid : '?')
          + ' · processo avviato ' + (h.process_started_at || '—')
          + ' · <code>diagnostics_dashboard.py</code> sul disco (mtime) ' + (h.dashboard_py_mtime || '—');
        if (h.reload_recommended) {
          el.className = 'small warn';
          el.innerHTML =
            t + ' — <strong>serve riavvio</strong> (codice aggiornato dopo l\'avvio; '
            + 'su NX es.: <code>python scripts/deploy_dashboard_to_nx.py</code> o kill PID + <code>python3 diagnostics_dashboard.py</code>).';
        } else {
          el.className = 'small muted';
          el.textContent = t + ' · Nessun deploy più nuovo del processo.';
        }
      } catch (e) {
        el.className = 'small warn';
        el.textContent = 'Impossibile leggere /api/health: ' + String(e);
      }
    }
    async function loadStatus() {
      try {
        const res = await fetch(dashboardApi('/api/status'));
        const data = await res.json();
        document.getElementById('summary').textContent = `${data.running ? 'Running...' : 'Updated ' + data.updated_at}: ${data.summary}`;
        const tests = data.tests || {};
        document.getElementById('cards').innerHTML = Object.entries(tests).map(([name, value]) => renderCard(name, value)).join('');
      } catch (e) {
        const el = document.getElementById('summary');
        if (el) el.textContent = 'Errore fetch /api/status — controlla console (F12): ' + String(e);
      }
    }
    async function runAll() {
      await fetch(dashboardApi('/api/run/all'), { method: 'POST' });
      await loadStatus();
    }
    function mirrorApriltagTabCanvasesFromStrip() {
      const pairs = [
        { src: 'alwaysBox0', dest: 'camtagBox0' },
        { src: 'alwaysBox6', dest: 'camtagBox6' },
      ];
      pairs.forEach(({ src, dest }) => {
        const imgEl = document.getElementById(src);
        const cv = document.getElementById(dest);
        if (!imgEl || imgEl.tagName !== 'IMG' || !cv || cv.tagName !== 'CANVAS') return;
        if (!imgEl.naturalWidth) return;
        const rect = cv.getBoundingClientRect();
        const w = Math.max(2, Math.floor(rect.width));
        const h = Math.max(2, Math.floor(rect.height));
        if (cv.width !== w || cv.height !== h) {
          cv.width = w;
          cv.height = h;
        }
        const ctx = cv.getContext('2d');
        ctx.drawImage(imgEl, 0, 0, w, h);
        cv.classList.remove('bad-frame');
      });
      requestAnimationFrame(mirrorApriltagTabCanvasesFromStrip);
    }
    function wireApriltagOverlayImgHandlers() {
      ['alwaysBox0', 'alwaysBox6'].forEach((id) => {
        const im = document.getElementById(id);
        if (!im) return;
        const dup = id === 'alwaysBox0' ? document.getElementById('camtagBox0') : document.getElementById('camtagBox6');
        im.addEventListener('error', function () {
          this.classList.add('bad-frame');
          if (dup) dup.classList.add('bad-frame');
        });
        im.addEventListener('load', function () {
          this.classList.remove('bad-frame');
          if (dup) dup.classList.remove('bad-frame');
        });
      });
    }
    async function refreshCameraStatus() {
      try {
        const res = await fetch(dashboardApi('/api/cameras/status'));
        const data = await res.json();
        let allOk = true;
        for (const dev of [0, 6]) {
          const c = (data.cameras || {})[String(dev)] || {};
          allOk = allOk && !!c.available;
          const line = `/dev/video${dev} - ${c.label || 'camera'} | ${c.available ? 'OK' : 'warming'} | age=${c.age_ms ?? '-'}ms | started=${c.started ? 'yes' : 'no'}${c.error ? ' | ' + c.error : ''}`;
          setText(`cam${dev}Status`, line);
          setText(`alwaysCam${dev}Status`, line);
        }
        setText('cameraStatus', allOk ? 'streaming' : 'warming');
        setText('alwaysCameraStatus', allOk ? 'cam OK' : 'cam warming');
        const el = document.getElementById('cameraStatus');
        if (el) el.className = allOk ? 'ok' : 'warn';
        const elA = document.getElementById('alwaysCameraStatus');
        if (elA) elA.className = 'pill ' + (allOk ? 'ok' : 'warn');
      } catch (e) {
        setText('cameraStatus', 'error');
        setText('alwaysCameraStatus', 'cam errore');
        const el = document.getElementById('cameraStatus');
        if (el) el.className = 'bad';
        const elA = document.getElementById('alwaysCameraStatus');
        if (elA) elA.className = 'pill bad';
      }
    }
    const APRIL_TAG_LOG_MAX = 48;
    let aprilTagLogLines = [];
    function appendAprilTagLog(data) {
      const t = new Date().toLocaleTimeString();
      const ok = !!data.ok;
      const parts = [];
      for (const dev of [0, 6]) {
        const c = (data.candidates || {})[String(dev)] || {};
        const det = c.tags;
        const list = (det && det.tags) ? det.tags : [];
        const err = c.error || (det && det.error);
        if (err) parts.push('cam' + dev + ': ERR ' + String(err).slice(0, 96));
        else if (!list.length) parts.push('cam' + dev + ': nessun tag');
        else parts.push('cam' + dev + ': id ' + list.map((x) => x.id).join(','));
      }
      const pipe = data.april_tag_pipeline && data.april_tag_pipeline.per_camera;
      let pipeLine = '';
      if (pipe) {
        const bits = [];
        for (const dev of [0, 6]) {
          const p = pipe[String(dev)] || {};
          const fo = p.frame_ok ? 'frame' : 'NO_FRAME';
          const sh = p.frame_shape_hw ? (p.frame_shape_hw[0] + 'x' + p.frame_shape_hw[1]) : '-';
          const age = (p.camera_cache && p.camera_cache.age_ms != null) ? (p.camera_cache.age_ms + 'ms') : '-';
          bits.push('v' + dev + ':' + fo + ' ' + sh + ' age~' + age);
        }
        const aruco = data.april_tag_pipeline.opencv_aruco_module ? 'aruco' : 'NO_ARUCO';
        pipeLine = ' | pipeline: ' + bits.join(' · ') + ' · ' + aruco;
      }
      const line = '[' + t + '] ok=' + ok + ' | ' + parts.join(' · ') + pipeLine;
      aprilTagLogLines.push(line);
      if (aprilTagLogLines.length > APRIL_TAG_LOG_MAX) aprilTagLogLines.shift();
      const el = document.getElementById('aprilTagLog');
      if (el) el.textContent = aprilTagLogLines.join('\n');
    }
    async function refreshBoxPlan() {
      try {
        const res = await fetch(dashboardApi('/api/box/plan'));
        const data = await res.json();
        const pre = document.getElementById('planSnapshot');
        if (pre) pre.textContent = JSON.stringify(data, null, 2);
        const label = data.ok ? 'target ok' : 'cerca tag';
        setText('boxStatus', label);
        const bs = document.getElementById('boxStatus');
        if (bs) bs.className = data.ok ? 'ok' : 'warn';
        setText('alwaysBoxStatus', label);
        const bsa = document.getElementById('alwaysBoxStatus');
        if (bsa) bsa.className = 'pill ' + (data.ok ? 'ok' : 'warn');
        updateBigTagState(data);
        updateTagSummaryFromPlan(data);
        appendAprilTagLog(data);
      } catch (e) {
        setText('boxStatus', 'error');
        setText('alwaysBoxStatus', 'planner errore');
        const bs = document.getElementById('boxStatus');
        if (bs) bs.className = 'bad';
        const bsa = document.getElementById('alwaysBoxStatus');
        if (bsa) bsa.className = 'pill bad';
        const t = new Date().toLocaleTimeString();
        aprilTagLogLines.push('[' + t + '] fetch /api/box/plan fallito: ' + String(e));
        if (aprilTagLogLines.length > APRIL_TAG_LOG_MAX) aprilTagLogLines.shift();
        const el = document.getElementById('aprilTagLog');
        if (el) el.textContent = aprilTagLogLines.join('\n');
      }
    }
    function updateBigTagState(data) {
      const tagBox = document.getElementById('tagStateBig');
      const ikBox = document.getElementById('ikStateBig');
      const gripBox = document.getElementById('gripStateBig');
      const detBox = document.getElementById('detectorStateBig');
      const vis = data.visible_summary || {};
      const s0 = vis['0'] || {};
      const s6 = vis['6'] || {};
      const boxSeen = !!data.box_tag_visible_any;
      const objectSeen = !!data.object_visible_any;
      const gripSeen = !!data.grip_point_visible_any;
      const anySeen = !!data.tag_visible_any;
      const ids = []
        .concat((s0.ids || []).map((x) => 'cam0:' + x))
        .concat((s6.ids || []).map((x) => 'cam6:' + x));
      if (tagBox) {
        tagBox.className = 'big-state ' + ((boxSeen || objectSeen) ? 'ok' : (anySeen ? 'warn' : 'bad'));
        tagBox.innerHTML =
          '<strong>' + (boxSeen ? 'TAG SCATOLA VISTO' : (objectSeen ? 'BOX DETECTED' : (anySeen ? 'SOLO LANDMARK' : 'NESSUNA SCATOLA'))) + '</strong>'
          + '<span class="small">tag: ' + (ids.length ? ids.join(', ') : 'nessuno')
          + ' · obj cam0=' + (s0.object_visible ? 'sì' : 'no') + ' cam6=' + (s6.object_visible ? 'sì' : 'no') + '</span>';
      }
      if (gripBox) {
        const gp = data.selected_grip_point || {};
        const err = gp.approach_error_px || [];
        const area = gp.box_area_px != null ? gp.box_area_px : '—';
        gripBox.className = 'big-state ' + (gripSeen ? 'ok' : 'bad');
        gripBox.innerHTML =
          '<strong>' + (gripSeen ? 'PUNTO PRESA OK' : 'PUNTO PRESA ASSENTE') + '</strong>'
          + '<span class="small">src: ' + (gp.source || '—')
          + ' · center: ' + (gp.grip_center_px ? gp.grip_center_px.join(',') : '—')
          + ' · err px: ' + (err.length ? err.join(',') : '—')
          + ' · area: ' + area + '</span>';
      }
      if (ikBox) {
        ikBox.className = 'big-state ' + (data.ok ? 'ok' : 'warn');
        ikBox.innerHTML =
          '<strong>' + (data.ok ? 'IK PRONTA' : 'IK NON PRONTA') + '</strong>'
          + '<span class="small">camera scelta: ' + (data.selected_camera ?? '—') + '</span>';
      }
      if (detBox) {
        const ds = data.object_detector || {};
        const backend0 = s0.object_backend || '—';
        const backend6 = s6.object_backend || '—';
        const hasModel = !!ds.model_exists;
        detBox.className = 'big-state ' + (objectSeen ? 'ok' : (hasModel ? 'warn' : 'warn'));
        detBox.innerHTML =
          '<strong>' + (objectSeen ? 'DETECTOR ATTIVO' : (hasModel ? 'MODELLO PRESENTE / NO BOX' : 'YOLO NON CONFIGURATO')) + '</strong>'
          + '<span class="small">cam0: ' + backend0 + ' · cam6: ' + backend6
          + ' · model: ' + (ds.model_path || 'nessuno') + '</span>';
      }
    }
    function updateTagSummaryFromPlan(data) {
      function htmlFor(dev) {
        const c = (data.candidates || {})[String(dev)] || {};
        const det = c.tags;
        const list = (det && det.tags) ? det.tags : [];
        const camErr = c.error || (det && det.error);
        if (camErr) {
          return '<span class="bad">Camera / planner:</span> ' + String(camErr).slice(0, 200);
        }
        if (!list.length) {
          if (c.object_detection && c.object_detection.ok) {
            return '<span class="ok">Box detector OK</span> · ' + (c.object_detection.backend || 'detector')
              + ' · conf ' + (c.object_detection.confidence ?? '—')
              + ' · grip ' + ((c.grip_point && c.grip_point.grip_center_px) ? c.grip_point.grip_center_px.join(',') : '—');
          }
          return '<span class="warn">Nessun AprilTag 0–3 o 5 e nessuna box detection</span> (o camera non pronta).';
        }
        const ids = list.map((t) => t.id).join(', ');
        const ik = c.preview && c.preview.ok;
        return (
          '<span class="ok">Sì — ' + list.length + ' tag (id: ' + ids + ')</span>'
          + (ik ? ' · <span class="ok">anteprima IK ok</span>' : ' · <span class="warn">IK non pronto (pose/niente target)</span>')
          + (c.object_detection && c.object_detection.ok ? ' · detector ' + (c.object_detection.backend || 'ok') : '')
        );
      }
      function one(dev) {
        const h = htmlFor(dev);
        const el = document.getElementById('tagSummary' + dev);
        if (el) el.innerHTML = h;
        const elA = document.getElementById('alwaysTag' + dev);
        if (elA) elA.innerHTML = h;
      }
      one(0);
      one(6);
    }
    async function refreshArmMotionDiag() {
      const pre = document.getElementById('armMotionDiagPre');
      try {
        const res = await fetch(dashboardApi('/api/arm/diagnose_motion?_=' + Date.now()));
        const d = await res.json();
        const hints = d.hints || [];
        const lines = [];
        lines.push('GO2_LOCAL=' + (d.go2_local ? '1' : '0'));
        lines.push('GO2_ENABLE_REAL_ARM (effettivo)=' + (d.real_arm_env ? '1' : '0'));
        lines.push('Feedback servo DDS: ' + (d.servo_feedback_ok ? 'OK' : 'NO'));
        lines.push('start_alignment.json: ' + (d.start_alignment_json ? 'presente' : 'manca'));
        lines.push('true_zero_pose.json: ' + (d.true_zero_json ? 'presente' : 'manca'));
        lines.push('Ultimo job: ' + (d.last_job_status || '—'));
        if (d.command_stack) lines.push('command_stack: ' + JSON.stringify(d.command_stack));
        lines.push('');
        lines.push('— Motivi / note —');
        hints.forEach((h) => lines.push('• ' + h));
        if (pre) pre.textContent = lines.join('\n');
      } catch (e) {
        if (pre) pre.textContent = 'Errore /api/arm/diagnose_motion: ' + String(e);
      }
    }
    let uiTuningPostTimer = null;
    const UI_TUNING_KEYS = ['tag_wait_s', 'visible_plan_wait_s', 'search_max_cycles', 'search_delay_ms', 'plan_delay_ms'];
    function tuneSliderLabels() {
      UI_TUNING_KEYS.forEach((k) => {
        const inp = document.getElementById('tune_' + k);
        const lab = document.getElementById('tune_' + k + '_v');
        if (inp && lab) lab.textContent = '(' + inp.value + ')';
      });
    }
    async function loadUiTuning() {
      const st = document.getElementById('uiTuningStatus');
      try {
        const res = await fetch(dashboardApi('/api/arm/ui_tuning?_=' + Date.now()));
        const j = await res.json();
        const eff = j.effective || {};
        UI_TUNING_KEYS.forEach((k) => {
          const inp = document.getElementById('tune_' + k);
          if (inp && eff[k] !== undefined) inp.value = String(eff[k]);
        });
        tuneSliderLabels();
        if (st) st.textContent = ' sincronizzato';
      } catch (e) {
        if (st) st.textContent = ' errore lettura';
      }
    }
    async function postUiTuningFromSliders() {
      const st = document.getElementById('uiTuningStatus');
      const body = {};
      UI_TUNING_KEYS.forEach((k) => {
        const inp = document.getElementById('tune_' + k);
        if (inp) {
          const v = parseFloat(inp.value);
          if (!Number.isNaN(v)) body[k] = v;
        }
      });
      try {
        const res = await fetch(dashboardApi('/api/arm/ui_tuning'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const j = await res.json();
        tuneSliderLabels();
        if (st) st.textContent = j.ok ? ' salvato' : (' errori: ' + (j.errors || []).join('; '));
      } catch (e) {
        if (st) st.textContent = ' POST fallito';
      }
    }
    function scheduleUiTuningPost() {
      clearTimeout(uiTuningPostTimer);
      tuneSliderLabels();
      uiTuningPostTimer = setTimeout(postUiTuningFromSliders, 350);
    }
    async function resetUiTuning() {
      await fetch(dashboardApi('/api/arm/ui_tuning'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reset: true }),
      });
      await loadUiTuning();
    }
    async function loadGraspSessionIntoUi() {
      try {
        const res = await fetch(dashboardApi('/api/arm/grasp_session?_=' + Date.now()));
        const j = await res.json();
        const e = j.effective || {};
        const t = (id, v) => {
          const el = document.getElementById(id);
          if (el) el.checked = !!v;
        };
        t('gs_trust_wrist', e.trust_wrist_absolute_ik);
        t('gs_fused_ik', e.use_fused_plan_ik);
        t('gs_fused_center', e.fused_with_center);
        t('gs_front_fallback', e.front_camera_fallback_grasp);
        t('gs_prefer_tag', e.prefer_tag_grip);
        t('gs_execute_arm', e.grasp_execute_arm);
      } catch (err) {}
    }
    async function applyGraspSessionFromUi() {
      const st = document.getElementById('graspSessionStatus');
      const body = {
        trust_wrist_absolute_ik: !!document.getElementById('gs_trust_wrist')?.checked,
        use_fused_plan_ik: !!document.getElementById('gs_fused_ik')?.checked,
        fused_with_center: !!document.getElementById('gs_fused_center')?.checked,
        front_camera_fallback_grasp: !!document.getElementById('gs_front_fallback')?.checked,
        prefer_tag_grip: !!document.getElementById('gs_prefer_tag')?.checked,
        grasp_execute_arm: !!document.getElementById('gs_execute_arm')?.checked,
      };
      try {
        const res = await fetch(dashboardApi('/api/arm/grasp_session'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const j = await res.json();
        if (st) st.textContent = j.ok ? ' sessione salvata' : (' errori: ' + (j.errors || []).join('; '));
        await refreshGraspPipeline();
        await refreshGraspJobPanel();
      } catch (e) {
        if (st) st.textContent = ' POST fallito';
      }
    }
    async function resetGraspSessionUi() {
      const st = document.getElementById('graspSessionStatus');
      try {
        await fetch(dashboardApi('/api/arm/grasp_session'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reset: true }),
        });
        await loadGraspSessionIntoUi();
        if (st) st.textContent = ' sessione reset (usa env)';
        await refreshGraspPipeline();
        await refreshGraspJobPanel();
      } catch (e) {
        if (st) st.textContent = ' reset fallito';
      }
    }
    async function graspAfterCrouch() {
      const st = document.getElementById('presaSequenceStatus');
      if (st) st.textContent = 'Crouch + grasp: invio POST…';
      try {
        const res = await fetch(dashboardApi('/api/arm/grasp_box/attempt_after_crouch'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ settle_s: 1.25 }),
        });
        const j = await res.json();
        if (st) st.textContent = j.message || JSON.stringify(j);
        setTimeout(refreshGraspJobPanel, 400);
        setTimeout(refreshGraspJobPanel, 2000);
      } catch (e) {
        if (st) st.textContent = 'Errore: ' + String(e);
      }
    }
    async function refreshGraspPipeline() {
      const pre = document.getElementById('graspPipelinePre');
      try {
        const res = await fetch(dashboardApi('/api/arm/grasp_pipeline?_=' + Date.now()));
        const d = await res.json();
        const lines = (d.narrative_it || []).join('\n');
        const compact = {
          updated_at: d.updated_at,
          environment: d.environment,
          grasp_trigger_params: d.grasp_trigger_params,
          effective_grasp_flags: d.effective_grasp_flags,
          fusion_plan_ok: d.fusion_plan_ok,
          fusion_ready_for_execute: d.fusion_ready_for_execute,
          grip_detection_any: d.grip_detection_any,
          selected_camera: d.selected_camera,
          candidates: d.candidates,
          diagnose_hints: d.diagnose_hints,
        };
        if (pre) pre.textContent = lines + '\n\n— JSON compatto —\n' + JSON.stringify(compact, null, 2);
      } catch (e) {
        if (pre) pre.textContent = 'Errore /api/arm/grasp_pipeline: ' + String(e);
      }
    }
    async function refreshDetectionNow() {
      const hint = document.getElementById('detectionPulse');
      if (hint) hint.textContent = 'aggiornamento…';
      try {
        await Promise.all([refreshBoxPlan(), refreshCameraStatus()]);
        if (hint) {
          const t = new Date().toLocaleTimeString();
          hint.textContent = 'aggiornato ' + t;
          setTimeout(() => {
            if (hint && hint.textContent === 'aggiornato ' + t) hint.textContent = '';
          }, 5000);
        }
      } catch (e) {
        if (hint) hint.textContent = 'errore';
      }
    }
    async function refreshGraspJobPanel() {
      const hint = document.getElementById('graspJobHint');
      const big = document.getElementById('graspPhaseBig');
      const pre = document.getElementById('graspJobDetailPre');
      try {
        const res = await fetch(dashboardApi('/api/arm/job_status?_=' + Date.now()));
        const data = await res.json();
        const st = data.status;
        const phaseIt = data.phase_label_it || (data.detail || {}).phase_label_it || '';
        const fullJson = JSON.stringify(data, null, 2);
        if (pre) pre.textContent = fullJson;
        const motionBox = document.getElementById('motionStateBig');
        if (motionBox) {
        const moving = st === 'running' || st === 'starting';
          const holding = st === 'idle' && phaseIt && phaseIt.toLowerCase().includes('hold');
          motionBox.className = 'big-state ' + (moving ? 'warn' : (holding ? 'ok' : 'warn'));
          motionBox.innerHTML =
            '<strong>' + (moving ? (st === 'starting' ? 'PREFLIGHT / AVVIO' : 'MOVIMENTO / SEQUENZA ATTIVA') : (holding ? 'HOLD ATTIVO' : 'MOTO FERMO')) + '</strong>'
            + '<span class="small">' + (phaseIt || st || '—') + '</span>';
        }
        const evPre = document.getElementById('graspEventLogPre');
        if (evPre) {
          const events = data.events || [];
          evPre.textContent = events.slice(-28).map((e) => {
            return '[' + (e.t || '') + '] ' + (e.kind || 'event') + ' · ' + (e.message || '');
          }).join('\n') || 'Nessun evento grasp ancora.';
        }
        if (big) {
          if (st === 'starting') {
            big.innerHTML =
              '<span class="warn">●</span> '
              + (phaseIt || 'Preflight camere/IK in corso… attendi qualche secondo.');
          } else if (st === 'running' && phaseIt) {
            big.innerHTML = '<span class="ok">●</span> ' + phaseIt;
          } else if (st === 'running') {
            big.innerHTML = '<span class="warn">●</span> Sequenza in corso… (in attesa di aggiornamento fase)';
          } else if (st === 'idle') {
            const pl = phaseIt || ((data.detail || {}).phase_label_it) || '';
            big.innerHTML = pl
              ? ('<span class="ok">Pronto</span> — ' + pl)
              : '<span class="ok">Pronto</span> — puoi avviare una nuova sequenza.';
          } else if (st === 'completed') {
            big.innerHTML = '<span class="ok">Completata</span>' + (phaseIt ? ' — ' + phaseIt : '');
          } else if (st === 'finished_no_ok') {
            const r = (data.detail || {}).result || {};
            const why = (r.reason || r.grasp_policy || '').toString();
            big.innerHTML =
              '<span class="bad">Terminata senza OK</span>'
              + (why ? ' — ' + why : '')
              + ' <span class="small muted">· vedi JSON e pannello «Flusso visione» sopra</span>';
          } else if (st === 'emergency_hold' || st === 'aborted') {
            big.innerHTML = '<span class="warn">Stato vecchio («' + st + '»)</span> — aggiorna (F5) o premi «FERMA» di nuovo; dopo deploy dovresti vedere «Pronto».';
          } else if (st === 'error') {
            big.innerHTML = '<span class="bad">Errore worker</span> — vedi JSON';
          } else {
            big.innerHTML = '<span class="muted">Job: ' + (st || '—') + '</span>';
          }
        }
        if (hint) {
          let tail = phaseIt || '';
          if (!tail && data.detail && data.detail.result) {
            const r = data.detail.result;
            tail = (r.grasp_policy || r.reason || '').toString().slice(0, 90);
          }
          hint.textContent = tail ? st + ' · ' + tail : (st || '—');
        }
        const genv = document.getElementById('graspEnvBadge');
        if (genv && data.environment) {
          const e = data.environment;
          const ra = String(e.GO2_ENABLE_REAL_ARM || '').toLowerCase();
          const raOn = ra === '1' || ra === 'true' || ra === 'yes';
          const loc = !!e.GO2_LOCAL;
          genv.innerHTML =
            '<span class="pill ' + (raOn ? 'ok' : 'bad') + '">REAL_ARM '
            + (e.GO2_ENABLE_REAL_ARM || '0') + '</span> '
            + '<span class="pill ' + (loc ? 'ok' : 'warn') + '">GO2_LOCAL '
            + (loc ? '1' : '0') + '</span> '
            + '<span class="pill warn">FUSED_IK ' + (e.GO2_GRASP_USE_FUSED_PLAN_IK || '0') + '</span> '
            + '<span class="pill ' + (String(e.GO2_GRASP_EXECUTE_ARM || '0') === '1' ? 'bad' : 'ok') + '">EXECUTE_ARM '
            + (e.GO2_GRASP_EXECUTE_ARM || '0') + '</span> '
            + '<span class="pill warn">FR_FALLBACK ' + (e.GO2_FRONT_CAMERA_FALLBACK_GRASP || '0') + '</span> '
            + '<span class="pill ' + (String(e.GO2_TRUST_WRIST_ABSOLUTE_IK || '0') === '1' ? 'ok' : 'warn') + '">TRUST_WRIST '
            + (e.GO2_TRUST_WRIST_ABSOLUTE_IK || '0') + '</span> '
            + '<span class="pill ' + (String(e.GO2_GRASP_PREFER_TAG_GRIP || '0') === '1' ? 'ok' : 'warn') + '">TAG_GRIP '
            + (e.GO2_GRASP_PREFER_TAG_GRIP || '0') + '</span>';
        }
      } catch (e) {
        if (hint) hint.textContent = '—';
        if (big) big.innerHTML = '<span class="bad">Errore lettura job</span>';
      }
    }
    function toggleAlwaysCamDetails() {
      const strip = document.getElementById('alwaysCamStrip');
      if (!strip) return;
      const expanded = strip.classList.toggle('expanded');
      try { localStorage.setItem('go2_always_cam_expanded', expanded ? '1' : '0'); } catch (err) {}
    }
    function openOpTab(id) {
      const btn = document.querySelector('.op-tab[data-op-tab="' + id + '"]');
      if (btn) btn.click();
      const panel = document.getElementById('opPanel');
      if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    function initOpTabs() {
      const tabs = document.querySelectorAll('.op-tab');
      function activate(id) {
        tabs.forEach((btn) => {
          const on = btn.dataset.opTab === id;
          btn.classList.toggle('is-active', on);
          btn.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        document.querySelectorAll('[data-op-panel]').forEach((p) => {
          p.classList.toggle('is-active', p.dataset.opPanel === id);
        });
        try {
          localStorage.setItem('go2_op_tab', id);
        } catch (err) {}
      }
      tabs.forEach((btn) => btn.addEventListener('click', () => activate(btn.dataset.opTab)));
      let initial = 'camtag';
      try {
        const s = localStorage.getItem('go2_op_tab');
        const legacy = { presa: 'graspseq', visione: 'camtag' };
        const key = (legacy[s] || s);
        if (key && document.getElementById('op-' + key)) initial = key;
      } catch (err) {}
      activate(initial);
      window.openOpTab = openOpTab;
    }
    async function basePose(mode) {
      const logEl = document.getElementById('baseDogLog');
      const modeLabel =
        mode === 'crouch'
          ? 'crouch = Abbassa il CANE (Sport StandDown), non il braccio'
          : mode === 'stand_up'
            ? 'stand_up = Alza il CANE (StandUp+BalanceStand)'
            : String(mode);
      try {
        if (logEl) logEl.textContent = 'Invio Sport… ' + modeLabel;
        const res = await fetch(dashboardApi('/api/base/accompany_mode'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: mode || 'stand_up' }),
          signal: AbortSignal.timeout(60000),
        });
        const data = await res.json();
        const head = modeLabel + '\nHTTP ' + res.status + '\n';
        if (logEl) logEl.textContent = head + JSON.stringify(data, null, 2);
        nxStackRefresh();
      } catch (e) {
        const msg =
          e && e.name === 'AbortError'
            ? 'timeout 60s — Sport RPC troppo lento o rete bloccata (server usa GO2_SPORT_RPC_TIMEOUT_S).'
            : String(e);
        if (logEl) logEl.textContent = modeLabel + '\n' + msg;
      }
    }
    async function armServoSnapshot() {
      try {
        const res = await fetch(dashboardApi('/api/arm/servo_snapshot'));
        const data = await res.json();
        setArmPoseLog(data);
      } catch (e) {
        setArmPoseLog(String(e));
      }
    }
    async function armSavePoseSnapshot() {
      const label = window.prompt('Nome pose (opzionale):', 'pose');
      if (label === null) return;
      try {
        const res = await fetch(dashboardApi('/api/arm/save_pose_snapshot'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label: label.trim() || 'pose' }),
        });
        const data = await res.json();
        setArmPoseLog(data);
        nxStackRefresh();
      } catch (e) {
        setArmPoseLog(String(e));
      }
    }
    async function armHoldPose() {
      try {
        const res = await fetch(dashboardApi('/api/arm/hold_pose'), { method: 'POST' });
        const data = await res.json();
        setArmPoseLog(data);
      } catch (e) {
        setArmPoseLog(String(e));
      }
    }
    async function armTeachStub() {
      try {
        const res = await fetch(dashboardApi('/api/arm/teach_mode'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enable: true }),
        });
        const data = await res.json();
        setArmPoseLog(data);
      } catch (e) {
        setArmPoseLog(String(e));
      }
    }
    async function saveTrueZeroPose() {
      try {
        const res = await fetch(dashboardApi('/api/arm/true_zero'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ op: 'save' }),
        });
        const data = await res.json();
        setArmPoseLog(data);
        refreshArmMotionDiag();
      } catch (e) {
        setArmPoseLog(String(e));
      }
    }
    const JOINT_SLIDER_BOUNDS = [
      [-135, 135],
      [-90, 90],
      [-90, 90],
      [-135, 135],
      [-90, 90],
      [-135, 135],
      [0, 90],
    ];
    function jointSliderDisplay(i) {
      const s = document.getElementById('jointSlide' + i);
      const v = document.getElementById('jointSlideV' + i);
      if (s && v) v.textContent = parseFloat(s.value).toFixed(1) + '°';
    }
    function jointSlidersInitDisplay() {
      for (let i = 0; i < 7; i++) jointSliderDisplay(i);
      const st = document.getElementById('jointLiveStatus');
      const cb = document.getElementById('jointLiveEnabled');
      if (st) st.textContent = (cb && cb.checked) ? 'Live attivo: sposta i cursori per comandare.' : 'Live disattivato.';
    }
    let _jointLiveRaf = 0;
    function jointSliderLiveInput(i) {
      jointSliderDisplay(i);
      const cb = document.getElementById('jointLiveEnabled');
      const st = document.getElementById('jointLiveStatus');
      if (!cb || !cb.checked) {
        if (st) st.textContent = 'Live disattivato.';
        return;
      }
      if (_jointLiveRaf) return;
      _jointLiveRaf = requestAnimationFrame(function() {
        _jointLiveRaf = 0;
        jointSendLivePose();
      });
    }
    function jointSliderLiveFlush() {
      for (let i = 0; i < 7; i++) jointSliderDisplay(i);
      const cb = document.getElementById('jointLiveEnabled');
      if (!cb || !cb.checked) return;
      if (_jointLiveRaf) {
        cancelAnimationFrame(_jointLiveRaf);
        _jointLiveRaf = 0;
      }
      jointSendLivePose();
    }
    async function jointSendLivePose() {
      const sd = jointEditorCollectFromSliders();
      if (!sd) return;
      const st = document.getElementById('jointLiveStatus');
      try {
        const res = await fetch(dashboardApi('/api/arm/joints/live_deg'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ servo_deg: sd }),
        });
        const data = await res.json();
        if (st) {
          if (data.ok || data.skipped) {
            st.textContent = 'live OK · ' + new Date().toLocaleTimeString();
          } else {
            st.textContent = 'live: ' + (data.reason || ('HTTP ' + res.status));
          }
        }
      } catch (e) {
        if (st) {
          const m = String(e);
          st.textContent = m + (m.indexOf('Failed to fetch') >= 0 ? fetchHint_failedToFetch() : '');
        }
      }
    }
    function jointEditorCollectFromSliders() {
      const out = [];
      for (let i = 0; i < 7; i++) {
        const s = document.getElementById('jointSlide' + i);
        if (!s) return null;
        out.push(parseFloat(s.value));
      }
      return out;
    }
    async function jointEditorLoad() {
      const pre = document.getElementById('jointEditorLog');
      try {
        const res = await fetch(dashboardApi('/api/arm/servo_snapshot?_=' + Date.now()));
        const data = await res.json();
        if (!data.ok || !data.servo_deg) {
          if (pre) pre.textContent = JSON.stringify(data, null, 2);
          return;
        }
        const sd = data.servo_deg;
        for (let i = 0; i < 7; i++) {
          const el = document.getElementById('jointSlide' + i);
          if (!el) continue;
          const b = JOINT_SLIDER_BOUNDS[i];
          let val = parseFloat(sd[i]);
          if (Number.isNaN(val)) val = 0;
          val = Math.min(b[1], Math.max(b[0], val));
          el.value = String(val);
          jointSliderDisplay(i);
        }
        if (pre) pre.textContent = 'Feedback DDS → slider:\n' + JSON.stringify(sd, null, 2);
      } catch (e) {
        const m = String(e);
        if (pre) pre.textContent = m + (m.indexOf('Failed to fetch') >= 0 ? fetchHint_failedToFetch() : '');
      }
    }
    async function jointEditorSaveZero() {
      const sd = jointEditorCollectFromSliders();
      if (!sd) return;
      const pre = document.getElementById('jointEditorLog');
      try {
        const res = await fetch(dashboardApi('/api/arm/true_zero'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ op: 'save', servo_deg: sd }),
        });
        const data = await res.json();
        if (pre) pre.textContent = JSON.stringify(data, null, 2);
        setArmPoseLog(data);
        refreshArmMotionDiag();
      } catch (e) {
        if (pre) pre.textContent = String(e);
      }
    }
    async function jointEditorSaveStart() {
      if (!window.confirm('Salvo START: scena AprilTag + angoli braccio dagli slider. Confermi?')) return;
      const sd = jointEditorCollectFromSliders();
      if (!sd) return;
      const pre = document.getElementById('jointEditorLog');
      const logStart = document.getElementById('startOpsLog');
      try {
        const res = await fetch(dashboardApi('/api/alignment/start_pose'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ servo_deg: sd }),
        });
        const data = await res.json();
        if (pre) pre.textContent = 'Salva START (slider) HTTP ' + res.status + ':\n' + JSON.stringify(data, null, 2);
        if (logStart) logStart.textContent = JSON.stringify(data, null, 2);
        refreshArmMotionDiag();
        nxStackRefresh();
      } catch (e) {
        if (pre) pre.textContent = String(e);
      }
    }
    async function jointEditorGoto() {
      const sd = jointEditorCollectFromSliders();
      if (!sd) return;
      if (!window.confirm('Sposta tutti i giunti in modo smooth (interpolato, più lento del live). Confermi?')) return;
      const pre = document.getElementById('jointEditorLog');
      try {
        const res = await fetch(dashboardApi('/api/arm/joints/goto_deg'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ servo_deg: sd }),
        });
        const data = await res.json();
        if (pre) pre.textContent = JSON.stringify(data, null, 2);
        const logEl = document.getElementById('armActionLog');
        if (logEl) logEl.textContent = 'goto_deg HTTP ' + res.status + '\n' + JSON.stringify(data, null, 2);
        refreshArmMotionDiag();
      } catch (e) {
        if (pre) pre.textContent = String(e);
      }
    }
    async function gotoTrueZeroPose() {
      if (!window.confirm('Portare il braccio alla posa ZERO (data/true_zero_pose.json)?')) return;
      try {
        const res = await fetch(dashboardApi('/api/arm/true_zero'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ op: 'goto_zero' }),
        });
        const data = await res.json();
        setArmPoseLog(data);
        const logEl = document.getElementById('armActionLog');
        if (logEl) {
          const err = !data.ok ? 'ERRORE — nessun movimento o comando rifiutato. Controlla reason / hint sotto.\n' : '';
          logEl.textContent = err + 'TRUE_ZERO goto_zero HTTP ' + res.status + '\n' + JSON.stringify(data, null, 2);
        }
        refreshArmMotionDiag();
      } catch (e) {
        setArmPoseLog(String(e));
      }
    }
    async function gotoSavedStartPose() {
      const logEl = document.getElementById('armActionLog');
      const startLog = document.getElementById('startOpsLog');
      const pending = 'Invio comando: Vai a START diretto…';
      if (logEl) logEl.textContent = pending;
      if (startLog) startLog.textContent = pending;
      try {
        const res = await fetch(dashboardApi('/api/arm/true_zero'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ op: 'goto_saved_start' }),
        });
        const data = await res.json();
        setArmPoseLog(data);
        const msg = 'Vai a START HTTP ' + res.status + '\n' + JSON.stringify(data, null, 2);
        if (logEl) {
          const err = !data.ok ? 'ERRORE — START diretto non eseguito. reason=' + (data.reason || 'unknown') + '\n' : '';
          logEl.textContent = err + msg + '\n\nVerifico subito se da START si vede la scatola…';
        }
        if (startLog) startLog.textContent = msg;
        refreshArmMotionDiag();
        await refreshDetectionNow();
        if (logEl) {
          logEl.textContent += '\nSTART visibility aggiornata: guarda VISIONE / PRESA sopra.';
        }
      } catch (e) {
        setArmPoseLog(String(e));
      }
    }
    async function gotoStartFromTrueZero() {
      if (!window.confirm('ZERO → START: percorso interpolato verso start_alignment.json. Confermi?')) return;
      try {
        const res = await fetch(dashboardApi('/api/arm/true_zero'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ op: 'goto_start' }),
        });
        const data = await res.json();
        setArmPoseLog(data);
        const logEl = document.getElementById('armActionLog');
        if (logEl) {
          const err = !data.ok ? 'ERRORE — nessun movimento o comando rifiutato. Controlla reason / hint sotto.\n' : '';
          logEl.textContent = err + 'ZERO → START HTTP ' + res.status + '\n' + JSON.stringify(data, null, 2)
            + '\n\nVerifico subito se da START si vede la scatola…';
        }
        refreshArmMotionDiag();
        await refreshDetectionNow();
        if (logEl) {
          logEl.textContent += '\nSTART visibility aggiornata: guarda VISIONE / PRESA sopra (box/tag visto, punto presa, camera scelta).';
        }
      } catch (e) {
        setArmPoseLog(String(e));
      }
    }
    async function refreshDragFollowStatus() {
      const badge = document.getElementById('dragFollowBadge');
      const pre = document.getElementById('dragFollowStatus');
      try {
        const res = await fetch(dashboardApi('/api/arm/drag_follow'));
        const data = await res.json();
        document.body.classList.toggle('drag-follow-running', !!(data && data.running));
        const mode = (data.params && data.params.mode) || data.mode || '';
        if (data.running) {
          const tail = mode === 'mirror'
            ? (' · η=' + (data.params && data.params.track_eta))
            : mode === 'passthrough'
              ? (' · α=' + (data.params && data.params.passthrough_alpha))
              : mode === 'echo'
                ? (' · eco · lead=' + (data.params && data.params.echo_base_lead))
                : '';
          badge.innerHTML = '<span class="ok">● ATTIVO</span> · ' + (mode || 'drag') + tail
            + ' · PID ' + (data.pid || '?')
            + ' · ~' + (data.remaining_s != null ? data.remaining_s : '?') + 's rimasti';
          badge.className = '';
          pre.classList.add('live');
          pre.textContent = JSON.stringify(data, null, 2);
        } else {
          badge.innerHTML = '<span class="bad">● FERMO</span>';
          badge.className = '';
          pre.classList.remove('live');
          pre.textContent = JSON.stringify(data, null, 2);
        }
      } catch (e) {
        document.body.classList.remove('drag-follow-running');
        if (badge) badge.textContent = 'Stato drag-follow: errore rete';
        if (pre) {
          pre.textContent = String(e);
          pre.classList.remove('live');
        }
      }
    }
    function dragSweetSpotT() {
      const el = document.getElementById('dragSweetSpot');
      if (!el) return 0.0;
      const v = Number(el.value);
      if (Number.isNaN(v)) return 0.0;
      // Scala v2: 50 corrisponde al vecchio estremo sinistro/morbido; 0..50 resta ultra-soft.
      return Math.max(0, Math.min(1, (v - 50) / 50));
    }
    function lerpDrag(a, b, t) {
      return a + (b - a) * t;
    }
    function dragFollowEchoFromT(t) {
      return {
        hz: Math.round(lerpDrag(7, 52, t)),
        command_delay_ms: Math.round(lerpDrag(52, 2, t)),
        echo_base_lead: Math.round(lerpDrag(0.0, 2.85, t) * 1000) / 1000,
        echo_lead_cap_deg: Math.round(lerpDrag(1.8, 18.0, t) * 10) / 10,
      };
    }
    function dragFollowPassthroughFromT(t) {
      return {
        hz: Math.round(lerpDrag(7, 50, t)),
        command_delay_ms: Math.round(lerpDrag(48, 2, t)),
        passthrough_alpha: Math.round(lerpDrag(0.1, 1.0, t) * 100) / 100,
        passthrough_max_step_deg: Math.round(lerpDrag(3.0, 23.0, t) * 10) / 10,
      };
    }
    function dragFollowMirrorFromT(t) {
      return {
        hz: Math.round(lerpDrag(7, 42, t)),
        command_delay_ms: Math.round(lerpDrag(52, 3, t)),
        track_eta: Math.round(lerpDrag(0.38, 0.945, t) * 1000) / 1000,
        mirror_max_step_deg: Math.round(lerpDrag(1.0, 12.0, t) * 10) / 10,
        mirror_base_eta_scale: Math.round(lerpDrag(1.0, 4.85, t) * 100) / 100,
        mirror_base_cap_scale: Math.round(lerpDrag(1.0, 3.2, t) * 100) / 100,
      };
    }
    function dragFollowSharedStatics() {
      return {
        seconds: 300,
        gain: 0.18,
        smooth: 0.55,
        max_step_deg: 0.55,
        deadband_deg: 0.04,
        echo_heavy_joint_count: 4,
        echo_decimals_heavy: 5,
        echo_decimals_rest: 3,
        gripper_mirror_scale: 1.0,
      };
    }
    function dragSweetSpotUpdateLabel() {
      const pre = document.getElementById('dragSweetSpotPreview');
      if (!pre) return;
      const t = dragSweetSpotT();
      const e = dragFollowEchoFromT(t);
      const p = dragFollowPassthroughFromT(t);
      const m = dragFollowMirrorFromT(t);
      const e0 = dragFollowEchoFromT(0), e1 = dragFollowEchoFromT(1);
      const p0 = dragFollowPassthroughFromT(0), p1 = dragFollowPassthroughFromT(1);
      const m0 = dragFollowMirrorFromT(0), m1 = dragFollowMirrorFromT(1);
      const br = String.fromCharCode(10);
      pre.textContent = [
        'Scala v2: cursore 50 = vecchio estremo sinistro (molto morbido). 0..50 = ultra morbido.',
        'ECHO      hz=' + e.hz + '  delay_ms=' + e.command_delay_ms + '  lead=' + e.echo_base_lead + '  lead_cap°=' + e.echo_lead_cap_deg,
        'PASS-THR  hz=' + p.hz + '  delay_ms=' + p.command_delay_ms + '  α=' + p.passthrough_alpha + '  max_step°=' + p.passthrough_max_step_deg,
        'MIRROR    hz=' + m.hz + '  delay_ms=' + m.command_delay_ms + '  η=' + m.track_eta + '  max_step°=' + m.mirror_max_step_deg + '  base_η×=' + m.mirror_base_eta_scale,
        '──────── Estremi effettivi: 50 (vecchio morbido) ⇄ 100 (reattivo) ────────',
        'ECHO      0→ hz=' + e0.hz + ' delay=' + e0.command_delay_ms + ' lead=' + e0.echo_base_lead + ' cap°=' + e0.echo_lead_cap_deg + '   |   100→ hz=' + e1.hz + ' delay=' + e1.command_delay_ms + ' lead=' + e1.echo_base_lead + ' cap°=' + e1.echo_lead_cap_deg,
        'PASS      0→ hz=' + p0.hz + ' α=' + p0.passthrough_alpha + ' step°=' + p0.passthrough_max_step_deg + '   |   100→ hz=' + p1.hz + ' α=' + p1.passthrough_alpha + ' step°=' + p1.passthrough_max_step_deg,
        'MIRROR    0→ η=' + m0.track_eta + ' step°=' + m0.mirror_max_step_deg + ' baseη×=' + m0.mirror_base_eta_scale + '   |   100→ η=' + m1.track_eta + ' step°=' + m1.mirror_max_step_deg + ' baseη×=' + m1.mirror_base_eta_scale,
      ].join(br);
      try {
        localStorage.setItem('go2_drag_sweet_spot_v2', String(Math.round(Number(document.getElementById('dragSweetSpot').value) || 50)));
      } catch (err) {}
    }
    async function dragFollowFetchLog() {
      const el = document.getElementById('dragFollowDiagLog');
      try {
        const res = await fetch(dashboardApi('/api/arm/drag_follow/log?lines=120'));
        const data = await res.json();
        el.textContent = data.loop_log || JSON.stringify(data, null, 2);
      } catch (e) {
        el.textContent = String(e);
      }
    }
    async function dragFollowFetchDiagnostics() {
      const el = document.getElementById('dragFollowDiagBundle');
      try {
        const res = await fetch(dashboardApi('/api/arm/drag_follow/diagnostics?servo=1&lines_process=80&lines_loop=120&lines_jsonl=60'));
        const data = await res.json();
        el.textContent = JSON.stringify(data, null, 2);
      } catch (e) {
        el.textContent = String(e);
      }
    }
    async function dragFollowStart() {
      try {
        const t = dragSweetSpotT();
        const res = await fetch(dashboardApi('/api/arm/drag_follow'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(Object.assign(
            { enable: true, mode: 'echo' },
            dragFollowSharedStatics(),
            dragFollowEchoFromT(t),
          )),
        });
        const data = await res.json();
        document.getElementById('dragFollowStatus').textContent = JSON.stringify(data, null, 2);
        await refreshDragFollowStatus();
        await dragFollowFetchLog();
        nxStackRefresh();
      } catch (e) {
        document.getElementById('dragFollowStatus').textContent = String(e);
      }
    }
    async function dragFollowStartPassthrough() {
      try {
        const t = dragSweetSpotT();
        const res = await fetch(dashboardApi('/api/arm/drag_follow'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(Object.assign(
            { enable: true, mode: 'passthrough' },
            dragFollowSharedStatics(),
            dragFollowPassthroughFromT(t),
          )),
        });
        const data = await res.json();
        document.getElementById('dragFollowStatus').textContent = JSON.stringify(data, null, 2);
        await refreshDragFollowStatus();
        await dragFollowFetchLog();
        nxStackRefresh();
      } catch (e) {
        document.getElementById('dragFollowStatus').textContent = String(e);
      }
    }
    async function dragFollowStartMirrorLegacy() {
      try {
        const t = dragSweetSpotT();
        const res = await fetch(dashboardApi('/api/arm/drag_follow'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(Object.assign(
            { enable: true, mode: 'mirror' },
            dragFollowSharedStatics(),
            dragFollowMirrorFromT(t),
          )),
        });
        const data = await res.json();
        document.getElementById('dragFollowStatus').textContent = JSON.stringify(data, null, 2);
        await refreshDragFollowStatus();
        await dragFollowFetchLog();
        nxStackRefresh();
      } catch (e) {
        document.getElementById('dragFollowStatus').textContent = String(e);
      }
    }
    async function dragFollowStop() {
      try {
        const res = await fetch(dashboardApi('/api/arm/drag_follow'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enable: false }),
        });
        const data = await res.json();
        document.getElementById('dragFollowStatus').textContent = JSON.stringify(data, null, 2);
        await refreshDragFollowStatus();
      } catch (e) {
        document.getElementById('dragFollowStatus').textContent = String(e);
      }
    }
    async function saveStartPose() {
      try {
        const res = await fetch(dashboardApi('/api/alignment/start_pose'), { method: 'POST' });
        const data = await res.json();
        document.getElementById('startOpsLog').textContent = JSON.stringify(data, null, 2);
      } catch (e) {
        document.getElementById('startOpsLog').textContent = String(e);
      }
    }
    async function loadStartPose() {
      try {
        const res = await fetch(dashboardApi('/api/alignment/start_pose'));
        const data = await res.json();
        document.getElementById('startOpsLog').textContent = JSON.stringify(data, null, 2);
      } catch (e) {
        document.getElementById('startOpsLog').textContent = String(e);
      }
    }
    async function nxStackRefresh() {
      try {
        const res = await fetch(dashboardApi('/api/nx/stack/status'));
        const data = await res.json();
        document.getElementById('nxStackBox').textContent = JSON.stringify(data, null, 2);
        const loc = data.go2_local;
        const el = document.getElementById('nxModeBadge');
        el.textContent = loc ? 'GO2_LOCAL attivo (sensori su questa macchina)' : 'GO2_LOCAL spento — dashboard non sul robot';
        el.className = 'pill ' + (loc ? 'ok' : 'bad');
      } catch (e) {
        document.getElementById('nxStackBox').textContent = String(e);
      }
    }
    async function emergencyHold() {
      try {
        const res = await fetch(dashboardApi('/api/arm/emergency_hold'), { method: 'POST' });
        const data = await res.json();
        document.getElementById('armActionLog').textContent = JSON.stringify(data, null, 2);
        const holdOk = !!(data.hold_ok || (data.hold && data.hold.ok));
        const dragOff = !!(data.drag_follow_stop && data.drag_follow_stop.drag_follow_stopped);
        let line = '';
        if (dragOff) line += 'Drag fermato. ';
        if (holdOk) line += 'FERMA: abort + hold DDS inviati.';
        else {
          const why = (data.hold && data.hold.reason) ? String(data.hold.reason) : (data.reason || 'vedi JSON');
          line += 'Abort sequenza OK ma hold fallito — ' + why.slice(0, 120);
        }
        setText('boxStatus', line || 'FERMA eseguito (dettaglio nel log sotto)');
        const bs = document.getElementById('boxStatus');
        if (bs) bs.className = holdOk ? 'ok' : 'bad';
        nxStackRefresh();
        refreshGraspJobPanel();
        void refreshDragFollowStatus();
      } catch (e) {
        setText('boxStatus', String(e));
        document.getElementById('boxStatus').className = 'bad';
      }
    }
    function setPresaSequenceStatus(htmlOrText, useHtml) {
      const el = document.getElementById('presaSequenceStatus');
      if (!el) return;
      if (useHtml) el.innerHTML = htmlOrText;
      else el.textContent = htmlOrText;
    }
    async function attemptGrasp() {
      const logEl = document.getElementById('armActionLog');
      setPresaSequenceStatus('Invio POST /api/arm/grasp_box/attempt…', false);
      try {
        const fetchOptions = {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        };
        if (window.AbortSignal && typeof AbortSignal.timeout === 'function') {
          fetchOptions.signal = AbortSignal.timeout(60000);
        }
        const res = await fetch(dashboardApi('/api/arm/grasp_box/attempt'), fetchOptions);
        let data;
        const raw = await res.text();
        try {
          data = raw ? JSON.parse(raw) : {};
        } catch (je) {
          const msg = 'Risposta non JSON (HTTP ' + res.status + '): ' + raw.slice(0, 400);
          setPresaSequenceStatus(msg, false);
          if (logEl) logEl.textContent = msg;
          return;
        }
        if (logEl) logEl.textContent = JSON.stringify(data, null, 2);
        if (res.ok && (data.started || data.accepted)) {
          if (data.accepted && !data.started) {
            setPresaSequenceStatus(
              '<span class="ok">● Preflight avviato</span> (HTTP 202) — il server calcola tag/IK in background. '
                + 'Controlla sotto <strong>fase</strong> e <code>/api/arm/job_status</code>; passa a «sequenza» quando il preflight è OK.',
              true
            );
          } else {
            setPresaSequenceStatus(
              '<span class="ok">● Sequenza avviata</span> — resta su questa scheda per la <strong>fase</strong> in tempo reale. '
                + 'Camere/tag: scheda <strong>Camere &amp; AprilTag</strong>.',
              true
            );
          }
          setText('boxStatus', data.accepted && !data.started ? 'preflight (async)' : 'presa avviata (background)');
          const bs = document.getElementById('boxStatus');
          if (bs) bs.className = 'warn';
        } else if (res.status === 403 && (data.reason || '').indexOf('GRASP_EXECUTE') >= 0) {
          setPresaSequenceStatus(
            '<span class="bad">Grasp disabilitato</span> — sul processo Flask serve <code>GO2_GRASP_EXECUTE_ARM=1</code> '
              + '(lo script <code>nx_start_dashboard.sh</code> del deploy lo imposta). Riavvia la dashboard sulla NX o export prima di <code>python3 diagnostics_dashboard.py</code>.',
            true
          );
          setText('boxStatus', 'blocked execute_arm');
          const bs = document.getElementById('boxStatus');
          if (bs) bs.className = 'bad';
        } else if (res.status === 409 && data.reason === 'preflight_tag_or_ik_not_ready') {
          const nar = (data.preflight && data.preflight.narrative_it) ? data.preflight.narrative_it.slice(0, 4).join(' · ') : '';
          setPresaSequenceStatus(
            '<span class="warn">Preflight non OK</span> — servono tag scatola 0–3 visibili e piano IK pronto. '
              + (nar ? ('Dettaglio: ' + nar.slice(0, 220) + '… ') : '')
              + 'Apri scheda <strong>Camere &amp; AprilTag</strong> e «Pipeline presa».',
            true
          );
          setText('boxStatus', 'preflight blocked');
          const bs = document.getElementById('boxStatus');
          if (bs) bs.className = 'warn';
        } else if (res.status === 409 && (data.reason === 'grasp_preflight_already_in_flight')) {
          setPresaSequenceStatus(
            '<span class="warn">Preflight già avviato</span> — attendi qualche secondo (vedi fase sotto) o premi «FERMA — hold» per annullare.',
            true
          );
          setText('boxStatus', 'preflight già in volo');
          const bs = document.getElementById('boxStatus');
          if (bs) bs.className = 'warn';
        } else if (res.status === 409) {
          setPresaSequenceStatus(
            '<span class="warn">Sequenza già in corso</span> — attendi il completamento o premi «FERMA — hold».',
            true
          );
          setText('boxStatus', 'presa già in corso');
          const bs = document.getElementById('boxStatus');
          if (bs) bs.className = 'warn';
        } else {
          const why = data.reason || data.message || JSON.stringify(data);
          setPresaSequenceStatus('Rifiutato o errore server: ' + String(why).slice(0, 280), false);
          setText('boxStatus', data.attempted_motion ? 'motion sent' : 'blocked');
          const bs = document.getElementById('boxStatus');
          if (bs) bs.className = data.attempted_motion ? 'ok' : 'warn';
        }
        nxStackRefresh();
        refreshGraspJobPanel();
      } catch (e) {
        const msg =
          e && e.name === 'TimeoutError'
            ? 'Timeout 60s sulla richiesta POST — server/processo Flask bloccato? Controlla journal sulla NX.'
            : ('Rete o eccezione: ' + String(e));
        setPresaSequenceStatus(msg, false);
        if (logEl) logEl.textContent = msg;
      }
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
        const res = await fetch(dashboardApi('/api/lidar/frame'));
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
    initOpTabs();
    jointSlidersInitDisplay();
    wireApriltagOverlayImgHandlers();
    (function initAlwaysCamStrip() {
      try {
        const expanded = localStorage.getItem('go2_always_cam_expanded') === '1';
        const strip = document.getElementById('alwaysCamStrip');
        if (strip && expanded) strip.classList.add('expanded');
        const q = '?nc=' + Date.now();
        const m0 = document.getElementById('alwaysCam0');
        const m6 = document.getElementById('alwaysCam6');
        if (m0) m0.src = '/stream/robot/camera/0.mjpg' + q;
        if (m6) m6.src = '/stream/robot/camera/6.mjpg' + q;
        const tagPairs = [
          ['alwaysBox0', '0'],
          ['alwaysBox6', '6'],
        ];
        tagPairs.forEach(([id, dev]) => {
          const te = document.getElementById(id);
          if (te) te.src = '/stream/robot/camera/' + dev + '/tags.mjpg' + q;
        });
      } catch (err) {}
    })();
    refreshGraspJobPanel();
    loadStatus();
    refreshServerBoot();
    nxStackRefresh();
    (function initDragSweetSpot() {
      const el = document.getElementById('dragSweetSpot');
      if (!el) return;
      try {
        const s = localStorage.getItem('go2_drag_sweet_spot_v2');
        if (s !== null && s !== '') {
          const n = parseInt(s, 10);
          if (!Number.isNaN(n)) el.value = String(Math.max(0, Math.min(100, n)));
        }
      } catch (err) {}
      el.addEventListener('input', dragSweetSpotUpdateLabel);
      el.addEventListener('change', dragSweetSpotUpdateLabel);
      dragSweetSpotUpdateLabel();
    })();
    refreshDragFollowStatus();
    fetch(dashboardApi('/api/cameras/warmup'), { method: 'POST' }).catch(() => {});
    mirrorApriltagTabCanvasesFromStrip();
    refreshCameraStatus();
    refreshLidar();
    refreshBoxPlan();
    refreshArmMotionDiag();
    loadUiTuning();
    loadGraspSessionIntoUi();
    refreshGraspPipeline();
    UI_TUNING_KEYS.forEach((k) => {
      const inp = document.getElementById('tune_' + k);
      if (!inp) return;
      inp.addEventListener('input', scheduleUiTuningPost);
      inp.addEventListener('change', postUiTuningFromSliders);
    });
    setInterval(loadStatus, 5000);
    setInterval(refreshServerBoot, 12000);
    setInterval(refreshDragFollowStatus, 900);
    setInterval(refreshCameraStatus, 1200);
    setInterval(refreshLidar, 900);
    setInterval(refreshBoxPlan, 1600);
    setInterval(refreshGraspJobPanel, 900);
    setInterval(refreshArmMotionDiag, 2200);
    setInterval(refreshGraspPipeline, 3000);
  </script>
</body>
</html>
"""


@APP.route("/favicon.ico")
def favicon() -> Response:
    """Browser requests this by default; evita 404 in console (nessuna icona dedicata)."""
    return Response(status=204)


@APP.route("/")
def index() -> Response:
    url_prefix = os.environ.get("GO2_DASHBOARD_URL_PREFIX", "").strip().rstrip("/")
    script_root = url_prefix or ((request.script_root or "").rstrip("/"))
    html = render_template_string(
        HTML,
        go2_host=GO2_HOST,
        xt16_host=XT16_HOST,
        servo_arm_host=SERVO_ARM_HOST,
        dashboard_port=int(os.environ.get("GO2_DASHBOARD_PORT", "5050")),
        dashboard_bind=GO2_DASHBOARD_BIND,
        go2_local="1" if GO2_LOCAL else "0",
        script_root=script_root,
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

    period = float(os.environ.get("GO2_MJPEG_FRAME_PERIOD_S", "0.05"))
    if not GO2_LOCAL:
        period = max(period, 0.12)

    def generate():
        last: bytes | None = None
        first_wait_s = float(os.environ.get("GO2_MJPEG_FIRST_FRAME_WAIT_S", "1.8"))
        while True:
            if GO2_LOCAL and cv2 is not None:
                jpg = CAMERA_CACHE.peek_jpeg(device)
                if jpg is None and last is None:
                    jpg = CAMERA_CACHE.get_jpeg(device, wait_s=first_wait_s)
            else:
                jpg = robot_camera_jpeg(device)
            if jpg is None:
                jpg = last
            if jpg is not None:
                last = jpg
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + jpg + b"\r\n"
                )
            time.sleep(period)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@APP.route("/stream/robot/camera/<int:device>/tags.mjpg")
def stream_robot_camera_tagsmjpeg(device: int) -> Response:
    """MJPEG con overlay AprilTag — stesso ritmo dello stream raw (non polling HTTP su .jpg)."""
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)

    base = float(os.environ.get("GO2_MJPEG_FRAME_PERIOD_S", "0.05"))
    period = float(os.environ.get("GO2_APRILTAG_MJPEG_PERIOD_S", str(base)))
    period = max(0.03, period)
    if not GO2_LOCAL:
        period = max(period, 0.12)

    def generate():
        last: bytes | None = None
        first_wait_s = float(os.environ.get("GO2_MJPEG_FIRST_FRAME_WAIT_S", "1.8"))
        while True:
            image = _apriltag_overlay_jpeg_bytes(device)
            if image is None and last is None:
                # Non lasciare il browser con riquadro nero: mostra raw finché l'overlay tag non è pronto.
                image = robot_camera_jpeg(device) if not GO2_LOCAL else CAMERA_CACHE.get_jpeg(device, wait_s=first_wait_s)
            if image is None:
                image = last
            if image is not None:
                last = image
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + image + b"\r\n"
                )
            time.sleep(period)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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
        from box_object_detector import detect_box_object, detector_status

        if GO2_LOCAL:
            CAMERA_CACHE.start(0)
            CAMERA_CACHE.start(6)

        opencv_aruco = bool(cv2 is not None and hasattr(cv2, "aruco"))
        candidates = {}
        per_cam_pipeline: dict[str, Any] = {}
        det_status = detector_status()
        for device in (0, 6):
            cache_row = (CAMERA_CACHE.stats().get(str(device)) if GO2_LOCAL else None)
            frame = frame_from_camera(device)
            per_cam_pipeline[str(device)] = {
                "dev_path": f"/dev/video{device}",
                "frame_ok": frame is not None,
                "frame_shape_hw": (list(frame.shape[:2]) if frame is not None else None),
                "camera_cache": cache_row,
            }
            if frame is None:
                candidates[str(device)] = {
                    "ok": False,
                    "error": f"camera /dev/video{device} unavailable",
                    "camera_label": CAMERA_DEVICES.get(device, "unknown"),
                }
                continue
            object_det = detect_box_object(frame)
            prefer_tag = _effective_grasp_bool("prefer_tag_grip", "GO2_GRASP_PREFER_TAG_GRIP")
            result = plan_from_frame(frame, object_detection=object_det, prefer_tag_grip=prefer_tag)
            result["camera_device"] = device
            result["camera_label"] = CAMERA_DEVICES.get(device, "unknown")
            wrist_abs_trust = _effective_grasp_bool("trust_wrist_absolute_ik", "GO2_TRUST_WRIST_ABSOLUTE_IK")
            result["absolute_ik_safe"] = bool(device != 0 or wrist_abs_trust)
            result["absolute_ik_note"] = (
                "front/fixed camera heuristic allowed"
                if result["absolute_ik_safe"]
                else "wrist camera target is wrist-relative: use visual servo, not absolute base IK"
            )
            candidates[str(device)] = result

        def score(item: dict[str, Any], device_key: str) -> tuple[int, int, int, int, float, float]:
            tags = item.get("tags", {}).get("tags", [])
            poses = item.get("poses", {}).get("poses", [])
            nearest = min([p.get("range_m", 999.0) for p in poses], default=999.0)
            grip = item.get("grip_point") or {}
            obj = item.get("object_detection") or {}
            safe_ik = bool(item.get("absolute_ik_safe", True))
            # Safe IK wins. Wrist grip still appears in UI but does not win absolute IK by default.
            return (
                3 if (item.get("ok") and safe_ik) else (2 if item.get("ok") else (1 if grip.get("ok") else 0)),
                1 if grip.get("ok") else 0,
                1 if str(device_key) == "0" and grip.get("ok") and not item.get("ok") else 0,
                len(tags),
                float(obj.get("confidence") or 0.0),
                -nearest,
            )

        selected_key = None
        if candidates:
            selected_key = max(candidates, key=lambda k: score(candidates[k], k))
        selected = candidates.get(selected_key) if selected_key is not None else None
        ok = bool(selected and selected.get("ok"))
        tag_cal = (selected or {}).get("tag_calibration") if selected else None
        visible_summary: dict[str, Any] = {}
        any_tag = False
        any_box_tag = False
        any_object = False
        any_grip = False
        for key, cand in candidates.items():
            tags = ((cand or {}).get("tags") or {}).get("tags") or []
            ids = [int(t.get("id", -1)) for t in tags]
            box_ids = [i for i in ids if i in BOX_TAG_IDS_IK]
            obj = (cand or {}).get("object_detection") or {}
            grip = (cand or {}).get("grip_point") or {}
            any_tag = any_tag or bool(ids)
            any_box_tag = any_box_tag or bool(box_ids)
            any_object = any_object or bool(obj.get("ok"))
            any_grip = any_grip or bool(grip.get("ok"))
            visible_summary[key] = {
                "tag_visible": bool(ids),
                "box_tag_visible": bool(box_ids),
                "object_visible": bool(obj.get("ok")),
                "object_backend": obj.get("backend"),
                "object_confidence": obj.get("confidence"),
                "grip_point_ok": bool(grip.get("ok")),
                "grip_center_px": grip.get("grip_center_px"),
                "box_area_px": grip.get("box_area_px"),
                "approach_error_px": grip.get("approach_error_px"),
                "ids": ids,
                "box_ids": box_ids,
                "ik_ok": bool(((cand or {}).get("preview") or {}).get("ok")),
            }
        selected_grip = (selected or {}).get("grip_point") if selected else None
        return jsonify({
            "ok": ok,
            "mode": "dual-camera-fusion",
            "selected_camera": None if selected is None else int(selected_key),
            "selected": selected,
            "candidates": candidates,
            "tag_visible_any": any_tag,
            "box_tag_visible_any": any_box_tag,
            "object_visible_any": any_object,
            "grip_point_visible_any": any_grip,
            "selected_grip_point": selected_grip,
            "visible_summary": visible_summary,
            "tag_calibration": tag_cal,
            "object_detector": det_status,
            "april_tag_pipeline": {
                "go2_local": GO2_LOCAL,
                "opencv_aruco_module": opencv_aruco,
                "hint": (
                    "Il planner usa gli stessi frame del CameraCache (non lo stream MJPEG). "
                    "frame_ok=false → V4L2/cache; tag 0–3 danno metrica; detector box fornisce fallback senza tag."
                ),
                "per_camera": per_cam_pipeline,
            },
            "real_motion_enabled": os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"},
            "command_stack": command_stack_status(),
            "note": (
                "tag25h9: box IDs 0..3 use edge length 19 mm default (BOX_TAG_SIZE_M); "
                "ID 5 landmark above XT16 uses 61 mm (REFERENCE_TAG_SIZE_M). "
                "Pose/range per tag uses tag_edge_length_m. IK preview averages box tags only. "
                "Each tag includes diagonal_px / mean_edge_px (larger ≈ closer). "
                "Perpendicular grasp normal to tag plane is not implemented yet — preview uses fixed base offsets."
            ),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": repr(exc), "mode": "dual-camera-fusion"})


@APP.route("/api/box/annotated.jpg")
@APP.route("/api/box/annotated/<int:device>.jpg")
def api_box_annotated(device: int = 6) -> Response:
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)
    raw = _apriltag_overlay_jpeg_bytes(device)
    if raw is None:
        return Response("camera frame unavailable", status=503)
    return Response(raw, mimetype="image/jpeg", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@APP.route("/api/arm/diagnose_motion", methods=["GET"])
def api_arm_diagnose_motion() -> Any:
    return jsonify(arm_diagnose_motion())


@APP.route("/api/arm/grasp_pipeline", methods=["GET"])
def api_arm_grasp_pipeline() -> Any:
    """Debug flusso visione → IK → motion: leggibile dalla UI senza avviare il grasp."""
    return jsonify(grasp_pipeline_status())


_ALLOWED_UI_TUNING: dict[str, tuple[float, float]] = {
    "tag_wait_s": (5.0, 300.0),
    "visible_plan_wait_s": (0.5, 120.0),
    "search_max_cycles": (1.0, 40.0),
    "search_delay_ms": (80.0, 2500.0),
    "plan_delay_ms": (80.0, 3200.0),
}


def _ui_tuning_defaults_dict() -> dict[str, float]:
    return {
        "tag_wait_s": float(os.environ.get("GO2_GRASP_TAG_WAIT_S", "90")),
        "visible_plan_wait_s": float(os.environ.get("GO2_GRASP_VISIBLE_PLAN_WAIT_S", "15")),
        "search_max_cycles": float(int(os.environ.get("D1_SEARCH_MAX_CYCLES", str(D1_SEARCH_MAX_CYCLES)))),
        "search_delay_ms": float(D1_SEARCH_COMMAND_DELAY_MS),
        "plan_delay_ms": float(D1_PLAN_COMMAND_DELAY_MS),
    }


@APP.route("/api/arm/ui_tuning", methods=["GET", "POST"])
def api_arm_ui_tuning() -> Any:
    defaults = _ui_tuning_defaults_dict()
    if request.method == "GET":
        with ARM_UI_TUNING_LOCK:
            over = dict(ARM_UI_TUNING)
        return jsonify({"ok": True, "effective": {**defaults, **over}, "overrides": over, "defaults": defaults})
    body = request.get_json(silent=True) or {}
    if body.get("reset"):
        with ARM_UI_TUNING_LOCK:
            ARM_UI_TUNING.clear()
        return jsonify({"ok": True, "cleared": True, "effective": _ui_tuning_defaults_dict(), "defaults": defaults})
    changed: dict[str, float] = {}
    errs: list[str] = []
    with ARM_UI_TUNING_LOCK:
        for key, (lo, hi) in _ALLOWED_UI_TUNING.items():
            if key not in body:
                continue
            try:
                raw = float(body[key])
            except (TypeError, ValueError):
                errs.append(f"{key}: valore non numerico")
                continue
            if key == "search_max_cycles":
                raw = float(int(round(raw)))
            v = max(lo, min(hi, raw))
            ARM_UI_TUNING[key] = v
            changed[key] = v
        over = dict(ARM_UI_TUNING)
    return jsonify(
        {
            "ok": not errs,
            "errors": errs,
            "changed": changed,
            "effective": {**defaults, **over},
            "defaults": defaults,
        }
    )


@APP.route("/api/arm/emergency_hold", methods=["POST"])
def api_arm_emergency_hold() -> Any:
    """
    Segnala abort al loop grasp, ferma drag-follow (altrimenti il subprocess
    continua a comandare il braccio), invia hold DDS, poi riporta il job a ``idle``.
    """
    drag_stop = _drag_follow_stop_if_running(hold_after_stop=False)
    ARM_GRASP_ABORT.set()
    kill_motion = _kill_d1_motion_helpers()
    time.sleep(float(os.environ.get("GO2_EMERGENCY_PREHOLD_SETTLE_S", "0.05")))
    hold = publish_d1_hold_current()
    time.sleep(float(os.environ.get("GO2_GRASP_ABORT_DRAIN_S", "0.15")))
    # Non chiamare ARM_GRASP_ABORT.clear() qui: il flag resta attivo finché il worker grasp
    # non esce (oppure un nuovo tentativo lo azzera nel preflight). Altrimenti il loop
    # non vede mai l'abort durante sleep/subprocess lunghi.
    hold_ok = bool(hold.get("ok"))
    phase = (
        ("Drag fermato. " if drag_stop.get("drag_follow_stopped") else "")
        + (
            "Hold inviato al braccio. Puoi premere di nuovo «Avvia sequenza presa»."
            if hold_ok
            else f"Hold non riuscito: {hold.get('reason', 'unknown')}. Controlla GO2_ENABLE_REAL_ARM e feedback DDS."
        )
    )
    _arm_job_update(
        "idle",
        {
            "last_action": "emergency_hold",
            "hold": hold,
            "kill_motion": kill_motion,
            "drag_follow_stop": drag_stop,
            "phase_label_it": phase,
        },
    )
    return jsonify(
        {
            "ok": hold_ok,
            "abort_signaled": True,
            "hold_ok": hold_ok,
            "hold": hold,
            "kill_motion": kill_motion,
            "drag_follow_stop": drag_stop,
        }
    )


@APP.route("/api/arm/job_status", methods=["GET"])
def api_arm_job_status() -> Any:
    with ARM_OPERATION_LOCK:
        detail = dict(LAST_ARM_JOB.get("detail") or {})
        payload = {
            "status": LAST_ARM_JOB.get("status"),
            "updated_at": LAST_ARM_JOB.get("updated_at"),
            "detail": detail,
            "phase_label_it": detail.get("phase_label_it"),
            "events": list(ARM_GRASP_EVENTS[-ARM_GRASP_EVENTS_MAX:]),
            "environment": {
                "GO2_LOCAL": GO2_LOCAL,
                "GO2_ENABLE_REAL_ARM": os.environ.get("GO2_ENABLE_REAL_ARM", "0"),
                "GO2_GRASP_EXECUTE_ARM": "1" if _grasp_execute_enabled() else "0",
                "GO2_GRASP_USE_FUSED_PLAN_IK": "1"
                if _effective_grasp_bool("use_fused_plan_ik", "GO2_GRASP_USE_FUSED_PLAN_IK")
                else "0",
                "GO2_FRONT_CAMERA_FALLBACK_GRASP": "1"
                if _effective_grasp_bool("front_camera_fallback_grasp", "GO2_FRONT_CAMERA_FALLBACK_GRASP")
                else "0",
                "GO2_TRUST_WRIST_ABSOLUTE_IK": "1"
                if _effective_grasp_bool("trust_wrist_absolute_ik", "GO2_TRUST_WRIST_ABSOLUTE_IK")
                else "0",
                "GO2_GRASP_PREFER_TAG_GRIP": "1"
                if _effective_grasp_bool("prefer_tag_grip", "GO2_GRASP_PREFER_TAG_GRIP")
                else "0",
            },
        }
    return jsonify({"ok": True, **payload})


@APP.route("/api/arm/grasp_session", methods=["GET", "POST"])
def api_arm_grasp_session() -> Any:
    """Flag grasp effettivi: sessione processo (dashboard) > variabili d'ambiente."""
    if request.method == "GET":
        with ARM_GRASP_SESSION_LOCK:
            ov = dict(ARM_GRASP_SESSION)
        return jsonify({"ok": True, "overrides": ov, "effective": grasp_session_effective_flags()})
    body = request.get_json(silent=True) or {}
    if body.get("reset"):
        with ARM_GRASP_SESSION_LOCK:
            ARM_GRASP_SESSION.clear()
        return jsonify({"ok": True, "cleared": True, "effective": grasp_session_effective_flags(), "overrides": {}})
    allowed = {
        "trust_wrist_absolute_ik",
        "use_fused_plan_ik",
        "fused_with_center",
        "front_camera_fallback_grasp",
        "prefer_tag_grip",
        "grasp_execute_arm",
    }
    errs: list[str] = []
    changed: dict[str, Any] = {}
    with ARM_GRASP_SESSION_LOCK:
        for k in allowed:
            if k not in body:
                continue
            raw = body[k]
            if raw is None:
                ARM_GRASP_SESSION.pop(k, None)
                changed[k] = None
                continue
            if isinstance(raw, bool):
                ARM_GRASP_SESSION[k] = raw
            elif isinstance(raw, (int, float)):
                ARM_GRASP_SESSION[k] = bool(int(raw))
            elif isinstance(raw, str):
                ARM_GRASP_SESSION[k] = raw.strip().lower() in {"1", "true", "yes", "on"}
            else:
                errs.append(f"{k}: tipo non supportato")
                continue
            changed[k] = ARM_GRASP_SESSION[k]
        ov = dict(ARM_GRASP_SESSION)
    return jsonify(
        {
            "ok": not errs,
            "errors": errs,
            "changed": changed,
            "overrides": ov,
            "effective": grasp_session_effective_flags(),
        }
    )


@APP.route("/api/arm/grasp_box/attempt_after_crouch", methods=["POST"])
def api_arm_grasp_box_attempt_after_crouch() -> Any:
    """Sport StandDown poi stessa sequenza di ``/api/arm/grasp_box/attempt`` (preflight asincrono)."""
    body = request.get_json(silent=True) or {}
    settle_s = float(body.get("settle_s", 1.2))
    drain_s = float(body.get("drain_s", os.environ.get("GO2_GRASP_RESTART_DRAIN_S", "0.35")))
    with ARM_OPERATION_LOCK:
        st = LAST_ARM_JOB.get("status")
        if st == "running":
            return (
                jsonify({"ok": False, "started": False, "reason": "arm_job_already_running"}),
                409,
            )
        if st == "starting":
            return (
                jsonify({"ok": False, "started": False, "reason": "grasp_preflight_already_in_flight"}),
                409,
            )
    if not _grasp_execute_enabled():
        _arm_event("blocked", "Avvio grasp bloccato: GO2_GRASP_EXECUTE_ARM=0 (modalità sicura)")
        return (
            jsonify(
                {
                    "ok": False,
                    "started": False,
                    "reason": "GO2_GRASP_EXECUTE_ARM=0",
                    "message": "Movimento fisico disabilitato.",
                }
            ),
            403,
        )
    with ARM_OPERATION_LOCK:
        _arm_job_update(
            "starting",
            {"phase_label_it": "Accucciata Go2, poi preflight grasp…"},
        )
    threading.Thread(
        target=_grasp_crouch_then_preflight_worker,
        args=(drain_s, settle_s),
        daemon=True,
        name="grasp-crouch-preflight",
    ).start()
    return (
        jsonify(
            {
                "ok": True,
                "started": False,
                "accepted": True,
                "async_preflight": True,
                "message": "Crouch + preflight grasp in background.",
                "job_status_url": "/api/arm/job_status",
            }
        ),
        202,
    )


@APP.route("/api/arm/grasp_box/attempt", methods=["POST"])
def api_arm_grasp_box_attempt() -> Any:
    drain_s = float(os.environ.get("GO2_GRASP_RESTART_DRAIN_S", "0.35"))
    with ARM_OPERATION_LOCK:
        st = LAST_ARM_JOB.get("status")
        if st == "running":
            return (
                jsonify({"ok": False, "started": False, "reason": "arm_job_already_running"}),
                409,
            )
        if st == "starting":
            return (
                jsonify({"ok": False, "started": False, "reason": "grasp_preflight_already_in_flight"}),
                409,
            )
    if not _grasp_execute_enabled():
        _arm_event("blocked", "Avvio grasp bloccato: GO2_GRASP_EXECUTE_ARM=0 (modalità sicura)")
        return (
            jsonify(
                {
                    "ok": False,
                    "started": False,
                    "reason": "GO2_GRASP_EXECUTE_ARM=0",
                    "message": "Movimento fisico disabilitato: usa la UI per verificare TAG/IK e riabilita solo dopo aver corretto posa START.",
                }
            ),
            403,
        )
    with ARM_OPERATION_LOCK:
        _arm_job_update(
            "starting",
            {"phase_label_it": "Preflight camere/IK (asincrono)…"},
        )
    threading.Thread(
        target=_grasp_preflight_and_start,
        args=(drain_s,),
        daemon=True,
        name="grasp-preflight",
    ).start()
    return (
        jsonify(
            {
                "ok": True,
                "started": False,
                "accepted": True,
                "async_preflight": True,
                "message": "Preflight avviato in background; stato in /api/arm/job_status.",
                "job_status_url": "/api/arm/job_status",
            }
        ),
        202,
    )


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
        "object_detector": object_detector_stack_status,
        "camera_status": lambda: {"ok": True, "mode": "local-cache" if GO2_LOCAL else "ssh-snapshot", "cameras": CAMERA_CACHE.stats()},
        "box_plan": lambda: json.loads(api_box_plan().get_data(as_text=True)),
        "dds": dds_lowstate_probe,
    }
    if name not in tests:
        return jsonify({"ok": False, "error": f"Unknown test {name!r}"}), 404
    return jsonify(tests[name]())


if __name__ == "__main__":
    port = int(os.environ.get("GO2_DASHBOARD_PORT", "5050"))
    host = GO2_DASHBOARD_BIND
    print(f"Starting Go2 diagnostics dashboard on http://{host}:{port} (GO2_LOCAL={GO2_LOCAL})")
    print(
        f"GO2_ENABLE_REAL_ARM={os.environ.get('GO2_ENABLE_REAL_ARM', '0')} | "
        f"GO2_ENABLE_BASE_MOTION={os.environ.get('GO2_ENABLE_BASE_MOTION', '0')} "
        "(base_motion must be 1 on NX for Sport Stand up / Crouch)."
    )
    print("Arm motion requires GO2_ENABLE_REAL_ARM=1 and bin/d1_arm_* on this host.")
    warmup_realtime_feeds()
    set_status({
        "updated_at": now_iso(),
        "running": True,
        "summary": "Dashboard online; warming cameras and running diagnostics in background...",
        "tests": {},
    })
    threading.Thread(target=background_run, daemon=True).start()
    APP.run(host=host, port=port, debug=False, threaded=True)
