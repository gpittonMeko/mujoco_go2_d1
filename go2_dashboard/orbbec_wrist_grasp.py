"""Pianificazione presa **metrica** dal polso (RealSense D456 / legacy Orbbec) sulla NX.

Catena (tutto NX-side, niente worker):
  1. Cattura allineata color+depth (pyrealsense2 polso o pyorbbecsdk legacy) → depth metrica reale (mm).
  2. Detection oggetto sul frame color (``box_object_detector``).
  3. Profondità mediana nel bbox → back-projection pinhole → XYZ ottico (OpenCV).
  4. Trasformazione ottico→base_link via FK del braccio alla posa servo corrente.
  5. Reach guard (distanza dall'origine braccio) + IK per gli stadi pre_grasp/approach/grasp/lift.

Backend polso: ``GO2_WRIST_DEPTH_BACKEND=realsense`` (default) o ``orbbec``. Import-safe su Windows/PC.
"""
from __future__ import annotations

import math
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import numpy as np

from go2_dashboard.grasp_visual_servo import _tolist

_PIPE_LOCK = threading.Lock()

# Mount braccio in base_link (coerente con d1_arm_publish_lite._MOUNT_BASE_LINK_M).
_MOUNT_BASE_LINK_M = np.array([0.15, 0.0, 0.06], dtype=float)
_ARM_ORIGIN_FK_M = np.array([0.15, 0.0, 0.06], dtype=float)


def _env_float(key: str, default: float) -> float:
    try:
        return float((os.environ.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def depth_plausible_m(z_m: float | None) -> bool:
    """Depth polso plausibile per presa (scarta spike D456 su sfondo/riflessi)."""
    if z_m is None:
        return False
    try:
        z = float(z_m)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(z):
        return False
    z_min = _env_float("GO2_WRIST_DEPTH_PLAUSIBLE_MIN_M", 0.18)
    z_max = _env_float("GO2_WRIST_DEPTH_PLAUSIBLE_MAX_M", 1.25)
    return z_min <= z <= z_max


def _filter_wrist_detection(det: dict[str, Any], intr: dict[str, Any]) -> dict[str, Any]:
    """Scarta falsi positivi (angolo alto / blob minuscolo / conf bassa) sul polso."""
    if not isinstance(det, dict):
        return {"ok": False, "reason": "no_detection"}
    if not det.get("ok"):
        return det
    h = float(intr.get("height") or 480)
    w = float(intr.get("width") or 640)
    bbox = _tolist(det.get("bbox_xyxy"), []) or []
    # Con START laterale il centro bbox può stare in alto nel frame pur essendo l'oggetto valido:
    # usiamo il **bordo inferiore** del riquadro (base oggetto) per il filtro orizzonte.
    bottom_y = float(bbox[3]) if len(bbox) >= 4 else 0.0
    if not bottom_y:
        center = _tolist(det.get("bbox_center_px"), [0, 0]) or [0, 0]
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
        is_gray_cylinder = str(det.get("label") or "").strip().lower() == "gray_cylinder"
        bh_ratio = max(0.0, float(bbox[3]) - float(bbox[1])) / max(h, 1.0)
        max_h_ratio = _env_float(
            "GO2_WRIST_DETECT_MAX_BBOX_HEIGHT_RATIO_GRAY",
            0.48,
        ) if is_gray_cylinder else _env_float("GO2_WRIST_DETECT_MAX_BBOX_HEIGHT_RATIO", 0.36)
        if bh_ratio > max_h_ratio:
            return {
                **det,
                "ok": False,
                "reason": "bbox_too_tall",
                "hint_it": "Riquadro troppo alto (probabilmente include le chele): alza il crop o avvicina la scatola.",
            }
        max_bottom_ratio = _env_float(
            "GO2_WRIST_DETECT_MAX_BOTTOM_Y_RATIO_GRAY",
            0.82,
        ) if is_gray_cylinder else _env_float("GO2_WRIST_DETECT_MAX_BOTTOM_Y_RATIO", 0.72)
        if bottom_y > h * max_bottom_ratio:
            return {
                **det,
                "ok": False,
                "reason": "bbox_too_low",
                "hint_it": "Detection troppo in basso (zona pinza/chele): alza il braccio o stringi il crop.",
            }
    return det


def _metric_grasp_min_z_for_detection(det: dict[str, Any]) -> float | None:
    label = str(det.get("label") or "").strip().lower()
    if label == "gray_cylinder":
        return _env_float("GO2_GRASP_GRAY_CYLINDER_MIN_Z_BASE_LINK_M", 0.065)
    return None


def _apply_metric_grasp_min_z(
    tb: list[float],
    det: dict[str, Any],
) -> tuple[list[float], dict[str, Any] | None]:
    min_z = _metric_grasp_min_z_for_detection(det)
    if min_z is None or len(tb) < 3 or float(tb[2]) >= min_z:
        return tb, None
    corrected = [float(tb[0]), float(tb[1]), float(min_z)]
    return corrected, {
        "axis": "z",
        "from_m": round(float(tb[2]), 4),
        "to_m": round(float(min_z), 4),
        "reason": "gray_cylinder_min_z_base_link",
    }


@contextmanager
def _metric_capture_lock() -> Iterator[Any]:
    """Serializza SDK metrico; rispetta il lease «Ruba Orbbec» già acquisito (no doppio flock)."""
    from go2_dashboard import orbbec_lock

    try:
        from go2_dashboard.d1_jog.orbbec_capture import we_hold_steal_lease

        if we_hold_steal_lease():
            yield orbbec_lock.LockState(True, None)
            return
    except Exception:
        pass
    with orbbec_lock.orbbec_guard("grasp_capture", preempt=True) as st:
        yield st


def _wrist_depth_backend() -> str:
    from go2_dashboard.cameras import wrist_depth_backend

    return wrist_depth_backend()


def _wrist_debug_tag() -> str:
    return "wrist_realsense" if _wrist_depth_backend() == "realsense" else "wrist_orbbec"


def available() -> bool:
    """True se il backend depth polso è disponibile (pyrealsense2 o pyorbbecsdk)."""
    if _wrist_depth_backend() == "realsense":
        try:
            import pyrealsense2 as rs  # noqa: F401
            return True
        except Exception:
            return False
    if (os.environ.get("GO2_WRIST_ORBBEC_ENABLE", "1") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    try:
        import pyorbbecsdk  # noqa: F401
        return True
    except Exception:
        return False


def capture_aligned(
    *,
    timeout_ms: int | None = None,
    max_frames: int | None = None,
    fast: bool = False,
    force_full: bool = False,
) -> dict[str, Any]:
    """Cattura color BGR + depth uint16 allineata depth→color (RealSense polso o Orbbec legacy)."""
    if _wrist_depth_backend() == "realsense":
        from go2_dashboard.realsense_pyrs import capture_aligned_on_demand

        return capture_aligned_on_demand(
            role="wrist",
            timeout_ms=timeout_ms,
            max_frames=max_frames,
            fast=fast,
            force_full=force_full,
        )
    return _capture_aligned_orbbec(timeout_ms=timeout_ms, max_frames=max_frames)


def wrist_camera_health() -> dict[str, Any]:
    """Diagnostica semplice camera polso: la D456 e' vista e dà depth reale?

    Restituisce un esito chiaro per il badge UI (verde/rosso) senza dipendere dalla
    presa: enumerazione device + una cattura color+depth + conteggio pixel depth.
    """
    # #region agent log
    import json as _json
    import time as _time

    def _dbg149a4f_wh(location: str, message: str, data: dict[str, Any] | None = None, *, hypothesis_id: str = "") -> None:
        try:
            payload = {
                "sessionId": "149a4f",
                "timestamp": int(_time.time() * 1000),
                "location": location,
                "message": message,
                "data": data or {},
                "hypothesisId": hypothesis_id,
            }
            with (PROJECT_ROOT / "debug-149a4f.log").open("a", encoding="utf-8") as f:
                f.write(_json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    t_health0 = _time.perf_counter()
    _dbg149a4f_wh("orbbec_wrist_grasp.py:wrist_camera_health", "health_start", hypothesis_id="H2")
    # #endregion
    backend = _wrist_depth_backend()
    out: dict[str, Any] = {"ok": False, "backend": backend}
    if not available():
        out["reason"] = "backend_unavailable"
        out["hint_it"] = (
            "SDK depth polso non disponibile (pyrealsense2/pyorbbecsdk). "
            "Esegui sulla NX, non sul PC."
        )
        return out

    if backend == "realsense":
        try:
            from go2_dashboard.realsense_pyrs import list_devices, resolve_device_serial
        except Exception as exc:
            out["reason"] = "import_failed"
            out["detail"] = repr(exc)
            return out
        serial = resolve_device_serial("wrist")
        out["serial"] = serial
        out["devices"] = list_devices()
        if not serial:
            out["reason"] = "realsense_device_not_found"
            out["hint_it"] = (
                "Camera polso D456 NON vista dal sistema. Controlla il cavo USB e/o "
                "imposta GO2_WRIST_REALSENSE_SERIAL sulla NX."
            )
            return out
        # Banda USB: su USB 2.x la D456 NON riesce a streammare la depth (depth map vuota).
        usb_type = None
        for d in out["devices"]:
            if isinstance(d, dict) and str(d.get("serial")) == str(serial):
                usb_type = d.get("usb_type")
                break
        out["usb_type"] = usb_type
        if usb_type and str(usb_type).strip().startswith("2"):
            out["ok"] = False
            out["reason"] = "usb2_low_bandwidth"
            out["hint_it"] = (
                f"Camera polso D456 collegata su USB {usb_type} (banda insufficiente): "
                "la depth resta quasi vuota. SPOSTA il cavo della camera polso su una porta "
                "USB 3.0 (di solito blu) della Jetson, poi ricontrolla."
            )
            return out

    # #region agent log
    _dbg149a4f_wh(
        "orbbec_wrist_grasp.py:wrist_camera_health",
        "capture_start",
        {"serial": out.get("serial"), "usb_type": out.get("usb_type")},
        hypothesis_id="H2,H3",
    )
    t_cap0 = _time.perf_counter()
    # #endregion
    health_retries = max(1, int(_env_float("GO2_WRIST_HEALTH_CAPTURE_RETRIES", 2)))
    cap: dict[str, Any] = {"ok": False, "reason": "capture_failed"}
    best_nz = -1
    for attempt in range(health_retries):
        cap_try = capture_aligned(fast=False, force_full=True)
        if not cap_try.get("ok"):
            cap = cap_try
            continue
        depth_try = cap_try.get("depth_u16")
        try:
            nz_try = int(np.count_nonzero(depth_try)) if depth_try is not None else 0
        except Exception:
            nz_try = 0
        if nz_try > best_nz:
            best_nz = nz_try
            cap = cap_try
        min_nz_health = int(_env_float("GO2_WRIST_HEALTH_MIN_DEPTH_NONZERO", 800))
        if nz_try >= min_nz_health:
            break
    # #region agent log
    _dbg149a4f_wh(
        "orbbec_wrist_grasp.py:wrist_camera_health",
        "capture_done",
        {
            "capture_ms": round((_time.perf_counter() - t_cap0) * 1000.0, 1),
            "cap_ok": bool(cap.get("ok")),
            "cap_reason": cap.get("reason"),
            "health_retries": health_retries,
            "best_depth_nonzero_px": best_nz,
            "force_full": True,
        },
        hypothesis_id="H2,H3,H_FAST",
    )
    # #endregion
    if not cap.get("ok"):
        out["reason"] = cap.get("reason") or "capture_failed"
        out["detail"] = cap.get("detail")
        out["hint_it"] = cap.get("hint_it") or (
            "Camera vista ma nessun frame allineato — riprova; se persiste è USB/banda."
        )
        return out

    depth = cap.get("depth_u16")
    scale_mm = float(cap.get("depth_scale_mm") or 1.0)
    try:
        nz = int(np.count_nonzero(depth)) if depth is not None else 0
        total = int(depth.size) if depth is not None else 1
    except Exception:
        nz, total = 0, 1
    nz_ratio = round(nz / max(1, total), 3)
    median_m = None
    if depth is not None and nz > 0:
        h, w = depth.shape[:2]
        cy0, cy1 = int(h * 0.35), int(h * 0.65)
        cx0, cx1 = int(w * 0.35), int(w * 0.65)
        roi = depth[cy0:cy1, cx0:cx1]
        nzr = roi[roi > 0]
        if nzr.size > 0:
            median_m = round(float(np.median(nzr)) * scale_mm / 1000.0, 3)

    min_nz = int(_env_float("GO2_WRIST_HEALTH_MIN_DEPTH_NONZERO", 800))
    depth_ok = nz >= min_nz
    out.update(
        {
            "ok": bool(depth_ok),
            "depth_nonzero_px": nz,
            "depth_nonzero_ratio": nz_ratio,
            "depth_center_median_m": median_m,
            "color_ok": cap.get("color_bgr") is not None,
        }
    )
    if depth_ok:
        rng = f" (centro ~{median_m:.2f} m)" if median_m is not None else ""
        out["hint_it"] = f"Camera polso OK — depth reale presente{rng}."
    else:
        out["reason"] = "weak_depth"
        hint_parts = [
            f"Camera vista ma poca depth ({nz} px su ~{total}, serve ≥{min_nz}).",
        ]
        if nz < 150:
            hint_parts.append(
                "Quasi nessun pixel valido: chiudi tab Scene/MJPEG, metti una scatola opaca "
                "a 45–70 cm dal polso (non sotto 40 cm), luce uniforme."
            )
        else:
            hint_parts.append(
                "Scena lucida/scura o oggetto troppo vicino (<0.4 m): allontana un po', "
                "migliora luce, evita superfici riflettenti."
            )
        out["hint_it"] = " ".join(hint_parts)
    # #region agent log
    _dbg149a4f_wh(
        "orbbec_wrist_grasp.py:wrist_camera_health",
        "health_done",
        {
            "total_ms": round((_time.perf_counter() - t_health0) * 1000.0, 1),
            "ok": bool(out.get("ok")),
            "reason": out.get("reason"),
            "depth_nonzero_px": out.get("depth_nonzero_px"),
        },
        hypothesis_id="H2,H3",
    )
    # #endregion
    return out


def wrist_depth_bbox_probe(
    *,
    color_hint: str | None = "blue",
    manual_bbox_xyxy: list[float] | None = None,
) -> dict[str, Any]:
    """Diagnostica numerica: quanti pixel depth ci sono nel bbox scatola vs frame intero."""
    import sys

    import numpy as np

    from go2_dashboard.paths import PROJECT_ROOT

    out: dict[str, Any] = {"ok": False, "backend": _wrist_depth_backend()}
    if not available():
        out["reason"] = "backend_unavailable"
        return out

    if _wrist_depth_backend() == "realsense":
        try:
            from go2_dashboard.cameras import CAMERA_CACHE

            settle = float((os.environ.get("GO2_REALSENSE_CAPTURE_SETTLE_S") or "0.45").strip() or 0.45)
            CAMERA_CACHE.request_pause(0, duration_s=settle + 0.25)
        except Exception:
            pass

    cap = capture_aligned(fast=False, force_full=True)
    if not cap.get("ok"):
        out["reason"] = cap.get("reason") or "capture_failed"
        out["detail"] = cap.get("detail")
        return out

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    from box_object_detector import detect_box_object

    depth = cap["depth_u16"]
    color = cap["color_bgr"]
    scale_mm = float(cap.get("depth_scale_mm") or 1.0)
    h, w = depth.shape[:2]

    def _region(mask: np.ndarray) -> dict[str, Any]:
        roi = depth[mask]
        nz = roi[roi > 0]
        med_m = round(float(np.median(nz)) * scale_mm / 1000.0, 4) if nz.size else None
        return {
            "pixels": int(mask.sum()),
            "depth_nonzero": int(nz.size),
            "nonzero_ratio": round(int(nz.size) / max(1, int(mask.sum())), 4),
            "median_m": med_m,
            "min_m": round(float(nz.min()) * scale_mm / 1000.0, 4) if nz.size else None,
            "max_m": round(float(nz.max()) * scale_mm / 1000.0, 4) if nz.size else None,
        }

    nz_all = int(np.count_nonzero(depth))
    out["frame"] = {
        "size_px": [w, h],
        "depth_nonzero": nz_all,
        "depth_nonzero_ratio": round(nz_all / max(1, h * w), 4),
        "scale_mm": scale_mm,
    }

    det = _filter_wrist_detection(detect_box_object(color, color_hint=color_hint), cap["intrinsics"])
    out["detection_ok"] = bool(det.get("ok"))
    out["bbox_xyxy"] = det.get("bbox_xyxy")
    out["confidence"] = det.get("confidence")

    if det.get("bbox_xyxy"):
        x0, y0, x1, y1 = [int(round(float(v))) for v in det["bbox_xyxy"][:4]]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        bbox_mask = np.zeros((h, w), dtype=bool)
        bbox_mask[y0:y1, x0:x1] = True
        out["bbox_region"] = _region(bbox_mask)
        out["depth_median_fn"] = _depth_median_m(depth, scale_mm, det["bbox_xyxy"])
        out["depth_ring_fn"] = _depth_ring_stats(depth, scale_mm, det["bbox_xyxy"])
        out["depth_halo_fn"] = _depth_halo_stats(depth, scale_mm, det["bbox_xyxy"])
        cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
        patch = depth[max(0, cy - 2) : cy + 3, max(0, cx - 2) : cx + 3]
        out["bbox_center_patch_raw"] = [int(v) for v in patch.reshape(-1).tolist()]
        out["bbox_center_patch_m"] = [
            round(int(v) * scale_mm / 1000.0, 4) for v in patch.reshape(-1).tolist() if int(v) > 0
        ]
    else:
        out["detection_reason"] = det.get("reason")

    if manual_bbox_xyxy and len(manual_bbox_xyxy) >= 4:
        mx0, my0, mx1, my1 = [int(round(float(v))) for v in manual_bbox_xyxy[:4]]
        mx0, my0 = max(0, mx0), max(0, my0)
        mx1, my1 = min(w, mx1), min(h, my1)
        if mx1 > mx0 and my1 > my0:
            manual_mask = np.zeros((h, w), dtype=bool)
            manual_mask[my0:my1, mx0:mx1] = True
            out["manual_bbox_xyxy"] = [mx0, my0, mx1, my1]
            out["manual_bbox_region"] = _region(manual_mask)

    cy0, cy1 = int(h * 0.35), int(h * 0.65)
    cx0, cx1 = int(w * 0.35), int(w * 0.65)
    center_mask = np.zeros((h, w), dtype=bool)
    center_mask[cy0:cy1, cx0:cx1] = True
    out["frame_center"] = _region(center_mask)

    # quadranti per capire dove c'è depth
    quads: dict[str, Any] = {}
    mx, my = w // 2, h // 2
    for name, mask in {
        "tl": np.s_[:my, :mx],
        "tr": np.s_[:my, mx:],
        "bl": np.s_[my:, :mx],
        "br": np.s_[my:, mx:],
    }.items():
        sub = depth[mask]
        quads[name] = {"depth_nonzero": int(np.count_nonzero(sub)), "pixels": int(sub.size)}
    out["quadrants"] = quads

    bbox_nz = int((out.get("bbox_region") or {}).get("depth_nonzero") or 0)
    out["ok"] = True
    if bbox_nz > 0:
        out["hint_it"] = (
            f"Depth reale nel bbox: {bbox_nz} px "
            f"(mediana ~{(out.get('bbox_region') or {}).get('median_m')} m)."
        )
    else:
        out["hint_it"] = (
            f"RGB vede la scatola ma depth nel bbox = 0 px "
            f"(frame intero {nz_all} px depth sparsi). "
            "La faccia liscia non riflette pattern stereo — avvicina o aggiungi texture ai bordi."
        )
    return out


def _capture_aligned_orbbec(*, timeout_ms: int | None = None, max_frames: int | None = None) -> dict[str, Any]:
    """Cattura una coppia (color BGR, depth uint16) allineata depth→color dall'Orbbec.

    Apre la pipeline on-demand (color 640x480 MJPG + depth Y16 640x480) e la chiude subito:
    evita il claim USB continuo che confliggerebbe con lo stream V4L2 del dashboard.

    Acquisisce il lock cross-process ``orbbec_lock`` (con ``preempt``): se un altro processo
    (es. jog :5053 o un altro stream) sta usando l'Orbbec, gli chiede di cedere e attende fino
    a ``GO2_ORBBEC_LOCK_TIMEOUT_S``. Se resta occupato torna ``orbbec_busy`` con il detentore.
    """
    import cv2
    import pyorbbecsdk as ob

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
    with _PIPE_LOCK, _metric_capture_lock() as _lk:
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
    for extra in (0.08, 0.0, -0.08, -0.15):
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
        "Depth assente nel riquadro oggetto (faccia liscia/riflettente). "
        "Provo bordi e alone intorno al bbox; avvicina la scatola se persiste."
    )
    ring = _depth_ring_stats(depth_u16, scale_mm, bbox_xyxy)
    if ring.get("ok"):
        ring["depth_fallback_ring"] = True
        return ring
    halo = _depth_halo_stats(depth_u16, scale_mm, bbox_xyxy)
    if halo.get("ok"):
        halo["depth_fallback_halo"] = True
        return halo
    return last


def _depth_ring_stats(depth_u16: np.ndarray, scale_mm: float, bbox_xyxy) -> dict[str, Any]:
    """Mediana depth sul bordo del bbox (evita centro lucido a 0)."""
    h, w = depth_u16.shape[:2]
    x0, y0, x1, y1 = [float(v) for v in bbox_xyxy[:4]]
    bw, bh = (x1 - x0), (y1 - y0)
    if bw < 8 or bh < 8:
        return {"ok": False, "reason": "bbox_too_small", "support": 0}
    inner_frac = _env_float("GO2_ORBBEC_DEPTH_RING_INNER_FRAC", 0.55)
    inner_frac = max(0.25, min(0.82, inner_frac))
    ix0 = int(round(x0 + bw * inner_frac))
    ix1 = int(round(x1 - bw * inner_frac))
    iy0 = int(round(y0 + bh * inner_frac))
    iy1 = int(round(y1 - bh * inner_frac))
    ox0 = int(max(0, min(w - 1, round(x0))))
    oy0 = int(max(0, min(h - 1, round(y0))))
    ox1 = int(max(1, min(w, round(x1))))
    oy1 = int(max(1, min(h, round(y1))))
    if ix1 <= ix0 or iy1 <= iy0:
        margin = max(2, int(min(bw, bh) * 0.08))
        ix0, ix1 = int(round(x0 + margin)), int(round(x1 - margin))
        iy0, iy1 = int(round(y0 + margin)), int(round(y1 - margin))
        if ix1 <= ix0 or iy1 <= iy0:
            return {"ok": False, "reason": "bbox_too_small", "support": 0}
    outer = depth_u16[oy0:oy1, ox0:ox1].copy()
    inner = depth_u16[iy0:iy1, ix0:ix1]
    outer[iy0 - oy0 : iy1 - oy0, ix0 - ox0 : ix1 - ox0] = 0
    nz = outer[outer > 0]
    try:
        min_support = int(float(os.environ.get("GO2_ORBBEC_DEPTH_RING_MIN_SUPPORT", "8")))
    except ValueError:
        min_support = 8
    if nz.size < min_support:
        return {"ok": False, "reason": "no_depth_ring", "support": int(nz.size)}
    med_mm = float(np.median(nz))
    return {
        "ok": True,
        "depth_m": med_mm * float(scale_mm) / 1000.0,
        "support": int(nz.size),
        "roi_px": [ox0, oy0, ox1, oy1],
        "depth_source": "bbox_ring",
        "iqr_m": float(np.subtract(*np.percentile(nz, [75, 25]))) * float(scale_mm) / 1000.0,
    }


def _depth_halo_stats(depth_u16: np.ndarray, scale_mm: float, bbox_xyxy) -> dict[str, Any]:
    """Depth su alone **fuori** dal bbox color (bordi fisici / tavolo accanto a faccia liscia)."""
    h, w = depth_u16.shape[:2]
    x0, y0, x1, y1 = [float(v) for v in bbox_xyxy[:4]]
    bw, bh = max(8.0, x1 - x0), max(8.0, y1 - y0)
    expand = _env_float("GO2_ORBBEC_DEPTH_HALO_EXPAND", 0.20)
    expand = max(0.08, min(0.35, expand))
    ox0 = int(max(0, min(w - 1, round(x0 - bw * expand))))
    oy0 = int(max(0, min(h - 1, round(y0 - bh * expand))))
    ox1 = int(max(1, min(w, round(x1 + bw * expand))))
    oy1 = int(max(1, min(h, round(y1 + bh * expand))))
    ix0 = int(max(0, min(w - 1, round(x0))))
    iy0 = int(max(0, min(h - 1, round(y0))))
    ix1 = int(max(1, min(w, round(x1))))
    iy1 = int(max(1, min(h, round(y1))))
    if ox1 <= ox0 or oy1 <= oy0:
        return {"ok": False, "reason": "halo_roi_empty", "support": 0}
    outer = depth_u16[oy0:oy1, ox0:ox1].copy()
    outer[iy0 - oy0 : iy1 - oy0, ix0 - ox0 : ix1 - ox0] = 0
    nz = outer[outer > 0]
    min_support = int(_env_float("GO2_ORBBEC_DEPTH_HALO_MIN_SUPPORT", 6))
    if nz.size < min_support:
        return {"ok": False, "reason": "no_depth_halo", "support": int(nz.size)}
    med_mm = float(np.median(nz))
    return {
        "ok": True,
        "depth_m": med_mm * float(scale_mm) / 1000.0,
        "support": int(nz.size),
        "roi_px": [ox0, oy0, ox1, oy1],
        "depth_source": "bbox_halo",
        "hint_it": "Depth dai bordi intorno alla scatola (faccia centrale liscia).",
        "iqr_m": float(np.subtract(*np.percentile(nz, [75, 25]))) * float(scale_mm) / 1000.0,
    }


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _estimate_depth_from_bbox_area(det: dict[str, Any]) -> float:
    """Stima distanza da area bbox (no AprilTag): area grande → oggetto vicino."""
    try:
        ar = float(det.get("bbox_area_ratio") or 0.0)
    except (TypeError, ValueError):
        ar = 0.0
    if ar <= 0:
        try:
            area = float(det.get("bbox_area_px") or 0.0)
            ar = area / (640.0 * 480.0)
        except (TypeError, ValueError):
            ar = 0.02
    ref_ar = _env_float("GO2_WRIST_RGB_DEPTH_REF_AREA_RATIO", 0.04)
    ref_m = _env_float("GO2_WRIST_RGB_DEPTH_REF_DIST_M", 0.48)
    z = ref_m * math.sqrt(ref_ar / max(ar, 0.005))
    z_min = _env_float("GO2_WRIST_RGB_DEPTH_MIN_M", 0.32)
    z_max = _env_float("GO2_WRIST_RGB_DEPTH_MAX_M", 0.68)
    return max(z_min, min(z_max, z))


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
            tag=_wrist_debug_tag(), logical_camera=0, step="graspgen_metric_plan",
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
        tb, min_z_adjustment = _apply_metric_grasp_min_z(tb, det)
        considered.append({"idx": gi, "confidence": round(float(conf), 4),
                           "camera_xyz_m": [round(x, 4) for x in t],
                           "target_xyz_m": [round(x, 4) for x in tb],
                           "reach_m": round(reach_m, 4), "reachable": reachable,
                           "min_z_adjustment": min_z_adjustment})
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
                      "grasp_4x4": [[round(float(g4[r][cc]), 5) for cc in range(4)] for r in range(4)],
                      "min_z_adjustment": min_z_adjustment}
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
            "min_z_adjustment": chosen.get("min_z_adjustment"),
        },
        "target": {
            "base_xyz_m": target_bl_round,
            "camera_xyz_m": cam_xyz,
            "depth_median_m": round(Zc, 4),
            "depth_support_fraction": 1.0,
            "source": "graspgen_orbbec_metric",
            "ok": True,
            "min_z_adjustment": chosen.get("min_z_adjustment"),
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


def plan_wrist_grasp_metric(
    servo_deg7: list[float],
    *,
    instruction: str | None = None,
    color_hint: str | None = None,
    fast_capture: bool = False,
) -> dict[str, Any]:
    """Piano presa metrico dal polso alla posa servo corrente. Ritorna struttura ricca per UI/IK."""
    t0 = time.time()
    cap = capture_aligned(fast=fast_capture)
    if not cap.get("ok"):
        return {"ok": False, "reason": cap.get("reason", "capture_failed"), "detail": cap.get("detail")}

    depth_nonzero_px = cap.get("depth_nonzero_px")

    import sys

    from go2_dashboard.paths import PROJECT_ROOT

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    from box_object_detector import detect_box_object, parse_color_from_instruction
    import arm_kinematics_d1_template as K

    hint = color_hint or parse_color_from_instruction(instruction or "")

    color = cap["color_bgr"]
    depth = cap["depth_u16"]
    intr = cap["intrinsics"]
    scale_mm = cap["depth_scale_mm"]

    det = _filter_wrist_detection(detect_box_object(color, color_hint=hint), intr)
    debug_snap: dict[str, Any] = {}
    try:
        from go2_dashboard.grasp_detect_debug import save_detection_snapshot

        debug_snap = save_detection_snapshot(
            color,
            det if isinstance(det, dict) else None,
            tag=_wrist_debug_tag(),
            logical_camera=0,
            step="wrist_metric_plan",
        )
    except Exception as exc:
        debug_snap = {"saved": False, "error": repr(exc)}
    if not det.get("ok") or not det.get("bbox_center_px"):
        return {"ok": False, "reason": "no_detection", "detection": det, "debug_snapshot": debug_snap}
    det.setdefault("frame_size_px", [intr["width"], intr["height"]])
    det["logical_camera"] = 0
    if instruction:
        det["instruction"] = instruction
    if hint:
        det["color_hint"] = hint

    cx, cy = det["bbox_center_px"]
    dm = _depth_median_m(depth, scale_mm, det["bbox_xyxy"])
    if not dm.get("ok"):
        retries = 0 if fast_capture else max(0, int(_env_float("GO2_ORBBEC_DEPTH_CAPTURE_RETRIES", 1)))
        for _ in range(retries):
            cap2 = capture_aligned(fast=fast_capture)
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
        rgb_fb = _truthy_env("GO2_WRIST_RGB_DEPTH_FALLBACK", "1") and bool(det.get("ok"))
        if rgb_fb:
            z_fb = _estimate_depth_from_bbox_area(det)
            bbox = _tolist(det.get("bbox_xyxy"), []) or []
            roi_px = (
                [int(round(float(bbox[0]))), int(round(float(bbox[1]))), int(round(float(bbox[2]))), int(round(float(bbox[3])))]
                if len(bbox) >= 4
                else [0, 0, 0, 0]
            )
            # Depth stimata: NON usare IK 3D (target falso → braccio va fuori verso la scatola).
            out_rgb: dict[str, Any] = {
                "ok": False,
                "reason": "rgb_depth_estimate_only",
                "partial_rgb_ok": True,
                "rgb_depth_fallback": True,
                "depth_m": z_fb,
                "depth_source": "rgb_bbox_area",
                "depth_support": int(dm.get("support") or 0),
                "depth_diag": dm,
                "depth_nonzero_px": depth_nonzero_px,
                "roi_px": roi_px,
                "detection": det,
                "object_detection": det,
                "debug_snapshot": debug_snap,
                "hint_it": (
                    f"Depth stimata da bbox ({z_fb:.2f} m) — solo servo visivo sul riquadro, "
                    "senza IK 3D finché la D456 non dà depth reale."
                ),
            }
            if debug_snap.get("image_url"):
                out_rgb["metric_viz_url"] = str(debug_snap["image_url"])
            # #region agent log
            try:
                import json
                from go2_dashboard.paths import PROJECT_ROOT

                payload = {
                    "sessionId": "16a61f",
                    "runId": "grasp-fix-v1",
                    "hypothesisId": "H1",
                    "location": "orbbec_wrist_grasp.py:rgb_fb",
                    "message": "rgb_depth_estimate_only early return",
                    "data": {"depth_m": z_fb, "bbox": det.get("bbox_xyxy"), "roi_px": roi_px},
                    "timestamp": int(time.time() * 1000),
                }
                log_path = PROJECT_ROOT / "data" / "debug-16a61f.ndjson"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass
            # #endregion
            return out_rgb
        else:
            out_fail: dict[str, Any] = {
                "ok": False,
                "reason": dm.get("reason", "depth_failed"),
                "detection": det,
                "depth_diag": dm,
                "debug_snapshot": debug_snap,
                "hint_it": dm.get("hint_it")
                or "Oggetto visto in RGB ma depth polso insufficiente nel bbox — riprova o avvicina la scatola.",
                "object_detection": det,
                "partial_rgb_ok": True,
            }
            if debug_snap.get("image_url"):
                out_fail["metric_viz_url"] = str(debug_snap["image_url"])
            return out_fail
    Z = float(dm["depth_m"])
    depth_source = str(dm.get("depth_source") or "realsense_metric")
    rgb_depth_fallback = bool(dm.get("rgb_depth_fallback"))
    if not depth_plausible_m(Z):
        rgb_fb = _truthy_env("GO2_WRIST_RGB_DEPTH_FALLBACK", "1") and bool(det.get("ok"))
        hint = (
            f"Depth {Z:.2f} m fuori range plausibile polso "
            f"({_env_float('GO2_WRIST_DEPTH_PLAUSIBLE_MIN_M', 0.18):.2f}–"
            f"{_env_float('GO2_WRIST_DEPTH_PLAUSIBLE_MAX_M', 1.25):.2f} m) — "
            "probabile sfondo/riflesso nel bbox."
        )
        if rgb_fb:
            z_fb = _estimate_depth_from_bbox_area(det)
            out_rgb: dict[str, Any] = {
                "ok": False,
                "reason": "depth_implausible",
                "partial_rgb_ok": True,
                "rgb_depth_fallback": True,
                "depth_m": z_fb,
                "depth_m_raw": round(Z, 4),
                "depth_source": "rgb_bbox_area",
                "depth_support": dm.get("support", 0),
                "roi_px": dm.get("roi_px"),
                "detection": det,
                "object_detection": det,
                "debug_snapshot": debug_snap,
                "hint_it": hint + f" Uso stima RGB ({z_fb:.2f} m) + servo visivo.",
            }
            if debug_snap.get("image_url"):
                out_rgb["metric_viz_url"] = str(debug_snap["image_url"])
            return out_rgb
        return {
            "ok": False,
            "reason": "depth_implausible",
            "depth_m_raw": round(Z, 4),
            "depth_diag": dm,
            "detection": det,
            "object_detection": det,
            "partial_rgb_ok": True,
            "debug_snapshot": debug_snap,
            "hint_it": hint,
        }

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
    tb, min_z_adjustment = _apply_metric_grasp_min_z(tb, det)

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
        "rgb_depth_fallback": rgb_depth_fallback,
        # ``absolute_ik_safe`` gate: blocca l'esecuzione se fuori reach o IK non completa.
        "absolute_ik_safe": bool(reachable and ik_all_ok),
        # ``depth_observation`` fa promuovere il piano a 3D validato in worker_flat_plan_assessment.
        "depth_observation": {
            "ok": True,
            "source": depth_source,
            "depth_median_m": round(Z, 4),
            "depth_support_fraction": 1.0 if not rgb_depth_fallback else 0.0,
            "depth_iqr_m": round(dm.get("iqr_m", 0.0), 4),
            "rgb_depth_fallback": rgb_depth_fallback,
        },
        "image_source": "orbbec_sdk_aligned",
        "rgbd_embedded": True,
        "depth_embedded": True,
        "object_detection": det,
        "camera_xyz_m": camera_xyz,
        "depth_m": round(Z, 4),
        "depth_source": depth_source,
        "depth_support": dm["support"],
        "depth_nonzero_px": depth_nonzero_px,
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
            "min_z_adjustment": min_z_adjustment,
        },
        "grasp_display_base_link_m": target_bl_round,
        "metric_target_adjustment": min_z_adjustment,
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
    try:
        from go2_dashboard.grasp_online_calib import apply_online_offset

        out = apply_online_offset(out)
    except Exception:
        pass
    return out
