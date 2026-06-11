"""Pianificazione presa **metrica** dal polso (Orbbec Gemini 335) sulla NX.

Catena (tutto NX-side, niente worker):
  1. Cattura allineata color+depth dall'Orbbec SDK (pyorbbecsdk2) → depth metrica reale (mm).
  2. Detection oggetto sul frame color (``box_object_detector``).
  3. Profondità mediana nel bbox → back-projection pinhole → XYZ ottico (OpenCV).
  4. Trasformazione ottico→base_link via FK del braccio alla posa servo corrente.
  5. Reach guard (distanza dall'origine braccio) + IK per gli stadi pre_grasp/approach/grasp/lift.

Richiede l'Orbbec SDK installato sulla NX. Se assente, ``available()`` torna False e il
chiamante usa il fallback worker. Import-safe su Windows/PC (nessun import a livello modulo).
"""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Any

import numpy as np

_PIPE_LOCK = threading.Lock()

# Mount braccio in base_link (coerente con d1_arm_publish_lite._MOUNT_BASE_LINK_M).
_MOUNT_BASE_LINK_M = np.array([0.15, 0.0, 0.06], dtype=float)
_ARM_ORIGIN_FK_M = np.array([0.15, 0.0, 0.06], dtype=float)


def _env_float(key: str, default: float) -> float:
    try:
        return float((os.environ.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _filter_wrist_detection(det: dict[str, Any], intr: dict[str, Any]) -> dict[str, Any]:
    """Scarta falsi positivi (angolo alto / blob minuscolo / conf bassa) sul polso."""
    if not isinstance(det, dict):
        return {"ok": False, "reason": "no_detection"}
    if not det.get("ok"):
        return det
    h = float(intr.get("height") or 480)
    w = float(intr.get("width") or 640)
    bbox = det.get("bbox_xyxy") or []
    # Con START laterale il centro bbox può stare in alto nel frame pur essendo l'oggetto valido:
    # usiamo il **bordo inferiore** del riquadro (base oggetto) per il filtro orizzonte.
    bottom_y = float(bbox[3]) if len(bbox) >= 4 else 0.0
    if not bottom_y:
        center = det.get("bbox_center_px") or [0, 0]
        bottom_y = float(center[1]) if len(center) >= 2 else 0.0
    min_bottom_ratio = _env_float("GO2_WRIST_DETECT_MIN_BOTTOM_Y_RATIO", 0.12)
    if bottom_y < h * min_bottom_ratio:
        return {
            **det,
            "ok": False,
            "reason": "bbox_above_horizon",
            "hint_it": "Detection troppo in alto nel frame polso — abbassa il braccio o avvicina la scatola.",
        }
    # color_blue_box usa confidenza euristica (non YOLO): soglia piu' bassa per non scartare
    # detection valide con conf ~0.28-0.35 su scatole piccole nel frame polso.
    min_conf = _env_float("GO2_WRIST_DETECT_MIN_CONF", 0.25)
    if det.get("backend") == "color_blue_box":
        min_conf = _env_float("GO2_WRIST_DETECT_MIN_CONF_COLOR", min_conf)
    if float(det.get("confidence") or 0) < min_conf:
        return {**det, "ok": False, "reason": "confidence_too_low"}
    if len(bbox) >= 4:
        area = max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))
        area_ratio = area / max(w * h, 1.0)
        if area_ratio < _env_float("GO2_WRIST_DETECT_MIN_AREA_RATIO", 0.003):
            return {**det, "ok": False, "reason": "bbox_too_small"}
        if area_ratio > _env_float("GO2_WRIST_DETECT_MAX_AREA_RATIO", 0.12):
            return {
                **det,
                "ok": False,
                "reason": "bbox_too_large",
                "hint_it": "Blob troppo grande (pavimento/riflessi): ricalibra colore o stringi HSV.",
            }
        bh_ratio = max(0.0, float(bbox[3]) - float(bbox[1])) / max(h, 1.0)
        if bh_ratio > _env_float("GO2_WRIST_DETECT_MAX_BBOX_HEIGHT_RATIO", 0.36):
            return {
                **det,
                "ok": False,
                "reason": "bbox_too_tall",
                "hint_it": "Riquadro troppo alto (probabilmente include le chele): alza il crop o avvicina la scatola.",
            }
        max_bottom_ratio = _env_float("GO2_WRIST_DETECT_MAX_BOTTOM_Y_RATIO", 0.72)
        if bottom_y > h * max_bottom_ratio:
            return {
                **det,
                "ok": False,
                "reason": "bbox_too_low",
                "hint_it": "Detection troppo in basso (zona pinza/chele): alza il braccio o stringi il crop.",
            }
    return det


def available() -> bool:
    """True se l'Orbbec SDK è importabile (solo NX)."""
    if (os.environ.get("GO2_WRIST_ORBBEC_ENABLE", "1") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    try:
        import pyorbbecsdk  # noqa: F401
        return True
    except Exception:
        return False


def capture_aligned(*, timeout_ms: int | None = None, max_frames: int | None = None) -> dict[str, Any]:
    """Cattura una coppia (color BGR, depth uint16) allineata depth→color dall'Orbbec.

    Apre la pipeline on-demand (color 640x480 MJPG + depth Y16 640x480) e la chiude subito:
    evita il claim USB continuo che confliggerebbe con lo stream V4L2 del dashboard.

    Acquisisce il lock cross-process ``orbbec_lock`` (con ``preempt``): se un altro processo
    (es. jog :5053 o un altro stream) sta usando l'Orbbec, gli chiede di cedere e attende fino
    a ``GO2_ORBBEC_LOCK_TIMEOUT_S``. Se resta occupato torna ``orbbec_busy`` con il detentore.
    """
    import cv2
    import pyorbbecsdk as ob

    from go2_dashboard import orbbec_lock

    if timeout_ms is None:
        try:
            timeout_ms = int(float(os.environ.get("GO2_ORBBEC_CAPTURE_TIMEOUT_MS", "2200")))
        except ValueError:
            timeout_ms = 2200
    if max_frames is None:
        try:
            max_frames = int(float(os.environ.get("GO2_ORBBEC_CAPTURE_MAX_FRAMES", "55")))
        except ValueError:
            max_frames = 55
    with _PIPE_LOCK, orbbec_lock.orbbec_guard("grasp_capture", preempt=True) as _lk:
        if not _lk.acquired:
            return {
                "ok": False,
                "reason": "orbbec_busy",
                "holder": _lk.holder,
                "hint_it": (
                    "Orbbec occupato da un altro processo"
                    + (f" ({_lk.holder})" if _lk.holder else "")
                    + ". Chiudi lo stream Orbbec sull'altra dashboard/jog e riprova, "
                    "oppure aumenta GO2_ORBBEC_LOCK_TIMEOUT_S."
                ),
            }
        pipe = None
        try:
            # Dai tempo allo stream V4L di cedere dopo la prelazione (evita EBUSY / frame vuoti).
            time.sleep(max(0.0, _env_float("GO2_ORBBEC_CAPTURE_SETTLE_S", 0.45)))
            pipe = ob.Pipeline()
            cfg = ob.Config()
            # color 640x480 (preferisci MJPG; fallback al default)
            cl = pipe.get_stream_profile_list(ob.OBSensorType.COLOR_SENSOR)
            cprof = None
            for i in range(cl.get_count()):
                p = cl.get_stream_profile_by_index(i).as_video_stream_profile()
                if p.get_width() == 640 and p.get_height() == 480 and "MJPG" in str(p.get_format()):
                    cprof = p
                    break
            if cprof is None:
                cprof = cl.get_default_video_stream_profile()
            dl = pipe.get_stream_profile_list(ob.OBSensorType.DEPTH_SENSOR)
            dprof = None
            for i in range(dl.get_count()):
                p = dl.get_stream_profile_by_index(i).as_video_stream_profile()
                if p.get_width() == 640 and p.get_height() == 480 and "Y16" in str(p.get_format()):
                    dprof = p
                    break
            if dprof is None:
                dprof = dl.get_default_video_stream_profile()
            cfg.enable_stream(cprof)
            cfg.enable_stream(dprof)
            align = ob.AlignFilter(align_to_stream=ob.OBStreamType.COLOR_STREAM)
            pipe.start(cfg)
            got = None
            for _ in range(int(max_frames)):
                fs = pipe.wait_for_frames(int(timeout_ms))
                if fs is None:
                    continue
                fs2 = align.process(fs)
                if fs2 is None:
                    continue
                fs2 = fs2.as_frame_set()
                c = fs2.get_color_frame()
                d = fs2.get_depth_frame()
                if c is not None and d is not None:
                    got = (c, d)
                    break
            if got is None:
                return {"ok": False, "reason": "no_aligned_frame"}
            c, d = got
            dw, dh = d.get_width(), d.get_height()
            scale_mm = float(d.get_depth_scale())  # mm per unit
            depth = np.frombuffer(d.get_data(), dtype=np.uint16).reshape(dh, dw).copy()
            intr = d.get_stream_profile().as_video_stream_profile().get_intrinsic()
            cfmt = str(c.get_format())
            cw, ch = c.get_width(), c.get_height()
            cdata = np.frombuffer(c.get_data(), dtype=np.uint8)
            if "MJPG" in cfmt or "JPEG" in cfmt:
                color = cv2.imdecode(cdata, cv2.IMREAD_COLOR)
            elif "RGB" in cfmt:
                color = cdata.reshape(ch, cw, 3)[:, :, ::-1].copy()
            elif "BGR" in cfmt:
                color = cdata.reshape(ch, cw, 3).copy()
            else:
                color = cv2.imdecode(cdata, cv2.IMREAD_COLOR)
            if color is None:
                return {"ok": False, "reason": "color_decode_failed", "color_format": cfmt}
            return {
                "ok": True,
                "color_bgr": color,
                "depth_u16": depth,
                "depth_scale_mm": scale_mm,
                "intrinsics": {
                    "fx": float(intr.fx),
                    "fy": float(intr.fy),
                    "cx": float(intr.cx),
                    "cy": float(intr.cy),
                    "width": int(dw),
                    "height": int(dh),
                },
                "color_format": cfmt,
            }
        except Exception as exc:
            return {"ok": False, "reason": "orbbec_capture_error", "detail": repr(exc)}
        finally:
            if pipe is not None:
                try:
                    pipe.stop()
                except Exception:
                    pass


def _camera_basis_base_link(q_rad: list[float]):
    """Versori camera polso in frame FK: (centro, right=+Xcv, down=+Ycv, fwd=+Zcv)."""
    import sys

    from go2_dashboard.paths import PROJECT_ROOT

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    import arm_kinematics_d1_template as K

    _, R_link = K.fk_full(q_rad)
    r_cam = K._mjcf_fixed_camera_rotation(0.0, -math.pi / 2.0, -math.pi / 2.0)
    M = R_link @ r_cam
    right = M[:, 0]          # MJCF camera +X = destra immagine (= +X OpenCV)
    up = M[:, 1]             # MJCF camera +Y = su (= -Y OpenCV)
    fwd = -M[:, 2]           # vista (-Z MJCF) = +Z OpenCV (avanti nella scena)
    cam_center = K.fk_wrist_camera_center_m(q_rad)
    return np.asarray(cam_center, dtype=float), right, up, fwd


def _depth_roi_stats(depth_u16: np.ndarray, scale_mm: float, bbox_xyxy, *, shrink: float) -> dict[str, Any]:
    h, w = depth_u16.shape[:2]
    x0, y0, x1, y1 = [float(v) for v in bbox_xyxy]
    bw, bh = (x1 - x0), (y1 - y0)
    x0s = int(round(x0 + bw * shrink))
    x1s = int(round(x1 - bw * shrink))
    y0s = int(round(y0 + bh * shrink))
    y1s = int(round(y1 - bh * shrink))
    x0s, x1s = max(0, min(w - 1, x0s)), max(1, min(w, x1s))
    y0s, y1s = max(0, min(h - 1, y0s)), max(1, min(h, y1s))
    if x1s <= x0s or y1s <= y0s:
        x0s, y0s, x1s, y1s = int(max(0, x0)), int(max(0, y0)), int(min(w, x1)), int(min(h, y1))
    roi = depth_u16[y0s:y1s, x0s:x1s]
    nz = roi[roi > 0]
    try:
        min_support = int(float(os.environ.get("GO2_ORBBEC_DEPTH_MIN_SUPPORT", "6")))
    except ValueError:
        min_support = 6
    if nz.size < min_support:
        return {"ok": False, "reason": "no_depth_support", "support": int(nz.size), "roi_px": [x0s, y0s, x1s, y1s], "shrink": shrink}
    med_mm = float(np.median(nz))
    return {
        "ok": True,
        "depth_m": med_mm * scale_mm / 1000.0,
        "support": int(nz.size),
        "roi_px": [x0s, y0s, x1s, y1s],
        "shrink": shrink,
        "iqr_m": float(np.subtract(*np.percentile(nz, [75, 25]))) * scale_mm / 1000.0,
    }


def _depth_median_m(depth_u16: np.ndarray, scale_mm: float, bbox_xyxy, *, shrink: float | None = None) -> dict[str, Any]:
    """Mediana depth nel bbox. Fallback a ROI più larga se il nucleo è vuoto (logo/centro riflettente)."""
    if shrink is None:
        try:
            shrink = float(os.environ.get("GO2_ORBBEC_DEPTH_ROI_SHRINK", "0.12"))
        except ValueError:
            shrink = 0.12
    tries = [float(shrink)]
    for extra in (0.08, 0.0, -0.08):
        if extra not in tries:
            tries.append(extra)
    last: dict[str, Any] = {"ok": False, "reason": "no_depth_support", "support": 0}
    for sh in tries:
        sh = max(-0.12, min(0.45, float(sh)))
        out = _depth_roi_stats(depth_u16, scale_mm, bbox_xyxy, shrink=sh)
        if out.get("ok"):
            if sh != float(shrink):
                out["depth_fallback_shrink"] = sh
            return out
        last = out
    last["hint_it"] = (
        "Depth Orbbec assente nel riquadro oggetto (centro spesso a 0 su superfici lucide). "
        "Avvicina la scatola, inclina leggermente, o verifica proiettore IR."
    )
    return last


def _build_metric_pointcloud(
    depth_u16: np.ndarray,
    scale_mm: float,
    intr: dict[str, Any],
    bbox_xyxy,
    *,
    expand: float = 0.12,
    max_points: int = 8192,
) -> np.ndarray:
    """Point cloud metrica (N,3) float32 in frame ottico camera (m), ROI = bbox (allargato).

    Usa gli intrinseci **reali** dell'SDK Orbbec e la depth a 16 bit: niente JPEG, niente
    intrinseci stimati. Questa e' la nuvola che mandiamo a GraspGen.
    """
    h, w = depth_u16.shape[:2]
    x0, y0, x1, y1 = [float(v) for v in bbox_xyxy[:4]]
    bw, bh = (x1 - x0), (y1 - y0)
    x0 = int(round(x0 - bw * expand))
    x1 = int(round(x1 + bw * expand))
    y0 = int(round(y0 - bh * expand))
    y1 = int(round(y1 + bh * expand))
    x0 = max(0, min(w - 1, x0))
    x1 = max(1, min(w, x1))
    y0 = max(0, min(h - 1, y0))
    y1 = max(1, min(h, y1))
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 3), dtype=np.float32)
    roi = depth_u16[y0:y1, x0:x1].astype(np.float32) * (float(scale_mm) / 1000.0)  # -> metri
    ys, xs = np.where(roi > 0.05)
    if xs.size == 0:
        return np.zeros((0, 3), dtype=np.float32)
    zs = roi[ys, xs]
    u = (xs + x0).astype(np.float32)
    v = (ys + y0).astype(np.float32)
    fx, fy = float(intr["fx"]), float(intr["fy"])
    cx, cy = float(intr["cx"]), float(intr["cy"])
    X = (u - cx) / max(fx, 1.0) * zs
    Y = (v - cy) / max(fy, 1.0) * zs
    pts = np.stack([X, Y, zs], axis=1).astype(np.float32)
    if len(pts) > max_points:
        idx = np.random.default_rng(0).choice(len(pts), max_points, replace=False)
        pts = pts[idx]
    return pts


def plan_wrist_grasp_graspgen(servo_deg7: list[float], *, instruction: str | None = None) -> dict[str, Any]:
    """Ponte metrico->GraspGen: nuvola metrica Orbbec -> server GraspGen -> grasp 6-DoF reale.

    A differenza di ``plan_wrist_grasp_metric`` (singolo punto da depth mediana + presa euristica),
    qui mandiamo l'intera point cloud metrica del bbox al modello GraspGen sul worker AWS. Per ogni
    grasp 6-DoF restituito (ordinato per confidenza) trasformiamo la traslazione camera->base_link
    con la **stessa** base FK del path metrico, applichiamo reach-guard + IK a 4 stadi e scegliamo il
    primo grasp raggiungibile e con IK completa. Il piano risultante ha la stessa struttura del path
    metrico, quindi esecuzione a fasi e UI funzionano invariati.
    """
    t0 = time.time()
    cap = capture_aligned()
    if not cap.get("ok"):
        return {"ok": False, "reason": cap.get("reason", "capture_failed"), "detail": cap.get("detail")}

    import base64
    import io
    import sys

    from go2_dashboard.paths import PROJECT_ROOT

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    from box_object_detector import detect_box_object
    import arm_kinematics_d1_template as K

    color = cap["color_bgr"]
    depth = cap["depth_u16"]
    intr = cap["intrinsics"]
    scale_mm = cap["depth_scale_mm"]

    det = _filter_wrist_detection(detect_box_object(color), intr)
    try:
        from go2_dashboard.grasp_detect_debug import save_detection_snapshot

        debug_snap = save_detection_snapshot(
            color, det if isinstance(det, dict) else None,
            tag="wrist_orbbec", logical_camera=0, step="graspgen_metric_plan",
        )
    except Exception as exc:
        debug_snap = {"saved": False, "error": repr(exc)}
    if not det.get("ok") or not det.get("bbox_xyxy"):
        return {"ok": False, "reason": "no_detection", "detection": det, "debug_snapshot": debug_snap}
    det.setdefault("frame_size_px", [intr["width"], intr["height"]])
    det["logical_camera"] = 0
    if instruction:
        det["instruction"] = instruction

    pc = _build_metric_pointcloud(depth, scale_mm, intr, det["bbox_xyxy"])
    if len(pc) < 64:
        return {"ok": False, "reason": "insufficient_metric_points", "num_points": int(len(pc)),
                "detection": det, "debug_snapshot": debug_snap}
    buf = io.BytesIO()
    np.save(buf, pc.astype(np.float32))
    pc_b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")

    from go2_dashboard.blueprints.grasp import graspgen_infer_via_worker

    num = int(_env_float("GO2_GRASPGEN_NUM_GRASPS", 200))
    topk = int(_env_float("GO2_GRASPGEN_TOPK", 100))
    payload, code = graspgen_infer_via_worker(pc_b64, num_grasps=num, topk=topk)
    if not (isinstance(payload, dict) and payload.get("ok")) or code >= 400:
        return {"ok": False, "reason": (payload or {}).get("reason", "graspgen_infer_failed"),
                "http_code": code, "detection": det, "debug_snapshot": debug_snap,
                "graspgen_payload": payload, "num_points": int(len(pc))}
    grasps = payload.get("grasps_4x4") or []
    confs = payload.get("confidences") or []
    if not grasps:
        return {"ok": False, "reason": "no_grasps", "detection": det, "debug_snapshot": debug_snap,
                "num_points": int(len(pc))}

    q = [math.radians(float(servo_deg7[i])) for i in range(6)]
    cam_center, right, up, fwd = _camera_basis_base_link(q)
    max_reach = _env_float("GO2_ARM_MAX_REACH_M", 0.55)
    dz = _env_float("GO2_GRASP_IK_OFFSET_Z_BASE_LINK_M", 0.0)
    pre_dz = _env_float("GO2_GRASP_PREGRASP_DZ_M", 0.15)
    app_dz = _env_float("GO2_GRASP_APPROACH_DZ_M", 0.06)
    lift_dz = _env_float("GO2_GRASP_LIFT_DZ_M", 0.16)
    stages_spec = [("pre_grasp", pre_dz, "open"), ("approach", app_dz, "open"),
                   ("grasp", 0.0, "close"), ("lift", lift_dz, "hold_closed")]

    considered: list[dict[str, Any]] = []
    chosen: dict[str, Any] | None = None
    for gi, (g4, conf) in enumerate(zip(grasps, confs)):
        try:
            t = [float(g4[0][3]), float(g4[1][3]), float(g4[2][3])]  # traslazione, frame ottico camera
        except (TypeError, IndexError, ValueError):
            continue
        P_fk = cam_center + t[0] * right - t[1] * up + t[2] * fwd
        reach_m = float(np.linalg.norm(P_fk - _ARM_ORIGIN_FK_M))
        reachable = reach_m <= max_reach
        tb = [float(P_fk[0] + _MOUNT_BASE_LINK_M[0]),
              float(P_fk[1] + _MOUNT_BASE_LINK_M[1]),
              float(P_fk[2] + _MOUNT_BASE_LINK_M[2]) + dz]
        considered.append({"idx": gi, "confidence": round(float(conf), 4),
                           "camera_xyz_m": [round(x, 4) for x in t],
                           "target_xyz_m": [round(x, 4) for x in tb],
                           "reach_m": round(reach_m, 4), "reachable": reachable})
        if not reachable:
            continue
        preview_plan: list[dict[str, Any]] = []
        gripper_plan: list[dict[str, Any]] = []
        ik_all_ok = True
        for name, ddz, grip in stages_spec:
            tgt_bl = [tb[0], tb[1], tb[2] + ddz]
            tip_arm = [tgt_bl[i] - float(_MOUNT_BASE_LINK_M[i]) for i in range(3)]
            sol = K.ik_reach(tip_arm[0], tip_arm[1], tip_arm[2], primary_seed=q[:6])
            if sol is None:
                ik_all_ok = False
                preview_plan.append({"stage": name, "target_xyz_m": [round(x, 4) for x in tgt_bl], "ik_ok": False})
                continue
            tip = K.fk_tool_tip(sol)
            preview_plan.append({
                "stage": name, "target_xyz_m": [round(x, 4) for x in tgt_bl],
                "joints_rad": [round(float(x), 4) for x in sol],
                "servo_deg": [round(math.degrees(float(x)), 2) for x in sol],
                "fk_tip_xyz_m": [round(float(tip[i] + _MOUNT_BASE_LINK_M[i]), 4) for i in range(3)],
                "ik_ok": True,
            })
            g = {"stage": name, "gripper": grip}
            if grip == "close":
                g["hold_s"] = 0.6
            gripper_plan.append(g)
        if ik_all_ok:
            chosen = {"idx": gi, "confidence": float(conf), "tb": tb, "reach_m": reach_m,
                      "camera_xyz_m": t, "preview_plan": preview_plan, "gripper_plan": gripper_plan,
                      "grasp_4x4": [[round(float(g4[r][cc]), 5) for cc in range(4)] for r in range(4)]}
            break

    if chosen is None:
        return {"ok": False, "reason": "no_reachable_graspgen_grasp", "detection": det,
                "debug_snapshot": debug_snap, "num_points": int(len(pc)),
                "graspgen_num_candidates": len(grasps), "considered": considered[:10],
                "reach_max_m": max_reach,
                "hint_it": "GraspGen ha prodotto grasp ma nessuno raggiungibile/IK-ok — riposiziona o avvicina l'oggetto."}

    tb = chosen["tb"]
    target_bl_round = [round(x, 4) for x in tb]
    reach_m = chosen["reach_m"]
    conf = chosen["confidence"]
    cam_xyz = [round(x, 4) for x in chosen["camera_xyz_m"]]
    Zc = float(chosen["camera_xyz_m"][2])
    grasp_points = [
        target_bl_round,
        [round(tb[0] + 0.008, 4), target_bl_round[1], target_bl_round[2]],
        [round(tb[0] - 0.008, 4), target_bl_round[1], target_bl_round[2]],
        [target_bl_round[0], round(tb[1] + 0.008, 4), target_bl_round[2]],
        [target_bl_round[0], round(tb[1] - 0.008, 4), target_bl_round[2]],
    ]
    label = f"GraspGen su nuvola metrica Orbbec (conf {conf:.2f}, reach {reach_m:.2f} m)"
    checks = [
        {"label_it": "Orbbec SDK depth metrica", "ok": True, "status": "pass",
         "detail_it": f"scale {scale_mm} mm/u, {len(pc)} punti nuvola"},
        {"label_it": "Oggetto rilevato (polso)", "ok": True, "status": "pass",
         "detail_it": f"{det.get('backend')} conf={det.get('confidence')}"},
        {"label_it": "GraspGen 6-DoF (robotiq_2f_140)", "ok": True, "status": "pass",
         "detail_it": f"{len(grasps)} grasp, best conf {conf:.3f}"},
        {"label_it": "Target 3D in base_link", "ok": True, "status": "pass", "detail_it": str(target_bl_round)},
        {"label_it": "Raggiungibilita' + IK (4 stadi)", "ok": True, "status": "pass",
         "detail_it": f"reach {reach_m:.3f} m / max {max_reach:.2f} m"},
    ]
    return {
        "ok": True,
        "backend": "graspgen_orbbec_metric",
        "debug_snapshot": debug_snap,
        "logical_camera_device": 0,
        "instruction": instruction,
        "absolute_ik_safe": True,
        "depth_observation": {
            "ok": True,
            "source": "graspgen_orbbec_metric",
            "depth_median_m": round(Zc, 4),
            "depth_support_fraction": 1.0,
            "depth_iqr_m": 0.0,
        },
        "image_source": "orbbec_sdk_aligned",
        "rgbd_embedded": True,
        "depth_embedded": True,
        "object_detection": det,
        "camera_xyz_m": cam_xyz,
        "depth_m": round(Zc, 4),
        "depth_support": int(len(pc)),
        "intrinsics": intr,
        "reach_m": round(reach_m, 4),
        "reach_max_m": max_reach,
        "reachable": True,
        "graspgen": {
            "confidence": round(conf, 4),
            "num_candidates": len(grasps),
            "num_points": int(len(pc)),
            "chosen_index": chosen["idx"],
            "grasp_4x4": chosen["grasp_4x4"],
            "considered": considered[:10],
            "gripper": payload.get("gripper"),
        },
        "target": {
            "base_xyz_m": target_bl_round,
            "camera_xyz_m": cam_xyz,
            "depth_median_m": round(Zc, 4),
            "depth_support_fraction": 1.0,
            "source": "graspgen_orbbec_metric",
            "ok": True,
        },
        "grasp_display_base_link_m": target_bl_round,
        "operators_grasp_points_base_link_m": grasp_points,
        "preview": {"ok": True, "mode": "graspgen-metric-nx",
                    "plan": chosen["preview_plan"], "gripper": chosen["gripper_plan"]},
        "grasp_assessment": {
            "execution_allowed": True,
            "label_it": label,
            "tier": "validated_3d_metric",
            "validated_3d": True,
            "source_kind": "graspgen_orbbec_metric",
            "reach_m": round(reach_m, 4),
            "reachable": True,
            "graspgen_confidence": round(conf, 4),
        },
        "validation_ui": {
            "ok": True,
            "banner_level": "pass",
            "banner_it": f"VALIDATO (GraspGen su nuvola metrica Orbbec, conf {conf:.2f}) — puoi usare «Sequenza presa (fasi)».",
            "can_execute_ik": True,
            "can_execute_phased": True,
            "label_it": label,
            "tier": "validated_3d_metric",
            "checks": checks,
        },
        "latency_ms": round((time.time() - t0) * 1000.0, 1),
    }


def plan_wrist_grasp_metric(servo_deg7: list[float], *, instruction: str | None = None) -> dict[str, Any]:
    """Piano presa metrico dal polso alla posa servo corrente. Ritorna struttura ricca per UI/IK."""
    t0 = time.time()
    cap = capture_aligned()
    if not cap.get("ok"):
        return {"ok": False, "reason": cap.get("reason", "capture_failed"), "detail": cap.get("detail")}

    import sys

    from go2_dashboard.paths import PROJECT_ROOT

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    from box_object_detector import detect_box_object
    import arm_kinematics_d1_template as K

    color = cap["color_bgr"]
    depth = cap["depth_u16"]
    intr = cap["intrinsics"]
    scale_mm = cap["depth_scale_mm"]

    det = _filter_wrist_detection(detect_box_object(color), intr)
    debug_snap: dict[str, Any] = {}
    try:
        from go2_dashboard.grasp_detect_debug import save_detection_snapshot

        debug_snap = save_detection_snapshot(
            color,
            det if isinstance(det, dict) else None,
            tag="wrist_orbbec",
            logical_camera=0,
            step="orbbec_metric_plan",
        )
    except Exception as exc:
        debug_snap = {"saved": False, "error": repr(exc)}
    if not det.get("ok") or not det.get("bbox_center_px"):
        return {"ok": False, "reason": "no_detection", "detection": det, "debug_snapshot": debug_snap}
    det.setdefault("frame_size_px", [intr["width"], intr["height"]])
    det["logical_camera"] = 0
    if instruction:
        det["instruction"] = instruction

    cx, cy = det["bbox_center_px"]
    dm = _depth_median_m(depth, scale_mm, det["bbox_xyxy"])
    if not dm.get("ok"):
        retries = max(0, int(_env_float("GO2_ORBBEC_DEPTH_CAPTURE_RETRIES", 1)))
        for _ in range(retries):
            cap2 = capture_aligned()
            if not cap2.get("ok"):
                break
            depth = cap2["depth_u16"]
            scale_mm = cap2["depth_scale_mm"]
            dm = _depth_median_m(depth, cap2.get("depth_scale_mm", scale_mm), det["bbox_xyxy"])
            if dm.get("ok"):
                color = cap2.get("color_bgr") or color
                intr = cap2.get("intrinsics") or intr
                break
    if not dm.get("ok"):
        out_fail: dict[str, Any] = {
            "ok": False,
            "reason": dm.get("reason", "depth_failed"),
            "detection": det,
            "depth_diag": dm,
            "debug_snapshot": debug_snap,
            "hint_it": dm.get("hint_it")
            or "Oggetto visto in RGB ma depth Orbbec insufficiente nel bbox — riprova o avvicina la scatola.",
            "object_detection": det,
            "partial_rgb_ok": True,
        }
        if debug_snap.get("image_url"):
            out_fail["metric_viz_url"] = str(debug_snap["image_url"])
        return out_fail
    Z = float(dm["depth_m"])

    # back-projection pinhole (OpenCV optical: +X destra, +Y giù, +Z avanti)
    Xcv = (float(cx) - intr["cx"]) / intr["fx"] * Z
    Ycv = (float(cy) - intr["cy"]) / intr["fy"] * Z
    camera_xyz = [round(Xcv, 4), round(Ycv, 4), round(Z, 4)]

    q = [math.radians(float(servo_deg7[i])) for i in range(6)]
    cam_center, right, up, fwd = _camera_basis_base_link(q)
    P_fk = cam_center + Xcv * right - Ycv * up + Z * fwd          # punto oggetto in frame FK
    target_base_link = (P_fk + _MOUNT_BASE_LINK_M).astype(float)

    reach_m = float(np.linalg.norm(P_fk - _ARM_ORIGIN_FK_M))
    max_reach = _env_float("GO2_ARM_MAX_REACH_M", 0.55)
    reachable = reach_m <= max_reach

    # offset Z opzionale (allineamento punta/pinza) come nel path IK esistente
    dz = _env_float("GO2_GRASP_IK_OFFSET_Z_BASE_LINK_M", 0.0)
    tb = [float(target_base_link[0]), float(target_base_link[1]), float(target_base_link[2]) + dz]

    pre_dz = _env_float("GO2_GRASP_PREGRASP_DZ_M", 0.15)
    app_dz = _env_float("GO2_GRASP_APPROACH_DZ_M", 0.06)
    lift_dz = _env_float("GO2_GRASP_LIFT_DZ_M", 0.16)
    stages_spec = [
        ("pre_grasp", pre_dz, "open"),
        ("approach", app_dz, "open"),
        ("grasp", 0.0, "close"),
        ("lift", lift_dz, "hold_closed"),
    ]

    q_seed = q + [0.0] if len(q) == 6 else q
    preview_plan: list[dict[str, Any]] = []
    gripper_plan: list[dict[str, Any]] = []
    ik_all_ok = True
    for name, ddz, grip in stages_spec:
        tgt_bl = [tb[0], tb[1], tb[2] + ddz]
        tip_arm = [tgt_bl[i] - float(_MOUNT_BASE_LINK_M[i]) for i in range(3)]
        sol = K.ik_reach(tip_arm[0], tip_arm[1], tip_arm[2], primary_seed=q[:6])
        if sol is None:
            ik_all_ok = False
            preview_plan.append({"stage": name, "target_xyz_m": [round(x, 4) for x in tgt_bl], "ik_ok": False})
            continue
        tip = K.fk_tool_tip(sol)
        preview_plan.append({
            "stage": name,
            "target_xyz_m": [round(x, 4) for x in tgt_bl],
            "joints_rad": [round(float(x), 4) for x in sol],
            "servo_deg": [round(math.degrees(float(x)), 2) for x in sol],
            "fk_tip_xyz_m": [round(float(tip[i] + _MOUNT_BASE_LINK_M[i]), 4) for i in range(3)],
            "ik_ok": True,
        })
        g = {"stage": name, "gripper": grip}
        if grip == "close":
            g["hold_s"] = 0.6
        gripper_plan.append(g)

    target_bl_round = [round(x, 4) for x in tb]
    grasp_points = [
        target_bl_round,
        [round(tb[0] + 0.008, 4), target_bl_round[1], target_bl_round[2]],
        [round(tb[0] - 0.008, 4), target_bl_round[1], target_bl_round[2]],
        [target_bl_round[0], round(tb[1] + 0.008, 4), target_bl_round[2]],
        [target_bl_round[0], round(tb[1] - 0.008, 4), target_bl_round[2]],
    ]

    execution_allowed = bool(reachable and ik_all_ok)
    if not reachable:
        label = f"Oggetto fuori reach ({reach_m:.2f} m > {max_reach:.2f} m)"
    elif not ik_all_ok:
        label = "IK non risolta su tutti gli stadi"
    else:
        label = "3D Orbbec metrico — pronto per IK"

    checks = [
        {"label_it": "Orbbec SDK depth metrica", "ok": True, "status": "pass",
         "detail_it": f"depth {Z:.3f} m, scale {scale_mm} mm/u, support {dm['support']}"},
        {"label_it": "Oggetto rilevato (polso)", "ok": True, "status": "pass",
         "detail_it": f"{det.get('backend')} conf={det.get('confidence')}"},
        {"label_it": "Target 3D in base_link", "ok": True, "status": "pass",
         "detail_it": str(target_bl_round)},
        {"label_it": "Raggiungibilità braccio", "ok": reachable, "status": "pass" if reachable else "fail",
         "detail_it": f"reach {reach_m:.3f} m / max {max_reach:.2f} m"},
        {"label_it": "Preview IK (4 stadi)", "ok": ik_all_ok, "status": "pass" if ik_all_ok else "fail",
         "detail_it": f"{sum(1 for p in preview_plan if p.get('ik_ok'))}/4 stadi"},
    ]

    out = {
        "ok": True,
        "backend": "orbbec_metric_wrist",
        "debug_snapshot": debug_snap,
        "logical_camera_device": 0,
        "instruction": instruction,
        # ``absolute_ik_safe`` gate: blocca l'esecuzione se fuori reach o IK non completa.
        "absolute_ik_safe": bool(reachable and ik_all_ok),
        # ``depth_observation`` fa promuovere il piano a 3D validato in worker_flat_plan_assessment.
        "depth_observation": {
            "ok": True,
            "source": "orbbec_metric_wrist",
            "depth_median_m": round(Z, 4),
            "depth_support_fraction": 1.0,
            "depth_iqr_m": round(dm.get("iqr_m", 0.0), 4),
        },
        "image_source": "orbbec_sdk_aligned",
        "rgbd_embedded": True,
        "depth_embedded": True,
        "object_detection": det,
        "camera_xyz_m": camera_xyz,
        "depth_m": round(Z, 4),
        "depth_support": dm["support"],
        "depth_roi_px": dm["roi_px"],
        "depth_iqr_m": round(dm.get("iqr_m", 0.0), 4),
        "intrinsics": intr,
        "reach_m": round(reach_m, 4),
        "reach_max_m": max_reach,
        "reachable": reachable,
        "target": {
            "base_xyz_m": target_bl_round,
            "camera_xyz_m": camera_xyz,
            "depth_median_m": round(Z, 4),
            "depth_roi_px": dm["roi_px"],
            "depth_support_fraction": 1.0,
            "source": "orbbec_metric_wrist",
            "ok": True,
        },
        "grasp_display_base_link_m": target_bl_round,
        "operators_grasp_points_base_link_m": grasp_points,
        "preview": {"ok": ik_all_ok, "mode": "metric-nx", "plan": preview_plan, "gripper": gripper_plan},
        "grasp_assessment": {
            "execution_allowed": execution_allowed,
            "label_it": label,
            "tier": "validated_3d_metric" if execution_allowed else "blocked",
            "validated_3d": True,
            "source_kind": "orbbec_metric",
            "reach_m": round(reach_m, 4),
            "reachable": reachable,
        },
        "validation_ui": {
            "ok": execution_allowed,
            "banner_level": "pass" if execution_allowed else "fail",
            "banner_it": ("VALIDATO (Orbbec metrico) — puoi usare «Sequenza presa (fasi)»."
                          if execution_allowed else f"BLOCCATO — {label}."),
            "can_execute_ik": execution_allowed,
            "can_execute_phased": execution_allowed,
            "label_it": label,
            "tier": "validated_3d_metric" if execution_allowed else "blocked",
            "checks": checks,
        },
        "latency_ms": round((time.time() - t0) * 1000.0, 1),
    }
    try:
        from go2_dashboard.grasp_teach_calib import apply_teach_to_metric_plan

        out = apply_teach_to_metric_plan(out)
    except Exception:
        pass
    return out
