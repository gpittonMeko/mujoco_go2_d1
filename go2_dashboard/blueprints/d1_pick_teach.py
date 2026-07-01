"""API Orbbec RGB + pick teach — condivisa tra dashboard 5052 e D1 jog 5053."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context

from go2_dashboard.d1_jog import (
    orbbec_capture,
    pick_preset,
    pick_vision,
    program_runner,
    program_store,
    service,
    vision_streams,
)
from go2_dashboard.paths import PROJECT_ROOT

bp = Blueprint("d1_pick_teach", __name__)

_PICK_CAMERA_PREF: dict[str, str] = {
    "detect_camera": (os.environ.get("D1_PICK_DETECT_CAMERA") or "wrist").strip().lower(),
    "grasp_camera": (os.environ.get("D1_PICK_GRASP_CAMERA") or "wrist").strip().lower(),
}
_PICK_CAMERA_PREF_LOCK = threading.Lock()
_RS_PANEL_LOCK = threading.Lock()
_RS_PANEL_CACHE: dict[str, dict[str, Any]] = {
    "wrist": {"ts": 0.0, "panels": {}},
    "front": {"ts": 0.0, "panels": {}},
}
_TUNING_CYCLES_PATH = PROJECT_ROOT / "data" / "pick_tuning_cycles.jsonl"
_DETECTOR_CONFIG_PATH = PROJECT_ROOT / "data" / "d1_pick_detector_config.json"

_DETECTOR_MODEL_PRESETS: dict[str, dict[str, str]] = {
    "hsv_strict": {
        "D1_PICK_DETECT_BACKEND": "color",
        "D1_PICK_COLOR_ONLY": "1",
        "GO2_CLASSIC_BOX_FALLBACK": "1",
        "D1_COLOR_BOX_MIN_AREA_FRAC": "0.004",
        "D1_COLOR_BOX_MIN_SOLIDITY": "0.55",
        "D1_COLOR_BOX_RELAX_H_PAD": "14",
        "D1_COLOR_BOX_RELAX_S_SCALE": "0.55",
        "D1_COLOR_BOX_RELAX_V_SCALE": "0.50",
    },
    "hsv_robust": {
        "D1_PICK_DETECT_BACKEND": "color",
        "D1_PICK_COLOR_ONLY": "1",
        "GO2_CLASSIC_BOX_FALLBACK": "1",
        "D1_COLOR_BOX_MIN_AREA_FRAC": "0.0015",
        "D1_COLOR_BOX_MIN_SOLIDITY": "0.42",
        "D1_COLOR_BOX_RELAX_H_PAD": "18",
        "D1_COLOR_BOX_RELAX_S_SCALE": "0.45",
        "D1_COLOR_BOX_RELAX_V_SCALE": "0.40",
    },
    "yolo_classic": {
        "D1_PICK_DETECT_BACKEND": "color_then_yolo",
        "D1_PICK_COLOR_ONLY": "0",
        "GO2_CLASSIC_BOX_FALLBACK": "1",
    },
}

_DETECTOR_PARAM_SPECS: dict[str, dict[str, Any]] = {
    "D1_COLOR_BOX_H_MIN": {"type": "int", "min": 0, "max": 179, "default": 95},
    "D1_COLOR_BOX_H_MAX": {"type": "int", "min": 0, "max": 179, "default": 130},
    "D1_COLOR_BOX_S_MIN": {"type": "int", "min": 0, "max": 255, "default": 45},
    "D1_COLOR_BOX_V_MIN": {"type": "int", "min": 0, "max": 255, "default": 35},
    "D1_COLOR_BOX_MIN_AREA_FRAC": {"type": "float", "min": 0.0003, "max": 0.08, "default": 0.004},
    "D1_COLOR_BOX_MIN_SOLIDITY": {"type": "float", "min": 0.2, "max": 1.0, "default": 0.55},
    "D1_COLOR_BOX_MAX_BOTTOM_Y_FRAC": {"type": "float", "min": 0.45, "max": 0.98, "default": 0.72},
    "D1_COLOR_BOX_RELAX_H_PAD": {"type": "int", "min": 0, "max": 40, "default": 14},
    "D1_COLOR_BOX_RELAX_S_SCALE": {"type": "float", "min": 0.2, "max": 1.2, "default": 0.55},
    "D1_COLOR_BOX_RELAX_V_SCALE": {"type": "float", "min": 0.2, "max": 1.2, "default": 0.50},
    "D1_COLOR_BOX_COMP_TARGET_V_MED": {"type": "float", "min": 40.0, "max": 180.0, "default": 92.0},
    "D1_COLOR_BOX_COMP_MAX_GAIN": {"type": "float", "min": 1.0, "max": 4.0, "default": 2.4},
    "D1_COLOR_BOX_COMP_BETA": {"type": "int", "min": -40, "max": 80, "default": 6},
    "D1_COLOR_BOX_COMP_CLAHE_CLIP": {"type": "float", "min": 1.0, "max": 6.0, "default": 2.2},
    "D1_COLOR_BOX_COMP_CLAHE_TILE": {"type": "int", "min": 4, "max": 32, "default": 8},
}


def _read_detector_config_file() -> dict[str, Any]:
    try:
        raw = json.loads(_DETECTOR_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _save_detector_config_file(doc: dict[str, Any]) -> None:
    _DETECTOR_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DETECTOR_CONFIG_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _sanitize_detector_value(name: str, value: Any) -> float | int | None:
    spec = _DETECTOR_PARAM_SPECS.get(name)
    if spec is None or value is None:
        return None
    try:
        out: float | int
        if spec["type"] == "int":
            out = int(round(float(value)))
        else:
            out = float(value)
    except (TypeError, ValueError):
        return None
    out = max(spec["min"], min(spec["max"], out))
    if spec["type"] == "int":
        return int(out)
    return round(float(out), 5)


def _detector_model_mode_from_env() -> str:
    backend = (os.environ.get("D1_PICK_DETECT_BACKEND") or "color").strip().lower()
    color_only = (os.environ.get("D1_PICK_COLOR_ONLY") or "1").strip().lower() not in {"0", "false", "no", "off"}
    if not color_only and backend in {"color_then_yolo", "yolo", "ultralytics", "classic", "classic_contour"}:
        return "yolo_classic"
    if os.environ.get("D1_COLOR_BOX_MIN_SOLIDITY", "") == "0.42" or os.environ.get("D1_COLOR_BOX_MIN_AREA_FRAC", "") == "0.0015":
        return "hsv_robust"
    return "hsv_strict"


def _apply_detector_preset(mode: str) -> str:
    mode_norm = str(mode or "").strip().lower()
    if mode_norm not in _DETECTOR_MODEL_PRESETS:
        mode_norm = "hsv_strict"
    for k, v in _DETECTOR_MODEL_PRESETS[mode_norm].items():
        os.environ[k] = str(v)
    return mode_norm


def _effective_detector_params() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, spec in _DETECTOR_PARAM_SPECS.items():
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            out[name] = spec["default"]
            continue
        parsed = _sanitize_detector_value(name, raw)
        out[name] = spec["default"] if parsed is None else parsed
    return out


def _detector_config_payload() -> dict[str, Any]:
    cfg = _read_detector_config_file()
    return {
        "ok": True,
        "path": str(_DETECTOR_CONFIG_PATH),
        "saved": cfg,
        "mode": _detector_model_mode_from_env(),
        "params": _effective_detector_params(),
        "param_specs": _DETECTOR_PARAM_SPECS,
    }


def _apply_detector_saved_config_once() -> None:
    cfg = _read_detector_config_file()
    if not cfg:
        return
    try:
        mode = _apply_detector_preset(cfg.get("mode") or "hsv_strict")
        params = cfg.get("params") if isinstance(cfg.get("params"), dict) else {}
        for name in _DETECTOR_PARAM_SPECS:
            if name in params:
                parsed = _sanitize_detector_value(name, params.get(name))
                if parsed is not None:
                    os.environ[name] = str(parsed)
        cfg["mode"] = mode
    except Exception:
        return


_apply_detector_saved_config_once()


def _color_stream_source_setting(camera_role: str) -> str:
    role = _normalize_camera_role(camera_role)
    env_key = "D1_PICK_WRIST_COLOR_SOURCE" if role == "wrist" else "D1_PICK_FRONT_COLOR_SOURCE"
    # A D456 V4L node can expose IR or false-color depth. Those frames may
    # still pass chroma diagnostics, so the wrist RGB panel uses SDK color only.
    default = "realsense_only" if role == "wrist" else "cache_first"
    val = str(os.environ.get(env_key, default) or default).strip().lower()
    if val not in {"cache_first", "realsense_first", "cache_only", "realsense_only"}:
        val = default
    return val


def _color_stream_source_order(camera_role: str) -> list[str]:
    mode = _color_stream_source_setting(camera_role)
    if mode == "cache_only":
        return ["cache"]
    if mode == "realsense_only":
        return ["realsense"]
    if mode == "cache_first":
        return ["cache", "realsense"]
    return ["realsense", "cache"]


def _normalize_camera_role(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in {"0", "wrist", "polso", "camera0", "cam0"}:
        return "wrist"
    if s in {"6", "front", "frontal", "frontale", "camera6", "cam6"}:
        return "front"
    return "wrist"


def _logical_for_role(role: str) -> int:
    return 0 if _normalize_camera_role(role) == "wrist" else 6


def _camera_pref() -> dict[str, Any]:
    with _PICK_CAMERA_PREF_LOCK:
        detect = _normalize_camera_role(_PICK_CAMERA_PREF.get("detect_camera", "wrist"))
        grasp = _normalize_camera_role(_PICK_CAMERA_PREF.get("grasp_camera", "wrist"))
        _PICK_CAMERA_PREF["detect_camera"] = detect
        _PICK_CAMERA_PREF["grasp_camera"] = grasp
        return {
            "detect_camera": detect,
            "grasp_camera": grasp,
            "detect_logical": _logical_for_role(detect),
            "grasp_logical": _logical_for_role(grasp),
        }


def _upsert_camera_pref(body: dict[str, Any]) -> dict[str, Any]:
    with _PICK_CAMERA_PREF_LOCK:
        if "detect_camera" in body:
            _PICK_CAMERA_PREF["detect_camera"] = _normalize_camera_role(body.get("detect_camera"))
        if "grasp_camera" in body:
            _PICK_CAMERA_PREF["grasp_camera"] = _normalize_camera_role(body.get("grasp_camera"))
    return _camera_pref()


def _force_wrist_detect_enabled() -> bool:
    return os.environ.get("GO2_PICK_FORCE_WRIST_DETECT", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _resolve_detect_role(body: dict[str, Any], pref: dict[str, Any]) -> str:
    role = _normalize_camera_role(body.get("detect_camera") or pref.get("detect_camera"))
    if _force_wrist_detect_enabled():
        return "wrist"
    return role


def _reset_motion_lock_for_pick() -> None:
    if os.environ.get("GO2_PICK_FORCE_IDLE_BEFORE_AUTOMOVE", "1").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    try:
        from go2_dashboard import d1_arm_motion

        d1_arm_motion.end_live_session(skip_hold=True)
    except Exception:
        pass
    try:
        service.motion_force_idle()
    except Exception:
        pass


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


def _pick_scene_jpeg() -> Response:
    path = pick_vision.scene_overlay_path()
    if not path.is_file():
        return jsonify({"ok": False, "reason": "no_scene_overlay"}), 404
    return send_file(path, mimetype="image/jpeg", max_age=0)


def _last_detection_payload() -> dict[str, Any]:
    preset = pick_preset.load_preset()
    ld = preset.get("last_detection") if isinstance(preset, dict) else None
    overlay = pick_vision.scene_overlay_path()
    ts = int(time.time())
    return {
        "ok": True,
        "has_last_detection": isinstance(ld, dict),
        "last_detection": ld if isinstance(ld, dict) else None,
        "preview_available": overlay.is_file(),
        "preview_url": f"/api/pick/scene.jpg?t={ts}" if overlay.is_file() else None,
        "camera_select": _camera_pref(),
    }


def _recent_valid_wrist_detection() -> tuple[bool, str, dict[str, Any] | None]:
    preset = pick_preset.load_preset()
    ld = preset.get("last_detection") if isinstance(preset, dict) else None
    if not isinstance(ld, dict):
        return False, "missing_last_detection", None
    if not bool(ld.get("detected")):
        return False, "last_detection_not_detected", ld
    logical = ld.get("logical_camera")
    if logical is not None:
        try:
            if int(logical) != 0:
                return False, "last_detection_not_wrist", ld
        except Exception:
            return False, "last_detection_bad_logical", ld
    ts_raw = str(ld.get("at") or "").strip()
    if ts_raw:
        try:
            ts = time.mktime(time.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S"))
            age_s = max(0.0, time.time() - ts)
            max_age_s = float(os.environ.get("GO2_PICK_MAX_DETECTION_AGE_S", "6.0"))
            if age_s > max_age_s:
                return False, "last_detection_stale", ld
        except Exception:
            pass
    return True, "ok", ld


def _detect_on_logical_camera(logical: int) -> dict[str, Any]:
    try:
        import cv2
        import numpy as np
        from go2_dashboard import cameras as cameras_mod
    except Exception as exc:
        return {"ok": False, "reason": "camera_backend_unavailable", "error": repr(exc)}

    logical = int(logical)
    if logical not in {0, 6}:
        return {"ok": False, "reason": "bad_logical_camera", "logical_camera": logical}

    try:
        cameras_mod.CAMERA_CACHE.start(logical)
        jpg = cameras_mod.CAMERA_CACHE.get_jpeg(logical, wait_s=2.5)
    except Exception as exc:
        return {"ok": False, "reason": "camera_frame_error", "error": repr(exc), "logical_camera": logical}
    if not jpg:
        return {"ok": False, "reason": "no_frame", "logical_camera": logical}
    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return {"ok": False, "reason": "jpeg_decode_failed", "logical_camera": logical}

    scripts_dir = str(PROJECT_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from box_object_detector import detect_box_object, detector_status

    det = pick_preset.stabilize_detection_orientation(detect_box_object(frame))
    overlay = frame.copy()
    if hasattr(pick_vision, "_draw_detection"):
        try:
            overlay = pick_vision._draw_detection(overlay, det)
        except Exception:
            overlay = frame
    ok_enc, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), int(os.environ.get("D1_ORBBEC_JPEG_QUALITY", "88"))])
    if ok_enc and buf is not None:
        pick_vision.scene_overlay_path().parent.mkdir(parents=True, exist_ok=True)
        pick_vision.scene_overlay_path().write_bytes(buf.tobytes())

    ts = int(time.time())
    last_detection = pick_preset.stabilize_detection_orientation(
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "backend": det.get("backend"),
            "label": det.get("label"),
            "confidence": det.get("confidence"),
            "grip_center_px": det.get("grip_center_px"),
            "bbox_xyxy": det.get("bbox_xyxy"),
            "norm": det.get("norm"),
            "orientation_deg": det.get("orientation_deg"),
            "orient_axis_px": det.get("orient_axis_px"),
            "orient_box_px": det.get("orient_box_px"),
            "grip_align_deg": det.get("grip_align_deg"),
            "grip_align_axis_px": det.get("grip_align_axis_px"),
            "detect_method": det.get("detect_method"),
            "detected": bool(det.get("ok")),
            "logical_camera": logical,
        }
    )
    return {
        "ok": True,
        "detection_ok": bool(det.get("ok")),
        "detection": det,
        "last_detection": last_detection,
        "detector_status": detector_status(),
        "preview_url": f"/api/pick/scene.jpg?t={ts}",
        "image_url": f"/api/robot/camera/{logical}.jpg?t={ts}",
        "logical_camera": logical,
        "hint_it": (
            "Oggetto rilevato dalla camera selezionata."
            if det.get("ok")
            else "Nessuna detection valida dalla camera selezionata."
        ),
    }


def _capture_and_detect_isolated() -> dict[str, Any]:
    """Esegue il capture/detect in un processo isolato per evitare crash del server."""
    timeout_s = float(os.environ.get("D1_PICK_SNAPSHOT_TIMEOUT_S", "90"))
    code = (
        "import json; "
        "from go2_dashboard.d1_jog import pick_vision; "
        "out = pick_vision.capture_and_detect(); "
        "print('RESULT:' + json.dumps(out, ensure_ascii=False))"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": "pick_snapshot_timeout",
            "hint": "Capture D1 troppo lento: riprova o controlla il backend camera.",
        }

    result_line = None
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("RESULT:"):
            result_line = line[len("RESULT:") :]
            break
    if result_line is not None:
        try:
            return json.loads(result_line)
        except json.JSONDecodeError:
            pass

    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": "pick_snapshot_subprocess_failed",
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-800:],
            "stderr_tail": (proc.stderr or "")[-800:],
            "hint": "Il worker camera ha fallito senza chiudere Flask; riprova o controlla il log del backend.",
        }

    return {
        "ok": False,
        "reason": "pick_snapshot_no_result",
        "stdout_tail": (proc.stdout or "")[-800:],
        "stderr_tail": (proc.stderr or "")[-800:],
    }


def _pick_gripper_move(j6_target: float, *, action: str) -> tuple[Response, int]:
    fb = service.read_servo_deg(fast=True)
    if not fb.get("ok") or not fb.get("servo_deg"):
        return jsonify({"ok": False, "reason": fb.get("reason", "no_feedback")}), 502
    cur = list(fb["servo_deg"])
    target = service.clamp_servo_deg(cur[:7])
    target[6] = round(float(j6_target), 3)
    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        return jsonify(couple), 502
    out = program_runner.move_to_servo_deg_smooth(target)
    out["action"] = action
    out["gripper_target_deg"] = target[6]
    out["target_servo_deg"] = target
    code = 200 if out.get("ok") else 502
    return jsonify(out), code


def _scan_variant_from_body(body: dict[str, Any], *, default: str = "base") -> str:
    raw = str(body.get("scan_variant") or body.get("variant") or default).strip().lower()
    left_aliases = {"j90_left", "left", "sinistra", "-90", "j90_opposite", "opposite", "altro_lato", "other_side"}
    right_aliases = {"j90", "j90_right", "right", "destra", "90", "+90"}
    if raw in left_aliases:
        return "j90_left"
    if raw in right_aliases:
        return "j90"
    return "base"


def _detector_status_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    det = snapshot.get("detection")
    if isinstance(det, dict) and isinstance(det.get("detector_status"), dict):
        return det["detector_status"]
    return {}


def _detector_model_is_generic(status: dict[str, Any]) -> bool:
    model_path = str(status.get("model_path") or "").replace("\\", "/").lower()
    if model_path.endswith(("/yolov8n.pt", "/yolo11n.pt", "/yolov8s.pt", "/yolo11s.pt")):
        return True
    labels = status.get("trained_labels")
    if isinstance(labels, list):
        label_set = {str(x).strip().lower() for x in labels}
        coco_core = {"person", "bicycle", "car", "umbrella", "bottle", "cup", "dining table"}
        if len(label_set) >= 70 and coco_core.issubset(label_set):
            return True
    return False


def _teach_model_active() -> tuple[bool, dict[str, Any]]:
    try:
        from go2_dashboard.d1_jog import pick_teach_model

        data = pick_preset.load_preset()
        info = pick_teach_model.list_teach_samples()
        return pick_teach_model.model_is_active(data), info
    except Exception as exc:
        return False, {"ok": False, "reason": "teach_model_status_error", "error": str(exc)}


def _read_tuning_cycles(limit: int = 80) -> list[dict[str, Any]]:
    p = _TUNING_CYCLES_PATH
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except Exception:
        return []
    if limit > 0:
        rows = rows[-int(limit):]
    return rows


def _append_tuning_cycle(body: dict[str, Any]) -> dict[str, Any]:
    _TUNING_CYCLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    item: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "side": str(body.get("side") or body.get("scan_variant") or "unknown"),
        "result": str(body.get("result") or "unknown"),
        "error_cm": body.get("error_cm"),
        "note": str(body.get("note") or "").strip()[:300],
    }
    if body.get("detect") is not None:
        item["detect"] = bool(body.get("detect"))
    try:
        item["error_cm"] = round(float(item.get("error_cm") or 0.0), 2)
    except Exception:
        item["error_cm"] = None
    try:
        preset = pick_preset.load_preset()
        ld = preset.get("last_detection") if isinstance(preset, dict) else None
        if isinstance(ld, dict):
            item["last_detection"] = {
                "label": ld.get("label"),
                "confidence": ld.get("confidence"),
                "orientation_deg": ld.get("orientation_deg"),
                "grip_center_px": ld.get("grip_center_px"),
            }
        if isinstance(preset, dict):
            item["joint_offset_deg"] = preset.get("joint_offset_deg")
            item["manual_orient_offset_deg"] = preset.get("manual_orient_offset_deg")
    except Exception:
        pass
    with _TUNING_CYCLES_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=True) + "\n")
    return item


# --- Braccio D1 (stesse API di :5053) — Pick teach integrato su :5052 ---
@bp.route("/api/joints/feedback", methods=["GET"])
def joints_feedback() -> Response:
    return jsonify(service.read_servo_deg())


@bp.route("/api/joints/jog", methods=["POST"])
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


@bp.route("/api/joints/session_begin", methods=["POST"])
def joints_session_begin() -> Response:
    body = request.get_json(silent=True) or {}
    servo, err = _servo_deg_from_body(body) if body.get("servo_deg") else (None, None)
    if err:
        return jsonify({"ok": False, "reason": err}), 400
    return jsonify(service.joint_control_begin(servo_deg=servo))


@bp.route("/api/joints/session_end", methods=["POST"])
def joints_session_end() -> Response:
    return jsonify(service.joint_control_end())


@bp.route("/api/joints/release", methods=["POST"])
def joints_release() -> Response:
    body = request.get_json(silent=True) or {}
    release_enabled = os.environ.get("GO2_ENABLE_JOINT_RELEASE", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not release_enabled:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "release_globally_disabled",
                    "hint_it": "Release giunti disabilitato globalmente (GO2_ENABLE_JOINT_RELEASE=0).",
                }
            ),
            403,
        )
    allow_unsafe = os.environ.get("GO2_ALLOW_UNSAFE_RELEASE", "0").strip().lower() in {"1", "true", "yes", "on"}
    has_confirm = str(body.get("confirm") or "").strip().upper() == "ARM_RELEASE_JOINTS"
    has_ack = bool(body.get("ack_gravity_risk"))
    if not allow_unsafe and not (has_confirm and has_ack):
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "release_requires_explicit_confirm",
                    "hint_it": "Release bloccato: invia confirm=ARM_RELEASE_JOINTS e ack_gravity_risk=true.",
                }
            ),
            403,
        )
    out = service.motor_release()
    out["funcode"] = 5
    out["action"] = "motor_release"
    code = 200 if out.get("ok") or out.get("skipped") else 502
    return jsonify(out), code


@bp.route("/api/joints/zero", methods=["POST"])
def joints_zero() -> Response:
    out = service.go_zero()
    code = 200 if out.get("ok") or out.get("skipped") else 502
    return jsonify(out), code


@bp.route("/api/arm/status", methods=["GET"])
def arm_status() -> Response:
    return jsonify({"ok": True, "arm_coupled": service.arm_coupled()})


@bp.route("/api/arm/motion/reset", methods=["POST"])
def arm_motion_reset() -> Response:
    """Clear a stale software motion lock without releasing motor torque."""
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm") or "").strip().upper() != "RESET_ARM_MOTION":
        return jsonify({"ok": False, "reason": "confirm_required"}), 403

    from go2_dashboard.d1_jog.motion_guard import force_idle, status as motion_status

    before = motion_status()
    stop = program_runner.request_stop()
    service._halt_cartesian_stream(wait_idle=True)
    force_idle()
    after = motion_status()
    return jsonify(
        {
            "ok": True,
            "action": "arm_motion_reset",
            "torque_released": False,
            "funcode5_required_on_next_motion": True,
            "before": before,
            "after": after,
            "program_stop": stop,
        }
    )


@bp.route("/api/arm/couple", methods=["POST"])
def arm_couple() -> Response:
    body = request.get_json(silent=True) or {}
    out = service.ensure_coupled(
        with_power=bool(body.get("with_power")),
        force=bool(body.get("force")),
    )
    code = 200 if out.get("ok") or out.get("skipped") else 502
    return jsonify(out), code


@bp.route("/api/orbbec/capture", methods=["POST"])
def orbbec_capture_frame() -> Response:
    out = orbbec_capture.capture_orbbec_jpeg()
    code = 200 if out.get("ok") else 502
    return jsonify(out), code

@bp.route("/api/orbbec/live.mjpg")
def orbbec_live_mjpeg() -> Response:
    return Response(
        stream_with_context(orbbec_capture.generate_rgb_mjpeg_stream()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@bp.route("/api/orbbec/streams", methods=["GET"])
def orbbec_streams() -> Response:
    return jsonify(orbbec_capture.orbbec_stream_catalog())


@bp.route("/api/orbbec/stream.mjpg")
def orbbec_stream_mjpeg() -> Response:
    kind = request.args.get("kind", "rgb")
    idx_raw = request.args.get("index", "").strip()
    try:
        idx = int(idx_raw) if idx_raw else None
    except ValueError:
        idx = None
    return Response(
        stream_with_context(orbbec_capture.generate_orbbec_stream_mjpeg(kind, index=idx)),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@bp.route("/api/pick/vision/streams", methods=["GET"])
def api_pick_vision_streams() -> Response:
    pref = _camera_pref()
    return jsonify(
        {
            "ok": True,
            "cameras": [
                {"key": "wrist", "label": "Polso", "logical": 0, "description": "Camera polso log.0"},
                {"key": "front", "label": "Frontale", "logical": 6, "description": "Camera frontale log.6"},
            ],
            "streams": [
                {"key": "color", "label": "Color", "description": "RGB"},
                {"key": "depth", "label": "Depth", "description": "Mappa profondità"},
                {"key": "ir1", "label": "IR1", "description": "Infrarosso sinistro"},
                {"key": "ir2", "label": "IR2", "description": "Infrarosso destro"},
                {"key": "grid", "label": "Grid", "description": "Quadro riassuntivo"},
            ],
            "default_panels": {
                "wrist": {"rgb": "color", "depth": "depth", "ir": "ir1", "meta": "ir2"},
                "front": {"rgb": "color", "depth": "depth", "ir": "ir1", "meta": "ir2"},
            },
            "selection": pref,
        }
    )


def _panel_placeholder_jpeg(cv2: Any, *, camera_role: str, panel: str) -> bytes:
    cam_it = "polso" if camera_role == "wrist" else "frontale"
    ph = vision_streams.placeholder_bgr(cv2, title=f"{cam_it.upper()} {panel.upper()}", subtitle="attesa stream…")
    return vision_streams.encode_jpeg(ph, cv2) or b""


def _cache_stats_rgb_usable(stats: Any) -> bool:
    """Accept a camera-cache frame only when diagnostics confirm real color."""
    if not isinstance(stats, dict) or not stats.get("available"):
        return False
    return stats.get("rgb_like") is True and str(stats.get("stream_kind") or "").lower() == "rgb"


def _read_color_jpeg_from_cache(cameras_mod: Any, *, camera_role: str) -> bytes | None:
    logical = _logical_for_role(camera_role)
    try:
        cameras_mod.CAMERA_CACHE.start(logical)
        stats = cameras_mod.CAMERA_CACHE.stats().get(str(logical), {})
        if not _cache_stats_rgb_usable(stats):
            return None
        jpg = cameras_mod.CAMERA_CACHE.peek_jpeg(logical)
        if jpg is not None:
            return jpg
        jpg = cameras_mod.CAMERA_CACHE.get_jpeg(logical, wait_s=0.8)
        # The cache can change source while waiting (for example when an SDK
        # depth capture releases V4L). Re-check before publishing the frame.
        stats = cameras_mod.CAMERA_CACHE.stats().get(str(logical), {})
        return jpg if jpg is not None and _cache_stats_rgb_usable(stats) else None
    except Exception:
        return None


def _refresh_realsense_panels(
    camera_role: str,
    *,
    cv2: Any,
    include_ir: bool = False,
) -> dict[str, bytes | None]:
    try:
        from go2_dashboard import realsense_pyrs as rp
    except Exception:
        rp = None

    panels: dict[str, bytes | None] = {"color": None, "depth": None, "ir1": None, "ir2": None, "grid": None}
    role = _normalize_camera_role(camera_role)

    if rp is not None and role == "wrist":
        try:
            peek = rp.peek_bundle()
            if isinstance(peek, dict) and peek.get("color") is not None:
                wrist = vision_streams.bundle_preview_jpegs(peek, cv2)
                for k in panels:
                    panels[k] = wrist.get(k)
                return panels
        except Exception:
            pass

    if rp is None:
        return panels

    cap = rp.capture_aligned_on_demand(
        role=role,
        fast=False,
        force_full=True,
        include_ir=include_ir,
    )
    if not cap.get("ok"):
        return panels
    color = cap.get("color_bgr")
    depth = cap.get("depth_u16")
    ir = cap.get("ir_u8")
    ir2 = cap.get("ir2_u8")
    bundle = {
        "color": color,
        "depth_mm": depth,
        "ir": ir,
        "ir1": ir,
        "ir2": ir2,
    }
    return vision_streams.bundle_preview_jpegs(bundle, cv2)


def _cached_panel_jpeg(camera_role: str, panel: str, *, cv2: Any) -> bytes | None:
    role = _normalize_camera_role(camera_role)
    with _RS_PANEL_LOCK:
        cache = _RS_PANEL_CACHE.setdefault(role, {"ts": 0.0, "panels": {}})
        ts = float(cache.get("ts") or 0.0)
        if time.time() - ts > float(os.environ.get("D1_PICK_STREAM_REFRESH_S", "0.9")):
            cache["panels"] = _refresh_realsense_panels(
                role,
                cv2=cv2,
                include_ir=panel in {"ir1", "ir2", "grid"},
            )
            cache["ts"] = time.time()
        panels = cache.get("panels") or {}
        return panels.get(panel)


def _pick_vision_stream_generator(panel: str, *, camera_role: str = "wrist"):
    import cv2

    role = _normalize_camera_role(camera_role)
    panel = (panel or "color").strip().lower()
    if panel not in {"color", "depth", "ir1", "ir2", "grid"}:
        panel = "color"
    period = float(os.environ.get("VISION_STREAM_MJPEG_PERIOD_S", "0.08"))
    last_out: bytes | None = _panel_placeholder_jpeg(cv2, camera_role=role, panel=panel)

    try:
        from go2_dashboard import cameras as cameras_mod
    except Exception:
        cameras_mod = None

    while True:
        try:
            jpg: bytes | None = None
            if panel == "color" and cameras_mod is not None:
                for source in _color_stream_source_order(role):
                    if source == "cache" and cameras_mod is not None:
                        jpg = _read_color_jpeg_from_cache(cameras_mod, camera_role=role)
                    elif source == "realsense":
                        # Fallback robusto: cattura on-demand RealSense (evita frame stale/pausa cache).
                        jpg = _cached_panel_jpeg(role, "color", cv2=cv2)
                    if jpg:
                        break
            else:
                jpg = _cached_panel_jpeg(role, panel, cv2=cv2)
            if jpg:
                last_out = jpg
            out = last_out or _panel_placeholder_jpeg(cv2, camera_role=role, panel=panel)
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + out + b"\r\n"
        except Exception:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + (last_out or b"") + b"\r\n"
        time.sleep(period)


@bp.route("/api/pick/vision/stream.mjpg")
def api_pick_vision_stream_mjpg() -> Response:
    panel = request.args.get("panel", "color")
    camera_role = request.args.get("camera", request.args.get("role", "wrist"))
    return Response(
        stream_with_context(_pick_vision_stream_generator(panel, camera_role=camera_role)),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@bp.route("/api/pick/vision/rgb_health")
def api_pick_vision_rgb_health() -> Response:
    role = _normalize_camera_role(request.args.get("camera", "wrist"))
    logical = _logical_for_role(role)
    camera_cache_stats: dict[str, Any] = {}
    try:
        from go2_dashboard import cameras as cameras_mod

        cameras_mod.CAMERA_CACHE.start(logical)
        camera_cache_stats = cameras_mod.CAMERA_CACHE.stats().get(str(logical), {}) or {}
    except Exception as exc:
        camera_cache_stats = {"available": False, "error": str(exc)}

    with _RS_PANEL_LOCK:
        st = _RS_PANEL_CACHE.get(role, {"ts": 0.0, "panels": {}})
        last_ts = float(st.get("ts") or 0.0)
        panels = st.get("panels") or {}
    age_s = round(max(0.0, time.time() - last_ts), 3) if last_ts > 0 else None
    return jsonify(
        {
            "ok": True,
            "camera": role,
            "logical": logical,
            "color_source_mode": _color_stream_source_setting(role),
            "color_source_order": _color_stream_source_order(role),
            "camera_cache": camera_cache_stats,
            "panel_cache": {
                "age_s": age_s,
                "has_color": bool(panels.get("color")),
                "has_depth": bool(panels.get("depth")),
                "has_ir1": bool(panels.get("ir1")),
                "has_ir2": bool(panels.get("ir2")),
            },
        }
    )


@bp.route("/api/pick/vision/realsense/reset", methods=["POST"])
def api_pick_vision_realsense_reset() -> Response:
    body = request.get_json(silent=True) or {}
    role = _normalize_camera_role(body.get("camera") or body.get("role") or "wrist")
    logical = _logical_for_role(role)
    try:
        from go2_dashboard import realsense_pyrs as rp
    except Exception as exc:
        return jsonify({"ok": False, "reason": "realsense_module_unavailable", "error": str(exc)}), 503

    try:
        rp.stop()
    except Exception:
        pass
    time.sleep(float(os.environ.get("D1_PICK_RS_RESET_PAUSE_S", "0.45")))
    cap = rp.capture_aligned_on_demand(
        role=role,
        fast=False,
        force_full=True,
        include_ir=False,
    )

    panels: dict[str, bytes | None] = {}
    if cap.get("ok"):
        try:
            import cv2

            bundle = {
                "color": cap.get("color_bgr"),
                "depth_mm": cap.get("depth_u16"),
                "ir": cap.get("ir_u8"),
                "ir1": cap.get("ir_u8"),
                "ir2": cap.get("ir2_u8"),
            }
            panels = vision_streams.bundle_preview_jpegs(bundle, cv2)
        except Exception:
            panels = {}

    with _RS_PANEL_LOCK:
        _RS_PANEL_CACHE[role] = {"ts": time.time() if panels else 0.0, "panels": panels}

    capture_summary = {
        k: v
        for k, v in cap.items()
        if k not in {"color_bgr", "depth_u16", "ir_u8", "ir2_u8"}
    }
    capture_summary.update(
        {
            "has_color": cap.get("color_bgr") is not None,
            "has_depth": cap.get("depth_u16") is not None,
            "has_ir1": cap.get("ir_u8") is not None,
            "has_ir2": cap.get("ir2_u8") is not None,
        }
    )

    code = 200 if cap.get("ok") else 503
    return jsonify(
        {
            "ok": bool(cap.get("ok")),
            "camera": role,
            "logical": logical,
            "capture": capture_summary,
        }
    ), code


@bp.route("/api/pick/camera/select", methods=["GET", "POST"])
def api_pick_camera_select() -> Response:
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        return jsonify({"ok": True, **_upsert_camera_pref(body)})
    return jsonify({"ok": True, **_camera_pref()})

@bp.route("/api/orbbec/last.jpg")
def orbbec_last_jpeg() -> Response:
    path = orbbec_capture.latest_snapshot_path()
    if path is None:
        return jsonify({"ok": False, "reason": "no_snapshot"}), 404
    return send_file(path, mimetype="image/jpeg", max_age=0)

@bp.route("/api/orbbec/lock")
def orbbec_lock_get() -> Response:
    return jsonify(orbbec_capture.orbbec_lock_status())


@bp.route("/api/orbbec/steal", methods=["POST"])
def orbbec_steal() -> Response:
    try:
        out = orbbec_capture.steal_orbbec()
    except Exception as exc:
        return jsonify({"ok": False, "reason": "orbbec_steal_error", "error": str(exc)}), 500
    code = 200 if out.get("ok") else 409
    return jsonify(out), code


@bp.route("/api/orbbec/release", methods=["POST"])
def orbbec_release() -> Response:
    return jsonify(orbbec_capture.release_orbbec_steal())


@bp.route("/api/orbbec/probe")
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

@bp.route("/api/pick/preset", methods=["GET"])
def pick_preset_get() -> Response:
    return jsonify(pick_preset.preset_info())

@bp.route("/api/pick/preset", methods=["POST"])
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

@bp.route("/api/pick/preset/from_pose", methods=["POST"])
def pick_preset_from_pose() -> Response:
    """Salva offset = posa attuale − SCANSIONE (dopo jog in teach)."""
    body = request.get_json(silent=True) or {}
    servo, err = _servo_deg_from_body(body)
    if servo is None:
        return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
    out = pick_preset.offsets_from_current_vs_scan(servo)
    code = 200 if out.get("ok") else 404
    return jsonify(out), code

@bp.route("/api/pick/teach/samples", methods=["GET"])
def pick_teach_samples_list() -> Response:
    from go2_dashboard.d1_jog import pick_teach_model

    return jsonify(pick_teach_model.list_teach_samples())

@bp.route("/api/pick/teach/finish", methods=["POST"])
def pick_teach_finish() -> Response:
    """Salva un esempio teach (dopo release) e attiva coppia sulla posa insegnata."""
    from go2_dashboard.d1_jog import pick_teach_model

    body = request.get_json(silent=True) or {}
    vis = body.get("vision_at_scan")
    if body.get("servo_deg"):
        servo, err = _servo_deg_from_body(body)
        if servo is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        out = pick_teach_model.finish_teach_sample_after_release(
            vision_at_scan=vis if isinstance(vis, dict) else None,
            taught_servo_deg=servo,
        )
    else:
        out = pick_teach_model.finish_teach_sample_after_release(
            vision_at_scan=vis if isinstance(vis, dict) else None,
        )
    code = 200 if out.get("ok") else 502
    return jsonify(out), code

@bp.route("/api/pick/teach/samples/<sample_id>", methods=["DELETE"])
def pick_teach_sample_delete(sample_id: str) -> Response:
    from go2_dashboard.d1_jog import pick_teach_model

    out = pick_teach_model.delete_teach_sample(sample_id)
    code = 200 if out.get("ok") else 404
    return jsonify(out), code

@bp.route("/api/pick/teach/build_model", methods=["POST"])
def pick_teach_build_model() -> Response:
    from go2_dashboard.d1_jog import pick_teach_model

    out = pick_teach_model.build_teach_model()
    code = 200 if out.get("ok") else 400
    return jsonify(out), code

@bp.route("/api/pick/calibrate/zero/finish", methods=["POST"])
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

@bp.route("/api/pick/preset/nudge", methods=["POST"])
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


@bp.route("/api/pick/tuning/cycles", methods=["GET", "POST"])
def pick_tuning_cycles() -> Response:
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        item = _append_tuning_cycle(body)
        return jsonify({"ok": True, "saved": item, "count": len(_read_tuning_cycles(limit=0))})
    try:
        limit = int(str(request.args.get("limit", "80")))
    except ValueError:
        limit = 80
    rows = _read_tuning_cycles(limit=max(1, min(limit, 300)))
    return jsonify({"ok": True, "count": len(rows), "items": rows})

@bp.route("/api/pick/vision/crop", methods=["GET"])
def pick_vision_crop_get() -> Response:
    from go2_dashboard.d1_jog import pick_vision_crop

    return jsonify(pick_vision_crop.crop_settings_info())

@bp.route("/api/pick/vision/crop", methods=["POST"])
def pick_vision_crop_set() -> Response:
    from go2_dashboard.d1_jog import pick_vision_crop

    body = request.get_json(silent=True) or {}
    fr = body.get("crop_fracs") if isinstance(body.get("crop_fracs"), dict) else body
    if not isinstance(fr, dict):
        return jsonify({"ok": False, "reason": "crop_fracs_required"}), 400
    saved = pick_vision_crop.save_crop_fracs(fr)
    return jsonify({"ok": True, **pick_vision_crop.crop_settings_info(), "saved": saved})

@bp.route("/api/pick/vision/crop/preview", methods=["POST"])
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


@bp.route("/api/pick/detector/config", methods=["GET", "POST"])
def pick_detector_config() -> Response:
    if request.method == "GET":
        out = _detector_config_payload()
        try:
            import sys

            scripts_dir = str(PROJECT_ROOT / "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            from box_object_detector import detector_status

            out["detector_status"] = detector_status()
        except Exception as exc:
            out["detector_status"] = {"ok": False, "reason": repr(exc)}
        return jsonify(out)

    body = request.get_json(silent=True) or {}
    mode = _apply_detector_preset(body.get("mode") or _detector_model_mode_from_env())
    raw_params = body.get("params") if isinstance(body.get("params"), dict) else body
    applied: dict[str, Any] = {}
    for name in _DETECTOR_PARAM_SPECS:
        if name not in raw_params:
            continue
        parsed = _sanitize_detector_value(name, raw_params.get(name))
        if parsed is None:
            continue
        os.environ[name] = str(parsed)
        applied[name] = parsed
    saved = {
        "mode": mode,
        "params": _effective_detector_params(),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        _save_detector_config_file(saved)
    except OSError as exc:
        return jsonify({"ok": False, "reason": "detector_config_save_failed", "error": repr(exc)}), 500
    out = _detector_config_payload()
    out["applied"] = applied
    return jsonify(out)


@bp.route("/api/pick/detect/metric", methods=["POST"])
def pick_detect_metric() -> Response:
    body = request.get_json(silent=True) or {}
    if _normalize_camera_role(body.get("detect_camera") or "wrist") != "wrist":
        return jsonify({"ok": False, "reason": "metric_detect_requires_wrist_camera"}), 400

    fb = service.read_servo_deg(fast=True)
    if not fb.get("ok") or not isinstance(fb.get("servo_deg"), list):
        return jsonify({"ok": False, "reason": "no_servo_feedback", "feedback": fb}), 503
    scan_sd = service.clamp_servo_deg([float(x) for x in fb.get("servo_deg", [])[:7]])

    try:
        from go2_dashboard.orbbec_wrist_grasp import plan_wrist_grasp_metric

        metric = plan_wrist_grasp_metric(
            scan_sd,
            instruction=str(body.get("instruction") or "").strip() or None,
            fast_capture=True,
        )
    except Exception as exc:
        return jsonify({"ok": False, "reason": "metric_detect_exception", "error": str(exc)}), 502

    det = metric.get("detection") if isinstance(metric.get("detection"), dict) else {}
    if not det and isinstance(metric.get("object_detection"), dict):
        det = dict(metric.get("object_detection") or {})
    out = {
        "ok": bool(metric.get("ok")),
        "reason": metric.get("reason"),
        "detection_ok": bool(det.get("ok")),
        "detection": det,
        "depth_m": metric.get("depth_m"),
        "depth_source": metric.get("depth_source"),
        "metric_viz_url": metric.get("metric_viz_url"),
        "validation_ui": metric.get("validation_ui"),
        "hint_it": metric.get("hint_it"),
        "metric_plan": metric,
    }
    code = 200 if out["ok"] or out["detection_ok"] else 502
    return jsonify(out), code

@bp.route("/api/pick/snapshot", methods=["POST"])
def pick_snapshot() -> Response:
    body = request.get_json(silent=True) or {}
    pref = _camera_pref()
    detect_role = _resolve_detect_role(body, pref)
    logical = _logical_for_role(detect_role)
    try:
        if logical == 0:
            out = _capture_and_detect_isolated()
            cap = out.get("capture") if isinstance(out, dict) else None
            cap_reason = cap.get("reason") if isinstance(cap, dict) else None
            if not out.get("ok") and (
                out.get("reason") in {"capture_failed", "pick_snapshot_no_result", "pick_snapshot_subprocess_failed"}
                or cap_reason == "realsense_wrist_capture_failed"
            ):
                # Fallback anti-contenzione: retry nello stesso processo del server.
                out = pick_vision.capture_and_detect()
                out["snapshot_mode"] = "inprocess_retry"
            out["logical_camera"] = 0
            out["detect_camera"] = "wrist"
        else:
            out = _detect_on_logical_camera(logical)
            out["detect_camera"] = "front"
        _apply_pick_detection_to_preset(out)
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "reason": "pick_snapshot_error",
                "error": str(exc),
                "hint": "Errore server durante foto/recognition — controlla dashboard_run.log; non usare reset Orbbec con fuser sulla 5052.",
            }
        ), 500
    code = 200 if out.get("ok") else 502
    return jsonify(out), code

@bp.route("/api/pick/detect", methods=["POST"])
def pick_detect() -> Response:
    body = request.get_json(silent=True) or {}
    pref = _camera_pref()
    detect_role = _resolve_detect_role(body, pref)
    logical = _logical_for_role(detect_role)
    if logical == 0 and body.get("capture_if_missing", True):
        out = pick_vision.capture_and_detect()
        out["logical_camera"] = 0
        out["detect_camera"] = "wrist"
    elif logical == 0:
        out = pick_vision.detect_on_latest_snapshot(capture_if_missing=False)
        out["logical_camera"] = 0
        out["detect_camera"] = "wrist"
    else:
        out = _detect_on_logical_camera(logical)
        out["detect_camera"] = "front"
    _apply_pick_detection_to_preset(out)
    code = 200 if out.get("ok") else 502
    return jsonify(out), code


@bp.route("/api/pick/detection/last")
def pick_detection_last() -> Response:
    return jsonify(_last_detection_payload())

@bp.route("/api/pick/diagnostic")
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

@bp.route("/api/pick/scene.jpg")
def pick_scene_jpeg() -> Response:
    return _pick_scene_jpeg()

@bp.route("/api/pick/detect.jpg")
def pick_detect_jpeg() -> Response:
    return _pick_scene_jpeg()

@bp.route("/api/pick/grasp/goto", methods=["POST"])
def pick_grasp_goto() -> Response:
    body = request.get_json(silent=True) or {}
    pref = _camera_pref()
    detect_role = _resolve_detect_role(body, pref)
    grasp_role = _normalize_camera_role(body.get("grasp_camera") or pref.get("grasp_camera"))
    scan_variant = str(body.get("scan_variant") or body.get("variant") or "").strip().lower() or None
    found = program_store.find_scan_waypoint(variant=scan_variant)
    if found is None:
        left_aliases = {"j90_left", "left", "sinistra", "-90", "j90_opposite", "opposite", "altro_lato", "other_side"}
        reason = "scan_j90_left_waypoint_not_found" if scan_variant in left_aliases else "scan_waypoint_not_found"
        return jsonify({"ok": False, "reason": reason, "scan_variant": scan_variant}), 404
    _program_id, wp = found
    raw = wp.get("servo_deg")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "reason": "invalid_scan_waypoint"}), 400
    scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
    preset = pick_preset.load_preset()
    off = pick_preset.effective_joint_offsets(
        last_detection=preset.get("last_detection"),
    )
    if off is None:
        return jsonify(
            {
                "ok": False,
                "reason": "grasp_preset_missing",
                "hint": "Calibrazione zero, offset programma o foto normale prima di Presa oggetto",
            }
        ), 404
    target = pick_preset.grasp_servo_approach_from_scan(
        scan_sd,
        offsets=off,
        last_detection=preset.get("last_detection"),
    )
    if target is None:
        return jsonify({"ok": False, "reason": "grasp_target_invalid"}), 400

    require_det = os.environ.get("GO2_PICK_REQUIRE_VALID_WRIST_DETECTION", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if require_det:
        det_ok, det_reason, det = _recent_valid_wrist_detection()
        if not det_ok:
            return jsonify(
                {
                    "ok": False,
                    "reason": "grasp_requires_valid_wrist_detection",
                    "detail": det_reason,
                    "hint_it": "Fai prima 'Foto + detect' col polso e verifica detection_ok=true, poi riprova avvicinamento.",
                    "last_detection": det,
                }
            ), 409

    _reset_motion_lock_for_pick()
    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        return jsonify(couple), 502
    open_j6 = pick_preset.gripper_open_j6_deg(scan_sd)
    out = program_runner.move_to_servo_deg_smooth(target, pin_joints={6: open_j6})
    out["preset"] = "grasp_approach"
    out["scan_variant"] = scan_variant or "default"
    out["waypoint_name"] = wp.get("name")
    out["gripper_open_deg"] = open_j6
    out["gripper_closed_deg"] = pick_preset.gripper_close_j6_deg(scan_sd)
    out["coupling"] = couple
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
    out["grasp_camera"] = grasp_role
    out["grasp_logical"] = _logical_for_role(grasp_role)
    if grasp_role != "wrist":
        out["warning_it"] = (
            "Camera frontale selezionata per presa: questo step usa comunque l'approccio da preset. "
            "Usa Presa automatica per validazione camera frontale."
        )
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
    code = 200 if out.get("ok") else 502
    return jsonify(out), code

@bp.route("/api/pick/gripper/open", methods=["POST"])
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

@bp.route("/api/pick/gripper/close", methods=["POST"])
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


@bp.route("/api/pick/grasp/close_and_lift", methods=["POST"])
def pick_grasp_close_and_lift() -> Response:
    body = request.get_json(silent=True) or {}
    lift_enabled = body.get("lift", True) is not False

    found = program_store.find_scan_waypoint()
    if found is None:
        return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
    _program_id, wp = found
    raw = wp.get("servo_deg")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "reason": "invalid_scan_waypoint"}), 400
    scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
    close_j6 = pick_preset.gripper_close_j6_deg(scan_sd)

    close_resp, close_code = _pick_gripper_move(close_j6, action="gripper_close")
    close_out = close_resp.get_json(silent=True) or {"ok": False, "reason": "close_decode_failed"}
    if close_code >= 400 or not close_out.get("ok"):
        return (
            jsonify({"ok": False, "reason": str(close_out.get("reason") or "gripper_close_failed"), "close": close_out}),
            502,
        )

    lift_out: dict[str, Any] | None = None
    if lift_enabled:
        try:
            from go2_dashboard.d1_arm_publish_lite import goto_home_servo_deg

            lift_out = dict(goto_home_servo_deg(delay_ms=None))
        except Exception as exc:
            lift_out = {"ok": False, "reason": "lift_exception", "error": str(exc)}
        if not lift_out.get("ok"):
            return jsonify({"ok": False, "reason": str(lift_out.get("reason") or "lift_failed"), "close": close_out, "lift": lift_out}), 502

    return jsonify({"ok": True, "close": close_out, "lift": lift_out, "lift_enabled": lift_enabled})


@bp.route("/api/pick/left/sequence", methods=["POST"])
def pick_left_sequence() -> Response:
    """Sequenza presa sinistra con gate visivo e fallback teach manuale.

    Passi:
    1. vai a scansione sinistra;
    2. snapshot + riconoscimento;
    3. richiede modello teach attivo;
    4. avvicinamento presa;
    5. chiusura pinza opzionale (default: sì).
    """
    body = request.get_json(silent=True) or {}
    scan_variant = _scan_variant_from_body(body, default="j90_left")
    close_enabled = body.get("close", True) is not False
    require_teach_model = body.get("require_teach_model", True) is not False
    require_detector_model = body.get("require_detector_model", False) is True
    require_custom_detector_model = body.get("require_custom_detector_model", False) is True
    require_metric_3d = body.get("require_metric_3d", True) is not False
    instruction = str(body.get("instruction") or "prendi il pezzo").strip()
    steps: list[dict[str, Any]] = []

    def fail(phase: str, reason: str, *, code: int = 409, **extra: Any) -> tuple[Response, int]:
        payload = {
            "ok": False,
            "phase": phase,
            "reason": reason,
            "scan_variant": scan_variant,
            "steps": steps,
            "manual_teach_required": True,
            "manual_teach_next": {
                "button": "Teaching manuale sinistra",
                "api": "/api/pick/teach/finish",
                "hint_it": (
                    "Rifai il teach: scansione sinistra, foto del pezzo, smolla giunti, "
                    "porta il braccio sulla presa reale, salva teach e ricrea il modello."
                ),
            },
        }
        payload.update(extra)
        return jsonify(payload), code

    found = program_store.find_scan_waypoint(variant=scan_variant)
    if found is None:
        return fail("scan", "scan_j90_left_waypoint_not_found", code=404)
    _program_id, wp = found
    raw = wp.get("servo_deg")
    if not isinstance(raw, list) or len(raw) < 6:
        return fail("scan", "invalid_scan_waypoint", code=400, waypoint_name=wp.get("name"))
    scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])

    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        steps.append({"step": "couple", "ok": False, "result": couple})
        return fail("scan", "couple_failed", code=502, coupling=couple)
    scan_out = program_runner.move_to_servo_deg_smooth(scan_sd)
    steps.append({
        "step": "scan_left",
        "ok": bool(scan_out.get("ok")),
        "waypoint_name": wp.get("name"),
        "result": scan_out,
    })
    if not scan_out.get("ok"):
        return fail("scan", str(scan_out.get("reason") or "scan_move_failed"), code=502)

    try:
        snap = pick_vision.capture_and_detect()
        _apply_pick_detection_to_preset(snap)
    except Exception as exc:
        steps.append({"step": "snapshot", "ok": False, "error": str(exc)})
        return fail("recognition", "pick_snapshot_error", code=500, error=str(exc))
    det_status = _detector_status_from_snapshot(snap)
    steps.append({
        "step": "recognition",
        "ok": bool(snap.get("ok") and snap.get("detection_ok")),
        "detection_ok": bool(snap.get("detection_ok")),
        "detector_status": det_status,
        "preview_url": snap.get("preview_url"),
        "image_url": snap.get("image_url"),
        "reason": (snap.get("detection") or {}).get("reason") if isinstance(snap.get("detection"), dict) else snap.get("reason"),
    })
    if not snap.get("ok") or not snap.get("detection_ok"):
        return fail(
            "recognition",
            str(snap.get("hint_it") or (snap.get("detection") or {}).get("reason") or "object_not_detected"),
            snapshot=snap,
        )
    if require_detector_model and not det_status.get("model_exists"):
        return fail(
            "recognition",
            "detector_model_missing",
            detector_status=det_status,
            hint_it="Il rilevamento ha usato fallback/colore: configura GO2_YOLO_MODEL o disattiva require_detector_model.",
        )
    if require_custom_detector_model and _detector_model_is_generic(det_status):
        return fail(
            "recognition",
            "custom_detector_model_missing",
            detector_status=det_status,
            hint_it=(
                "Il modello attivo e' COCO generico (es. yolov8n/yolo11n), non il modello custom del pezzo. "
                "Configura GO2_YOLO_MODEL sul tuo modello addestrato prima della presa automatica."
            ),
        )

    metric_plan: dict[str, Any] | None = None
    try:
        from go2_dashboard.orbbec_wrist_grasp import plan_wrist_grasp_metric

        metric_plan = plan_wrist_grasp_metric(scan_sd, instruction=instruction, fast_capture=True)
    except Exception as exc:
        metric_plan = {"ok": False, "reason": "metric_3d_exception", "error": str(exc)}
    metric_ok = bool(
        metric_plan.get("ok")
        and ((metric_plan.get("validation_ui") or {}).get("ok") is not False)
        and ((metric_plan.get("grasp_assessment") or {}).get("execution_allowed") is not False)
    )
    steps.append({
        "step": "metric_3d",
        "ok": metric_ok,
        "reason": metric_plan.get("reason"),
        "backend": metric_plan.get("backend"),
        "target": metric_plan.get("target"),
        "validation_ui": metric_plan.get("validation_ui"),
        "metric_viz_url": metric_plan.get("metric_viz_url"),
    })
    if require_metric_3d and not metric_ok:
        return fail(
            "metric_3d",
            str(metric_plan.get("hint_it") or metric_plan.get("reason") or "metric_3d_not_validated"),
            metric_plan=metric_plan,
        )

    model_active, model_info = _teach_model_active()
    steps.append({
        "step": "teach_model",
        "ok": bool(model_active),
        "teach_samples_count": model_info.get("count"),
        "has_active_model": model_info.get("has_active_model"),
    })
    if require_teach_model and not model_active:
        return fail(
            "teach_model",
            "teach_model_missing",
            teach_model=model_info,
            hint_it="Serve almeno un teach manuale salvato e «Crea modello teach» prima della presa sinistra automatica.",
        )

    preset = pick_preset.load_preset()
    off = pick_preset.effective_joint_offsets(last_detection=preset.get("last_detection"))
    if off is None:
        return fail("approach", "grasp_preset_missing", code=404)
    target = pick_preset.grasp_servo_approach_from_scan(
        scan_sd,
        offsets=off,
        last_detection=preset.get("last_detection"),
    )
    if target is None:
        return fail("approach", "grasp_target_invalid", code=400)
    open_j6 = pick_preset.gripper_open_j6_deg(scan_sd)
    approach = program_runner.move_to_servo_deg_smooth(target, pin_joints={6: open_j6})
    approach["preset"] = "grasp_approach"
    approach["scan_variant"] = scan_variant
    approach["waypoint_name"] = wp.get("name")
    approach["target_servo_deg"] = target
    approach["joint_offset_deg_effective"] = off
    steps.append({"step": "approach", "ok": bool(approach.get("ok")), "result": approach})
    if not approach.get("ok"):
        return fail("approach", str(approach.get("reason") or "approach_failed"), code=502)

    close_out: dict[str, Any] | None = None
    if close_enabled:
        close_j6 = pick_preset.gripper_close_j6_deg(scan_sd)
        close_resp, close_code = _pick_gripper_move(close_j6, action="gripper_close")
        close_out = close_resp.get_json(silent=True) or {"ok": False, "reason": "close_decode_failed"}
        steps.append({"step": "close", "ok": bool(close_out.get("ok")), "result": close_out})
        if close_code >= 400 or not close_out.get("ok"):
            return fail("close", str(close_out.get("reason") or "gripper_close_failed"), code=502, close=close_out)

    return jsonify({
        "ok": True,
        "scan_variant": scan_variant,
        "steps": steps,
        "snapshot": snap,
        "metric_plan": metric_plan,
        "approach": approach,
        "close": close_out,
        "manual_teach_required": False,
    })


@bp.route("/api/pick/full_sequence", methods=["POST"])
def pick_full_sequence() -> Response:
    """Presa completa dal lato scelto.

    Flusso operativo:
    1. muovi a 90 gradi sinistra/destra dal waypoint scelto;
    2. dalla posizione raggiunta fai piano metrico RGB+depth dal polso;
    3. cache del piano IK;
    4. esegui presa a fasi;
    5. se fallisce, la UI puo' avviare il teaching posizione.
    """
    body = request.get_json(silent=True) or {}
    pref = _camera_pref()
    detect_role = _resolve_detect_role(body, pref)
    grasp_role = _normalize_camera_role(body.get("grasp_camera") or pref.get("grasp_camera"))
    scan_variant = _scan_variant_from_body(body, default="j90_left")
    instruction = str(body.get("instruction") or "prendi il pezzo").strip()
    close_enabled = body.get("close", True) is not False
    execute_enabled = body.get("execute", True) is not False
    steps: list[dict[str, Any]] = []

    def fail(phase: str, reason: str, *, code: int = 409, **extra: Any) -> tuple[Response, int]:
        payload = {
            "ok": False,
            "phase": phase,
            "reason": reason,
            "scan_variant": scan_variant,
            "detect_camera": detect_role,
            "detect_logical": _logical_for_role(detect_role),
            "grasp_camera": grasp_role,
            "grasp_logical": _logical_for_role(grasp_role),
            "steps": steps,
            "operator_can_teach": True,
            "manual_teach_next": {
                "button": "Teaching posizione presa",
                "api": "/api/pick/teach/finish",
                "hint_it": "Premi Teaching posizione presa: 5s, release completo, posa manuale, salvataggio dopo 20s.",
            },
        }
        payload.update(extra)
        return jsonify(payload), code

    found = program_store.find_scan_waypoint(variant=scan_variant)
    if found is None:
        reason = "scan_j90_left_waypoint_not_found" if scan_variant == "j90_left" else "scan_j90_waypoint_not_found"
        return fail("move_90", reason, code=404)
    _program_id, wp = found
    raw = wp.get("servo_deg")
    if not isinstance(raw, list) or len(raw) < 6:
        return fail("move_90", "invalid_scan_waypoint", code=400, waypoint_name=wp.get("name"))
    scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])

    _reset_motion_lock_for_pick()
    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        steps.append({"step": "couple", "ok": False, "result": couple})
        return fail("move_90", "couple_failed", code=502, coupling=couple)

    move_90 = program_runner.move_to_servo_deg_smooth(scan_sd)
    steps.append(
        {
            "step": "move_90",
            "ok": bool(move_90.get("ok")),
            "scan_variant": scan_variant,
            "waypoint_name": wp.get("name"),
            "target_servo_deg": scan_sd,
            "result": move_90,
        }
    )
    if not move_90.get("ok"):
        return fail("move_90", str(move_90.get("reason") or "move_90_failed"), code=502)

    metric_plan: dict[str, Any]
    if detect_role == "front":
        front_det = _detect_on_logical_camera(6)
        metric_plan = {
            "ok": bool(front_det.get("ok") and front_det.get("detection_ok")),
            "backend": "front_camera_2d",
            "reason": None if front_det.get("detection_ok") else (front_det.get("reason") or "front_detection_failed"),
            "detection": front_det.get("detection"),
            "validation_ui": {
                "ok": bool(front_det.get("detection_ok")),
                "banner_it": (
                    "Validazione frontale 2D OK"
                    if front_det.get("detection_ok")
                    else "Validazione frontale 2D fallita"
                ),
            },
            "grasp_assessment": {
                "execution_allowed": bool(front_det.get("detection_ok")),
                "label_it": "front_2d" if front_det.get("detection_ok") else "front_2d_failed",
            },
            "logical_camera_device": 6,
        }
    else:
        try:
            from go2_dashboard.operator_plan_cache import set_last_grasp_plan
            from go2_dashboard.orbbec_wrist_grasp import plan_wrist_grasp_metric

            metric_plan = plan_wrist_grasp_metric(scan_sd, instruction=instruction, fast_capture=False)
            if metric_plan.get("ok"):
                set_last_grasp_plan(metric_plan)
        except Exception as exc:
            metric_plan = {"ok": False, "reason": "metric_3d_exception", "error": str(exc)}

    metric_ok = bool(
        metric_plan.get("ok")
        and ((metric_plan.get("validation_ui") or {}).get("ok") is not False)
        and ((metric_plan.get("grasp_assessment") or {}).get("execution_allowed") is not False)
    )
    steps.append(
        {
            "step": "rgbd_scan_ik",
            "ok": metric_ok,
            "detect_camera": detect_role,
            "grasp_camera": grasp_role,
            "reason": metric_plan.get("reason"),
            "backend": metric_plan.get("backend"),
            "target": metric_plan.get("target"),
            "validation_ui": metric_plan.get("validation_ui"),
            "grasp_assessment": metric_plan.get("grasp_assessment"),
            "metric_viz_url": metric_plan.get("metric_viz_url"),
        }
    )
    if not metric_ok:
        return fail(
            "rgbd_scan_ik",
            str(metric_plan.get("hint_it") or metric_plan.get("reason") or "metric_3d_not_validated"),
            metric_plan=metric_plan,
        )

    execute_out: dict[str, Any] | None = None
    if execute_enabled:
        try:
            from go2_dashboard.grasp_phased_execute import execute_phased_from_cached_plan

            execute_out = execute_phased_from_cached_plan(
                confirm="EXECUTE_PHASED_GRASP",
                allow_heuristic_override=False,
            )
        except Exception as exc:
            execute_out = {"ok": False, "reason": "execute_phased_exception", "error": str(exc)}
        steps.append({"step": "execute_phased", "ok": bool(execute_out.get("ok")), "result": execute_out})
        if not execute_out.get("ok"):
            return fail("execute_phased", str(execute_out.get("reason") or "execute_failed"), code=502, execute=execute_out)

    close_out: dict[str, Any] | None = None
    if close_enabled and not execute_enabled:
        close_j6 = pick_preset.gripper_close_j6_deg(scan_sd)
        close_resp, close_code = _pick_gripper_move(close_j6, action="gripper_close")
        close_out = close_resp.get_json(silent=True) or {"ok": False, "reason": "close_decode_failed"}
        steps.append({"step": "close", "ok": bool(close_out.get("ok")), "result": close_out})
        if close_code >= 400 or not close_out.get("ok"):
            return fail("close", str(close_out.get("reason") or "gripper_close_failed"), code=502, close=close_out)

    return jsonify(
        {
            "ok": True,
            "scan_variant": scan_variant,
            "detect_camera": detect_role,
            "detect_logical": _logical_for_role(detect_role),
            "grasp_camera": grasp_role,
            "grasp_logical": _logical_for_role(grasp_role),
            "steps": steps,
            "metric_plan": metric_plan,
            "execute": execute_out,
            "close": close_out,
            "operator_can_teach": True,
        }
    )

@bp.route("/api/presets/scan", methods=["GET"])
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

@bp.route("/api/presets/scan/goto", methods=["POST"])
def preset_scan_goto() -> Response:
    from go2_dashboard.debug_agent_log import dbg_agent_log

    body = request.get_json(silent=True) or {}
    variant = str(body.get("variant") or "base").strip().lower()
    left_aliases = {"j90_left", "left", "sinistra", "-90", "j90_opposite", "opposite", "altro_lato", "other_side"}
    right_aliases = {"j90", "j90_right", "right", "destra", "90", "+90"}
    if variant not in {"base", *left_aliases, *right_aliases}:
        variant = "base"
    if variant in left_aliases:
        scan_variant = "j90_left"
    elif variant in right_aliases:
        scan_variant = "j90"
    else:
        scan_variant = "base"
    dbg_agent_log(
        "d1_pick_teach.py:preset_scan_goto",
        "scan_goto_request",
        {"variant": variant, "scan_variant": scan_variant},
        hypothesis_id="H-SCAN",
    )
    found = program_store.find_scan_waypoint(variant=scan_variant)
    if found is None:
        reason = (
            "scan_j90_left_waypoint_not_found"
            if scan_variant == "j90_left"
            else "scan_j90_waypoint_not_found" if scan_variant == "j90" else "scan_waypoint_not_found"
        )
        dbg_agent_log(
            "d1_pick_teach.py:preset_scan_goto",
            "scan_goto_waypoint_missing",
            {"reason": reason, "scan_variant": scan_variant},
            hypothesis_id="H-SCAN",
        )
        return jsonify({
            "ok": False,
            "reason": reason,
            "hint_it": (
                "Waypoint «Punto SCANSIONE 90» non trovato nel programma D1 — "
                "salvalo in tab Pick teach / programma braccio, poi riprova."
            ),
        }), 404
    _program_id, wp = found
    raw = wp.get("servo_deg")
    if not isinstance(raw, list) or len(raw) < 6:
        return jsonify({"ok": False, "reason": "invalid_waypoint"}), 400
    servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
    _reset_motion_lock_for_pick()
    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        return jsonify(couple), 502
    from go2_dashboard import d1_arm_motion

    keep_lock = bool(d1_arm_motion.is_live_session_active())
    out = program_runner.move_to_servo_deg_smooth(servo, keep_lock=keep_lock)
    if not out.get("ok"):
        reason = str(out.get("reason") or "")
        if reason.startswith("plane_busy"):
            out["hint_it"] = (
                "Sessione braccio occupata — chiudi «Braccio D1 · giunti» (Fine controllo) "
                "o «Annulla flusso», poi riprova START +90°."
            )
    out["preset"] = "scan"
    out["coupling"] = couple
    out["scan_variant"] = scan_variant
    out["waypoint_name"] = wp.get("name")
    out["target_servo_deg"] = servo
    code = 200 if out.get("ok") else 502
    dbg_agent_log(
        "d1_pick_teach.py:preset_scan_goto",
        "scan_goto_result",
        {
            "http_code": code,
            "ok": out.get("ok"),
            "reason": out.get("reason"),
            "waypoint_name": wp.get("name"),
            "max_error_deg": (out.get("wait_at_target") or {}).get("max_error_deg"),
            "plane_busy": str(out.get("reason") or "").startswith("plane_busy"),
        },
        hypothesis_id="H-SCAN",
    )
    return jsonify(out), code

