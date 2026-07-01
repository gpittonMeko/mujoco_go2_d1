from __future__ import annotations

import os
import threading
import time
from typing import Any

from flask import request

from go2_dashboard.cameras import (
    CAMERA_CACHE,
    CAMERA_DEVICES,
    _v4l_index_for_logical_camera,
    _v4l_sysfs_card_name,
    debug_v4l_snapshot_jpeg,
    get_runtime_v4l_overrides,
    orbbec_logical0_probe_debug,
    set_runtime_v4l_overrides,
    usb_auto_v4l_mapping,
    v4l_candidates_for_logical_slot,
    v4l_index_in_usb_inventory,
    v4l_usb_inventory,
    wrist_depth_backend,
)
from go2_dashboard.operator_stack import go2_local

try:
    import cv2
except Exception:
    cv2 = None
try:
    import numpy as np
except Exception:
    np = None  # type: ignore[misc, assignment]


_RS_W_COLOR_LOCK = threading.Lock()
_RS_W_COLOR_CACHE: dict[str, Any] = {"ts": 0.0, "jpg": None}


def _wrist_realsense_color_jpeg() -> bytes | None:
    if cv2 is None:
        return None
    with _RS_W_COLOR_LOCK:
        now = time.time()
        ttl_s = float(os.environ.get("GO2_LOG0_RS_COLOR_TTL_S", "0.22"))
        cached = _RS_W_COLOR_CACHE.get("jpg")
        ts = float(_RS_W_COLOR_CACHE.get("ts") or 0.0)
        if isinstance(cached, (bytes, bytearray)) and now - ts <= max(0.06, ttl_s):
            return bytes(cached)
        try:
            from go2_dashboard import realsense_pyrs as rp

            cap = rp.capture_aligned_on_demand(role="wrist", fast=True, include_ir=False)
            color = cap.get("color_bgr") if isinstance(cap, dict) else None
            if color is None:
                return None
            q = int(os.environ.get("GO2_CAMERA_JPEG_QUALITY", "82"))
            ok, buf = cv2.imencode(".jpg", color, [int(cv2.IMWRITE_JPEG_QUALITY), q])
            if not ok or buf is None:
                return None
            jpg = bytes(buf.tobytes())
            _RS_W_COLOR_CACHE["jpg"] = jpg
            _RS_W_COLOR_CACHE["ts"] = now
            return jpg
        except Exception:
            return None

def _operator_camera_summary(
    cam_stats: dict[str, Any],
    inventory: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    orbbec = [
        r
        for r in (inventory or [])
        if str(r.get("usb_vid_pid", "")).replace(" ", "").lower() == "2bc5:080b"
    ]
    for log in sorted(CAMERA_DEVICES.keys()):
        key = str(log)
        st = cam_stats.get(key, {})
        v4l = _v4l_index_for_logical_camera(int(log))
        name = _v4l_sysfs_card_name(v4l)
        sk = st.get("stream_kind")
        rgb_like = bool(st.get("rgb_like"))
        color_ok = rgb_like or sk == "rgb"
        entry: dict[str, Any] = {
            "logical": int(log),
            "v4l_index": int(v4l),
            "device_path": f"/dev/video{v4l}",
            "sysfs_name": name,
            "stream_kind": sk,
            "color_ok": bool(color_ok),
            "error": st.get("error"),
        }
        if int(log) == 0 and orbbec and wrist_depth_backend() == "orbbec" and not color_ok:
            named_rgb = sorted(
                {
                    int(r["v4l_index"])
                    for r in orbbec
                    if "rgb" in (r.get("sysfs_name") or "").lower()
                    or "color" in (r.get("sysfs_name") or "").lower()
                    or "colour" in (r.get("sysfs_name") or "").lower()
                }
            )
            cand_txt = ", ".join(str(x) for x in named_rgb[:8]) if named_rgb else "vedi v4l_usb_inventory (nomi sysfs)"
            entry["fix_it"] = (
                "Stream con reticolo punti = quasi sempre IR/depth, non RGB. "
                f"Sulla NX: export GO2_VIDEO_INDEX_0=N con N tra gli indici Orbbec il cui nome sysfs contiene "
                f"RGB o Color (es. {cand_txt}). Poi riavvia la dashboard. "
                "Oppure apri i JPEG di debug per ogni N se GO2_ALLOW_RAW_V4L_DEBUG=1."
            )
        summary[key] = entry
    s0 = summary.get("0")
    s6 = summary.get("6")
    wrist_backend = wrist_depth_backend()
    if (
        isinstance(s0, dict)
        and isinstance(s6, dict)
        and s0.get("device_path")
        and s0.get("device_path") == s6.get("device_path")
    ):
        summary["_conflict"] = {
            "ok": False,
            "reason": "same_v4l_device",
            "hint_it": (
                f"log.0 (polso) e log.6 (frontale) puntano entrambi a {s0.get('device_path')}. "
                "Rimuovi GO2_VIDEO_INDEX_0/6 errati sulla NX: log.0 = D456 (8086:0b5c), "
                "log.6 = D435i (8086:0b3a). Tab Scene → frecce su log.0/log.6."
            ),
        }
    elif (
        wrist_backend == "orbbec"
        and isinstance(s0, dict)
        and "realsense" in str(s0.get("sysfs_name") or "").lower()
    ):
        summary["_conflict"] = {
            "ok": False,
            "reason": "log0_is_realsense",
            "hint_it": (
                f"log.0 è mappato su RealSense ({s0.get('device_path')}) ma il backend polso è Orbbec. "
                "Imposta GO2_WRIST_DEPTH_BACKEND=realsense oppure correggi GO2_VIDEO_INDEX_0."
            ),
        }
    return summary


try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None
try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[misc, assignment]
def _orbbec_rgb_sysfs_hints(inv: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not inv:
        return out
    for row in inv:
        if str(row.get("usb_vid_pid", "")).replace(" ", "").lower() != "2bc5:080b":
            continue
        name = str(row.get("sysfs_name") or "")
        nl = name.lower()
        if "rgb" in nl or "color" in nl or "colour" in nl:
            try:
                idx = int(row["v4l_index"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append({"v4l_index": idx, "sysfs_name": name})
    return sorted(out, key=lambda x: int(x["v4l_index"]))


def _enrich_v4l_nodes_detail(
    inv: list[dict[str, Any]],
    *,
    v4l_by_log: dict[str, Any] | None,
    depth_by_log: dict[str, Any] | None,
    http_origin: str,
    script_root: str,
) -> list[dict[str, Any]]:
    prefix = (script_root or "").rstrip("/")
    rgb_rev: dict[int, list[int]] = {}
    for log_s, v in (v4l_by_log or {}).items():
        try:
            rgb_rev.setdefault(int(v), []).append(int(log_s))
        except (TypeError, ValueError):
            continue
    depth_rev: dict[int, list[int]] = {}
    for log_s, v in (depth_by_log or {}).items():
        if v is None:
            continue
        try:
            depth_rev.setdefault(int(v), []).append(int(log_s))
        except (TypeError, ValueError):
            continue
    base = (http_origin or "").rstrip("/")
    out: list[dict[str, Any]] = []
    for row in inv:
        idx = int(row["v4l_index"])
        r = dict(row)
        vidp = str(r.get("usb_vid_pid", "")).replace(" ", "").lower()
        if vidp == "2bc5:080b":
            r["device_family"] = "orbbec_gemini"
        elif vidp == "8086:0b3a":
            r["device_family"] = "intel_realsense"
        else:
            r["device_family"] = "usb_other"
        r["maps_as_rgb_for_logical"] = sorted(rgb_rev.get(idx, []))
        r["maps_as_depth_for_logical"] = sorted(depth_rev.get(idx, []))
        slots = r.get("dashboard_logical_slots") or []
        r["dashboard_logical_slots"] = list(slots)
        nm = (r.get("sysfs_name") or "").lower()
        if any(x in nm for x in ("rgb", "color", "colour")):
            r["sysfs_stream_guess"] = "color_sysfs"
        elif any(x in nm for x in ("depth", "ir", "infra")):
            r["sysfs_stream_guess"] = "depth_ir_sysfs"
        else:
            r["sysfs_stream_guess"] = "generic_sysfs"
        path = f"/api/cameras/v4l/{idx}/preview.jpg"
        r["preview_jpg_path"] = path
        r["preview_jpg_url"] = f"{base}{prefix}{path}" if base else (f"{prefix}{path}" if prefix else path)
        out.append(r)
    return sorted(out, key=lambda x: int(x["v4l_index"]))


def _depth_v4l_index_for_logical_camera(device: int) -> int | None:
    key = f"GO2_DEPTH_VIDEO_INDEX_{int(device)}"
    raw = os.environ.get(key, os.environ.get("GO2_DEPTH_VIDEO_INDEX", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _depth_sysfs_hint_rows(
    inv: list[dict[str, Any]],
    *,
    http_origin: str,
    script_root: str,
) -> list[dict[str, Any]]:
    prefix = (script_root or "").rstrip("/")
    base = (http_origin or "").rstrip("/")
    out: list[dict[str, Any]] = []
    for row in inv:
        nm = str(row.get("sysfs_name") or "").lower()
        if "depth" not in nm and "ir" not in nm and "infra" not in nm:
            continue
        try:
            idx = int(row["v4l_index"])
        except (KeyError, TypeError, ValueError):
            continue
        path = f"/api/cameras/v4l/{idx}/preview.jpg"
        url = f"{base}{prefix}{path}" if base else (f"{prefix}{path}" if prefix else path)
        vidp = str(row.get("usb_vid_pid", "")).replace(" ", "").lower()
        if vidp == "2bc5:080b":
            fam = "orbbec_gemini"
        elif vidp == "8086:0b3a":
            fam = "intel_realsense"
        else:
            fam = "usb_other"
        out.append(
            {
                "v4l_index": idx,
                "sysfs_name": row.get("sysfs_name"),
                "usb_vid_pid": row.get("usb_vid_pid"),
                "device_family": fam,
                "preview_jpg_path": path,
                "preview_jpg_url": url,
                "note_it": "Anteprima JPEG UVC (spesso IR/mappa grezza). Depth metrica: SDK RealSense/Orbbec o env GO2_DEPTH_VIDEO_INDEX_*.",
            }
        )
    return sorted(out, key=lambda x: int(x["v4l_index"]))



def _robot_camera_jpeg(device: int) -> bytes | None:
    if go2_local() and cv2 is not None:
        if int(device) == 0:
            stats0 = CAMERA_CACHE.stats().get("0", {}) if hasattr(CAMERA_CACHE, "stats") else {}
            stream_kind = str(stats0.get("stream_kind") or "")
            rgb_like = bool(stats0.get("rgb_like"))
            force_rs = os.environ.get("GO2_LOG0_FORCE_RS_COLOR", "1").strip().lower() in {"1", "true", "yes", "on"}
            if force_rs and (stream_kind in {"mono_or_ir", "depth"} or not rgb_like):
                rs_jpg = _wrist_realsense_color_jpeg()
                if rs_jpg:
                    return rs_jpg
        jpg = CAMERA_CACHE.get_jpeg(device, wait_s=2.0)
        if jpg:
            return jpg
        jpg = CAMERA_CACHE.peek_jpeg(device)
        if jpg:
            return jpg
        from go2_dashboard.cameras import usb_auto_v4l_mapping, v4l_open_candidates_for_logical

        for v4l_idx in v4l_open_candidates_for_logical(device):
            snap = debug_v4l_snapshot_jpeg(int(v4l_idx))
            if snap:
                return snap
        auto = usb_auto_v4l_mapping()
        if device in auto:
            snap = debug_v4l_snapshot_jpeg(int(auto[device]))
            if snap:
                return snap
    return None

