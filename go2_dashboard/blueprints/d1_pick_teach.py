"""API Orbbec RGB + pick teach — condivisa tra dashboard 5052 e D1 jog 5053."""

from __future__ import annotations

import os
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
)

bp = Blueprint("d1_pick_teach", __name__)


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

@bp.route("/api/orbbec/last.jpg")
def orbbec_last_jpeg() -> Response:
    path = orbbec_capture.latest_snapshot_path()
    if path is None:
        return jsonify({"ok": False, "reason": "no_snapshot"}), 404
    return send_file(path, mimetype="image/jpeg", max_age=0)

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

@bp.route("/api/pick/snapshot", methods=["POST"])
def pick_snapshot() -> Response:
    out = pick_vision.capture_and_detect()
    _apply_pick_detection_to_preset(out)
    code = 200 if out.get("ok") else 502
    return jsonify(out), code

@bp.route("/api/pick/detect", methods=["POST"])
def pick_detect() -> Response:
    body = request.get_json(silent=True) or {}
    if body.get("capture_if_missing", True):
        out = pick_vision.capture_and_detect()
    else:
        out = pick_vision.detect_on_latest_snapshot(capture_if_missing=False)
    _apply_pick_detection_to_preset(out)
    code = 200 if out.get("ok") else 502
    return jsonify(out), code

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
    found = program_store.find_scan_waypoint()
    if found is None:
        return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
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
    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        return jsonify(couple), 502
    open_j6 = pick_preset.gripper_open_j6_deg(scan_sd)
    out = program_runner.move_to_servo_deg_smooth(target, pin_joints={6: open_j6})
    out["preset"] = "grasp_approach"
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
    body = request.get_json(silent=True) or {}
    variant = str(body.get("variant") or "base").strip().lower()
    if variant not in ("base", "j90", "90"):
        variant = "base"
    scan_variant = "j90" if variant in ("j90", "90") else "base"
    found = program_store.find_scan_waypoint(variant=scan_variant)
    if found is None:
        reason = "scan_j90_waypoint_not_found" if scan_variant == "j90" else "scan_waypoint_not_found"
        return jsonify({"ok": False, "reason": reason}), 404
    _program_id, wp = found
    raw = wp.get("servo_deg")
    if not isinstance(raw, list) or len(raw) < 6:
        return jsonify({"ok": False, "reason": "invalid_waypoint"}), 400
    servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        return jsonify(couple), 502
    out = program_runner.move_to_servo_deg_smooth(servo)
    out["preset"] = "scan"
    out["coupling"] = couple
    out["scan_variant"] = scan_variant
    out["waypoint_name"] = wp.get("name")
    out["target_servo_deg"] = servo
    code = 200 if out.get("ok") else 502
    return jsonify(out), code

