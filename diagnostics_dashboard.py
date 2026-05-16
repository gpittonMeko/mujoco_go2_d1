#!/usr/bin/env python3
"""Flask handlers + ``APP``. Run ``scripts/serve_dashboard_modular.py`` — not this file as __main__."""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import platform
import queue
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, abort, jsonify, render_template_string, request, send_from_directory
from jinja2 import TemplateNotFound

try:
    import cv2
except Exception:  # pragma: no cover - optional runtime dependency
    cv2 = None

try:
    import paramiko
except Exception:  # pragma: no cover - optional runtime dependency
    paramiko = None


PROJECT_ROOT = Path(__file__).resolve().parent
TAG5_CALIB_PATH = PROJECT_ROOT / "data" / "tag5_calibration_arm_base.json"
GO2_SCENE_ASSETS_DIR = PROJECT_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "assets"
D1_SCENE_MESH_DIR = (
    PROJECT_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "d1_550_description" / "meshes"
)
# Scena MuJoCo inclusa (Go2+D1 mesh + tavolo/palla) per anteprima PNG server-side.
MUJOCO_SCENE_PREVIEW_XML = PROJECT_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "scene_d1_mesh.xml"

from go2_dashboard.cameras import (
    CAMERA_CACHE,
    CAMERA_DEVICES,
    _cv_videocapture,
    _v4l_index_for_logical_camera,
    usb_auto_v4l_mapping,
)
from go2_dashboard.grasp_assessment import (
    candidate_grasp_assessment,
    detector_training_scope,
    plan_grasp_assessment,
)


def _scene_mesh_manifest() -> dict[str, list[str]]:
    """Elenco file mesh serviti da ``/api/arm/scene_meshes`` (solo nomi presenti su disco)."""
    go2: list[str] = []
    d1: list[str] = []
    if GO2_SCENE_ASSETS_DIR.is_dir():
        go2 = sorted(p.name for p in GO2_SCENE_ASSETS_DIR.glob("*.obj"))
    if D1_SCENE_MESH_DIR.is_dir():
        d1 = sorted(p.name for p in D1_SCENE_MESH_DIR.glob("*.STL"))
    return {"go2_obj": go2, "d1_stl": d1}


def _d1_stl_disk_summary() -> dict[str, Any]:
    """
    Metadati STL sul disco. I file ``Empty_Link*.STL`` **minimali** del repo (spesso ~684 byte,
    12 triangoli = un box) non sono la geometria CAD fina del D1 — da qui i «blocchi» in Three.js.
    """
    out: dict[str, Any] = {
        "meshes_dir": str(D1_SCENE_MESH_DIR),
        "files_byte_size": {},
        "looks_like_placeholder": False,
    }
    if not D1_SCENE_MESH_DIR.is_dir():
        return out
    sizes: list[int] = []
    for p in sorted(D1_SCENE_MESH_DIR.glob("*.STL")):
        try:
            sz = int(p.stat().st_size)
            out["files_byte_size"][p.name] = sz
            if sz > 0:
                sizes.append(sz)
        except OSError:
            out["files_byte_size"][p.name] = -1
    # Mesh CAD veri sono tipicamente ≫ 4 KiB; placeholder venduti nel repo sono tutti ~684 B.
    if sizes and max(sizes) < 4096:
        out["looks_like_placeholder"] = True
    return out


def _rpy_to_quat_xyzw_ros(roll: float, pitch: float, yaw: float) -> list[float]:
    """Quaternion (x,y,z,w) da RPY in radianti (convenzione URDF/ROS)."""
    cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
    qw = cr * cp * cy + sr * sp * sy
    qx = sr * cp * cy - cr * sp * sy
    qy = cr * sp * cy + sr * cp * sy
    qz = cr * cp * sy - sr * sp * cy
    return [qx, qy, qz, qw]


def _d1_urdf_visual_offsets_list() -> list[dict[str, Any]]:
    """
    Offset visual mesh nel frame di ogni link D1 (base + Empty_Link1..6), da URDF se presente.
    Senza file: identità — utente può impostare ``GO2_D1_URDF_PATH`` o vendere
    ``d1_550_description/urdf/d1_550_description.urdf`` sotto la root repo.
    """
    ident = {"pos_m": [0.0, 0.0, 0.0], "quat_xyzw": [0.0, 0.0, 0.0, 1.0]}
    out: list[dict[str, Any]] = [dict(ident) for _ in range(7)]
    cands: list[Path] = []
    envp = os.environ.get("GO2_D1_URDF_PATH", "").strip()
    if envp:
        cands.append(Path(envp))
    cands.extend(
        [
            PROJECT_ROOT / "d1_550_description" / "urdf" / "d1_550_description.urdf",
            PROJECT_ROOT
            / "unitree_mujoco"
            / "unitree_robots"
            / "go2_d1"
            / "d1_550_description"
            / "urdf"
            / "d1_550_description.urdf",
        ]
    )
    path = next((p for p in cands if str(p) and p.is_file()), None)
    if path is None:
        return out
    try:
        import xml.etree.ElementTree as ET

        tree = ET.parse(str(path))
        root = tree.getroot()
        for link in root.findall("link"):
            vis = link.find("visual")
            if vis is None:
                continue
            geom_el = vis.find("geometry")
            mesh_el = geom_el.find("mesh") if geom_el is not None else None
            fn = ""
            if mesh_el is not None:
                fn = (mesh_el.get("filename") or mesh_el.get("uri") or "").replace("\\", "/")
            fnl = fn.lower()
            idx: int | None = None
            if "base_link" in fnl:
                idx = 0
            else:
                for li in range(1, 7):
                    if f"empty_link{li}" in fnl or f"link{li}.stl" in fnl:
                        idx = li
                        break
            if idx is None:
                continue
            orig = vis.find("origin")
            xyz = [0.0, 0.0, 0.0]
            rpy = [0.0, 0.0, 0.0]
            if orig is not None:
                xs = orig.get("xyz", "0 0 0").split()
                rs = orig.get("rpy", "0 0 0").split()
                if len(xs) >= 3:
                    xyz = [float(xs[0]), float(xs[1]), float(xs[2])]
                if len(rs) >= 3:
                    rpy = [float(rs[0]), float(rs[1]), float(rs[2])]
            q = _rpy_to_quat_xyzw_ros(rpy[0], rpy[1], rpy[2])
            out[idx] = {
                "pos_m": [round(float(xyz[i]), 6) for i in range(3)],
                "quat_xyzw": [round(float(q[i]), 6) for i in range(4)],
            }
    except Exception:
        return [dict(ident) for _ in range(7)]
    return out


def _nominal_tag5_arm_base_from_env() -> list[float] | None:
    """Centro AprilTag 5 (landmark XT-16) nel frame base braccio: ``GO2_TAG5_NOMINAL_ARM_BASE_M=x,y,z`` (m)."""
    raw = os.environ.get("GO2_TAG5_NOMINAL_ARM_BASE_M", "").strip()
    if not raw:
        # Fallback fisico coerente con la scena di laboratorio: riferimento assoluto
        # nel frame base del servo, con tag 5 a X=19 cm, Y=0, Z=8 cm.
        return [0.19, 0.0, 0.08]
    try:
        parts = [float(x.strip()) for x in raw.split(",")]
        if len(parts) >= 3:
            return [parts[0], parts[1], parts[2]]
    except ValueError:
        pass
    # Se l'env è malformata, non blocchiamo la calibrazione: usa il fallback fisico.
    return [0.19, 0.0, 0.08]


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
_LOG_VIS = logging.getLogger(__name__)


@APP.after_request
def _dashboard_api_cors_headers(response: Response) -> Response:
    """Header CORS leggeri su /api/* — utili dietro proxy e per preflight OPTIONS (evita ``Failed to fetch``)."""
    if request.path.startswith("/api/"):
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        response.headers.setdefault(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Requested-With",
        )
        response.headers.setdefault("Access-Control-Max-Age", "3600")
    return response


@APP.route("/api", defaults={"subpath": ""}, methods=["OPTIONS"])
@APP.route("/api/<path:subpath>", methods=["OPTIONS"])
def _api_options_preflight(subpath: str = "") -> Response:
    """Risposta vuota a OPTIONS — alcuni browser inviano preflight su POST JSON."""
    return Response(status=204)


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


STATUS: dict[str, Any] = {
    "updated_at": None,
    "running": False,
    "summary": "No diagnostics run yet.",
    "tests": {},
}
STATUS_LOCK = threading.Lock()
# Process optional avviato da POST /api/arm/drag_follow (script sperimentale).
DRAG_FOLLOW_PROC: Optional[subprocess.Popen] = None
# Meta quando drag_follow è avviato da questa istanza Flask (PID, durata, parametri).
DRAG_FOLLOW_META: Optional[dict[str, Any]] = None
# Ultimo stop / uscita processo (feedback UI).
DRAG_FOLLOW_LAST_END: Optional[dict[str, Any]] = None
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


@APP.route("/api/arm/scene_meshes/<kind>/<path:filename>", methods=["GET"])
def api_arm_scene_meshes(kind: str, filename: str) -> Any:
    """Serve file .obj (Go2) o .STL (D1) per il viewer 3D; path traversal bloccato."""
    from werkzeug.utils import secure_filename

    base_dir: Path
    if kind == "go2":
        if not filename.lower().endswith(".obj"):
            abort(404)
        base_dir = GO2_SCENE_ASSETS_DIR
    elif kind == "d1":
        if not filename.lower().endswith(".stl"):
            abort(404)
        base_dir = D1_SCENE_MESH_DIR
    else:
        abort(404)
    safe = secure_filename(filename)
    if not safe or safe != filename:
        abort(404)
    path = base_dir / safe
    if not path.is_file():
        abort(404)
    mimetype = "model/stl" if kind == "d1" else "text/plain"
    return send_from_directory(str(base_dir), safe, mimetype=mimetype)


@APP.route("/api/mujoco/preview.png", methods=["GET"])
def api_mujoco_preview_png() -> Any:
    """
    Un singolo frame RGB del modello MuJoCo ``scene_d1_mesh.xml`` (keyframe ``home``).
    Richiede il pacchetto Python ``mujoco`` sul server; non incorpora il viewer GLFW nel browser.
    Disabilita con ``GO2_MUJOCO_PREVIEW=0``. Query: ``w``, ``h`` (64–960).
    """
    import io

    if os.environ.get("GO2_MUJOCO_PREVIEW", "1").strip().lower() in {"0", "false", "no", "off"}:
        return Response("GO2_MUJOCO_PREVIEW disabled", status=503, mimetype="text/plain")
    if not MUJOCO_SCENE_PREVIEW_XML.is_file():
        return Response(
            "scene XML missing on server — deploy unitree_mujoco/.../scene_d1_mesh.xml + go2_d1_d1mesh.xml (see deploy_dashboard_to_nx)",
            status=503,
            mimetype="text/plain",
        )
    try:
        import mujoco  # type: ignore
    except ImportError:
        return Response("mujoco Python package not installed", status=503, mimetype="text/plain")

    try:
        w = min(960, max(64, int(request.args.get("w", 640))))
        h = min(960, max(64, int(request.args.get("h", 480))))
    except (TypeError, ValueError):
        w, h = 640, 480

    try:
        model = mujoco.MjModel.from_xml_path(str(MUJOCO_SCENE_PREVIEW_XML.resolve()))
        data = mujoco.MjData(model)
        khome = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_KEY, "home")
        if khome >= 0:
            mujoco.mj_resetDataKeyframe(model, data, khome)
        n_robot = 25  # 7 free base + 12 gambe + 6 braccio (come simulate_python)
        if model.nq > n_robot and hasattr(model, "qpos0"):
            data.qpos[n_robot:] = model.qpos0[n_robot:]
        mujoco.mj_forward(model, data)
        renderer = mujoco.Renderer(model, h, w)
        try:
            renderer.update_scene(data)
            pixels = renderer.render()
        finally:
            close = getattr(renderer, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        return jsonify({"ok": False, "error": repr(exc)}), 503

    if cv2 is not None:
        try:
            ok, buf = cv2.imencode(".png", cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR))
            if ok:
                return Response(
                    buf.tobytes(),
                    mimetype="image/png",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
                )
        except Exception:
            pass
    try:
        from PIL import Image

        img = Image.fromarray(pixels)
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        return Response(
            bio.getvalue(),
            mimetype="image/png",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
    except Exception as enc_exc:
        return jsonify({"ok": False, "error": repr(enc_exc)}), 503


@APP.route("/api/health", methods=["GET"])
def api_health() -> Any:
    """Smoke test minimo — niente camere né DDS."""
    py_mt = _dashboard_py_mtime_epoch()
    reload_recommended = bool(py_mt is not None and py_mt > PROCESS_STARTED_AT_EPOCH)
    return jsonify(
        {
            "ok": True,
            "service": "go2_dashboard",
            "pid": os.getpid(),
            "process_started_at": PROCESS_STARTED_AT,
            "dashboard_py_mtime": _dashboard_py_mtime_iso(),
            "reload_recommended": reload_recommended,
            "reload_hint": (
                "Il file ``diagnostics_dashboard.py`` sul disco è più nuovo di questo processo: riavvia Flask "
                "(su NX: kill PID e rilancia). Il file ``templates/dashboard.html`` viene invece riletto automaticamente quando cambia."
                if reload_recommended
                else None
            ),
        }
    )


@APP.route("/api/base/sport_env", methods=["GET"])
def api_base_sport_env() -> Any:
    """
    Diagnostica IP/interfacce per Sport: la UI parla HTTP con questa macchina;
    i comandi base usano DDS (domain + interfaccia L2 verso il cane), non ``GO2_HOST``.
    """
    iface_raw = (GO2_DDS_INTERFACE or "").strip()
    port = int(os.environ.get("GO2_DASHBOARD_PORT", "5050"))
    return jsonify(
        {
            "ok": True,
            "sport_uses_dashboard_http_ip_for_rpc": False,
            "dashboard_bind_host": GO2_DASHBOARD_BIND,
            "dashboard_port": port,
            "go2_host_env": GO2_HOST,
            "go2_internal_host_env": GO2_INTERNAL_HOST,
            "dds_domain": GO2_DDS_DOMAIN,
            "dds_interface_env": iface_raw or None,
            "dds_interface_effective": iface_raw if iface_raw else "(vuoto — Cyclone DDS sceglie interfaccia di default)",
            "subnet_hint_it": (
                "La Sport API non è «scrivere a un IP»: usa DDS (Layer 2) sulla LAN Unitree tipica 192.168.123.0/24. "
                "Indirizzi spesso citati: Jetson/PC 192.168.123.18 o .222, MCU braccio .161, router .100; "
                "il cane come AP spesso .1 — il ping verifica solo IPv4, non sostituisce DDS domain/interfaccia."
            ),
            "sport_mode_service_hint_it": (
                "Su firmware/app Unitree: il controllo high-level richiede che il servizio sport sia consentito sul robot; "
                "in più segnalazioni (SDK Python issue #19) si risolve abilitando sport_mode dall’app Go2. "
                "Chiudi anche connessioni concorrenti (app/Wi‑Fi) che possono bloccare il canale."
            ),
            "motion_prepare_env_it": (
                "Se Sport risponde ma il robot non segue: sulla NX puoi provare MotionSwitcher prima dei comandi — "
                "GO2_SPORT_MOTION_PREPARE=1, opz. GO2_SPORT_RELEASE_IF_HELD=1, GO2_SPORT_SELECT_MODE=normal (o ai). "
                "Vedi script go2_accompany.py."
            ),
            "references": [
                {
                    "title": "unitree_sdk2_python — SportClient send error / sport_mode",
                    "url": "https://github.com/unitreerobotics/unitree_sdk2_python/issues/19",
                },
                {
                    "title": "Unitree sdk2 — SportClient (Go2)",
                    "url": "https://github.com/unitreerobotics/unitree_sdk2/blob/main/include/unitree/robot/go2/sport/sport_client.hpp",
                },
            ],
            "hint_it": (
                "Apri la dashboard su http://<IP-Jetson>:" + str(port) + " — corretto per Flask. "
                "Crouch/Stand inviano Sport RPC sulla NX via DDS: serve ``GO2_DDS_INTERFACE`` "
                "sulla NIC che ha reachability L2 verso il quadrupede (tipicamente eth0 su 192.168.123.x). "
                "Se manca o è wlan0/eth0 sbagliato, RPC può fallire (es. codice 3102)."
            ),
            "sport_connectivity_probe_get": "/api/base/sport_connectivity",
            "sport_connectivity_method_it": (
                "GET esegue MotionSwitcher.CheckMode sul cane (solo lettura, nessun movimento) — stesso DDS di SportClient."
            ),
        }
    )


@APP.route("/api/base/sport_last", methods=["GET"])
def api_base_sport_last() -> Any:
    """Ultimo risultato ``sport_accompany`` (anche da thread dopo HTTP 202)."""
    with LAST_SPORT_RPC_LOCK:
        snap = {k: v for k, v in LAST_SPORT_RPC.items()}
    return jsonify({"ok": True, **snap})


@APP.route("/api/base/sport_connectivity", methods=["GET"])
def api_base_sport_connectivity() -> Any:
    """
    Smoke DDS → cane senza movimento: ``MotionSwitcherClient.CheckMode`` (stesso trasporto della Sport API).

    Solo sulla Jetson con ``GO2_LOCAL=1``. Nessun comando StandDown/StandUp.
    """
    if not GO2_LOCAL:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "GO2_LOCAL!=1 — esegui questo probe sulla NX accanto al cane.",
                }
            ),
            403,
        )

    timeout_s = float(os.environ.get("GO2_SPORT_CONNECTIVITY_TIMEOUT_S", "15"))
    script = PROJECT_ROOT / "scripts" / "dds_motion_ping_once.py"
    if not script.is_file():
        return jsonify({"ok": False, "reason": "missing_scripts/dds_motion_ping_once.py"}), 500
    # Subprocess: un segfault in Cyclone/MotionSwitcher non deve abbattere il processo Flask.
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": f"probe_timeout_after_{timeout_s}s",
                    "hint_it": "MotionSwitcher non ha risposto in tempo — DDS o cane irraggiungibile.",
                }
            ),
            504,
        )
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 502

    if proc.returncode != 0:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "dds_motion_ping_subprocess_failed",
                    "returncode": proc.returncode,
                    "stderr": (proc.stderr or "")[:4000],
                    "stdout": (proc.stdout or "")[:1200],
                    "hint_it": "Il probe DDS è uscito con codice ≠0 (spesso segfault libreria nativa). Flask resta vivo.",
                }
            ),
            502,
        )
    try:
        result = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": f"json_decode:{exc!s}",
                    "stdout": (proc.stdout or "")[:1200],
                    "stderr": (proc.stderr or "")[:800],
                }
            ),
            502,
        )

    status = 200 if isinstance(result, dict) and result.get("ok") else 502
    return jsonify(result), status


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


ARM_GRASP_ABORT = threading.Event()
ARM_OPERATION_LOCK = threading.RLock()
LAST_ARM_JOB: dict[str, Any] = {"status": "idle", "updated_at": None, "detail": {}}
ARM_GRASP_EVENTS: list[dict[str, Any]] = []
ARM_GRASP_EVENTS_MAX = 80
# Ultimo esito Sport RPC (anche thread background): utile se HTTP 202 nasconde i codici.
LAST_SPORT_RPC: dict[str, Any] = {
    "updated_at": None,
    "mode": None,
    "sync": None,
    "result": None,
    "error": None,
}
LAST_SPORT_RPC_LOCK = threading.Lock()


def _sport_record_last(*, mode: str, sync: bool, result: Any | None, error: str | None) -> None:
    with LAST_SPORT_RPC_LOCK:
        LAST_SPORT_RPC["updated_at"] = now_iso()
        LAST_SPORT_RPC["mode"] = mode
        LAST_SPORT_RPC["sync"] = sync
        LAST_SPORT_RPC["result"] = result
        LAST_SPORT_RPC["error"] = error
# Override da dashboard (slider): usati dal grasp loop al posto di env per la sessione.
ARM_UI_TUNING: dict[str, Any] = {}
ARM_UI_TUNING_LOCK = threading.Lock()
# Flag grasp (bool) e stringhe opzionali — stessa istanza Flask sulla NX; reset esplicito da UI.
# Chiavi: trust_wrist_absolute_ik, use_fused_plan_ik, fused_with_center, front_camera_fallback_grasp,
# prefer_tag_grip, grasp_execute_arm (opzionale; se assente si usa solo env).
ARM_GRASP_SESSION: dict[str, Any] = {}
ARM_GRASP_SESSION_LOCK = threading.Lock()

# Offset visualizzazione 3D: tag5 → mount / camera front / offset locale polso (sessione Flask).
VIS_GEOMETRY_TUNING: dict[str, float] = {}
VIS_GEOMETRY_TUNING_LOCK = threading.Lock()
# EMA solo per la sfera verde (display) in scene_3d — non modifica il piano presa.
_SCENE3D_TARGET_DISPLAY_STATE: dict[str, Any] = {"ema_m": None}
_SCENE3D_TARGET_DISPLAY_LOCK = threading.Lock()


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


def _vis_geometry_defaults_dict() -> dict[str, float]:
    default_alpha = float(os.environ.get("GO2_SCENE3D_TARGET_EMA_ALPHA", "0.28"))
    # Legacy viewer calibration for the tag5 overlays.
    # Actual component poses in the 3D scene are taken directly from MJCF/FK below.
    return {
        "arm_vs_tag5_x": -0.19,
        "arm_vs_tag5_y": 0.0,
        "arm_vs_tag5_z": -0.08,
        "front_vs_tag5_x": 0.185,
        "front_vs_tag5_y": 0.0,
        "front_vs_tag5_z": -0.07,
        "wrist_local_dx": 0.0,
        "wrist_local_dy": 0.0,
        "wrist_local_dz": 0.0,
        "target_ema_alpha": default_alpha,
        "viz_go2_tx_m": 0.0,
        "viz_go2_ty_m": 0.0,
        "viz_go2_tz_m": 0.0,
        "viz_joint_markers_dx_m": -0.15,
        "viz_joint_markers_dy_m": 0.0,
        "viz_joint_markers_dz_m": 0.0,
        "viz_arm_mount_dx_m": -0.20,
        "viz_arm_mount_dy_m": 0.0,
        "viz_arm_mount_dz_m": 0.0,
        "viz_front_cam_dx_m": 0.225,
        "viz_front_cam_dy_m": 0.0,
        "viz_front_cam_dz_m": -0.05,
        "frustum_depth_rx_deg": 20.0,
        "frustum_depth_ry_deg": 0.0,
        "frustum_depth_rz_deg": 0.0,
        "frustum_wrist_rx_deg": 0.0,
        "frustum_wrist_ry_deg": 0.0,
        "frustum_wrist_rz_deg": 0.0,
        "frustum_depth_far_m": 0.62,
        "frustum_wrist_far_m": 0.58,
    }


_ALLOWED_VIS_GEOMETRY: dict[str, tuple[float, float]] = {
    "arm_vs_tag5_x": (-0.5, 0.5),
    "arm_vs_tag5_y": (-0.5, 0.5),
    "arm_vs_tag5_z": (-0.5, 0.5),
    "front_vs_tag5_x": (-0.5, 0.5),
    "front_vs_tag5_y": (-0.5, 0.5),
    "front_vs_tag5_z": (-0.5, 0.5),
    "wrist_local_dx": (-0.35, 0.35),
    "wrist_local_dy": (-0.35, 0.35),
    "wrist_local_dz": (-0.35, 0.35),
    "target_ema_alpha": (0.05, 0.99),
    "viz_go2_tx_m": (-0.95, 0.95),
    "viz_go2_ty_m": (-0.95, 0.95),
    "viz_go2_tz_m": (-0.95, 0.95),
    "viz_joint_markers_dx_m": (-0.15, 0.15),
    "viz_joint_markers_dy_m": (-0.15, 0.15),
    "viz_joint_markers_dz_m": (-0.15, 0.15),
    "viz_arm_mount_dx_m": (-0.25, 0.25),
    "viz_arm_mount_dy_m": (-0.25, 0.25),
    "viz_arm_mount_dz_m": (-0.25, 0.25),
    "viz_front_cam_dx_m": (-0.4, 0.4),
    "viz_front_cam_dy_m": (-0.4, 0.4),
    "viz_front_cam_dz_m": (-0.4, 0.4),
    "frustum_depth_rx_deg": (-120.0, 120.0),
    "frustum_depth_ry_deg": (-120.0, 120.0),
    "frustum_depth_rz_deg": (-120.0, 120.0),
    "frustum_wrist_rx_deg": (-120.0, 120.0),
    "frustum_wrist_ry_deg": (-120.0, 120.0),
    "frustum_wrist_rz_deg": (-120.0, 120.0),
    "frustum_depth_far_m": (0.04, 1.8),
    "frustum_wrist_far_m": (0.04, 1.8),
}

VIS_GEOMETRY_JSON_PATH = PROJECT_ROOT / "data" / "vis_geometry_tuning.json"
VIS_GEOMETRY_PRESETS_PATH = PROJECT_ROOT / "data" / "vis_geometry_presets.json"
VIS_GEOMETRY_PRESETS_LOCK = threading.Lock()


def _load_vis_geometry_from_disk() -> None:
    """Ripristina slider geometria 3D dopo riavvio Flask (NX/PC)."""
    if not VIS_GEOMETRY_JSON_PATH.is_file():
        return
    try:
        raw = json.loads(VIS_GEOMETRY_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    with VIS_GEOMETRY_TUNING_LOCK:
        for key, (lo, hi) in _ALLOWED_VIS_GEOMETRY.items():
            if key not in raw:
                continue
            try:
                v = float(raw[key])
            except (TypeError, ValueError):
                continue
            VIS_GEOMETRY_TUNING[key] = max(lo, min(hi, v))


def _save_vis_geometry_to_disk() -> None:
    with VIS_GEOMETRY_TUNING_LOCK:
        snap = {str(k): float(v) for k, v in VIS_GEOMETRY_TUNING.items()}
    try:
        VIS_GEOMETRY_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        VIS_GEOMETRY_JSON_PATH.write_text(
            json.dumps(snap, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _vis_geometry_effective() -> dict[str, float]:
    defaults = _vis_geometry_defaults_dict()
    with VIS_GEOMETRY_TUNING_LOCK:
        over = dict(VIS_GEOMETRY_TUNING)
    return {**defaults, **over}


def _vis_geometry_preset_snapshot_effective() -> dict[str, float]:
    eff = _vis_geometry_effective()
    out: dict[str, float] = {}
    for k in _ALLOWED_VIS_GEOMETRY:
        try:
            out[k] = float(eff[k])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _vis_geometry_apply_preset_values(values: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    with VIS_GEOMETRY_TUNING_LOCK:
        VIS_GEOMETRY_TUNING.clear()
        for key, (lo, hi) in _ALLOWED_VIS_GEOMETRY.items():
            if key not in values:
                continue
            try:
                raw_v = float(values[key])
            except (TypeError, ValueError):
                errs.append(key)
                continue
            VIS_GEOMETRY_TUNING[key] = max(lo, min(hi, raw_v))
    return errs


def _vis_geometry_presets_read_dict() -> dict[str, Any]:
    if not VIS_GEOMETRY_PRESETS_PATH.is_file():
        return {"version": 1, "presets": {}}
    try:
        raw = json.loads(VIS_GEOMETRY_PRESETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "presets": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "presets": {}}
    pr = raw.get("presets")
    if not isinstance(pr, dict):
        raw["presets"] = {}
    return raw


def _vis_geometry_presets_write_dict(data: dict[str, Any]) -> bool:
    try:
        VIS_GEOMETRY_PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        VIS_GEOMETRY_PRESETS_PATH.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def _sanitize_preset_name(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n or len(n) > 80:
        return None
    if any(c in n for c in "<>\r\n\x00"):
        return None
    return n


def _builtin_vis_geometry_preset_2_values() -> dict[str, float]:
    """Preset «2» incorporato se ``data/vis_geometry_presets.json`` non lo definisce.

    Geometria nominale camera polso/MJCF in ``arm_kinematics_d1_template``; slider polso a zero.
    """
    base = dict(_vis_geometry_defaults_dict())
    out: dict[str, float] = {}
    for key, (lo, hi) in _ALLOWED_VIS_GEOMETRY.items():
        try:
            out[key] = max(lo, min(hi, float(base[key])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _ensure_named_preset_on_disk(name: str, vals: dict[str, float]) -> None:
    """Aggiunge il preset al JSON se manca, così GET /presets e il menu mostrano il nome."""
    sn = _sanitize_preset_name(name)
    if not sn:
        return
    snap = {str(k): float(vals[k]) for k in _ALLOWED_VIS_GEOMETRY if k in vals}
    if not snap:
        return
    with VIS_GEOMETRY_PRESETS_LOCK:
        raw = _vis_geometry_presets_read_dict()
        presets_obj = raw.setdefault("presets", {})
        if not isinstance(presets_obj, dict):
            presets_obj = {}
            raw["presets"] = presets_obj
        if sn in presets_obj:
            return
        presets_obj[sn] = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "values": snap,
        }
        _vis_geometry_presets_write_dict(raw)


def _apply_startup_vis_geometry_preset_if_configured() -> None:
    """Applica un preset vis-geometry all'avvio del processo e persiste su ``vis_geometry_tuning.json``.

    Nome da ``GO2_VIS_GEOMETRY_DEFAULT_PRESET`` (default ``\"2\"``). Disabilitazione:
    ``GO2_SKIP_DEFAULT_VIS_GEOMETRY_PRESET=1``.

    Se il nome è ``\"2\"`` e non è definito in ``vis_geometry_presets.json``, si usa il preset
    incorporato e viene creato l'entry nel file (menu preset popolato).
    """
    skip = os.environ.get("GO2_SKIP_DEFAULT_VIS_GEOMETRY_PRESET", "").strip().lower()
    if skip in {"1", "true", "yes", "on"}:
        return
    # Nota: ``os.environ.get("KEY", "2")`` restituisce "" se KEY è esportata vuota → rompe il default.
    raw_dflt = os.environ.get("GO2_VIS_GEOMETRY_DEFAULT_PRESET")
    if raw_dflt is None:
        default_nm = "2"
    else:
        default_nm = raw_dflt.strip() or "2"
    name = _sanitize_preset_name(default_nm)
    if not name:
        return
    with VIS_GEOMETRY_PRESETS_LOCK:
        raw = _vis_geometry_presets_read_dict()
        entry = (raw.get("presets") or {}).get(name)
    vals: dict[str, Any] | None = None
    used_builtin = False
    if isinstance(entry, dict):
        v = entry.get("values")
        if isinstance(v, dict):
            vals = dict(v)
    if vals is None and name == "2":
        vals = _builtin_vis_geometry_preset_2_values()
        used_builtin = True
    if not vals:
        return
    errs = _vis_geometry_apply_preset_values(vals)
    if errs:
        _LOG_VIS.warning(
            "vis_geometry startup preset %r NOT applied (parse/clamp errors): %s",
            name,
            errs[:12],
        )
        return
    with _SCENE3D_TARGET_DISPLAY_LOCK:
        _SCENE3D_TARGET_DISPLAY_STATE["ema_m"] = None
    _save_vis_geometry_to_disk()
    try:
        eff = _vis_geometry_effective()
        _LOG_VIS.info(
            "vis_geometry startup preset %r OK — Corpo Go2 mm: tx=%.1f ty=%.1f tz=%.1f | "
            "mount vs tag5 mm: %.1f,%.1f,%.1f | wrist_local mm: %.1f,%.1f,%.1f",
            name,
            float(eff.get("viz_go2_tx_m", 0.0)) * 1000.0,
            float(eff.get("viz_go2_ty_m", 0.0)) * 1000.0,
            float(eff.get("viz_go2_tz_m", 0.0)) * 1000.0,
            float(eff.get("arm_vs_tag5_x", 0.0)) * 1000.0,
            float(eff.get("arm_vs_tag5_y", 0.0)) * 1000.0,
            float(eff.get("arm_vs_tag5_z", 0.0)) * 1000.0,
            float(eff.get("wrist_local_dx", 0.0)) * 1000.0,
            float(eff.get("wrist_local_dy", 0.0)) * 1000.0,
            float(eff.get("wrist_local_dz", 0.0)) * 1000.0,
        )
    except Exception:
        pass
    if used_builtin:
        _ensure_named_preset_on_disk("2", _builtin_vis_geometry_preset_2_values())


_load_vis_geometry_from_disk()
_apply_startup_vis_geometry_preset_if_configured()


def _scene3d_target_ema_update(
    raw_m: list[float] | None,
    alpha: float,
    *,
    freeze_on_missing: bool = False,
) -> list[float] | None:
    """EMA sul target fused per la vista 3D; reset se il planner non fornisce target."""
    with _SCENE3D_TARGET_DISPLAY_LOCK:
        st = _SCENE3D_TARGET_DISPLAY_STATE
        if raw_m is None or len(raw_m) < 3:
            if freeze_on_missing:
                ema = st.get("ema_m")
                if ema is None or not isinstance(ema, list) or len(ema) < 3:
                    return None
                return [round(float(ema[i]), 5) for i in range(3)]
            st["ema_m"] = None
            return None
        a = max(0.0, min(1.0, float(alpha)))
        ema = st.get("ema_m")
        if ema is None or not isinstance(ema, list) or len(ema) < 3:
            st["ema_m"] = [float(raw_m[i]) for i in range(3)]
        else:
            for i in range(3):
                ema[i] = a * float(raw_m[i]) + (1.0 - a) * float(ema[i])
        out = st["ema_m"]
        return [round(float(out[i]), 5) for i in range(3)]


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
        "object_detection_label": obj.get("label"),
        "object_detection_confidence": obj.get("confidence"),
        "object_bbox_area_ratio": obj.get("bbox_area_ratio"),
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
        "grasp_assessment": candidate_grasp_assessment(c),
    }


def _plan_ready_for_fused_ik(plan: dict[str, Any]) -> bool:
    """True solo se il piano selezionato è eseguibile **e** validato 3D, salvo override esplicito per preview euristiche."""
    if not plan.get("ok"):
        return False
    assessment = plan_grasp_assessment(plan)
    selected = assessment.get("selected") or {}
    return bool(selected.get("execution_allowed"))


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
    plan_assessment = plan_grasp_assessment(plan)
    selected_assessment = plan_assessment.get("selected") or {}
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
    if selected_assessment.get("validated_3d"):
        story.append(
            f"⑤ Candidato grasp 3D validato (camera scelta: {sel}, sorgente: {selected_assessment.get('source_kind')})."
        )
    elif selected_assessment.get("preview_only"):
        story.append(
            f"⑤ Solo preview euristica 2D/monoculare (camera scelta: {sel}). Utile per debug/UI, non equivalente a grasp 3D validato."
        )
    elif grip_any:
        story.append(
            "⑤ Punto presa 2D disponibile, ma senza validazione 3D: il visual servo può centrare/avvicinare prima di una vera policy di grasp."
        )
    else:
        story.append(
            "⑤ Piano grasp non pronto: servono tag scatola 0–3 oppure depth/pose 3D vera; il fallback bbox resta solo preview euristica."
        )
        for dev in (0, 6):
            d = per_dev[str(dev)]
            if d.get("camera_error"):
                story.append(f"   · /dev/video{dev}: frame — {d['camera_error']}")
                continue
            if d.get("object_detection_ok"):
                story.append(
                    f"   · /dev/video{dev}: detector {d.get('object_detection_backend')} vede grip point, ma non è ancora un candidato 3D validato."
                )
                continue
            if not d.get("tag_ids_seen"):
                story.append(f"   · /dev/video{dev}: nessun AprilTag e nessuna box detection utilizzabile.")
                continue
            if not d.get("has_box_tags_for_ik"):
                story.append(
                    f"   · /dev/video{dev}: vedi solo landmark (es. id5); per 3D box servono tag scatola 0–3 o depth/pose reale."
                )
                continue
            if not d.get("target_ok"):
                story.append(f"   · /dev/video{dev}: tag box ok ma target base no — {d.get('target_error')}")
                continue
            if not d.get("preview_ok"):
                fe = d.get("ik_failed_stage") or d.get("preview_error") or "?"
                story.append(f"   · /dev/video{dev}: target ok ma IK fallita (stage/err: {fe}).")

    fused_env = _effective_grasp_bool("use_fused_plan_ik", "GO2_GRASP_USE_FUSED_PLAN_IK")
    if selected_assessment.get("preview_only") and not selected_assessment.get("allow_heuristic_execute"):
        story.append(
            "⑥ Safety gate: preview euristica 2D non promossa a esecuzione. Per sbloccarla esplicitamente: GO2_GRASP_ALLOW_HEURISTIC_EXECUTE=1 (rischioso)."
        )
    elif fused_env and selected_assessment.get("validated_3d"):
        story.append("⑥ Piano fuso utilizzabile: il candidato selezionato è validato 3D ed è ammesso all'esecuzione.")

    fusion_ready_exec = _plan_ready_for_fused_ik(plan)
    front_first_flow_enabled = _front_first_grasp_flow_enabled()
    preferred_execution_flow = _grasp_front_first_sequence_steps()
    if front_first_flow_enabled:
        story.append("Flusso attivo: camera frontale → START fisso → presa → ritorno a START.")
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
            "GO2_GRASP_ALLOW_HEURISTIC_EXECUTE": os.environ.get("GO2_GRASP_ALLOW_HEURISTIC_EXECUTE", "0"),
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
        "front_first_flow_enabled": front_first_flow_enabled,
        "preferred_execution_flow": preferred_execution_flow,
        "candidates": per_dev,
        "grasp_assessment": plan_assessment,
        "selected_grasp_assessment": selected_assessment,
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
    }
    for k, v in extra.items():
        if v is None:
            continue
        try:
            event[k] = _json_safe_for_status(v)
        except Exception:
            event[k] = repr(v)
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


def _candidate_debug_summary(candidate: dict[str, Any] | None) -> dict[str, Any]:
    cand = candidate or {}
    tags = ((cand.get("tags") or {}).get("tags") or []) if isinstance(cand, dict) else []
    ids = [int(t.get("id", -1)) for t in tags if isinstance(t, dict)]
    box_ids = [i for i in ids if i in BOX_TAG_IDS_IK]
    grip = (cand.get("grip_point") or {}) if isinstance(cand, dict) else {}
    preview = (cand.get("preview") or {}) if isinstance(cand, dict) else {}
    target = (cand.get("target") or {}) if isinstance(cand, dict) else {}
    out: dict[str, Any] = {
        "ok": bool(cand.get("ok")),
        "camera_device": cand.get("camera_device"),
        "camera_label": cand.get("camera_label"),
        "tag_ids": ids,
        "box_tag_ids": box_ids,
        "tag_count": len(ids),
        "box_tag_count": len(box_ids),
        "grip_ok": bool(grip.get("ok")),
        "grip_source": grip.get("source"),
        "preview_ok": bool(preview.get("ok")),
        "absolute_ik_safe": bool(cand.get("absolute_ik_safe", True)),
        "target_base_xyz_m": target.get("base_xyz_m"),
    }
    if isinstance(grip, dict):
        for key in (
            "grip_center_px",
            "grip_axis_px",
            "box_bbox_px",
            "box_size_px",
            "approach_error_px",
            "approach_error_norm",
            "confidence",
        ):
            if key in grip:
                out[key] = grip.get(key)
    if isinstance(preview, dict):
        out["preview_failed_stage"] = preview.get("failed_stage")
        out["preview_target_xyz_m"] = preview.get("target_xyz_m")
    return out


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


def _sport_stand_modes_use_subprocess() -> bool:
    """StandDown/StandUp in processo figlio — evita SIGSEGV Cyclone che uccide Flask (default: on)."""
    return os.environ.get("GO2_SPORT_SUBPROCESS_STAND_MODES", "1").lower() in {"1", "true", "yes", "on"}


def _sport_accompany_subprocess(
    *,
    mode: str,
    enable: bool,
    stand_up_first: bool,
    speed_level: int | None,
) -> dict[str, Any]:
    """Esegue ``sport_accompany`` in subprocess; ritorna dict (anche se crash/timeout)."""
    script = PROJECT_ROOT / "scripts" / "sport_accompany_once.py"
    if not script.is_file():
        return {"ok": False, "reason": "missing_scripts/sport_accompany_once.py", "mode": mode}
    cmd: list[str] = [
        sys.executable,
        str(script),
        "--mode",
        mode,
        "--enable",
        "1" if enable else "0",
        "--stand-up-first",
        "1" if stand_up_first else "0",
    ]
    if speed_level is not None:
        cmd.extend(["--speed-level", str(int(speed_level))])
    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "55"))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "mode": mode,
            "reason": f"sport_subprocess_timeout_after_{timeout_s}s",
            "hint_it": "Il processo Sport non ha finito in tempo.",
        }
    stderr = (proc.stderr or "")[-4000:]
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return {
            "ok": False,
            "mode": mode,
            "reason": f"sport_subprocess_exit_{proc.returncode}",
            "hint_it": (
                "Uscita anomala (spesso SIGSEGV=-11 in CycloneDDS). Allinea cyclonedds + unitree_sdk2py sulla NX; "
                "evita due copie SDK in PYTHONPATH."
            ),
            "stderr_tail": stderr[-2500:],
        }
    try:
        out: dict[str, Any] = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {
            "ok": False,
            "mode": mode,
            "reason": "sport_subprocess_bad_json",
            "stdout": (proc.stdout or "")[:2000],
            "stderr": stderr[-1500:],
            "subprocess_returncode": proc.returncode,
        }
    if proc.returncode != 0 and not out.get("ok"):
        out["subprocess_returncode"] = proc.returncode
        if stderr.strip():
            out["stderr_tail"] = stderr[-2000:]
    return out


@APP.route("/api/base/accompany_mode", methods=["GET", "POST"])
def api_base_accompany_mode() -> Any:
    ok_gate, reason = _base_motion_allowed()
    if not ok_gate:
        return jsonify({"ok": False, "reason": reason}), 403

    if request.method == "GET":
        # Fallback LAN: alcuni browser/proxy rispondono "Failed to fetch" solo su POST JSON (preflight/CORB).
        # GET «semplice» evita Content-Type application/json + preflight.
        if os.environ.get("GO2_ALLOW_GET_BASE_MOTION", "1").lower() not in {"1", "true", "yes", "on"}:
            return jsonify({"ok": False, "reason": "GET disabled (set GO2_ALLOW_GET_BASE_MOTION=1)"}), 405
        mq = (request.args.get("mode") or "").strip().lower()
        if not mq:
            return jsonify({"ok": False, "reason": "missing_query_parameter_mode"}), 400
        body: dict[str, Any] = {"mode": mq, "enable": True, "stand_up_first": False}
        if request.args.get("stand_up_first", "").lower() in {"1", "true", "yes"}:
            body["stand_up_first"] = True
        if request.args.get("enable", "").lower() in {"0", "false", "no"}:
            body["enable"] = False
        sl = request.args.get("speed_level")
        if sl is not None and str(sl).strip() != "":
            try:
                body["speed_level"] = int(sl)
            except ValueError:
                pass
        if request.args.get("sync", "").lower() in {"1", "true", "yes"}:
            body["sync"] = True
    else:
        body = request.get_json(silent=True) or {}

    enable = bool(body.get("enable", True))
    stand_first = bool(body.get("stand_up_first", False))
    speed_raw = body.get("speed_level")
    speed_level = int(speed_raw) if speed_raw is not None else None

    iface = GO2_DDS_INTERFACE.strip() if GO2_DDS_INTERFACE else None
    mode = str(body.get("mode") or "joystick").strip().lower()
    dds_iface_report = iface if iface else None

    def _sport_call() -> Any:
        if mode in {"crouch", "stand_up"} and _sport_stand_modes_use_subprocess():
            return _sport_accompany_subprocess(
                mode=mode,
                enable=enable,
                stand_up_first=stand_first,
                speed_level=speed_level,
            )
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from go2_accompany import sport_accompany

        return sport_accompany(
            project_root=PROJECT_ROOT,
            domain=GO2_DDS_DOMAIN,
            iface=iface,
            enable=enable,
            mode=mode,
            stand_up_first=stand_first,
            speed_level=speed_level,
        )

    sync = os.environ.get("GO2_SPORT_RPC_SYNC", "0").lower() in {"1", "true", "yes"}
    if request.args.get("sync", "").lower() in {"1", "true", "yes"}:
        sync = True
    if isinstance(body.get("sync"), bool) and body.get("sync"):
        sync = True
    # Crouch/Stand: default sincrono così la risposta HTTP riporta i codici RPC reali (202 «OK» nasconde fallimenti DDS).
    async_stand = os.environ.get("GO2_SPORT_ASYNC_STAND_MODES", "0").lower() in {"1", "true", "yes"}
    if mode in {"crouch", "stand_up"} and not async_stand:
        sync = True

    if not sync:
        # Risposta immediata (202): import DDS + RPC Sport restano nel thread in background.
        # Così il browser non va in "Failed to fetch" mentre il worker è occupato (MJPEG + GIL + RPC lunga).
        def _bg() -> None:
            try:
                result = _sport_call()
                _sport_record_last(mode=mode, sync=False, result=result, error=None)
                _LOG_VIS.info("sport_accompany mode=%s ok=%s", mode, result.get("ok") if isinstance(result, dict) else result)
            except Exception as exc:
                _sport_record_last(mode=mode, sync=False, result=None, error=repr(exc))
                _LOG_VIS.exception("sport_accompany mode=%s failed (background)", mode)

        threading.Thread(target=_bg, name=f"sport-{mode}", daemon=True).start()
        return (
            jsonify(
                {
                    "ok": True,
                    "accepted": True,
                    "async": True,
                    "mode": mode,
                    "dds_domain": GO2_DDS_DOMAIN,
                    "dds_interface": dds_iface_report,
                    "hint_it": "Sport RPC avviato in background sulla NX. Se il cane non reagisce, controlla i log (dashboard_run.log) e DDS.",
                    "dds_hint_it": (
                        "Sport non usa l'IP del browser: DDS domain "
                        + str(GO2_DDS_DOMAIN)
                        + ", interfaccia "
                        + (dds_iface_report or "default Cyclone")
                        + ". Verifica GET /api/base/sport_env sulla NX."
                    ),
                }
            ),
            202,
        )

    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "45"))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_sport_call)
            result = fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        _sport_record_last(
            mode=mode,
            sync=True,
            result=None,
            error=f"sport_rpc_timeout_after_{timeout_s}s",
        )
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
        _sport_record_last(mode=mode, sync=True, result=None, error=repr(exc))
        return jsonify({"ok": False, "reason": repr(exc)}), 502

    _sport_record_last(mode=mode, sync=True, result=result, error=None)
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
import base64, cv2, sys, os
logical = {int(device)}
key = f"GO2_VIDEO_INDEX_{{logical}}"
try:
    v4l = int(str(os.environ.get(key, logical)).strip())
except ValueError:
    v4l = logical
cap = cv2.VideoCapture(v4l)
ok = False
frame = None
if cap.isOpened():
    for _ in range(3):
        ok, frame = cap.read()
        if ok and frame is not None:
            break
cap.release()
if not ok or frame is None:
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
    hold_between_chunks: bool | None = None,
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
    if hold_between_chunks is None:
        hold_between_chunks = os.environ.get("GO2_D1_HOLD_BETWEEN_CHUNKS", "1").lower() in {
            "1",
            "true",
            "yes",
        }
    _arm_event(
        "d1_run_begin",
        "Invio comandi D1",
        total_messages=len(messages),
        chunks_total=len(chunks),
        delay_ms=delay_ms,
        abortable=abortable,
        ignore_abort=ignore_abort,
        post_hold=bool(post_hold),
        hold_between_chunks=bool(hold_between_chunks),
    )
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
        _arm_event(
            "d1_chunk_begin",
            "Eseguo chunk D1",
            chunk_index=idx + 1,
            chunks_total=len(chunks),
            chunk_messages=len(chunk),
            first_stage=(chunk[0].get("data") or {}).get("stage") if chunk else None,
            last_stage=(chunk[-1].get("data") or {}).get("stage") if chunk else None,
        )
        result = _d1_arm_command_subprocess_run(
            str(helper),
            int(GO2_DDS_DOMAIN),
            int(delay_ms),
            stdin,
            cwd=str(PROJECT_ROOT),
            timeout_s=max(12.0, (delay_ms / 1000.0 + 0.4) * len(chunk)),
        )
        outs.append(result.stdout)
        errs.append(result.stderr)
        _arm_event(
            "d1_chunk_done",
            "Chunk D1 completato",
            chunk_index=idx + 1,
            chunks_total=len(chunks),
            returncode=result.returncode,
            stdout_tail=(result.stdout or "")[-700:],
            stderr_tail=(result.stderr or "")[-700:],
        )
        if result.returncode != 0:
            break
        # Tra un subprocess e l'altro i servo possono cedere: rileggi posa e ripeti hold breve
        # così il chunk successivo parte dal feedback reale (meno scatti).
        if (
            hold_between_chunks
            and idx + 1 < len(chunks)
            and os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"}
        ):
            publish_d1_hold_current(
                repeats=max(5, int(os.environ.get("D1_INTER_CHUNK_HOLD_REPEATS", "10"))),
                delay_ms=max(38, int(os.environ.get("D1_INTER_CHUNK_HOLD_DELAY_MS", "48"))),
            )
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


def _read_d1_servo_angles_cached() -> list[float] | None:
    """Ritorna l'ultimo feedback servo in cache senza avviare un refresh DDS costoso."""
    with _D1_SERVO_FB_CV:
        ang = _D1_SERVO_FB_STATE.get("angles")
        if isinstance(ang, list) and len(ang) >= 7:
            return [float(v) for v in ang[:7]]
    return None


def _read_d1_servo_angles_cached_or_start() -> list[float] | None:
    """
    Hold rapido: prima cache viva, poi eventuale snapshot START salvato.
    Non fa refresh DDS bloccante: il grasp non deve fermarsi qui per minuti.
    """
    cur = _read_d1_servo_angles_cached()
    if cur is not None:
        return cur
    try:
        if ALIGNMENT_START_PATH.is_file():
            data = json.loads(ALIGNMENT_START_PATH.read_text(encoding="utf-8"))
            arm = (data.get("arm_at_start") or data.get("arm") or {}) if isinstance(data, dict) else {}
            jr = arm.get("joints_rad") if isinstance(arm, dict) else None
            if isinstance(jr, list) and len(jr) >= 6:
                servo = [math.degrees(float(jr[i])) for i in range(6)]
                if len(jr) >= 7 and isinstance(jr[6], (int, float)):
                    servo.append(float(jr[6]))
                else:
                    servo.append(servo[-1] if servo else 0.0)
                while len(servo) < 7:
                    servo.append(servo[-1])
                return [float(v) for v in servo[:7]]
    except Exception:
        pass
    return None


def publish_d1_hold_current(*, repeats: int | None = None, delay_ms: int | None = None) -> dict[str, Any]:
    """
    Ripete la posa servo letta da cache/START: riduce cedimenti/creep tra un comando e l'altro.

    IMPORTANT: non fa una lettura DDS bloccante qui. Se il feedback non è già in cache,
    usa il salvataggio START oppure fallisce veloce invece di congelare la sequenza.
    """
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes"}:
        return {"ok": False, "reason": "GO2_ENABLE_REAL_ARM is not enabled"}
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.exists():
        return {"ok": False, "reason": f"D1 DDS helper missing: {helper}"}
    rpt = repeats if repeats is not None else int(os.environ.get("D1_HOLD_REPEATS", "14"))
    dms = delay_ms if delay_ms is not None else int(os.environ.get("D1_HOLD_DELAY_MS", "95"))
    cur = _read_d1_servo_angles_cached_or_start()
    if cur is None:
        return {"ok": False, "reason": "No cached D1 servo feedback; cannot hold quickly"}
    seq = int(time.time()) % 100000
    _arm_event(
        "hold_begin",
        "Hold posa corrente",
        repeats=rpt,
        delay_ms=dms,
        snapshot_deg=[round(float(v), 3) for v in cur[:7]],
    )
    messages: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    for i in range(max(3, rpt)):
        angles = {f"angle{idx}": round(float(cur[idx]), 3) for idx in range(7)}
        angles["mode"] = 1
        messages.append({"seq": seq + 1 + i, "address": 1, "funcode": 2, "data": angles})
    result = _run_d1_messages(
        messages, delay_ms=max(40, dms), ignore_abort=True, hold_between_chunks=False
    )
    _arm_event(
        "hold_done",
        "Hold completato",
        ok=bool(result.get("ok")),
        helper_returncode=result.get("helper_returncode"),
        mode="hold_current_pose",
    )
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


def _d1_servo_feedback_subprocess_run(
    exec_argv: list[str],
    *,
    cwd: str,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """
    Esegue helper C++/Python lettura servo.

    Su Linux (NX): ``source scripts/nx_dashboard_env.sh`` prima di ``exec`` così ``CYCLONEDDS_HOME`` /
    ``LD_LIBRARY_PATH`` coincidono con Sport. Senza questo, il wheel ``cyclonedds`` può caricare un
    ``libddsc`` incoerente e ``Topic(PubServoInfo_)`` fallisce mentre ``rt/lowstate`` funziona.
    """
    env_sh = PROJECT_ROOT / "scripts" / "nx_dashboard_env.sh"
    if os.name != "nt" and env_sh.is_file():
        inner = " ".join(shlex.quote(a) for a in exec_argv)
        script = (
            f"cd {shlex.quote(str(cwd))} && . {shlex.quote(str(env_sh))} && exec {inner}"
        )
        return subprocess.run(
            ["bash", "-c", script],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    return subprocess.run(
        exec_argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
    )


def _d1_arm_command_subprocess_run(
    helper: str,
    domain: int,
    delay_ms: int,
    stdin: str,
    *,
    cwd: str,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """
    Pubblica su ``rt/arm_Command`` via ``d1_arm_command``. Stesso wrapper del feedback:
    ``source nx_dashboard_env.sh`` così ``LD_LIBRARY_PATH`` / iceoryx coincidono con Sport.
    """
    env_sh = PROJECT_ROOT / "scripts" / "nx_dashboard_env.sh"
    if os.name != "nt" and env_sh.is_file():
        script = (
            f"cd {shlex.quote(cwd)} && . {shlex.quote(str(env_sh))} && "
            f"exec {shlex.quote(helper)} {int(domain)} {int(delay_ms)}"
        )
        return subprocess.run(
            ["bash", "-c", script],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    return subprocess.run(
        [helper, str(int(domain)), str(int(delay_ms))],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
    )


# Lettura servo via subprocess+DDS è costosa (secondi). Il viewer Three.js chiama ``/api/arm/scene_3d?fast=1`` ~5 Hz:
# senza cache ogni richiesta rilancia il helper → niente tempo reale e CPU saturata.
_D1_SERVO_FB_CV = threading.Condition()
_D1_SERVO_FB_STATE: dict[str, Any] = {
    "mono": 0.0,
    "angles": None,
    "diag": {},
    "refreshing": False,
    "fail_mono": 0.0,
    "fail_diag": {},
}


def _read_d1_servo_angles_uncached() -> tuple[list[float] | None, dict[str, Any]]:
    """Legge angoli servo D1: prima ``bin/d1_arm_feedback_helper`` (C++), poi ``scripts/d1_arm_servo_read_python.py`` (Python / stesso DDS di Sport)."""
    helper = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
    py_reader = PROJECT_ROOT / "scripts" / "d1_arm_servo_read_python.py"
    listen_s = max(1, int(os.environ.get("D1_FEEDBACK_HELPER_LISTEN_S", "3")))
    timeout_s = float(os.environ.get("D1_FEEDBACK_HELPER_TIMEOUT_S", "14"))
    domain = int(GO2_DDS_DOMAIN)
    base: dict[str, Any] = {
        "project_root": str(PROJECT_ROOT),
        "helper_path": str(helper),
        "helper_exists": helper.is_file(),
        "helper_executable": bool(helper.is_file() and os.access(helper, os.X_OK)),
        "python_reader_path": str(py_reader),
        "python_reader_exists": py_reader.is_file(),
        "dds_domain": domain,
        "listen_s": listen_s,
        "timeout_subprocess_s": timeout_s,
        "go2_local": bool(GO2_LOCAL),
    }

    def _parse_servo_stdout(stdout: str) -> tuple[list[float] | None, str | None]:
        latest: list[float] | None = None
        for line in (stdout or "").splitlines():
            if line.startswith("servo_angles "):
                parts = line.split()[1:]
                if len(parts) >= 7:
                    try:
                        latest = [float(v) for v in parts[:7]]
                    except ValueError:
                        latest = None
        return latest, None

    def _merge_stderr(d: dict[str, Any], stderr: str) -> None:
        st = (stderr or "").strip()
        if st:
            d["stderr_tail"] = st[-900:]

    def _run_cpp() -> tuple[list[float] | None, dict[str, Any]]:
        d: dict[str, Any] = dict(base)
        d["backend"] = "cpp_subprocess"
        if not helper.is_file():
            d["reason"] = "MISSING_BINARY"
            d["fix_it"] = "Sulla NX: bash scripts/build_d1_arm_helpers.sh oppure usa fallback Python (automatico)."
            return None, d
        if not os.access(helper, os.X_OK):
            d["reason"] = "HELPER_NOT_EXECUTABLE"
            d["fix_it"] = "chmod +x bin/d1_arm_feedback_helper"
            return None, d
        cmd = [str(helper), str(domain), str(listen_s)]
        d["argv"] = cmd
        try:
            t0 = time.perf_counter()
            result = _d1_servo_feedback_subprocess_run(
                cmd, cwd=str(PROJECT_ROOT), timeout_s=timeout_s
            )
            d["duration_s"] = round(time.perf_counter() - t0, 3)
            d["returncode"] = int(result.returncode)
            stderr = (result.stderr or "").strip()
            _merge_stderr(d, stderr)
            if stderr and ("symbol lookup error" in stderr or "undefined symbol" in stderr):
                d["reason"] = "HELPER_RUNTIME_LINK_ERROR"
                d["fix_it"] = (
                    "Ricompila ``d1_arm_feedback_helper`` sulla NX (``bash scripts/build_d1_arm_helpers.sh``) "
                    "oppure usa lettura Python (fallback automatico)."
                )
                return None, d
            stdout = result.stdout or ""
            for line in stdout.splitlines():
                if line.startswith("servo_count="):
                    d["dds_counts_line"] = line.strip()
                    break
            latest, _ = _parse_servo_stdout(stdout)
            if latest is not None:
                d["reason"] = "OK"
                return latest, d
            d["reason"] = "NO_SERVO_ANGLES_LINE"
            st = stdout.strip()
            d["stdout_tail"] = st[-900:] if st else None
            d["fix_it"] = (
                f"In {listen_s}s nessun topic DDS `current_servo_angle` (PubServoInfo). "
                "Braccio acceso? Prova GO2_DDS_DOMAIN / GO2_DDS_INTERFACE come per Sport."
            )
            return None, d
        except subprocess.TimeoutExpired as exc:
            d["reason"] = "HELPER_TIMEOUT"
            d["fix_it"] = "Aumenta D1_FEEDBACK_HELPER_TIMEOUT_S o verifica DDS."
            if exc.stdout:
                d["stdout_tail"] = str(exc.stdout)[-500:]
            if exc.stderr:
                d["stderr_tail"] = str(exc.stderr)[-500:]
            return None, d
        except Exception as exc:
            d["reason"] = "SUBPROCESS_FAILED"
            d["error"] = repr(exc)
            return None, d

    def _run_python() -> tuple[list[float] | None, dict[str, Any]]:
        d: dict[str, Any] = dict(base)
        d["backend"] = "python_subprocess"
        if not py_reader.is_file():
            d["reason"] = "MISSING_PYTHON_READER"
            d["fix_it"] = "Deploy aggiornato: manca scripts/d1_arm_servo_read_python.py"
            return None, d
        cmd = [sys.executable, str(py_reader), str(domain), str(listen_s)]
        d["argv"] = cmd
        try:
            t0 = time.perf_counter()
            result = _d1_servo_feedback_subprocess_run(
                cmd, cwd=str(PROJECT_ROOT), timeout_s=timeout_s
            )
            d["duration_s"] = round(time.perf_counter() - t0, 3)
            d["returncode"] = int(result.returncode)
            _merge_stderr(d, (result.stderr or "").strip())
            stdout = result.stdout or ""
            for line in stdout.splitlines():
                if line.startswith("servo_count="):
                    d["dds_counts_line"] = line.strip()
                    break
            latest, _ = _parse_servo_stdout(stdout)
            if latest is not None:
                d["reason"] = "OK"
                return latest, d
            d["reason"] = "NO_SERVO_ANGLES_LINE_PYTHON"
            st = stdout.strip()
            d["stdout_tail"] = st[-900:] if st else None
            d["fix_it"] = (
                "Python DDS: nessun campione PubServoInfo su ``current_servo_angle`` / ``rt/current_servo_angle``. "
                "Stesso ``GO2_DDS_DOMAIN`` e ``GO2_DDS_INTERFACE`` di Sport (eth0)."
            )
            return None, d
        except subprocess.TimeoutExpired as exc:
            d["reason"] = "PYTHON_READER_TIMEOUT"
            if exc.stdout:
                d["stdout_tail"] = str(exc.stdout)[-500:]
            if exc.stderr:
                d["stderr_tail"] = str(exc.stderr)[-500:]
            return None, d
        except Exception as exc:
            d["reason"] = "PYTHON_SUBPROCESS_FAILED"
            d["error"] = repr(exc)
            return None, d

    pref = os.environ.get("D1_SERVO_FEEDBACK_BACKEND", "auto").strip().lower()
    h_ok = helper.is_file() and os.access(helper, os.X_OK)
    if pref in ("python", "py"):
        order = ["python"]
    elif pref in ("cpp", "binary", "helper", "c++"):
        order = ["cpp"] if h_ok else ["python"]
    else:
        order = ["cpp", "python"] if h_ok else ["python"]

    last: dict[str, Any] = dict(base)
    last["backends_tried"] = list(order)
    for kind in order:
        if kind == "cpp":
            angles, diag = _run_cpp()
        else:
            angles, diag = _run_python()
        last.update(diag)
        if angles is not None:
            diag["backends_tried"] = list(order)
            return angles, diag
    last.setdefault("reason", "ALL_BACKENDS_FAILED")
    last["fix_it"] = (
        "Né C++ né Python hanno ricevuto PubServoInfo. Verifica ``GO2_DDS_INTERFACE`` (es. eth0), dominio DDS, "
        "servizio braccio Unitree attivo."
    )
    return None, last


def _read_d1_servo_angles_with_diag() -> tuple[list[float] | None, dict[str, Any]]:
    """Wrapper con cache TTL + un solo refresh in volo (Coordinamento tra richieste HTTP parallele)."""
    pos_ttl = float(os.environ.get("D1_SERVO_FEEDBACK_CACHE_TTL_S", "0.25"))
    neg_ttl = float(os.environ.get("D1_SERVO_FEEDBACK_NEGATIVE_CACHE_S", "2.0"))
    bypass = os.environ.get("D1_SERVO_FEEDBACK_BYPASS_CACHE", "").strip().lower() in {"1", "true", "yes"}

    def _hit_positive(now: float) -> tuple[list[float], dict[str, Any]] | None:
        ang = _D1_SERVO_FB_STATE.get("angles")
        t0 = float(_D1_SERVO_FB_STATE.get("mono") or 0.0)
        if isinstance(ang, list) and len(ang) >= 6 and (now - t0) < pos_ttl:
            d = dict(_D1_SERVO_FB_STATE.get("diag") or {})
            d["cache_hit"] = True
            d["cache_age_s"] = round(now - t0, 4)
            return list(ang), d
        return None

    def _hit_negative(now: float) -> tuple[list[float] | None, dict[str, Any]] | None:
        fm = float(_D1_SERVO_FB_STATE.get("fail_mono") or 0.0)
        fd = _D1_SERVO_FB_STATE.get("fail_diag")
        if neg_ttl > 0 and isinstance(fd, dict) and fd and (now - fm) < neg_ttl:
            d = dict(fd)
            d["cache_hit"] = True
            d["negative_cache"] = True
            d["cache_age_s"] = round(now - fm, 4)
            return None, d
        return None

    if not bypass:
        with _D1_SERVO_FB_CV:
            now = time.monotonic()
            if pos_ttl > 0:
                hp = _hit_positive(now)
                if hp is not None:
                    return hp
            if neg_ttl > 0:
                hn = _hit_negative(now)
                if hn is not None:
                    return hn

    with _D1_SERVO_FB_CV:
        while bool(_D1_SERVO_FB_STATE.get("refreshing")):
            _D1_SERVO_FB_CV.wait(timeout=35.0)
        now2 = time.monotonic()
        if not bypass:
            if pos_ttl > 0:
                hp2 = _hit_positive(now2)
                if hp2 is not None:
                    return hp2
            if neg_ttl > 0:
                hn2 = _hit_negative(now2)
                if hn2 is not None:
                    return hn2
        _D1_SERVO_FB_STATE["refreshing"] = True

    angles: list[float] | None = None
    diag: dict[str, Any] = {}
    try:
        angles, diag = _read_d1_servo_angles_uncached()
    except BaseException:
        with _D1_SERVO_FB_CV:
            _D1_SERVO_FB_STATE["refreshing"] = False
            _D1_SERVO_FB_CV.notify_all()
        raise
    with _D1_SERVO_FB_CV:
        _D1_SERVO_FB_STATE["refreshing"] = False
        if angles is not None:
            _D1_SERVO_FB_STATE["angles"] = list(angles)
            _D1_SERVO_FB_STATE["diag"] = dict(diag)
            _D1_SERVO_FB_STATE["mono"] = time.monotonic()
        else:
            _D1_SERVO_FB_STATE["fail_diag"] = dict(diag)
            _D1_SERVO_FB_STATE["fail_mono"] = time.monotonic()
        _D1_SERVO_FB_CV.notify_all()

    return angles, diag


def _read_d1_servo_angles() -> list[float] | None:
    angles, _diag = _read_d1_servo_angles_with_diag()
    return angles


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


def _grasp_prelude_fast_align() -> bool:
    return os.environ.get("GO2_GRASP_FAST_START_ALIGN", "1").lower() in {"1", "true", "yes"}


def _grasp_start_align_max_step_deg() -> list[float]:
    """Passi servo più grandi durante grasp→START (meno punti interpolati, meno «cede» tra i chunk)."""
    base = D1_START_ALIGN_MAX_STEP_DEG
    if not _grasp_prelude_fast_align():
        return base
    fast = _parse_step_deg_list(
        os.environ.get("D1_GRASP_START_ALIGN_MAX_STEP_DEG"),
        [4.5, 2.4, 2.2, 3.2, 4.5, 4.8, 8.0],
    )
    return [max(float(a), float(b)) for a, b in zip(fast, base)]


def _grasp_fold_max_step_deg() -> list[float]:
    """Solo sequenza grasp (fold): passi più grandi se ``GO2_GRASP_FAST_START_ALIGN``."""
    base = D1_FOLD_MAX_STEP_DEG
    if not _grasp_prelude_fast_align():
        return base
    fast = _parse_step_deg_list(
        os.environ.get("D1_GRASP_FOLD_MAX_STEP_DEG"),
        [4.2, 2.2, 2.0, 2.8, 4.2, 4.5, 7.5],
    )
    return [max(float(a), float(b)) for a, b in zip(fast, base)]


def _goto_saved_start_arm_pose(*, ignore_disable_env: bool = False, prelude_for_grasp: bool = False) -> dict[str, Any]:
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
    _arm_event(
        "goto_saved_start_begin",
        "Ritorno a START richiesto",
        ignore_disable_env=ignore_disable_env,
        prelude_for_grasp=prelude_for_grasp,
        saved_feedback_ok=bool(arm.get("feedback_ok")),
        saved_joints_deg=[round(math.degrees(float(v)), 3) for v in jr[:6]],
    )
    prehold = None
    do_prehold = os.environ.get("D1_START_PREHOLD", "1").lower() in {"1", "true", "yes"}
    if prelude_for_grasp and _front_first_grasp_flow_enabled():
        do_prehold = False
    if do_prehold:
        rep = int(os.environ.get("D1_START_PREHOLD_REPEATS", "10"))
        if prelude_for_grasp and _grasp_prelude_fast_align():
            cap = int(os.environ.get("GO2_GRASP_START_PREHOLD_CAP", "6"))
            rep = min(rep, max(3, cap))
        prehold = publish_d1_hold_current(
            repeats=rep,
            delay_ms=int(os.environ.get("D1_START_PREHOLD_DELAY_MS", "55")),
        )
    try:
        delay_ms = int(os.environ.get("D1_START_ALIGN_DELAY_MS", str(D1_SEARCH_COMMAND_DELAY_MS)))
        max_steps = _grasp_start_align_max_step_deg() if prelude_for_grasp else D1_START_ALIGN_MAX_STEP_DEG
        messages, sent = _stage_messages(stages, close_gripper=False, max_step_deg=max_steps)
        result = _run_d1_messages(messages, delay_ms=max(120, delay_ms), post_hold=True)
        _arm_event(
            "goto_saved_start_done",
            "START raggiunto",
            ok=bool(result.get("ok")),
            sent_stages=sent,
            helper_returncode=result.get("helper_returncode"),
            prehold_ok=bool((prehold or {}).get("ok")) if isinstance(prehold, dict) else None,
        )
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
    _arm_event(
        "goto_fold_begin",
        "Fold braccio richiesto",
        target_joints_deg=[round(math.degrees(v), 3) for v in jr],
    )
    try:
        delay_ms = int(os.environ.get("D1_FOLD_DELAY_MS", str(D1_SEARCH_COMMAND_DELAY_MS)))
        messages, sent = _stage_messages(stages, close_gripper=False, max_step_deg=_grasp_fold_max_step_deg())
        result = _run_d1_messages(messages, delay_ms=max(120, delay_ms), post_hold=True)
        _arm_event(
            "goto_fold_done",
            "Fold eseguito",
            ok=bool(result.get("ok")),
            sent_stages=sent,
            helper_returncode=result.get("helper_returncode"),
        )
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
    _grasp_live_phase(
        f"Attesa primo AprilTag (max {int(wait_s)}s) — mostra «Camere & AprilTag»…",
        progress_step=4,
        tag_wait_total_s=int(wait_s),
    )
    next_phase_ping = t_wait_start
    next_hold_ping = t_wait_start + 1.0
    while time.time() < deadline:
        if ARM_GRASP_ABORT.is_set():
            return {"ok": False, "reason": "aborted_while_waiting_tags", "last_plan": last}
        now = time.time()
        if now >= next_phase_ping:
            next_phase_ping = now + 2.8
            elapsed = int(now - t_wait_start)
            _grasp_live_phase(
                f"Attesa AprilTag… {elapsed}s / {int(wait_s)}s (polso o RealSense)",
                progress_step=4,
                tag_wait_elapsed_s=elapsed,
                tag_wait_total_s=int(wait_s),
            )
        if now >= next_hold_ping:
            next_hold_ping = now + 1.8
            _arm_hold_keepalive("attesa AprilTag, nessun movimento richiesto")
        plan = _box_plan_snapshot()
        if _plan_has_apriltag_detection(plan):
            _grasp_live_phase("AprilTag rilevato — proseguo con ricerca/IK…", progress_step=4)
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
        _arm_event(
            "stage_plan",
            "Pianifico stage D1",
            stage=stage.get("stage"),
            stage_index=offset,
            stages_total=len(stages),
            current_deg=current,
            target_deg=target,
            path_points=len(path),
            close_gripper=close_gripper,
            rehome=rehome,
            use_stable_start=use_stable,
            use_median_start=use_median_start,
            use_median_rehome=use_median_rehome,
            ease_profile=ease_profile,
        )
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
        d
        for t in tags
        if int(t.get("id", -1)) in _BOX_TAG_IDS_HINT
        for d in (_scalar_diagonal_px(t.get("diagonal_px")),)
        if d is not None
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
    _arm_event(
        "search_prepare",
        "Pianifico ricerca frontale",
        cycle=cycle,
        candidate=_candidate_debug_summary(front_plan),
        scan_hints=hints,
        current_deg=[round(float(v), 3) for v in current_deg[:7]],
        stage_count=len(stages),
    )
    try:
        sdelay = _effective_search_delay_ms()
        messages, sent = _stage_messages(stages, close_gripper=False, max_step_deg=D1_MAX_STEP_DEG_SEARCH)
        result = _run_d1_messages(messages, delay_ms=sdelay)
        _arm_event(
            "search_done",
            "Ricerca frontale completata",
            cycle=cycle,
            ok=bool(result.get("ok")),
            sent_stages=sent,
            helper_returncode=result.get("helper_returncode"),
        )
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
    assessment = candidate_grasp_assessment(selected if isinstance(selected, dict) else {})
    if not (selected.get("absolute_ik_safe", True)):
        return {
            "ok": False,
            "attempted_motion": False,
            "reason": "selected_camera_absolute_ik_not_safe",
            "hint": "La camera polso vede il target in frame polso: usa visual-servo o frontale/calibrazione, non IK assoluta base.",
        }
    if not assessment.get("execution_allowed"):
        return {
            "ok": False,
            "attempted_motion": False,
            "reason": "selected_candidate_not_allowed_for_execute",
            "grasp_assessment": assessment,
            "hint": "Il candidato corrente è preview euristica/non validato 3D. Servono tag box o depth/pose valida, salvo override GO2_GRASP_ALLOW_HEURISTIC_EXECUTE=1.",
        }
    if not plan_payload.get("ok") or not preview.get("ok") or not stages:
        return {"ok": False, "attempted_motion": False, "reason": "No valid IK plan to execute"}

    _arm_event(
        "plan_prepare",
        "Piano IK pronto da eseguire",
        selected_camera=plan_payload.get("selected_camera"),
        selected=_candidate_debug_summary(selected),
        preview_ok=bool(preview.get("ok")),
        preview_plan_len=len(stages),
        target=selected.get("target"),
    )
    _grasp_live_phase("Esecuzione piano IK sul braccio (comandi DDS multi-step)…", progress_step=7)
    try:
        pdelay = _effective_plan_delay_ms()
        messages, sent = _stage_messages(stages, close_gripper=True, max_step_deg=D1_MAX_STEP_DEG_GRASP)
        result = _run_d1_messages(messages, delay_ms=pdelay, post_hold=True)
        _arm_event(
            "plan_done",
            "Piano IK eseguito",
            selected_camera=plan_payload.get("selected_camera"),
            ok=bool(result.get("ok")),
            sent_stages=sent,
            helper_returncode=result.get("helper_returncode"),
        )
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


def _scalar_diagonal_px(val: Any) -> float | None:
    """AprilTag payload expected scalar; tolerate list/tuple (e.g. legacy [du, dv])."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, (list, tuple)):
        if len(val) == 0:
            return None
        if len(val) >= 2 and all(isinstance(x, (int, float)) for x in val[:2]):
            return float(math.hypot(float(val[0]), float(val[1])))
        try:
            return float(val[0])
        except (TypeError, ValueError):
            return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _max_box_diagonal_px_wrist(wrist_plan: dict[str, Any]) -> float | None:
    tags = (wrist_plan.get("tags") or {}).get("tags") or []
    diags: list[float] = []
    for t in tags:
        if int(t.get("id", -1)) not in BOX_TAG_IDS_IK:
            continue
        d = _scalar_diagonal_px(t.get("diagonal_px"))
        if d is not None:
            diags.append(d)
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
        d
        for t in tags
        if int(t.get("id", -1)) in BOX_TAG_IDS_IK
        for d in (_scalar_diagonal_px(t.get("diagonal_px")),)
        if d is not None
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
    _arm_event(
        "wrist_center_prepare",
        "Micro-centering polso",
        candidate=_candidate_debug_summary(wrist_plan),
        frame_hw=fh,
        center_hints=hints,
        current_deg=[round(float(v), 3) for v in current_deg[:7]],
        stage_count=len(stages),
    )
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
        _arm_event(
            "wrist_center_done",
            "Micro-centering polso completato",
            ok=bool(result.get("ok")),
            attempted_motion=bool(result.get("ok")),
            sent_stages=sent,
            helper_returncode=result.get("helper_returncode"),
        )
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


def _wait_for_front_camera_plan(wait_s: float | None = None) -> dict[str, Any]:
    """Attende un piano utilizzabile dalla camera frontale (logical 6)."""
    if wait_s is None:
        wait_s = _tune_float("front_plan_wait_s", "GO2_GRASP_FRONT_PLAN_WAIT_S", 15.0)
    deadline = time.time() + wait_s
    last = _box_plan_snapshot()
    t_wait_start = time.time()
    next_phase_ping = t_wait_start
    next_hold_ping = t_wait_start + 1.0
    while time.time() < deadline:
        if ARM_GRASP_ABORT.is_set():
            return {"ok": False, "reason": "aborted_while_waiting_front_camera_plan", "last_plan": last}
        now = time.time()
        if now >= next_phase_ping:
            next_phase_ping = now + 2.6
            elapsed = int(now - t_wait_start)
            rem = max(0.0, deadline - now)
            _grasp_live_phase(
                "Camera frontale Intelsense: attesa oggetto…",
                progress_step=2,
                front_wait_elapsed_s=elapsed,
                front_wait_remaining_s=round(rem, 1),
                front_wait_total_s=round(float(wait_s), 1),
            )
        if now >= next_hold_ping:
            next_hold_ping = now + 1.8
            _arm_hold_keepalive("attesa oggetto su camera frontale")
        plan = _box_plan_snapshot()
        front = _camera_candidate(plan, 6)
        if front.get("ok"):
            _grasp_live_phase(
                "Camera frontale vede l'oggetto — continuo con START fisso…",
                progress_step=2,
                front_wait_elapsed_s=round(time.time() - t_wait_start, 2),
            )
            return {"ok": True, "wait_s_elapsed": round(time.time() - t_wait_start, 2), "plan_snapshot": plan, "front_plan": front}
        last = plan
        if not _sleep_abortable(0.25):
            return {"ok": False, "reason": "aborted_while_waiting_front_camera_plan", "last_plan": last}
    return {"ok": False, "reason": "front_camera_plan_timeout", "wait_s": wait_s, "last_plan": last}


def _front_first_grasp_flow_enabled() -> bool:
    return os.environ.get("GO2_GRASP_FRONT_FIRST_FLOW", "1").lower() in {"1", "true", "yes"}


def _grasp_front_first_sequence_steps() -> list[str]:
    return ["front_camera_detect_object", "goto_saved_start", "grasp_from_start", "return_to_start"]


def _run_front_first_grasp_loop(max_cycles: int | None = None) -> dict[str, Any]:
    """Flusso richiesto: front camera → START → grasp → ritorno a START."""
    del max_cycles  # compatibilità firma: il flusso è guidato da timeout camera, non da cicli di search.
    alignment_prelude: dict[str, Any] = {}
    front_first_flow = _front_first_grasp_flow_enabled()
    if not front_first_flow and os.environ.get("GO2_GRASP_ENTRY_HOLD", "1").lower() in {"1", "true", "yes"}:
        if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"}:
            _grasp_live_phase("Hold iniziale sulla posa corrente (anti-cedimento)…", progress_step=1)
            er = int(os.environ.get("GO2_GRASP_ENTRY_HOLD_REPEATS", "20"))
            ed = int(os.environ.get("GO2_GRASP_ENTRY_HOLD_DELAY_MS", "90"))
            alignment_prelude["entry_hold"] = publish_d1_hold_current(repeats=max(8, er), delay_ms=max(45, ed))
    _grasp_live_phase("Attesa oggetto sulla camera frontale…", progress_step=2)
    front_wait = _wait_for_front_camera_plan()
    alignment_prelude["front_camera_wait_before_start"] = front_wait
    _arm_event(
        "front_first_wait_before_start",
        "Attesa camera frontale prima di START",
        wait_ok=bool(front_wait.get("ok")),
        reason=front_wait.get("reason"),
        last_plan_ok=bool((front_wait.get("last_plan") or {}).get("ok")) if isinstance(front_wait.get("last_plan"), dict) else None,
    )
    if not front_wait.get("ok") and not front_wait.get("skipped"):
        return {
            "ok": False,
            "attempted_motion": bool((alignment_prelude.get("entry_hold") or {}).get("attempted_motion")),
            "grasp_policy": "front_camera_wait_failed",
            "reason": str(front_wait.get("reason", "front camera wait failed")),
            "alignment_prelude": alignment_prelude,
            "cycles": [],
            "final_plan": front_wait.get("last_plan") or _box_plan_snapshot(),
            "dry_run_plan": {},
        }

    _grasp_live_phase("START fisso — riallineo il braccio sulla posa salvata…", progress_step=3)
    start_raw = _goto_saved_start_arm_pose(ignore_disable_env=True, prelude_for_grasp=True)
    alignment_prelude["goto_saved_start"] = start_raw
    _arm_event(
        "front_first_start_pose",
        "Riallineamento su START completato",
        ok=bool(start_raw.get("ok")),
        skipped=bool(start_raw.get("skipped")),
        reason=start_raw.get("reason"),
        sent_stages=start_raw.get("sent_stages"),
    )
    if not start_raw.get("ok") and not start_raw.get("skipped"):
        return {
            "ok": False,
            "attempted_motion": bool((alignment_prelude.get("entry_hold") or {}).get("attempted_motion")),
            "grasp_policy": "saved_start_align_failed",
            "reason": str(start_raw.get("reason", "goto_saved_start failed")),
            "alignment_prelude": alignment_prelude,
            "cycles": [],
            "final_plan": _box_plan_snapshot(),
            "dry_run_plan": {},
        }

    _grasp_live_phase("Da START: ricontrollo la camera frontale prima della presa…", progress_step=4)
    post_start_wait = _wait_for_front_camera_plan(
        _tune_float("front_plan_after_start_wait_s", "GO2_GRASP_FRONT_PLAN_AFTER_START_WAIT_S", 6.0)
    )
    alignment_prelude["front_camera_wait_after_start"] = post_start_wait
    _arm_event(
        "front_first_wait_after_start",
        "Attesa camera frontale dopo START",
        wait_ok=bool(post_start_wait.get("ok")),
        reason=post_start_wait.get("reason"),
        last_plan_ok=bool((post_start_wait.get("last_plan") or {}).get("ok")) if isinstance(post_start_wait.get("last_plan"), dict) else None,
    )
    if not post_start_wait.get("ok") and not post_start_wait.get("skipped"):
        return {
            "ok": False,
            "attempted_motion": bool((alignment_prelude.get("entry_hold") or {}).get("attempted_motion")),
            "grasp_policy": "front_camera_wait_after_start_failed",
            "reason": str(post_start_wait.get("reason", "front camera wait after start failed")),
            "alignment_prelude": alignment_prelude,
            "cycles": [],
            "final_plan": post_start_wait.get("last_plan") or _box_plan_snapshot(),
            "dry_run_plan": {},
        }

    final_plan = post_start_wait.get("plan_snapshot") or _box_plan_snapshot()
    front_plan = _camera_candidate(final_plan, 6)
    if not front_plan.get("ok"):
        return {
            "ok": False,
            "attempted_motion": False,
            "grasp_policy": "front_camera_plan_missing_after_start",
            "reason": "front_camera_plan_missing_after_start",
            "alignment_prelude": alignment_prelude,
            "cycles": [],
            "final_plan": final_plan,
            "dry_run_plan": front_wait.get("plan_snapshot") or {},
        }

    if os.environ.get("GO2_GRASP_HOLD_KEEPALIVE", "1").lower() in {"1", "true", "yes"} and os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"}:
        alignment_prelude["pre_grasp_hold"] = publish_d1_hold_current(
            repeats=max(3, int(os.environ.get("GO2_GRASP_PRE_EXECUTE_HOLD_REPEATS", "6"))),
            delay_ms=max(40, int(os.environ.get("GO2_GRASP_PRE_EXECUTE_HOLD_DELAY_MS", "55"))),
        )
    _grasp_live_phase("Presa da START con orientamento camera frontale…", progress_step=5)
    execution = publish_d1_arm_plan({"ok": True, "selected_camera": 6, "selected": front_plan})
    _arm_event(
        "front_first_execute",
        "Eseguo presa da START",
        execution_ok=bool(execution.get("ok")),
        attempted_motion=bool(execution.get("attempted_motion")),
        sent_stages=execution.get("sent_stages"),
        selected_camera=6,
        candidate=_candidate_debug_summary(front_plan),
    )
    if os.environ.get("GO2_GRASP_HOLD_KEEPALIVE", "1").lower() in {"1", "true", "yes"} and os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"}:
        alignment_prelude["post_grasp_hold"] = publish_d1_hold_current(
            repeats=max(3, int(os.environ.get("GO2_GRASP_POST_EXECUTE_HOLD_REPEATS", "6"))),
            delay_ms=max(40, int(os.environ.get("GO2_GRASP_POST_EXECUTE_HOLD_DELAY_MS", "55"))),
        )
    _grasp_live_phase("Ritorno a START con pinza chiusa…", progress_step=6)
    return_to_start = _goto_saved_start_arm_pose(ignore_disable_env=True, prelude_for_grasp=True)
    _arm_event(
        "front_first_return_start",
        "Ritorno a START dopo presa",
        ok=bool(return_to_start.get("ok")),
        skipped=bool(return_to_start.get("skipped")),
        reason=return_to_start.get("reason"),
        sent_stages=return_to_start.get("sent_stages"),
    )
    ok = bool(execution.get("ok")) and (bool(return_to_start.get("ok")) or bool(return_to_start.get("skipped")))
    attempted_motion = bool(execution.get("attempted_motion")) or bool(return_to_start.get("attempted_motion"))
    return {
        **execution,
        "ok": ok,
        "attempted_motion": attempted_motion,
        "grasp_policy": "front_camera_detect_start_grasp_return_start",
        "cycles": [],
        "final_plan": final_plan,
        "dry_run_plan": front_wait.get("plan_snapshot") or {},
        "alignment_prelude": alignment_prelude,
        "return_to_start": return_to_start,
        "front_first_flow": True,
        "preferred_execution_flow": _grasp_front_first_sequence_steps(),
    }


def _wait_for_visible_plan(wait_s: float | None = None) -> dict[str, Any]:
    if wait_s is None:
        wait_s = _tune_float("visible_plan_wait_s", "GO2_GRASP_VISIBLE_PLAN_WAIT_S", 15.0)
    deadline = time.time() + wait_s
    last = _box_plan_snapshot()
    next_hold_ping = time.time() + 1.2
    next_ui_ping = time.time()
    while time.time() < deadline:
        if ARM_GRASP_ABORT.is_set():
            return last
        now = time.time()
        if now >= next_ui_ping:
            next_ui_ping = now + 2.4
            rem = max(0.0, deadline - now)
            _grasp_live_phase(
                f"Piano con grip/telecamere — attesa… ~{int(rem)}s",
                progress_step=5,
                plan_wait_remaining_s=round(rem, 1),
                plan_wait_total_s=round(float(wait_s), 1),
            )
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
    # Default: camera frontale → START salvata → presa da START → ritorno a START.
    # Fallback legacy: fold → START → ricerca polso / piano fuso → IK.
    front_first = _front_first_grasp_flow_enabled()
    _arm_event(
        "grasp_loop_start",
        "Avvio grasp loop",
        flow="front_first" if front_first else "legacy",
        max_cycles=max_cycles,
        entry_hold=os.environ.get("GO2_GRASP_ENTRY_HOLD", "1"),
    )
    if front_first:
        return _run_front_first_grasp_loop(max_cycles)
    mc = (
        int(max_cycles)
        if max_cycles is not None
        else _tune_int("search_max_cycles", "D1_SEARCH_MAX_CYCLES", D1_SEARCH_MAX_CYCLES)
    )
    if os.environ.get("GO2_GRASP_ENTRY_HOLD", "1").lower() in {"1", "true", "yes"}:
        if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"}:
            _grasp_live_phase("Hold sulla posa corrente (anti-cedimento prima di fold/START)…", progress_step=1)
            er = int(os.environ.get("GO2_GRASP_ENTRY_HOLD_REPEATS", "20"))
            ed = int(os.environ.get("GO2_GRASP_ENTRY_HOLD_DELAY_MS", "90"))
            publish_d1_hold_current(repeats=max(8, er), delay_ms=max(45, ed))
    _grasp_live_phase("Fold braccio — posizione compatta", progress_step=2)
    fold_raw = _goto_fold_arm_pose()
    _grasp_live_phase("Riallineamento braccio alla posa START salvata…", progress_step=3)
    prelude_raw = _goto_saved_start_arm_pose(prelude_for_grasp=True)
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
    _grasp_live_phase("Allineamento vista — attesa tag/plan utilizzabile sulle camere…", progress_step=5)
    first_plan = _wait_for_visible_plan()
    last_front_plan = _camera_candidate(first_plan, 6) if _camera_candidate(first_plan, 6).get("ok") else None
    fused_confirm_count = 0
    fused_confirm_need = max(1, min(int(os.environ.get("GO2_GRASP_FUSED_CONFIRM_FRAMES", "2")), 6))
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

        cn_raw = center_hints.get("norm")
        live_cn: float | None
        if cn_raw is None:
            live_cn = None
        elif isinstance(cn_raw, (list, tuple)) and len(cn_raw) >= 2:
            live_cn = round(math.hypot(float(cn_raw[0]), float(cn_raw[1])), 4)
        else:
            live_cn = round(float(cn_raw), 4)

        _arm_event(
            "cycle_snapshot",
            "Snapshot ciclo grasp",
            cycle=cycle + 1,
            max_cycles=mc,
            wrist=_candidate_debug_summary(wrist_plan),
            front=_candidate_debug_summary(front_plan),
            grip_visible=bool(grip_vis),
            box_tags_visible=bool(box_vis),
            center_norm=live_cn,
            diagonal_px=round(float(md_px), 1) if md_px is not None else None,
            loss_streak=int(loss_streak),
            policy=wrist_policy,
            fused_env=fused_env,
            cached_wrist_ik_ok=last_valid_execute is not None,
            selected_camera=plan.get("selected_camera"),
        )
        _grasp_live_phase(
            f"Avvicinamento / ricerca — ciclo {cycle + 1} di {mc} (muovo polso verso tag)",
            progress_step=6,
            cycle=cycle + 1,
            max_cycles=mc,
            live_grip_visible=bool(grip_vis),
            live_wrist_box_tags=bool(box_vis),
            live_diagonal_px=round(float(md_px), 1) if md_px is not None else None,
            live_loss_streak=int(loss_streak),
            live_center_norm=live_cn,
        )

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
                _grasp_live_phase("Tag box molto vicino (diagonale) — eseguo IK da ultimo piano valido", progress_step=7)
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
                _grasp_live_phase("Punto presa perso dal polso — eseguo IK (ultimo piano valido)", progress_step=7)
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
                    _grasp_live_phase("Lock AprilTag sul polso confermato — eseguo piano IK (approccio / presa)", progress_step=7)
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
            if fused_confirm_count >= fused_confirm_need:
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
                        f"IK dal piano fuso (camera {sc}) — variabile GO2_GRASP_USE_FUSED_PLAN_IK=1 attiva (lock polso non richiesto).",
                        progress_step=7,
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
    fp_assessment = candidate_grasp_assessment(fp) if isinstance(fp, dict) else {}
    if fallback_ok and fp and fp.get("ok") and fp_assessment.get("execution_allowed"):
        if ARM_GRASP_ABORT.is_set():
            return _grasp_abort_return(
                log=log,
                first_plan=first_plan,
                attempted_motion=_attempted_from_log(),
                alignment_prelude=alignment_prelude,
            )
        _grasp_live_phase("Fallback presa da RealSense (camera 6) — esecuzione IK", progress_step=7)
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

    if fallback_ok and fp and fp.get("ok") and not fp_assessment.get("execution_allowed"):
        return {
            "ok": False,
            "attempted_motion": _attempted_from_log(),
            "grasp_policy": "front_camera_fallback_blocked_not_validated_3d",
            "reason": "front_camera_fallback_not_validated_3d_candidate",
            "grasp_assessment": fp_assessment,
            "cycles": log,
            "final_plan": final_plan,
            "dry_run_plan": first_plan,
            "alignment_prelude": alignment_prelude,
        }

    return {
        "ok": False,
        "attempted_motion": _attempted_from_log(),
        "grasp_policy": "continuous_wrist_search_no_lock",
        "reason": "Search cycles completed; wrist camera (logical 0) never locked. Set GO2_FRONT_CAMERA_FALLBACK_GRASP=1 for RealSense-only grasp (risky).",
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
        _arm_event(
            "preflight_snapshot",
            "Snapshot preflight grasp",
            sequence_start_ready=preflight.get("sequence_start_ready"),
            block_reason=preflight.get("sequence_start_block_reason"),
            front_first_flow_enabled=preflight.get("front_first_flow_enabled"),
            selected_camera=preflight.get("selected_camera"),
            fusion_ready_for_execute=preflight.get("fusion_ready_for_execute"),
            wrist_sees_box_tags=preflight.get("wrist_sees_box_tags"),
            wrist_preview_ok=preflight.get("wrist_preview_ok"),
            candidates={
                "0": _candidate_debug_summary((preflight.get("candidates") or {}).get("0")),
                "6": _candidate_debug_summary((preflight.get("candidates") or {}).get("6")),
            },
        )
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
        iface = GO2_DDS_INTERFACE.strip() if GO2_DDS_INTERFACE else None
        timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "45"))

        def _crouch_call() -> Any:
            if _sport_stand_modes_use_subprocess():
                return _sport_accompany_subprocess(
                    mode="crouch",
                    enable=True,
                    stand_up_first=False,
                    speed_level=None,
                )
            sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
            from go2_accompany import sport_accompany

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


def _json_safe_for_status(obj: Any) -> Any:
    """Copia ricorsiva serializzabile JSON: no NaN/Inf, numpy → Python nativi."""
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, int) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {str(k): _json_safe_for_status(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe_for_status(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    try:
        import numpy as np  # type: ignore

        if isinstance(obj, np.generic):
            return _json_safe_for_status(obj.item())
        if isinstance(obj, np.ndarray):
            return _json_safe_for_status(obj.tolist())
    except Exception:
        pass
    if hasattr(obj, "tolist") and callable(obj.tolist):
        try:
            return _json_safe_for_status(obj.tolist())
        except Exception:
            pass
    if hasattr(obj, "item") and callable(obj.item):
        try:
            return _json_safe_for_status(obj.item())
        except Exception:
            pass
    return str(obj)


def get_status() -> dict[str, Any]:
    with STATUS_LOCK:
        safe = _json_safe_for_status(STATUS)
        return json.loads(json.dumps(safe))


@APP.route("/api/status")
def api_status() -> Any:
    try:
        return jsonify(get_status())
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": repr(exc),
                "updated_at": now_iso(),
                "running": False,
                "summary": "Stato diagnostico non serializzabile (bug interno); premi «Run All Tests».",
                "tests": {},
            }
        )


def frame_from_camera(device: int) -> Any | None:
    if cv2 is None:
        return None
    jpg = robot_camera_jpeg(device)
    if jpg is None:
        return None
    import numpy as np

    arr = np.frombuffer(jpg, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _camera_jpeg_for_mjpeg(device: int, last_cam_jpg: bytes | None) -> bytes | None:
    """
    Stessa logica di ``/stream/robot/camera/<n>.mjpg``: peek cache, al primo frame ``get_jpeg`` con attesa,
    poi riuso dell’ultimo JPEG camera se la peek è vuota (evita buchi neri tra i frame).
    """
    if device not in CAMERA_DEVICES:
        return None
    first_wait_s = float(os.environ.get("GO2_MJPEG_FIRST_FRAME_WAIT_S", "1.8"))
    if GO2_LOCAL and cv2 is not None:
        CAMERA_CACHE.start(device)
        jpg = CAMERA_CACHE.peek_jpeg(device)
        if jpg is None and last_cam_jpg is None:
            jpg = CAMERA_CACHE.get_jpeg(device, wait_s=first_wait_s)
        elif jpg is None:
            jpg = last_cam_jpg
    else:
        jpg = robot_camera_jpeg(device)
        if jpg is None:
            jpg = last_cam_jpg
    return jpg


_TAG_DRAW_FNS: Optional[tuple[Any, Any, Any]] = None
_TAG_DRAW_IMPORT_ERROR: Optional[str] = None


def _box_planner_detect_draw() -> tuple[Any, Any, Any] | None:
    """Import lazy una tantum — evita costi e fallimenti ripetuti sul path MJPEG."""
    global _TAG_DRAW_FNS, _TAG_DRAW_IMPORT_ERROR
    if _TAG_DRAW_IMPORT_ERROR is not None:
        return None
    if _TAG_DRAW_FNS is None:
        try:
            scripts = str(PROJECT_ROOT / "scripts")
            if scripts not in sys.path:
                sys.path.insert(0, scripts)
            from box_grasp_planner import draw_grasp_overlay, plan_from_frame
            from box_object_detector import detect_box_object

            _TAG_DRAW_FNS = (plan_from_frame, detect_box_object, draw_grasp_overlay)
        except Exception as exc:
            _TAG_DRAW_IMPORT_ERROR = repr(exc)
            return None
    return _TAG_DRAW_FNS


def _encode_apriltag_overlay_frame(frame: Any, *, logical_camera_device: int | None = None) -> bytes | None:
    if cv2 is None:
        return None
    trio = _box_planner_detect_draw()
    if trio is None:
        return None
    plan_from_frame, detect_box_object, draw_grasp_overlay = trio
    try:
        obj = detect_box_object(frame)
        plan = plan_from_frame(frame, object_detection=obj, logical_camera_device=logical_camera_device)
        out = draw_grasp_overlay(frame, plan)
        aq = int(os.environ.get("GO2_ANNOTATED_JPEG_QUALITY", "72"))
        aq = max(55, min(95, aq))
        ok, jpg = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), aq])
        return jpg.tobytes() if ok else None
    except Exception:
        return None


def _jpeg_apply_apriltag_if_possible(jpg: bytes, *, logical_camera_device: int | None = None) -> bytes:
    """
    Decodifica un JPEG camera → overlay tag/detector/grip/preview → JPEG; se overlay fallisce o è disabilitato, ritorna il raw.
    Così lo stream ``tags.mjpg`` non resta mai nero mentre il MJPEG grezzo ha dati.
    """
    if cv2 is None:
        return jpg
    if _env_truthy(os.environ.get("GO2_APRILTAG_STREAM_RAW_ONLY"), default="0"):
        return jpg
    import numpy as np

    arr = np.frombuffer(jpg, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return jpg
    enc = _encode_apriltag_overlay_frame(frame, logical_camera_device=logical_camera_device)
    return enc if enc is not None else jpg


def _apriltag_overlay_jpeg_bytes(device: int) -> bytes | None:
    """Un frame annotato (o raw se overlay non disponibile) per GET ``/api/box/annotated``."""
    if device not in CAMERA_DEVICES:
        return None
    jpg = _camera_jpeg_for_mjpeg(device, None)
    if jpg is None:
        return None
    return _jpeg_apply_apriltag_if_possible(jpg, logical_camera_device=device)


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


_DASHBOARD_HTML_PATH = PROJECT_ROOT / "templates" / "dashboard.html"
_DASHBOARD_HTML_CACHE_MT: float | None = None
_DASHBOARD_HTML_CACHE_TEXT: str = ""


def _load_dashboard_html() -> str:
    """Carica il markup principale da file (evita monolite da migliaia di righe nel .py)."""
    try:
        return _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return (
            "<!doctype html><html><head><meta charset=\"utf-8\"/><title>Dashboard</title></head>"
            f"<body><pre>Template mancante o illeggibile {_DASHBOARD_HTML_PATH}:\n{exc!r}</pre></body></html>"
        )


def _get_dashboard_html() -> str:
    """Markup della dashboard: ricarica da disco se ``dashboard.html`` cambia (no riavvio Flask)."""
    global _DASHBOARD_HTML_CACHE_MT, _DASHBOARD_HTML_CACHE_TEXT
    try:
        mtime = float(_DASHBOARD_HTML_PATH.stat().st_mtime)
    except OSError:
        return _load_dashboard_html()
    if _DASHBOARD_HTML_CACHE_MT != mtime or not _DASHBOARD_HTML_CACHE_TEXT:
        _DASHBOARD_HTML_CACHE_TEXT = _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
        _DASHBOARD_HTML_CACHE_MT = mtime
    return _DASHBOARD_HTML_CACHE_TEXT


@APP.route("/favicon.ico")
def favicon() -> Response:
    """Browser requests this by default; evita 404 in console (nessuna icona dedicata)."""
    return Response(status=204)


@APP.route("/")
def index() -> Response:
    url_prefix = os.environ.get("GO2_DASHBOARD_URL_PREFIX", "").strip().rstrip("/")
    script_root = url_prefix or ((request.script_root or "").rstrip("/"))
    template_text = _get_dashboard_html()
    status_code = 200
    try:
        # ``dashboard.html`` viene riletto via mtime; svuotiamo la cache Jinja così anche gli ``include``
        # (es. ``_always_cam_strip.html``) riflettono subito le modifiche senza riavviare Flask.
        APP.jinja_env.cache.clear()
        html = render_template_string(
            template_text,
            go2_host=GO2_HOST,
            xt16_host=XT16_HOST,
            servo_arm_host=SERVO_ARM_HOST,
            dashboard_port=int(os.environ.get("GO2_DASHBOARD_PORT", "5050")),
            dashboard_bind=GO2_DASHBOARD_BIND,
            go2_local="1" if GO2_LOCAL else "0",
            script_root=script_root,
        )
    except TemplateNotFound as exc:
        status_code = 500
        html = (
            "<!doctype html><html><head><meta charset=\"utf-8\"/><title>Dashboard template missing</title></head>"
            f"<body><pre>Template include mancante o illeggibile: {exc!r}</pre></body></html>"
        )
    return Response(html, status=status_code, mimetype="text/html", headers={"Cache-Control": "no-store"})


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
        last_cam: bytes | None = None
        last_sent: bytes | None = None
        while True:
            jpg = _camera_jpeg_for_mjpeg(device, last_cam)
            if jpg is not None:
                last_cam = jpg
            if jpg is None:
                jpg = last_cam
            if jpg is None:
                if last_sent is not None:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + last_sent + b"\r\n"
                    )
                time.sleep(period)
                continue
            image = _jpeg_apply_apriltag_if_possible(jpg, logical_camera_device=device)
            last_sent = image
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
    payload: dict[str, Any] = {
        "ok": True,
        "go2_local": bool(GO2_LOCAL),
        "mode": "local-cache" if GO2_LOCAL else "ssh-snapshot",
        "cameras": CAMERA_CACHE.stats(),
    }
    if GO2_LOCAL and cv2 is not None:
        payload["v4l_index_by_logical"] = {str(d): _v4l_index_for_logical_camera(d) for d in CAMERA_DEVICES}
        auto_m = usb_auto_v4l_mapping()
        if auto_m:
            payload["v4l_usb_auto_map"] = {str(k): int(v) for k, v in sorted(auto_m.items())}
    return jsonify(payload)


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
            v4l_idx = _v4l_index_for_logical_camera(device)
            frame = frame_from_camera(device)
            per_cam_pipeline[str(device)] = {
                "logical_device": device,
                "v4l_index": v4l_idx,
                "dev_path": f"/dev/video{v4l_idx}",
                "frame_ok": frame is not None,
                "frame_shape_hw": (list(frame.shape[:2]) if frame is not None else None),
                "camera_cache": cache_row,
            }
            if frame is None:
                candidates[str(device)] = {
                    "ok": False,
                    "error": (
                        f"camera logical {device} ({CAMERA_DEVICES.get(device, 'unknown')}) "
                        f"unavailable at /dev/video{v4l_idx}"
                    ),
                    "camera_label": CAMERA_DEVICES.get(device, "unknown"),
                }
                continue
            object_det = detect_box_object(frame)
            prefer_tag = _effective_grasp_bool("prefer_tag_grip", "GO2_GRASP_PREFER_TAG_GRIP")
            result = plan_from_frame(
                frame,
                object_detection=object_det,
                prefer_tag_grip=prefer_tag,
                logical_camera_device=device,
            )
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
        assessment = plan_grasp_assessment({
            "ok": ok,
            "selected_camera": None if selected is None else int(selected_key),
            "selected": selected,
            "candidates": candidates,
        })
        detector_scope = detector_training_scope(det_status)
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
            "selected_grasp_assessment": assessment.get("selected"),
            "grasp_assessment": assessment,
            "visible_summary": visible_summary,
            "tag_calibration": tag_cal,
            "object_detector": det_status,
            "object_detector_scope": detector_scope,
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
                "ID 5 landmark above XT16 uses 60 mm default (REFERENCE_TAG_SIZE_M / LIDAR_LANDMARK_TAG_SIZE_M). "
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


# Ultimo landmark tag5 (XT-16) **grezzo** valido: evita salti quando ``scene_3d?fast=1`` salta il piano visione.
_SCENE3D_TAG5_LM_CACHE: dict[str, Any] = {"xyz": None, "mono": 0.0}
# EMA sul centro mostrato (sfera/cilindro) per attenuare jitter tra camere / frame.
_SCENE3D_TAG5_EMA: dict[str, Any] = {"xyz": None}

# Stato display-only: centro tag 5 in ``base_link`` per viewer (sfera + cilindro XT-16).
_VIEWER_XT16_TAG_BL_EMA: dict[str, Any] = {"xyz": None}


def _smooth_viewer_xt16_tag_base_link(raw: list[float] | None) -> list[float] | None:
    """EMA sul centro tag 5 in ``base_link`` per sfera/cilindro XT-16 nel viewer (meno glitch frame-to-frame)."""
    if raw is None or len(raw) < 3:
        return raw
    alpha = float(os.environ.get("GO2_VIEWER_XT16_TAG_EMA_ALPHA", "0.28"))
    prev = _VIEWER_XT16_TAG_BL_EMA.get("xyz")
    cur = [float(raw[i]) for i in range(3)]
    if not isinstance(prev, list) or len(prev) < 3:
        _VIEWER_XT16_TAG_BL_EMA["xyz"] = list(cur)
        return [round(x, 5) for x in cur]
    sm = [alpha * cur[i] + (1.0 - alpha) * float(prev[i]) for i in range(3)]
    _VIEWER_XT16_TAG_BL_EMA["xyz"] = list(sm)
    return [round(float(x), 5) for x in sm]


def _scene3d_tag5_xyz_display_smoothed(tag5_raw: list[float] | None) -> list[float] | None:
    """EMA sul landmark; se ``tag5_raw`` è None resta l'ultimo valore smussato se c'è."""
    prev = _SCENE3D_TAG5_EMA.get("xyz")
    if tag5_raw is None:
        if isinstance(prev, list) and len(prev) >= 3:
            return [float(prev[i]) for i in range(3)]
        return None
    alpha = float(os.environ.get("GO2_SCENE3D_TAG5_EMA_ALPHA", "0.38"))
    try:
        raw3 = [float(tag5_raw[i]) for i in range(3)]
    except (TypeError, ValueError, IndexError):
        return None
    if prev is None or not isinstance(prev, list) or len(prev) < 3:
        out = list(raw3)
    else:
        out = [alpha * raw3[i] + (1.0 - alpha) * float(prev[i]) for i in range(3)]
    _SCENE3D_TAG5_EMA["xyz"] = out
    return out


def _merged_apriltag_rows_from_plan(plan_blob: dict[str, Any]) -> list[dict[str, Any]]:
    """Unisce stime da camera 0 (polso) e 6 (front). L'ordine è arbitrario; ``tags_for_viewer`` deduplica per id."""
    from box_grasp_planner import apriltag_tag_estimates_base_m

    out: list[dict[str, Any]] = []
    cands = plan_blob.get("candidates")
    if not isinstance(cands, dict):
        return out
    for key in ("0", "6"):
        c = cands.get(key)
        if not isinstance(c, dict):
            continue
        pwrap = c.get("poses")
        if isinstance(pwrap, dict):
            out.extend(apriltag_tag_estimates_base_m(pwrap))
    return out


def _pick_stable_reference_tag_xyz(
    rows: list[dict[str, Any]], *, ref_id: int
) -> list[float] | None:
    """Landmark (es. tag5 su XT-16): preferisci stima da camera polso ``logical_camera_device==0``, altrimenti ``range_m`` minimo."""
    cand: list[dict[str, Any]] = []
    rid = int(ref_id)
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            if int(r.get("id", -1)) != rid:
                continue
        except (TypeError, ValueError):
            continue
        bx = r.get("base_xyz_m")
        if not isinstance(bx, list) or len(bx) < 3:
            continue
        try:
            _ = [float(bx[i]) for i in range(3)]
        except (TypeError, ValueError):
            continue
        cand.append(r)
    if not cand:
        return None
    for r in cand:
        try:
            if int(r.get("logical_camera_device", -1)) == 0:
                bx = r["base_xyz_m"]
                return [float(bx[i]) for i in range(3)]
        except (TypeError, ValueError):
            continue

    def _rng(rr: dict[str, Any]) -> float:
        try:
            return float(rr.get("range_m") or 999.0)
        except (TypeError, ValueError):
            return 999.0

    best = min(cand, key=_rng)
    bx = best["base_xyz_m"]
    return [float(bx[i]) for i in range(3)]


def _dedupe_apriltag_rows_for_viewer(
    rows: list[dict[str, Any]],
    *,
    reference_tag_id: int,
    depth_logical_cam: int = 6,
    wrist_logical_cam: int = 0,
) -> list[dict[str, Any]]:
    """Una stima per ``id`` per Three.js: scatola 0–3 preferisce RealSense (6), landmark ``reference_tag_id`` dal polso (0).

    Il piano unisce ancora tutte le righe in ``apriltag_tag_estimates_base_m``; qui evitiamo due sfere
    per lo stesso id (polso vede 5 e 0–3, fronte vede soprattutto 0–3).
    """
    from box_grasp_planner import BOX_TAG_IDS

    groups: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            tid = int(r.get("id", -1))
        except (TypeError, ValueError):
            continue
        bx = r.get("base_xyz_m")
        if not isinstance(bx, list) or len(bx) < 3:
            continue
        try:
            _ = [float(bx[i]) for i in range(3)]
        except (TypeError, ValueError):
            continue
        groups.setdefault(tid, []).append(r)

    def _rng(rr: dict[str, Any]) -> float:
        try:
            return float(rr.get("range_m") or 999.0)
        except (TypeError, ValueError):
            return 999.0

    def _idev(rr: dict[str, Any]) -> int | None:
        v = rr.get("logical_camera_device")
        if isinstance(v, (int, float)):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        return None

    ref = int(reference_tag_id)
    out: list[dict[str, Any]] = []
    for tid in sorted(groups.keys(), key=lambda x: (x < 0, x)):
        cand = groups[tid]
        chosen: dict[str, Any] | None = None
        if tid == ref:
            for r in cand:
                if _idev(r) == wrist_logical_cam:
                    chosen = r
                    break
            if chosen is None:
                chosen = min(cand, key=_rng)
        elif tid in BOX_TAG_IDS:
            for r in cand:
                if _idev(r) == depth_logical_cam:
                    chosen = r
                    break
            if chosen is None:
                for r in cand:
                    if _idev(r) == wrist_logical_cam:
                        chosen = r
                        break
            if chosen is None:
                chosen = min(cand, key=_rng)
        else:
            chosen = min(cand, key=_rng)
        if chosen is not None:
            out.append(dict(chosen))
    return out


def _arm_scene_3d_payload(*, geometry_fast: bool = False) -> dict[str, Any]:
    """JSON per vista 3D: catena FK, vis_geometry, scene_graph; IK/target da /api/box/plan se non fast.

    geometry_fast: salta api_box_plan (pesante su NX) — utile durante gli slider «Allinea vista 3D».
    """
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import numpy as np

    from arm_kinematics_d1_template import (
        ARM_FOLD_POSE,
        DEPTH_CAMERA_ARM_BASE_M,
        depth_camera_optical_axis_unit_arm_base,
        fk_chain_positions,
        fk_d1_joint_locals_m,
        fk_tool_tip,
        fk_wrist_camera_center_m,
        fk_wrist_camera_view_axis_unit_m,
        nominal_object_along_depth_optical_arm_m,
    )
    from box_grasp_planner import (
        REFERENCE_TAG_ID_LIDAR_FRAME,
        REFERENCE_TAG_SIZE_M,
        apriltag_tag_estimates_base_m,
        tag5_calibration_offset_arm_base_m,
    )

    vg = _vis_geometry_effective()
    payload: dict[str, Any] = {
        "ok": True,
        "frame": "arm_base",
        "axes_hint": {
            "x": "avanti (davanti al cane)",
            "y": "sinistra",
            "z": "su",
            "unit": "m",
            "three_js_note": "Viewer: asse Three.js Y su ≈ robot Z; mapping applicato lato client.",
        },
        "vis_geometry_effective": {k: round(float(v), 6) for k, v in vg.items()},
    }
    wrist_off: np.ndarray | None = None
    wo = (vg["wrist_local_dx"], vg["wrist_local_dy"], vg["wrist_local_dz"])
    if any(abs(wo[i]) > 1e-12 for i in range(3)):
        wrist_off = np.array(wo, dtype=float)

    q_fb: list[float] | None = None
    # FK intermedia = fold; l'unica lettura servo DDS è dopo ``api_box_plan()`` (sotto), così evitiamo due subprocess.
    q_vis = [float(x) for x in ARM_FOLD_POSE]

    _mount_nom = np.array([0.15, 0.0, 0.06], dtype=float)
    _mount_bl_list = [round(float(x), 5) for x in _mount_nom]
    _depth_vis_arm = np.asarray(DEPTH_CAMERA_ARM_BASE_M, dtype=float)

    def _ab_to_bl(p: list[float] | Any) -> list[float]:
        a = [float(np.asarray(p, dtype=float)[i]) for i in range(3)]
        return [round(a[i] + _mount_bl_list[i], 5) for i in range(3)]

    _d_ax = depth_camera_optical_axis_unit_arm_base()
    cam_sites: dict[str, Any] = {
        "mjcf_ref": "unitree_mujoco/unitree_robots/go2_d1/go2_d1_d1mesh.xml",
        "logical_6_go2_front": {
            "label": "MJCF depth_camera (base_link)",
            "pos_arm_base_m": [round(float(_depth_vis_arm[i]), 5) for i in range(3)],
            "view_axis_unit_m": [round(float(_d_ax[i]), 5) for i in range(3)],
        },
        "logical_0_wrist": None,
    }
    wc = fk_wrist_camera_center_m(q_vis, wrist_off)
    wv = fk_wrist_camera_view_axis_unit_m(q_vis, wrist_off)
    wc_mjcf = fk_wrist_camera_center_m(q_vis, None)
    wv_mjcf = fk_wrist_camera_view_axis_unit_m(q_vis, None)
    cam_sites["logical_0_wrist"] = {
        "label": "wrist_camera (FK + offset slider locale; q_vis = feedback o fold)",
        "pos_arm_base_m": [round(float(wc[i]), 5) for i in range(3)],
        "view_axis_unit_m": [round(float(wv[i]), 5) for i in range(3)],
        "mjcf_pos_arm_base_m": [round(float(wc_mjcf[i]), 5) for i in range(3)],
        "mjcf_view_axis_unit_m": [round(float(wv_mjcf[i]), 5) for i in range(3)],
    }
    payload["mujoco_camera_sites_arm_m"] = cam_sites
    plan_blob: dict[str, Any] = {}
    if geometry_fast:
        payload["geometry_fast_preview"] = True
    else:
        try:
            plan_blob = json.loads(api_box_plan().get_data(as_text=True))
        except Exception as exc:
            plan_blob = {}
            payload["plan_parse_error"] = repr(exc)
    sel = plan_blob.get("selected") if isinstance(plan_blob.get("selected"), dict) else {}
    est_rows: list[dict[str, Any]] = []
    if not geometry_fast and isinstance(plan_blob.get("candidates"), dict):
        est_rows = _merged_apriltag_rows_from_plan(plan_blob)
    if not est_rows:
        poses_blob = sel.get("poses") if isinstance(sel.get("poses"), dict) else {}
        if isinstance(poses_blob, dict):
            est_rows = apriltag_tag_estimates_base_m(poses_blob)
    payload["apriltag_tag_estimates_base_m"] = est_rows

    tag5_raw: list[float] | None = _pick_stable_reference_tag_xyz(
        est_rows, ref_id=REFERENCE_TAG_ID_LIDAR_FRAME
    )
    if tag5_raw is None and geometry_fast:
        hold_s = float(os.environ.get("GO2_SCENE3D_TAG5_STALE_HOLD_S", "12.0"))
        cached = _SCENE3D_TAG5_LM_CACHE.get("xyz")
        t0 = float(_SCENE3D_TAG5_LM_CACHE.get("mono") or 0.0)
        if isinstance(cached, list) and len(cached) >= 3 and (time.monotonic() - t0) < hold_s:
            tag5_raw = [float(cached[i]) for i in range(3)]
    if tag5_raw is not None:
        _SCENE3D_TAG5_LM_CACHE["xyz"] = [float(tag5_raw[i]) for i in range(3)]
        _SCENE3D_TAG5_LM_CACHE["mono"] = time.monotonic()

    tag5_xyz = _scene3d_tag5_xyz_display_smoothed(tag5_raw)

    markers: dict[str, Any] = {
        "tag5_estimated_m": None
        if tag5_xyz is None
        else [round(tag5_xyz[i], 5) for i in range(3)],
        "arm_mount_m": None,
        "front_camera_from_tag5_m": None,
        "mjcf_depth_camera_m": [round(float(_depth_vis_arm[i]), 5) for i in range(3)],
        "mjcf_wrist_camera_m": None,
        "wrist_camera_display_m": None,
        "object_nominal_along_mjcf_optical_arm_m": None,
        "mjcf_depth_optical_axis_unit_arm_m": [round(float(_d_ax[i]), 5) for i in range(3)],
        "note": "Mount/camera front da tag5+slider legacy; polso = FK.",
    }
    if tag5_xyz is not None:
        markers["arm_mount_m"] = [
            round(tag5_xyz[0] + vg["arm_vs_tag5_x"], 5),
            round(tag5_xyz[1] + vg["arm_vs_tag5_y"], 5),
            round(tag5_xyz[2] + vg["arm_vs_tag5_z"], 5),
        ]
        markers["front_camera_from_tag5_m"] = [
            round(tag5_xyz[0] + vg["front_vs_tag5_x"], 5),
            round(tag5_xyz[1] + vg["front_vs_tag5_y"], 5),
            round(tag5_xyz[2] + vg["front_vs_tag5_z"], 5),
        ]
    markers["mjcf_wrist_camera_m"] = [
        round(float(fk_wrist_camera_center_m(q_vis, None)[i]), 5) for i in range(3)
    ]
    markers["wrist_camera_display_m"] = [
        round(float(fk_wrist_camera_center_m(q_vis, wrist_off)[i]), 5) for i in range(3)
    ]
    _nom_obj_ab = nominal_object_along_depth_optical_arm_m()
    markers["object_nominal_along_mjcf_optical_arm_m"] = [
        round(float(_nom_obj_ab[i]), 5) for i in range(3)
    ]
    payload["vis_geometry_markers_arm_m"] = markers

    chain_mm: dict[str, Any] | None = None
    if (
        tag5_xyz is not None
        and markers.get("arm_mount_m")
        and markers.get("front_camera_from_tag5_m")
        and markers.get("tag5_estimated_m")
    ):
        t5l = markers["tag5_estimated_m"]
        aml = markers["arm_mount_m"]
        fcl = markers["front_camera_from_tag5_m"]

        def _dist3(a: list[float], b: list[float]) -> float:
            return float(
                math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)
            )

        av = (vg["arm_vs_tag5_x"], vg["arm_vs_tag5_y"], vg["arm_vs_tag5_z"])
        fv = (vg["front_vs_tag5_x"], vg["front_vs_tag5_y"], vg["front_vs_tag5_z"])
        chain_mm = {
            "tag5_to_arm_mount_mm": round(_dist3(t5l, aml) * 1000.0, 1),
            "tag5_to_front_camera_model_mm": round(_dist3(t5l, fcl) * 1000.0, 1),
            "arm_mount_to_front_camera_model_mm": round(_dist3(aml, fcl) * 1000.0, 1),
            "slider_arm_vs_norm_mm": round(
                math.sqrt(av[0] ** 2 + av[1] ** 2 + av[2] ** 2) * 1000.0, 1
            ),
            "slider_front_vs_norm_mm": round(
                math.sqrt(fv[0] ** 2 + fv[1] ** 2 + fv[2] ** 2) * 1000.0, 1
            ),
            "arm_base_origin_to_tag5_mm": round(_dist3([0.0, 0.0, 0.0], t5l) * 1000.0, 1),
            "note_it": "+X avanti: metriche mount/tag/camera da slider legacy tag5.",
        }
        mjdf = markers.get("mjcf_depth_camera_m")
        if isinstance(mjdf, list) and len(mjdf) >= 3:
            chain_mm["mjcf_depth_to_tag5_mm"] = round(_dist3(mjdf, t5l) * 1000.0, 1)
            chain_mm["mjcf_depth_to_slider_front_mm"] = round(_dist3(mjdf, fcl) * 1000.0, 1)
    payload["vis_geometry_chain_mm"] = chain_mm
    # Riepilogo calibrazione: stessi ``base_xyz_m`` del planner; offset file tag5 **solo** su id landmark (5).
    ca_vis: dict[str, Any] = {
        "planner_viewer_tag_positions_aligned": True,
        "note_it": (
            "Le posizioni tag in Three.js usano ``tags_for_viewer``: una riga per id — tag scatola 0–3 da "
            "RealSense (6) se c'è, altrimenti polso (0); landmark id 5 dal polso. ``apriltag_tag_estimates_base_m`` "
            "resta l'unione completa (debug). Offset file tag5 solo su id 5."
        ),
        "tag5_offset_file_present": TAG5_CALIB_PATH.is_file(),
        "nominal_tag5_env_configured": _nominal_tag5_arm_base_from_env() is not None,
        "tag5_calibration_enabled": os.environ.get("GO2_TAG5_CALIBRATION_ENABLE", "1"),
        "tag5_visible_in_plan": tag5_xyz is not None,
        "mjcf_depth_to_tag5_mm": None if chain_mm is None else chain_mm.get("mjcf_depth_to_tag5_mm"),
        "mjcf_depth_to_slider_front_mm": None if chain_mm is None else chain_mm.get("mjcf_depth_to_slider_front_mm"),
        "align_hint_it": (
            "Se il tag5 è visibile nel piano: riduci «MJCF depth ↔ tag5» (mm) muovendo i cursori "
            "«Camera frontale» / rotazioni frustum finché cono viola e sfera rossa sono coerenti con la telecamera reale."
            if tag5_xyz is not None
            else "Inquadra il landmark tag 5 (polso o RealSense) e aggiorna la vista 3D per vedere la metrica depth↔tag5."
        ),
        "three_js_autotune_it": (
            "Il viewer aggiorna **sfera rossa tag5**, **cilindro XT-16** e **pinza FK (magenta)** da `scene_3d` dopo "
            "calibrazione / plan: non sono slider manuali. Affina solo mount/corpo/cam con i preset geometria se la mesh "
            "STL non coincide con i giunti (spesso placeholder Empty_Link)."
        ),
    }
    payload["calibration_visual_alignment"] = ca_vis

    _nom_dep = float(os.environ.get("GO2_OBJECT_NOMINAL_DEPTH_ALONG_OPTICAL_M", "0.20"))
    _nom_vec = np.asarray(nominal_object_along_depth_optical_arm_m(_nom_dep), dtype=float)
    _cam_vec = np.asarray(_depth_vis_arm, dtype=float)
    _delta = _nom_vec - _cam_vec
    payload["nominal_object_depth_along_optical_m"] = round(_nom_dep, 5)
    payload["mjcf_depth_optical_selfcheck_mm"] = {
        "chord_depth_to_nominal_mm": round(float(np.linalg.norm(_delta) * 1000.0), 3),
        "projection_on_optical_axis_mm": round(float(np.dot(_delta, _d_ax) * 1000.0), 3),
        "expected_projection_mm": round(_nom_dep * 1000.0, 3),
    }

    silhouette_anchor: list[float] | None = None
    if tag5_xyz is not None:
        silhouette_anchor = [round(tag5_xyz[i], 5) for i in range(3)]
    else:
        nom = _nominal_tag5_arm_base_from_env()
        if nom is not None and len(nom) >= 3:
            silhouette_anchor = [round(float(nom[i]), 5) for i in range(3)]
    payload["go2_silhouette_anchor_arm_m"] = silhouette_anchor

    tgt = sel.get("target") if isinstance(sel.get("target"), dict) else {}
    raw_target: list[float] | None = None
    if tgt.get("ok") and isinstance(tgt.get("base_xyz_m"), list) and len(tgt["base_xyz_m"]) >= 3:
        b = tgt["base_xyz_m"]
        raw_target = [round(float(b[i]), 5) for i in range(3)]
        payload["object_target_base_xyz_m"] = raw_target
    disp_t = _scene3d_target_ema_update(
        raw_target,
        float(vg["target_ema_alpha"]),
        freeze_on_missing=geometry_fast,
    )
    if disp_t is not None:
        payload["object_target_base_xyz_m_display"] = disp_t
    preview = sel.get("preview") if isinstance(sel.get("preview"), dict) else {}
    if preview.get("ok") and isinstance(preview.get("plan"), list):
        traj_targets: list[list[float]] = []
        traj_tips: list[list[float]] = []
        ghost_chains: list[list[list[float]]] = []
        stages: list[str | None] = []
        for st in preview["plan"]:
            if not isinstance(st, dict):
                continue
            stages.append(str(st.get("stage") or ""))
            txyz = st.get("target_xyz_m")
            if isinstance(txyz, list) and len(txyz) >= 3:
                traj_targets.append([round(float(txyz[i]), 5) for i in range(3)])
            ftip = st.get("fk_tip_xyz_m")
            if isinstance(ftip, list) and len(ftip) >= 3:
                traj_tips.append([round(float(ftip[i]), 5) for i in range(3)])
            jr = st.get("joints_rad")
            if isinstance(jr, list) and len(jr) >= 6:
                try:
                    q = [float(jr[i]) for i in range(6)]
                    ghost_chains.append(fk_chain_positions(q))
                except Exception:
                    pass
        payload["ik_trajectory"] = {
            "targets_xyz_m": traj_targets,
            "fk_tool_xyz_m": traj_tips,
            "ghost_chains_m": ghost_chains,
            "stages": stages,
        }
    sc = plan_blob.get("selected_camera")
    if sc is not None:
        payload["selected_camera"] = sc
    tags_sel = ((sel or {}).get("tags") or {}).get("tags") or []
    tid_seen = sorted({int(t.get("id", -1)) for t in tags_sel})
    gp = (sel or {}).get("grip_point") if isinstance(sel, dict) else {}
    if not isinstance(gp, dict):
        gp = {}
    payload["vision_snapshot"] = {
        "planner_ok": bool(plan_blob.get("ok")) if not geometry_fast else False,
        "logical_camera_used": sc,
        "tag_ids_in_selected_frame": tid_seen,
        "grip_point_ok": bool(gp.get("ok")),
        "grip_source": gp.get("source"),
        "target_ok": bool(tgt.get("ok")) if isinstance(tgt, dict) else False,
        "preview_ik_ok": bool((sel.get("preview") or {}).get("ok")) if sel else False,
        "geometry_fast_preview": bool(geometry_fast),
        "hint": (
            "Anteprima geometria senza /api/box/plan (slider)."
            if geometry_fast
            else "Aggiornato con /api/box/plan (CameraCache). Nessun file log locale sul PC: questo è il riassunto percepito."
        ),
    }
    off_t5 = tag5_calibration_offset_arm_base_m()
    tag5_lm: dict[str, Any] = {
        "reference_tag_id": 5,
        "mount": "Landmark AprilTag 5 sopra XT-16.",
        "frames": "Frame base braccio = arm_link00; mount→base_link fissato a (0.15,0,0.06) dal MJCF.",
        "nominal_arm_base_m": _nominal_tag5_arm_base_from_env(),
        "offset_applied_m": off_t5,
        "d1_mesh_online_refs": [
            "https://support.unitree.com/home/en/developer/D1Arm_services",
            "https://www.unitree.com/D1-T/",
            "https://github.com/unitreerobotics/unitree_ros/issues/116",
        ],
    }
    if TAG5_CALIB_PATH.is_file():
        try:
            tag5_lm["saved"] = json.loads(TAG5_CALIB_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            tag5_lm["saved_read_error"] = True
    payload["tag5_xt16_landmark"] = tag5_lm

    _eps_x = 1e-4
    arm_link00_m = [0.0, 0.0, 0.0]
    cro: dict[str, Any] = {
        "frame_note_it": "+X avanti. Slider vista: corpo Go2, mount braccio, camera muso.",
        "arm_link00_xyz_m": arm_link00_m,
        "tag5_xyz_m": markers.get("tag5_estimated_m"),
        "front_camera_slider_xyz_m": markers.get("front_camera_from_tag5_m"),
        "object_target_xyz_m": raw_target,
        "ok_arm_behind_xt16_front_ahead_object": None,
    }
    _t5m = markers.get("tag5_estimated_m")
    _fcm = markers.get("front_camera_from_tag5_m")
    if _t5m is not None and _fcm is not None and raw_target is not None and len(raw_target) >= 3:
        x0 = float(arm_link00_m[0])
        x5 = float(_t5m[0])
        xf = float(_fcm[0])
        xo = float(raw_target[0])
        cro["ok_arm_behind_xt16_front_ahead_object"] = bool(
            x0 <= x5 + _eps_x and x5 <= xf + _eps_x and xf <= xo + _eps_x
        )
        cro["delta_x_tag5_minus_arm_m"] = round(x5 - x0, 5)
        cro["delta_x_front_minus_tag5_m"] = round(xf - x5, 5)
        cro["delta_x_object_minus_front_m"] = round(xo - xf, 5)
    payload["chain_order_plus_x"] = cro

    # Rilettura servo (una sola) subito prima della FK viewer: ``api_box_plan()`` può richiedere secondi.
    _curv, _servo_diag_full = _read_d1_servo_angles_with_diag()
    if _curv is not None and len(_curv) >= 6:
        q_fb = [math.radians(float(_curv[i])) for i in range(6)]
        q_vis = [float(x) for x in q_fb]
        payload["servo_feedback_ok"] = True
        payload["joints_deg"] = [round(float(_curv[i]), 3) for i in range(min(7, len(_curv)))]
        payload["chain_xyz_m"] = fk_chain_positions(q_fb)
        _tipv = fk_tool_tip(q_fb)
        payload["tool_tip_xyz_m"] = [round(float(_tipv[i]), 5) for i in range(3)]
        payload["servo_feedback_diag"] = {
            "reason": "OK",
            "backend": _servo_diag_full.get("backend"),
            "backends_tried": _servo_diag_full.get("backends_tried"),
            "duration_s": _servo_diag_full.get("duration_s"),
            "dds_domain": _servo_diag_full.get("dds_domain"),
            "listen_s": _servo_diag_full.get("listen_s"),
            "helper_path": _servo_diag_full.get("helper_path"),
        }
    else:
        q_fb = None
        q_vis = [float(x) for x in ARM_FOLD_POSE]
        payload["servo_feedback_ok"] = False
        payload.pop("joints_deg", None)
        payload.pop("chain_xyz_m", None)
        payload.pop("tool_tip_xyz_m", None)
        payload["servo_feedback_diag"] = _servo_diag_full
    _wc_live = fk_wrist_camera_center_m(q_vis, wrist_off)
    _wv_live = fk_wrist_camera_view_axis_unit_m(q_vis, wrist_off)
    _wc_mj = fk_wrist_camera_center_m(q_vis, None)
    _wv_mj = fk_wrist_camera_view_axis_unit_m(q_vis, None)
    mcs = payload.get("mujoco_camera_sites_arm_m")
    if isinstance(mcs, dict):
        mcs["logical_0_wrist"] = {
            "label": "wrist_camera (FK + offset slider locale; q_vis = feedback o fold)",
            "pos_arm_base_m": [round(float(_wc_live[i]), 5) for i in range(3)],
            "view_axis_unit_m": [round(float(_wv_live[i]), 5) for i in range(3)],
            "mjcf_pos_arm_base_m": [round(float(_wc_mj[i]), 5) for i in range(3)],
            "mjcf_view_axis_unit_m": [round(float(_wv_mj[i]), 5) for i in range(3)],
        }
    markers["mjcf_wrist_camera_m"] = [
        round(float(fk_wrist_camera_center_m(q_vis, None)[i]), 5) for i in range(3)
    ]
    markers["wrist_camera_display_m"] = [
        round(float(fk_wrist_camera_center_m(q_vis, wrist_off)[i]), 5) for i in range(3)
    ]

    # --- Viewer 3D: base_link + mesh (mount e camera muso regolabili da slider viz_*) ---
    _mount_bl = _mount_nom
    tip_v = fk_tool_tip(q_vis)
    _ch_bl = fk_chain_positions(q_vis)
    _jmark_bias = (
        float(vg["viz_joint_markers_dx_m"]),
        float(vg["viz_joint_markers_dy_m"]),
        float(vg["viz_joint_markers_dz_m"]),
    )
    payload["scene_graph"] = {
        "frame": "base_link",
        "arm_mount_xyz_m": [round(float(x), 5) for x in _mount_bl],
        "arm_base_to_base_link_offset_m": [round(float(x), 5) for x in _mount_bl],
        "d1_joint_locals_m": fk_d1_joint_locals_m(q_vis),
        "d1_joint_centers_base_link_m": [
            _ab_to_bl(
                [
                    round(float(_ch_bl[i][j]) + _jmark_bias[j], 5)
                    for j in range(3)
                ]
            )
            for i in range(1, 7)
        ],
        "d1_mesh_visual_offsets_m": _d1_urdf_visual_offsets_list(),
        "tool_tip_xyz_m": [round(float(tip_v[i]), 5) for i in range(3)],
        "pose_is_feedback": bool(q_fb is not None),
        "go2_body_offset_base_link_m": [
            round(float(vg["viz_go2_tx_m"]), 5),
            round(float(vg["viz_go2_ty_m"]), 5),
            round(float(vg["viz_go2_tz_m"]), 5),
        ],
    }
    def _rows_to_tags_view(rows_src: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tv: list[dict[str, Any]] = []
        for row in rows_src:
            if not isinstance(row, dict):
                continue
            try:
                tid = int(row.get("id", -1))
            except (TypeError, ValueError):
                continue
            bx = row.get("base_xyz_m")
            cx = row.get("camera_xyz_m")
            tv.append(
                {
                    "id": tid,
                    "base_xyz_m": bx if isinstance(bx, list) and len(bx) >= 3 else None,
                    "base_xyz_base_link_m": _ab_to_bl(bx) if isinstance(bx, list) and len(bx) >= 3 else None,
                    "camera_xyz_m": cx if isinstance(cx, list) and len(cx) >= 3 else None,
                    "logical_camera_device": row.get("logical_camera_device"),
                }
            )
        return tv

    est_full: list[dict[str, Any]] = list(payload.get("apriltag_tag_estimates_base_m") or [])
    rows_viewer = _dedupe_apriltag_rows_for_viewer(
        est_full,
        reference_tag_id=REFERENCE_TAG_ID_LIDAR_FRAME,
    )
    tags_frustum = _rows_to_tags_view(est_full)
    tags_view = _rows_to_tags_view(rows_viewer)
    payload["tags_for_viewer"] = tags_view

    vc_bl: dict[str, Any] = {
        "depth_front_arm_base_m": [round(float(_depth_vis_arm[i]), 5) for i in range(3)],
        "depth_front_base_link_m": _ab_to_bl(_depth_vis_arm.tolist()),
    }
    front_cam_display_bl = list(vc_bl["depth_front_base_link_m"])
    wc_vis = fk_wrist_camera_center_m(q_vis, wrist_off)
    wrist_cam_display_bl = _ab_to_bl(wc_vis)
    vc_bl["front_display_base_link_m"] = [round(float(x), 5) for x in front_cam_display_bl]
    vc_bl["wrist_arm_base_m"] = [round(float(wc_vis[i]), 5) for i in range(3)]
    vc_bl["wrist_base_link_m"] = wrist_cam_display_bl
    vc_bl["wrist_display_base_link_m"] = [round(float(x), 5) for x in wrist_cam_display_bl]
    payload["viewer_cameras_base_link_m"] = vc_bl

    _wv_axis_vis = fk_wrist_camera_view_axis_unit_m(q_vis, wrist_off)

    def _frustum_axis_correct_arm_base(ax: np.ndarray, rx_d: float, ry_d: float, rz_d: float) -> np.ndarray:
        """R = Rz·Ry·Rx (gradi), applicato al versore nel frame arm_link00 (parallelo a base_link)."""
        rx, ry, rz = math.radians(float(rx_d)), math.radians(float(ry_d)), math.radians(float(rz_d))
        cx, sx = math.cos(rx), math.sin(rx)
        cy, sy = math.cos(ry), math.sin(ry)
        cz, sz = math.cos(rz), math.sin(rz)
        Rx = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]], dtype=float)
        Ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]], dtype=float)
        Rz = np.array([[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]], dtype=float)
        R = Rz @ Ry @ Rx
        out = R @ np.asarray(ax, dtype=float).reshape(3)
        n = float(np.linalg.norm(out))
        if n < 1e-12:
            return np.asarray(ax, dtype=float).reshape(3)
        return (out / n).astype(float)

    _d_ax_corr = _frustum_axis_correct_arm_base(
        _d_ax,
        vg["frustum_depth_rx_deg"],
        vg["frustum_depth_ry_deg"],
        vg["frustum_depth_rz_deg"],
    )
    _wv_axis_corr = _frustum_axis_correct_arm_base(
        _wv_axis_vis,
        vg["frustum_wrist_rx_deg"],
        vg["frustum_wrist_ry_deg"],
        vg["frustum_wrist_rz_deg"],
    )
    _fr_near = 0.02
    _depth_far_m = float(vg["frustum_depth_far_m"])
    _wrist_far_m = float(vg["frustum_wrist_far_m"])

    def _tags_bl_rows(dev: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for r in tags_frustum:
            if not isinstance(r, dict):
                continue
            try:
                if int(r.get("logical_camera_device", 10**9)) != int(dev):
                    continue
            except (TypeError, ValueError):
                continue
            bx = r.get("base_xyz_base_link_m")
            if isinstance(bx, list) and len(bx) >= 3:
                rows.append(r)
        return rows

    def _look_at_bl_from_tags(rows: list[dict[str, Any]], id_order: tuple[int, ...]) -> list[float] | None:
        for tid in id_order:
            for r in rows:
                try:
                    if int(r.get("id", -1)) != int(tid):
                        continue
                except (TypeError, ValueError):
                    continue
                b = r.get("base_xyz_base_link_m")
                if not isinstance(b, list) or len(b) < 3:
                    continue
                return [float(b[i]) for i in range(3)]
        return None

    def _unit_toward(from_bl: list[float], to_bl: list[float]) -> np.ndarray | None:
        v = np.asarray(to_bl, dtype=float).reshape(3) - np.asarray(from_bl, dtype=float).reshape(3)
        n = float(np.linalg.norm(v))
        if n < 1e-5:
            return None
        return (v / n).astype(float)

    _d_center_bl = _ab_to_bl(_depth_vis_arm.tolist())
    rows6 = _tags_bl_rows(6)
    # Cono RealSense: priorità **tag scatola id 0** (oggetto davanti), poi altri id / fallback punta.
    look_d = _look_at_bl_from_tags(rows6, (0,))
    if look_d is None:
        look_d = _look_at_bl_from_tags(rows6, (1, 2, 3, REFERENCE_TAG_ID_LIDAR_FRAME))
    if look_d is None:
        look_d = _ab_to_bl([round(float(tip_v[i]), 5) for i in range(3)])
    _d_axis_geo = _unit_toward(_d_center_bl, look_d)
    if _d_axis_geo is not None:
        _d_ax_final = _frustum_axis_correct_arm_base(
            _d_axis_geo,
            vg["frustum_depth_rx_deg"],
            vg["frustum_depth_ry_deg"],
            vg["frustum_depth_rz_deg"],
        )
    else:
        _d_ax_final = _d_ax_corr

    _w_center_bl = _ab_to_bl(wc_vis.tolist())
    rows0 = _tags_bl_rows(0)
    p0 = _look_at_bl_from_tags(rows0, (0,))
    p5_wrist = _look_at_bl_from_tags(rows0, (REFERENCE_TAG_ID_LIDAR_FRAME,))
    p5_front = _look_at_bl_from_tags(rows6, (REFERENCE_TAG_ID_LIDAR_FRAME,))
    p5 = p5_wrist if p5_wrist is not None else p5_front
    look_w_mid: list[float] | None = None
    if p0 is not None and p5 is not None:
        look_w_mid = [(float(p0[i]) + float(p5[i])) * 0.5 for i in range(3)]
    elif p0 is not None:
        look_w_mid = list(p0)
    elif p5 is not None:
        look_w_mid = list(p5)
    if look_w_mid is None:
        look_w_mid = _ab_to_bl([round(float(tip_v[i]), 5) for i in range(3)])
    _w_axis_geo = _unit_toward(_w_center_bl, look_w_mid)
    if _w_axis_geo is not None:
        _w_ax_final = _frustum_axis_correct_arm_base(
            _w_axis_geo,
            vg["frustum_wrist_rx_deg"],
            vg["frustum_wrist_ry_deg"],
            vg["frustum_wrist_rz_deg"],
        )
    else:
        _w_ax_final = _wv_axis_corr

    payload["scene_camera_frusta_base_link"] = {
        "depth_mjcf": {
            "label": "front_camera (display / base_link)",
            "center_m": front_cam_display_bl,
            "axis_unit_m": [1.0, 0.0, 0.0],
            "fovy_deg": 62.0,
            "aspect": 4.0 / 3.0,
            "near_m": round(_fr_near, 4),
            "far_m": round(_depth_far_m, 4),
            "axis_correction_deg_arm_base": {
                "rx": round(float(vg["frustum_depth_rx_deg"]), 2),
                "ry": round(float(vg["frustum_depth_ry_deg"]), 2),
                "rz": round(float(vg["frustum_depth_rz_deg"]), 2),
            },
        },
        "wrist": {
            "label": "wrist_camera (display / base_link)",
            "center_m": wrist_cam_display_bl,
            "axis_unit_m": [round(float(_w_ax_final[i]), 5) for i in range(3)],
            "fovy_deg": 78.0,
            "aspect": 4.0 / 3.0,
            "near_m": round(_fr_near, 4),
            "far_m": round(_wrist_far_m, 4),
            "axis_correction_deg_arm_base": {
                "rx": round(float(vg["frustum_wrist_rx_deg"]), 2),
                "ry": round(float(vg["frustum_wrist_ry_deg"]), 2),
                "rz": round(float(vg["frustum_wrist_rz_deg"]), 2),
            },
        },
    }

    front_tag0_bl = _look_at_bl_from_tags(rows6, (0,))
    target_raw_bl = None if raw_target is None else _ab_to_bl([float(raw_target[i]) for i in range(3)])
    target_disp_bl = None if disp_t is None else _ab_to_bl([float(disp_t[i]) for i in range(3)])
    viewer_target_bl = front_tag0_bl if front_tag0_bl is not None else target_raw_bl
    viewer_target_disp_bl = front_tag0_bl if front_tag0_bl is not None else target_disp_bl
    lm: dict[str, Any] = {
        "depth_camera_mjcf_m": _ab_to_bl(_depth_vis_arm.tolist()),
        "wrist_camera_mjcf_m": _ab_to_bl(wc_vis),
        "front_camera_display_base_link_m": [round(float(x), 5) for x in front_cam_display_bl],
        "wrist_camera_display_base_link_m": [round(float(x), 5) for x in wrist_cam_display_bl],
        "xt16_tag_m": None,
        "front_camera_slider_m": None,
        "object_nominal_20cm_base_link_m": _ab_to_bl(_nom_obj_ab.tolist()),
        "viewer_target_front_tag0_base_link_m": None
        if front_tag0_bl is None
        else [round(float(front_tag0_bl[i]), 5) for i in range(3)],
        # Target visualizzato nel viewer (base_link): priorità tag0 camera frontale, fallback target planner.
        "object_target_base_link_m": None
        if viewer_target_bl is None
        else [round(float(viewer_target_bl[i]), 5) for i in range(3)],
        "object_target_display_base_link_m": None
        if viewer_target_disp_bl is None
        else [round(float(viewer_target_disp_bl[i]), 5) for i in range(3)],
        # Alias vecchio per compatibilità frontend.
        "object_grasp_target_display_base_link_m": None
        if viewer_target_disp_bl is None
        else [round(float(viewer_target_disp_bl[i]), 5) for i in range(3)],
    }
    for row in tags_view:
        if int(row.get("id", -1)) != 5:
            continue
        bx = row.get("base_xyz_base_link_m")
        if isinstance(bx, list) and len(bx) >= 3:
            lm["xt16_tag_m"] = [round(float(bx[i]), 5) for i in range(3)]
            lm["xt16_tag_source"] = "vision"
            break
    if lm["xt16_tag_m"] is None:
        nom5 = _nominal_tag5_arm_base_from_env()
        if nom5 is not None and len(nom5) >= 3:
            lm["xt16_tag_m"] = _ab_to_bl(nom5)
            lm["xt16_tag_source"] = "env"
        else:
            depth_m = float(os.environ.get("GO2_TAG5_FALLBACK_DEPTH_ALONG_OPTICAL_M", "0.42"))
            pt = np.asarray(_depth_vis_arm, dtype=float) + depth_m * np.asarray(_d_ax_corr, dtype=float)
            lm["xt16_tag_m"] = _ab_to_bl([round(float(pt[i]), 5) for i in range(3)])
            lm["xt16_tag_source"] = "depth_cone_ray"
    if isinstance(lm.get("xt16_tag_m"), list) and len(lm["xt16_tag_m"]) >= 3:
        lm["xt16_tag_m"] = _smooth_viewer_xt16_tag_base_link(lm["xt16_tag_m"])
    fcs = markers.get("front_camera_from_tag5_m")
    if isinstance(fcs, list) and len(fcs) >= 3:
        lm["front_camera_slider_m"] = _ab_to_bl(fcs)
    tip_arm = fk_tool_tip(q_vis)
    lm["tool_tip_base_link_m"] = _ab_to_bl([round(float(tip_arm[i]), 5) for i in range(3)])
    _tag_half = float(REFERENCE_TAG_SIZE_M) * 0.5
    _cyl_h = float(os.environ.get("GO2_VIEWER_XT16_CYLINDER_HEIGHT_M", "0.08"))
    _cyl_r = float(os.environ.get("GO2_VIEWER_XT16_CYLINDER_RADIUS_M", "0.05"))
    xtlm = lm.get("xt16_tag_m")
    if isinstance(xtlm, list) and len(xtlm) >= 3:
        tx, ty, tz = float(xtlm[0]), float(xtlm[1]), float(xtlm[2])
        cz = tz - _tag_half - _cyl_h / 2.0
        lm["xt16_lidar_cylinder_base_link_m"] = {
            "center_m": [round(tx, 5), round(ty, 5), round(cz, 5)],
            "radius_m": round(_cyl_r, 6),
            "height_m": round(_cyl_h, 6),
            "axis_unit_m": [0.0, 0.0, 1.0],
            "tag_plane_half_m": round(_tag_half, 5),
            "note_it": "Simbolo XT-16: cilindro Ø10 cm × h 8 cm, AprilTag 5 sul riferimento assoluto del frame base_link (X=19 cm, Y=0, Z=8 cm)."
        }
    payload["viewer_landmarks_base_link_m"] = lm
    _body_half_x = 0.1881
    _body_half_y = 0.04675
    payload["viewer_topdown_footprint_base_link_m"] = {
        "frame": "base_link",
        "body_box_center_m": [0.0, 0.0],
        "body_box_size_m": [round(_body_half_x * 2.0, 4), round(_body_half_y * 2.0, 4)],
        "body_box_corners_m": [
            [-round(_body_half_x, 5), -round(_body_half_y, 5)],
            [round(_body_half_x, 5), -round(_body_half_y, 5)],
            [round(_body_half_x, 5), round(_body_half_y, 5)],
            [-round(_body_half_x, 5), round(_body_half_y, 5)],
        ],
        "front_nose_center_m": [0.285, 0.0],
        "front_nose_radius_m": 0.045,
        "front_camera_xy_m": [round(float(front_cam_display_bl[0]), 5), round(float(front_cam_display_bl[1]), 5)],
        "arm_mount_xy_m": [round(float(_mount_bl[0]), 5), round(float(_mount_bl[1]), 5)],
        "note_it": "Footprint top-down Go2 nel frame base_link: box collision + naso/front camera per orientamento assoluto.",
    }

    _d1m = _d1_stl_disk_summary()
    payload["d1_mesh_assets"] = _d1m
    payload["viewer_3d_warnings"] = []
    if _d1m.get("looks_like_placeholder"):
        payload["viewer_3d_warnings"].append(
            "Mesh D1: i file STL sul server sembrano placeholder (Empty_Link tipici a ~0.5–1 KiB, pochi triangoli = un box). "
            "Non è un errore di Three.js: sostituisci i file in "
            "unitree_mujoco/unitree_robots/go2_d1/d1_550_description/meshes/ con il set del SDK Unitree o "
            "da un clone tipo github.com/JeewanthaSadaruwan/unitree-D1-550-Robot-ARM "
            "poi esegui: python scripts/sync_d1_meshes_from_package.py <.../d1_550_description/meshes> ."
        )

    payload["scene_mesh"] = {
        "manifest": _scene_mesh_manifest(),
        "api_pattern": "/api/arm/scene_meshes/<go2|d1>/<filename>",
    }
    payload["viewer_geometry_notes_it"] = (
        "Camera frontale display: sfera viola davanti al cane + frustum lungo +X (fronte robot). "
        "Camera polso display: sfera gialla sul polso con frustum orientato dalla cinematica del braccio. "
        "Pinza (tool tip): sfera magenta da FK. "
        "XT-16: cilindro grigio sopra il landmark tag 5. "
        "Tag 5: da visione / offset file / nominale env; se no, fallback lungo cono depth."
    )

    return payload


def _calibration_flow_payload() -> dict[str, Any]:
    """Contenuto UI per la calibrazione landmark tag5 + coerenza viewer/planner."""
    path = TAG5_CALIB_PATH
    saved_summary: dict[str, Any] | None = None
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            saved_summary = {
                "updated_at": raw.get("updated_at"),
                "logical_camera_device": raw.get("logical_camera_device"),
                "offset_arm_base_m": raw.get("offset_arm_base_m"),
            }
        except (OSError, json.JSONDecodeError):
            saved_summary = {"read_error": True}
    nominal = _nominal_tag5_arm_base_from_env()
    return {
        "ok": True,
        "markers_explained_it": {
            "tag5_xt16": (
                "AprilTag **25h9 ID 5** fisso sul robot (vicino XT-16): **un solo** landmark sul corpo basta per "
                "**correggere** la mappa tvec camera → frame base braccio (`data/tag5_calibration_arm_base.json`). "
                "Non servono altri ArUco/AprilTag **sul cane** né che **entrambe** le telecamere lo vedano: per il POST "
                "«Salva nominale» basta **una** camera nitida (di solito **polso / video0**)."
            ),
            "arm_link00_nominal_it": (
                "**arm_link00** è il frame **base braccio** (origine al primo giunto D1), non il muso del Go2: assi in metri, "
                "**+X** verso la testa del cane. Il **nominale** del centro tag 5 è la tua stima fisica di dove cade quel punto nel "
                "mondo reale in quel frame (CAD, metro, oppure confronto con la **scena 3D** dopo aver messo braccio/cilindro "
                "XT-16 come li vedi dal vivo, poi «Salva nominale» salva la correzione interna della euristica tvec)."
            ),
            "dual_probe_optional": (
                "**GET …/tag5_calibration?dual_probe=1** è **solo diagnostica**: se nello stesso istante **video0** e "
                "**video6** inquadrano **lo stesso** ID 5, confronta due stime euristiche in base (mm di disaccordo). "
                "**Non** scrive file, **non** calibra extrinseci separati per camera e **non** sostituisce il POST da "
                "**una** camera di riferimento (tipicamente polso)."
            ),
            "box_tags_0_3": (
                "Tag **0–3** sulla **scatola**: servono a dove prendere l’oggetto; non sostituiscono il landmark sul robot."
            ),
            "cross_camera_geometry_it": (
                "Per offset **diversi** su **polso (0)** e **RealSense (6)** quando **entrambe** vedono lo **stesso** "
                "AprilTag (anche su scatola / oggetto): **POST /api/arm/tag_calibration_shared_dual** con `tag_id`, "
                "`nominal_arm_base_m`, opz. `tag_edge_length_m`. Scrive `offset_by_logical_camera_device_m` nello stesso "
                "JSON della calibrazione tag5. La profondità RealSense «tag→suolo» non è ancora integrata nel server."
            ),
        },
        "dynamic": {
            "nominal_tag5_arm_base_m": nominal,
            "nominal_configured": nominal is not None,
            "tag5_offset_file_present": path.is_file(),
            "saved_calibration_summary": saved_summary,
            "tag5_calibration_enable_env": os.environ.get("GO2_TAG5_CALIBRATION_ENABLE", "1"),
        },
        "steps_it": [
            {
                "n": 1,
                "title": "Nominale centro tag 5 in arm_link00 (base braccio)",
                "body": (
                    "Coordinate **in metri** nell’origine **arm_link00** (primo giunto), **+X** avanti sul cane: è **diverso** "
                    "dal solo allineamento mesh Three.js. Il nominale descrive dove sta il **centro fisico** del tag 5 "
                    "rispetto al braccio (misura/CAD, o confronto visivo con la scena 3D + cilindro XT-16). "
                    "Dopo aver impostato un nominale plausibile, «Salva nominale» misura l’errore della euristica tvec→base ",
                    "e lo corregge per planner e viewer."
                ),
            },
            {
                "n": 2,
                "title": "Inquadra il tag 5 (di solito solo il polso)",
                "body": (
                    "Serve **un** frame nitido con ID **5** rilevato. Sul Go2+D1 spesso **solo la camera polso (0)** "
                    "vede il landmark sul corpo; la RealSense (6) può **non** inquadrarlo: va bene lo stesso. "
                    "Nel POST di calibrazione usa **camera 0** (polso) come riferimento. "
                    "**dual_probe** ha senso **solo** se in quell’istante **entrambe** le camere vedono **lo stesso** tag 5 "
                    "(raro); altrimenti ignorarlo. **Non** calibra la geometria relativa tra le due telecamere."
                ),
            },
            {
                "n": 3,
                "title": "Salva nominale tag 5 (XT-16) su disco",
                "body": (
                    "Scrive `offset_arm_base_m = nominale fisico − euristica(tvec)` in `data/tag5_calibration_arm_base.json` "
                    "(tipicamente da **camera 0**). Vale come **fallback** per ogni camera se non esiste una voce "
                    "specifica in `offset_by_logical_camera_device_m`. **Planner** e **Three.js** usano "
                    "`camera_tvec_to_base_xyz` con priorità correzione **per-device** quando presente."
                ),
            },
            {
                "n": 4,
                "title": "Allineamento visivo telecamere nel viewer",
                "body": (
                    "Regola slider **Camera frontale**, **Corpo Go2**, rotazioni **frustum** nel pannello geometria: "
                    "obiettivo è che **cono viola** + **sfera viola** corrispondano alla RealSense reale, mentre la "
                    "**sfera rossa** (tag5 stimato) resti geometricamente plausibile. "
                    "Usa `mjcf_depth_to_tag5_mm` in `scene_3d` → `vis_geometry_chain_mm` come metrica. "
                    "**Geometria tra polso e muso:** dopo il passo 6 (offset per-device) gli slider servono soprattutto "
                    "per il **modello MJCF** nel viewer; il planner userà gli offset salvati per 0 e 6."
                ),
            },
            {
                "n": 5,
                "title": "Tag 0–3 sulla scatola (dimensione e IK)",
                "body": (
                    "Verifica **BOX_TAG_SIZE_M** (default 19 mm) = lato stampato del tag sulla scatola. "
                    "Il target presa in base braccio usa la **stessa** mappa `camera_tvec_to_base_xyz` usata per il tag 5; "
                    "calibrazione + viewer devono essere plausibili prima che l’IK «prenda bene» la scatola."
                ),
            },
            {
                "n": 6,
                "title": "Stesso AprilTag visibile da polso e RealSense (correzione per camera)",
                "body": (
                    "Quando **video0** e **video6** inquadrano **lo stesso** tag (anche su oggetto / scatola, non serve a terra): "
                    "**POST /api/arm/tag_calibration_shared_dual** con `tag_id`, `nominal_arm_base_m` (centro tag in arm_link00, m) "
                    "e opz. `tag_edge_length_m` se l’ID non è tra 0–3 o 5. "
                    "Scrive `offset_by_logical_camera_device_m` nel JSON accanto a `offset_arm_base_m` del tag 5. "
                    "La distanza tag→suolo con **depth** RealSense non è ancora calcolata nel server (solo nota in risposta)."
                ),
            },
        ],
        "env_hints": {
            "GO2_TAG5_NOMINAL_ARM_BASE_M": "es. 0.19,0.0,0.08 (tag 5 assoluto in base_link)",
            "GO2_TAG5_CALIBRATION_ENABLE": "1 (default) oppure 0 per disattivare il file offset",
            "dual_probe": "GET /api/arm/tag5_calibration?dual_probe=1",
            "shared_dual_tag": "POST /api/arm/tag_calibration_shared_dual",
        },
    }


@APP.route("/api/arm/calibration_flow", methods=["GET"])
def api_arm_calibration_flow() -> Any:
    return jsonify(_calibration_flow_payload())


@APP.route("/api/arm/servo_feedback_diag", methods=["GET"])
def api_arm_servo_feedback_diag() -> Any:
    """Perché non leggiamo il servo: esegue il probe DDS una volta e ritorna ``diag`` completo."""
    angles, diag = _read_d1_servo_angles_with_diag()
    resp = jsonify(
        {
            "ok": True,
            "servo_feedback_ok": angles is not None and len(angles or []) >= 6,
            "joints_deg": angles,
            "diag": diag,
        }
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@APP.route("/api/arm/scene_3d", methods=["GET"])
def api_arm_scene_3d() -> Any:
    try:
        fast = request.args.get("fast", "").strip().lower() in ("1", "true", "yes")
        resp = jsonify(_arm_scene_3d_payload(geometry_fast=fast))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    except Exception as exc:
        return jsonify({"ok": False, "error": repr(exc)}), 500


def _tag5_dual_camera_probe_payload() -> dict[str, Any]:
    """Solo se **entrambe** le camere rilevano l'AprilTag 5 nello stesso giro: confronto euristiche base braccio.

    Non calibra extrinseci 0↔6; spesso sul corpo il tag 5 è visibile solo dal polso — allora questo endpoint
    restituisce disaccordo alto o metà campi nulli: comportamento atteso, ignorare o non chiamare dual_probe.
    """
    import numpy as np

    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from box_grasp_planner import (
        REFERENCE_TAG_ID_LIDAR_FRAME,
        _camera_tvec_to_base_heuristic_xyz,
        camera_tvec_to_base_xyz,
        plan_from_frame,
    )

    devices: dict[str, Any] = {}
    heur_pairs: list[tuple[int, list[float]]] = []
    cal_pairs: list[tuple[int, list[float]]] = []

    for dev in (0, 6):
        key = f"V4L2_{dev}"
        frame = frame_from_camera(dev)
        if frame is None:
            devices[key] = {"logical_device": dev, "frame_ok": False, "error": "no_frame"}
            continue
        pl = plan_from_frame(frame, object_detection=None, logical_camera_device=dev)
        poses_wrap = pl.get("poses") or {}
        pose_list = poses_wrap.get("poses") if isinstance(poses_wrap, dict) else poses_wrap
        if not isinstance(pose_list, list):
            pose_list = []
        cam_xyz: list[float] | None = None
        for p in pose_list:
            try:
                if int(p.get("id", -1)) != REFERENCE_TAG_ID_LIDAR_FRAME:
                    continue
                c = p.get("camera_xyz_m")
                if isinstance(c, list) and len(c) >= 3:
                    cam_xyz = [float(c[0]), float(c[1]), float(c[2])]
                break
            except (TypeError, ValueError):
                continue
        if cam_xyz is None:
            devices[key] = {"logical_device": dev, "frame_ok": True, "tag5_seen": False}
            continue
        h = _camera_tvec_to_base_heuristic_xyz(cam_xyz)
        b = camera_tvec_to_base_xyz(cam_xyz, logical_camera_device=dev)
        devices[key] = {
            "logical_device": dev,
            "frame_ok": True,
            "tag5_seen": True,
            "tag5_camera_xyz_m": [round(x, 5) for x in cam_xyz],
            "heuristic_arm_base_m": [round(float(x), 5) for x in h],
            "calibrated_arm_base_m": [round(float(x), 5) for x in b],
        }
        heur_pairs.append((dev, h))
        cal_pairs.append((dev, b))

    out: dict[str, Any] = {
        "ok": True,
        "reference_tag_id": REFERENCE_TAG_ID_LIDAR_FRAME,
        "devices": devices,
    }

    def _dist_mm(pairs: list[tuple[int, list[float]]]) -> float | None:
        if len(pairs) < 2:
            return None
        a = np.asarray(pairs[0][1], dtype=float)
        b = np.asarray(pairs[1][1], dtype=float)
        return round(float(np.linalg.norm(a - b) * 1000.0), 2)

    hm = _dist_mm(heur_pairs)
    cm = _dist_mm(cal_pairs)
    if hm is not None:
        out["heuristic_disagreement_mm"] = hm
    if cm is not None:
        out["calibrated_disagreement_mm"] = cm
    out["interpretation_it"] = (
        "La funzione `_camera_tvec_to_base_heuristic_xyz` è **unica** per tutte le camere: non modella "
        "intrinseci/extrinseci separati per device. Due viste dello stesso tag 5 producono quasi sempre "
        "stime base diverse finché non combini: (1) POST offset da **una** camera di riferimento (di solito polso), "
        "(2) slider viewer RealSense + `vis_geometry_chain_mm` in `/api/arm/scene_3d`."
    )
    return out


@APP.route("/api/arm/tag5_calibration", methods=["GET", "POST", "DELETE"])
def api_arm_tag5_calibration() -> Any:
    """
    Calibrazione del riferimento fisico tag 5 (fisso su XT-16).
    POST: legge frame, trova tag 5, calcola la correzione interna = nominale fisico - euristica (senza correzione precedente).
    Richiede ``GO2_TAG5_NOMINAL_ARM_BASE_M`` o JSON ``nominal_tag5_arm_base_m``.

    GET ``?dual_probe=1``: confronta tag 5 su ``/dev/video0`` e ``/dev/video6`` (nessuna scrittura su disco).
    """
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from box_grasp_planner import REFERENCE_TAG_ID_LIDAR_FRAME, make_tag5_calibration_record, plan_from_frame

    path = TAG5_CALIB_PATH
    if request.method == "GET":
        if request.args.get("dual_probe", "").lower() in ("1", "true", "yes", "on"):
            try:
                return jsonify(_tag5_dual_camera_probe_payload())
            except Exception as exc:
                return jsonify({"ok": False, "error": repr(exc)}), 500
        out: dict[str, Any] = {
            "ok": True,
            "path": str(path),
            "nominal_env_m": _nominal_tag5_arm_base_from_env(),
            "enable_env": os.environ.get("GO2_TAG5_CALIBRATION_ENABLE", "1"),
        }
        if path.is_file():
            try:
                out["saved"] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                out["read_error"] = repr(exc)
        out["guide_url"] = "/api/arm/calibration_flow"
        return jsonify(out)

    if request.method == "DELETE":
        try:
            if path.is_file():
                path.unlink()
            return jsonify({"ok": True, "cleared": True})
        except OSError as exc:
            return jsonify({"ok": False, "error": repr(exc)}), 500

    body = request.get_json(silent=True) or {}
    nominal: list[float] | None = None
    n_body = body.get("nominal_tag5_arm_base_m")
    if isinstance(n_body, list) and len(n_body) >= 3:
        try:
            nominal = [float(n_body[0]), float(n_body[1]), float(n_body[2])]
        except (TypeError, ValueError):
            nominal = None
    if nominal is None:
        nominal = _nominal_tag5_arm_base_from_env()
    if not nominal or len(nominal) < 3:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Imposta il centro tag 5 nel frame base braccio (m): env "
                    "GO2_TAG5_NOMINAL_ARM_BASE_M=x,y,z oppure POST JSON nominal_tag5_arm_base_m."
                ),
            }
        ), 400

    prefer_dev = body.get("camera_device")
    dev_order: list[int] = []
    if prefer_dev is not None:
        try:
            dev_order.append(int(prefer_dev))
        except (TypeError, ValueError):
            pass
    # Per la calibrazione tag5 sul corpo del robot la camera utile è di norma la polso (0).
    # La frontale (6) resta disponibile solo se la si seleziona esplicitamente.
    if not dev_order:
        dev_order.append(0)

    found: tuple[int, list[float]] | None = None
    for dev in dev_order:
        frame = frame_from_camera(dev)
        if frame is None:
            continue
        pl = plan_from_frame(frame, object_detection=None, logical_camera_device=dev)
        poses_blob = pl.get("poses") or {}
        for p in poses_blob.get("poses") or []:
            if int(p.get("id", -1)) == REFERENCE_TAG_ID_LIDAR_FRAME:
                cam = p.get("camera_xyz_m")
                if isinstance(cam, list) and len(cam) >= 3:
                    found = (dev, [float(cam[0]), float(cam[1]), float(cam[2])])
                    break
        if found:
            break

    if not found:
        return jsonify(
            {"ok": False, "error": "AprilTag 5 non rilevato sulla camera selezionata (di norma la polso / device 0)."}
        ), 400

    dev, cam_xyz = found
    rec = make_tag5_calibration_record(nominal, cam_xyz, logical_camera_device=dev)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old.get("offset_by_logical_camera_device_m"), dict):
                rec["offset_by_logical_camera_device_m"] = old["offset_by_logical_camera_device_m"]
            if isinstance(old.get("dual_shared_tag_calib"), dict):
                rec["dual_shared_tag_calib"] = old["dual_shared_tag_calib"]
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return jsonify(
        {
            "ok": True,
            "saved": rec,
            "camera_logical_device_used": dev,
            "next_steps_it": [
                "Aggiorna la vista 3D: la sfera rossa (tag5) e i tag scatola usano lo stesso offset del planner.",
                "Allinea cono/sfera viola RealSense con i cursori «Camera frontale» / frustum (vedi calibration_visual_alignment in scene_3d).",
            ],
        }
    )


def _v4l_for_log(logical: int) -> int:
    try:
        return int(_v4l_index_for_logical_camera(int(logical)))
    except Exception:
        return int(logical)


@APP.route("/api/arm/tag_calibration_shared_dual", methods=["GET", "POST"])
def api_arm_tag_calibration_shared_dual() -> Any:
    """
    Offset ``tvec→base`` **separati** per ``logical_camera_device`` 0 e 6 usando lo **stesso**
    AprilTag (es. su scatola) visibile da **entrambe** le camere nello stesso momento.

    Non integra ancora la profondità RealSense verso «terra»: solo solvePnP + euristica + offset.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from box_grasp_planner import TRACKED_TAG_IDS, _camera_tvec_to_base_heuristic_xyz, tvec_camera_m_for_tag_id

    path = TAG5_CALIB_PATH

    if request.method == "GET":
        out: dict[str, Any] = {"ok": True, "path": str(path)}
        if path.is_file():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                out["saved"] = d
                ob = d.get("offset_by_logical_camera_device_m")
                out["has_per_device_offsets"] = isinstance(ob, dict) and len(ob) > 0
            except (OSError, json.JSONDecodeError) as exc:
                out["read_error"] = repr(exc)
        out["hint_it"] = (
            "POST con tag_id, nominal_arm_base_m [x,y,z] in arm_link00, opz. tag_edge_length_m se l'ID non è 0–3 o 5. "
            "Richiede frame da video0 e video6 con **lo stesso** tag rilevato."
        )
        return jsonify(out)

    body = request.get_json(silent=True) or {}
    try:
        tag_id = int(body.get("tag_id", -1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "tag_id intero richiesto"}), 400
    if tag_id < 0 or tag_id > 491:
        return jsonify({"ok": False, "error": "tag_id fuori range"}), 400

    n_body = body.get("nominal_arm_base_m")
    if not isinstance(n_body, list) or len(n_body) < 3:
        return jsonify({"ok": False, "error": "nominal_arm_base_m: lista di 3 numeri (m) in arm_link00"}), 400
    try:
        nominal = [float(n_body[0]), float(n_body[1]), float(n_body[2])]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "nominal_arm_base_m non numerici"}), 400

    raw_devs = body.get("logical_devices")
    if isinstance(raw_devs, list) and raw_devs:
        devices: list[int] = []
        for x in raw_devs:
            try:
                d = int(x)
            except (TypeError, ValueError):
                continue
            if d in (0, 6) and d not in devices:
                devices.append(d)
    else:
        devices = [0, 6]
    if len(devices) < 2:
        return jsonify({"ok": False, "error": "Servono due logical_devices tra 0 e 6 (default [0,6])"}), 400

    tag_edge_m = body.get("tag_edge_length_m")
    edge_f: float | None = None
    if tag_edge_m is not None:
        try:
            edge_f = float(tag_edge_m)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "tag_edge_length_m non numerico"}), 400
        if edge_f <= 0 or edge_f > 0.5:
            return jsonify({"ok": False, "error": "tag_edge_length_m implausibile (0–0.5 m)"}), 400
    elif int(tag_id) not in TRACKED_TAG_IDS:
        return jsonify(
            {
                "ok": False,
                "error": f"tag_id {tag_id} non in {sorted(TRACKED_TAG_IDS)}: imposta tag_edge_length_m (lato tag in m)",
            }
        ), 400

    overs: dict[int, float] | None = {int(tag_id): float(edge_f)} if edge_f is not None else None

    per_dev: dict[str, Any] = {}
    offsets: dict[int, list[float]] = {}
    for dev in devices:
        frame = frame_from_camera(dev)
        if frame is None:
            return jsonify({"ok": False, "error": f"Nessun frame da logical camera {dev}"}), 400
        tvec = tvec_camera_m_for_tag_id(frame, tag_id, tag_edge_length_overrides=overs)
        if tvec is None:
            return jsonify(
                {"ok": False, "error": f"AprilTag {tag_id} non rilevato su /dev/video{_v4l_for_log(dev)} (logical {dev})"}
            ), 400
        h = _camera_tvec_to_base_heuristic_xyz(tvec)
        off = [float(nominal[i]) - float(h[i]) for i in range(3)]
        offsets[int(dev)] = off
        per_dev[str(dev)] = {
            "tag_camera_xyz_m": [round(tvec[i], 6) for i in range(3)],
            "heuristic_base_before_m": [round(h[i], 6) for i in range(3)],
            "offset_arm_base_m": [round(off[i], 6) for i in range(3)],
        }

    merged: dict[str, Any] = {}
    if path.is_file():
        try:
            merged = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            merged = {}
    obm = dict(merged.get("offset_by_logical_camera_device_m") or {})
    for dev, off in offsets.items():
        obm[str(int(dev))] = [round(float(off[i]), 6) for i in range(3)]
    merged["offset_by_logical_camera_device_m"] = obm
    merged["dual_shared_tag_calib"] = {
        "tag_id": int(tag_id),
        "nominal_arm_base_m": [round(float(nominal[i]), 6) for i in range(3)],
        "logical_devices": [int(d) for d in devices],
        "per_device": per_dev,
        "tag_edge_length_m": None if edge_f is None else round(edge_f, 6),
        "updated_at": now_iso(),
        "depth_to_ground_note_it": (
            "Distanza tag→suolo con **profondità** RealSense non è calcolata qui: servirebbe depth allineato al RGB "
            "nel pixel del tag + modello piano suolo / Z suolo noto in base braccio (integrazione futura)."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return jsonify(
        {
            "ok": True,
            "saved_path": str(path),
            "offset_by_logical_camera_device_m": obm,
            "dual_shared_tag_calib": merged["dual_shared_tag_calib"],
            "next_steps_it": [
                "Ricarica /api/box/plan e scene_3d: le pose da video0 e video6 useranno gli offset rispettivi.",
                "Mantieni anche la calibrazione tag 5 (XT-16) con POST /api/arm/tag5_calibration dal polso.",
            ],
        }
    )


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


@APP.route("/api/arm/vis_geometry", methods=["GET", "POST"])
def api_arm_vis_geometry() -> Any:
    """Slider sessione: geometria vista 3D (tag5, camera front, polso, EMA, frustum display)."""
    defaults = _vis_geometry_defaults_dict()
    if request.method == "GET":
        with VIS_GEOMETRY_TUNING_LOCK:
            over = dict(VIS_GEOMETRY_TUNING)
        return jsonify({"ok": True, "effective": {**defaults, **over}, "overrides": over, "defaults": defaults})
    body = request.get_json(silent=True) or {}
    if body.get("reset"):
        with VIS_GEOMETRY_TUNING_LOCK:
            VIS_GEOMETRY_TUNING.clear()
        try:
            if VIS_GEOMETRY_JSON_PATH.is_file():
                VIS_GEOMETRY_JSON_PATH.unlink()
        except OSError:
            pass
        with _SCENE3D_TARGET_DISPLAY_LOCK:
            _SCENE3D_TARGET_DISPLAY_STATE["ema_m"] = None
        return jsonify({"ok": True, "cleared": True, "effective": _vis_geometry_defaults_dict(), "defaults": defaults})
    changed: dict[str, float] = {}
    errs: list[str] = []
    persist = body.get("persist", True)
    if persist is None or isinstance(persist, str):
        persist = str(persist).lower() not in {"0", "false", "no", "off"}
    else:
        persist = bool(persist)
    with VIS_GEOMETRY_TUNING_LOCK:
        for key, (lo, hi) in _ALLOWED_VIS_GEOMETRY.items():
            if key not in body:
                continue
            try:
                raw = float(body[key])
            except (TypeError, ValueError):
                errs.append(f"{key}: valore non numerico")
                continue
            v = max(lo, min(hi, raw))
            VIS_GEOMETRY_TUNING[key] = v
            changed[key] = v
        over = dict(VIS_GEOMETRY_TUNING)
    if not errs and persist:
        _save_vis_geometry_to_disk()
    return jsonify(
        {
            "ok": not errs,
            "errors": errs,
            "changed": changed,
            "effective": {**defaults, **over},
            "defaults": defaults,
            "persisted_path": "data/vis_geometry_tuning.json" if persist else None,
            "persisted_to_disk": bool(persist and not errs),
        }
    )


@APP.route("/api/arm/vis_geometry/presets", methods=["GET"])
def api_vis_geometry_presets_list() -> Any:
    """Elenco preset nominati (file ``data/vis_geometry_presets.json``)."""
    with VIS_GEOMETRY_PRESETS_LOCK:
        raw = _vis_geometry_presets_read_dict()
    presets_obj = raw.get("presets") or {}
    if not isinstance(presets_obj, dict):
        presets_obj = {}
    items: list[dict[str, Any]] = []
    for name in sorted(presets_obj.keys(), key=lambda x: str(x).lower()):
        entry = presets_obj.get(name)
        if not isinstance(entry, dict):
            entry = {}
        vals = entry.get("values")
        nk = len(vals) if isinstance(vals, dict) else 0
        items.append({"name": name, "saved_at": entry.get("saved_at"), "n_keys": nk})
    return jsonify({"ok": True, "presets": items, "path": "data/vis_geometry_presets.json"})


@APP.route("/api/arm/vis_geometry/presets/save", methods=["POST"])
def api_vis_geometry_presets_save() -> Any:
    """Salva lo stato effettivo corrente (tutti i parametri consentiti) come preset nominato."""
    body = request.get_json(silent=True) or {}
    name = _sanitize_preset_name(body.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "nome non valido (1–80 caratteri)"}), 400
    overwrite = bool(body.get("overwrite"))
    snap = _vis_geometry_preset_snapshot_effective()
    with VIS_GEOMETRY_PRESETS_LOCK:
        raw = _vis_geometry_presets_read_dict()
        presets_obj = raw.setdefault("presets", {})
        if not isinstance(presets_obj, dict):
            presets_obj = {}
            raw["presets"] = presets_obj
        if name in presets_obj and not overwrite:
            return jsonify({"ok": False, "error": "preset già esistente", "hint": "overwrite: true"}), 409
        presets_obj[name] = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "values": snap,
        }
        if not _vis_geometry_presets_write_dict(raw):
            return jsonify(
                {
                    "ok": False,
                    "error": "impossibile scrivere data/vis_geometry_presets.json (permessi o disco)",
                    "path": "data/vis_geometry_presets.json",
                }
            ), 500
    return jsonify(
        {
            "ok": True,
            "name": name,
            "n_keys": len(snap),
            "path": "data/vis_geometry_presets.json",
            "message_it": f"preset «{name}» scritto su disco ({len(snap)} parametri)",
        }
    )


@APP.route("/api/arm/vis_geometry/presets/load", methods=["POST"])
def api_vis_geometry_presets_load() -> Any:
    """Carica un preset nel tuning corrente (opzionale persist su ``vis_geometry_tuning.json``)."""
    body = request.get_json(silent=True) or {}
    name = _sanitize_preset_name(body.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "nome non valido"}), 400
    persist = body.get("persist", True)
    if isinstance(persist, str):
        persist = persist.lower() not in {"0", "false", "no", "off"}
    else:
        persist = bool(persist)
    with VIS_GEOMETRY_PRESETS_LOCK:
        raw = _vis_geometry_presets_read_dict()
        entry = (raw.get("presets") or {}).get(name)
    if not isinstance(entry, dict):
        pk = (
            sorted(str(k) for k in (raw.get("presets") or {}).keys())
            if isinstance(raw.get("presets"), dict)
            else []
        )
        _LOG_VIS.warning(
            "vis_geometry presets/load 404 name=%r preset_keys=%s file=%s",
            name,
            pk[:40],
            VIS_GEOMETRY_PRESETS_PATH,
        )
        return jsonify({"ok": False, "error": "preset non trovato", "preset_keys": pk}), 404
    vals = entry.get("values")
    if not isinstance(vals, dict):
        return jsonify({"ok": False, "error": "preset corrotto (values)"}), 400
    errs = _vis_geometry_apply_preset_values(vals)
    with _SCENE3D_TARGET_DISPLAY_LOCK:
        _SCENE3D_TARGET_DISPLAY_STATE["ema_m"] = None
    if persist and not errs:
        _save_vis_geometry_to_disk()
    defaults = _vis_geometry_defaults_dict()
    with VIS_GEOMETRY_TUNING_LOCK:
        over = dict(VIS_GEOMETRY_TUNING)
    apply_ok = len(errs) == 0
    _LOG_VIS.warning(
        "vis_geometry presets/load name=%r persist=%s apply_ok=%s n_err=%s wrist_dx=%s wrist_dz=%s file=%s",
        name,
        persist,
        apply_ok,
        len(errs),
        round(float(over.get("wrist_local_dx", defaults["wrist_local_dx"])), 6),
        round(float(over.get("wrist_local_dz", defaults["wrist_local_dz"])), 6),
        VIS_GEOMETRY_PRESETS_PATH,
    )
    return jsonify(
        {
            "ok": apply_ok,
            "errors": errs,
            "name": name,
            "effective": {**defaults, **over},
            "persisted_to_disk": bool(persist and not errs),
        }
    )


@APP.route("/api/arm/vis_geometry/presets/remove", methods=["POST"])
def api_vis_geometry_presets_remove() -> Any:
    body = request.get_json(silent=True) or {}
    name = _sanitize_preset_name(body.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "nome non valido"}), 400
    with VIS_GEOMETRY_PRESETS_LOCK:
        raw = _vis_geometry_presets_read_dict()
        presets_obj = raw.get("presets") or {}
        if not isinstance(presets_obj, dict):
            return jsonify({"ok": False, "error": "file preset vuoto"}), 404
        if name not in presets_obj:
            return jsonify({"ok": False, "error": "preset non trovato"}), 404
        del presets_obj[name]
        if not _vis_geometry_presets_write_dict(raw):
            return jsonify(
                {
                    "ok": False,
                    "error": "impossibile aggiornare data/vis_geometry_presets.json dopo rimozione",
                    "path": "data/vis_geometry_presets.json",
                }
            ), 500
    return jsonify({"ok": True, "removed": name})


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
            {"phase_label_it": "Accucciata Go2, poi preflight grasp…", "progress_step": 0},
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
            {"phase_label_it": "Preflight camere/IK (asincrono)…", "progress_step": 0},
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
    # Questo file è il modulo che espone ``APP`` e tutta la logica; l'HTTP server si avvia solo da
    # ``scripts/serve_dashboard_modular.py`` (o dal deploy NX che lo invoca).
    print(
        "Non avviare questo file direttamente.\n"
        "  python scripts/serve_dashboard_modular.py\n"
        "Sulla NX dopo deploy: lo script remoto usa già il modular.\n",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(2)
