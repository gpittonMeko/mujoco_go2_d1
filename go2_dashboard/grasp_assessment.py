"""Assess detector/tracker outputs as validated 3D grasp candidates vs heuristic previews."""

from __future__ import annotations

import os
from typing import Any

BOX_TAG_IDS = frozenset({0, 1, 2, 3})

STUB_BACKENDS = frozenset({"stub", "openvla_runtime_stub"})


def is_rejected_stub_backend(backend: str | None) -> bool:
    b = (backend or "").strip().lower()
    if b in STUB_BACKENDS:
        return True
    return "stub" in b and b not in {"box_grasp_planner"}


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
    elif "graspgen" in backend_l:
        source_kind = "graspgen_6dof"
        validated_source_3d = True
        evidence.append("graspgen_backend")
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


def worker_flat_plan_assessment(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Assessment per JSON piatto restituito dal worker (non struttura candidates multi-camera)."""
    blob = plan or {}
    depth_obs = blob.get("depth_observation") or {}
    obj = blob.get("object_detection") or {}
    target = blob.get("target") or {}
    preview = blob.get("preview") or {}
    stub = is_rejected_stub_backend(str(blob.get("backend") or ""))
    cand = {
        "target": target,
        "preview": preview,
        "object_detection": obj,
        "tags": blob.get("tags") or {},
        "grip_point": blob.get("grip_point") or {},
        "absolute_ik_safe": blob.get("absolute_ik_safe", True),
    }
    base = candidate_grasp_assessment(cand)
    if depth_obs.get("ok") or obj.get("backend") == "rgbd_depth_fused":
        base["validated_source_3d"] = True
        base["source_kind"] = "depth_3d_estimate"
        if target.get("ok") and preview.get("ok"):
            base["tier"] = "validated_3d_grasp_candidate"
            base["label_it"] = "3D RGB-D — pronto per IK"
            base["execution_allowed"] = bool(base.get("absolute_ik_safe", True))
        elif target.get("ok") or preview.get("ok"):
            base["tier"] = "validated_3d_observation"
            base["label_it"] = "3D RGB-D — target/IK parziali"
    if stub:
        base["execution_allowed"] = False
        base["tier"] = "stub_plan"
        base["label_it"] = "Piano stub — non eseguire"
        base["warnings"] = list(base.get("warnings") or []) + ["stub_worker_backend"]
    base["worker_backend"] = blob.get("backend")
    base["hint_it"] = (
        "Esecuzione consentita."
        if base.get("execution_allowed")
        else "Serve piano 3D validato (depth/tags) o override esplicito."
    )
    return base


def plan_grasp_assessment(plan: dict[str, Any] | None) -> dict[str, Any]:
    blob = plan or {}
    candidates = blob.get("candidates") or {}
    per_camera: dict[str, Any] = {}
    for key, cand in candidates.items():
        per_camera[str(key)] = candidate_grasp_assessment(cand if isinstance(cand, dict) else {})
    selected_key = blob.get("selected_camera")
    selected_key_s = None if selected_key is None else str(selected_key)
    selected = per_camera.get(selected_key_s) if selected_key_s is not None else None
    worker_backend = str(blob.get("backend") or "").strip().lower()
    stub_plan = is_rejected_stub_backend(worker_backend)
    return {
        "selected_camera": selected_key,
        "selected": selected,
        "per_camera": per_camera,
        "worker_backend": worker_backend or None,
        "stub_plan": stub_plan,
        "has_validated_3d_any": any(bool(v.get("validated_3d")) for v in per_camera.values()),
        "has_preview_only_any": any(bool(v.get("preview_only")) for v in per_camera.values()),
        "selected_execution_allowed": False
        if stub_plan
        else bool((selected or {}).get("execution_allowed")),
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


def _check(status: str, ok: bool, label_it: str, detail_it: str = "") -> dict[str, Any]:
    return {"status": status, "ok": ok, "label_it": label_it, "detail_it": detail_it}


def build_grasp_validation_ui(
    *,
    health: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Checklist per tab Presa — ogni riga visibile in UI (#graspValidationPanel)."""
    h = health or {}
    p = plan or {}
    ass = p.get("grasp_assessment") or p.get("selected_grasp_assessment") or {}
    if not ass and p.get("ok"):
        ass = worker_flat_plan_assessment(p)

    checks: list[dict[str, Any]] = []
    worker_ok = bool(h.get("worker_reachable"))
    wp = h.get("worker_payload") or {}
    backend = str(p.get("backend") or wp.get("backend") or "")
    checks.append(
        _check(
            "pass" if worker_ok else "fail",
            worker_ok,
            "Worker AWS raggiungibile",
            h.get("worker_url") or "",
        )
    )
    real_backend = bool(backend) and not is_rejected_stub_backend(backend)
    checks.append(
        _check(
            "pass" if real_backend else "fail",
            real_backend,
            "Piano da planner reale (no stub)",
            f"backend={backend or '—'}",
        )
    )
    cloud = bool(h.get("cloud_mode"))
    checks.append(
        _check(
            "pass" if cloud else "warn",
            cloud,
            "Cloud mode (JPEG inline verso EC2)",
            "GO2_GRASP_CLOUD_MODE=1",
        )
    )
    img_src = str(p.get("image_source") or "")
    has_jpeg = "jpeg" in img_src or bool(p.get("cloud_embedded")) or bool(p.get("rgbd_embedded"))
    checks.append(
        _check(
            "pass" if has_jpeg else "fail",
            has_jpeg,
            "Immagine camera inviata al worker",
            img_src or "manca jpeg_base64",
        )
    )
    obj = p.get("object_detection") or {}
    obj_ok = bool(obj.get("ok"))
    checks.append(
        _check(
            "pass" if obj_ok else "fail",
            obj_ok,
            "Oggetto rilevato nel frame",
            f"{obj.get('backend', '—')} conf={obj.get('confidence', '—')}",
        )
    )
    depth_ok = bool((p.get("depth_observation") or {}).get("ok")) or obj.get("backend") == "rgbd_depth_fused"
    depth_emb = bool(p.get("depth_embedded"))
    checks.append(
        _check(
            "pass" if depth_ok else ("warn" if depth_emb else "fail"),
            depth_ok,
            "Profondità / fusione 3D",
            "rgbd_depth_fused" if depth_ok else ("depth inviata ma non validata" if depth_emb else "imposta GO2_DEPTH_VIDEO_INDEX_*"),
        )
    )
    tgt = p.get("target") or {}
    checks.append(
        _check(
            "pass" if tgt.get("ok") else "fail",
            bool(tgt.get("ok")),
            "Target 3D in base_link",
            str(tgt.get("base_xyz_m") or tgt.get("source") or "—"),
        )
    )
    prev = p.get("preview") or {}
    prev_ok = bool(prev.get("ok") and (prev.get("plan") or []))
    checks.append(
        _check(
            "pass" if prev_ok else "fail",
            prev_ok,
            "Preview IK (fasi pre_grasp→grasp)",
            f"{len(prev.get('plan') or [])} stadi" if prev_ok else (prev.get("failed_stage") or "IK fallita"),
        )
    )
    exec_ok = bool(ass.get("execution_allowed"))
    checks.append(
        _check(
            "pass" if exec_ok else "fail",
            exec_ok,
            "Esecuzione braccio consentita",
            ass.get("label_it") or ass.get("tier") or "—",
        )
    )

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    all_pass = not fails and bool(p.get("ok"))
    if is_rejected_stub_backend(backend):
        banner = "BLOCCATO: piano STUB — non usare. Riconfigura worker (planner/auto)."
        banner_level = "fail"
    elif not worker_ok:
        banner = "BLOCCATO: worker non raggiungibile — Avvia EC2 + Health."
        banner_level = "fail"
    elif not p.get("ok"):
        banner = "Genera un piano con «Piano VLA» prima di muovere il braccio."
        banner_level = "warn"
    elif not exec_ok:
        banner = "Piano pronto ma NON eseguibile: serve depth/tag validi (vedi righe rosse)."
        banner_level = "warn"
    elif warns:
        banner = "Piano eseguibile con avvisi — controlla overlay e tab 3D prima di muovere."
        banner_level = "warn"
    else:
        banner = "VALIDATO — puoi usare «Sequenza presa (fasi)»."
        banner_level = "pass"

    return {
        "ok": all_pass and exec_ok and real_backend,
        "banner_it": banner,
        "banner_level": banner_level,
        "checks": checks,
        "can_execute_phased": exec_ok and real_backend and prev_ok,
        "can_execute_ik": exec_ok and real_backend and bool(p.get("grasp_display_base_link_m")),
        "tier": ass.get("tier"),
        "label_it": ass.get("label_it"),
    }
