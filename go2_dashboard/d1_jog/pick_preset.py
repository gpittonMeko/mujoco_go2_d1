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
# Offset calibrazione presa: solo braccio J0–J5; J6 (pinza) gestito a parte.
_ARM_OFFSET_JOINTS = 6
_GRIPPER_JOINT_INDEX = 6


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


def _sanitize_arm_offsets(off: list[float]) -> list[float]:
    """Pinza (J6) sempre 0 nell'offset — apertura/chiusura non in calibrazione."""
    out = [0.0] * 7
    for i in range(_ARM_OFFSET_JOINTS):
        if i < len(off):
            out[i] = round(float(off[i]), 3)
    return out


def joint_offset_deg() -> list[float] | None:
    raw = load_preset().get("joint_offset_deg")
    if not isinstance(raw, list) or len(raw) < 6:
        return None
    return _sanitize_arm_offsets([float(x) for x in raw[:7]])


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
    delta = _sanitize_arm_offsets([round(g[i] - s[i], 3) for i in range(7)])
    return {
        "ok": True,
        "program_id": _pid,
        "scan_waypoint": scan_wp.get("name"),
        "grasp_waypoint": grasp_wp.get("name"),
        "scan_servo_deg": s,
        "grasp_servo_deg": g,
        "joint_offset_deg": delta,
    }


def _orient_enabled() -> bool:
    return os.environ.get("D1_PICK_ORIENT_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _preset_tuning() -> dict[str, Any]:
    data = load_preset().get("tuning")
    return data if isinstance(data, dict) else {}


def _preset_tuning_float(key: str, env_key: str, default: float) -> float:
    tuning = _preset_tuning()
    if key in tuning:
        try:
            return float(tuning[key])
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _orient_joint_index() -> int:
    return int(os.environ.get("D1_PICK_ORIENT_JOINT_INDEX", "5"))


def _orient_gain() -> float:
    return _preset_tuning_float("orient_gain", "D1_PICK_ORIENT_GAIN", 1.0)


def _orient_sign() -> float:
    raw = (os.environ.get("D1_PICK_ORIENT_SIGN") or "1").strip()
    try:
        s = float(raw)
    except ValueError:
        s = 1.0
    return -1.0 if s < 0 else 1.0


def _orient_perp_offset_deg() -> float:
    """Pinza ⊥ freccia lungo asse scatola: J5 segue asse corto (= lungo + 90°)."""
    return _preset_tuning_float("orient_perp_offset_deg", "D1_PICK_ORIENT_PERP_OFFSET_DEG", 90.0)


def _orient_max_delta_deg() -> float:
    return max(1.0, _preset_tuning_float("orient_max_delta_deg", "D1_PICK_ORIENT_MAX_DELTA_DEG", 12.0))


def _orient_smooth_alpha() -> float:
    return min(1.0, max(0.05, _preset_tuning_float("orient_smooth_alpha", "D1_PICK_ORIENT_SMOOTH_ALPHA", 0.4)))


def _circular_mean_deg(a: float, b: float, *, wa: float = 0.6, wb: float = 0.4) -> float:
    import math

    ra = math.radians(float(a))
    rb = math.radians(float(b))
    x = wa * math.cos(ra) + wb * math.cos(rb)
    y = wa * math.sin(ra) + wb * math.sin(rb)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return _normalize_angle_deg(a)
    return _normalize_angle_deg(math.degrees(math.atan2(y, x)))


def _det_axis_angle_deg(det: dict[str, Any], key: str) -> float | None:
    import math

    pts = det.get(key)
    if not isinstance(pts, list) or len(pts) < 2:
        return None
    p0, p1 = pts[0], pts[1]
    if not isinstance(p0, (list, tuple)) or not isinstance(p1, (list, tuple)):
        return None
    if len(p0) < 2 or len(p1) < 2:
        return None
    dx = float(p1[0]) - float(p0[0])
    dy = float(p1[1]) - float(p0[1])
    if abs(dx) + abs(dy) < 2.0:
        return None
    return round(math.degrees(math.atan2(dy, dx)), 2)


def _pick_measure_angle_deg(
    det: dict[str, Any],
    *,
    ref_long: float | None = None,
    mode: str | None = None,
) -> tuple[float | None, str]:
    """J5 segue il lato CORTO; l'asse lungo serve solo per overlay e stabilità."""
    _ = ref_long
    short_axis = _det_axis_angle_deg(det, "grip_align_axis_px")
    if short_axis is None and det.get("grip_align_deg") is not None:
        short_axis = _normalize_angle_deg(float(det["grip_align_deg"]))
    if short_axis is None:
        long_axis = _det_axis_angle_deg(det, "orient_axis_px")
        if long_axis is None and det.get("orientation_deg") is not None:
            long_axis = _normalize_angle_deg(float(det["orientation_deg"]))
        if long_axis is not None:
            short_axis = _normalize_angle_deg(long_axis + _orient_perp_offset_deg())
    if short_axis is not None:
        return short_axis, mode or "short_side"
    raw = det.get("orientation_deg")
    if raw is not None:
        return _normalize_angle_deg(float(raw) + _orient_perp_offset_deg()), "short_from_long"
    return None, "none"


def _best_delta_to_ref_deg(ref_angle: float, cur_angle: float) -> float:
    """Ref fisso (calib); prova cur±90°/180° e tiene il Δ minimo (ambiguità rettangolo)."""
    best: float | None = None
    for adj in (0.0, 90.0, -90.0, 180.0):
        d = _angle_delta_shortest(cur_angle + adj, ref_angle)
        if best is None or abs(d) < abs(best):
            best = d
    return float(best if best is not None else 0.0)


def _normalize_angle_deg(angle: float) -> float:
    a = float(angle)
    while a <= -90.0:
        a += 180.0
    while a > 90.0:
        a -= 180.0
    return round(a, 2)


def _angle_delta_shortest(cur: float, ref: float) -> float:
    delta = _normalize_angle_deg(cur) - _normalize_angle_deg(ref)
    while delta > 90.0:
        delta -= 180.0
    while delta < -90.0:
        delta += 180.0
    return round(delta, 2)


def _stable_box_orientation_deg(raw: float, ref: float | None) -> float:
    """Evita salti ±90° tra foto (minAreaRect ambiguo) — tiene continuità col riferimento calib."""
    if ref is None:
        return _normalize_angle_deg(raw)
    ref_n = _normalize_angle_deg(ref)
    best = _normalize_angle_deg(raw)
    best_err = abs(_angle_delta_shortest(best, ref_n))
    for adj in (90.0, -90.0, 180.0):
        cand = _normalize_angle_deg(float(raw) + adj)
        err = abs(_angle_delta_shortest(cand, ref_n))
        if err < best_err:
            best_err = err
            best = cand
    return best


def _short_side_deg_from_long(long_deg: float) -> float:
    """Lato corto = lungo + 90° (geometria rettangolo fissa)."""
    return _normalize_angle_deg(float(long_deg) + _orient_perp_offset_deg())


def _j5_servo_deg_from_short_side(short_deg: float) -> float:
    """Mappa angolo lato corto (immagine) → convenzione giunto J5."""
    return _normalize_angle_deg(_orient_sign() * float(short_deg))


def _j5_align_deg_from_box_orient(orient_deg: float) -> float:
    """Compat: short side da long, poi segno servo."""
    return _j5_servo_deg_from_short_side(_short_side_deg_from_long(orient_deg))


def _refresh_rectangle_axes(det: dict[str, Any]) -> dict[str, Any]:
    try:
        import sys

        scripts_dir = str(PROJECT_ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from box_object_detector import refresh_detection_rectangle_axes

        return refresh_detection_rectangle_axes(det)
    except Exception:
        return det


def calibration_orientation_ref_deg() -> float | None:
    data = load_preset()
    zc = data.get("zero_calibration")
    if not isinstance(zc, dict):
        return None
    if zc.get("orientation_ref_deg") is not None:
        return float(zc["orientation_ref_deg"])
    vis = zc.get("vision_at_scan")
    if isinstance(vis, dict) and vis.get("orientation_deg") is not None:
        return float(vis["orientation_deg"])
    return None


def _enrich_grip_align_overlay(det: dict[str, Any]) -> dict[str, Any]:
    """Ricalcola assi dal box: magenta=lungo, ciano=corto (pinza)."""
    if not isinstance(det, dict):
        return det
    if det.get("orientation_deg") is None:
        return det
    return _refresh_rectangle_axes(det)


def stabilize_detection_orientation(det: dict[str, Any]) -> dict[str, Any]:
    """Stabilizza asse LUNGO; ricalcola sempre CORTO = lungo + 90° dal box."""
    if not isinstance(det, dict):
        return det
    det = _refresh_rectangle_axes(det)
    raw = det.get("orientation_deg")
    if raw is None:
        return det
    ref = calibration_orientation_ref_deg()
    stable = _stable_box_orientation_deg(float(raw), ref)
    prev = load_preset().get("last_detection")
    if isinstance(prev, dict):
        prev_f = prev.get("orientation_deg_filtered")
        if prev_f is None:
            prev_f = prev.get("orientation_deg")
        if prev_f is not None:
            jump = abs(_angle_delta_shortest(stable, float(prev_f)))
            if jump > 8.0:
                alpha = _orient_smooth_alpha()
                stable = _circular_mean_deg(
                    float(prev_f),
                    stable,
                    wa=1.0 - alpha,
                    wb=alpha,
                )
    out = dict(det)
    out["orientation_deg"] = stable
    out["orientation_deg_filtered"] = stable
    if abs(stable - float(raw)) >= 0.01:
        out["orientation_deg_raw"] = round(float(raw), 2)
    return _enrich_grip_align_overlay(out)


def _vision_orientation_delta_deg(
    ref: dict[str, Any] | None,
    cur: dict[str, Any] | None,
    *,
    measure_mode: str | None = None,
    apply_cap: bool = True,
) -> float | None:
    """Δ J5 servo da angoli freccia in pixel (ref calib fisso, cur con simmetria 90°)."""
    if not _orient_enabled():
        return None
    if not ref or not cur:
        return None
    if not ref.get("detected") or not cur.get("detected"):
        return None
    ref_long = ref.get("orientation_deg")
    if ref_long is None and cur.get("orientation_deg") is None:
        return None
    ref_long_f = float(ref_long) if ref_long is not None else None
    ref_angle, _ = _pick_measure_angle_deg(
        ref,
        ref_long=ref_long_f,
        mode=measure_mode,
    )
    cur_angle, _ = _pick_measure_angle_deg(
        cur,
        ref_long=ref_long_f,
        mode=measure_mode,
    )
    if ref_angle is None or cur_angle is None:
        ref_a = ref.get("orientation_deg")
        cur_a = cur.get("orientation_deg")
        if ref_a is None or cur_a is None:
            return None
        ref_stable = _normalize_angle_deg(float(ref_a))
        cur_stable = _stable_box_orientation_deg(float(cur_a), ref_stable)
        delta = _angle_delta_shortest(
            cur_stable * _orient_sign(),
            ref_stable * _orient_sign(),
        )
    else:
        delta = _best_delta_to_ref_deg(
            ref_angle * _orient_sign(),
            cur_angle * _orient_sign(),
        )
    raw = round(delta * _orient_gain(), 2)
    if not apply_cap:
        return raw
    cap = _orient_max_delta_deg()
    if abs(raw) > cap:
        return round(cap if raw > 0 else -cap, 2)
    return raw


def _j5_target_breakdown(
    *,
    scan_j5: float,
    base_off_j5: float,
    zc: dict[str, Any] | None,
    data: dict[str, Any],
    cur_dict: dict[str, Any] | None,
) -> dict[str, Any]:
    """J5 finale: ancorato alla posa insegnata in calib + Δ visione + slider manuale."""
    j = _orient_joint_index()
    manual = float(data.get("manual_orient_offset_deg") or 0.0)
    taught_sd = zc.get("taught_servo_deg") if isinstance(zc, dict) else None
    j5_taught = (
        float(taught_sd[j])
        if isinstance(taught_sd, list) and len(taught_sd) > j
        else round(scan_j5 + base_off_j5, 3)
    )
    ref_vis = zc.get("vision_at_scan") if isinstance(zc, dict) else None
    measure_mode = zc.get("orient_measure_mode") if isinstance(zc, dict) else None
    d_vis_raw = _vision_orientation_delta_deg(
        ref_vis if isinstance(ref_vis, dict) else None,
        cur_dict,
        measure_mode=str(measure_mode) if measure_mode else None,
        apply_cap=False,
    )
    d_vis = _vision_orientation_delta_deg(
        ref_vis if isinstance(ref_vis, dict) else None,
        cur_dict,
        measure_mode=str(measure_mode) if measure_mode else None,
        apply_cap=True,
    )
    has_calib = isinstance(zc, dict) and isinstance(ref_vis, dict) and ref_vis.get("orientation_deg") is not None
    if has_calib and _orient_enabled():
        j5_final = round(j5_taught + (d_vis if d_vis is not None else 0.0) + manual, 3)
        mode = "taught_plus_vision"
    else:
        j5_final = round(scan_j5 + base_off_j5 + manual, 3)
        mode = "offset_only"
    return {
        "mode": mode,
        "scan_j5_deg": round(scan_j5, 3),
        "j5_taught_deg": round(j5_taught, 3),
        "j5_base_offset_deg": round(base_off_j5, 3),
        "vision_delta_deg": d_vis,
        "vision_delta_raw_deg": d_vis_raw,
        "orient_measure_mode": measure_mode,
        "manual_offset_deg": round(manual, 3),
        "j5_target_deg": j5_final,
        "effective_offset_deg": round(j5_final - scan_j5, 3),
    }


def _resolve_j5_target_deg(
    scan_j5: float,
    base_off_j5: float,
    *,
    zc: dict[str, Any] | None,
    data: dict[str, Any],
    cur_dict: dict[str, Any] | None,
) -> float:
    return float(
        _j5_target_breakdown(
            scan_j5=scan_j5,
            base_off_j5=base_off_j5,
            zc=zc,
            data=data,
            cur_dict=cur_dict,
        )["j5_target_deg"]
    )


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
    """Offset presa: modello teach interpolato (se attivo) oppure calib zero + visione."""
    data = load_preset()
    cur_dict = last_detection if isinstance(last_detection, dict) else data.get("last_detection")
    try:
        from go2_dashboard.d1_jog import pick_teach_model

        if pick_teach_model.model_is_active(data):
            model_off, _meta = pick_teach_model.effective_offsets_from_model(
                cur_dict if isinstance(cur_dict, dict) else None,
                data=data,
            )
            if model_off is not None:
                return model_off
    except Exception:
        pass

    off = joint_offset_deg()
    if off is None:
        return None
    out = [float(x) for x in off[:7]]
    while len(out) < 7:
        out.append(out[-1])
    zc = data.get("zero_calibration")
    if not isinstance(zc, dict):
        return out
    ref_vis = zc.get("vision_at_scan")
    ref_dict = ref_vis if isinstance(ref_vis, dict) else None
    if cur_dict is None or not isinstance(cur_dict, dict):
        cur_dict = None
    dpx = _vision_pixel_delta(ref_dict, cur_dict)
    if dpx is not None:
        k0 = _preset_tuning_float("px_to_j0_deg", "D1_PICK_PX_TO_J0_DEG", 0.04)
        k1 = _preset_tuning_float("px_to_j1_deg", "D1_PICK_PX_TO_J1_DEG", 0.035)
        k2 = _preset_tuning_float("px_to_j2_deg", "D1_PICK_PX_TO_J2_DEG", 0.015)
        out[0] = round(out[0] + dpx[0] * k0, 3)
        out[1] = round(out[1] + dpx[1] * k1, 3)
        out[2] = round(out[2] + dpx[1] * k2, 3)
        ref_norm = ref_dict.get("norm") if ref_dict else None
        cur_norm = cur_dict.get("norm") if cur_dict else None
        if (
            isinstance(ref_norm, (list, tuple))
            and isinstance(cur_norm, (list, tuple))
            and len(ref_norm) >= 2
            and len(cur_norm) >= 2
        ):
            out[0] = round(out[0] + (float(cur_norm[0]) - float(ref_norm[0])) * 8.0, 3)
            out[1] = round(out[1] + (float(cur_norm[1]) - float(ref_norm[1])) * 6.0, 3)
    j = _orient_joint_index()
    if 0 <= j < _ARM_OFFSET_JOINTS:
        scan_sd = zc.get("scan_servo_deg") if isinstance(zc, dict) else None
        scan_j5 = (
            float(scan_sd[j])
            if isinstance(scan_sd, list) and len(scan_sd) > j
            else None
        )
        if scan_j5 is not None:
            base_off_j5 = float(off[j]) if isinstance(off, list) and len(off) > j else 0.0
            out[j] = round(
                _resolve_j5_target_deg(
                    scan_j5,
                    base_off_j5,
                    zc=zc if isinstance(zc, dict) else None,
                    data=data,
                    cur_dict=cur_dict,
                )
                - scan_j5,
                3,
            )
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
    data = load_preset()
    zc = data.get("zero_calibration")
    zc_dict = zc if isinstance(zc, dict) else None
    cur_dict = last_detection if isinstance(last_detection, dict) else None
    if cur_dict is None and isinstance(data.get("last_detection"), dict):
        cur_dict = data["last_detection"]
    use_teach_model = False
    try:
        from go2_dashboard.d1_jog import pick_teach_model

        use_teach_model = pick_teach_model.model_is_active(data)
    except Exception:
        pass

    taught_off = joint_offset_deg()
    target = list(base)
    j5_idx = _orient_joint_index()
    for i in range(_ARM_OFFSET_JOINTS):
        if use_teach_model:
            target[i] = round(base[i] + float(off[i]), 3)
        elif (
            i == j5_idx
            and zc_dict is not None
            and isinstance(taught_off, list)
            and len(taught_off) > j5_idx
        ):
            target[i] = _resolve_j5_target_deg(
                float(base[i]),
                float(taught_off[j5_idx]),
                zc=zc_dict,
                data=data,
                cur_dict=cur_dict,
            )
        else:
            target[i] = round(base[i] + float(off[i]), 3)
    target[_GRIPPER_JOINT_INDEX] = round(base[_GRIPPER_JOINT_INDEX], 3)
    return service.clamp_servo_deg(target)


def _gripper_deg_from_env(name: str) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def scan_gripper_j6_deg(scan_servo_deg: list[float]) -> float:
    """J6 nel waypoint SCANSIONE (teach) — su D1 spesso ≈ chiusa, non aperta."""
    sd = [float(x) for x in scan_servo_deg[:7]]
    while len(sd) < 7:
        sd.append(sd[-1] if sd else 0.0)
    return float(sd[6])


def gripper_open_j6_deg(scan_servo_deg: list[float] | None = None, **_kw: Any) -> float:
    """J6 pinza aperta — esplicito in env (Gemini/D1: ~49.7°, non il valore scansione ~5°)."""
    explicit = _gripper_deg_from_env("D1_GRIPPER_OPEN_DEG")
    if explicit is not None:
        return explicit
    if scan_servo_deg is not None:
        scan_j = scan_gripper_j6_deg(scan_servo_deg)
        closed = _gripper_deg_from_env("D1_GRIPPER_CLOSED_DEG")
        if closed is not None and abs(scan_j - closed) > 2.0:
            return scan_j
        return 90.0 if scan_j < 45.0 else 0.0
    return 49.7


def gripper_close_j6_deg(scan_servo_deg: list[float] | None = None, **_kw: Any) -> float:
    """J6 pinza chiusa — esplicito in env (su D1 test: ~5° da waypoint scansione)."""
    explicit = _gripper_deg_from_env("D1_GRIPPER_CLOSED_DEG")
    if explicit is not None:
        return explicit
    if scan_servo_deg is not None:
        return scan_gripper_j6_deg(scan_servo_deg)
    return 5.0


def grasp_servo_approach_from_scan(
    scan_servo_deg: list[float],
    *,
    offsets: list[float] | None = None,
    last_detection: dict[str, Any] | None = None,
) -> list[float] | None:
    """Posa presa per avvicinamento: braccio su offset, pinza ancora aperta (J6 da scansione)."""
    target = grasp_servo_from_scan(
        scan_servo_deg,
        offsets=offsets,
        last_detection=last_detection,
    )
    if target is None:
        return None
    target[_GRIPPER_JOINT_INDEX] = round(gripper_open_j6_deg(scan_servo_deg), 3)
    return service.clamp_servo_deg(target)


def preset_info() -> dict[str, Any]:
    data = load_preset()
    off = joint_offset_deg()
    scan = program_store.find_scan_waypoint()
    try:
        from go2_dashboard.d1_jog import pick_teach_model

        teach_info = pick_teach_model.list_teach_samples()
    except Exception:
        teach_info = {"count": 0, "has_active_model": False}

    out: dict[str, Any] = {
        "ok": True,
        "preset_path": str(_PRESET_PATH),
        "has_preset": off is not None,
        "joint_offset_deg": off,
        "updated_at": data.get("updated_at"),
        "last_detection": data.get("last_detection"),
        "source": data.get("source"),
        "teach_samples_count": teach_info.get("count", 0),
        "has_teach_model": bool(teach_info.get("has_active_model")),
        "teach_model": teach_info.get("teach_model"),
        "tuning": tuning_info(),
    }
    if teach_info.get("has_active_model") and isinstance(data.get("last_detection"), dict):
        model_off, blend = pick_teach_model.effective_offsets_from_model(data["last_detection"], data=data)
        if model_off is not None:
            out["teach_model_offset_deg"] = model_off
            out["teach_model_blend"] = blend
            if isinstance(blend, dict) and blend.get("method"):
                out["teach_interp_method"] = blend.get("method")
            if isinstance(blend, dict) and blend.get("nearest_id"):
                out["teach_nearest_id"] = blend.get("nearest_id")
                out["teach_nearest_distance"] = blend.get("nearest_distance")
            if isinstance(blend, dict) and blend.get("j5_blend"):
                out["teach_j5_blend"] = blend.get("j5_blend")
                out["teach_j5_interp"] = blend.get("j5_interp")
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
        d_orient = _vision_orientation_delta_deg(
            ref_vis if isinstance(ref_vis, dict) else None,
            ld if isinstance(ld, dict) else None,
            measure_mode=str(zc.get("orient_measure_mode")) if zc.get("orient_measure_mode") else None,
        )
        if d_orient is not None:
            out["vision_orientation_delta_deg"] = d_orient
            out["orient_joint_index"] = _orient_joint_index()
            out["orient_gain"] = _orient_gain()
        if data.get("manual_orient_offset_deg") is not None:
            out["manual_orient_offset_deg"] = float(data["manual_orient_offset_deg"])
        if zc.get("orientation_ref_deg") is not None:
            out["orient_ref_deg"] = float(zc["orientation_ref_deg"])
        elif isinstance(ref_vis, dict) and ref_vis.get("orientation_deg") is not None:
            out["orient_ref_deg"] = float(ref_vis["orientation_deg"])
        if isinstance(ld, dict) and ld.get("orientation_deg") is not None:
            out["orient_cur_deg"] = float(ld["orientation_deg"])
        if zc.get("grip_align_ref_deg") is not None:
            out["grip_align_ref_deg"] = float(zc["grip_align_ref_deg"])
        if isinstance(ld, dict) and ld.get("grip_align_deg") is not None:
            out["grip_align_cur_deg"] = float(ld["grip_align_deg"])
        scan_sd = zc.get("scan_servo_deg")
        if isinstance(scan_sd, list) and len(scan_sd) > _orient_joint_index():
            base_off = off if isinstance(off, list) else joint_offset_deg()
            base_j5 = (
                float(base_off[_orient_joint_index()])
                if isinstance(base_off, list) and len(base_off) > _orient_joint_index()
                else 0.0
            )
            out["j5_breakdown"] = _j5_target_breakdown(
                scan_j5=float(scan_sd[_orient_joint_index()]),
                base_off_j5=base_j5,
                zc=zc,
                data=data,
                cur_dict=ld if isinstance(ld, dict) else None,
            )
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


def tuning_info() -> dict[str, Any]:
    data = load_preset()
    tuning = data.get("tuning") if isinstance(data.get("tuning"), dict) else {}
    out = {
        "ok": True,
        "preset_path": str(_PRESET_PATH),
        "orient_gain": _orient_gain(),
        "orient_max_delta_deg": _orient_max_delta_deg(),
        "orient_smooth_alpha": _orient_smooth_alpha(),
        "orient_perp_offset_deg": _orient_perp_offset_deg(),
        "px_to_j0_deg": _preset_tuning_float("px_to_j0_deg", "D1_PICK_PX_TO_J0_DEG", 0.04),
        "px_to_j1_deg": _preset_tuning_float("px_to_j1_deg", "D1_PICK_PX_TO_J1_DEG", 0.035),
        "px_to_j2_deg": _preset_tuning_float("px_to_j2_deg", "D1_PICK_PX_TO_J2_DEG", 0.015),
        "orient_joint_index": _orient_joint_index(),
        "orient_enabled": _orient_enabled(),
        "orient_sign": _orient_sign(),
        "saved": tuning,
    }
    return out


def _normalize_vision_ref(vis: dict[str, Any] | None) -> dict[str, Any] | None:
    """Riferimento visione alla foto di calibrazione (centro + rotazione pezzo)."""
    if not isinstance(vis, dict):
        return None
    out = dict(vis)
    if out.get("orientation_deg") is not None or out.get("grip_center_px"):
        out["detected"] = True
    elif "detected" not in out:
        out["detected"] = bool(out.get("label") or out.get("backend"))
    return out


def _couple_and_hold_taught_pose(taught_servo_deg: list[float]) -> dict[str, Any]:
    """Coppia ON e mantiene la posa insegnata (non salta a scan/zero)."""
    import time

    taught = service.clamp_servo_deg([float(x) for x in taught_servo_deg[:7]])
    couple = service.ensure_coupled(with_power=True, force=True)
    if not couple.get("ok") and not couple.get("skipped"):
        return {"ok": False, "reason": "couple_failed", "coupling": couple}
    settle = float(os.environ.get("D1_PICK_CALIB_COUPLE_SETTLE_S", "0.8"))
    if settle > 0:
        time.sleep(settle)
    hold_repeats = max(6, int(os.environ.get("D1_ZERO_HOLD_REPEATS", "8")))
    hold_delay = max(35, int(os.environ.get("D1_ZERO_HOLD_DELAY_MS", "55")))
    hold = service.hold_current_pose(
        servo_deg=taught,
        repeats=hold_repeats,
        delay_ms=hold_delay,
    )
    return {
        "ok": bool(hold.get("ok") or hold.get("skipped")),
        "coupling": couple,
        "hold": hold,
        "taught_servo_deg": taught,
    }


def finish_zero_calibration_after_release(
    *,
    vision_at_scan: dict[str, Any] | None = None,
    taught_servo_deg: list[float] | None = None,
) -> dict[str, Any]:
    """Dopo teach in release: leggi posa manuale, salva riferimento, poi coppia su quella posa."""
    taught: list[float] | None = None
    if isinstance(taught_servo_deg, list) and len(taught_servo_deg) >= 6:
        taught = service.clamp_servo_deg([float(x) for x in taught_servo_deg[:7]])
    else:
        fb = service.read_servo_deg(fast=False)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return {"ok": False, "reason": "no_feedback_in_release", "feedback": fb}
        taught = service.clamp_servo_deg(list(fb["servo_deg"]))

    out = save_zero_calibration(taught, vision_at_scan=vision_at_scan)
    if not out.get("ok"):
        return out

    hold_out = _couple_and_hold_taught_pose(taught)
    out["coupling"] = hold_out.get("coupling")
    out["hold"] = hold_out.get("hold")
    out["taught_servo_deg"] = taught
    out["servo_from_feedback"] = taught_servo_deg is None
    out["ok"] = True
    if not hold_out.get("ok"):
        out["coupling_warning"] = hold_out.get("reason", "hold_after_calib_failed")
        out["hint_it"] = (
            (out.get("hint_it") or "")
            + " Offset salvato; coppia/hold dopo calibrazione fallito — verifica coppia manuale."
        ).strip()
    return out


def save_zero_calibration(
    current_servo_deg: list[float],
    *,
    vision_at_scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Presa zero: offset insegnato a mano + riferimento visione (centro + rotazione pezzo)."""
    taught = compute_offsets_from_current_vs_scan(current_servo_deg)
    if not taught.get("ok"):
        return taught
    data = load_preset()
    raw_vis = vision_at_scan if isinstance(vision_at_scan, dict) else data.get("last_detection")
    vis = _normalize_vision_ref(raw_vis if isinstance(raw_vis, dict) else None)
    if isinstance(vis, dict):
        vis = stabilize_detection_orientation(vis)
    off = taught.get("joint_offset_deg") or []
    j5_off = float(off[5]) if isinstance(off, list) and len(off) > 5 else None
    orient_ref = vis.get("orientation_deg") if isinstance(vis, dict) else None
    if orient_ref is not None:
        orient_ref = _normalize_angle_deg(float(orient_ref))
    grip_align_ref = (
        float(vis.get("grip_align_deg"))
        if isinstance(vis, dict) and vis.get("grip_align_deg") is not None
        else (_short_side_deg_from_long(orient_ref) if orient_ref is not None else None)
    )
    short_ref = (
        vis.get("grip_align_deg")
        if isinstance(vis, dict) and vis.get("grip_align_deg") is not None
        else (_short_side_deg_from_long(orient_ref) if orient_ref is not None else None)
    )
    measure_mode = "short_side"
    measure_ref = round(float(short_ref), 2) if short_ref is not None else None
    data["zero_calibration"] = {
        "at": _now_iso(),
        "vision_at_scan": vis,
        "orientation_ref_deg": orient_ref,
        "grip_align_ref_deg": grip_align_ref,
        "orient_measure_mode": measure_mode,
        "orient_measure_ref_deg": measure_ref,
        "short_side_ref_deg": measure_ref,
        "grip_ref_px": vis.get("grip_center_px") if isinstance(vis, dict) else None,
        "joint_offset_deg": taught.get("joint_offset_deg"),
        "j5_offset_deg": j5_off,
        "scan_servo_deg": taught.get("scan_servo_deg"),
        "taught_servo_deg": taught.get("taught_servo_deg"),
        "scan_waypoint": taught.get("scan_waypoint"),
    }
    data["source"] = "zero_calibration"
    data["joint_offset_deg"] = taught.get("joint_offset_deg")
    if orient_ref is not None:
        data["manual_orient_offset_deg"] = 0.0
    save_preset(data)
    info = preset_info()
    info["ok"] = True
    info["zero_calibration"] = data["zero_calibration"]
    hints = [
        "Presa zero salvata — le prossime foto aggiornano posizione (px) e rotazione pezzo → J5.",
        "Flusso: Scansione +90° → Foto → Vai verso oggetto (pinza aperta) → Chiudi pinza.",
    ]
    if orient_ref is None:
        hints.insert(
            0,
            "Attenzione: rotazione pezzo non rilevata sulla foto calibrazione — rifai foto con blue box visibile.",
        )
    info["hint_it"] = " ".join(hints)
    return info


def compute_offsets_from_current_vs_scan(current_servo_deg: list[float]) -> dict[str, Any]:
    """Δ giunti = posa attuale − waypoint SCANSIONE (senza salvare preset)."""
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
    delta = _sanitize_arm_offsets([round(cur[i] - base[i], 3) for i in range(7)])
    return {
        "ok": True,
        "scan_waypoint": scan_wp.get("name"),
        "scan_servo_deg": base,
        "taught_servo_deg": cur,
        "joint_offset_deg": delta,
    }


def offsets_from_current_vs_scan(current_servo_deg: list[float]) -> dict[str, Any]:
    """Δ giunti = posa attuale − waypoint SCANSIONE (calibrazione teach)."""
    info = compute_offsets_from_current_vs_scan(current_servo_deg)
    if not info.get("ok"):
        return info
    saved = set_offsets(info["joint_offset_deg"], source="teach_pose_vs_scan")
    info.update(saved)
    info["ok"] = True
    return info


def nudge_offsets(
    *,
    joint_index: int,
    delta_deg: float,
) -> dict[str, Any]:
    """Aggiunge delta_deg all'offset di un giunto (calibrazione fine)."""
    idx = int(joint_index)
    if idx < 0 or idx >= _ARM_OFFSET_JOINTS:
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


def set_manual_orient_offset_deg(delta_deg: float) -> dict[str, Any]:
    data = load_preset()
    data["manual_orient_offset_deg"] = round(float(delta_deg), 3)
    save_preset(data)
    info = preset_info()
    info["ok"] = True
    return info


def set_tuning(updates: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "orient_gain": float,
        "orient_max_delta_deg": float,
        "orient_smooth_alpha": float,
        "orient_perp_offset_deg": float,
        "px_to_j0_deg": float,
        "px_to_j1_deg": float,
        "px_to_j2_deg": float,
    }
    data = load_preset()
    tuning = data.get("tuning") if isinstance(data.get("tuning"), dict) else {}
    tuning = dict(tuning)
    changed: dict[str, Any] = {}
    for key, caster in keys.items():
        if key not in updates:
            continue
        try:
            val = caster(updates[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key}_invalid")
        if key == "orient_smooth_alpha":
            val = min(1.0, max(0.05, float(val)))
        elif key == "orient_max_delta_deg":
            val = max(1.0, float(val))
        tuning[key] = round(float(val), 4)
        changed[key] = tuning[key]
    if not changed:
        return tuning_info()
    data["tuning"] = tuning
    save_preset(data)
    out = tuning_info()
    out["ok"] = True
    out["updated"] = changed
    return out


def set_offsets(
    joint_offset_deg_list: list[float],
    *,
    source: str = "manual",
    last_detection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    off = _sanitize_arm_offsets([float(x) for x in joint_offset_deg_list[:7]])
    data = load_preset()
    data["joint_offset_deg"] = off
    data["source"] = source
    if last_detection is not None:
        data["last_detection"] = stabilize_detection_orientation(last_detection)
    save_preset(data)
    return preset_info()
