"""Libreria teach multipla + modello interpolato visione → offset giunti."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from go2_dashboard.d1_jog import pick_preset, program_store, service

GUIDED_SCENARIOS = (
    "center", "left", "right", "upper", "lower", "rotate_cw", "rotate_ccw", "corner",
)


def _append_history(data: dict[str, Any], event: str, **payload: Any) -> None:
    rows = data.get("teach_history")
    if not isinstance(rows, list):
        rows = []
    rows.append({"at": pick_preset._now_iso(), "event": event, **payload})
    data["teach_history"] = rows[-200:]


def _vision_age_s(vis: dict[str, Any] | None) -> float | None:
    if not isinstance(vis, dict) or not vis.get("at"):
        return None
    try:
        captured = time.mktime(time.strptime(str(vis["at"]), "%Y-%m-%dT%H:%M:%S"))
        return max(0.0, time.time() - captured)
    except (TypeError, ValueError):
        return None


def _feature_weights() -> dict[str, float]:
    return {
        "norm_x": float(os.environ.get("D1_PICK_TEACH_W_NORM_X", "12.0")),
        "norm_y": float(os.environ.get("D1_PICK_TEACH_W_NORM_Y", "12.0")),
        "short_deg": float(os.environ.get("D1_PICK_TEACH_W_SHORT_DEG", "0.35")),
        "px_x": float(os.environ.get("D1_PICK_TEACH_W_PX_X", "0.02")),
        "px_y": float(os.environ.get("D1_PICK_TEACH_W_PX_Y", "0.02")),
    }


def _vision_features(det: dict[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(det, dict) or not det.get("detected"):
        return None
    norm = det.get("norm")
    gpx = det.get("grip_center_px")
    short_deg = det.get("grip_align_deg")
    if short_deg is None and det.get("orientation_deg") is not None:
        short_deg = pick_preset._short_side_deg_from_long(float(det["orientation_deg"]))
    out: dict[str, float] = {}
    if isinstance(norm, (list, tuple)) and len(norm) >= 2:
        out["norm_x"] = float(norm[0])
        out["norm_y"] = float(norm[1])
    if isinstance(gpx, (list, tuple)) and len(gpx) >= 2:
        out["px_x"] = float(gpx[0])
        out["px_y"] = float(gpx[1])
    if short_deg is not None:
        out["short_deg"] = float(short_deg)
    return out if out else None


_POSITION_KEYS = ("norm_x", "norm_y", "px_x", "px_y")


def _position_weights() -> dict[str, float]:
    w = _feature_weights()
    return {k: w[k] for k in _POSITION_KEYS if k in w}


def _position_distance(a: dict[str, float], b: dict[str, float]) -> float:
    w = _position_weights()
    d2 = 0.0
    for key, wt in w.items():
        if key in a and key in b:
            d2 += wt * (float(a[key]) - float(b[key])) ** 2
    return d2 ** 0.5


def _sample_position_features(sample: dict[str, Any]) -> dict[str, float] | None:
    cached = sample.get("vision_features")
    if isinstance(cached, dict):
        out = {k: float(cached[k]) for k in _POSITION_KEYS if k in cached}
        if out:
            return out
    full = _vision_features(sample.get("vision_at_scan") if isinstance(sample, dict) else None)
    if not full:
        return None
    return {k: full[k] for k in _POSITION_KEYS if k in full}


def _nearest_sample_by_position(
    cur: dict[str, float],
    samples: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float, int]:
    best: dict[str, Any] | None = None
    best_d = 1e9
    best_i = -1
    pos_cur = {k: cur[k] for k in _POSITION_KEYS if k in cur}
    for i, s in enumerate(samples):
        ref = _sample_position_features(s)
        if not ref:
            continue
        d = _position_distance(pos_cur, ref)
        if d < best_d:
            best_d, best, best_i = d, s, i
    return best, best_d, best_i


def _scan_j5_from_samples(samples: list[dict[str, Any]]) -> float:
    for s in samples:
        sd = s.get("scan_servo_deg")
        if isinstance(sd, list) and len(sd) > 5:
            return float(sd[5])
    found = program_store.find_scan_waypoint()
    if found:
        _pid, wp = found
        sd = wp.get("servo_deg")
        if isinstance(sd, list) and len(sd) > 5:
            return float(sd[5])
    return 0.0


def _j5_base_offset(sample: dict[str, Any]) -> float | None:
    j = pick_preset._orient_joint_index()
    off = sample.get("joint_offset_deg")
    if isinstance(off, list) and len(off) > j:
        return float(off[j])
    return None


def _orient_delta_for_teach(
    sample: dict[str, Any],
    cur_det: dict[str, Any] | None,
) -> float | None:
    """Quanto è ruotato il pezzo adesso rispetto alla foto di quel teach."""
    ref_vis = sample.get("vision_at_scan")
    if not isinstance(ref_vis, dict):
        return None
    return pick_preset._vision_orientation_delta_deg(
        ref_vis,
        cur_det if isinstance(cur_det, dict) else None,
        apply_cap=False,
    )


def _j5_corrected_offset(
    sample: dict[str, Any],
    cur_det: dict[str, Any] | None,
) -> float | None:
    """J5 = offset al teach + Δ rotazione pezzo (non sostituisce la rotazione insegnata)."""
    base = _j5_base_offset(sample)
    if base is None:
        return None
    d_vis = _orient_delta_for_teach(sample, cur_det)
    if d_vis is None:
        return round(base, 3)
    return round(base + float(d_vis), 3)


def _j5_eligible_indices(
    dists: list[float],
    *,
    nn_dist: float,
) -> list[int]:
    """Solo teach vicini in posizione influenzano J5 (rotazione locale per teach)."""
    margin = float(os.environ.get("D1_PICK_TEACH_J5_POS_MARGIN", "2.5"))
    nn_max = float(os.environ.get("D1_PICK_TEACH_NN_MAX", "7.5"))
    cutoff = min(nn_max, max(margin, float(nn_dist) + margin))
    eligible = [i for i, d in enumerate(dists) if float(d) <= cutoff]
    if eligible:
        return eligible
    if not dists:
        return []
    best_i = min(range(len(dists)), key=lambda i: float(dists[i]))
    return [best_i]


def _blend_j5_orientation_offset(
    samples: list[dict[str, Any]],
    weights: list[float],
    w_sum: float,
    cur_det: dict[str, Any] | None,
    *,
    dists: list[float],
    nn_dist: float,
    data: dict[str, Any] | None = None,
) -> tuple[float, list[dict[str, Any]]]:
    """Media pesata (solo teach vicini) di: offset J5 al teach + Δ rotazione pezzo vs quel teach."""
    eligible = _j5_eligible_indices(dists, nn_dist=nn_dist)
    j5_w = [weights[i] if i in eligible else 0.0 for i in range(len(samples))]
    j5_w_sum = sum(j5_w)
    if j5_w_sum < 1e-9:
        j5_w_sum = w_sum
        j5_w = list(weights)

    base_sum = 0.0
    delta_sum = 0.0
    used: list[dict[str, Any]] = []
    for i, s in enumerate(samples):
        w = j5_w[i]
        if w <= 0:
            continue
        base = _j5_base_offset(s)
        if base is None:
            continue
        d_vis = _orient_delta_for_teach(s, cur_det) or 0.0
        base_sum += w * base
        delta_sum += w * float(d_vis)
        used.append(
            {
                "id": s.get("id"),
                "weight": round(w / j5_w_sum, 4),
                "position_dist": dists[i] if i < len(dists) else None,
                "j5_base_offset_deg": round(base, 3),
                "orient_delta_deg": round(float(d_vis), 3),
                "j5_corrected_offset_deg": round(base + float(d_vis), 3),
            }
        )
    manual = float((data or {}).get("manual_orient_offset_deg") or 0.0)
    if j5_w_sum > 0:
        j5_blended = round(base_sum / j5_w_sum + delta_sum / j5_w_sum + manual, 3)
    else:
        j5_blended = manual
    return j5_blended, used


def _position_weights_for_samples(
    cur: dict[str, float],
    samples: list[dict[str, Any]],
    *,
    idw_power: float,
) -> tuple[list[float], list[float], float]:
    weights: list[float] = []
    dists: list[float] = []
    pos_cur = {k: cur[k] for k in _POSITION_KEYS if k in cur}
    for s in samples:
        ref = _sample_position_features(s)
        if ref is None:
            weights.append(0.0)
            dists.append(1e6)
            continue
        d = _position_distance(pos_cur, ref)
        dists.append(round(d, 4))
        weights.append(1.0 / (d ** float(idw_power) + 1e-4))
    w_sum = sum(weights)
    return weights, dists, w_sum


def _centroid_features(samples: list[dict[str, Any]]) -> dict[str, float] | None:
    feats = []
    for s in samples:
        f = _vision_features(s.get("vision_at_scan") if isinstance(s, dict) else None)
        if f:
            feats.append(f)
    if not feats:
        return None
    keys = set()
    for f in feats:
        keys.update(f.keys())
    return {k: sum(f[k] for f in feats if k in f) / len(feats) for k in keys}


def interpolate_joint_offsets(
    cur_det: dict[str, Any] | None,
    samples: list[dict[str, Any]],
    *,
    idw_power: float = 2.0,
    data: dict[str, Any] | None = None,
) -> tuple[list[float] | None, dict[str, Any]]:
    """Posizione: IDW (o NN su J0–J4). Rotazione J5: offset teach + Δ pezzo, interpolati."""
    meta: dict[str, Any] = {"method": "idw_position_orient", "n_samples": len(samples)}
    if not samples:
        return None, {**meta, "reason": "no_samples"}

    j5_idx = pick_preset._orient_joint_index()
    cur = _vision_features(cur_det)
    if cur is None:
        off = samples[-1].get("joint_offset_deg")
        if isinstance(off, list) and len(off) >= 6:
            meta["fallback"] = "last_sample_no_detection"
            out = pick_preset._sanitize_arm_offsets([float(x) for x in off[:7]])
            if 0 <= j5_idx < 6:
                corrected = _j5_corrected_offset(samples[-1], cur_det)
                if corrected is not None:
                    out[j5_idx] = corrected
            return out, meta
        return None, {**meta, "reason": "no_current_vision"}

    weights, dists, w_sum = _position_weights_for_samples(cur, samples, idw_power=idw_power)
    if w_sum < 1e-9:
        return None, {**meta, "reason": "zero_weight"}

    nearest, nn_dist, nn_i = _nearest_sample_by_position(cur, samples)
    nn_max = float(os.environ.get("D1_PICK_TEACH_NN_MAX", "7.5"))
    min_pos_dist = min(float(d) for d in dists) if dists else 0.0
    nn_dist_val = float(nn_dist if nearest is not None else min_pos_dist)
    meta["nearest_distance"] = round(nn_dist_val, 4)
    if nearest:
        meta["nearest_id"] = nearest.get("id")
        meta["nearest_index"] = nn_i

    off_sum = [0.0] * 6
    used: list[dict[str, Any]] = []
    use_nn_spatial = nearest is not None and nn_dist <= nn_max

    if use_nn_spatial:
        raw = nearest.get("joint_offset_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return None, {**meta, "reason": "nearest_invalid"}
        for j in range(6):
            if j != j5_idx:
                off_sum[j] = float(raw[j])
        meta["method"] = "nn_position_j5_orient_blend"
        used.append(
            {"id": nearest.get("id"), "weight": 1.0, "distance": round(nn_dist, 4), "role": "spatial_nn"}
        )
    else:
        for i, s in enumerate(samples):
            w = weights[i]
            if w <= 0:
                continue
            off = s.get("joint_offset_deg")
            if not isinstance(off, list) or len(off) < 6:
                continue
            for j in range(6):
                if j == j5_idx:
                    continue
                off_sum[j] += w * float(off[j])
            used.append(
                {
                    "id": s.get("id"),
                    "weight": round(w / w_sum, 4),
                    "distance": dists[i],
                    "role": "spatial_idw",
                }
            )
        off_sum = [round(v / w_sum, 3) for v in off_sum]

    j5_blended, j5_used = _blend_j5_orientation_offset(
        samples, weights, w_sum, cur_det, dists=dists, nn_dist=nn_dist_val, data=data
    )
    off_sum[j5_idx] = j5_blended
    out = pick_preset._sanitize_arm_offsets(off_sum + [0.0])
    meta["weights"] = used
    meta["j5_blend"] = j5_used
    meta["j5_interp"] = "offset_plus_orient_delta_idw"
    meta["cur_features"] = {k: round(v, 4) for k, v in cur.items()}
    return out, meta


def guided_quality_report(samples: list[dict[str, Any]] | None) -> dict[str, Any]:
    rows = [row for row in (samples or []) if isinstance(row, dict)]
    valid = [row for row in rows if _vision_features(row.get("vision_at_scan")) is not None]
    covered = {str(row.get("scenario")) for row in valid if row.get("scenario") in GUIDED_SCENARIOS}
    features = [_vision_features(row.get("vision_at_scan")) for row in valid]
    features = [row for row in features if row]
    xs = [float(row["norm_x"]) for row in features if "norm_x" in row]
    ys = [float(row["norm_y"]) for row in features if "norm_y" in row]
    return {
        "ready": all(name in covered for name in GUIDED_SCENARIOS),
        "required_scenarios": list(GUIDED_SCENARIOS),
        "covered_scenarios": [name for name in GUIDED_SCENARIOS if name in covered],
        "missing_scenarios": [name for name in GUIDED_SCENARIOS if name not in covered],
        "guided_count": len(covered),
        "valid_detection_count": len(valid),
        "position_span_norm": {
            "x": round(max(xs) - min(xs), 4) if len(xs) >= 2 else 0.0,
            "y": round(max(ys) - min(ys), 4) if len(ys) >= 2 else 0.0,
        },
    }


def validate_guided_scenario(
    scenario: str,
    vis: dict[str, Any],
    samples: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    cur = _vision_features(vis)
    if cur is None or "norm_x" not in cur or "norm_y" not in cur:
        return {"ok": False, "reason": "2d_position_features_missing"}
    if scenario == "center":
        return {"ok": True}
    center_sample = next(
        (row for row in (samples or []) if isinstance(row, dict) and row.get("scenario") == "center"),
        None,
    )
    center = _vision_features(center_sample.get("vision_at_scan")) if center_sample else None
    if center is None:
        return {"ok": False, "reason": "center_scenario_required_first"}
    dx = float(cur["norm_x"]) - float(center["norm_x"])
    dy = float(cur["norm_y"]) - float(center["norm_y"])
    pos_margin = float(os.environ.get("D1_PICK_TEACH_SCENARIO_MARGIN_NORM", "0.08"))
    if scenario == "left" and dx > -pos_margin:
        return {"ok": False, "reason": "move_box_further_left", "delta_norm": [dx, dy]}
    if scenario == "right" and dx < pos_margin:
        return {"ok": False, "reason": "move_box_further_right", "delta_norm": [dx, dy]}
    if scenario == "upper" and dy > -pos_margin:
        return {"ok": False, "reason": "move_box_further_up", "delta_norm": [dx, dy]}
    if scenario == "lower" and dy < pos_margin:
        return {"ok": False, "reason": "move_box_further_down", "delta_norm": [dx, dy]}
    if scenario == "corner" and (abs(dx) < pos_margin or abs(dy) < pos_margin):
        return {"ok": False, "reason": "move_box_to_roi_corner", "delta_norm": [dx, dy]}
    if scenario in {"rotate_cw", "rotate_ccw"}:
        ref_a = center.get("short_deg")
        cur_a = cur.get("short_deg")
        if ref_a is None or cur_a is None:
            return {"ok": False, "reason": "2d_orientation_features_missing"}
        delta = ((float(cur_a) - float(ref_a) + 90.0) % 180.0) - 90.0
        min_angle = float(os.environ.get("D1_PICK_TEACH_SCENARIO_ROT_DEG", "15"))
        if scenario == "rotate_cw" and delta < min_angle:
            return {"ok": False, "reason": "rotate_box_more_clockwise", "delta_angle_deg": delta}
        if scenario == "rotate_ccw" and delta > -min_angle:
            return {"ok": False, "reason": "rotate_box_more_counterclockwise", "delta_angle_deg": delta}
    return {"ok": True, "delta_norm": [round(dx, 4), round(dy, 4)]}


def list_teach_samples() -> dict[str, Any]:
    data = pick_preset.load_preset()
    samples = data.get("teach_samples")
    if not isinstance(samples, list):
        samples = []
    model = data.get("teach_model") if isinstance(data.get("teach_model"), dict) else None
    return {
        "ok": True,
        "samples": samples,
        "count": len(samples),
        "teach_model": model,
        "has_active_model": bool(model and model.get("active")),
        "quality": guided_quality_report(samples),
        "history": [row for row in (data.get("teach_history") or [])[-40:] if isinstance(row, dict)],
    }


def reset_guided_session() -> dict[str, Any]:
    data = pick_preset.load_preset()
    previous = data.get("teach_samples")
    previous_count = len(previous) if isinstance(previous, list) else 0
    data["teach_samples"] = []
    model = data.get("teach_model")
    if isinstance(model, dict):
        model["active"] = False
        model["invalidated_at"] = pick_preset._now_iso()
        model["invalidated_reason"] = "guided_session_reset"
        data["teach_model"] = model
    session_id = f"teach_session_{int(time.time())}"
    data["teach_session"] = {"id": session_id, "started_at": pick_preset._now_iso()}
    _append_history(data, "session_reset", previous_count=previous_count, session_id=session_id)
    pick_preset.save_preset(data)
    return {**list_teach_samples(), "session_id": session_id}


def add_teach_sample(
    current_servo_deg: list[float],
    *,
    vision_at_scan: dict[str, Any] | None = None,
    hold_out: dict[str, Any] | None = None,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Aggiunge un esempio teach (non sostituisce la calib zero singola)."""
    taught = pick_preset.compute_offsets_from_current_vs_scan(current_servo_deg)
    if not taught.get("ok"):
        return taught

    data = pick_preset.load_preset()
    samples = data.get("teach_samples")
    if not isinstance(samples, list):
        samples = []

    raw_vis = vision_at_scan if isinstance(vision_at_scan, dict) else data.get("last_detection")
    vis = pick_preset._normalize_vision_ref(raw_vis if isinstance(raw_vis, dict) else None)
    if isinstance(vis, dict):
        vis = pick_preset.stabilize_detection_orientation(vis)

    sample_id = f"teach_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    orient_ref = vis.get("orientation_deg") if isinstance(vis, dict) else None
    if orient_ref is not None:
        orient_ref = pick_preset._normalize_angle_deg(float(orient_ref))
    short_ref = (
        float(vis.get("grip_align_deg"))
        if isinstance(vis, dict) and vis.get("grip_align_deg") is not None
        else (
            pick_preset._short_side_deg_from_long(orient_ref)
            if orient_ref is not None
            else None
        )
    )

    entry: dict[str, Any] = {
        "id": sample_id,
        "at": pick_preset._now_iso(),
        "vision_at_scan": vis,
        "vision_features": _vision_features(vis),
        "orientation_ref_deg": orient_ref,
        "short_side_ref_deg": short_ref,
        "joint_offset_deg": taught.get("joint_offset_deg"),
        "taught_servo_deg": taught.get("taught_servo_deg"),
        "scan_servo_deg": taught.get("scan_servo_deg"),
        "scan_waypoint": taught.get("scan_waypoint"),
        "scenario": scenario if scenario in GUIDED_SCENARIOS else None,
    }
    samples.append(entry)
    data["teach_samples"] = samples
    if isinstance(data.get("teach_model"), dict):
        data["teach_model"]["active"] = False
        data["teach_model"]["invalidated_at"] = pick_preset._now_iso()
        data["teach_model"]["invalidated_reason"] = "new_sample_added"
    _append_history(
        data,
        "sample_saved",
        sample_id=sample_id,
        scenario=entry.get("scenario"),
        detection_ok=bool(vis and vis.get("detected")),
    )
    pick_preset.save_preset(data)

    if hold_out is None:
        hold_out = pick_preset._couple_and_hold_taught_pose(
            taught.get("taught_servo_deg") or current_servo_deg
        )
    out = list_teach_samples()
    out["ok"] = True
    out["sample"] = entry
    out["coupling"] = hold_out.get("coupling")
    out["hold"] = hold_out.get("hold")
    out["hint_it"] = (
        f"Teach #{len(samples)} salvato. Sposta il pezzo, torna a Scansione +90° e aggiungi altri esempi. "
        "Poi «Crea modello teach»."
    )
    if not vis or not vis.get("detected"):
        out["warning_it"] = "Foto senza rilevamento — esempio salvato ma considera di eliminarlo."
    return out


def finish_teach_sample_after_release(
    *,
    vision_at_scan: dict[str, Any] | None = None,
    taught_servo_deg: list[float] | None = None,
    scenario: str | None = None,
    require_valid_vision: bool = False,
) -> dict[str, Any]:
    def fail(reason: str, **payload: Any) -> dict[str, Any]:
        data = pick_preset.load_preset()
        _append_history(data, "sample_failed", reason=reason, scenario=scenario)
        pick_preset.save_preset(data)
        payload.pop("ok", None)
        payload.pop("reason", None)
        return {"ok": False, "reason": reason, **payload}

    taught: list[float] | None = None
    if isinstance(taught_servo_deg, list) and len(taught_servo_deg) >= 6:
        taught = service.clamp_servo_deg([float(x) for x in taught_servo_deg[:7]])
    else:
        fb = service.read_servo_deg(fast=False)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return {"ok": False, "reason": "no_feedback_in_release", "feedback": fb}
        taught = service.clamp_servo_deg(list(fb["servo_deg"]))
    # Safety first: in release riattiva coppia/HOLD prima di qualsiasi write.
    # Se il processo si interrompe durante il salvataggio, il braccio resta
    # comunque sostenuto dal publisher esterno già esistente.
    hold_out = pick_preset._couple_and_hold_taught_pose(taught)
    if not hold_out.get("ok"):
        return fail("hold_before_teach_persist_failed", **hold_out)
    vis = vision_at_scan if isinstance(vision_at_scan, dict) else pick_preset.load_preset().get("last_detection")
    if require_valid_vision:
        max_age = float(os.environ.get("D1_PICK_TEACH_MAX_VISION_AGE_S", "300"))
        age = _vision_age_s(vis if isinstance(vis, dict) else None)
        if not isinstance(vis, dict) or not vis.get("detected"):
            return fail("fresh_2d_detection_required", **hold_out)
        if age is None or age > max_age:
            return fail("2d_detection_too_old", detection_age_s=age, **hold_out)
        if scenario not in GUIDED_SCENARIOS:
            return fail("guided_scenario_required", **hold_out)
        validation = validate_guided_scenario(
            str(scenario),
            vis,
            pick_preset.load_preset().get("teach_samples"),
        )
        if not validation.get("ok"):
            return fail(str(validation.get("reason")), validation=validation, **hold_out)
    return add_teach_sample(
        taught,
        vision_at_scan=vis if isinstance(vis, dict) else None,
        hold_out=hold_out,
        scenario=scenario,
    )


def delete_teach_sample(sample_id: str) -> dict[str, Any]:
    sid = (sample_id or "").strip()
    if not sid:
        return {"ok": False, "reason": "sample_id_required"}
    data = pick_preset.load_preset()
    samples = data.get("teach_samples")
    if not isinstance(samples, list):
        return {"ok": False, "reason": "no_samples"}
    new_samples = [s for s in samples if isinstance(s, dict) and s.get("id") != sid]
    if len(new_samples) == len(samples):
        return {"ok": False, "reason": "sample_not_found", "sample_id": sid}
    data["teach_samples"] = new_samples
    model = data.get("teach_model")
    if isinstance(model, dict) and model.get("active"):
        model["active"] = False
        model["invalidated_at"] = pick_preset._now_iso()
        model["invalidated_reason"] = "sample_deleted"
        data["teach_model"] = model
    pick_preset.save_preset(data)
    out = list_teach_samples()
    out["ok"] = True
    out["deleted_id"] = sid
    out["hint_it"] = "Esempio eliminato. Ricrea il modello teach se necessario."
    return out


def build_teach_model(*, require_guided_quality: bool = False) -> dict[str, Any]:
    """Costruisce modello interpolato da tutti gli esempi teach."""
    data = pick_preset.load_preset()
    samples = data.get("teach_samples")
    if not isinstance(samples, list) or not samples:
        zc = data.get("zero_calibration")
        if isinstance(zc, dict) and zc.get("joint_offset_deg"):
            samples = [
                {
                    "id": "legacy_zero_calibration",
                    "at": zc.get("at"),
                    "vision_at_scan": zc.get("vision_at_scan"),
                    "joint_offset_deg": zc.get("joint_offset_deg"),
                    "taught_servo_deg": zc.get("taught_servo_deg"),
                    "scan_servo_deg": zc.get("scan_servo_deg"),
                    "scan_waypoint": zc.get("scan_waypoint"),
                    "vision_features": _vision_features(zc.get("vision_at_scan")),
                }
            ]
        else:
            return {
                "ok": False,
                "reason": "no_teach_samples",
                "hint_it": "Aggiungi almeno un teach (Scansione +90° → Aggiungi teach) prima di creare il modello.",
            }

    valid = [s for s in samples if isinstance(s, dict) and isinstance(s.get("joint_offset_deg"), list)]
    if not valid:
        return {"ok": False, "reason": "no_valid_samples"}
    quality = guided_quality_report(valid)
    if require_guided_quality and not quality.get("ready"):
        data = pick_preset.load_preset()
        _append_history(data, "build_failed", reason="guided_scenarios_incomplete")
        pick_preset.save_preset(data)
        return {"ok": False, "reason": "guided_scenarios_incomplete", "quality": quality}

    centroid = _centroid_features(valid)
    baseline, blend_meta = interpolate_joint_offsets(
        {"detected": True, **(_synthetic_det_from_features(centroid) if centroid else {})},
        valid,
        data=data,
    )
    if baseline is None:
        baseline = pick_preset._sanitize_arm_offsets(
            [float(x) for x in valid[0]["joint_offset_deg"][:7]]
        )

    model: dict[str, Any] = {
        "at": pick_preset._now_iso(),
        "active": True,
        "method": "idw",
        "n_samples": len(valid),
        "sample_ids": [s.get("id") for s in valid],
        "centroid_features": centroid,
        "feature_weights": _feature_weights(),
        "baseline_offset_deg": baseline,
        "build_blend_meta": blend_meta,
    }
    data["teach_model"] = model
    data["joint_offset_deg"] = baseline
    data["source"] = "teach_model"
    scan = program_store.find_scan_waypoint()
    if scan:
        _pid, wp = scan
        sd = wp.get("servo_deg")
        if isinstance(sd, list):
            data["teach_model"]["scan_servo_deg"] = [float(x) for x in sd[:7]]
            data["teach_model"]["scan_waypoint"] = wp.get("name")

    _append_history(data, "build_ok", sample_count=len(valid), guided_ready=bool(quality.get("ready")))
    pick_preset.save_preset(data)
    info = pick_preset.preset_info()
    info["ok"] = True
    info["teach_model"] = model
    info["quality"] = quality
    info["hint_it"] = (
        f"Modello teach attivo ({len(valid)} esempi). "
        "Le prossime prese interpolano offset da foto + libreria teach."
    )
    return info


def _synthetic_det_from_features(feat: dict[str, float]) -> dict[str, Any]:
    det: dict[str, Any] = {"detected": True}
    if "norm_x" in feat and "norm_y" in feat:
        det["norm"] = [feat["norm_x"], feat["norm_y"]]
    if "px_x" in feat and "px_y" in feat:
        det["grip_center_px"] = [feat["px_x"], feat["px_y"]]
    if "short_deg" in feat:
        det["grip_align_deg"] = feat["short_deg"]
    return det


def model_is_active(data: dict[str, Any] | None = None) -> bool:
    raw = data if isinstance(data, dict) else pick_preset.load_preset()
    model = raw.get("teach_model")
    return isinstance(model, dict) and bool(model.get("active"))


def effective_offsets_from_model(
    cur_det: dict[str, Any] | None,
    *,
    data: dict[str, Any] | None = None,
) -> tuple[list[float] | None, dict[str, Any] | None]:
    raw = data if isinstance(data, dict) else pick_preset.load_preset()
    if not model_is_active(raw):
        return None, None
    samples = raw.get("teach_samples")
    if not isinstance(samples, list) or not samples:
        return None, None
    model = raw.get("teach_model") or {}
    sample_ids = model.get("sample_ids")
    if isinstance(sample_ids, list) and sample_ids:
        id_set = set(sample_ids)
        samples = [s for s in samples if isinstance(s, dict) and s.get("id") in id_set]
    off, meta = interpolate_joint_offsets(cur_det, samples, data=raw)
    if off is None:
        off = model.get("baseline_offset_deg")
        if isinstance(off, list):
            off = pick_preset._sanitize_arm_offsets([float(x) for x in off[:7]])
    if off is None:
        return None, None
    meta = meta or {}
    meta["teach_model_at"] = model.get("at")
    meta["n_samples"] = model.get("n_samples")
    return off, meta
