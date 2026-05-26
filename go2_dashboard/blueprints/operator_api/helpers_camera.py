from __future__ import annotations

import os
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
        if int(log) == 0 and orbbec and not color_ok:
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
        return CAMERA_CACHE.get_jpeg(device)
    return None

