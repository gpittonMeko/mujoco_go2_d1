"""Preset presa: offset giunti fissi rispetto alla posa scansione (non IK da pixel)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from go2_dashboard.d1_jog import program_store, service
from go2_dashboard.paths import PROJECT_ROOT

_PRESET_PATH = Path(
    os.environ.get(
        "D1_PICK_PRESET_PATH",
        str(PROJECT_ROOT / "data" / "d1_pick_preset.json"),
    )
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def load_preset() -> dict[str, Any]:
    if not _PRESET_PATH.is_file():
        return {}
    try:
        return json.loads(_PRESET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_preset(data: dict[str, Any]) -> dict[str, Any]:
    _PRESET_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = _now_iso()
    _PRESET_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def joint_offset_deg() -> list[float] | None:
    raw = load_preset().get("joint_offset_deg")
    if not isinstance(raw, list) or len(raw) < 6:
        return None
    out = [float(x) for x in raw[:7]]
    while len(out) < 7:
        out.append(out[-1])
    return out


def offsets_from_program_waypoints() -> dict[str, Any]:
    """Delta PRESA − SCANSIONE dal programma salvato."""
    scan = program_store.find_scan_waypoint()
    if scan is None:
        return {"ok": False, "reason": "scan_waypoint_not_found"}
    grasp_sub = (os.environ.get("D1_GRASP_WAYPOINT_SUBSTR") or "presa").strip()
    grasp = program_store.find_waypoint_by_name_substr(grasp_sub, program_id=scan[0])
    if grasp is None:
        return {"ok": False, "reason": "grasp_waypoint_not_found", "hint": f"Cerca waypoint con «{grasp_sub}» nel programma"}
    _pid, scan_wp = scan
    _pid2, grasp_wp = grasp
    scan_sd = scan_wp.get("servo_deg")
    grasp_sd = grasp_wp.get("servo_deg")
    if not isinstance(scan_sd, list) or not isinstance(grasp_sd, list):
        return {"ok": False, "reason": "invalid_waypoint_servo"}
    s = [float(x) for x in scan_sd[:7]]
    g = [float(x) for x in grasp_sd[:7]]
    while len(s) < 7:
        s.append(s[-1])
    while len(g) < 7:
        g.append(g[-1])
    delta = [round(g[i] - s[i], 3) for i in range(7)]
    return {
        "ok": True,
        "program_id": _pid,
        "scan_waypoint": scan_wp.get("name"),
        "grasp_waypoint": grasp_wp.get("name"),
        "scan_servo_deg": s,
        "grasp_servo_deg": g,
        "joint_offset_deg": delta,
    }


def _vision_pixel_delta(
    ref: dict[str, Any] | None,
    cur: dict[str, Any] | None,
) -> tuple[float, float] | None:
    if not ref or not cur:
        return None
    if not ref.get("detected") or not cur.get("detected"):
        return None
    rc = ref.get("grip_center_px")
    nc = cur.get("grip_center_px")
    if not isinstance(rc, (list, tuple)) or not isinstance(nc, (list, tuple)):
        return None
    if len(rc) < 2 or len(nc) < 2:
        return None
    return float(nc[0]) - float(rc[0]), float(nc[1]) - float(rc[1])


def effective_joint_offsets(*, last_detection: dict[str, Any] | None = None) -> list[float] | None:
    """Offset presa + correzione px rispetto alla calibrazione zero (se presente)."""
    off = joint_offset_deg()
    if off is None:
        return None
    out = [float(x) for x in off[:7]]
    while len(out) < 7:
        out.append(out[-1])
    data = load_preset()
    zc = data.get("zero_calibration")
    if not isinstance(zc, dict):
        return out
    ref_vis = zc.get("vision_at_scan")
    cur = last_detection if last_detection is not None else data.get("last_detection")
    dpx = _vision_pixel_delta(
        ref_vis if isinstance(ref_vis, dict) else None,
        cur if isinstance(cur, dict) else None,
    )
    if dpx is None:
        return out
    k0 = float(os.environ.get("D1_PICK_PX_TO_J0_DEG", "0.04"))
    k1 = float(os.environ.get("D1_PICK_PX_TO_J1_DEG", "0.035"))
    k2 = float(os.environ.get("D1_PICK_PX_TO_J2_DEG", "0.015"))
    out[0] = round(out[0] + dpx[0] * k0, 3)
    out[1] = round(out[1] + dpx[1] * k1, 3)
    out[2] = round(out[2] + dpx[1] * k2, 3)
    ref_norm = ref_vis.get("norm") if isinstance(ref_vis, dict) else None
    cur_norm = cur.get("norm") if isinstance(cur, dict) else None
    if (
        isinstance(ref_norm, (list, tuple))
        and isinstance(cur_norm, (list, tuple))
        and len(ref_norm) >= 2
        and len(cur_norm) >= 2
    ):
        out[0] = round(out[0] + (float(cur_norm[0]) - float(ref_norm[0])) * 8.0, 3)
        out[1] = round(out[1] + (float(cur_norm[1]) - float(ref_norm[1])) * 6.0, 3)
    return out


def grasp_servo_from_scan(
    scan_servo_deg: list[float],
    *,
    offsets: list[float] | None = None,
    last_detection: dict[str, Any] | None = None,
) -> list[float] | None:
    off = offsets if offsets is not None else effective_joint_offsets(last_detection=last_detection)
    if off is None:
        return None
    base = [float(x) for x in scan_servo_deg[:7]]
    while len(base) < 7:
        base.append(base[-1])
    target = [round(base[i] + float(off[i]), 3) for i in range(7)]
    return service.clamp_servo_deg(target)


def preset_info() -> dict[str, Any]:
    data = load_preset()
    off = joint_offset_deg()
    scan = program_store.find_scan_waypoint()
    out: dict[str, Any] = {
        "ok": True,
        "preset_path": str(_PRESET_PATH),
        "has_preset": off is not None,
        "joint_offset_deg": off,
        "updated_at": data.get("updated_at"),
        "last_detection": data.get("last_detection"),
        "source": data.get("source"),
    }
    zc = data.get("zero_calibration")
    out["has_zero_calibration"] = isinstance(zc, dict) and bool(zc.get("joint_offset_deg"))
    if isinstance(zc, dict):
        out["zero_calibration_at"] = zc.get("at")
        ref_vis = zc.get("vision_at_scan")
        ld = data.get("last_detection")
        dpx = _vision_pixel_delta(
            ref_vis if isinstance(ref_vis, dict) else None,
            ld if isinstance(ld, dict) else None,
        )
        if dpx is not None:
            out["vision_pixel_delta"] = [round(dpx[0], 1), round(dpx[1], 1)]
    if scan and off:
        _pid, wp = scan
        sd = wp.get("servo_deg")
        if isinstance(sd, list):
            eff = effective_joint_offsets(last_detection=data.get("last_detection"))
            grasp = grasp_servo_from_scan(
                sd,
                offsets=eff,
                last_detection=data.get("last_detection"),
            )
            out["scan_servo_deg"] = sd
            out["joint_offset_deg_effective"] = eff
            out["grasp_servo_deg_computed"] = grasp
    return out


def finish_zero_calibration_after_release(
    *,
    vision_at_scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dopo teach in release: coppia forzata, feedback, salva presa zero."""
    import time

    couple = service.ensure_coupled(with_power=True, force=True)
    if not couple.get("ok") and not couple.get("skipped"):
        return {"ok": False, "reason": "couple_failed", "coupling": couple}
    settle = float(os.environ.get("D1_PICK_CALIB_COUPLE_SETTLE_S", "0.8"))
    if settle > 0:
        time.sleep(settle)
    fb = service.read_servo_deg(fast=False)
    if not fb.get("ok") or not fb.get("servo_deg"):
        return {
            "ok": False,
            "reason": "no_feedback_after_couple",
            "coupling": couple,
            "feedback": fb,
        }
    out = save_zero_calibration(
        list(fb["servo_deg"]),
        vision_at_scan=vision_at_scan,
    )
    out["coupling"] = couple
    out["servo_from_feedback"] = True
    return out


def save_zero_calibration(
    current_servo_deg: list[float],
    *,
    vision_at_scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Presa zero: offset insegnato a mano + riferimento visione alla scansione."""
    taught = offsets_from_current_vs_scan(current_servo_deg)
    if not taught.get("ok"):
        return taught
    data = load_preset()
    vis = vision_at_scan if isinstance(vision_at_scan, dict) else data.get("last_detection")
    data["zero_calibration"] = {
        "at": _now_iso(),
        "vision_at_scan": vis,
        "joint_offset_deg": taught.get("joint_offset_deg"),
        "scan_servo_deg": taught.get("scan_servo_deg"),
        "taught_servo_deg": taught.get("taught_servo_deg"),
        "scan_waypoint": taught.get("scan_waypoint"),
    }
    data["source"] = "zero_calibration"
    data["joint_offset_deg"] = taught.get("joint_offset_deg")
    save_preset(data)
    info = preset_info()
    info["ok"] = True
    info["zero_calibration"] = data["zero_calibration"]
    info["hint_it"] = (
        "Presa zero salvata — «Foto · riconoscimento» + «Presa oggetto»: "
        "offset insegnato + correzione se sposti il pezzo in immagine."
    )
    return info


def offsets_from_current_vs_scan(current_servo_deg: list[float]) -> dict[str, Any]:
    """Δ giunti = posa attuale − waypoint SCANSIONE (calibrazione teach)."""
    scan = program_store.find_scan_waypoint()
    if scan is None:
        return {"ok": False, "reason": "scan_waypoint_not_found"}
    _pid, scan_wp = scan
    scan_sd = scan_wp.get("servo_deg")
    if not isinstance(scan_sd, list):
        return {"ok": False, "reason": "invalid_scan_waypoint"}
    cur = [float(x) for x in current_servo_deg[:7]]
    base = [float(x) for x in scan_sd[:7]]
    while len(cur) < 7:
        cur.append(cur[-1])
    while len(base) < 7:
        base.append(base[-1])
    delta = [round(cur[i] - base[i], 3) for i in range(7)]
    info = set_offsets(delta, source="teach_pose_vs_scan")
    info["ok"] = True
    info["scan_waypoint"] = scan_wp.get("name")
    info["scan_servo_deg"] = base
    info["taught_servo_deg"] = cur
    info["joint_offset_deg"] = delta
    return info


def nudge_offsets(
    *,
    joint_index: int,
    delta_deg: float,
) -> dict[str, Any]:
    """Aggiunge delta_deg all'offset di un giunto (calibrazione fine)."""
    idx = int(joint_index)
    if idx < 0 or idx > 6:
        return {"ok": False, "reason": "joint_index_out_of_range"}
    off = joint_offset_deg()
    if off is None:
        derived = offsets_from_program_waypoints()
        if derived.get("ok"):
            off = list(derived["joint_offset_deg"])
        else:
            off = [0.0] * 7
    off = list(off)
    off[idx] = round(float(off[idx]) + float(delta_deg), 3)
    info = set_offsets(off, source="nudge")
    info["ok"] = True
    info["nudged_joint"] = idx
    info["nudged_delta_deg"] = float(delta_deg)
    return info


def set_offsets(
    joint_offset_deg_list: list[float],
    *,
    source: str = "manual",
    last_detection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    off = [round(float(x), 3) for x in joint_offset_deg_list[:7]]
    while len(off) < 7:
        off.append(off[-1])
    data = load_preset()
    data["joint_offset_deg"] = off
    data["source"] = source
    if last_detection is not None:
        data["last_detection"] = last_detection
    save_preset(data)
    return preset_info()
