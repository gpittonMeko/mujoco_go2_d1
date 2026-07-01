"""API grasp operator: proxy verso worker HTTP (OpenVLA / AWS / RTX locale)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from flask import Blueprint, Response, jsonify, request

from go2_dashboard.grasp_assessment import (
    build_grasp_validation_ui,
    is_rejected_stub_backend,
    worker_flat_plan_assessment,
)
from go2_dashboard.grasp_ec2_control import ec2_control_available, run_ec2_action
from go2_dashboard.grasp_phased_execute import execute_phased_from_cached_plan
from go2_dashboard.grasp_rgbd_embed import embed_rgbd_into_plan_body
from go2_dashboard.operator_plan_cache import set_last_grasp_plan
from go2_dashboard.paths import PROJECT_ROOT

bp = Blueprint("go2_dashboard_grasp", __name__, url_prefix="/api/grasp")


def _robot_jpeg(device: int) -> bytes | None:
    """JPEG da CameraCache senza importare operator_api (evita ciclo con routes)."""
    try:
        from go2_dashboard.cameras import CAMERA_CACHE
        from go2_dashboard.operator_stack import go2_local

        if go2_local():
            return CAMERA_CACHE.get_jpeg(device)
    except Exception:
        pass
    return None


def _worker_base() -> str:
    return (os.environ.get("GO2_ANYGRASP_WORKER_URL") or "http://127.0.0.1:8765").strip().rstrip("/")


def _proxy_enabled() -> bool:
    return os.environ.get("GO2_ANYGRASP_PROXY", "1").lower() in {"1", "true", "yes", "on"}


def _cloud_mode() -> bool:
    if os.environ.get("GO2_GRASP_CLOUD_MODE", "0").lower() in {"1", "true", "yes", "on"}:
        return True
    base = _worker_base()
    try:
        host = (urllib.parse.urlparse(base).hostname or "").strip()
    except Exception:
        return False
    if not host or host in {"127.0.0.1", "localhost"}:
        return False
    if host.startswith("192.168.") or host.startswith("10."):
        return False
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                if 16 <= int(parts[1]) <= 31:
                    return False
            except ValueError:
                pass
    # IP pubblico / DNS AWS → invia JPEG inline
    return True


def _worker_token() -> str:
    return (os.environ.get("GO2_WORKER_TOKEN") or "").strip()


def _embed_robot_cameras(body: dict[str, Any]) -> dict[str, Any]:
    """Cloud/LAN: JPEG RGB + depth V4L inline verso worker AWS."""
    out = dict(body)
    embed_rgbd = _cloud_mode() or os.environ.get("GO2_GRASP_EMBED_RGBD", "1").lower() in {"1", "true", "yes", "on"}
    if not embed_rgbd:
        return out
    logical = int(out.get("logical_camera_device") or 0)
    if not out.get("jpeg_base64"):
        wrist = _robot_jpeg(0 if logical == 0 else logical)
        if wrist:
            out["jpeg_base64"] = base64.standard_b64encode(wrist).decode("ascii")
    if not out.get("jpeg_base64_front"):
        front = _robot_jpeg(6)
        if front:
            out["jpeg_base64_front"] = base64.standard_b64encode(front).decode("ascii")
    if not out.get("image_url"):
        out["image_url"] = f"embedded://camera/{logical}"
    out = embed_rgbd_into_plan_body(out)
    out["cloud_embedded"] = _cloud_mode()
    return out


def _proxy_json(
    method: str, path: str, body: dict[str, Any] | None = None, timeout_s: float = 60.0
) -> tuple[dict[str, Any], int]:
    url = _worker_base() + path
    data = None
    headers = {"Accept": "application/json"}
    token = _worker_token()
    if token:
        headers["X-Worker-Token"] = token
    if body is not None and method.upper() != "GET":
        payload = _embed_robot_cameras(body) if path == "/plan" else body
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {"ok": True}, resp.getcode() or 200
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(err_body) if err_body.strip() else {"ok": False, "reason": str(exc)}
        except json.JSONDecodeError:
            payload = {"ok": False, "reason": str(exc), "body": err_body[:800]}
        return payload, exc.code
    except Exception as exc:
        return {"ok": False, "reason": "worker_unreachable", "detail": repr(exc), "worker_url": url}, 503


@bp.route("/detection_debug", methods=["GET"])
def grasp_detection_debug_manifest() -> Any:
    """Ultimo manifest bbox (front/wrist) dopo run_full — vedi anche ``data/grasp_debug_manifest.json`` sulla NX."""
    from go2_dashboard.grasp_detect_debug import read_debug_manifest

    return jsonify(read_debug_manifest())


@bp.route("/detection_debug/<tag>.jpg", methods=["GET"])
def grasp_detection_debug_jpg(tag: str) -> Any:
    """JPEG annotato con bbox (tag: front, wrist, wrist_realsense, wrist_orbbec alias)."""
    safe = "".join(c for c in (tag or "").strip() if c.isalnum() or c in {"_", "-"})
    if not safe:
        return Response("bad tag", status=400)
    path = PROJECT_ROOT / "data" / f"grasp_debug_{safe}.jpg"
    if not path.is_file():
        return Response("snapshot not found — premi «Foto SDK (metrica)» o ▸ Prendi", status=404)
    try:
        data = path.read_bytes()
    except OSError:
        return Response("read failed", status=500)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@bp.route("/calibrate_color", methods=["POST"])
def grasp_calibrate_color() -> Any:
    """Auto-calibrazione del colore scatola dal polso (Orbbec).

    Cattura un frame **attraverso il lock Orbbec** (``capture_aligned`` → prelazione cooperativa,
    nessuna seconda pipeline), campiona l'HSV della scatola e fissa le soglie
    ``D1_COLOR_BOX_*`` (in-process + ``data/color_box_calib.json``). Body opzionale:
    ``{"bbox_norm":[x0,y0,x1,y1]}`` o ``{"point_norm":[u,v],"radius_frac":0.06}``;
    se assente usa l'auto-detect del blob piu' saturo. Non muove il braccio.
    """
    body = request.get_json(silent=True) or {}
    import sys as _sys

    try:
        from go2_dashboard.orbbec_wrist_grasp import capture_aligned

        s = str(PROJECT_ROOT / "scripts")
        if s not in _sys.path:
            _sys.path.insert(0, s)
        from box_object_detector import calibrate_color_from_frame, detect_box_object
    except Exception as exc:
        return jsonify({"ok": False, "reason": "import_failed", "detail": repr(exc)}), 500

    cap = capture_aligned()
    if not cap.get("ok"):
        return jsonify({
            "ok": False,
            "reason": cap.get("reason", "capture_failed"),
            "detail": cap.get("detail"),
            "holder": cap.get("holder"),
            "hint_it": cap.get("hint_it"),
        }), 200

    frame = cap["color_bgr"]
    out = calibrate_color_from_frame(
        frame,
        bbox_norm=body.get("bbox_norm"),
        point_norm=body.get("point_norm"),
        radius_frac=float(body.get("radius_frac") or 0.06),
    )
    if out.get("ok"):
        det = detect_box_object(frame)
        out["verify_detection"] = {
            "ok": bool(det.get("ok")),
            "backend": det.get("backend"),
            "confidence": det.get("confidence"),
            "bbox_xyxy": det.get("bbox_xyxy"),
            "orientation_deg": det.get("orientation_deg"),
            "reason": det.get("reason"),
        }
        try:
            from go2_dashboard.grasp_detect_debug import save_detection_snapshot
            from go2_dashboard.orbbec_wrist_grasp import _wrist_debug_tag

            snap = save_detection_snapshot(
                frame, det if isinstance(det, dict) else None,
                tag=_wrist_debug_tag(), logical_camera=0, step="color_calibration",
            )
            out["debug_image_url"] = snap.get("image_url")
        except Exception:
            pass
    return jsonify(out), 200


@bp.route("/rgbd_snapshot", methods=["GET"])
def grasp_rgbd_snapshot() -> Any:
    """RGB da cache dashboard + anteprima depth V4L (non metrica) per worker cloud."""
    logical = int(request.args.get("logical", 0))
    out: dict[str, Any] = {
        "ok": True,
        "logical_camera_device": logical,
        "rgb_ok": False,
        "depth_ok": False,
        "hint_it": "Depth = JPEG UVC grezzo; per metrica serve SDK o env GO2_DEPTH_VIDEO_INDEX_*.",
    }
    rgb = _robot_jpeg(logical)
    if rgb:
        out["rgb_jpeg_b64"] = base64.standard_b64encode(rgb).decode("ascii")
        out["rgb_ok"] = True
    try:
        from go2_dashboard.blueprints.operator_api.helpers_camera import _depth_v4l_index_for_logical_camera
        from go2_dashboard.cameras import debug_v4l_snapshot_jpeg

        didx = _depth_v4l_index_for_logical_camera(logical)
        if didx is not None:
            depth_raw = debug_v4l_snapshot_jpeg(didx, jpeg_quality=55)
            if depth_raw:
                out["depth_jpeg_b64"] = base64.standard_b64encode(depth_raw).decode("ascii")
                out["depth_v4l_index"] = didx
                out["depth_ok"] = True
        else:
            out["depth_skip_reason"] = "no_GO2_DEPTH_VIDEO_INDEX"
    except Exception as exc:
        out["depth_error"] = repr(exc)
    if not out["rgb_ok"]:
        out["ok"] = False
        out["reason"] = "rgb_unavailable"
    return jsonify(out)


def grasp_health_payload() -> dict[str, Any]:
    worker = _worker_base()
    use_proxy = _proxy_enabled()
    out: dict[str, Any] = {
        "ok": True,
        "mode": "unconfigured",
        "worker_url": worker,
        "proxy_enabled": use_proxy,
        "cloud_mode": _cloud_mode(),
        "ec2_control_available": ec2_control_available(),
        "checkpoint_env": (os.environ.get("GO2_ANYGRASP_CHECKPOINT") or "").strip() or None,
        "stub_forbidden": True,
    }
    if use_proxy:
        proxied, code = _proxy_json("GET", "/health", timeout_s=3.0)
        out["worker_reachable"] = code < 500 and bool((proxied or {}).get("ok"))
        out["worker_payload"] = proxied
        if out["worker_reachable"]:
            out["mode"] = "proxy"
            out["ok"] = True
            wp_backend = str((proxied or {}).get("backend") or "")
            if is_rejected_stub_backend(wp_backend):
                out["ok"] = False
                out["reason"] = "worker_stub_backend"
        else:
            out["mode"] = "worker_down"
            out["ok"] = False
            out["reason"] = "worker_unreachable"
            out["hint_it"] = "Avvia EC2 (tab Presa → Avvia EC2) o verifica GO2_ANYGRASP_WORKER_URL / token."
    else:
        out["worker_reachable"] = False
        out["ok"] = False
        out["mode"] = "proxy_disabled"
        out["hint_it"] = (
            "Imposta GO2_ANYGRASP_WORKER_URL e GO2_ANYGRASP_PROXY=1; "
            "GO2_GRASP_CLOUD_MODE=1 per worker AWS (JPEG inline)."
        )
    out["validation_ui"] = build_grasp_validation_ui(health=out, plan=None)
    return out


@bp.route("/health", methods=["GET"])
def grasp_health() -> Any:
    return jsonify(grasp_health_payload())


@bp.route("/ec2/status", methods=["GET"])
def grasp_ec2_status() -> Any:
    return jsonify(
        {
            "ok": True,
            "ec2_control_available": ec2_control_available(),
            **run_ec2_action("status"),
        }
    )


@bp.route("/ec2/start", methods=["POST"])
def grasp_ec2_start() -> Any:
    body = request.get_json(silent=True) or {}
    wait = bool(body.get("wait_health", True))
    out = run_ec2_action("start", wait_health=wait)
    return jsonify(out), 200 if out.get("ok") else 503


@bp.route("/ec2/stop", methods=["POST"])
def grasp_ec2_stop() -> Any:
    out = run_ec2_action("stop")
    return jsonify(out), 200 if out.get("ok") else 503


def grasp_plan_via_worker(body: dict[str, Any], *, timeout_s: float = 120.0) -> tuple[dict[str, Any], int]:
    if not _proxy_enabled():
        return (
            {
                "ok": False,
                "reason": "anygrasp_worker_not_configured",
                "hint_it": "GO2_ANYGRASP_PROXY=0 — abilita proxy verso worker.",
            },
            503,
        )
    payload, code = _proxy_json("POST", "/plan", body=body, timeout_s=timeout_s)
    if isinstance(payload, dict):
        if payload.get("ok") and is_rejected_stub_backend(str(payload.get("backend") or "")):
            payload = {
                "ok": False,
                "reason": "worker_stub_plan_rejected",
                "hint_it": "Il worker ha risposto STUB — vietato in produzione. Su EC2: GO2_GRASP_WORKER_BACKEND=auto o planner.",
                "worker_payload": payload,
            }
            code = 503
        else:
            payload["grasp_assessment"] = worker_flat_plan_assessment(payload)
            payload["selected_grasp_assessment"] = payload["grasp_assessment"]
            health = grasp_health_payload()
            payload["validation_ui"] = build_grasp_validation_ui(health=health, plan=payload)
    if code < 400 and isinstance(payload, dict) and payload.get("ok"):
        if not payload.get("validation_ui", {}).get("can_execute_phased") and os.environ.get(
            "GO2_GRASP_REQUIRE_VALIDATED_EXECUTE", "1"
        ).lower() in {"1", "true", "yes", "on"}:
            payload["execute_blocked"] = True
            payload["execute_block_hint_it"] = payload["validation_ui"].get("banner_it")
        set_last_grasp_plan(payload)
        stamp = PROJECT_ROOT / "data" / "go2_vla_last_plan_unix.txt"
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            import time

            stamp.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass
    return payload, code


def graspgen_infer_via_worker(
    point_cloud_b64: str, *, num_grasps: int = 200, topk: int = 100, timeout_s: float = 90.0
) -> tuple[dict[str, Any], int]:
    """Inoltra una point cloud metrica (b64 di np.save (N,3) float32) al server GraspGen via worker.

    Ritorna ``(payload, http_code)`` con ``grasps_4x4`` + ``confidences`` (camera frame).
    """
    if not _proxy_enabled():
        return ({"ok": False, "reason": "anygrasp_worker_not_configured"}, 503)
    body = {"point_cloud_b64": point_cloud_b64, "num_grasps": int(num_grasps), "topk": int(topk)}
    return _proxy_json("POST", "/graspgen_infer", body=body, timeout_s=timeout_s)


@bp.route("/plan", methods=["POST"])
def grasp_plan() -> Any:
    body = request.get_json(silent=True) or {}
    payload, code = grasp_plan_via_worker(body)
    return jsonify(payload), code


@bp.route("/run_full", methods=["POST"])
def grasp_run_full() -> Any:
    """Sequenza presa completa: frontale → START → pose estimation polso → presa a fasi.

    Body JSON::

        {
          "instruction": "prendi la scatola bianca",
          "confirm": "RUN_FULL_GRASP",   // obbligatorio per muovere il braccio (START + presa)
          "front_camera": 6,              // opz. (default 6)
          "wrist_camera": 0,              // opz. (default 0)
          "goto_start": true,             // opz.
          "execute": true,                // opz.
          "allow_heuristic": false        // opz. override esecuzione su piano euristico
        }

    Senza ``confirm`` la sequenza è un dry-run (detect + piano dalla posa corrente, niente movimenti).
    """
    from go2_dashboard.grasp_full_sequence import run_full_grasp_sequence

    body = request.get_json(silent=True) or {}

    def _int(key: str, default: int) -> int:
        try:
            return int(body.get(key, default))
        except (TypeError, ValueError):
            return default

    def _bool(key: str, default: bool) -> bool:
        v = body.get(key)
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in {"1", "true", "yes", "on"}

    allow_heur = body.get("allow_heuristic")
    out = run_full_grasp_sequence(
        instruction=str(body.get("instruction") or body.get("task") or ""),
        confirm=body.get("confirm"),
        front_camera=_int("front_camera", 6),
        wrist_camera=_int("wrist_camera", 0),
        do_goto_start=_bool("goto_start", True),
        do_execute=_bool("execute", True),
        allow_heuristic_override=None if allow_heur is None else _bool("allow_heuristic", False),
    )
    return jsonify(out), 200 if out.get("ok") else 503


@bp.route("/side_approach_setup", methods=["POST"])
def grasp_side_approach_setup() -> Any:
    """Setup presa di lato: frontale vede l'oggetto → (alzati|2 passi) → gira 90° dx → braccio START 90° sx.

    Evita di avere la testa del cane tra pinza e scatole. Body JSON::

        {
          "instruction": "prendi la scatola blu dgs",
          "confirm": "RUN_SIDE_GRASP_SETUP",  // obbligatorio per muovere cane+braccio
          "posture": "auto",                   // auto | crouch | standing
          "front_camera": 6
        }

    Senza ``confirm`` è un dry-run (detection + piano movimenti). Dopo il setup, usa il Grasp Coach.
    """
    from go2_dashboard.grasp_side_approach import start_side_approach_setup

    body = request.get_json(silent=True) or {}
    try:
        front_cam = int(body.get("front_camera", 6))
    except (TypeError, ValueError):
        front_cam = 6
    out, code = start_side_approach_setup(
        instruction=str(body.get("instruction") or body.get("task") or ""),
        confirm=body.get("confirm"),
        posture=str(body.get("posture") or "auto"),
        front_camera=front_cam,
    )
    return jsonify(out), code


@bp.route("/side_approach_status", methods=["GET"])
def grasp_side_approach_status() -> Any:
    """Stato/avanzamento del flusso «presa di lato» (per polling UI)."""
    from go2_dashboard.grasp_side_approach import side_approach_status

    return jsonify(side_approach_status()), 200


@bp.route("/autonomous_run", methods=["POST"])
def grasp_autonomous_run() -> Any:
    from go2_dashboard.grasp_autonomous_loop import start_autonomous_grasp

    body = request.get_json(silent=True) or {}
    out, code = start_autonomous_grasp(
        instruction=str(body.get("instruction") or body.get("task") or ""),
        confirm=body.get("confirm"),
        color_hint=body.get("color_hint"),
        max_cycles=body.get("max_cycles"),
        use_supervisor=body.get("use_supervisor"),
    )
    return jsonify(out), code


@bp.route("/autonomous_status", methods=["GET"])
def grasp_autonomous_status() -> Any:
    from go2_dashboard.grasp_autonomous_loop import autonomous_grasp_status

    return jsonify(autonomous_grasp_status()), 200


@bp.route("/collect", methods=["POST"])
def grasp_collect() -> Any:
    from go2_dashboard.grasp_collection_mission import start_collect_mission

    body = request.get_json(silent=True) or {}
    targets = body.get("targets")
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(",") if t.strip()]
    try:
        front_cam = int(body.get("front_camera", 6))
    except (TypeError, ValueError):
        front_cam = 6
    out, code = start_collect_mission(
        targets=targets if isinstance(targets, list) else None,
        instruction=str(body.get("instruction") or ""),
        confirm=body.get("confirm"),
        max_picks=body.get("max_picks"),
        front_camera=front_cam,
    )
    return jsonify(out), code


@bp.route("/collect_status", methods=["GET"])
def grasp_collect_status() -> Any:
    from go2_dashboard.grasp_collection_mission import collect_mission_status

    return jsonify(collect_mission_status()), 200


@bp.route("/teach_run", methods=["POST"])
def grasp_teach_run() -> Any:
    """Flusso unificato teach: scan j90 → gate → presa singola o raccolta."""
    import json
    import time

    from go2_dashboard.grasp_teach_flow import start_teach_flow
    from go2_dashboard.paths import PROJECT_ROOT

    # #region agent log
    _t0 = time.perf_counter()

    def _dbg_teach(hypothesis_id: str, message: str, data: dict) -> None:
        try:
            p = PROJECT_ROOT / "data" / "debug-16a61f.ndjson"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "sessionId": "16a61f",
                            "hypothesisId": hypothesis_id,
                            "location": "grasp.py:teach_run",
                            "message": message,
                            "data": data,
                            "timestamp": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass

    _dbg_teach("H2", "teach_run_handler_enter", {})
    # #endregion

    body = request.get_json(silent=True) or {}
    try:
        front_cam = int(body.get("front_camera", 6))
    except (TypeError, ValueError):
        front_cam = 6
    out, code = start_teach_flow(
        instruction=str(body.get("instruction") or body.get("task") or ""),
        confirm=body.get("confirm"),
        color_hint=body.get("color_hint"),
        max_cycles=body.get("max_cycles"),
        max_picks=body.get("max_picks"),
        front_camera=front_cam,
        use_supervisor=body.get("use_supervisor"),
    )

    # #region agent log
    _dbg_teach(
        "H2",
        "teach_run_handler_exit",
        {
            "handler_ms": round((time.perf_counter() - _t0) * 1000.0, 2),
            "http_code": int(code),
            "started": bool(out.get("started")),
            "reason": out.get("reason"),
        },
    )
    # #endregion
    return jsonify(out), code


@bp.route("/teach_status", methods=["GET"])
def grasp_teach_status() -> Any:
    from go2_dashboard.grasp_teach_flow import teach_flow_status

    return jsonify(teach_flow_status()), 200


@bp.route("/teach_cancel", methods=["POST"])
def grasp_teach_cancel() -> Any:
    from go2_dashboard.grasp_teach_flow import cancel_teach_flow

    body = request.get_json(silent=True) or {}
    out = cancel_teach_flow(reason_it=str(body.get("reason_it") or "").strip() or None)
    return jsonify(out), 200


@bp.route("/execute_phased", methods=["POST"])
def grasp_execute_phased() -> Any:
    """Esegue preview IK a fasi sul D1 (pre_grasp → grasp → lift) con gate assessment."""
    body = request.get_json(silent=True) or {}
    out = execute_phased_from_cached_plan(
        confirm=body.get("confirm"),
        max_stages=body.get("max_stages"),
        allow_heuristic_override=body.get("allow_heuristic_override"),
    )
    return jsonify(out), 200 if out.get("ok") else 503


@bp.route("/execute", methods=["POST"])
def grasp_execute() -> Any:
    body = request.get_json(silent=True) or {}
    if _proxy_enabled():
        payload, code = _proxy_json("POST", "/execute", body=body, timeout_s=120.0)
        return jsonify(payload), code
    return (
        jsonify(
            {
                "ok": False,
                "reason": "anygrasp_worker_not_configured",
                "hint_it": "Worker grasp non raggiungibile.",
            }
        ),
        503,
    )
