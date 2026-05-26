"""Calibrazione AprilTag tag 5 + dual probe / shared dual — **solo** dipendenze ``box_grasp_planner`` + cache camere.

Usato da ``serve_dashboard_lite`` senza importare ``diagnostics_dashboard``."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any

from go2_dashboard.cameras import CAMERA_CACHE, CAMERA_DEVICES, _v4l_index_for_logical_camera
from go2_dashboard.operator_stack import go2_local
from go2_dashboard.paths import PROJECT_ROOT

TAG5_CALIB_PATH = PROJECT_ROOT / "data" / "tag5_calibration_arm_base.json"

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _scripts_on_path() -> None:
    scripts = str(PROJECT_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def _nominal_tag5_arm_base_from_env() -> list[float] | None:
    raw = os.environ.get("GO2_TAG5_NOMINAL_ARM_BASE_M", "").strip()
    if not raw:
        return [0.19, 0.0, 0.08]
    try:
        parts = [float(x.strip()) for x in raw.split(",")]
        if len(parts) >= 3:
            return [parts[0], parts[1], parts[2]]
    except ValueError:
        pass
    return [0.19, 0.0, 0.08]


def frame_from_camera(device: int) -> Any | None:
    if not go2_local() or cv2 is None:
        return None
    d = int(device)
    if d not in CAMERA_DEVICES:
        return None
    CAMERA_CACHE.start(d)
    wait = float(os.environ.get("GO2_TAG5_FRAME_WAIT_S", "2.5"))
    jpg = CAMERA_CACHE.get_jpeg(d, wait_s=wait)
    if not jpg:
        return None
    import numpy as np

    arr = np.frombuffer(jpg, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _v4l_for_log(logical: int) -> int:
    try:
        return int(_v4l_index_for_logical_camera(int(logical)))
    except Exception:
        return int(logical)


def tag5_preview_jpeg_and_meta(*, logical_device: int) -> tuple[bytes | None, dict[str, Any]]:
    """Un frame JPEG con overlay AprilTag (id 5 evidenziato) per anteprima calibrazione."""
    dev = int(logical_device)
    if dev not in CAMERA_DEVICES:
        return None, {"ok": False, "error": "invalid_logical_device", "logical_device": dev}
    frame = frame_from_camera(dev)
    if frame is None:
        return None, {"ok": False, "error": "no_frame", "logical_device": dev, "v4l_index": _v4l_for_log(dev)}
    if cv2 is None:
        return None, {"ok": False, "error": "no_cv2", "logical_device": dev}
    _scripts_on_path()
    from box_grasp_planner import REFERENCE_TAG_ID_LIDAR_FRAME, draw_grasp_overlay, plan_from_frame

    pl = plan_from_frame(frame, object_detection=None, logical_camera_device=dev)
    overlay = draw_grasp_overlay(frame, pl)
    tag5_seen = False
    for t in (pl.get("tags") or {}).get("tags") or []:
        try:
            if int(t.get("id", -1)) == REFERENCE_TAG_ID_LIDAR_FRAME:
                tag5_seen = True
                break
        except (TypeError, ValueError):
            continue
    ok_enc, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok_enc or buf is None:
        return None, {
            "ok": False,
            "error": "jpeg_encode_failed",
            "logical_device": dev,
            "tag5_seen": tag5_seen,
            "v4l_index": _v4l_for_log(dev),
        }
    return buf.tobytes(), {
        "ok": True,
        "tag5_seen": tag5_seen,
        "logical_device": dev,
        "v4l_index": _v4l_for_log(dev),
    }


def tag5_dual_camera_probe_payload() -> dict[str, Any]:
    import numpy as np

    _scripts_on_path()
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
        "Due camere → due stime base normali. Usa offset tag5 da una camera (polso) o calib 0+6 sullo stesso tag."
    )
    return out


def calibration_flow_payload() -> dict[str, Any]:
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
                "AprilTag 25h9 ID 5 sul robot → file tag5_calibration_arm_base.json. "
                "Calib polso = slot logico 0 (Orbbec RGB); su Jetson può essere /dev/video6 — "
                "GO2_VIDEO_INDEX_0=6 se serve. Slot 6 = RealSense."
            ),
            "arm_link00_nominal_it": "arm_link00 = base braccio D1; +X verso la testa del cane.",
            "dual_probe_optional": (
                "GET tag5_calibration?dual_probe=1 = confronto slot 0 (polso Orbbec) vs 6 (RealSense RGB)."
            ),
            "box_tags_0_3": "Tag 0–3 sulla scatola = target presa.",
            "cross_camera_geometry_it": "POST tag_calibration_shared_dual se stesso tag visto da 0 e 6 insieme.",
        },
        "dynamic": {
            "nominal_tag5_arm_base_m": nominal,
            "nominal_configured": nominal is not None,
            "tag5_offset_file_present": path.is_file(),
            "saved_calibration_summary": saved_summary,
            "tag5_calibration_enable_env": os.environ.get("GO2_TAG5_CALIBRATION_ENABLE", "1"),
        },
        "steps_it": [
            {"n": 1, "title": "Nominale tag 5 (arm_link00, metri)", "body": "Default 0.19, 0, 0.08 o env GO2_TAG5_NOMINAL_ARM_BASE_M."},
            {
                "n": 2,
                "title": "Orbbec sul polso: slot logico 0 sul tag 5",
                "body": (
                    "Scegli camera 0 nel form; GO2_VIDEO_INDEX_0=6 se RGB è /dev/video6. "
                    "«Salva» = rileva ID 5 e scrive il JSON."
                ),
            },
            {"n": 3, "title": "Tab 3D", "body": "«Avvia aggiornamento» dopo il salvataggio."},
        ],
        "env_hints": {
            "GO2_TAG5_NOMINAL_ARM_BASE_M": "es. 0.19,0.0,0.08",
            "GO2_TAG5_CALIBRATION_ENABLE": "1 (default)",
            "GO2_VIDEO_INDEX_0": "indice V4L per slot polso Orbbec (es. 6)",
            "dual_probe": "GET /api/arm/tag5_calibration?dual_probe=1",
            "shared_dual_tag": "POST /api/arm/tag_calibration_shared_dual",
        },
    }


def handle_tag5_calibration_get(*, dual_probe: bool) -> tuple[dict[str, Any], int]:
    path = TAG5_CALIB_PATH
    if dual_probe:
        try:
            return tag5_dual_camera_probe_payload(), 200
        except Exception as exc:
            return {"ok": False, "error": repr(exc)}, 500
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
    return out, 200


def handle_tag5_calibration_delete() -> tuple[dict[str, Any], int]:
    path = TAG5_CALIB_PATH
    try:
        if path.is_file():
            path.unlink()
        return {"ok": True, "cleared": True}, 200
    except OSError as exc:
        return {"ok": False, "error": repr(exc)}, 500


def handle_tag5_calibration_post(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    _scripts_on_path()
    from box_grasp_planner import REFERENCE_TAG_ID_LIDAR_FRAME, make_tag5_calibration_record, plan_from_frame

    path = TAG5_CALIB_PATH
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
        return (
            {
                "ok": False,
                "error": (
                    "Imposta il centro tag 5 nel frame base braccio (m): env "
                    "GO2_TAG5_NOMINAL_ARM_BASE_M=x,y,z oppure POST JSON nominal_tag5_arm_base_m."
                ),
            },
            400,
        )

    prefer_dev = body.get("camera_device")
    dev_order: list[int] = []
    if prefer_dev is not None:
        try:
            dev_order.append(int(prefer_dev))
        except (TypeError, ValueError):
            pass
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
        return (
            {
                "ok": False,
                "error": (
                    "AprilTag 5 non rilevato sulla camera selezionata (di norma la polso / device 0). "
                    "Verifica inquadratura e GET /api/cameras/status."
                ),
            },
            400,
        )

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
    return (
        {
            "ok": True,
            "saved": rec,
            "camera_logical_device_used": dev,
            "next_steps_it": [
                "Aggiorna tab 3D: landmark tag5 e tag scatola usano offset in scene_3d.",
                "Poi usa Hermes o il tab Moto (giunti) per muovere il braccio.",
            ],
        },
        200,
    )


def handle_tag_calibration_shared_dual_get() -> tuple[dict[str, Any], int]:
    path = TAG5_CALIB_PATH
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
        "POST con tag_id, nominal_arm_base_m [x,y,z] in arm_link00, opz. tag_edge_length_m se l'ID non è tra i tag noti."
    )
    return out, 200


def handle_tag_calibration_shared_dual_post(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    _scripts_on_path()
    from box_grasp_planner import TRACKED_TAG_IDS, _camera_tvec_to_base_heuristic_xyz, tvec_camera_m_for_tag_id

    path = TAG5_CALIB_PATH
    try:
        tag_id = int(body.get("tag_id", -1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "tag_id intero richiesto"}, 400
    if tag_id < 0 or tag_id > 491:
        return {"ok": False, "error": "tag_id fuori range"}, 400

    n_body = body.get("nominal_arm_base_m")
    if not isinstance(n_body, list) or len(n_body) < 3:
        return {"ok": False, "error": "nominal_arm_base_m: lista di 3 numeri (m) in arm_link00"}, 400
    try:
        nominal = [float(n_body[0]), float(n_body[1]), float(n_body[2])]
    except (TypeError, ValueError):
        return {"ok": False, "error": "nominal_arm_base_m non numerici"}, 400

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
        return {"ok": False, "error": "Servono due logical_devices tra 0 e 6 (default [0,6])"}, 400

    tag_edge_m = body.get("tag_edge_length_m")
    edge_f: float | None = None
    if tag_edge_m is not None:
        try:
            edge_f = float(tag_edge_m)
        except (TypeError, ValueError):
            return {"ok": False, "error": "tag_edge_length_m non numerico"}, 400
        if edge_f <= 0 or edge_f > 0.5:
            return {"ok": False, "error": "tag_edge_length_m implausibile (0–0.5 m)"}, 400
    elif int(tag_id) not in TRACKED_TAG_IDS:
        return (
            {
                "ok": False,
                "error": f"tag_id {tag_id} non in {sorted(TRACKED_TAG_IDS)}: imposta tag_edge_length_m (lato tag in m)",
            },
            400,
        )

    overs: dict[int, float] | None = {int(tag_id): float(edge_f)} if edge_f is not None else None

    per_dev: dict[str, Any] = {}
    offsets: dict[int, list[float]] = {}
    for dev in devices:
        frame = frame_from_camera(dev)
        if frame is None:
            return {"ok": False, "error": f"Nessun frame da logical camera {dev}"}, 400
        tvec = tvec_camera_m_for_tag_id(frame, tag_id, tag_edge_length_overrides=overs)
        if tvec is None:
            return (
                {
                    "ok": False,
                    "error": f"AprilTag {tag_id} non rilevato su /dev/video{_v4l_for_log(dev)} (logical {dev})",
                },
                400,
            )
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
            "Distanza tag→suolo con profondità RealSense non è calcolata in questa API."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return (
        {
            "ok": True,
            "saved_path": str(path),
            "offset_by_logical_camera_device_m": obm,
            "dual_shared_tag_calib": merged["dual_shared_tag_calib"],
            "next_steps_it": [
                "Ricarica scene_3d e worker grasp: le pose useranno gli offset per-device.",
                "Mantieni la calibrazione tag 5 (XT-16) con POST /api/arm/tag5_calibration dal polso.",
            ],
        },
        200,
    )
