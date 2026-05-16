"""Assess detector/tracker outputs as validated 3D grasp candidates vs heuristic previews."""

from __future__ import annotations

import os
from typing import Any

BOX_TAG_IDS = frozenset({0, 1, 2, 3})


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def candidate_grasp_assessment(candidate: dict[str, Any] | None) -> dict[str, Any]:
    cand = candidate or {}
    target = cand.get("target") or {}
    preview = cand.get("preview") or {}
    grip = cand.get("grip_point") or {}
    obj = cand.get("object_detection") or {}
    tags_obj = cand.get("tags") or {}
    tag_rows = tags_obj.get("tags") or []
    tag_ids: list[int] = []
    for row in tag_rows:
        try:
            tag_ids.append(int(row.get("id", -1)))
        except (TypeError, ValueError, AttributeError):
            continue
    box_tag_ids = [tid for tid in tag_ids if tid in BOX_TAG_IDS]
    has_box_tags = bool(box_tag_ids)
    target_ok = bool(target.get("ok"))
    preview_ok = bool(preview.get("ok") and (preview.get("plan") or []))
    grip_ok = bool(grip.get("ok"))
    object_ok = bool(obj.get("ok"))
    absolute_ik_safe = bool(cand.get("absolute_ik_safe", True))
    allow_heuristic_execute = _env_truthy("GO2_GRASP_ALLOW_HEURISTIC_EXECUTE", "0")
    backend = str(obj.get("backend") or grip.get("source") or target.get("source") or "")
    backend_l = backend.lower()
    evidence: list[str] = []
    warnings: list[str] = []
    source_kind = "none"
    tier = "no_grasp_candidate"
    label_it = "nessun candidato"
    validated_source_3d = False

    if has_box_tags:
        source_kind = "apriltag_box_pose"
        validated_source_3d = True
        evidence.append("box_tags_visible")
    elif "depth" in backend_l or "pointcloud" in backend_l or "rgbd" in backend_l:
        source_kind = "depth_3d_estimate"
        validated_source_3d = True
        evidence.append("depth_backend")
    elif object_ok or grip_ok or target_ok or preview_ok:
        source_kind = "monocular_heuristic"

    if validated_source_3d and target_ok and preview_ok:
        tier = "validated_3d_grasp_candidate"
        label_it = "3D validato — pronto per IK"
        evidence.append("target_ok")
        evidence.append("preview_ik_ok")
    elif validated_source_3d:
        tier = "validated_3d_observation"
        label_it = "3D osservato — target/IK non pronti"
        if not target_ok:
            warnings.append("target_not_ready")
        if not preview_ok:
            warnings.append("preview_ik_not_ready")
    elif grip_ok or object_ok or target_ok or preview_ok:
        tier = "heuristic_preview_only"
        label_it = "preview euristica 2D/monoculare"
        if object_ok:
            evidence.append("object_detection")
        if grip_ok:
            evidence.append("grip_point")
        if target_ok:
            evidence.append("target_ok")
        if preview_ok:
            evidence.append("preview_ik_ok")
        warnings.append("object_pose_not_validated_3d")
        warnings.append("grasp_normal_not_validated")
        if not has_box_tags:
            warnings.append("no_box_tags")
    else:
        warnings.append("no_target")

    execution_allowed = False
    if tier == "validated_3d_grasp_candidate" and absolute_ik_safe:
        execution_allowed = True
    elif tier == "heuristic_preview_only" and absolute_ik_safe and allow_heuristic_execute:
        execution_allowed = True
        warnings.append("heuristic_execution_override_enabled")

    if not absolute_ik_safe:
        warnings.append("absolute_ik_not_safe")

    return {
        "tier": tier,
        "label_it": label_it,
        "source_kind": source_kind,
        "validated_source_3d": validated_source_3d,
        "validated_3d": tier == "validated_3d_grasp_candidate",
        "preview_only": tier == "heuristic_preview_only",
        "execution_allowed": execution_allowed,
        "allow_heuristic_execute": allow_heuristic_execute,
        "tag_ids_seen": tag_ids,
        "box_tag_ids": box_tag_ids,
        "evidence": evidence,
        "warnings": warnings,
        "object_backend": obj.get("backend"),
        "object_confidence": obj.get("confidence"),
        "grip_source": grip.get("source"),
        "absolute_ik_safe": absolute_ik_safe,
    }


def plan_grasp_assessment(plan: dict[str, Any] | None) -> dict[str, Any]:
    blob = plan or {}
    candidates = blob.get("candidates") or {}
    per_camera: dict[str, Any] = {}
    for key, cand in candidates.items():
        per_camera[str(key)] = candidate_grasp_assessment(cand if isinstance(cand, dict) else {})
    selected_key = blob.get("selected_camera")
    selected_key_s = None if selected_key is None else str(selected_key)
    selected = per_camera.get(selected_key_s) if selected_key_s is not None else None
    return {
        "selected_camera": selected_key,
        "selected": selected,
        "per_camera": per_camera,
        "has_validated_3d_any": any(bool(v.get("validated_3d")) for v in per_camera.values()),
        "has_preview_only_any": any(bool(v.get("preview_only")) for v in per_camera.values()),
        "selected_execution_allowed": bool((selected or {}).get("execution_allowed")),
    }


def detector_training_scope(detector_status: dict[str, Any] | None) -> dict[str, Any]:
    ds = detector_status or {}
    trained_labels = ds.get("trained_labels") or []
    scope = str(ds.get("training_scope") or "heuristic_only")
    open_vocabulary = bool(ds.get("open_vocabulary"))
    if scope == "open_vocabulary_text_prompted":
        label_it = "open-vocabulary (prompt testuali)"
    elif scope == "closed_set_labels":
        label_it = "closed-set su classi note"
    else:
        label_it = "solo fallback euristico / nessun modello"
    return {
        "training_scope": scope,
        "label_it": label_it,
        "open_vocabulary": open_vocabulary,
        "trained_labels": trained_labels,
        "trained_label_count": len(trained_labels),
        "model_family": ds.get("model_family"),
        "model_path": ds.get("model_path"),
        "model_exists": bool(ds.get("model_exists")),
        "recommended_use_it": ds.get("recommended_use_it"),
    }
