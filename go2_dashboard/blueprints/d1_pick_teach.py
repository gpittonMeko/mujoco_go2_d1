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
    try:
        out = pick_vision.capture_and_detect()
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
    body = request.get_json(silent=True) or {}
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

