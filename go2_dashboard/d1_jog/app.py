"""Flask app — pagina jog D1 con slider (SDK)."""

from __future__ import annotations

import json
import os
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, after_this_request, jsonify, render_template, request, send_file, stream_with_context
from werkzeug.exceptions import HTTPException

from go2_dashboard.d1_jog import (
    cartesian,
    grasp6d,
    jog_stream,
    orbbec_capture,
    pick_preset,
    pick_vision,
    program_runner,
    program_store,
    service,
    wrist_rgbd,
)
from go2_dashboard.paths import PROJECT_ROOT
from go2_dashboard.sport_lane import (
    GO2_DDS_DOMAIN,
    GO2_DDS_INTERFACE,
    accompany_mode_handle,
    base_motion_allowed,
    sport_last_payload,
)

THERMAL_SETTINGS: dict[str, Any] = {
    "warn_c": 62.0,
    "critical_c": 72.0,
    "auto_crouch": False,
}

_PROCESS_STARTED = datetime.now().isoformat(timespec="seconds")
_GO2_HERO_CANDIDATES = (
    PROJECT_ROOT / "data" / "unitree_robot_main.png",
)
_GO2_FEATURES_CANDIDATES = (
    PROJECT_ROOT / "data" / "unitree_robot_main.png",
)


def _go2_svg_fallback(*, label: str) -> bytes:
    svg = f"""
<svg xmlns='http://www.w3.org/2000/svg' width='1280' height='720' viewBox='0 0 1280 720'>
  <defs>
    <linearGradient id='bg' x1='0' y1='0' x2='0' y2='1'>
      <stop offset='0%' stop-color='#bfefff'/>
      <stop offset='100%' stop-color='#66c6ff'/>
    </linearGradient>
    <linearGradient id='ground' x1='0' y1='0' x2='0' y2='1'>
      <stop offset='0%' stop-color='#9ae58d'/>
      <stop offset='100%' stop-color='#49b95d'/>
    </linearGradient>
  </defs>
  <rect width='1280' height='720' fill='url(#bg)'/>
  <ellipse cx='1030' cy='118' rx='95' ry='95' fill='rgba(255,255,255,.65)'/>
  <rect y='560' width='1280' height='160' fill='url(#ground)'/>
  <g transform='translate(248 276)' fill='#0a5f90' stroke='#0a5f90' stroke-linecap='round'>
    <rect x='80' y='48' width='590' height='120' rx='46' fill='#1383c2'/>
    <rect x='622' y='58' width='110' height='72' rx='25' fill='#1383c2'/>
    <circle cx='704' cy='95' r='14' fill='#9fe4ff'/>
    <rect x='196' y='74' width='300' height='46' rx='20' fill='#c6f4ff' stroke='none'/>
    <path d='M145 164 102 286' stroke-width='34'/>
    <path d='M318 164 288 290' stroke-width='34'/>
    <path d='M494 164 532 290' stroke-width='34'/>
    <path d='M648 164 690 286' stroke-width='34'/>
    <path d='M736 86 796 50' stroke-width='23'/>
    <circle cx='811' cy='42' r='18'/>
  </g>
  <text x='58' y='90' fill='#0e5b86' font-size='52' font-family='Trebuchet MS, Segoe UI, sans-serif' font-weight='700'>Unitree Go2</text>
  <text x='60' y='136' fill='#1b6e99' font-size='30' font-family='Trebuchet MS, Segoe UI, sans-serif'>{label}</text>
</svg>
""".strip()
    return svg.encode("utf-8")


def _usb_vid_pid_for_v4l(index: int) -> tuple[str, str] | None:
    try:
        cur = Path(f"/sys/class/video4linux/video{int(index)}/device").resolve()
    except OSError:
        return None
    for _ in range(20):
        vendor = cur / "idVendor"
        product = cur / "idProduct"
        if vendor.is_file() and product.is_file():
            try:
                return vendor.read_text().strip().lower(), product.read_text().strip().lower()
            except OSError:
                return None
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _is_realsense_color_capture_node(index: int) -> bool:
    """RealSense RGB capture is UVC interface 1.3, stream index 0."""
    try:
        device_path = str(Path(f"/sys/class/video4linux/video{int(index)}/device").resolve())
        stream_index = int(Path(f"/sys/class/video4linux/video{int(index)}/index").read_text().strip())
    except (OSError, ValueError):
        return False
    return ":1.3" in device_path and stream_index == 0


def _resolve_realsense_rgb_index(*, role: str) -> int:
    if role == "wrist":
        env_key, pid, fallback = "D1_WRIST_V4L_INDEX", "0b5c", 4
    else:
        env_key, pid, fallback = "D1_FRONT_V4L_INDEX", "0b3a", 10
    candidates: list[int] = []
    base = Path("/sys/class/video4linux")
    if base.is_dir():
        for node in sorted(base.glob("video*")):
            tail = node.name[5:]
            if not tail.isdigit():
                continue
            idx = int(tail)
            pair = _usb_vid_pid_for_v4l(idx)
            if pair == ("8086", pid) and _is_realsense_color_capture_node(idx):
                candidates.append(idx)
    try:
        configured = int(os.environ.get(env_key, str(fallback)))
    except ValueError:
        configured = fallback
    if configured in candidates:
        return configured
    return candidates[0] if candidates else configured


def _frame_chroma_bgr(frame: Any) -> float:
    try:
        import cv2

        return float(
            cv2.mean(cv2.absdiff(frame[:, :, 0], frame[:, :, 1]))[0]
            + cv2.mean(cv2.absdiff(frame[:, :, 1], frame[:, :, 2]))[0]
        )
    except Exception:
        return 0.0


def _mount_motor_health(app: Flask) -> None:
    """Restore the complete historical motor-management UI inside port 5056."""
    from go2_dashboard.motor_health_app import create_motor_health_app

    motor_app = create_motor_health_app()
    for rule in motor_app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        path = "/motors" if rule.rule == "/" else rule.rule
        if rule.rule == "/api/health":
            path = "/api/motor/health"
        endpoint = f"motor_health.{rule.endpoint}"
        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        app.add_url_rule(path, endpoint, motor_app.view_functions[rule.endpoint], methods=methods)


def create_d1_jog_app() -> Flask:
    template_dir = PROJECT_ROOT / "templates"
    static_dir = PROJECT_ROOT / "static"
    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir) if static_dir.is_dir() else None,
        static_url_path="/static",
    )
    front_last_jpg: bytes | None = None
    wrist_last_jpg: bytes | None = None
    front_last_at = 0.0
    wrist_last_at = 0.0
    front_snapshot_lock = threading.Lock()
    auto_calibration_lock = threading.Lock()
    auto_calibration_progress_lock = threading.Lock()
    auto_calibration_progress: dict[str, Any] = {
        "ok": True,
        "running": False,
        "phase": "idle",
        "step": None,
        "max_steps": 48,
        "sample_count": 0,
        "saved": None,
        "reason": None,
        "message": "AUTO non avviata",
        "tags": None,
        "reproj_px": None,
        "residual_m": None,
        "residual_deg": None,
        "max_residual_m": 0.025,
        "max_residual_deg": 6.0,
        "build_ready": False,
        "cal_ok": False,
        "progress_percent": 0,
        "next_action": None,
        "offset_meta": None,
        "updated_at": None,
        "history": [],
    }
    # Stato fase "search": prima di raccogliere sample, il braccio prova alcune
    # pose di vista molto diverse e sceglie quella che vede piu' tag come base
    # per l'orbita di calibrazione (risolve: poca variabilita', polso troppo in basso).
    auto_calibration_search: dict[str, Any] = {
        "active": False,
        "done": False,
        "index": 0,
        "results": [],          # [{"index", "tags", "reproj", "servo_deg"}]
        "best_tags": -1,
        "best_base": None,      # servo_deg della posa migliore
        "session_key": None,    # per resettare a nuova sessione
    }

    def _set_auto_progress(**fields: Any) -> None:
        with auto_calibration_progress_lock:
            auto_calibration_progress.update(fields)
            auto_calibration_progress["updated_at"] = time.time()
            hist = list(auto_calibration_progress.get("history") or [])
            phase = str(auto_calibration_progress.get("phase") or "")
            if phase and phase not in {"idle"}:
                hist.append(
                    {
                        "at": time.strftime("%H:%M:%S"),
                        "phase": phase,
                        "step": auto_calibration_progress.get("step"),
                        "sample_count": auto_calibration_progress.get("sample_count"),
                        "reason": auto_calibration_progress.get("reason"),
                        "message": auto_calibration_progress.get("message"),
                        "residual_m": auto_calibration_progress.get("residual_m"),
                        "residual_deg": auto_calibration_progress.get("residual_deg"),
                        "tags": auto_calibration_progress.get("tags"),
                        "saved": auto_calibration_progress.get("saved"),
                    }
                )
                auto_calibration_progress["history"] = hist[-40:]

    def _auto_progress_from_quality(quality: dict[str, Any] | None) -> dict[str, Any]:
        q = quality if isinstance(quality, dict) else {}
        res = q.get("residual") if isinstance(q.get("residual"), dict) else {}
        return {
            "sample_count": int(q.get("sample_count") or len(grasp6d.list_handeye_samples())),
            "progress_percent": int(q.get("progress_percent") or 0),
            "build_ready": bool(q.get("build_ready")),
            "next_action": q.get("next_action"),
            "residual_m": res.get("translation_rms_m"),
            "residual_deg": res.get("rotation_rms_deg"),
            "max_residual_m": q.get("max_translation_rms_m") or 0.025,
            "max_residual_deg": q.get("max_rotation_rms_deg") or 6.0,
        }

    def _auto_search_offsets() -> list[list[float]]:
        """Pose di vista grossolane per la fase search (offset da scan SX).

        Obiettivo: vista genuinamente diversa (yaw base ampio, braccio piu'
        alto/meno piegato, polso che guarda avanti invece che in basso) per
        trovare dove si vedono piu' tag prima di orbitare per la calibrazione.
        J4 negativo = polso meno puntato in basso.
        """
        return [
            [0, -12, 10, 0, -20, 0, 0],     # arretra + guarda avanti: vede tutta la board
            [-24, -6, 4, 0, -12, 0, 0],     # yaw sx ampio
            [24, -6, 4, 0, -12, 0, 0],      # yaw dx ampio
            [-16, -14, 12, 0, -22, 0, 0],   # yaw sx + arretra alto
            [16, -14, 12, 0, -22, 0, 0],    # yaw dx + arretra alto
            [0, -16, 14, 0, -10, 0, 0],     # molto arretrato/alto (whole board)
            [0, 0, 0, 0, 0, 0, 0],          # scan SX di riferimento
            [0, 8, -8, 0, 12, 0, 0],        # avvicina/basso (fallback board bassa)
        ]

    def _auto_search_enabled() -> bool:
        return os.environ.get("D1_GRASP6D_AUTO_SEARCH_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}

    def _auto_search_reset(session_key: str, base_scan_left: list[float]) -> None:
        # Se ci sono pose di riferimento salvate manualmente, saltiamo la search:
        # l'AUTO le ripete direttamente (replay) come target di raccolta.
        refs = _auto_reference_poses()
        if refs:
            auto_calibration_search.update(
                {
                    "active": False,
                    "done": True,
                    "index": 0,
                    "ref_index": 0,
                    "results": [],
                    "best_tags": -1,
                    "best_base": None,
                    "session_key": session_key,
                    "candidates": [],
                    "use_refs": True,
                }
            )
            return
        offsets = _auto_search_offsets()
        candidates = [service.clamp_servo_deg([base_scan_left[i] + off[i] for i in range(7)]) for off in offsets]
        auto_calibration_search.update(
            {
                "active": True,
                "done": False,
                "index": 0,
                "ref_index": 0,
                "results": [],
                "best_tags": -1,
                "best_base": None,
                "session_key": session_key,
                "candidates": candidates,
                "use_refs": False,
            }
        )

    camera_diag: dict[str, dict[str, Any]] = {
        "wrist": {"index": None, "chroma": None, "rgb_like": False},
        "front": {"index": None, "chroma": None, "rgb_like": False},
    }
    startup_arm_stabilization: dict[str, Any] = {"ok": False, "reason": "not_run"}

    @app.after_request
    def _cors(resp: Response) -> Response:
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Access-Control-Allow-Origin", "*")
            resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
        return resp

    @app.errorhandler(Exception)
    def _api_json_error(exc: Exception) -> tuple[Response, int]:
        if not request.path.startswith("/api/"):
            if isinstance(exc, HTTPException):
                return exc
            raise exc
        return jsonify({"ok": False, "reason": str(exc), "error": type(exc).__name__}), 500

    @app.route("/api/<path:_sub>", methods=["OPTIONS"])
    def _options(_sub: str) -> Response:
        return Response(status=204)

    def _page_ctx(*, dash_mode: str = "arm") -> dict[str, str | int]:
        return {
            "dashboard_port": int(os.environ.get("D1_JOG_PORT", os.environ.get("GO2_DASHBOARD_PORT", "5056"))),
            "d1_arm_host": os.environ.get("D1_ARM_HOST", os.environ.get("SERVO_ARM_HOST", "192.168.123.100")),
            "go2_local": os.environ.get("GO2_LOCAL", "0"),
            "dash_mode": dash_mode,
        }

    @app.route("/")
    def index() -> str:
        return render_template("d1_jog_dashboard.html", **_page_ctx(dash_mode="arm"))

    @app.route("/focus/teach")
    def focus_teach_alias() -> str:
        return render_template("d1_jog_dashboard.html", **_page_ctx(dash_mode="arm"))

    @app.route("/focus/hermes")
    def focus_hermes() -> str:
        return render_template(
            "hermes.html",
            port=int(os.environ.get("D1_JOG_PORT", "5056")),
        )

    @app.route("/api/assets/go2_hero.png")
    @app.route("/assets/unitree_go2_hero.png")
    @app.route("/assets/unitree_robot_main.png")
    def go2_hero_asset() -> Response:
        for path in _GO2_HERO_CANDIDATES:
            if path.is_file():
                return send_file(path, mimetype="image/png", max_age=0)
        return Response(_go2_svg_fallback(label="Frutiger Aero hero card"), mimetype="image/svg+xml")

    @app.route("/api/assets/go2_features.png")
    @app.route("/assets/unitree_go2_features.png")
    def go2_features_asset() -> Response:
        for path in _GO2_FEATURES_CANDIDATES:
            if path.is_file():
                return send_file(path, mimetype="image/png", max_age=0)
        return Response(_go2_svg_fallback(label="Live dashboard visual"), mimetype="image/svg+xml")

    @app.route("/program")
    def program_editor() -> str:
        return render_template("d1_program_editor.html", **_page_ctx(dash_mode="arm"))

    @app.route("/api/motion/status")
    def motion_status() -> Response:
        return jsonify(service.motion_status())

    @app.route("/api/daemon/status")
    def daemon_status() -> Response:
        out = {"ok": True, **service.runtime_safety_status()}
        daemon = out.get("command_daemon") if isinstance(out.get("command_daemon"), dict) else {}
        target = daemon.get("hold_target_servo_deg") if isinstance(daemon, dict) else None
        if not isinstance(target, list):
            last_publish = out.get("last_publish") if isinstance(out.get("last_publish"), dict) else {}
            target = last_publish.get("last_pose_target_servo_deg")
        feedback = service.read_servo_deg(fast=True)
        raw = feedback.get("servo_deg") if feedback.get("ok") else None
        if isinstance(target, list) and isinstance(raw, list) and len(target) >= 7 and len(raw) >= 7:
            errs = [round(abs(float(target[i]) - float(raw[i])), 2) for i in range(7)]
            out["hold_target_error_deg"] = errs
            out["hold_target_max_error_deg"] = max(errs)
            out["feedback"] = {
                "ok": True,
                "servo_deg": raw[:7],
                "dds_counts": feedback.get("dds_counts"),
                "arm_feedback": feedback.get("arm_feedback") or [],
                "arm_feedback_count": feedback.get("arm_feedback_count") or 0,
            }
        else:
            out["feedback"] = feedback
        return jsonify(out)

    @app.route("/api/motion/reset", methods=["POST"])
    def motion_reset() -> Response:
        return jsonify(service.motion_reset())

    @app.route("/api/health")
    def health() -> Response:
        st = service.binaries_status()
        daemon = st.get("command_daemon") or {}
        return jsonify(
            {
                "ok": (
                    st["command_ok"]
                    and st["feedback_ok"]
                    and bool(daemon.get("alive"))
                ),
                "service": "d1_jog_dashboard",
                "started_at": _PROCESS_STARTED,
                "binaries": st,
                "command_daemon": daemon,
                "runtime_safety": service.runtime_safety_status(),
                "startup_arm_stabilization": startup_arm_stabilization,
                "dds_domain": int(os.environ.get("D1_DDS_DOMAIN", os.environ.get("GO2_DDS_DOMAIN", "0"))),
                "dds_interface": (os.environ.get("GO2_DDS_INTERFACE") or os.environ.get("D1_DDS_INTERFACE") or "eth0"),
                "cyclonedds_uri_set": bool((os.environ.get("CYCLONEDDS_URI") or "").strip()),
                "dds_runtime_ok": os.environ.get("D1_DDS_RUNTIME_OK") == "1",
                "dds_runtime_dir": os.environ.get(
                    "D1_DDS_LIB_DIR", "/home/unitree/sdk_reinstall_backup_19700225_160102"
                ),
            }
        )

    def _parse_servo_env(key: str, default_vals: list[float]) -> list[float]:
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            return service.clamp_servo_deg(default_vals)
        try:
            vals = [float(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            return service.clamp_servo_deg(default_vals)
        if len(vals) < 7:
            return service.clamp_servo_deg(default_vals)
        return service.clamp_servo_deg(vals[:7])

    def _scan_side_target(side: str) -> list[float]:
        # Il mapping fisico va verificato sul robot: i riferimenti storici erano invertiti
        # rispetto ai pulsanti UI, quindi li teniamo espliciti qui.
        left_default = [-90.0, 19.2, 26.0, 0.1, 37.8, 0.4, 5.0]
        right_default = [90.0, 19.2, 26.0, 0.1, 37.8, 0.4, 5.0]
        if side == "left":
            return _parse_servo_env("D1_SCAN_LEFT_DEG", left_default)
        return _parse_servo_env("D1_SCAN_RIGHT_DEG", right_default)

    def _safe_transit_target() -> list[float] | None:
        fb = service.read_servo_deg(fast=True)
        base = fb.get("servo_deg") if fb.get("ok") else service.get_servo_cache()
        if not isinstance(base, list) or len(base) < 7:
            return None
        return service.safe_zero_pose_from_servo([float(x) for x in base[:7]])

    def _move_via_safe_transit(target_servo_deg: list[float]) -> dict[str, Any]:
        program_runner.request_stop()
        service.motion_reset()
        program_runner.clear_stop_request()
        transit = _safe_transit_target()
        if transit is None:
            return {
                "ok": False,
                "reason": "safe_transit_unavailable",
                "safety_interlock": True,
                "target_servo_deg": target_servo_deg,
            }
        service._halt_cartesian_stream(wait_idle=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return {"ok": False, "reason": "couple_failed", "coupling": couple, "target_servo_deg": target_servo_deg}
        transit_move = None
        transit_move = _move_to_point_with_busy_recovery(transit)
        if not (transit_move.get("ok") or transit_move.get("skipped")):
            transit_move["target_servo_deg"] = transit
            transit_move["coupling"] = couple
            transit_move["phase"] = "fold_before_rotate"
            return transit_move
        side_transit = transit[:]
        side_transit[0] = float(target_servo_deg[0])
        side_transit_move = _move_to_point_with_busy_recovery(side_transit)
        if not (side_transit_move.get("ok") or side_transit_move.get("skipped")):
            side_transit_move["target_servo_deg"] = side_transit
            side_transit_move["coupling"] = couple
            side_transit_move["phase"] = "rotate_while_folded"
            return side_transit_move
        move = _move_to_point_with_busy_recovery(target_servo_deg)
        move["coupling"] = couple
        move["transit_zero"] = transit
        move["transit_move"] = transit_move
        move["side_transit"] = side_transit
        move["side_transit_move"] = side_transit_move
        move["target_servo_deg"] = target_servo_deg
        return move

    def _move_to_point_with_busy_recovery(target_servo_deg: list[float]) -> dict[str, Any]:
        out = program_runner.move_to_servo_deg_smooth(target_servo_deg)
        if out.get("reason") == "motion_busy:program":
            program_runner.request_stop()
            time.sleep(0.15)
            service.motion_reset()
            program_runner.clear_stop_request()
            out = program_runner.move_to_servo_deg_smooth(target_servo_deg)
            out["recovered_from_busy"] = True
        return out

    def _open_rgb_cap(cv2: Any, idx: int) -> Any:
        cap = cv2.VideoCapture(f"/dev/video{idx}", cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        return cap

    def _read_rgb_frame(cv2: Any, idx: int) -> Any | None:
        cap = _open_rgb_cap(cv2, idx)
        if not cap.isOpened():
            cap.release()
            return None
        best = None
        best_score = -1.0
        try:
            # Let auto-exposure settle: the first valid frame is often very dark.
            warmup_frames = max(3, int(os.environ.get("D1_CAMERA_SNAPSHOT_WARMUP", "3")))
            for _ in range(warmup_frames):
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                chroma = _frame_chroma_bgr(frame)
                brightness = float(frame.mean())
                score = chroma + (0.25 * brightness)
                if score > best_score:
                    best, best_score = frame, score
        finally:
            cap.release()
        return best

    def _front_camera_jpeg() -> bytes | None:
        nonlocal front_last_jpg, front_last_at
        cache_s = max(0.2, float(os.environ.get("D1_CAMERA_SNAPSHOT_CACHE_S", "2.2")))
        if front_last_jpg is not None and time.monotonic() - front_last_at < cache_s:
            return front_last_jpg
        if not front_snapshot_lock.acquire(blocking=False):
            return front_last_jpg
        try:
            try:
                import cv2
            except ImportError:
                return front_last_jpg
            idx = _resolve_realsense_rgb_index(role="front")
            frame = _read_rgb_frame(cv2, idx)
            if frame is None:
                return front_last_jpg
            chroma = _frame_chroma_bgr(frame)
            camera_diag["front"] = {"index": idx, "chroma": round(chroma, 3), "rgb_like": chroma >= 2.5}
            ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok_enc and buf is not None:
                front_last_jpg = buf.tobytes()
                front_last_at = time.monotonic()
            return front_last_jpg
        finally:
            front_snapshot_lock.release()

    def _wrist_camera_jpeg() -> bytes | None:
        nonlocal wrist_last_jpg, wrist_last_at
        cache_s = max(0.2, float(os.environ.get("D1_CAMERA_SNAPSHOT_CACHE_S", "2.2")))
        if wrist_last_jpg is not None and time.monotonic() - wrist_last_at < cache_s:
            return wrist_last_jpg
        # Durante una capture 6D librealsense deve essere l'unico owner della
        # D456. La UI continua a mostrare l'ultimo frame invece di contenderla.
        if wrist_rgbd.capture_active():
            return wrist_last_jpg
        if not wrist_rgbd.try_acquire_camera():
            return wrist_last_jpg
        try:
            try:
                import cv2
            except ImportError:
                return wrist_last_jpg
            idx = _resolve_realsense_rgb_index(role="wrist")
            frame = _read_rgb_frame(cv2, idx)
            if frame is None:
                return wrist_last_jpg
            chroma = _frame_chroma_bgr(frame)
            camera_diag["wrist"] = {"index": idx, "chroma": round(chroma, 3), "rgb_like": chroma >= 2.5}
            ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok_enc and buf is not None:
                wrist_last_jpg = buf.tobytes()
                wrist_last_at = time.monotonic()
            return wrist_last_jpg
        finally:
            wrist_rgbd.release_camera()

    def _front_mjpeg_stream():
        nonlocal front_last_jpg
        try:
            import cv2
        except ImportError:
            return
        idx = _resolve_realsense_rgb_index(role="front")
        period = max(0.05, float(os.environ.get("D1_FRONT_MJPEG_PERIOD_S", "0.10")))
        cap = None
        while True:
            try:
                if cap is None:
                    cap = _open_rgb_cap(cv2, idx)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        if front_last_jpg is not None:
                            yield (
                                b"--frame\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                b"Cache-Control: no-store\r\n\r\n" + front_last_jpg + b"\r\n"
                            )
                        time.sleep(period)
                        continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(period)
                    continue
                chroma = _frame_chroma_bgr(frame)
                camera_diag["front"] = {"index": idx, "chroma": round(chroma, 3), "rgb_like": chroma >= 2.5}
                ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if not ok_enc or buf is None:
                    time.sleep(period)
                    continue
                front_last_jpg = buf.tobytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + front_last_jpg + b"\r\n"
                )
                time.sleep(period)
            except GeneratorExit:
                break
            except Exception:
                if cap is not None:
                    cap.release()
                    cap = None
                time.sleep(period)
        if cap is not None:
            cap.release()

    def _wrist_mjpeg_stream():
        nonlocal wrist_last_jpg
        try:
            import cv2
        except ImportError:
            return
        idx = _resolve_realsense_rgb_index(role="wrist")
        period = max(0.05, float(os.environ.get("D1_ORBBEC_LIVE_HTTP_PERIOD_S", "0.10")))
        cap = None
        while True:
            try:
                if cap is None:
                    cap = _open_rgb_cap(cv2, idx)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        if wrist_last_jpg is not None:
                            yield (
                                b"--frame\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                b"Cache-Control: no-store\r\n\r\n" + wrist_last_jpg + b"\r\n"
                            )
                        time.sleep(period)
                        continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(period)
                    continue
                chroma = _frame_chroma_bgr(frame)
                camera_diag["wrist"] = {"index": idx, "chroma": round(chroma, 3), "rgb_like": chroma >= 2.5}
                ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if not ok_enc or buf is None:
                    time.sleep(period)
                    continue
                wrist_last_jpg = buf.tobytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + wrist_last_jpg + b"\r\n"
                )
                time.sleep(period)
            except GeneratorExit:
                break
            except Exception:
                if cap is not None:
                    cap.release()
                    cap = None
                time.sleep(period)
        if cap is not None:
            cap.release()

    @app.route("/api/front/last.jpg")
    def front_last_jpeg() -> Response:
        jpg = _front_camera_jpeg()
        if jpg is None:
            return Response("front camera unavailable", status=503)
        return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.route("/api/front/live.mjpg")
    def front_live_mjpg() -> Response:
        return Response(
            stream_with_context(_front_mjpeg_stream()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
        )

    @app.route("/api/cameras/rgb_status")
    def cameras_rgb_status() -> Response:
        wrist_idx = _resolve_realsense_rgb_index(role="wrist")
        front_idx = _resolve_realsense_rgb_index(role="front")
        wrist_live = orbbec_capture.live_rgb_status()
        wrist_status = wrist_live if wrist_live.get("chroma") is not None else camera_diag["wrist"]
        return jsonify(
            {
                "ok": True,
                "wrist": {**wrist_status, "index": wrist_idx, "expected_usb_pid": "0b5c"},
                "front": {**camera_diag["front"], "index": front_idx, "expected_usb_pid": "0b3a"},
                "detection_source": "wrist",
                "depth_nodes_allowed_in_ui": False,
            }
        )

    @app.route("/api/base/sport_last", methods=["GET"])
    def api_base_sport_last() -> Response:
        return jsonify(sport_last_payload())

    @app.route("/api/base/accompany_mode", methods=["GET", "POST"])
    def api_base_accompany_mode() -> Response:
        payload, code = accompany_mode_handle(request)
        return jsonify(payload), code

    @app.route("/api/base/move_nudge", methods=["POST"])
    def api_base_move_nudge() -> Response:
        body = request.get_json(silent=True) or {}
        direction = str(body.get("direction") or "forward").strip().lower()
        duration_s = float(body.get("duration_s", 0.35))
        speed = float(body.get("speed", 0.22))
        yaw_speed = float(body.get("yaw_speed", 0.45))
        vectors: dict[str, tuple[float, float, float]] = {
            "forward": (speed, 0.0, 0.0),
            "backward": (-speed, 0.0, 0.0),
            "left": (0.0, speed, 0.0),
            "right": (0.0, -speed, 0.0),
            "turn_left": (0.0, 0.0, yaw_speed),
            "turn_right": (0.0, 0.0, -yaw_speed),
        }
        if direction not in vectors:
            return jsonify({"ok": False, "reason": "unknown_direction"}), 400
        ok_gate, reason = base_motion_allowed()
        if not ok_gate:
            return jsonify({"ok": False, "reason": reason}), 403
        import sys

        scripts_dir = str(PROJECT_ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from go2_accompany import sport_move

        vx, vy, vyaw = vectors[direction]
        out = sport_move(
            project_root=PROJECT_ROOT,
            domain=GO2_DDS_DOMAIN,
            iface=(GO2_DDS_INTERFACE.strip() if GO2_DDS_INTERFACE else None),
            vx=vx,
            vy=vy,
            vyaw=vyaw,
            duration_s=max(0.08, min(1.2, duration_s)),
            stand_first=True,
        )
        out["direction"] = direction
        if not out.get("ok"):
            try:
                from go2_dashboard.go2_motor_sport import invoke_dds_sport_ping

                out["sport_probe"] = invoke_dds_sport_ping()
            except Exception as exc:
                out["sport_probe"] = {"ok": False, "reason": repr(exc)}
        return jsonify(out), (200 if out.get("ok") else 502)

    @app.route("/api/motor/thermal_settings", methods=["GET", "POST"])
    def api_motor_thermal_settings() -> Response:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if "warn_c" in body:
                THERMAL_SETTINGS["warn_c"] = float(body.get("warn_c"))
            if "critical_c" in body:
                THERMAL_SETTINGS["critical_c"] = float(body.get("critical_c"))
            if "auto_crouch" in body:
                THERMAL_SETTINGS["auto_crouch"] = bool(body.get("auto_crouch"))
        return jsonify({"ok": True, **THERMAL_SETTINGS})

    def _read_motor_temperatures_once(timeout_s: float = 2.2) -> dict[str, Any]:
        import sys

        sys.path.insert(0, str(PROJECT_ROOT / "unitree_sdk2_python"))
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

        seen: dict[str, Any] = {"temps": None}

        def cb(msg: Any) -> None:
            vals: list[float] = []
            try:
                # Thermal protection applies only to the 12 Go2 leg joints.
                # D1 arm joints must never be folded into the dog thermal gate.
                for i in range(12):
                    st = msg.motor_state[i]
                    t = None
                    if hasattr(st, "temperature"):
                        t = float(getattr(st, "temperature"))
                    elif hasattr(st, "temp"):
                        t = float(getattr(st, "temp"))
                    if t is None:
                        t = 0.0
                    vals.append(t)
            except Exception:
                vals = []
            if vals:
                seen["temps"] = vals

        if GO2_DDS_INTERFACE:
            ChannelFactoryInitialize(GO2_DDS_DOMAIN, GO2_DDS_INTERFACE)
        else:
            ChannelFactoryInitialize(GO2_DDS_DOMAIN)
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(cb, 8)
        t0 = time.time()
        while time.time() - t0 < timeout_s and seen.get("temps") is None:
            time.sleep(0.08)
        return {"ok": bool(seen.get("temps")), "temps_c": seen.get("temps")}

    @app.route("/api/motor/thermal_status", methods=["GET"])
    def api_motor_thermal_status() -> Response:
        try:
            out = _read_motor_temperatures_once(timeout_s=2.0)
        except Exception as exc:
            return jsonify({"ok": False, "reason": repr(exc), **THERMAL_SETTINGS}), 502
        temps = out.get("temps_c") or []
        max_temp = max(temps) if temps else None
        state = "ok"
        if max_temp is not None and max_temp >= float(THERMAL_SETTINGS["critical_c"]):
            state = "critical"
        elif max_temp is not None and max_temp >= float(THERMAL_SETTINGS["warn_c"]):
            state = "warn"
        return jsonify(
            {
                "ok": out.get("ok", False),
                "state": state,
                "max_temp_c": max_temp,
                "temps_c": temps,
                **THERMAL_SETTINGS,
            }
        )

    @app.route("/api/scan/side_detect", methods=["POST"])
    def scan_side_detect() -> Response:
        body = request.get_json(silent=True) or {}
        side = str(body.get("side") or "right").strip().lower()
        if side not in ("left", "right"):
            return jsonify({"ok": False, "reason": "side_must_be_left_or_right"}), 400
        raw_override = body.get("override_servo_deg")
        if isinstance(raw_override, list) and len(raw_override) >= 7:
            target = service.clamp_servo_deg([float(x) for x in raw_override[:7]])
        else:
            target = _scan_side_target(side)
        program_runner.request_stop()
        service.motion_reset()
        transit = _safe_transit_target()
        if transit is None:
            return jsonify(
                {
                    "ok": False,
                    "reason": "safe_transit_unavailable",
                    "safety_interlock": True,
                    "scan_side": side,
                    "target_servo_deg": target,
                }
            ), 503
        service._halt_cartesian_stream(wait_idle=True)
        program_runner.clear_stop_request()
        fb_before = service.read_servo_deg(fast=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify(
                {
                    "ok": False,
                    "reason": "couple_failed",
                    "coupling": couple,
                    "feedback_before": fb_before,
                    "target_servo_deg": target,
                    "scan_side": side,
                }
            ), 502
        move = None
        transit_move = None
        transit_move = _move_to_point_with_busy_recovery(transit)
        if not (transit_move.get("ok") or transit_move.get("skipped")):
            transit_move["action"] = transit_move.get("action") or "scan_side_transit"
            transit_move["target_servo_deg"] = transit
            transit_move["scan_side"] = side
            transit_move["coupling"] = couple
            transit_move["phase"] = "fold_before_rotate"
            return jsonify(
                {
                    "ok": False,
                    "reason": transit_move.get("reason", "transit_failed"),
                    "scan_side": side,
                    "target_servo_deg": target,
                    "coupling": couple,
                    "feedback_before": fb_before,
                    "transit_zero": transit,
                    "transit_move": transit_move,
                }
            ), 502
        side_transit = transit[:]
        side_transit[0] = float(target[0])
        side_transit_move = _move_to_point_with_busy_recovery(side_transit)
        if not (side_transit_move.get("ok") or side_transit_move.get("skipped")):
            side_transit_move["phase"] = "rotate_while_folded"
            return jsonify(
                {
                    "ok": False,
                    "reason": side_transit_move.get("reason", "side_transit_failed"),
                    "scan_side": side,
                    "target_servo_deg": target,
                    "coupling": couple,
                    "feedback_before": fb_before,
                    "transit_zero": transit,
                    "transit_move": transit_move,
                    "side_transit": side_transit,
                    "side_transit_move": side_transit_move,
                }
            ), 502
        move = _move_to_point_with_busy_recovery(target)
        move["action"] = move.get("action") or "scan_side_move"
        move["target_servo_deg"] = target
        move["scan_side"] = side
        move["transit_zero"] = transit
        move["transit_move"] = transit_move
        move["side_transit"] = side_transit
        move["side_transit_move"] = side_transit_move
        settle_s = float(os.environ.get("D1_SCAN_SIDE_SETTLE_S", "1.1"))
        if settle_s > 0:
            time.sleep(settle_s)
        fb_mid = service.read_servo_deg(fast=True)
        max_error = None
        if isinstance(fb_mid.get("servo_deg"), list) and len(fb_mid["servo_deg"]) >= 7:
            errs = [abs(float(fb_mid["servo_deg"][i]) - float(target[i])) for i in range(7)]
            max_error = max(errs) if errs else None
        move["feedback_mid"] = fb_mid
        move["max_error_deg"] = max_error
        if not (move.get("ok") or move.get("skipped")):
            move["coupling"] = couple
            move["feedback_before"] = fb_before
            return jsonify(move), 502
        force_2d = str(body.get("vision_mode") or "").strip().lower() == "2d"
        if not force_2d and os.environ.get("D1_GRASP6D_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}:
            detect = _capture_grasp6d_plan()
            grasp_estimate = detect.get("grasp_estimate") or {}
        else:
            detect = pick_vision.capture_and_detect()
            _apply_pick_detection_to_preset(detect)
            grasp_estimate = _scan_grasp_estimate(target, detect)
        ok_move = bool(move.get("ok") or move.get("skipped"))
        ok_all = ok_move and bool(detect.get("ok"))
        fb_after = service.read_servo_deg(fast=True)
        return jsonify(
            {
                "ok": ok_all,
                "scan_side": side,
                "target_servo_deg": target,
                "transit_zero": transit,
                "coupling": couple,
                "feedback_before": fb_before,
                "feedback_mid": fb_mid,
                "feedback_after": fb_after,
                "move": move,
                "detection": detect,
                "grasp_estimate": grasp_estimate,
            }
        ), (200 if ok_all else 502)

    @app.route("/assets/<string:name>")
    def dashboard_asset(name: str) -> Response:
        allowed = {
            "unitree_robot_main.png": PROJECT_ROOT / "data" / "unitree_robot_main.png",
            "unitree_go2_hero.png": PROJECT_ROOT / "data" / "unitree_go2_hero.png",
            "unitree_go2_features.png": PROJECT_ROOT / "data" / "unitree_go2_features.png",
        }
        path = allowed.get(name)
        if path is None or not path.is_file():
            return Response("asset not found", status=404)
        return send_file(path, conditional=True, max_age=3600)

    @app.route("/api/joints/feedback")
    def joints_feedback() -> Response:
        return jsonify(service.read_servo_deg())

    @app.route("/api/joints/jog", methods=["POST"])
    def joints_jog() -> Response:
        body = request.get_json(silent=True) or {}
        raw = body.get("servo_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return jsonify({"ok": False, "reason": "servo_deg required (6-7 floats)"}), 400
        try:
            servo = [float(x) for x in raw[:7]]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "servo_deg must be numeric"}), 400
        while len(servo) < 7:
            servo.append(servo[-1])
        if body.get("joint_index") is not None:
            try:
                ji = int(body["joint_index"])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "joint_index_invalid"}), 400
            if ji < 0 or ji > 6:
                return jsonify({"ok": False, "reason": "joint_index_out_of_range"}), 400
            servo = service.merge_single_joint_jog(servo, ji)
        with_enable = bool(body.get("with_enable"))
        if with_enable:
            out = service.jog_with_enable(servo)
        else:
            out = service.jog_pose_deg(servo, keep_lock=bool(body.get("session")))
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/session_begin", methods=["POST"])
    def joints_session_begin() -> Response:
        body = request.get_json(silent=True) or {}
        servo, err = _servo_deg_from_body(body) if body.get("servo_deg") else (None, None)
        if err:
            return jsonify({"ok": False, "reason": err}), 400
        return jsonify(service.joint_control_begin(servo_deg=servo))

    @app.route("/api/joints/session_end", methods=["POST"])
    def joints_session_end() -> Response:
        return jsonify(service.joint_control_end())

    @app.route("/api/joints/hold", methods=["POST"])
    def joints_hold() -> Response:
        body = request.get_json(silent=True) or {}
        raw = body.get("servo_deg")
        servo: list[float] | None = None
        if isinstance(raw, list) and len(raw) >= 6:
            try:
                servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "servo_deg_invalid"}), 400
        out = service.hold_pose_stream(servo_deg=servo)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/hold_now", methods=["POST"])
    def joints_hold_now() -> Response:
        body = request.get_json(silent=True) or {}
        reason = str(body.get("reason") or "ui").strip() or "ui"
        # HOLD ORA utente = hard (power+couple). Abort motion usano soft.
        out = service.request_emergency_hold(reason=reason, hard=True)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/micro_jog", methods=["POST"])
    def joints_micro_jog() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            joint_index = int(body.get("joint_index"))
            delta_deg = float(body.get("delta_deg"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "joint_index_and_delta_deg_required"}), 400
        out = service.micro_jog_current_pose(joint_index=joint_index, delta_deg=delta_deg)
        code = 200 if out.get("ok") or out.get("skipped") else 422
        return jsonify(out), code

    @app.route("/api/joints/enable", methods=["POST"])
    def joints_enable() -> Response:
        body = request.get_json(silent=True) or {}
        mode = int(body.get("mode", 1))
        with_power = bool(body.get("with_power", False))
        out = service.enable_all(mode=mode, with_power=with_power)
        out["funcode"] = "6+5" if with_power else 5
        out["action"] = "enable_motors"
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/power", methods=["POST"])
    def joints_power() -> Response:
        out = service.arm_power_on()
        out["funcode"] = 6
        out["action"] = "arm_power_on"
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/release", methods=["POST"])
    def joints_release() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "RELEASE_ARM_TORQUE":
            return jsonify(
                {
                    "ok": False,
                    "reason": "explicit_release_confirmation_required",
                    "required_confirm": "RELEASE_ARM_TORQUE",
                    "safety": "Release disabilitato per richieste accidentali: toglie coppia e fa cadere il braccio.",
                }
            ), 409
        out = service.motor_release()
        out["funcode"] = 5
        out["action"] = "motor_release"
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/zero", methods=["POST"])
    def joints_zero() -> Response:
        program_runner.clear_stop_request()
        out = service.go_zero()
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/arm/true_zero", methods=["GET", "POST"])
    def arm_true_zero() -> Response:
        if request.method == "GET":
            return jsonify(service.true_zero_pose_info())
        body = request.get_json(silent=True) or {}
        op = str(body.get("op") or body.get("action") or "goto").strip().lower()
        if op in {"save", "memorize", "store"}:
            servo, err = _servo_deg_from_body(body)
            if servo is None:
                return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
            out = service.save_true_zero_pose(servo_deg=servo)
            code = 200 if out.get("ok") else 502
            return jsonify(out), code
        if op in {"goto", "goto_zero", "move", "transit"}:
            program_runner.clear_stop_request()
            out = service.goto_true_zero_pose()
            code = 200 if out.get("ok") or out.get("skipped") else 502
            return jsonify(out), code
        return jsonify({"ok": False, "reason": "unsupported_true_zero_op", "op": op}), 400

    @app.route("/api/arm/status")
    def arm_status() -> Response:
        return jsonify({"ok": True, "arm_coupled": service.arm_coupled()})

    @app.route("/api/arm/couple", methods=["POST"])
    def arm_couple() -> Response:
        body = request.get_json(silent=True) or {}
        feedback = service.read_servo_deg(fast=True)
        if feedback.get("ok") and feedback.get("servo_deg"):
            out = service.couple_and_hold_pose(
                feedback["servo_deg"],
                with_power=bool(body.get("with_power")),
                force=bool(body.get("force", True)),
            )
            out["feedback_before"] = feedback
        else:
            out = service.ensure_coupled(
                with_power=bool(body.get("with_power")),
                force=bool(body.get("force")),
            )
            out["feedback_before"] = feedback
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/arm/maintain", methods=["POST"])
    def arm_maintain() -> Response:
        """Rinnova coppia dopo cambio pagina / movimento — mai release."""
        body = request.get_json(silent=True) or {}
        raw = body.get("servo_deg")
        servo: list[float] | None = None
        if isinstance(raw, list) and len(raw) >= 6:
            try:
                servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "servo_deg_invalid"}), 400
        out = service.hold_pose_stream(servo_deg=servo)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    def _servo_deg_from_body(body: dict) -> tuple[list[float] | None, str | None]:
        raw = body.get("servo_deg")
        if isinstance(raw, list) and len(raw) >= 6:
            try:
                sd = [float(x) for x in raw[:7]]
                while len(sd) < 7:
                    sd.append(sd[-1] if sd else 0.0)
                return service.clamp_servo_deg(sd), None
            except (TypeError, ValueError):
                return None, "servo_deg_invalid"
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return None, str(fb.get("reason", "no_feedback"))
        return fb["servo_deg"], None

    @app.route("/api/cartesian/pose")
    def cartesian_pose() -> Response:
        cached = service.get_servo_cache()
        if cached is not None:
            pose = cartesian.tcp_pose_m(cached)
            return jsonify({"ok": True, "servo_deg": cached, "cached": True, **pose})
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return jsonify({"ok": False, "reason": fb.get("reason", "no_feedback")}), 503
        pose = cartesian.tcp_pose_m(fb["servo_deg"])
        return jsonify({"ok": True, "servo_deg": fb["servo_deg"], **pose})

    @app.route("/api/cartesian/jog_start", methods=["POST"])
    def cartesian_jog_start() -> Response:
        body = request.get_json(silent=True) or {}
        raw_sd = body.get("servo_deg")
        if isinstance(raw_sd, list) and len(raw_sd) >= 6:
            try:
                servo_deg = service.clamp_servo_deg([float(x) for x in raw_sd[:7]])
                err = None
            except (TypeError, ValueError):
                servo_deg, err = None, "servo_deg_invalid"
        else:
            servo_deg, err = None, None
        if servo_deg is None:
            cached = service.get_servo_cache()
            if cached is not None:
                servo_deg = cached
            else:
                fb = service.read_servo_deg(fast=True)
                if not fb.get("ok") or not fb.get("servo_deg"):
                    return jsonify({"ok": False, "reason": fb.get("reason", "no_feedback")}), 503
                servo_deg = fb["servo_deg"]
        try:
            vel = float(body.get("velocity_pct", body.get("speed_pct", 30)))
            max_sp = body.get("max_speed_mm_s")
            max_speed = float(max_sp) if max_sp is not None else None
            acc = body.get("accel_mm_s2")
            dec = body.get("decel_mm_s2")
            accel = float(acc) if acc is not None else None
            decel = float(dec) if dec is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "invalid_numeric_params"}), 400
        out = service.cartesian_begin_jog(
            axis=str(body.get("axis", "x")),
            sign=float(body.get("sign", 1)),
            velocity_pct=vel,
            max_speed_mm_s=max_speed,
            accel_mm_s2=accel,
            decel_mm_s2=decel,
            servo_deg=servo_deg,
        )
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/cartesian/move_tcp", methods=["POST"])
    def cartesian_move_tcp() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            delta_mm = float(body.get("delta_mm", body.get("step_mm", 10)))
            sign = float(body.get("sign", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "delta_mm and sign must be numeric"}), 400
        out = service.cartesian_move_tcp(
            axis=str(body.get("axis", "x")),
            sign=sign,
            delta_mm=delta_mm,
        )
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/cartesian/jog_stop", methods=["POST"])
    def cartesian_jog_stop() -> Response:
        body = request.get_json(silent=True) or {}
        out = service.cartesian_end_jog(hold_after=bool(body.get("hold_after")))
        return jsonify(out)

    @app.route("/api/cartesian/jog_update", methods=["POST"])
    def cartesian_jog_update() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            vel = body.get("velocity_pct", body.get("speed_pct"))
            velocity_pct = float(vel) if vel is not None else None
            max_sp = body.get("max_speed_mm_s")
            max_speed = float(max_sp) if max_sp is not None else None
            acc = body.get("accel_mm_s2")
            dec = body.get("decel_mm_s2")
            accel = float(acc) if acc is not None else None
            decel = float(dec) if dec is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "invalid_numeric_params"}), 400
        out = jog_stream.jog_update(
            velocity_pct=velocity_pct,
            max_speed_mm_s=max_speed,
            accel_mm_s2=accel,
            decel_mm_s2=decel,
        )
        code = 200 if out.get("ok") else 409
        return jsonify(out), code

    @app.route("/api/cartesian/jog_status")
    def cartesian_jog_status() -> Response:
        return jsonify(jog_stream.jog_status())

    @app.route("/api/cartesian/jog_tick", methods=["POST"])
    def cartesian_jog_tick_route() -> Response:
        body = request.get_json(silent=True) or {}
        servo_deg, err = _servo_deg_from_body(body)
        if servo_deg is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        try:
            vel = float(body.get("velocity_pct", body.get("speed_pct", 25)))
            dt_s = float(body.get("dt_s", 0.04))
            max_sp = body.get("max_speed_mm_s")
            max_speed = float(max_sp) if max_sp is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "velocity_pct and dt_s must be numeric"}), 400
        out = cartesian.cartesian_jog_tick(
            servo_deg,
            axis=str(body.get("axis", "x")),
            sign=float(body.get("sign", 1)),
            velocity_pct=vel,
            dt_s=dt_s,
            max_speed_mm_s=max_speed,
        )
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/cartesian/nudge", methods=["POST"])
    def cartesian_nudge() -> Response:
        jog_stream.jog_stop()
        body = request.get_json(silent=True) or {}
        servo_deg, err = _servo_deg_from_body(body)
        if servo_deg is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        axis = str(body.get("axis", "x"))
        sign = body.get("sign", 1)
        step_mm = body.get("step_mm")
        interp = body.get("interpolated")
        try:
            smm = float(step_mm) if step_mm is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "step_mm must be numeric"}), 400
        out = cartesian.cartesian_nudge(
            servo_deg,
            axis=axis,
            sign=float(sign),
            step_mm=smm,
            interpolated=interp if interp is None else bool(interp),
        )
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/programs", methods=["GET"])
    def programs_list() -> Response:
        return jsonify({"ok": True, "programs": program_store.list_programs()})

    @app.route("/api/programs", methods=["POST"])
    def programs_create() -> Response:
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "Programma"))
        prog = program_store.create_program(name)
        return jsonify({"ok": True, "program": prog})

    @app.route("/api/programs/<program_id>", methods=["GET"])
    def programs_get(program_id: str) -> Response:
        prog = program_store.load_program(program_id)
        if prog is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        return jsonify({"ok": True, "program": prog})

    @app.route("/api/programs/<program_id>", methods=["DELETE"])
    def programs_delete(program_id: str) -> Response:
        if program_store.delete_program(program_id):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "reason": "program_not_found"}), 404

    @app.route("/api/programs/<program_id>/waypoints", methods=["POST"])
    def programs_add_waypoint(program_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        servo_deg, err = _servo_deg_from_body(body)
        if servo_deg is None:
            return jsonify({"ok": False, "reason": err or "servo_deg_required"}), 400
        tcp_pose = cartesian.tcp_pose_m(servo_deg)
        prog, wp = program_store.add_waypoint(
            program_id,
            name=str(body.get("name", "")) or None,
            servo_deg=servo_deg,
            tcp_pose=tcp_pose,
        )
        if prog is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        return jsonify({"ok": True, "program": prog, "waypoint": wp})

    @app.route("/api/programs/<program_id>/teach_capture", methods=["POST"])
    def programs_teach_capture(program_id: str) -> Response:
        """Cattura una posa in release, riattiva subito HOLD, poi la persiste.

        L'ordine e' intenzionale: il braccio viene messo in sicurezza prima di
        qualsiasi scrittura su disco. Il publisher resta il daemon hold esterno
        usato da ``service.couple_and_hold_pose``.
        """
        if program_store.load_program(program_id) is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        body = request.get_json(silent=True) or {}
        feedback = service.read_servo_deg(fast=False)
        raw = feedback.get("servo_deg")
        if not feedback.get("ok") or not isinstance(raw, list) or len(raw) < 7:
            return jsonify(
                {
                    "ok": False,
                    "reason": feedback.get("reason", "no_feedback_in_release"),
                    "feedback": feedback,
                    "safety": "Sostieni il braccio e premi HOLD ORA: la posa non e' stata salvata.",
                }
            ), 503
        taught = service.clamp_servo_deg([float(x) for x in raw[:7]])
        hold = service.couple_and_hold_pose(taught, with_power=True, force=True)
        if not (hold.get("ok") or hold.get("skipped")):
            return jsonify(
                {
                    "ok": False,
                    "reason": hold.get("reason", "hold_failed"),
                    "feedback": feedback,
                    "hold": hold,
                    "safety": "Posa non salvata perche' HOLD non e' stato confermato.",
                }
            ), 502
        tcp_pose = cartesian.tcp_pose_m(taught)
        prog, wp = program_store.add_waypoint(
            program_id,
            name=str(body.get("name", "")) or None,
            servo_deg=taught,
            tcp_pose=tcp_pose,
        )
        if prog is None:
            return jsonify(
                {
                    "ok": False,
                    "reason": "program_save_failed_after_hold",
                    "hold": hold,
                    "held_servo_deg": taught,
                }
            ), 500
        return jsonify(
            {
                "ok": True,
                "program": prog,
                "waypoint": wp,
                "held_servo_deg": taught,
                "feedback": feedback,
                "hold": hold,
            }
        )

    @app.route("/api/programs/<program_id>/waypoints/<waypoint_id>", methods=["DELETE"])
    def programs_delete_waypoint(program_id: str, waypoint_id: str) -> Response:
        prog = program_store.delete_waypoint(program_id, waypoint_id)
        if prog is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        return jsonify({"ok": True, "program": prog})

    @app.route("/api/orbbec/capture", methods=["POST"])
    def orbbec_capture_frame() -> Response:
        out = orbbec_capture.capture_orbbec_jpeg()
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/orbbec/live.mjpg")
    def orbbec_live_mjpeg() -> Response:
        period = max(0.20, float(os.environ.get("D1_ORBBEC_LIVE_HTTP_PERIOD_S", "0.35")))

        def generate_fallback():
            last: bytes | None = None
            while True:
                try:
                    cap = orbbec_capture.capture_orbbec_jpeg()
                    path = orbbec_capture.latest_snapshot_path()
                    if cap.get("ok") and path is not None and path.is_file():
                        jpg = path.read_bytes()
                        if jpg:
                            last = jpg
                except Exception:
                    pass
                if last:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + last + b"\r\n"
                    )
                time.sleep(period)

        def generate_combined():
            yielded = False
            for chunk in orbbec_capture.generate_rgb_mjpeg_stream():
                yielded = True
                yield chunk
            if yielded:
                return
            yield from generate_fallback()

        return Response(
            stream_with_context(generate_combined()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
        )

    @app.route("/api/orbbec/last.jpg")
    def orbbec_last_jpeg() -> Response:
        jpg = _wrist_camera_jpeg()
        if jpg is not None:
            return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
        path = orbbec_capture.latest_snapshot_path()
        if path is None:
            return jsonify({"ok": False, "reason": "no_snapshot"}), 404
        return send_file(path, mimetype="image/jpeg", max_age=0)

    @app.route("/api/orbbec/probe")
    def orbbec_probe() -> Response:
        from go2_dashboard.cameras import _v4l_sysfs_card_name

        order = orbbec_capture._v4l_indices_probe_order()
        nodes = [
            {
                "index": idx,
                "sysfs_name": _v4l_sysfs_card_name(idx),
                "ir_sysfs": orbbec_capture._v4l_sysfs_name_is_ir(_v4l_sysfs_card_name(idx)),
            }
            for idx in order
        ]
        chosen = orbbec_capture.resolve_orbbec_rgb_v4l_index(force_probe=True)
        chroma_map = {}
        spread_map = {}
        for idx in orbbec_capture.orbbec_all_v4l_indices():
            spread, chroma = orbbec_capture._probe_index_rgb_quality(idx)
            chroma_map[idx] = round(chroma, 2)
            spread_map[idx] = round(spread, 2)
        return jsonify(
            {
                "ok": chosen is not None,
                "probe_order": order,
                "orbbec_nodes": orbbec_capture.orbbec_all_v4l_indices(),
                "chroma_by_index": chroma_map,
                "spread_by_index": spread_map,
                "min_chroma_rgb": orbbec_capture._orbbec_min_frame_chroma(),
                "min_channel_spread": orbbec_capture._orbbec_min_channel_spread(),
                "nodes": nodes,
                "chosen_v4l_index": chosen,
                "pinned_v4l_index": orbbec_capture._pinned_rgb_v4l_index(),
                "auto_discovery": orbbec_capture._auto_discovery_enabled(),
                "rgb_only": orbbec_capture._rgb_only(),
                "stream_kind": "rgb" if chosen is not None else "none",
            }
        )

    @app.route("/api/pick/preset", methods=["GET"])
    def pick_preset_get() -> Response:
        return jsonify(pick_preset.preset_info())

    @app.route("/api/pick/tuning", methods=["GET", "POST"])
    def pick_tuning() -> Response:
        if request.method == "GET":
            return jsonify(pick_preset.tuning_info())
        body = request.get_json(silent=True) or {}
        try:
            out = pick_preset.set_tuning(body)
        except ValueError as exc:
            return jsonify({"ok": False, "reason": str(exc)}), 400
        return jsonify(out)

    @app.route("/api/pick/preset", methods=["POST"])
    def pick_preset_set() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("from_program"):
            derived = pick_preset.offsets_from_program_waypoints()
            if not derived.get("ok"):
                return jsonify(derived), 404
            info = pick_preset.set_offsets(
                derived["joint_offset_deg"],
                source="program_delta",
            )
            info["derived"] = derived
            return jsonify(info)
        if "manual_orient_offset_deg" in body and body.get("joint_offset_deg") is None:
            try:
                info = pick_preset.set_manual_orient_offset_deg(
                    float(body.get("manual_orient_offset_deg", 0)),
                )
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "manual_orient_offset_deg_invalid"}), 400
            return jsonify(info)
        raw = body.get("joint_offset_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return jsonify({"ok": False, "reason": "joint_offset_deg_required"}), 400
        try:
            off = [float(x) for x in raw[:7]]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "joint_offset_deg_invalid"}), 400
        last_det = body.get("last_detection")
        info = pick_preset.set_offsets(
            off,
            source=str(body.get("source", "manual")),
            last_detection=last_det if isinstance(last_det, dict) else None,
        )
        if "manual_orient_offset_deg" in body:
            try:
                info = pick_preset.set_manual_orient_offset_deg(
                    float(body.get("manual_orient_offset_deg", 0)),
                )
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "manual_orient_offset_deg_invalid"}), 400
        return jsonify(info)

    @app.route("/api/pick/preset/from_pose", methods=["POST"])
    def pick_preset_from_pose() -> Response:
        """Salva offset = posa attuale − SCANSIONE (dopo jog in teach)."""
        body = request.get_json(silent=True) or {}
        servo, err = _servo_deg_from_body(body)
        if servo is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        out = pick_preset.offsets_from_current_vs_scan(servo)
        code = 200 if out.get("ok") else 404
        return jsonify(out), code

    @app.route("/api/pick/teach/samples", methods=["GET", "DELETE"])
    def pick_teach_samples_list() -> Response:
        from go2_dashboard.d1_jog import pick_teach_model

        if request.method == "DELETE":
            return jsonify(pick_teach_model.reset_guided_session())
        return jsonify(pick_teach_model.list_teach_samples())

    @app.route("/api/pick/teach/scan", methods=["POST"])
    def pick_teach_guided_scan() -> Response:
        out = _legacy_scan_and_detect()
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/teach/finish", methods=["POST"])
    def pick_teach_finish() -> Response:
        """Salva un esempio teach (dopo release) e attiva coppia sulla posa insegnata."""
        from go2_dashboard.d1_jog import pick_teach_model

        body = request.get_json(silent=True) or {}
        vis = body.get("vision_at_scan")
        scenario = str(body.get("scenario") or "").strip()
        require_valid_vision = bool(body.get("require_valid_vision"))
        if body.get("servo_deg"):
            servo, err = _servo_deg_from_body(body)
            if servo is None:
                return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
            out = pick_teach_model.finish_teach_sample_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
                taught_servo_deg=servo,
                scenario=scenario,
                require_valid_vision=require_valid_vision,
            )
        else:
            out = pick_teach_model.finish_teach_sample_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
                scenario=scenario,
                require_valid_vision=require_valid_vision,
            )
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/pick/teach/samples/<sample_id>", methods=["DELETE"])
    def pick_teach_sample_delete(sample_id: str) -> Response:
        from go2_dashboard.d1_jog import pick_teach_model

        out = pick_teach_model.delete_teach_sample(sample_id)
        code = 200 if out.get("ok") else 404
        return jsonify(out), code

    @app.route("/api/pick/teach/build_model", methods=["POST"])
    def pick_teach_build_model() -> Response:
        from go2_dashboard.d1_jog import pick_teach_model

        body = request.get_json(silent=True) or {}
        out = pick_teach_model.build_teach_model(require_guided_quality=bool(body.get("guided")))
        code = 200 if out.get("ok") else 400
        return jsonify(out), code

    @app.route("/api/pick/calibrate/zero/finish", methods=["POST"])
    def pick_calibrate_zero_finish() -> Response:
        """Chiude calibrazione: coppia ON (fine task) + offset + riferimento visione."""
        body = request.get_json(silent=True) or {}
        vis = body.get("vision_at_scan")
        if body.get("servo_deg"):
            servo, err = _servo_deg_from_body(body)
            if servo is None:
                return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
            out = pick_preset.finish_zero_calibration_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
                taught_servo_deg=servo,
            )
        else:
            out = pick_preset.finish_zero_calibration_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
            )
        code = 200 if (out.get("ok") or out.get("has_zero_calibration")) else 502
        return jsonify(out), code

    @app.route("/api/pick/preset/nudge", methods=["POST"])
    def pick_preset_nudge() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            joint = int(body.get("joint", body.get("joint_index", 0)))
            delta = float(body.get("delta_deg", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "joint_and_delta_deg_required"}), 400
        if delta == 0:
            return jsonify({"ok": False, "reason": "delta_deg_zero"}), 400
        out = pick_preset.nudge_offsets(joint_index=joint, delta_deg=delta)
        code = 200 if out.get("ok") else 400
        return jsonify(out), code

    def _apply_pick_detection_to_preset(out: dict[str, Any]) -> None:
        if not out.get("ok") or not out.get("last_detection"):
            return
        preset = pick_preset.load_preset()
        if preset.get("joint_offset_deg"):
            pick_preset.set_offsets(
                preset["joint_offset_deg"],
                source=preset.get("source", "unchanged"),
                last_detection=out["last_detection"],
            )
        else:
            derived = pick_preset.offsets_from_program_waypoints()
            if derived.get("ok"):
                pick_preset.set_offsets(
                    derived["joint_offset_deg"],
                    source="program_delta",
                    last_detection=out["last_detection"],
                )

    def _scan_grasp_estimate(scan_servo_deg: list[float], vision: dict[str, Any]) -> dict[str, Any]:
        """Dati leggibili per UI: pixel, posa di presa e distanza solo se calibrata."""
        detection = vision.get("detection") if isinstance(vision.get("detection"), dict) else {}
        last_detection = vision.get("last_detection") if isinstance(vision.get("last_detection"), dict) else {}
        preset = pick_preset.load_preset()
        offsets = pick_preset.effective_joint_offsets(last_detection=last_detection)
        grasp_servo = (
            pick_preset.grasp_servo_from_scan(
                scan_servo_deg,
                offsets=offsets,
                last_detection=last_detection,
            )
            if offsets is not None
            else None
        )
        center = detection.get("grip_center_px") or detection.get("bbox_center_px")
        bbox = detection.get("bbox_xyxy")
        known_width_m = max(0.0, float(os.environ.get("D1_BLUE_BOX_WIDTH_M", "0")))
        focal_px = max(1.0, float(os.environ.get("D1_WRIST_RGB_FX_PX", "615")))
        distance_m = None
        if known_width_m > 0 and isinstance(bbox, list) and len(bbox) >= 4:
            width_px = abs(float(bbox[2]) - float(bbox[0]))
            if width_px >= 2:
                distance_m = round((focal_px * known_width_m) / width_px, 3)
        tcp = None
        if isinstance(grasp_servo, list):
            try:
                from go2_dashboard.d1_jog import cartesian

                tcp = cartesian.tcp_pose_m(grasp_servo)
            except Exception:
                tcp = None
        return {
            "detected": bool(vision.get("detection_ok")),
            "label": detection.get("label"),
            "confidence": detection.get("confidence"),
            "center_px": center,
            "normalized_xy": detection.get("norm"),
            "orientation_deg": detection.get("orientation_deg"),
            "bbox_xyxy": bbox,
            "metric_distance_m": distance_m,
            "metric_distance_available": distance_m is not None,
            "distance_note_it": (
                "Stima pinhole da larghezza scatola calibrata."
                if distance_m is not None
                else "Distanza metrica non disponibile dal solo RGB: configura D1_BLUE_BOX_WIDTH_M o usa depth allineata."
            ),
            "known_box_width_m": known_width_m or None,
            "effective_joint_offsets_deg": offsets,
            "grasp_servo_deg": grasp_servo,
            "grasp_tcp_estimate": tcp,
        }

    def _legacy_scan_and_detect() -> dict[str, Any]:
        """Raggiunge la posa SCANSIONE canonica e acquisisce una detection 2D nuova."""
        found = program_store.find_scan_waypoint()
        if found is None:
            return {"ok": False, "reason": "scan_waypoint_not_found"}
        _program_id, waypoint = found
        raw = waypoint.get("servo_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return {"ok": False, "reason": "invalid_scan_waypoint"}
        scan_servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
        move = _move_via_safe_transit(scan_servo)
        if not (move.get("ok") or move.get("skipped")):
            return {"ok": False, "reason": move.get("reason", "scan_move_failed"), "move": move}
        settle_s = float(os.environ.get("D1_PICK_2D_SCAN_SETTLE_S", "1.0"))
        if settle_s > 0:
            time.sleep(settle_s)
        vision = pick_vision.capture_and_detect()
        _apply_pick_detection_to_preset(vision)
        feedback = service.read_servo_deg(fast=True)
        out = {
            **vision,
            "scan_waypoint": waypoint.get("name"),
            "scan_servo_deg": scan_servo,
            "scan_move": move,
            "scan_feedback": feedback,
            "vision_mode": "2d",
        }
        out["grasp_estimate"] = _scan_grasp_estimate(scan_servo, vision)
        if not vision.get("detection_ok"):
            out["ok"] = False
            out["reason"] = "box_2d_not_detected"
        return out

    @app.route("/api/pick/vision/crop", methods=["GET"])
    def pick_vision_crop_get() -> Response:
        from go2_dashboard.d1_jog import pick_vision_crop

        return jsonify(pick_vision_crop.crop_settings_info())

    @app.route("/api/pick/vision/crop", methods=["POST"])
    def pick_vision_crop_set() -> Response:
        from go2_dashboard.d1_jog import pick_vision_crop

        body = request.get_json(silent=True) or {}
        fr = body.get("crop_fracs") if isinstance(body.get("crop_fracs"), dict) else body
        if not isinstance(fr, dict):
            return jsonify({"ok": False, "reason": "crop_fracs_required"}), 400
        saved = pick_vision_crop.save_crop_fracs(fr)
        return jsonify({"ok": True, **pick_vision_crop.crop_settings_info(), "saved": saved})

    @app.route("/api/pick/vision/crop/preview", methods=["POST"])
    def pick_vision_crop_preview() -> Response:
        """Solo ROI sulla foto salvata (senza YOLO) — per regolare i bordi."""
        from go2_dashboard.d1_jog import pick_vision_crop

        body = request.get_json(silent=True) or {}
        if isinstance(body.get("crop_fracs"), dict):
            pick_vision_crop.save_crop_fracs(body["crop_fracs"])
        snap = orbbec_capture.latest_snapshot_path()
        if snap is None or not snap.is_file():
            return jsonify({"ok": False, "reason": "no_snapshot", "hint": "Fai prima una foto"}), 404
        frame = pick_vision._read_bgr_from_jpeg(snap)
        if frame is None:
            return jsonify({"ok": False, "reason": "decode_failed"}), 502
        _, _, roi = pick_vision_crop.crop_frame_for_detection(frame)
        overlay = pick_vision_crop.draw_crop_roi_outline(frame, roi)
        try:
            import cv2
        except ImportError:
            return jsonify({"ok": False, "reason": "cv2_unavailable"}), 502
        quality = int(os.environ.get("D1_ORBBEC_JPEG_QUALITY", "88"))
        ok_enc, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok_enc or buf is None:
            return jsonify({"ok": False, "reason": "encode_failed"}), 502
        pick_vision.scene_overlay_path().parent.mkdir(parents=True, exist_ok=True)
        pick_vision.scene_overlay_path().write_bytes(buf.tobytes())
        ts = int(time.time())
        return jsonify(
            {
                "ok": True,
                "preview_url": f"/api/pick/scene.jpg?t={ts}",
                "roi_px": list(roi),
                "crop_fracs": pick_vision_crop.vision_crop_fracs(),
            }
        )

    def _public_box6d(box: dict[str, Any]) -> dict[str, Any]:
        out = dict(box)
        # I punti servono al renderer server-side, non al browser: rimuoverli
        # evita risposte JSON di migliaia di righe durante il monitor live.
        out.pop("sample_px_yx", None)
        out.pop("sample_height_m", None)
        for key in ("T_camera_box",):
            value = out.get(key)
            if hasattr(value, "tolist"):
                out[key] = value.tolist()
        plane = out.get("plane")
        if isinstance(plane, dict):
            plane = dict(plane)
            if hasattr(plane.get("normal"), "tolist"):
                plane["normal"] = plane["normal"].tolist()
            out["plane"] = plane
        return out

    def _capture_wrist_rgbd_with_retry(*, median_frames: int | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                if median_frames is None:
                    return wrist_rgbd.capture_aligned()
                return wrist_rgbd.capture_aligned(median_frames=median_frames)
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    continue
                raise
        raise RuntimeError(str(last_exc) if last_exc else "wrist_rgbd_capture_failed")

    def _estimate_grasp6d_box(frame: Any) -> dict[str, Any]:
        depth_box = grasp6d.estimate_box_pose(frame.depth_m, frame.intrinsics)
        if depth_box.get("ok"):
            return depth_box
        if os.environ.get("D1_GRASP6D_RGB_GUIDED_FALLBACK", "1").strip().lower() in {"0", "false", "no", "off"}:
            return depth_box
        try:
            from box_object_detector import detect_box_object

            rgb_det = detect_box_object(frame.color_bgr)
        except Exception as exc:
            out = dict(depth_box)
            out["rgb_guided_error"] = repr(exc)
            return out
        if not rgb_det.get("ok"):
            out = dict(depth_box)
            out["rgb_detection"] = rgb_det
            return out
        guided = grasp6d.estimate_box_pose_rgb_guided(
            frame.depth_m,
            frame.intrinsics,
            rgb_det,
            plane_hint=depth_box.get("plane") if isinstance(depth_box.get("plane"), dict) else None,
        )
        guided["depth_only_reason"] = depth_box.get("reason")
        guided["depth_only_box"] = _public_box6d(depth_box)
        return guided

    def _capture_grasp6d_plan() -> dict[str, Any]:
        try:
            frame = _capture_wrist_rgbd_with_retry()
        except Exception as exc:
            return {
                "ok": False,
                "reason": "wrist_rgbd_capture_failed",
                "detail": str(exc),
                "rgbd": wrist_rgbd.health(),
            }
        box = _estimate_grasp6d_box(frame)
        public_box = _public_box6d(box)
        if not box.get("ok"):
            return {"ok": False, "reason": box.get("reason"), "rgbd": frame.public_info(), "box": public_box}
        feedback = service.read_servo_deg(fast=True)
        if not feedback.get("ok") or not isinstance(feedback.get("servo_deg"), list):
            return {"ok": False, "reason": "arm_feedback_unavailable", "feedback": feedback, "box": public_box}
        plan = grasp6d.plan_grasp(box, current_servo_deg=feedback["servo_deg"])
        return {
            "ok": bool(plan.get("ok")),
            "reason": plan.get("reason"),
            "source": "rgbd_cuboid_6d",
            "rgbd": frame.public_info(),
            "box": public_box,
            "plan": plan,
            "feedback": feedback,
            "grasp_estimate": {
                "detected": bool(box.get("ok")),
                "label": "scatola 3D",
                "metric_distance_m": round(float(box["center_camera_m"][2]), 3),
                "metric_distance_available": True,
                "grasp_servo_deg": ((plan.get("selected") or {}).get("grasp") or {}).get("servo_deg"),
                "grasp_tcp_estimate": {
                    "xyz_m": [
                        row[3]
                        for row in ((plan.get("selected") or {}).get("T_base_grasp") or [[0, 0, 0, 0]] * 4)[:3]
                    ],
                    "frame": "arm_base",
                },
            },
        }

    def _summarize_grasp6d_cluster_observations(observations: list[dict[str, Any]]) -> dict[str, Any]:
        import numpy as np

        reason_counts: dict[str, int] = {}
        valid: list[dict[str, Any]] = []
        for obs in observations:
            reason = str(obs.get("reason") or ("ok" if obs.get("ok") else "unknown"))
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if obs.get("ok"):
                valid.append(obs)
        centers: list[np.ndarray] = []
        rotations: list[np.ndarray] = []
        point_counts: list[int] = []
        for obs in valid:
            selected = ((obs.get("plan") or {}).get("selected") or {})
            raw_t = selected.get("T_base_grasp") or (obs.get("plan") or {}).get("T_base_box")
            try:
                T = np.asarray(raw_t, dtype=float).reshape(4, 4)
            except (TypeError, ValueError):
                continue
            centers.append(T[:3, 3])
            rotations.append(T[:3, :3])
            box = obs.get("box") if isinstance(obs.get("box"), dict) else {}
            try:
                point_counts.append(int(box.get("point_count") or 0))
            except (TypeError, ValueError):
                point_counts.append(0)

        valid_count = len(valid)
        total = len(observations)
        valid_ratio = valid_count / max(total, 1)
        spread_m = None
        rotation_spread_deg = None
        if len(centers) >= 2:
            c = np.stack(centers)
            spread_m = float(np.max(np.linalg.norm(c - np.mean(c, axis=0), axis=1)))
        elif len(centers) == 1:
            spread_m = 0.0
        if len(rotations) >= 2:
            rot_spread = 0.0
            ref = rotations[0]
            for R in rotations[1:]:
                cos_angle = float(np.clip((np.trace(R @ ref.T) - 1.0) * 0.5, -1.0, 1.0))
                rot_spread = max(rot_spread, math.degrees(math.acos(cos_angle)))
            rotation_spread_deg = rot_spread
        elif len(rotations) == 1:
            rotation_spread_deg = 0.0

        min_valid = max(2, int(os.environ.get("D1_GRASP6D_CLUSTER_PROBE_MIN_VALID", "3")))
        max_spread_m = float(os.environ.get("D1_GRASP6D_CLUSTER_PROBE_MAX_SPREAD_M", "0.012"))
        max_rot_deg = float(os.environ.get("D1_GRASP6D_CLUSTER_PROBE_MAX_ROT_DEG", "7.0"))
        stable = bool(
            valid_count >= min_valid
            and spread_m is not None
            and rotation_spread_deg is not None
            and spread_m <= max_spread_m
            and rotation_spread_deg <= max_rot_deg
        )
        return {
            "ok": stable,
            "total_observations": total,
            "valid_observations": valid_count,
            "valid_ratio": round(valid_ratio, 3),
            "reason_counts": reason_counts,
            "spread_m": spread_m,
            "rotation_spread_deg": rotation_spread_deg,
            "point_count_min": min(point_counts) if point_counts else None,
            "point_count_max": max(point_counts) if point_counts else None,
            "thresholds": {
                "min_valid_observations": min_valid,
                "max_spread_m": max_spread_m,
                "max_rotation_spread_deg": max_rot_deg,
            },
            "ready_for_pregrasp": stable,
            "ready_for_execute": stable,
            "next_action": "pregrasp" if stable else "debug_cluster_or_reposition_object",
        }

    def _probe_grasp6d_cluster(*, frames: int, interval_s: float) -> dict[str, Any]:
        observations: list[dict[str, Any]] = []
        for index in range(frames):
            obs = _capture_grasp6d_plan()
            observations.append(
                {
                    "index": index + 1,
                    "ok": bool(obs.get("ok")),
                    "reason": obs.get("reason"),
                    "box": obs.get("box"),
                    "plan": obs.get("plan"),
                    "rgbd": obs.get("rgbd"),
                }
            )
            if index < frames - 1 and interval_s > 0:
                time.sleep(interval_s)
        summary = _summarize_grasp6d_cluster_observations(observations)
        out = {
            "ok": bool(summary.get("ok")),
            "mode": "cluster_probe",
            "summary": summary,
            "observations": observations,
            "safety": {
                "moves_arm": False,
                "uses_cached_pose_for_execute": False,
            },
        }
        _save_grasp6d_run({"at": time.time(), **out})
        return out

    def _grasp6d_debug_jpeg(*, capture_new: bool = True) -> tuple[bytes | None, dict[str, Any]]:
        import cv2
        import numpy as np

        try:
            frame = _capture_wrist_rgbd_with_retry() if capture_new else wrist_rgbd.last_frame()
            if frame is None:
                frame = _capture_wrist_rgbd_with_retry(median_frames=1)
        except Exception as exc:
            return None, {"ok": False, "reason": "wrist_rgbd_capture_failed", "detail": str(exc)}

        box = _estimate_grasp6d_box(frame)
        canvas = frame.color_bgr.copy()
        h, w = canvas.shape[:2]
        fx = float(frame.intrinsics.get("fx", 1.0))
        fy = float(frame.intrinsics.get("fy", 1.0))
        ppx = float(frame.intrinsics.get("ppx", w * 0.5))
        ppy = float(frame.intrinsics.get("ppy", h * 0.5))

        depth = np.asarray(frame.depth_m, dtype=np.float32)
        valid_depth = np.isfinite(depth) & (depth > 0.10) & (depth < 1.5)
        if np.count_nonzero(valid_depth) > 0:
            dvals = depth[valid_depth]
            dmin = float(np.percentile(dvals, 5))
            dmax = float(np.percentile(dvals, 95))
            if dmax <= dmin:
                dmax = dmin + 0.05
            depth_u8 = np.zeros_like(depth, dtype=np.uint8)
            depth_u8[valid_depth] = np.clip(((depth[valid_depth] - dmin) / (dmax - dmin)) * 255.0, 0, 255).astype(np.uint8)
            depth_color = cv2.applyColorMap(depth_u8, cv2.COLORMAP_TURBO)
            depth_color[~valid_depth] = 0
            dh = min(220, h // 2)
            dw = min(300, w // 2)
            inset = cv2.resize(depth_color, (dw, dh), interpolation=cv2.INTER_AREA)
            x0 = max(0, w - dw - 8)
            y0 = 8
            canvas[y0 : y0 + dh, x0 : x0 + dw] = cv2.addWeighted(canvas[y0 : y0 + dh, x0 : x0 + dw], 0.20, inset, 0.80, 0)
            cv2.rectangle(canvas, (x0, y0), (x0 + dw, y0 + dh), (30, 220, 220), 1)
            cv2.putText(canvas, "depth heatmap", (x0 + 8, y0 + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

        debug_points = {"total_depth_points": 0, "above_floor_points": 0, "above_floor_default_points": 0}
        plane_dbg = box.get("plane") if isinstance(box.get("plane"), dict) else {}
        pts, pxs = grasp6d.depth_to_points(depth, frame.intrinsics)
        debug_points["total_depth_points"] = int(len(pts))
        if plane_dbg.get("ok"):
            if len(pts) > 0:
                nrm_raw = plane_dbg.get("normal")
                if isinstance(nrm_raw, np.ndarray):
                    nrm = nrm_raw.astype(float).reshape(3)
                elif isinstance(nrm_raw, list) and len(nrm_raw) >= 3:
                    nrm = np.asarray(nrm_raw[:3], dtype=float)
                else:
                    nrm = np.asarray([0.0, 0.0, -1.0], dtype=float)
                d0 = float(plane_dbg.get("d") or 0.0)
                signed = pts @ nrm + d0
                min_h_default = float((box.get("height_threshold_m") or {}).get("default_min", 0.025))
                min_h_used = float((box.get("height_threshold_m") or {}).get("min", min_h_default))
                max_h_used = float((box.get("height_threshold_m") or {}).get("max", 0.45))
                default_mask = (signed > min_h_default) & (signed < max_h_used)
                used_mask = (signed > min_h_used) & (signed < max_h_used)
                debug_points["above_floor_default_points"] = int(np.count_nonzero(default_mask))
                debug_points["above_floor_points"] = int(np.count_nonzero(used_mask))
                px_used = pxs[used_mask]
                if len(px_used) > 1800:
                    idx = np.linspace(0, len(px_used) - 1, 1800, dtype=int)
                    px_used = px_used[idx]
                for yx in px_used:
                    py = int(float(yx[0]))
                    px = int(float(yx[1]))
                    if 0 <= px < w and 0 <= py < h:
                        canvas[py, px] = (200, 70, 250)
        cv2.putText(canvas, "6D cluster debug", (14, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 220, 40), 2, cv2.LINE_AA)
        cv2.putText(
            canvas,
            f"reason: {box.get('reason', 'ok')}",
            (14, 50),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.56,
            (60, 230, 255),
            2,
            cv2.LINE_AA,
        )

        stride = int(box.get("mask_stride") or 3)
        components = box.get("components") if isinstance(box.get("components"), list) else []
        selected_label = None
        sel = box.get("selected_component")
        if isinstance(sel, dict):
            selected_label = int(sel.get("label") or -1)

        for comp in components:
            if not isinstance(comp, dict):
                continue
            label = int(comp.get("label") or -1)
            cnt = int(comp.get("point_count") or 0)
            bbox = comp.get("mask_bbox_xywh")
            center = comp.get("center_px_yx")
            is_selected = label == selected_label
            color = (40, 220, 40) if is_selected else (40, 160, 255)
            if isinstance(bbox, list) and len(bbox) >= 4:
                x = int(bbox[0] * stride)
                y = int(bbox[1] * stride)
                bw = int(bbox[2] * stride)
                bh = int(bbox[3] * stride)
                cv2.rectangle(canvas, (x, y), (x + bw, y + bh), color, 2)
                cv2.putText(
                    canvas,
                    f"L{label} p{cnt}",
                    (max(2, x), max(14, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    color,
                    1,
                    cv2.LINE_AA,
                )
            if isinstance(center, list) and len(center) >= 2:
                cy = int(float(center[0]))
                cx = int(float(center[1]))
                cv2.drawMarker(canvas, (cx, cy), color, markerType=cv2.MARKER_CROSS, markerSize=12, thickness=2)

        sample_px = box.get("sample_px_yx") if isinstance(box.get("sample_px_yx"), list) else []
        sample_h = box.get("sample_height_m") if isinstance(box.get("sample_height_m"), list) else []
        if sample_px:
            hvals = np.asarray(sample_h if len(sample_h) == len(sample_px) else [0.0] * len(sample_px), dtype=float)
            if hvals.size:
                hmin = float(np.min(hvals))
                hmax = float(np.max(hvals))
                span = max(hmax - hmin, 1e-6)
            else:
                hmin, span = 0.0, 1.0
            for i, p in enumerate(sample_px):
                if not isinstance(p, list) or len(p) < 2:
                    continue
                py = int(float(p[0]))
                px = int(float(p[1]))
                if px < 0 or py < 0 or px >= w or py >= h:
                    continue
                hv = float(hvals[i]) if i < len(hvals) else 0.0
                t = max(0.0, min(1.0, (hv - hmin) / span))
                color = (int(255 * (1.0 - t)), int(220 * t), int(255 * t))
                cv2.circle(canvas, (px, py), 1, color, -1)
            cv2.putText(
                canvas,
                f"point cloud samples: {len(sample_px)}",
                (14, 72),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.53,
                (200, 255, 200),
                2,
                cv2.LINE_AA,
            )

        plan_out: dict[str, Any] | None = None
        if box.get("ok"):
            fb = service.read_servo_deg(fast=True)
            if fb.get("ok") and isinstance(fb.get("servo_deg"), list):
                plan_out = grasp6d.plan_grasp(box, current_servo_deg=fb.get("servo_deg")[:7])
        if plan_out and plan_out.get("ok"):
            selected = (plan_out.get("selected") or {})
            grasp_T = np.asarray(selected.get("T_base_grasp"), dtype=float)
            pre_T = np.asarray(selected.get("T_base_pregrasp"), dtype=float)
            cal = grasp6d.load_calibration()
            fb = service.read_servo_deg(fast=True)
            if cal.get("ok") and fb.get("ok") and isinstance(fb.get("servo_deg"), list):
                q_now = np.radians(np.asarray(fb.get("servo_deg")[:6], dtype=float))
                T_base_tool = grasp6d.fk_tool_transform(q_now)
                T_base_camera = T_base_tool @ np.asarray(cal["T_tool_camera_np"], dtype=float)
                T_camera_base = np.linalg.inv(T_base_camera)

                def _proj(Tb: np.ndarray) -> tuple[int, int] | None:
                    if Tb.shape != (4, 4):
                        return None
                    pc = T_camera_base @ Tb
                    z = float(pc[2, 3])
                    if z <= 0.05:
                        return None
                    u = int(round(fx * float(pc[0, 3]) / z + ppx))
                    v = int(round(fy * float(pc[1, 3]) / z + ppy))
                    if 0 <= u < w and 0 <= v < h:
                        return (u, v)
                    return None

                gp = _proj(grasp_T)
                pp = _proj(pre_T)
                if pp is not None:
                    cv2.drawMarker(canvas, pp, (0, 220, 255), markerType=cv2.MARKER_TILTED_CROSS, markerSize=18, thickness=2)
                    cv2.putText(canvas, "pre", (pp[0] + 6, pp[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1, cv2.LINE_AA)
                if gp is not None:
                    cv2.drawMarker(canvas, gp, (0, 255, 80), markerType=cv2.MARKER_STAR, markerSize=20, thickness=2)
                    cv2.putText(canvas, "grasp", (gp[0] + 6, gp[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 80), 1, cv2.LINE_AA)
                if gp is not None and pp is not None:
                    cv2.line(canvas, pp, gp, (100, 240, 255), 1, cv2.LINE_AA)

        plane = box.get("plane") if isinstance(box.get("plane"), dict) else {}
        depth_valid = float(frame.public_info().get("depth_valid_fraction") or 0.0)
        info = (
            f"depth_valid={depth_valid:.3f} plane_inliers={float(plane.get('inlier_fraction') or 0.0):.3f} "
            f"pts={debug_points.get('total_depth_points',0)} above={debug_points.get('above_floor_points',0)} comps={len(components)}"
        )
        cv2.putText(canvas, info, (14, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1, cv2.LINE_AA)
        if depth_valid < 0.12:
            cv2.putText(canvas, "hint: depth_valid low -> illumina la scena / riduci riflessi / avvicina camera", (14, h - 36), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (70, 230, 255), 1, cv2.LINE_AA)

        # Una heatmap RGB non mostra la geometria. Aggiungiamo due viste
        # metriche della stessa point cloud nel frame camera: X-Z dall'alto e
        # Z-Y laterale. Sono valide anche senza calibrazione hand-eye.
        cloud = np.full((h, w, 3), (8, 27, 40), dtype=np.uint8)
        cv2.putText(cloud, "POINT CLOUD METRICA (frame D456)", (14, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (235, 245, 255), 2, cv2.LINE_AA)
        mid = h // 2
        cv2.line(cloud, (0, mid), (w, mid), (55, 82, 96), 1)
        cv2.putText(cloud, "TOP: X / Z", (14, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (90, 210, 255), 1, cv2.LINE_AA)
        cv2.putText(cloud, "SIDE: Z / Y", (14, mid + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (90, 210, 255), 1, cv2.LINE_AA)
        if len(pts) > 0:
            plot_pts = pts
            if len(plot_pts) > 7000:
                plot_pts = plot_pts[np.linspace(0, len(plot_pts) - 1, 7000, dtype=int)]
            nrm_raw = plane_dbg.get("normal")
            nrm = np.asarray(nrm_raw, dtype=float).reshape(3) if isinstance(nrm_raw, (list, np.ndarray)) else np.asarray([0.0, 0.0, -1.0])
            if plane_dbg.get("ok"):
                signed_plot = plot_pts @ nrm + float(plane_dbg.get("d") or 0.0)
                min_used = float((box.get("height_threshold_m") or {}).get("min", 0.025))
                is_object = signed_plot > min_used
            else:
                is_object = np.zeros(len(plot_pts), dtype=bool)
            x_lim = max(0.18, float(np.percentile(np.abs(plot_pts[:, 0]), 98)))
            z_lo = max(0.10, float(np.percentile(plot_pts[:, 2], 2)))
            z_hi = max(z_lo + 0.10, float(np.percentile(plot_pts[:, 2], 98)))
            y_lo = float(np.percentile(plot_pts[:, 1], 2))
            y_hi = max(y_lo + 0.08, float(np.percentile(plot_pts[:, 1], 98)))

            def _sx(x: float) -> int:
                return int(np.clip((x / (2.0 * x_lim) + 0.5) * (w - 36) + 18, 4, w - 5))

            def _sz(z: float, top: int, bottom: int) -> int:
                return int(np.clip(bottom - (z - z_lo) / (z_hi - z_lo) * (bottom - top), top, bottom))

            def _sy(y: float) -> int:
                return int(np.clip(h - 14 - (y - y_lo) / (y_hi - y_lo) * (h - mid - 48), mid + 34, h - 8))

            for p, obj_pt in zip(plot_pts, is_object):
                color = (70, 235, 115) if obj_pt else (100, 118, 128)
                cv2.circle(cloud, (_sx(float(p[0])), _sz(float(p[2]), 58, mid - 10)), 1, color, -1)
                side_x = int(np.clip(18 + (float(p[2]) - z_lo) / (z_hi - z_lo) * (w - 36), 4, w - 5))
                cv2.circle(cloud, (side_x, _sy(float(p[1]))), 1, color, -1)

            center = box.get("center_camera_m")
            if isinstance(center, list) and len(center) >= 3:
                cx, cy, cz = map(float, center[:3])
                top_p = (_sx(cx), _sz(cz, 58, mid - 10))
                side_p = (int(np.clip(18 + (cz - z_lo) / (z_hi - z_lo) * (w - 36), 4, w - 5)), _sy(cy))
                cv2.drawMarker(cloud, top_p, (0, 255, 255), cv2.MARKER_STAR, 18, 2)
                cv2.drawMarker(cloud, side_p, (0, 255, 255), cv2.MARKER_STAR, 18, 2)
                cv2.putText(cloud, "box/grasp", (top_p[0] + 7, top_p[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1, cv2.LINE_AA)
        else:
            cv2.putText(cloud, "Nessun punto depth valido", (14, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (70, 150, 255), 2, cv2.LINE_AA)
        canvas = np.hstack([canvas, cloud])

        ok_enc, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok_enc or buf is None:
            return None, {"ok": False, "reason": "jpeg_encode_failed", "box": _public_box6d(box), "rgbd": frame.public_info()}
        meta = {
            "ok": bool(box.get("ok")),
            "reason": box.get("reason"),
            "box": _public_box6d(box),
            "rgbd": frame.public_info(),
            "components_count": len(components),
            "debug_points": debug_points,
            "plan": plan_out,
        }
        return bytes(buf), meta

    def _save_grasp6d_run(payload: dict[str, Any]) -> None:
        import json

        path = PROJECT_ROOT / "data" / "d1_grasp6d_last.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def _load_grasp6d_run() -> dict[str, Any]:
        import json

        path = PROJECT_ROOT / "data" / "d1_grasp6d_last.json"
        if not path.is_file():
            return {"ok": False, "reason": "last_run_not_found", "path": str(path)}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "reason": "last_run_read_failed", "detail": str(exc), "path": str(path)}
        return {"ok": True, "path": str(path), "run": payload}

    def _tail_text(path: Path, *, max_chars: int = 12000) -> dict[str, Any]:
        if not path.is_file():
            return {"ok": False, "reason": "log_not_found", "path": str(path)}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "reason": "log_read_failed", "detail": str(exc), "path": str(path)}
        return {
            "ok": True,
            "path": str(path),
            "tail": text[-max_chars:],
            "size_bytes": path.stat().st_size,
        }

    @app.route("/api/pick/rgbd/health", methods=["GET", "POST"])
    def pick_rgbd_health() -> Response:
        capture = request.method == "POST" or request.args.get("capture") in {"1", "true", "yes"}
        out = wrist_rgbd.health(capture=capture)
        return jsonify(out), (200 if out.get("ok") else 503)

    @app.route("/api/pick/metric/preview", methods=["POST"])
    def pick_metric_preview() -> Response:
        out = _capture_grasp6d_plan()
        _save_grasp6d_run({"mode": "preview", "at": time.time(), **out})
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/metric/calibration/sample", methods=["POST"])
    def pick_metric_calibration_sample() -> Response:
        feedback: dict[str, Any] | None = None

        def _hold_current() -> dict[str, Any]:
            nonlocal feedback
            feedback = service.read_servo_deg(fast=False)
            raw_pose = feedback.get("servo_deg") if isinstance(feedback, dict) else None
            if not feedback.get("ok") or not isinstance(raw_pose, list) or len(raw_pose) < 7:
                return {"ok": False, "reason": "arm_feedback_unavailable", "feedback": feedback}
            return service.couple_and_hold_pose(list(raw_pose[:7]), with_power=True, force=True)

        # Poka-yoke: prima congeliamo la posa corrente, poi catturiamo il
        # target. In Release il braccio può cedere durante l'acquisizione RGB-D:
        # sample e FK devono riferirsi alla stessa posa ferma.
        hold = _hold_current()
        if not hold.get("ok"):
            grasp6d.record_calibration_event("sample_failed", reason="hold_failed", hold_ok=False)
            return jsonify({"ok": False, "reason": "hold_before_sample_persist_failed", "hold": hold}), 502

        try:
            frame = _capture_wrist_rgbd_with_retry(median_frames=2)
        except Exception as exc:
            grasp6d.record_calibration_event(
                "sample_failed", reason="wrist_rgbd_capture_failed", hold_ok=bool(hold.get("ok"))
            )
            return jsonify(
                {"ok": False, "reason": "wrist_rgbd_capture_failed", "detail": str(exc), "hold": hold}
            ), 503

        marker = grasp6d.detect_calibration_marker(frame.color_bgr, frame.intrinsics)
        if not marker.get("ok"):
            grasp6d.record_calibration_event(
                "sample_failed", reason=str(marker.get("reason")), hold_ok=True
            )
            return jsonify({**marker, "hold": hold}), 422
        if (
            marker.get("target_type") == "aprilgrid_36h11"
            and marker.get("pose_method") != "tag_corners"
            and os.environ.get("D1_GRASP6D_ALLOW_CENTER_ONLY_CALIB", "0").lower() not in {"1", "true", "yes", "on"}
        ):
            grasp6d.record_calibration_event(
                "sample_failed",
                reason="aprilgrid_corner_pose_required",
                visible_marker_count=marker.get("visible_marker_count"),
                reprojection_rms_px=marker.get("reprojection_rms_px"),
                hold_ok=True,
            )
            return jsonify(
                {
                    **marker,
                    "ok": False,
                    "reason": "aprilgrid_corner_pose_required",
                    "hint": "La calibrazione 6D salva solo pose AprilGrid da corner multi-tag, non fallback tag_centers.",
                    "hold": hold,
                }
            ), 422
        # Scarta i frame DEBOLI: pochi tag o reproiezione alta non devono MAI
        # entrare nel calcolo hand-eye (stesse soglie del badge live e dell'AUTO).
        _min_tags = max(8, int(os.environ.get("D1_GRASP6D_AUTO_MIN_VISIBLE_TAGS", "12")))
        _max_reproj = float(os.environ.get("D1_GRASP6D_AUTO_MAX_REPROJ_PX", "1.15"))
        _tags = int(marker.get("visible_marker_count") or 0)
        _reproj = marker.get("reprojection_rms_px")
        if _tags < _min_tags:
            grasp6d.record_calibration_event(
                "sample_failed", reason="too_few_tags",
                visible_marker_count=_tags, reprojection_rms_px=_reproj, hold_ok=True,
            )
            return jsonify({
                "ok": False, "reason": "too_few_tags",
                "hint": f"Frame scartato: {_tags} tag visti, ne servono almeno {_min_tags}. Riavvicina/riorienta la griglia.",
                "marker": marker, "hold": hold,
            }), 422
        if _reproj is not None and float(_reproj) > _max_reproj:
            grasp6d.record_calibration_event(
                "sample_failed", reason="reprojection_too_high",
                visible_marker_count=_tags, reprojection_rms_px=_reproj, hold_ok=True,
            )
            return jsonify({
                "ok": False, "reason": "reprojection_too_high",
                "hint": f"Frame scartato: reproiezione {float(_reproj):.2f}px > {_max_reproj:.2f}px. Vista instabile o sfocata.",
                "marker": marker, "hold": hold,
            }), 422
        # Posa planare ambigua: la rotazione stimata e' inaffidabile (due
        # soluzioni speculari quasi equivalenti) e falsa la hand-eye. Scarta.
        if bool(marker.get("pose_ambiguous")):
            grasp6d.record_calibration_event(
                "sample_failed", reason="pose_ambiguous",
                visible_marker_count=_tags, reprojection_rms_px=_reproj, hold_ok=True,
            )
            return jsonify({
                "ok": False, "reason": "pose_ambiguous",
                "hint": (
                    f"Frame scartato: posa ambigua (ratio {marker.get('pose_ambiguity_ratio')} < "
                    f"{marker.get('min_ambiguity_ratio')}). Inclina di piu' la griglia rispetto alla camera."
                ),
                "marker": marker, "hold": hold,
            }), 422
        raw = feedback.get("servo_deg") if isinstance(feedback, dict) else None
        import numpy as np

        T_base_tool = grasp6d.fk_tool_transform(np.radians(np.asarray(raw[:6], dtype=float)))
        samples = grasp6d.list_handeye_samples()
        candidate = {
            "T_base_tool": T_base_tool.tolist(),
            "T_camera_target": marker["T_camera_target"],
            "marker": {
                key: marker.get(key)
                for key in (
                    "target_type",
                    "visible_marker_count",
                    "pose_method",
                    "object_point_variant",
                    "reprojection_rms_px",
                    "marker_ids",
                    "object_point_variant",
                )
                if key in marker
            },
        }
        novelty = grasp6d.sample_pose_novelty(samples, candidate)
        if samples and not novelty.get("useful"):
            grasp6d.record_calibration_event(
                "sample_failed",
                reason="pose_too_similar",
                visible_marker_count=marker.get("visible_marker_count"),
                reprojection_rms_px=marker.get("reprojection_rms_px"),
                hold_ok=True,
            )
            return jsonify(
                {
                    "ok": False,
                    "reason": "pose_too_similar",
                    "hint": "Sample non salvato: cambia di più posizione/orientamento prima di riprovare.",
                    "novelty": novelty,
                    "marker": marker,
                    "hold": hold,
                }
            ), 422
        if len(samples) >= 8:
            current_quality = grasp6d.handeye_quality_report(samples)
            candidate_quality = grasp6d.handeye_quality_report(samples + [candidate])
            current_severity = grasp6d.residual_severity(current_quality)
            candidate_severity = grasp6d.residual_severity(candidate_quality)
            improves = (
                current_severity is not None
                and candidate_severity is not None
                and candidate_severity <= current_severity * 0.98
            )
            if not improves and not candidate_quality.get("build_ready"):
                grasp6d.record_calibration_event(
                    "sample_failed",
                    reason="residual_not_improving",
                    visible_marker_count=marker.get("visible_marker_count"),
                    reprojection_rms_px=marker.get("reprojection_rms_px"),
                    hold_ok=True,
                )
                return jsonify(
                    {
                        "ok": False,
                        "reason": "residual_not_improving",
                        "hint": "Sample non salvato: dopo 8 campioni accetto solo viste che migliorano il residuo.",
                        "current_quality": current_quality,
                        "candidate_quality": candidate_quality,
                        "marker": marker,
                        "hold": hold,
                    }
                ), 422
        out = grasp6d.append_handeye_sample(
            T_base_tool,
            np.asarray(marker["T_camera_target"], dtype=float),
            marker=marker,
            servo_deg=[float(x) for x in raw[:7]],
        )
        out["marker"] = marker
        out["rgbd"] = frame.public_info()
        out["hold"] = hold
        grasp6d.record_calibration_event(
            "sample_saved",
            sample_count=int(out.get("sample_count") or 0),
            target_type=marker.get("target_type"),
            marker_id=marker.get("marker_id"),
            visible_marker_count=marker.get("visible_marker_count"),
            reprojection_rms_px=marker.get("reprojection_rms_px"),
            hold_ok=True,
        )
        return jsonify(out)

    @app.route("/api/pick/metric/calibration/probe", methods=["POST"])
    def pick_metric_calibration_probe() -> Response:
        """Verifica il target senza salvare sample e senza comandare il braccio."""
        try:
            frame = _capture_wrist_rgbd_with_retry(median_frames=2)
        except Exception as exc:
            return jsonify({"ok": False, "reason": "wrist_rgbd_capture_failed", "detail": str(exc)}), 503
        marker = grasp6d.detect_calibration_marker(frame.color_bgr, frame.intrinsics)
        marker["rgbd"] = frame.public_info()
        return jsonify(marker), (200 if marker.get("ok") else 422)

    @app.route("/api/pick/metric/calibration/live_status", methods=["POST"])
    def pick_metric_calibration_live_status() -> Response:
        """Stato live: target, posa motori, novità vista e qualità provvisoria."""
        feedback = service.read_servo_deg(fast=True)
        raw = feedback.get("servo_deg") if isinstance(feedback, dict) else None
        if not feedback.get("ok") or not isinstance(raw, list) or len(raw) < 7:
            return jsonify({"ok": False, "reason": "arm_feedback_unavailable", "feedback": feedback}), 503
        import numpy as np

        T_base_tool = grasp6d.fk_tool_transform(np.radians(np.asarray(raw[:6], dtype=float)))
        try:
            frame = _capture_wrist_rgbd_with_retry(median_frames=1)
        except Exception as exc:
            samples = grasp6d.list_handeye_samples()
            return jsonify(
                {
                    "ok": False,
                    "reason": "wrist_rgbd_capture_failed",
                    "detail": str(exc),
                    "feedback": feedback,
                    "quality": grasp6d.handeye_quality_report(samples),
                }
            ), 503
        marker = grasp6d.detect_calibration_marker(frame.color_bgr, frame.intrinsics)
        samples = grasp6d.list_handeye_samples()
        candidate = {
            "T_base_tool": T_base_tool.tolist(),
            "T_camera_target": marker.get("T_camera_target"),
            "marker": {
                key: marker.get(key)
                for key in (
                    "target_type",
                    "visible_marker_count",
                    "pose_method",
                    "reprojection_rms_px",
                    "marker_ids",
                )
                if key in marker
            },
        }
        novelty = (
            grasp6d.sample_pose_novelty(samples, candidate)
            if marker.get("ok")
            else {"ok": False, "reason": marker.get("reason") or "target_not_valid"}
        )
        current_quality = grasp6d.handeye_quality_report(samples)
        candidate_quality = (
            grasp6d.handeye_quality_report(samples + [candidate])
            if marker.get("ok")
            else current_quality
        )
        current_severity = grasp6d.residual_severity(current_quality)
        candidate_severity = grasp6d.residual_severity(candidate_quality)
        residual_improves = None
        if current_severity is not None and candidate_severity is not None:
            residual_improves = bool(candidate_severity <= current_severity * 0.98)
        can_judge_residual = len(samples) >= 8 and residual_improves is not None
        save_recommended = bool(
            marker.get("ok")
            and novelty.get("useful")
            and not (
                marker.get("target_type") == "aprilgrid_36h11"
                and marker.get("pose_method") != "tag_corners"
                and os.environ.get("D1_GRASP6D_ALLOW_CENTER_ONLY_CALIB", "0").lower() not in {"1", "true", "yes", "on"}
            )
            and (not can_judge_residual or residual_improves or candidate_quality.get("build_ready"))
        )
        out = {
            "ok": bool(marker.get("ok") and novelty.get("ok")),
            "marker": marker,
            "novelty": novelty,
            "quality": candidate_quality,
            "current_quality": current_quality,
            "residual_improves": residual_improves,
            "current_residual_severity": current_severity,
            "candidate_residual_severity": candidate_severity,
            "feedback": feedback,
            "rgbd": frame.public_info(),
            "save_recommended": save_recommended,
        }
        return jsonify(out), (200 if marker.get("ok") else 422)

    @app.route("/api/pick/metric/calibration/frame_quality", methods=["POST"])
    def pick_metric_calibration_frame_quality() -> Response:
        """Feedback VELOCE sulla qualita' del frame: solo tag + reproiezione.

        Niente FK/novita'/solve/quality-report: serve per un badge live rapido
        sopra lo stream. Le soglie sono le stesse dell'AUTO, cosi' 'good' qui
        significa 'accettabile anche in automazione'.
        """
        try:
            frame = _capture_wrist_rgbd_with_retry(median_frames=1)
        except Exception as exc:
            return jsonify({"ok": False, "verdict": "bad", "reason": "wrist_rgbd_capture_failed", "detail": str(exc)}), 503
        marker = grasp6d.detect_calibration_marker(frame.color_bgr, frame.intrinsics)
        tags = int(marker.get("visible_marker_count") or 0)
        reproj = marker.get("reprojection_rms_px")
        pose_method = marker.get("pose_method")
        target_type = marker.get("target_type")
        min_tags = max(8, int(os.environ.get("D1_GRASP6D_AUTO_MIN_VISIBLE_TAGS", "12")))
        max_reproj = float(os.environ.get("D1_GRASP6D_AUTO_MAX_REPROJ_PX", "1.15"))
        ok = bool(marker.get("ok"))
        corners_ok = not (
            target_type == "aprilgrid_36h11"
            and pose_method != "tag_corners"
            and os.environ.get("D1_GRASP6D_ALLOW_CENTER_ONLY_CALIB", "0").lower() not in {"1", "true", "yes", "on"}
        )
        reproj_ok = reproj is None or float(reproj) <= max_reproj
        ambiguous = bool(marker.get("pose_ambiguous"))
        if ok and tags >= min_tags and corners_ok and reproj_ok and not ambiguous:
            verdict = "good"
        elif ambiguous:
            # Reproiezione ok ma rotazione ambigua: NON salvare, inclina il target.
            verdict = "warn"
        elif ok and tags >= max(4, min_tags // 2) and corners_ok:
            verdict = "warn"
        else:
            verdict = "bad"
        return jsonify(
            {
                "ok": ok,
                "verdict": verdict,
                "tags": tags,
                "min_tags": min_tags,
                "reprojection_rms_px": reproj,
                "max_reprojection_rms_px": max_reproj,
                "pose_method": pose_method,
                "target_type": target_type,
                "pose_ambiguous": ambiguous,
                "pose_ambiguity_ratio": marker.get("pose_ambiguity_ratio"),
                "reason": "pose_ambiguous" if ambiguous else marker.get("reason"),
            }
        )

    def _auto_calibration_offsets() -> list[list[float]]:
        """Orbit hand-eye pensata per residual cm-level.

        Priorita': rotazione polso (J4/J5) + yaw base (J0) con compensate,
        poi approach in/out (J1/J2). Offset zero solo per il primo sample.
        """
        # Requisito hand-eye (Tsai-Lenz/Daniilidis): rotazioni relative attorno ad
        # assi NON paralleli, angoli ampi, traslazione minima. Quindi ruotiamo i
        # TRE assi del polso J3/J4/J5 (assi distinti) in entrambe le direzioni e in
        # combinazioni diagonali, tenendo J0/J1/J2 quasi fermi (poca traslazione =
        # board resta in frame). J3 prima era sempre 0: asse di rotazione sprecato.
        a = float(os.environ.get("D1_GRASP6D_AUTO_ROT_AMPL_DEG", "24"))
        h = a * 0.6
        return [
            [0, 0, 0, 0, 0, 0, 0],
            # Asse A: pitch polso J4 (entrambe le direzioni, angolo ampio)
            [0, 2, -2, 0, a, 0, 0],
            [0, -2, 2, 0, -a, 0, 0],
            # Asse B: J3 (asse distinto da J4)
            [0, 0, 0, a, 0, 0, 0],
            [0, 0, 0, -a, 0, 0, 0],
            # Asse C: J5 (roll)
            [0, 0, 0, 0, 0, a, 0],
            [0, 0, 0, 0, 0, -a, 0],
            # Diagonali J3+J4 (asse obliquo, non parallelo ai precedenti)
            [0, 1, -1, h, h, 0, 0],
            [0, -1, 1, -h, -h, 0, 0],
            # Diagonali J4+J5
            [0, 1, -1, 0, h, h, 0],
            [0, -1, 1, 0, -h, -h, 0],
            # Diagonali J3+J5
            [0, 0, 0, h, 0, h, 0],
            [0, 0, 0, -h, 0, -h, 0],
            # Tripla J3+J4+J5 (massima diversita' d'asse)
            [0, 1, -1, h, h, h, 0],
            [0, -1, 1, -h, -h, -h, 0],
            # Piccolo yaw base per una direzione di vista extra (traslazione minima)
            [-10, 1, -1, 0, 8, 0, 0],
            [10, 1, -1, 0, -8, 0, 0],
        ]

    def _select_auto_calibration_offset(
        *,
        base: list[float],
        samples: list[dict[str, Any]],
        step: int,
        residual_stuck: bool,
    ) -> tuple[list[float], dict[str, Any]]:
        """Sceglie l'offset con massima distanza dai sample gia' presi (joint space)."""
        bank = _auto_calibration_offsets()
        scale = 1.0
        if residual_stuck:
            scale = float(os.environ.get("D1_GRASP6D_AUTO_STUCK_OFFSET_SCALE", "1.55"))
        existing: list[list[float]] = []
        for sample in samples:
            servo = sample.get("servo_deg")
            if isinstance(servo, list) and len(servo) >= 6:
                existing.append([float(x) for x in servo[:7]])

        best_offset = [0.0] * 7
        best_score = -1.0
        best_index = 0
        for index, raw_off in enumerate(bank):
            if index == 0 and (step > 0 or existing):
                continue
            offset = [float(v) * scale for v in raw_off]
            target = service.clamp_servo_deg([base[i] + offset[i] for i in range(7)])
            # Premia la diversita' d'asse di rotazione (J3/J4/J5), non la traslazione base.
            wrist_boost = 0.35 * (abs(offset[3]) + abs(offset[4]) + abs(offset[5])) + 0.05 * abs(offset[0])
            if not existing:
                score = 1000.0 + wrist_boost + 0.01 * float((index + step) % max(len(bank), 1))
            else:
                min_l2 = min(
                    sum((float(target[j]) - float(prev[j])) ** 2 for j in range(6)) ** 0.5
                    for prev in existing
                )
                score = float(min_l2) + wrist_boost
            # Evita di ripescare sempre lo stesso indice quando i score sono simili.
            score += 0.05 * float((index + step * 3) % max(len(bank), 1))
            if score > best_score:
                best_score = score
                best_offset = offset
                best_index = index
        return best_offset, {
            "offset_index": best_index,
            "offset_score": round(best_score, 3),
            "offset_scale": scale,
            "bank_size": len(bank),
        }

    def _auto_calibration_base_pose() -> list[float]:
        """Auto 6D parte dal preset scan sinistro, non dalla posa corrente casuale."""
        return _scan_side_target("left")

    def _auto_calibration_daemon_ready() -> dict[str, Any]:
        daemon = service.command_daemon_status()
        ok = bool(daemon.get("alive") and daemon.get("ok", True))
        if daemon.get("external"):
            ok = ok and bool(daemon.get("hold_active"))
        heartbeat_age = daemon.get("heartbeat_age_ms")
        if heartbeat_age is not None:
            try:
                ok = ok and float(heartbeat_age) < 1500.0
            except (TypeError, ValueError):
                ok = False
        return {
            "ok": ok,
            "daemon": daemon,
            "reason": None if ok else "hold_daemon_not_ready_for_auto_calibration",
        }

    CALIB_REFS_PATH = grasp6d.HAND_EYE_SAMPLES_PATH.parent / "d1_grasp6d_calibration_refs.json"

    def _auto_reference_poses() -> list[dict[str, Any]]:
        """Pose di calibrazione salvate manualmente come riferimento per l'AUTO."""
        try:
            data = json.loads(CALIB_REFS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        refs = data.get("refs") if isinstance(data, dict) else None
        out: list[dict[str, Any]] = []
        if isinstance(refs, list):
            for r in refs:
                servo = r.get("servo_deg") if isinstance(r, dict) else None
                if isinstance(servo, list) and len(servo) >= 6:
                    out.append(r)
        return out

    def _save_calibration_refs(max_trans_err_m: float, max_rot_err_deg: float) -> dict[str, Any]:
        """Salva i sample buoni (errore sotto soglia) come pose di riferimento."""
        samples = grasp6d.list_handeye_samples()
        quality = grasp6d.handeye_quality_report(samples)
        debug = quality.get("sample_debug") if isinstance(quality, dict) else None
        refs: list[dict[str, Any]] = []
        for row in (debug or []):
            servo = row.get("servo_deg")
            if not isinstance(servo, list) or len(servo) < 6:
                continue
            terr = row.get("translation_error_m")
            rerr = row.get("rotation_error_deg")
            # Se non c'e' residuo per-sample (pochi sample), salva comunque la posa.
            good = True
            if terr is not None and float(terr) > max_trans_err_m:
                good = False
            if rerr is not None and float(rerr) > max_rot_err_deg:
                good = False
            if good:
                refs.append({
                    "servo_deg": [float(x) for x in servo[:7]],
                    "visible_marker_count": row.get("visible_marker_count"),
                    "translation_error_m": terr,
                    "rotation_error_deg": rerr,
                })
        CALIB_REFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ok": True,
            "count": len(refs),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "residual": quality.get("residual") if isinstance(quality, dict) else None,
            "refs": refs,
        }
        tmp = CALIB_REFS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(CALIB_REFS_PATH)
        return payload

    def _append_current_ref(visible_marker_count: int) -> dict[str, Any]:
        """Aggiunge la posa motori CORRENTE come riferimento AUTO (dedup)."""
        feedback = service.read_servo_deg(fast=True)
        servo = feedback.get("servo_deg") if isinstance(feedback, dict) else None
        if not feedback.get("ok") or not isinstance(servo, list) or len(servo) < 6:
            return {"ok": False, "reason": "arm_feedback_unavailable"}
        servo = [float(x) for x in servo[:7]]
        refs = _auto_reference_poses()
        for r in refs:
            existing = r.get("servo_deg") or []
            if len(existing) >= 6 and max(abs(float(existing[i]) - servo[i]) for i in range(6)) < 2.0:
                return {"ok": True, "count": len(refs), "duplicate": True}
        refs.append({
            "servo_deg": servo,
            "visible_marker_count": visible_marker_count or None,
            "translation_error_m": None,
            "rotation_error_deg": None,
        })
        CALIB_REFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ok": True,
            "count": len(refs),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "refs": refs,
        }
        tmp = CALIB_REFS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(CALIB_REFS_PATH)
        return {"ok": True, "count": len(refs), "duplicate": False}

    @app.route("/api/pick/metric/calibration/refs", methods=["GET", "POST", "DELETE"])
    def pick_metric_calibration_refs() -> Response:
        if request.method == "DELETE":
            try:
                CALIB_REFS_PATH.unlink(missing_ok=True)
            except OSError as exc:
                return jsonify({"ok": False, "reason": "refs_delete_failed", "detail": str(exc)}), 500
            return jsonify({"ok": True, "count": 0})
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if body.get("append_current"):
                out = _append_current_ref(int(body.get("visible_marker_count") or 0))
                if not out.get("ok"):
                    return jsonify(out), 503
                grasp6d.record_calibration_event("calibration_ref_appended", count=out.get("count"))
                return jsonify(out)
            max_t = float(body.get("max_translation_error_m") or os.environ.get("D1_GRASP6D_REF_MAX_TRANS_ERR_M", "0.03"))
            max_r = float(body.get("max_rotation_error_deg") or os.environ.get("D1_GRASP6D_REF_MAX_ROT_ERR_DEG", "8.0"))
            out = _save_calibration_refs(max_t, max_r)
            grasp6d.record_calibration_event("calibration_refs_saved", count=out.get("count"))
            return jsonify(out)
        refs = _auto_reference_poses()
        return jsonify({"ok": True, "count": len(refs), "refs": refs})

    @app.route("/api/pick/metric/calibration/auto_progress", methods=["GET"])
    def pick_metric_calibration_auto_progress() -> Response:
        """Stato live AUTO (anche se lo step e' partito da script/agente, non solo UI)."""
        with auto_calibration_progress_lock:
            snap = dict(auto_calibration_progress)
            snap["history"] = list(auto_calibration_progress.get("history") or [])
        snap["lock_busy"] = bool(auto_calibration_lock.locked())
        snap["search"] = {
            "active": bool(auto_calibration_search.get("active")),
            "done": bool(auto_calibration_search.get("done")),
            "index": int(auto_calibration_search.get("index") or 0),
            "total": len(auto_calibration_search.get("candidates") or []),
            "best_tags": int(auto_calibration_search.get("best_tags") or 0) if auto_calibration_search.get("best_tags", -1) >= 0 else None,
        }
        cal = grasp6d.load_calibration()
        snap["cal_ok"] = bool(cal.get("ok"))
        if snap.get("sample_count") is None:
            snap["sample_count"] = len(grasp6d.list_handeye_samples())
        return jsonify(snap)

    @app.route("/api/pick/metric/calibration/auto_step", methods=["POST"])
    def pick_metric_calibration_auto_step() -> Response:
        """Esegue un solo step auto-calibrazione, mai un loop cieco server-side."""
        if os.environ.get("D1_GRASP6D_AUTO_MOTION_ENABLE", "0").strip().lower() not in {"1", "true", "yes", "on"}:
            grasp6d.record_calibration_event(
                "auto_motion_disabled",
                reason="disabled_until_d1_command_flow_is_stable",
                hold_ok=None,
            )
            return jsonify(
                {
                    "ok": False,
                    "reason": "auto_motion_disabled_until_command_flow_stable",
                    "safety": "Auto-calibrazione con movimento disabilitata: usare calibrazione assistita/manuale finche' il flusso DDS/daemon non e' stabilizzato.",
                    "required_env": "D1_GRASP6D_AUTO_MOTION_ENABLE=1",
                }
            ), 409
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "AUTO_CALIBRATE_6D":
            return jsonify({"ok": False, "reason": "explicit_auto_calibration_confirmation_required"}), 400
        if not auto_calibration_lock.acquire(blocking=False):
            _set_auto_progress(
                running=True,
                phase="busy",
                message="Step AUTO gia' in corso (attendi)",
                reason="auto_calibration_step_already_running",
            )
            return jsonify({"ok": False, "reason": "auto_calibration_step_already_running"}), 409

        @after_this_request
        def _release_auto_calibration_lock(resp: Response) -> Response:
            try:
                with auto_calibration_progress_lock:
                    still_running = bool(auto_calibration_progress.get("phase") in {
                        "moving", "settling", "capturing", "evaluating", "pruning", "starting",
                    })
                    if still_running:
                        auto_calibration_progress["running"] = False
                        if auto_calibration_progress.get("phase") not in {"done", "error", "saved", "skipped"}:
                            auto_calibration_progress["phase"] = "idle_wait"
            finally:
                auto_calibration_lock.release()
            return resp

        import numpy as np

        daemon_ready = _auto_calibration_daemon_ready()
        if not daemon_ready.get("ok"):
            _set_auto_progress(
                running=False,
                phase="error",
                reason=daemon_ready.get("reason") or "hold_daemon_not_ready",
                message="HOLD/daemon non pronto",
            )
            return jsonify({"ok": False, **daemon_ready}), 503

        step = max(0, int(body.get("step") or 0))
        max_samples = max(8, min(20, int(body.get("max_samples") or 16)))
        requested_base = body.get("base_servo_deg")
        base = _auto_calibration_base_pose()
        samples = grasp6d.list_handeye_samples()
        min_n = grasp6d.calib_min_samples()
        # Pool target: accumula viste diverse prima di potare verso i migliori.
        collect_target = max(min_n + 6, int(os.environ.get("D1_GRASP6D_AUTO_COLLECT_TARGET", "12")))
        current_quality = grasp6d.handeye_quality_report(samples)
        _set_auto_progress(
            running=True,
            phase="starting",
            step=step,
            max_steps=48,
            message=f"Step {step + 1}: avvio",
            reason=None,
            saved=None,
            tags=None,
            reproj_px=None,
            offset_meta=None,
            **_auto_progress_from_quality(current_quality),
        )

        # --- Rilevamento BLOCCO + re-search automatico ---------------------
        # Se non aggiungiamo sample da troppi step (braccio bloccato su una
        # vista con pochi tag / residuo alto), rifacciamo la SEARCH per
        # spostarlo su viste nuove. Se il residuo e' catastrofico con sample
        # degeneri, azzeriamo i sample e ripartiamo puliti. Funziona anche
        # senza refresh del client (server-side).
        new_session = bool(body.get("new_session")) or step == 0
        # Non contare i passi di SEARCH come "nessun progresso": la search non
        # salva sample per definizione, altrimenti si riavvia in loop.
        search_busy = bool(auto_calibration_search.get("active") and not auto_calibration_search.get("done"))
        prev_n = auto_calibration_search.get("last_sample_count")
        if prev_n is None or int(prev_n) != len(samples):
            auto_calibration_search["last_sample_count"] = len(samples)
            auto_calibration_search["steps_since_progress"] = 0
        elif not search_busy:
            auto_calibration_search["steps_since_progress"] = int(auto_calibration_search.get("steps_since_progress") or 0) + 1
        steps_stuck = int(auto_calibration_search.get("steps_since_progress") or 0)
        reset_after = max(2, int(os.environ.get("D1_GRASP6D_AUTO_RESEARCH_AFTER_STUCK", "3")))
        _res = current_quality.get("residual") if isinstance(current_quality.get("residual"), dict) else {}
        _trans_m = _res.get("translation_rms_m")
        _rot_deg = _res.get("rotation_rms_deg")
        _tmax = float(current_quality.get("max_translation_rms_m") or 0.025)
        _rmax = float(current_quality.get("max_rotation_rms_deg") or 6.0)
        catastrophic = bool(
            (_trans_m is not None and float(_trans_m) > 2.0 * _tmax)
            or (_rot_deg is not None and float(_rot_deg) > 2.0 * _rmax)
        )
        force_research = (not new_session) and (not search_busy) and _auto_search_enabled() and steps_stuck >= reset_after
        reset_on_stuck = os.environ.get("D1_GRASP6D_AUTO_RESET_ON_STUCK", "1").strip().lower() in {"1", "true", "yes", "on"}
        if new_session or force_research:
            # Wipe SOLO all'avvio di una nuova sessione con residuo catastrofico
            # (sample vecchi degeneri). MAI in mezzo alla sessione: azzerare in
            # continuazione impedisce di accumulare sample e di far scendere il
            # residuo con il pruning degli outlier.
            if new_session and catastrophic and reset_on_stuck and len(samples) >= min_n:
                try:
                    grasp6d.HAND_EYE_SAMPLES_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
                grasp6d.record_calibration_event(
                    "auto_samples_reset_catastrophic",
                    reason="degenerate_residual_restart_with_search",
                    translation_rms_m=_trans_m,
                    rotation_rms_deg=_rot_deg,
                    steps_stuck=steps_stuck,
                )
                samples = grasp6d.list_handeye_samples()
                current_quality = grasp6d.handeye_quality_report(samples)
            elif force_research and len(samples) > min_n:
                # Bloccato: elimina gli outlier peggiori (foto che non migliorano)
                # e cambia vista, ma NON azzerare tutto.
                pruned_stuck = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=3, force_drop=1)
                samples = grasp6d.list_handeye_samples()
                current_quality = grasp6d.handeye_quality_report(samples)
                grasp6d.record_calibration_event(
                    "auto_prune_on_stuck",
                    reason="drop_worst_and_relocate_view",
                    dropped=len(pruned_stuck.get("dropped") or []) if isinstance(pruned_stuck, dict) else None,
                    steps_stuck=steps_stuck,
                    hold_ok=True,
                )
            if _auto_search_enabled():
                _auto_search_reset(session_key=f"s{int(time.time())}", base_scan_left=base)
                auto_calibration_search["steps_since_progress"] = 0
                auto_calibration_search["last_sample_count"] = len(samples)
                grasp6d.record_calibration_event(
                    "auto_research_triggered",
                    reason="new_session" if new_session else "stuck_low_progress",
                    steps_stuck=steps_stuck,
                    catastrophic=catastrophic,
                    hold_ok=True,
                )
                _set_auto_progress(
                    phase="search",
                    running=True,
                    message=("Nuova sessione: cerco la vista migliore" if new_session else f"Bloccato da {steps_stuck} step: cambio vista (tengo i sample buoni)"),
                    **_auto_progress_from_quality(current_quality),
                )

        if current_quality.get("build_ready"):
            built = grasp6d.build_handeye_calibration(samples)
            _set_auto_progress(
                running=False,
                phase="done",
                reason="already_build_ready",
                message="Calibrazione gia' pronta (cal_ok)",
                cal_ok=bool(built.get("ok")),
                saved=False,
                **_auto_progress_from_quality(current_quality),
            )
            return jsonify({"ok": True, "done": True, "reason": "already_build_ready", "build": built, "quality": current_quality})
        # Residual alto / pieno: prune automatico e continua (non chiudere come fallimento).
        pre_prune = None
        next_act = str(current_quality.get("next_action") or "")
        residual_stuck = next_act in {
            "prune_and_aggiungi_sample",
            "residuo_alto_non_calcolare",
            "sessione_incoerente_reset",
        }
        if residual_stuck and len(samples) >= collect_target:
            _set_auto_progress(
                phase="pruning",
                message=f"Step {step + 1}: prune outlier (residuo alto)",
                **_auto_progress_from_quality(current_quality),
            )
            pre_prune = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=4, force_drop=1)
            samples = grasp6d.list_handeye_samples()
            current_quality = grasp6d.handeye_quality_report(samples)
            if current_quality.get("build_ready") or (pre_prune.get("build") or {}).get("ok"):
                built = grasp6d.build_handeye_calibration(samples)
                if built.get("ok"):
                    _set_auto_progress(
                        running=False,
                        phase="done",
                        reason="build_ok_after_auto_prune",
                        message="cal_ok dopo prune automatico",
                        cal_ok=True,
                        **_auto_progress_from_quality(current_quality),
                    )
                    return jsonify(
                        {
                            "ok": True,
                            "done": True,
                            "reason": "build_ok_after_auto_prune",
                            "build": built,
                            "quality": current_quality,
                            "prune": pre_prune,
                        }
                    )
        if len(samples) >= max_samples:
            # Fai spazio e continua il movimento: mai ok:false qui (la UI si fermerebbe).
            pre_prune = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=6, force_drop=3)
            samples = grasp6d.list_handeye_samples()
            current_quality = grasp6d.handeye_quality_report(samples)
            built = grasp6d.build_handeye_calibration(samples)
            if built.get("ok"):
                return jsonify(
                    {
                        "ok": True,
                        "done": True,
                        "reason": "build_ok_after_max_samples_prune",
                        "build": built,
                        "quality": current_quality,
                        "prune": pre_prune,
                    }
                )
            # Ancora pieno: forza altri drop finche' c'e' slot, poi fall-through al move.
            guard = 0
            while len(samples) >= max_samples and len(samples) > min_n and guard < 6:
                pre_prune = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=2, force_drop=1)
                samples = grasp6d.list_handeye_samples()
                current_quality = grasp6d.handeye_quality_report(samples)
                guard += 1
            # Se proprio non si puo' scendere sotto max_samples, alza il tetto locale di 1
            # e continua comunque (meglio sample in piu' che stop AUTO).
            if len(samples) >= max_samples:
                max_samples = min(20, len(samples) + 1)

        from go2_dashboard.d1_jog import motion_profile

        # --- Fase SEARCH: la (ri)attivazione e' gia' decisa sopra (nuova
        # sessione o blocco). Qui si esegue soltanto lo step di search. ---
        search_active = bool(
            _auto_search_enabled()
            and auto_calibration_search.get("active")
            and not auto_calibration_search.get("done")
        )
        search_candidates = auto_calibration_search.get("candidates") or []
        if search_active and int(auto_calibration_search.get("index") or 0) >= len(search_candidates):
            auto_calibration_search["active"] = False
            auto_calibration_search["done"] = True
            search_active = False
        # Search finita: orbita attorno alla posa migliore trovata (non scan SX).
        if (
            not search_active
            and auto_calibration_search.get("done")
            and isinstance(auto_calibration_search.get("best_base"), list)
        ):
            base = list(auto_calibration_search["best_base"])

        if search_active:
            idx = int(auto_calibration_search.get("index") or 0)
            best_so_far = max(0, int(auto_calibration_search.get("best_tags") or 0))
            target = service.clamp_servo_deg(list(search_candidates[idx]))
            offset = [float(target[i]) - float(base[i]) for i in range(7)]
            offset_meta = {
                "search": True,
                "search_index": idx,
                "search_total": len(search_candidates),
                "best_tags": best_so_far,
            }
            _set_auto_progress(
                phase="planning",
                message=f"Search {idx + 1}/{len(search_candidates)}: provo vista (best {best_so_far} tag finora)",
                offset_meta=offset_meta,
                **_auto_progress_from_quality(current_quality),
            )
        else:
            # REPLAY riferimenti: se ci sono pose validate manualmente, l'AUTO le
            # ripete esattamente (target assoluto) per raccogliere i sample nei
            # punti buoni. Esaurite, passa all'orbita attorno all'ultimo.
            using_reference = False
            _refs = _auto_reference_poses() if auto_calibration_search.get("use_refs") else []
            _ref_idx = int(auto_calibration_search.get("ref_index") or 0)
            if _refs and _ref_idx < len(_refs):
                ref_servo = [float(x) for x in _refs[_ref_idx]["servo_deg"][:7]]
                target = service.clamp_servo_deg(ref_servo)
                offset = [float(target[i]) - float(base[i]) for i in range(7)]
                auto_calibration_search["ref_index"] = _ref_idx + 1
                offset_meta = {"reference": True, "ref_index": _ref_idx, "ref_total": len(_refs)}
                using_reference = True
                _set_auto_progress(
                    phase="planning",
                    message=f"Step {step + 1}: replay riferimento {_ref_idx + 1}/{len(_refs)}",
                    offset_meta=offset_meta,
                    **_auto_progress_from_quality(current_quality),
                )
            else:
                offset, offset_meta = _select_auto_calibration_offset(
                    base=base,
                    samples=samples,
                    step=step,
                    residual_stuck=residual_stuck,
                )
                _set_auto_progress(
                    phase="planning",
                    message=f"Step {step + 1}: scelgo posa #{offset_meta.get('offset_index')} (score {offset_meta.get('offset_score')})",
                    offset_meta=offset_meta,
                    **_auto_progress_from_quality(current_quality),
                )
                target = service.clamp_servo_deg([base[i] + offset[i] for i in range(7)])

        max_delta = max(abs(float(target[i]) - float(base[i])) for i in range(6))
        if search_active:
            max_delta_allowed = float(os.environ.get("D1_GRASP6D_AUTO_SEARCH_MAX_DELTA_DEG", "30"))
        elif not search_active and offset_meta.get("reference"):
            # Pose validate e raggiungibili: consenti escursione ampia per centrarle.
            max_delta_allowed = float(os.environ.get("D1_GRASP6D_AUTO_REF_MAX_DELTA_DEG", "70"))
        else:
            max_delta_allowed = float(os.environ.get("D1_GRASP6D_AUTO_MAX_DELTA_DEG", "24"))
        if max_delta > max_delta_allowed:
            # Clamp soft verso la soglia invece di abortire lo step.
            shrink = max_delta_allowed / max(max_delta, 1e-6)
            offset = [float(v) * shrink for v in offset]
            target = service.clamp_servo_deg([base[i] + offset[i] for i in range(7)])
            offset_meta = {**offset_meta, "delta_clamped": True, "max_delta_deg": round(max_delta, 2)}

        pre_feedback = service.read_servo_deg(fast=False)
        pre_raw = pre_feedback.get("servo_deg") if pre_feedback.get("ok") else None
        if not isinstance(pre_raw, list) or len(pre_raw) < 7:
            return jsonify(
                {"ok": False, "reason": "feedback_before_move_unavailable", "feedback": pre_feedback, "daemon": daemon_ready.get("daemon")}
            ), 503
        # Niente couple/power a ogni step se HOLD e' gia' attivo (flood = cedimento).
        if service.arm_coupled() and daemon_ready.get("ok"):
            pre_hold = {"ok": True, "skipped": True, "reason": "already_holding_soft_path"}
        else:
            pre_hold = service.couple_and_hold_pose(list(pre_raw[:7]), with_power=True, force=True, acquire_lock=False)
            if not pre_hold.get("ok"):
                return jsonify({"ok": False, "reason": "pre_move_hold_failed", "hold": pre_hold, "daemon": daemon_ready.get("daemon")}), 502
        daemon_ready = _auto_calibration_daemon_ready()
        if not daemon_ready.get("ok"):
            return jsonify({"ok": False, **daemon_ready, "pre_hold": pre_hold}), 503

        program_runner.request_stop()
        program_runner.clear_stop_request()
        service._halt_cartesian_stream(wait_idle=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify({"ok": False, "reason": "couple_failed", "coupling": couple}), 502
        tracking_limit = max(4.0, float(os.environ.get("D1_GRASP6D_AUTO_TRACKING_MAX_ERR_DEG", "15")))
        tracking_violation_limit = max(1, int(os.environ.get("D1_GRASP6D_AUTO_TRACKING_MAX_VIOLATIONS", "3")))
        # Mai fold/safe-transit. Se siamo lontani da scan SX, usa la posa corrente
        # come base (offset piccoli) invece di un salto pericoloso.
        start_delta = max(abs(float(base[i]) - float(pre_raw[i])) for i in range(6))
        max_start_delta = float(os.environ.get("D1_GRASP6D_AUTO_MAX_START_DELTA_DEG", "40"))
        if search_active:
            base_source = "search_probe"
        elif offset_meta.get("reference"):
            base_source = "reference_replay"
        elif auto_calibration_search.get("done") and isinstance(auto_calibration_search.get("best_base"), list):
            base_source = "search_best"
        else:
            base_source = "scan_left"
        # Per il replay riferimenti il target e' ASSOLUTO e validato: non rimappare
        # su base=posa corrente (romperebbe la posa). Vai diretto alla posa salvata.
        if start_delta > max_start_delta and not offset_meta.get("reference"):
            base = service.clamp_servo_deg(list(pre_raw[:7]))
            target = service.clamp_servo_deg([base[i] + offset[i] for i in range(7)])
            base_source = "current_pose"
            grasp6d.record_calibration_event(
                "auto_base_current_pose",
                reason="start_far_from_scan_left_using_current",
                hold_ok=True,
            )
        prev_move_speed = os.environ.get("D1_PROG_MOVE_DEG_PER_S")
        auto_move_speed = os.environ.get("D1_GRASP6D_AUTO_MOVE_DEG_PER_S", "5").strip()
        if auto_move_speed:
            os.environ["D1_PROG_MOVE_DEG_PER_S"] = auto_move_speed
        auto_pose_mode = motion_profile.auto_move_mode()
        auto_step_deg = motion_profile.auto_joint_step_deg()
        auto_min_delay = motion_profile.auto_waypoint_delay_ms()
        move: dict[str, Any] = {"ok": False, "reason": "auto_move_not_started"}
        try:
            try:
                _set_auto_progress(
                    phase="moving",
                    message=f"Step {step + 1}: muovo braccio verso posa calibrazione",
                    offset_meta=offset_meta,
                )
                move = program_runner.move_to_servo_deg_smooth(
                    target,
                    tracking_max_error_deg=tracking_limit,
                    pose_mode=auto_pose_mode,
                    max_step_deg=auto_step_deg,
                    min_delay_ms=auto_min_delay,
                )
            finally:
                if prev_move_speed is None:
                    os.environ.pop("D1_PROG_MOVE_DEG_PER_S", None)
                else:
                    os.environ["D1_PROG_MOVE_DEG_PER_S"] = prev_move_speed
            if not (move.get("ok") or move.get("skipped")):
                payload: dict[str, Any] = {"ok": False, "reason": "move_failed", "move": move, "target_servo_deg": target}
                move_reason = str(move.get("reason") or "move_failed")
                grasp6d.record_calibration_event(
                    "auto_watchdog_stop" if "tracking" in move_reason or "feedback_missing" in move_reason else "auto_motion_failed",
                    reason=move_reason,
                    max_tracking_error_deg=move.get("max_tracking_error_deg"),
                    tracking_limit_deg=move.get("tracking_limit_deg") or tracking_limit,
                    hold_ok=bool((move.get("safety_hold") or {}).get("ok")) if isinstance(move.get("safety_hold"), dict) else None,
                )
                # Sempre freeze sulla posa MISURATA, anche se il move ha gia' tentato un hold.
                payload["safety_hold"] = service.request_emergency_hold(
                    reason="auto_calibration_move_failed", hard=False
                )
                payload["safety_hold_source"] = "auto_step_finally_measured_soft"
                return jsonify(payload), 502
        except Exception as exc:
            safety_hold = service.request_emergency_hold(reason="auto_calibration_exception", hard=False)
            grasp6d.record_calibration_event(
                "auto_motion_failed",
                reason=f"exception:{type(exc).__name__}",
                hold_ok=bool(safety_hold.get("ok")),
            )
            return jsonify(
                {
                    "ok": False,
                    "reason": "auto_calibration_exception",
                    "error": repr(exc),
                    "safety_hold": safety_hold,
                    "target_servo_deg": target,
                }
            ), 502
        settle_s = max(0.4, min(4.0, float(os.environ.get("D1_GRASP6D_AUTO_SETTLE_S", "2.0"))))
        if search_active:
            # In search conta solo il numero di tag: bastano settle breve e 1 frame.
            settle_s = min(settle_s, max(0.3, float(os.environ.get("D1_GRASP6D_AUTO_SEARCH_SETTLE_S", "0.8"))))
        _set_auto_progress(
            phase="settling",
            message=f"Step {step + 1}: settle {settle_s:.1f}s (braccio fermo)",
            offset_meta=offset_meta,
        )
        settle_deadline = time.monotonic() + settle_s
        settle_missing = 0
        settle_violations = 0
        while time.monotonic() < settle_deadline:
            time.sleep(min(0.12, max(0.0, settle_deadline - time.monotonic())))
            guard_fb = service.read_servo_deg(fast=True)
            guard_raw = guard_fb.get("servo_deg") if guard_fb.get("ok") else None
            if not isinstance(guard_raw, list) or len(guard_raw) < 7:
                settle_missing += 1
                if settle_missing >= tracking_violation_limit:
                    grasp6d.record_calibration_event(
                        "auto_watchdog_stop",
                        reason="settle_feedback_missing",
                        tracking_limit_deg=tracking_limit,
                        tracking_violation_limit=tracking_violation_limit,
                        hold_ok=None,
                    )
                    return jsonify(
                        {
                            "ok": False,
                            "reason": "settle_feedback_missing",
                            "move": move,
                            "feedback": guard_fb,
                            "tracking_missing_count": settle_missing,
                            "tracking_violation_limit": tracking_violation_limit,
                            "safety_hold": service.request_emergency_hold(
                                reason="auto_calibration_settle_feedback_missing", hard=False
                            ),
                        }
                    ), 503
                continue
            settle_missing = 0
            guard_errs = [round(abs(float(target[i]) - float(guard_raw[i])), 2) for i in range(7)]
            guard_max = max(guard_errs[:6]) if guard_errs else 0.0
            if guard_max > tracking_limit:
                settle_violations += 1
                if settle_violations >= tracking_violation_limit:
                    grasp6d.record_calibration_event(
                        "auto_watchdog_stop",
                        reason="settle_tracking_error_too_high",
                        max_tracking_error_deg=guard_max,
                        tracking_limit_deg=tracking_limit,
                        tracking_violation_limit=tracking_violation_limit,
                        hold_ok=None,
                    )
                    return jsonify(
                        {
                            "ok": False,
                            "reason": "settle_tracking_error_too_high",
                            "move": move,
                            "target_servo_deg": target,
                            "servo_deg": guard_raw[:7],
                            "tracking_errors_deg": guard_errs,
                            "max_tracking_error_deg": guard_max,
                            "tracking_limit_deg": tracking_limit,
                            "tracking_violation_count": settle_violations,
                            "tracking_violation_limit": tracking_violation_limit,
                            "safety_hold": service.request_emergency_hold(
                                reason="auto_calibration_settle_tracking_error", hard=False
                            ),
                        }
                    ), 502
            else:
                settle_violations = 0
        feedback = service.read_servo_deg(fast=False)
        raw = feedback.get("servo_deg") if feedback.get("ok") else None
        if not isinstance(raw, list) or len(raw) < 7:
            return jsonify(
                {
                    "ok": False,
                    "reason": "feedback_after_move_unavailable",
                    "feedback": feedback,
                    "move": move,
                    "safety_hold": service.request_emergency_hold(
                        reason="auto_calibration_feedback_missing", hard=False
                    ),
                }
            ), 503
        # Solo funcode-2 mode0: niente re-couple dopo ogni offset.
        hold = service.hold_pose_stream(servo_deg=list(raw[:7]))
        if not (hold.get("ok") or hold.get("skipped")):
            return jsonify(
                {
                    "ok": False,
                    "reason": "hold_after_move_failed",
                    "hold": hold,
                    "feedback": feedback,
                    "move": move,
                    "safety_hold": service.request_emergency_hold(
                        reason="auto_calibration_hold_after_move_failed", hard=False
                    ),
                }
            ), 502
        rest_s = max(0.0, min(4.0, float(os.environ.get("D1_GRASP6D_AUTO_REST_S", "1.2"))))
        if search_active:
            rest_s = 0.0  # in search non serve il rest: conta solo i tag
        if rest_s > 0:
            _set_auto_progress(
                phase="resting",
                message=f"Step {step + 1}: rest {rest_s:.1f}s prima dello scatto",
            )
            time.sleep(rest_s)
        median_frames = max(1, min(5, int(os.environ.get("D1_GRASP6D_AUTO_MEDIAN_FRAMES", "3"))))
        if search_active:
            median_frames = 1
        _set_auto_progress(
            phase="capturing",
            message=f"Step {step + 1}: catturo RGBD (median {median_frames}) + AprilGrid",
        )
        try:
            frame = _capture_wrist_rgbd_with_retry(median_frames=median_frames)
        except Exception as exc:
            _set_auto_progress(
                running=False,
                phase="error",
                reason="wrist_rgbd_capture_failed",
                message="Cattura polso fallita",
            )
            return jsonify({"ok": False, "reason": "wrist_rgbd_capture_failed", "detail": str(exc), "hold": hold}), 503
        marker = grasp6d.detect_calibration_marker(frame.color_bgr, frame.intrinsics)
        # --- SEARCH: registra i tag visti, aggiorna la posa migliore, non salva sample ---
        if search_active:
            visible = int(marker.get("visible_marker_count") or 0)
            reproj = marker.get("reprojection_rms_px")
            idx = int(auto_calibration_search.get("index") or 0)
            auto_calibration_search.setdefault("results", []).append(
                {"index": idx, "tags": visible, "reproj": reproj, "servo_deg": [float(x) for x in raw[:7]]}
            )
            if visible > int(auto_calibration_search.get("best_tags") or -1):
                auto_calibration_search["best_tags"] = visible
                auto_calibration_search["best_base"] = [float(x) for x in raw[:7]]
            auto_calibration_search["index"] = idx + 1
            total = len(auto_calibration_search.get("candidates") or [])
            good_enough = max(8, int(os.environ.get("D1_GRASP6D_AUTO_SEARCH_GOOD_TAGS", "18")))
            search_finished = (auto_calibration_search["index"] >= total) or (visible >= good_enough)
            if search_finished:
                auto_calibration_search["active"] = False
                auto_calibration_search["done"] = True
            best_tags = int(auto_calibration_search.get("best_tags") or 0)
            _set_auto_progress(
                running=False,
                phase="search",
                saved=False,
                reason="search_viewpoint",
                message=(
                    f"Search {idx + 1}/{total}: vista con {visible} tag"
                    + (f" · scelta base migliore ({best_tags} tag), inizio orbita" if search_finished else " · provo prossima vista")
                ),
                tags=visible,
                reproj_px=reproj,
            )
            grasp6d.record_calibration_event(
                "auto_search_viewpoint",
                reason="search_done" if search_finished else "search_probe",
                visible_marker_count=visible,
                reprojection_rms_px=reproj,
                hold_ok=True,
            )
            return jsonify(
                {
                    "ok": True,
                    "saved": False,
                    "reason": "search_viewpoint",
                    "search": {
                        "index": idx,
                        "total": total,
                        "tags": visible,
                        "best_tags": best_tags,
                        "done": bool(search_finished),
                    },
                    "marker": marker,
                    "move": move,
                    "hold": hold,
                    "base_source": "search",
                    "rest_s": rest_s,
                    "offset_meta": offset_meta,
                }
            ), 200
        _set_auto_progress(
            phase="evaluating",
            message=f"Step {step + 1}: valuto sample (tag/novita'/residuo)",
            tags=marker.get("visible_marker_count"),
            reproj_px=marker.get("reprojection_rms_px"),
        )
        if not marker.get("ok"):
            skip_reason = marker.get("reason") or "target_not_valid"
            grasp6d.record_calibration_event(
                "auto_sample_skipped",
                reason=skip_reason,
                visible_marker_count=marker.get("visible_marker_count"),
                reprojection_rms_px=marker.get("reprojection_rms_px"),
                hold_ok=True,
            )
            _set_auto_progress(
                running=False,
                phase="skipped",
                saved=False,
                reason=skip_reason,
                message=f"Step {step + 1}: skip · {skip_reason}",
                tags=marker.get("visible_marker_count"),
                reproj_px=marker.get("reprojection_rms_px"),
            )
            return jsonify(
                {
                    "ok": True,
                    "saved": False,
                    "reason": skip_reason,
                    "marker": marker,
                    "move": move,
                    "hold": hold,
                    "base_source": base_source,
                    "rest_s": rest_s,
                    "offset_meta": offset_meta,
                }
            ), 200
        min_tags = max(8, int(os.environ.get("D1_GRASP6D_AUTO_MIN_VISIBLE_TAGS", "12")))
        # Non pretendere piu' tag di quanti la vista migliore trovata in search ne veda.
        search_best_tags = int(auto_calibration_search.get("best_tags") or 0)
        if search_best_tags >= 8:
            min_tags = min(min_tags, max(8, search_best_tags - 3))
        visible_tags = int(marker.get("visible_marker_count") or 0)
        if visible_tags < min_tags:
            grasp6d.record_calibration_event(
                "auto_sample_skipped",
                reason="too_few_visible_tags",
                visible_marker_count=visible_tags,
                min_visible_tags=min_tags,
                hold_ok=True,
            )
            _set_auto_progress(
                running=False,
                phase="skipped",
                saved=False,
                reason="too_few_visible_tags",
                message=f"Step {step + 1}: skip · solo {visible_tags}/{min_tags} tag",
                tags=visible_tags,
                reproj_px=marker.get("reprojection_rms_px"),
            )
            return jsonify(
                {
                    "ok": True,
                    "saved": False,
                    "reason": "too_few_visible_tags",
                    "min_visible_tags": min_tags,
                    "marker": marker,
                    "move": move,
                    "hold": hold,
                    "base_source": base_source,
                    "rest_s": rest_s,
                    "offset_meta": offset_meta,
                }
            ), 200
        max_reproj = float(os.environ.get("D1_GRASP6D_AUTO_MAX_REPROJ_PX", "1.15"))
        reproj = marker.get("reprojection_rms_px")
        if reproj is not None and float(reproj) > max_reproj:
            grasp6d.record_calibration_event(
                "auto_sample_skipped",
                reason="reprojection_too_high",
                visible_marker_count=visible_tags,
                reprojection_rms_px=reproj,
                hold_ok=True,
            )
            _set_auto_progress(
                running=False,
                phase="skipped",
                saved=False,
                reason="reprojection_too_high",
                message=f"Step {step + 1}: skip · reproj {float(reproj):.2f}px > {max_reproj:.2f}",
                tags=visible_tags,
                reproj_px=reproj,
            )
            return jsonify(
                {
                    "ok": True,
                    "saved": False,
                    "reason": "reprojection_too_high",
                    "max_reproj_px": max_reproj,
                    "marker": marker,
                    "move": move,
                    "hold": hold,
                    "base_source": base_source,
                    "rest_s": rest_s,
                    "offset_meta": offset_meta,
                }
            ), 200
        if marker.get("target_type") == "aprilgrid_36h11" and marker.get("pose_method") != "tag_corners":
            grasp6d.record_calibration_event(
                "auto_sample_skipped",
                reason="aprilgrid_corner_pose_required",
                visible_marker_count=marker.get("visible_marker_count"),
                reprojection_rms_px=marker.get("reprojection_rms_px"),
                hold_ok=True,
            )
            return jsonify(
                {
                    "ok": True,
                    "saved": False,
                    "reason": "aprilgrid_corner_pose_required",
                    "marker": marker,
                    "move": move,
                    "hold": hold,
                    "base_source": base_source,
                    "rest_s": rest_s,
                    "offset_meta": offset_meta,
                }
            ), 200
        # Posa planare ambigua: rotazione inaffidabile -> non deve entrare nella
        # hand-eye (causa dei residui enormi). Scarto e continuo l'orbita.
        if bool(marker.get("pose_ambiguous")):
            grasp6d.record_calibration_event(
                "auto_sample_skipped",
                reason="pose_ambiguous",
                visible_marker_count=visible_tags,
                reprojection_rms_px=reproj,
                hold_ok=True,
            )
            _set_auto_progress(
                running=False,
                phase="skipped",
                saved=False,
                reason="pose_ambiguous",
                message=(
                    f"Step {step + 1}: skip · posa ambigua "
                    f"(ratio {marker.get('pose_ambiguity_ratio')}) · inclina di piu' il target"
                ),
                tags=visible_tags,
                reproj_px=reproj,
            )
            return jsonify(
                {
                    "ok": True,
                    "saved": False,
                    "reason": "pose_ambiguous",
                    "pose_ambiguity_ratio": marker.get("pose_ambiguity_ratio"),
                    "marker": marker,
                    "move": move,
                    "hold": hold,
                    "base_source": base_source,
                    "rest_s": rest_s,
                    "offset_meta": offset_meta,
                }
            ), 200

        T_base_tool = grasp6d.fk_tool_transform(np.radians(np.asarray(raw[:6], dtype=float)))
        candidate = {
            "T_base_tool": T_base_tool.tolist(),
            "T_camera_target": marker["T_camera_target"],
            "marker": {
                key: marker.get(key)
                for key in ("target_type", "visible_marker_count", "pose_method", "object_point_variant", "reprojection_rms_px", "marker_ids")
                if key in marker
            },
        }
        min_n = grasp6d.calib_min_samples()
        current_quality = grasp6d.handeye_quality_report(samples) if samples else {}
        residual_stuck = bool(
            len(samples) >= min_n
            and isinstance(current_quality, dict)
            and current_quality.get("next_action") in {"prune_and_aggiungi_sample", "residuo_alto_non_calcolare", "sessione_incoerente_reset"}
        )
        # Mai soft novelty: pose troppo simili abbassano la qualita' del solve (errori cm).
        novelty = grasp6d.sample_pose_novelty(samples, candidate, soft=False)
        if samples and not novelty.get("useful"):
            prune_stuck = None
            if residual_stuck and len(samples) >= min_n + 1:
                prune_stuck = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=3, force_drop=1)
                samples = grasp6d.list_handeye_samples()
                current_quality = grasp6d.handeye_quality_report(samples)
            elif residual_stuck and len(samples) >= min_n:
                prune_stuck = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=2, force_drop=1)
                samples = grasp6d.list_handeye_samples()
                current_quality = grasp6d.handeye_quality_report(samples)
            grasp6d.record_calibration_event(
                "auto_sample_skipped",
                reason="pose_too_similar",
                visible_marker_count=marker.get("visible_marker_count"),
                reprojection_rms_px=marker.get("reprojection_rms_px"),
                hold_ok=True,
            )
            _set_auto_progress(
                running=False,
                phase="skipped",
                saved=False,
                reason="pose_too_similar",
                message=f"Step {step + 1}: skip · posa troppo simile",
                tags=marker.get("visible_marker_count"),
                reproj_px=marker.get("reprojection_rms_px"),
                **_auto_progress_from_quality(current_quality),
            )
            return jsonify(
                {
                    "ok": True,
                    "saved": False,
                    "reason": "pose_too_similar",
                    "novelty": novelty,
                    "quality": current_quality,
                    "prune": prune_stuck,
                    "marker": marker,
                    "move": move,
                    "hold": hold,
                    "base_source": base_source,
                    "rest_s": rest_s,
                    "offset_meta": offset_meta,
                }
            ), 200
        # Prima costruiamo un POOL diverso (fino a collect_target): accettiamo
        # sample nuovi/vari anche se non abbassano subito il residuo, cosi' il
        # pruning finale puo' scartare i peggiori e tenere i migliori. Solo dopo
        # collect_target pretendiamo che ogni foto migliori davvero il residuo.
        if len(samples) >= collect_target:
            candidate_quality = grasp6d.handeye_quality_report(samples + [candidate])
            current_severity = grasp6d.residual_severity(current_quality)
            candidate_severity = grasp6d.residual_severity(candidate_quality)
            improves_residual = (
                current_severity is not None
                and candidate_severity is not None
                and candidate_severity <= current_severity * 0.97
            )
            improves_diversity = float(candidate_quality.get("diversity_score") or 0.0) > float(
                current_quality.get("diversity_score") or 0.0
            ) + 0.03
            # Solo se il residual NON peggiora e c'e' ancora margine di diversita' sotto soglia.
            diversity_headroom = float(current_quality.get("diversity_score") or 0.0) < 0.92
            accept_for_diversity = improves_diversity and diversity_headroom and (
                candidate_severity is None
                or current_severity is None
                or candidate_severity <= current_severity * 1.01
            )
            if (
                not improves_residual
                and not accept_for_diversity
                and not candidate_quality.get("build_ready")
            ):
                prune_stuck = None
                if residual_stuck:
                    prune_stuck = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=3, force_drop=1)
                grasp6d.record_calibration_event(
                    "auto_sample_skipped",
                    reason="residual_not_improving",
                    visible_marker_count=marker.get("visible_marker_count"),
                    reprojection_rms_px=marker.get("reprojection_rms_px"),
                    hold_ok=True,
                )
                _set_auto_progress(
                    running=False,
                    phase="skipped",
                    saved=False,
                    reason="residual_not_improving",
                    message=f"Step {step + 1}: skip · non migliora residuo",
                    tags=marker.get("visible_marker_count"),
                    reproj_px=marker.get("reprojection_rms_px"),
                    **_auto_progress_from_quality(candidate_quality),
                )
                return jsonify(
                    {
                        "ok": True,
                        "saved": False,
                        "reason": "residual_not_improving",
                        "current_quality": current_quality,
                        "candidate_quality": candidate_quality,
                        "prune": prune_stuck,
                        "marker": marker,
                        "move": move,
                        "hold": hold,
                        "base_source": base_source,
                        "rest_s": rest_s,
                        "offset_meta": offset_meta,
                    }
                ), 200

        out = grasp6d.append_handeye_sample(
            T_base_tool,
            np.asarray(marker["T_camera_target"], dtype=float),
            marker=marker,
            servo_deg=[float(x) for x in raw[:7]],
        )
        samples_after = grasp6d.list_handeye_samples()
        prune = None
        # Finche' stiamo costruendo il pool (< collect_target) NON potiamo: prima
        # accumula viste diverse. Al raggiungimento del pool, elimina i peggiori
        # e tieni i migliori (foto che non aiutano = scartate).
        if len(samples_after) >= collect_target:
            prune = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=max(4, collect_target - min_n))
            samples_after = grasp6d.list_handeye_samples()
        quality = grasp6d.handeye_quality_report(samples_after)
        built = None
        if quality.get("build_ready") or (isinstance(prune, dict) and (prune.get("build") or {}).get("ok")):
            built = grasp6d.build_handeye_calibration(samples_after)
            if not built.get("ok") and len(samples_after) >= min_n:
                prune = grasp6d.prune_handeye_outliers(min_keep=min_n, max_drop=5)
                samples_after = grasp6d.list_handeye_samples()
                quality = grasp6d.handeye_quality_report(samples_after)
                if quality.get("build_ready") or (prune.get("build") or {}).get("ok"):
                    built = grasp6d.build_handeye_calibration(samples_after)
        grasp6d.record_calibration_event(
            "auto_sample_saved",
            sample_count=int(out.get("sample_count") or 0),
            target_type=marker.get("target_type"),
            visible_marker_count=marker.get("visible_marker_count"),
            reprojection_rms_px=marker.get("reprojection_rms_px"),
            hold_ok=True,
        )
        done = bool(built and built.get("ok"))
        _set_auto_progress(
            running=False,
            phase="done" if done else "saved",
            saved=True,
            reason="cal_ok" if done else "sample_saved",
            message=(
                f"Step {step + 1}: cal_ok! sample={out.get('sample_count')}"
                if done
                else f"Step {step + 1}: sample salvato ({out.get('sample_count')})"
            ),
            cal_ok=done,
            tags=marker.get("visible_marker_count"),
            reproj_px=marker.get("reprojection_rms_px"),
            offset_meta=offset_meta,
            **_auto_progress_from_quality(quality),
        )
        return jsonify(
            {
                "ok": True,
                "saved": True,
                "done": done,
                "sample": out,
                "build": built,
                "prune": prune,
                "quality": quality,
                "marker": marker,
                "move": move,
                "hold": hold,
                "target_servo_deg": target,
                "base_servo_deg": base,
                "base_source": base_source,
                "requested_base_ignored": isinstance(requested_base, list),
                "rest_s": rest_s,
                "offset_meta": offset_meta,
                "auto_soft": {
                    "pose_mode": auto_pose_mode,
                    "joint_step_deg": auto_step_deg,
                    "min_delay_ms": auto_min_delay,
                    "move_deg_per_s": auto_move_speed,
                    "median_frames": median_frames,
                },
            }
        )

    @app.route("/api/pick/metric/calibration/prune", methods=["POST"])
    def pick_metric_calibration_prune() -> Response:
        out = grasp6d.prune_handeye_outliers()
        grasp6d.record_calibration_event(
            "prune_ok" if out.get("ok") else "prune_failed",
            before=out.get("before"),
            after=out.get("after"),
            dropped=len(out.get("dropped") or []),
            build_ok=bool((out.get("build") or {}).get("ok")),
        )
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/metric/calibration/build", methods=["POST"])
    def pick_metric_calibration_build() -> Response:
        prune = grasp6d.prune_handeye_outliers(min_keep=grasp6d.calib_min_samples(), max_drop=5)
        out = grasp6d.build_handeye_calibration(grasp6d.list_handeye_samples())
        if not out.get("ok") and prune.get("build") and prune["build"].get("ok"):
            out = prune["build"]
        grasp6d.record_calibration_event(
            "build_ok" if out.get("ok") else "build_failed",
            sample_count=int(out.get("sample_count") or len(grasp6d.list_handeye_samples())),
            reason=out.get("reason"),
            translation_rms_m=out.get("translation_rms_m"),
            rotation_rms_deg=out.get("rotation_rms_deg"),
            pruned=bool(prune.get("changed")),
        )
        out["prune"] = {k: prune.get(k) for k in ("before", "after", "dropped", "changed") if k in prune}
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/metric/calibration", methods=["GET", "DELETE"])
    def pick_metric_calibration_status() -> Response:
        if request.method == "DELETE":
            try:
                grasp6d.HAND_EYE_SAMPLES_PATH.unlink(missing_ok=True)
            except OSError as exc:
                return jsonify({"ok": False, "reason": "sample_reset_failed", "detail": str(exc)}), 500
            grasp6d.record_calibration_event("samples_reset", calibration_kept=grasp6d.CALIBRATION_PATH.exists())
        cal = grasp6d.load_calibration()
        cal.pop("T_tool_camera_np", None)
        samples = grasp6d.list_handeye_samples()
        quality = grasp6d.handeye_quality_report(samples)
        return jsonify(
            {
                "ok": True,
                "sample_count": len(samples),
                "samples": [
                    {"index": index + 1, "at": sample.get("at"), "marker": sample.get("marker")}
                    for index, sample in enumerate(samples)
                    if isinstance(sample, dict)
                ],
                "calibration": cal,
                "quality": quality,
                "history": grasp6d.calibration_history(),
                "target": {
                    "type": "aprilgrid_36h11",
                    "dictionary": "DICT_APRILTAG_36h11",
                    "grid_cols_rows": [6, 4],
                    "marker_ids": [288, 311],
                    "marker_size_mm": 30.0,
                    "marker_gap_mm": 15.0,
                    "minimum_samples": 8,
                    "recommended_samples": 10,
                    "fallback_target": {
                        "type": "aruco_4x4_50",
                        "marker_id": 0,
                        "marker_size_mm": 60.0,
                        "download_url": "/api/pick/metric/calibration/target.pdf",
                    },
                },
                "auto_motion_enabled": os.environ.get("D1_GRASP6D_AUTO_MOTION_ENABLE", "0")
                .strip()
                .lower()
                in {"1", "true", "yes", "on"},
                "control": {
                    "arm_coupled": bool(service.arm_coupled()),
                },
            }
        )

    @app.route("/api/pick/metric/calibration/target.pdf", methods=["GET"])
    def pick_metric_calibration_target_pdf() -> Response:
        path = PROJECT_ROOT / "output" / "pdf" / "d1_handeye_aruco_4x4_50_id0_60mm.pdf"
        if not path.is_file():
            return jsonify({"ok": False, "reason": "calibration_target_pdf_missing"}), 404
        return send_file(
            path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name="d1_handeye_aruco_4x4_50_id0_60mm.pdf",
            max_age=0,
        )

    @app.route("/api/pick/snapshot", methods=["POST"])
    def pick_snapshot() -> Response:
        out = pick_vision.capture_and_detect()
        _apply_pick_detection_to_preset(out)
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/pick/detect", methods=["POST"])
    def pick_detect() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("capture_if_missing", True):
            out = pick_vision.capture_and_detect()
        else:
            out = pick_vision.detect_on_latest_snapshot(capture_if_missing=False)
        _apply_pick_detection_to_preset(out)
        feedback = service.read_servo_deg(fast=True)
        if feedback.get("ok") and feedback.get("servo_deg"):
            out["grasp_estimate"] = _scan_grasp_estimate(feedback["servo_deg"], out)
            out["scan_feedback"] = feedback
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    def _pick_scene_jpeg() -> Response:
        path = pick_vision.scene_overlay_path()
        if not path.is_file():
            return jsonify({"ok": False, "reason": "no_scene_overlay"}), 404
        return send_file(path, mimetype="image/jpeg", max_age=0)

    @app.route("/api/pick/diagnostic")
    def pick_diagnostic() -> Response:
        import sys

        from go2_dashboard.paths import PROJECT_ROOT

        cap = orbbec_capture.capture_orbbec_jpeg()
        det_out: dict[str, Any] = {"ok": False, "reason": "capture_failed"}
        if cap.get("ok"):
            scripts_dir = str(PROJECT_ROOT / "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            from box_object_detector import detector_status

            det_out = pick_vision.detect_on_latest_snapshot(capture_if_missing=False)
            det_out["detector_status"] = detector_status()
        return jsonify(
            {
                "ok": cap.get("ok", False),
                "capture": cap,
                "detection": det_out,
                "preset": pick_preset.preset_info(),
            }
        )

    @app.route("/api/pick/scene.jpg")
    def pick_scene_jpeg() -> Response:
        return _pick_scene_jpeg()

    @app.route("/api/pick/detect.jpg")
    def pick_detect_jpeg() -> Response:
        return _pick_scene_jpeg()

    def _execute_legacy_grasp_approach() -> tuple[dict[str, Any], int]:
        found = program_store.find_scan_waypoint()
        if found is None:
            return {"ok": False, "reason": "scan_waypoint_not_found"}, 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list):
            return {"ok": False, "reason": "invalid_scan_waypoint"}, 400
        scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
        preset = pick_preset.load_preset()
        off = pick_preset.effective_joint_offsets(
            last_detection=preset.get("last_detection"),
        )
        if off is None:
            return (
                {
                    "ok": False,
                    "reason": "grasp_preset_missing",
                    "hint": "Calibrazione zero, offset programma o foto normale prima di Presa oggetto",
                },
                404,
            )
        target = pick_preset.grasp_servo_approach_from_scan(
            scan_sd,
            offsets=off,
            last_detection=preset.get("last_detection"),
        )
        if target is None:
            return {"ok": False, "reason": "grasp_target_invalid"}, 400
        transit = _safe_transit_target()
        service._halt_cartesian_stream(wait_idle=True)
        program_runner.clear_stop_request()
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return couple, 502
        transit_move = None
        if transit is not None:
            transit_move = program_runner.move_to_servo_deg_smooth(transit)
            if not (transit_move.get("ok") or transit_move.get("skipped")):
                transit_move["phase"] = "transit_zero"
                transit_move["target_servo_deg"] = transit
                transit_move["coupling"] = couple
                return (
                    {
                        "ok": False,
                        "reason": transit_move.get("reason", "transit_failed"),
                        "preset": "grasp_approach",
                        "coupling": couple,
                        "transit_zero": transit,
                        "transit_move": transit_move,
                        "scan_servo_deg": scan_sd,
                    },
                    502,
                )
        open_j6 = pick_preset.gripper_open_j6_deg(scan_sd)
        out = program_runner.move_to_servo_deg_smooth(target, pin_joints={6: open_j6})
        out["preset"] = "grasp_approach"
        out["gripper_open_deg"] = open_j6
        out["gripper_closed_deg"] = pick_preset.gripper_close_j6_deg(scan_sd)
        out["coupling"] = couple
        out["transit_zero"] = transit
        out["transit_move"] = transit_move
        out["scan_servo_deg"] = scan_sd
        out["joint_offset_deg"] = off
        out["has_zero_calibration"] = bool(preset.get("zero_calibration"))
        zc = preset.get("zero_calibration") or {}
        ref_vis = zc.get("vision_at_scan") if isinstance(zc, dict) else None
        ld = preset.get("last_detection")
        dpx = pick_preset._vision_pixel_delta(
            ref_vis if isinstance(ref_vis, dict) else None,
            ld if isinstance(ld, dict) else None,
        )
        if dpx is not None:
            out["vision_pixel_delta"] = [round(dpx[0], 1), round(dpx[1], 1)]
        d_orient = pick_preset._vision_orientation_delta_deg(
            ref_vis if isinstance(ref_vis, dict) else None,
            ld if isinstance(ld, dict) else None,
        )
        if d_orient is not None:
            out["vision_orientation_delta_deg"] = d_orient
            out["orient_joint_index"] = pick_preset._orient_joint_index()
        zc_dict = zc if isinstance(zc, dict) else None
        if zc_dict and isinstance(ld, dict):
            scan_sd = zc_dict.get("scan_servo_deg")
            base_off = preset.get("joint_offset_deg")
            j5i = pick_preset._orient_joint_index()
            if (
                isinstance(scan_sd, list)
                and len(scan_sd) > j5i
                and isinstance(base_off, list)
                and len(base_off) > j5i
            ):
                out["j5_breakdown"] = pick_preset._j5_target_breakdown(
                    scan_j5=float(scan_sd[j5i]),
                    base_off_j5=float(base_off[j5i]),
                    zc=zc_dict,
                    data=preset,
                    cur_dict=ld,
                )
        manual_orient = preset.get("manual_orient_offset_deg")
        if manual_orient is not None:
            out["manual_orient_offset_deg"] = float(manual_orient)
        out["joint_offset_deg_effective"] = off
        out["target_servo_deg"] = target
        try:
            from go2_dashboard.d1_jog import pick_teach_model

            if pick_teach_model.model_is_active(preset):
                _moff, blend = pick_teach_model.effective_offsets_from_model(
                    preset.get("last_detection"),
                    data=preset,
                )
                out["has_teach_model"] = True
                out["teach_model_blend"] = blend
                if isinstance(blend, dict):
                    out["teach_interp_method"] = blend.get("method")
                    out["teach_nearest_id"] = blend.get("nearest_id")
        except Exception:
            pass
        return out, (200 if out.get("ok") else 502)

    def _execute_grasp6d_attempt(*, dry_run: bool = False, pregrasp_only: bool = False) -> dict[str, Any]:
        import numpy as np

        run: dict[str, Any] = {
            "ok": False,
            "mode": "dry_run" if dry_run else ("pregrasp_only" if pregrasp_only else "grasp6d"),
            "started_at": time.time(),
            "steps": [],
            "daemon_invariant": "external_hold_only",
        }

        def step(name: str, payload: dict[str, Any]) -> None:
            run["steps"].append({"name": name, **payload})

        initial = _capture_grasp6d_plan()
        step("plan", initial)
        if not initial.get("ok"):
            run["reason"] = initial.get("reason", "plan_failed")
            run["finished_at"] = time.time()
            _save_grasp6d_run(run)
            return run
        run["plan"] = initial
        if dry_run:
            run.update({"ok": True, "finished_at": time.time()})
            _save_grasp6d_run(run)
            return run

        couple = service.ensure_coupled_for_motion()
        step("couple", couple)
        if not (couple.get("ok") or couple.get("skipped")):
            run["reason"] = couple.get("reason", "couple_failed")
            run["finished_at"] = time.time()
            _save_grasp6d_run(run)
            return run

        open_deg = pick_preset.gripper_open_j6_deg()
        close_deg = pick_preset.gripper_close_j6_deg()
        latest = initial
        for attempt_index in range(2):
            run["attempt"] = attempt_index + 1
            selected = (latest.get("plan") or {}).get("selected") or {}
            pre = ((selected.get("pregrasp") or {}).get("servo_deg"))
            if not isinstance(pre, list):
                run["reason"] = "pregrasp_target_missing"
                break
            pre = service.clamp_servo_deg([float(x) for x in pre[:7]])
            pre[6] = open_deg
            moved = program_runner.move_to_servo_deg_smooth(pre, pin_joints={6: open_deg})
            step("pregrasp", {"attempt": attempt_index + 1, **moved})
            if not moved.get("ok"):
                run["reason"] = moved.get("reason", "pregrasp_failed")
                break

            # Tre osservazioni consecutive: nessun movimento finale se la posa oscilla.
            observations: list[dict[str, Any]] = []
            for _ in range(3):
                obs = _capture_grasp6d_plan()
                step("realign_observation", {"attempt": attempt_index + 1, "ok": obs.get("ok"), "reason": obs.get("reason")})
                if not obs.get("ok"):
                    observations = []
                    break
                observations.append(obs)
            if len(observations) != 3:
                run["reason"] = "realign_not_stable"
                latest = _capture_grasp6d_plan()
                continue
            target_ts = [
                np.asarray((((o.get("plan") or {}).get("selected") or {}).get("T_base_grasp")), dtype=float)
                for o in observations
            ]
            centers = np.stack([T[:3, 3] for T in target_ts])
            spread_m = float(np.max(np.linalg.norm(centers - np.mean(centers, axis=0), axis=1)))
            rotations = [T[:3, :3] for T in target_ts]
            rot_spread_deg = 0.0
            for R in rotations[1:]:
                c = float(np.clip((np.trace(R @ rotations[0].T) - 1.0) * 0.5, -1.0, 1.0))
                rot_spread_deg = max(rot_spread_deg, math.degrees(math.acos(c)))
            stable = spread_m <= 0.008 and rot_spread_deg <= 5.0
            step(
                "realign_gate",
                {"attempt": attempt_index + 1, "ok": stable, "spread_m": spread_m, "rotation_spread_deg": rot_spread_deg},
            )
            if not stable:
                run["reason"] = "realign_not_stable"
                latest = observations[-1]
                continue
            latest = observations[-1]
            if pregrasp_only:
                run.update({"ok": True, "reason": None, "finished_at": time.time()})
                _save_grasp6d_run(run)
                return run
            selected = (latest.get("plan") or {}).get("selected") or {}
            grasp_target = ((selected.get("grasp") or {}).get("servo_deg"))
            if not isinstance(grasp_target, list):
                run["reason"] = "grasp_target_missing"
                break
            grasp_target = service.clamp_servo_deg([float(x) for x in grasp_target[:7]])
            grasp_target[6] = open_deg
            approached = program_runner.move_to_servo_deg_smooth(grasp_target, pin_joints={6: open_deg})
            step("approach", {"attempt": attempt_index + 1, **approached})
            if not approached.get("ok"):
                run["reason"] = approached.get("reason", "approach_failed")
                break

            closed_target = list(grasp_target)
            closed_target[6] = close_deg
            closed = program_runner.move_to_servo_deg_smooth(closed_target)
            step("close", {"attempt": attempt_index + 1, **closed})
            if not closed.get("ok"):
                run["reason"] = closed.get("reason", "close_failed")
                break

            grasp_T = np.asarray(selected["T_base_grasp"], dtype=float)
            lift_T = grasp_T.copy()
            lift_T[2, 3] += float(os.environ.get("D1_GRASP6D_LIFT_M", "0.09"))
            lift_ik = grasp6d.ik_pose(lift_T, primary_seed=np.radians(np.asarray(closed_target[:6], dtype=float)))
            if not lift_ik.get("ok"):
                step("lift", lift_ik)
                run["reason"] = lift_ik.get("reason", "lift_ik_failed")
                break
            lift_target = service.clamp_servo_deg(list(lift_ik["servo_deg"]))
            lift_target[6] = close_deg
            lifted = program_runner.move_to_servo_deg_smooth(lift_target, pin_joints={6: close_deg})
            step("lift", {"attempt": attempt_index + 1, **lifted})
            if not lifted.get("ok"):
                run["reason"] = lifted.get("reason", "lift_failed")
                break

            fb = service.read_servo_deg(fast=True)
            actual_j6 = float(fb["servo_deg"][6]) if fb.get("ok") and isinstance(fb.get("servo_deg"), list) else None
            gripper_blocked = actual_j6 is not None and actual_j6 > close_deg + float(
                os.environ.get("D1_GRASP6D_GRIPPER_BLOCK_DELTA_DEG", "2.5")
            )
            post = _capture_grasp6d_plan()
            floor_absent = not post.get("ok") and post.get("reason") in {
                "no_cluster_above_floor",
                "object_component_not_found",
                "object_cluster_too_small",
                "no_safe_6d_grasp_candidate",
            }
            moved_with_lift = False
            if post.get("ok"):
                before_box = np.asarray((latest.get("plan") or {}).get("T_base_box"), dtype=float)
                after_box = np.asarray((post.get("plan") or {}).get("T_base_box"), dtype=float)
                if before_box.shape == (4, 4) and after_box.shape == (4, 4):
                    moved_with_lift = float(after_box[2, 3] - before_box[2, 3]) >= 0.04
            verified = bool(gripper_blocked and (floor_absent or moved_with_lift))
            verify = {
                "ok": verified,
                "actual_gripper_deg": actual_j6,
                "closed_empty_deg": close_deg,
                "gripper_blocked": gripper_blocked,
                "floor_position_absent": floor_absent,
                "box_moved_with_lift": moved_with_lift,
            }
            step("verify", verify)
            if verified:
                run.update({"ok": True, "reason": None, "verification": verify, "finished_at": time.time()})
                _save_grasp6d_run(run)
                return run

            run["reason"] = "grasp_not_verified"
            # Recovery controllato: torna al pregrasp e riapre, senza mai fare release.
            recovered = program_runner.move_to_servo_deg_smooth(pre, pin_joints={6: close_deg})
            step("retract", {"attempt": attempt_index + 1, **recovered})
            if not recovered.get("ok"):
                break
            opened = list(pre)
            opened[6] = open_deg
            opened_out = program_runner.move_to_servo_deg_smooth(opened)
            step("reopen", {"attempt": attempt_index + 1, **opened_out})
            latest = _capture_grasp6d_plan()
            if not latest.get("ok"):
                break

        # Qualunque uscita fallita mantiene l'ultima posa comandata in HOLD.
        hold = service.hold_pose_stream(servo_deg=service.get_servo_cache())
        step("safe_hold", hold)
        run["finished_at"] = time.time()
        _save_grasp6d_run(run)
        return run

    @app.route("/api/pick/grasp/goto", methods=["POST"])
    def pick_grasp_goto() -> Response:
        body = request.get_json(silent=True) or {}
        if os.environ.get("D1_GRASP6D_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}:
            if not body.get("dry_run") and not body.get("pregrasp_only") and body.get("confirm") != "EXECUTE_GRASP6D":
                return jsonify(
                    {
                        "ok": False,
                        "reason": "explicit_grasp6d_confirmation_required",
                        "required_confirm": "EXECUTE_GRASP6D",
                    }
                ), 409
            out = _execute_grasp6d_attempt(
                dry_run=bool(body.get("dry_run")),
                pregrasp_only=bool(body.get("pregrasp_only")),
            )
            code = 200 if out.get("ok") else 422
            return jsonify(out), code
        out, code = _execute_legacy_grasp_approach()
        return jsonify(out), code

    @app.route("/api/pick/grasp_legacy/preview", methods=["POST"])
    def pick_grasp_legacy_preview() -> Response:
        out = pick_vision.capture_and_detect()
        _apply_pick_detection_to_preset(out)
        feedback = service.read_servo_deg(fast=True)
        if feedback.get("ok") and feedback.get("servo_deg"):
            out["grasp_estimate"] = _scan_grasp_estimate(feedback["servo_deg"], out)
            out["scan_feedback"] = feedback
        out["mode"] = "legacy"
        return jsonify(out), (200 if out.get("ok") else 502)

    @app.route("/api/pick/grasp_legacy/execute", methods=["POST"])
    def pick_grasp_legacy_execute() -> Response:
        from go2_dashboard.d1_jog import pick_teach_model

        preset = pick_preset.load_preset()
        session = preset.get("teach_session")
        model = preset.get("teach_model")
        if isinstance(session, dict) and not (isinstance(model, dict) and model.get("active")):
            return jsonify(
                {
                    "ok": False,
                    "reason": "guided_2d_calibration_incomplete",
                    "quality": pick_teach_model.guided_quality_report(preset.get("teach_samples")),
                }
            ), 409
        scan = _legacy_scan_and_detect()
        if not scan.get("ok"):
            scan["mode"] = "legacy"
            scan["phase"] = "fresh_2d_scan"
            return jsonify(scan), 422
        out, code = _execute_legacy_grasp_approach()
        out["mode"] = "legacy"
        out["fresh_scan"] = scan
        return jsonify(out), code

    @app.route("/api/pick/grasp6d/status", methods=["GET"])
    def pick_grasp6d_status() -> Response:
        enabled = os.environ.get("D1_GRASP6D_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        cal = grasp6d.load_calibration()
        cal.pop("T_tool_camera_np", None)
        out = {
            "ok": True,
            "enabled": enabled,
            "rgbd": wrist_rgbd.health(capture=False),
            "sample_count": len(grasp6d.list_handeye_samples()),
            "calibration": cal,
            "tuning": grasp6d.tuning_info(),
        }
        return jsonify(out)

    @app.route("/api/pick/grasp6d/tuning", methods=["GET", "POST"])
    def pick_grasp6d_tuning() -> Response:
        if request.method == "GET":
            return jsonify(grasp6d.tuning_info())
        body = request.get_json(silent=True) or {}
        if body.get("reset"):
            return jsonify(grasp6d.update_tuning(reset=True))
        current = dict(grasp6d.tuning_info()["values"])
        action = str(body.get("action") or "")
        steps = {
            "lower_height": ("min_box_height_m", -0.003),
            "lower_cluster": ("min_cluster_points", -5.0),
            "lower_dimension": ("min_box_dim_m", -0.003),
        }
        if action not in steps:
            return jsonify({"ok": False, "reason": "invalid_tuning_action", "allowed": sorted(steps)}), 400
        key, delta = steps[action]
        current[key] = float(current[key]) + delta
        return jsonify(grasp6d.update_tuning({key: current[key]}))

    @app.route("/api/pick/grasp6d/preview", methods=["POST"])
    def pick_grasp6d_preview() -> Response:
        out = _capture_grasp6d_plan()
        _save_grasp6d_run({"mode": "preview_api", "at": time.time(), **out})
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/grasp6d/cluster_probe", methods=["POST"])
    def pick_grasp6d_cluster_probe() -> Response:
        body = request.get_json(silent=True) or {}
        frames = max(3, min(12, int(body.get("frames") or 5)))
        interval_s = max(0.0, min(1.0, float(body.get("interval_s") or 0.18)))
        out = _probe_grasp6d_cluster(frames=frames, interval_s=interval_s)
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/grasp6d/debug", methods=["GET"])
    def pick_grasp6d_debug() -> Response:
        out_jpg, meta = _grasp6d_debug_jpeg(capture_new=request.args.get("capture", "1") not in {"0", "false", "no"})
        return jsonify(meta), (200 if meta.get("ok") else 422)

    @app.route("/api/pick/grasp6d/debug.jpg", methods=["GET"])
    def pick_grasp6d_debug_jpg() -> Response:
        out_jpg, meta = _grasp6d_debug_jpeg(capture_new=request.args.get("capture", "1") not in {"0", "false", "no"})
        if out_jpg is None:
            return jsonify(meta), 422
        return Response(out_jpg, mimetype="image/jpeg")

    @app.route("/api/pick/grasp6d/last_run", methods=["GET"])
    def pick_grasp6d_last_run() -> Response:
        out = _load_grasp6d_run()
        return jsonify(out), (200 if out.get("ok") else 404)

    @app.route("/api/pick/grasp6d/pregrasp", methods=["POST"])
    def pick_grasp6d_pregrasp() -> Response:
        out = _execute_grasp6d_attempt(pregrasp_only=True)
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/grasp6d/execute", methods=["POST"])
    def pick_grasp6d_execute() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "EXECUTE_GRASP6D":
            return jsonify({"ok": False, "reason": "explicit_grasp6d_confirmation_required", "required_confirm": "EXECUTE_GRASP6D"}), 409
        out = _execute_grasp6d_attempt(pregrasp_only=False)
        return jsonify(out), (200 if out.get("ok") else 422)

    def _pick_gripper_move(j6_target: float, *, action: str) -> tuple[Response, int]:
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return jsonify({"ok": False, "reason": fb.get("reason", "no_feedback")}), 502
        cur = list(fb["servo_deg"])
        target = service.clamp_servo_deg(cur[:7])
        target[6] = round(float(j6_target), 3)
        service._halt_cartesian_stream(wait_idle=True)
        program_runner.clear_stop_request()
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify(couple), 502
        out = program_runner.move_to_servo_deg_smooth(target)
        out["action"] = action
        out["gripper_target_deg"] = target[6]
        out["target_servo_deg"] = target
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/pick/gripper/open", methods=["POST"])
    def pick_gripper_open() -> Response:
        found = program_store.find_scan_waypoint()
        if found is None:
            return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list):
            return jsonify({"ok": False, "reason": "invalid_scan_waypoint"}), 400
        scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
        open_j6 = pick_preset.gripper_open_j6_deg(scan_sd)
        resp, code = _pick_gripper_move(open_j6, action="gripper_open")
        return resp, code

    @app.route("/api/pick/gripper/close", methods=["POST"])
    def pick_gripper_close() -> Response:
        found = program_store.find_scan_waypoint()
        if found is None:
            return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list):
            return jsonify({"ok": False, "reason": "invalid_scan_waypoint"}), 400
        scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
        close_j6 = pick_preset.gripper_close_j6_deg(scan_sd)
        resp, code = _pick_gripper_move(close_j6, action="gripper_close")
        return resp, code

    @app.route("/api/logs/d1_command_daemon", methods=["GET"])
    def d1_command_daemon_log() -> Response:
        path = PROJECT_ROOT / "logs" / "d1_command_daemon.log"
        out = _tail_text(path)
        out["daemon_status"] = service.command_daemon_status()
        out["runtime_safety"] = service.runtime_safety_status()
        return jsonify(out), (200 if out.get("ok") else 404)

    @app.route("/api/presets/scan", methods=["GET"])
    def preset_scan_info() -> Response:
        found = program_store.find_scan_waypoint()
        if found is None:
            return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
        program_id, wp = found
        return jsonify(
            {
                "ok": True,
                "program_id": program_id,
                "waypoint": wp,
                "servo_deg": wp.get("servo_deg"),
            }
        )

    @app.route("/api/presets/scan/goto", methods=["POST"])
    def preset_scan_goto() -> Response:
        body = request.get_json(silent=True) or {}
        variant = str(body.get("variant") or "base").strip().lower()
        if variant in ("j90_left", "left", "sx"):
            side_target = _scan_side_target("left")
            out = _move_via_safe_transit(side_target)
            out["preset"] = "scan"
            out["scan_variant"] = "j90_left"
            out["waypoint_name"] = "Punto SCANSIONE 90 SX"
            out["target_servo_deg"] = side_target
            return jsonify(out), (200 if out.get("ok") else 502)
        if variant in ("j90_right", "right", "dx"):
            side_target = _scan_side_target("right")
            out = _move_via_safe_transit(side_target)
            out["preset"] = "scan"
            out["scan_variant"] = "j90_right"
            out["waypoint_name"] = "Punto SCANSIONE 90 DX"
            out["target_servo_deg"] = side_target
            return jsonify(out), (200 if out.get("ok") else 502)
        if variant not in ("base", "j90", "90"):
            variant = "base"
        scan_variant = "j90" if variant in ("j90", "90") else "base"
        found = program_store.find_scan_waypoint(variant=scan_variant)
        if found is None:
            reason = "scan_j90_waypoint_not_found" if scan_variant == "j90" else "scan_waypoint_not_found"
            return jsonify({"ok": False, "reason": reason}), 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return jsonify({"ok": False, "reason": "invalid_waypoint"}), 400
        servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
        out = _move_via_safe_transit(servo)
        out["preset"] = "scan"
        out["scan_variant"] = scan_variant
        out["waypoint_name"] = wp.get("name")
        out["target_servo_deg"] = servo
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/programs/<program_id>/waypoints/<waypoint_id>/goto", methods=["POST"])
    def programs_goto_waypoint(program_id: str, waypoint_id: str) -> Response:
        prog = program_store.load_program(program_id)
        if prog is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        wp = next((w for w in (prog.get("waypoints") or []) if w.get("id") == waypoint_id), None)
        if wp is None:
            return jsonify({"ok": False, "reason": "waypoint_not_found"}), 404
        sd = wp.get("servo_deg")
        if not isinstance(sd, list):
            return jsonify({"ok": False, "reason": "invalid_waypoint"}), 400
        service._halt_cartesian_stream(wait_idle=True)
        program_runner.clear_stop_request()
        out = program_runner.move_to_servo_deg_smooth(sd)
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/programs/<program_id>/run", methods=["POST"])
    def programs_run(program_id: str) -> Response:
        service._halt_cartesian_stream(wait_idle=True)
        out = program_runner.run_program(program_id)
        code = 200 if out.get("ok") else 409
        return jsonify(out), code

    @app.route("/api/programs/run/status")
    def programs_run_status() -> Response:
        return jsonify({"ok": True, "status": program_runner.execution_status()})

    @app.route("/api/programs/run/stop", methods=["POST"])
    def programs_run_stop() -> Response:
        return jsonify(program_runner.request_stop())

    @app.route("/api/cartesian/move", methods=["POST"])
    def cartesian_move() -> Response:
        body = request.get_json(silent=True) or {}
        servo_deg, err = _servo_deg_from_body(body)
        if servo_deg is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        try:
            dx = float(body.get("dx_m", body.get("dx", 0)))
            dy = float(body.get("dy_m", body.get("dy", 0)))
            dz = float(body.get("dz_m", body.get("dz", 0)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "dx/dy/dz must be numeric (meters)"}), 400
        out = cartesian.cartesian_move_delta(
            servo_deg,
            dx_m=dx,
            dy_m=dy,
            dz_m=dz,
            interpolated=bool(body.get("interpolated", True)),
        )
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    _mount_motor_health(app)
    # Hermes torna nel processo principale :5056, come nel focus dashboard.
    os.environ.setdefault("GO2_HERMES_INTEGRATED", "1")
    os.environ.setdefault("HERMES_D1_JOG_URL", "http://127.0.0.1:5056")
    from go2_dashboard.hermes.routes import bp as hermes_bp

    app.register_blueprint(hermes_bp)
    # Il daemon deve esistere già all'avvio: health non può dichiarare sano un
    # servizio che possiede solo il file binario.
    daemon_started = service.ensure_command_daemon()
    auto_enable = os.environ.get("D1_JOG_AUTO_ENABLE", "1").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if daemon_started and auto_enable and service.binaries_status().get("real_arm"):
        feedback = service.read_servo_deg(fast=True)
        if feedback.get("ok") and feedback.get("servo_deg"):
            # Sempre with_power: un restart Flask senza power refresh e' una causa
            # tipica di braccio che "cede" per un istante all'avvio.
            atomic_hold = service.couple_and_hold_pose(
                feedback["servo_deg"], with_power=True, force=True
            )
            startup_arm_stabilization.update(
                {
                    "ok": bool(atomic_hold.get("ok") or atomic_hold.get("skipped")),
                    "reason": "stabilized",
                    "feedback": feedback,
                    "coupling_hold": atomic_hold,
                }
            )
        else:
            startup_arm_stabilization.update({"ok": False, "reason": "no_feedback", "feedback": feedback})
    elif not daemon_started:
        startup_arm_stabilization.update({"ok": False, "reason": "daemon_start_failed"})
    else:
        startup_arm_stabilization.update({"ok": True, "skipped": True, "reason": "auto_enable_disabled"})
    return app
