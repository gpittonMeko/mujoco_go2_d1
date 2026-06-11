"""HTTP routes for operator dashboard (see helpers_*.py)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

from flask import Response, jsonify, make_response, request

from go2_dashboard.blueprints.grasp import grasp_health_payload
from go2_dashboard.grasp_coach_agent import (
    grasp_coach_enabled,
    grasp_coach_feedback,
    grasp_coach_model,
    grasp_coach_model_ladder_it,
    grasp_coach_preview_metric,
    grasp_coach_step,
)
from go2_dashboard.grasp_coach_memory import read_recent_grasp_coach_events
from go2_dashboard.grasp_teach_calib import (
    teach_calib_cancel,
    teach_calib_clear,
    teach_calib_list_samples,
    teach_calib_start,
    teach_calib_status,
)
from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
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
from go2_dashboard import d1_arm_motion
from go2_dashboard.d1_arm_publish_lite import (
    ALIGNMENT_START_PATH,
    TRUE_ZERO_POSE_PATH,
    arm_emergency_stop_hold,
    goto_home_servo_deg,
    goto_joints_rad_clamped_six,
    check_at_saved_start_pose,
    goto_fold_compact_for_grasp,
    goto_saved_start_from_json,
    normalize_start_variant,
    resolve_start_alignment_path,
    save_start_alignment_json,
    start_alignment_status,
    goto_tool_target_base_link_m,
    goto_true_zero_from_json,
    goto_true_zero_then_saved_start_from_json,
    pick_tool_target_base_link_m_from_plan,
    publish_goto_servo_deg7,
    publish_live_pose_deg7,
    publish_move_one_joint_deg,
    save_true_zero_pose_json,
)
from go2_dashboard.hermes_agent import (
    hermes_enabled,
    hermes_effective_tts_voice,
    hermes_model,
    hermes_normalize_intent_reply,
    hermes_openai_base_url,
    hermes_personality_labels_it,
    hermes_resolve_personality,
    hermes_runtime_context_block,
    hermes_skills_status_payload,
    hermes_tts_voice,
    openai_api_key,
    openai_tts_mp3_base64,
    route_natural_language,
)
from go2_dashboard.operator_plan_cache import get_last_grasp_plan
from go2_dashboard.operator_session_memory import (
    append_operator_session_event,
    read_recent_operator_session_events,
)
from go2_dashboard.operator_scene import build_grasp_pipeline_stub, build_scene_3d_payload
from go2_dashboard.operator_stack import go2_local, nx_stack_status
from go2_dashboard.paths import PROJECT_ROOT
from go2_dashboard.scene_meshes import send_scene_mesh_file
from go2_dashboard.sport_lane import accompany_execute_json, accompany_mode_handle, sport_last_payload
from go2_dashboard import tag5_calibration_lite as t5
from . import bp, _PROCESS_STARTED_AT
from .helpers_hermes import (
    _hermes_apply_intent,
    _hermes_capabilities_from_body,
    _hermes_routing_note_for_caps,
    _hermes_sanitize_intent,
    hermes_append_turn_log_memory,
    hermes_apply_go2_base_lexicon_from_user_text,
    hermes_apply_grasp_full_lexicon_from_user_text,
    hermes_operator_memory_block_for_prompt,
    hermes_should_log_turn_to_memory,
    hermes_try_play_tts_mp3_on_go2_speaker,
    hermes_try_play_tts_mp3_on_go2_webrtc,
    hermes_try_play_tts_mp3_on_local_host,
    hermes_inject_arm_joint_delta_from_user_text,
    hermes_summarize_intent_it,
)
from .helpers_camera import (
    _depth_sysfs_hint_rows,
    _depth_v4l_index_for_logical_camera,
    _enrich_v4l_nodes_detail,
    _operator_camera_summary,
    _orbbec_rgb_sysfs_hints,
    _robot_camera_jpeg,
    cv2,
    np,
)
from .helpers_arm_http import (
    _arm_post_delay_ms,
    _op_now_iso,
    _parse_goto_max_step_deg,
    _true_zero_motion_http_response_lite,
)
from .helpers_mission import (
    _mission_admin_token_matches,
    _mission_env_safe,
    _mission_public_base_and_prefix,
    _mission_restart_instructions,
    _mission_run_box_detect_step,
    _mission_worker_summary,
    _nx_dashboard_delayed_pkill,
)
from .helpers_timing import merge_http_timing_into_json_dict

_LAST_WEBRTC_BEEP_LOCK = threading.Lock()
_LAST_WEBRTC_BEEP: dict[str, Any] = {"status": "idle"}
_WEBRTC_BEEP_STALE_RUNNING_S = 95.0


def _webrtc_beep_expire_stale_running() -> None:
    """Se un job async è rimasto ``running`` troppo a lungo, sblocca la coda."""
    with _LAST_WEBRTC_BEEP_LOCK:
        if _LAST_WEBRTC_BEEP.get("status") != "running":
            return
        started_raw = str(_LAST_WEBRTC_BEEP.get("started_at") or "").strip()
        if not started_raw:
            _LAST_WEBRTC_BEEP.update(
                {
                    "status": "done",
                    "ok": False,
                    "error": "stale_running_without_started_at",
                    "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                }
            )
            return
        try:
            started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        except ValueError:
            return
        age_s = (datetime.utcnow().replace(tzinfo=started.tzinfo) - started).total_seconds()
        if age_s >= _WEBRTC_BEEP_STALE_RUNNING_S:
            _LAST_WEBRTC_BEEP.update(
                {
                    "status": "done",
                    "ok": False,
                    "error": "stale_running_timeout",
                    "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "hint_it": (
                        f"Job WebRTC beep scaduto dopo {int(age_s)}s (probabile timeout handshake). "
                        "Riprova; chiudi app Unitree se muto."
                    ),
                }
            )

@bp.route("/api/health", methods=["GET"])
def api_health() -> Any:
    gl = go2_local()
    cv2_ok = cv2 is not None
    return jsonify(
        {
            "ok": True,
            "service": "go2_dashboard",
            "pid": os.getpid(),
            "process_started_at": _PROCESS_STARTED_AT,
            "dashboard_py_mtime": None,
            "reload_recommended": False,
            "reload_hint": None,
            "operator_dashboard": True,
            "go2_local": gl,
            "cv2": cv2_ok,
            "camera_jpeg_ready": gl and cv2_ok,
            "camera_jpeg_hint": (
                None
                if gl and cv2_ok
                else (
                    "Set GO2_LOCAL=1 (e riavvia) oppure installa OpenCV per Python su questo host — "
                    "anteprime /api/robot/camera/*.jpg e /api/cameras/v4l/*/preview.jpg richiedono entrambi."
                    if not gl
                    else "OpenCV (cv2) non importabile in questo processo — es. pip/install python3-opencv sulla NX."
                )
            ),
            "d1_arm_motion_backend": d1_arm_motion.motion_backend_name(),
            "d1_arm_command_ready": d1_arm_motion.command_binary_ready(),
        }
    )


@bp.route("/api/status", methods=["GET"])
def api_status() -> Any:
    return jsonify({"ok": True, "operator_dashboard": True, "pid": os.getpid()})


@bp.route("/api/hermes/status", methods=["GET"])
def api_hermes_status() -> Any:
    payload: dict[str, Any] = {
        "ok": True,
        "GO2_ENABLE_HERMES_AGENT": hermes_enabled(),
        "has_openai_api_key": bool(openai_api_key()),
        "openai_base_url": hermes_openai_base_url(),
        "model": hermes_model(),
        "tts_voice": hermes_tts_voice(),
        "tts_openai_supported": bool(openai_api_key()),
        "personality_presets": hermes_personality_labels_it(),
        "personality_env": (os.environ.get("GO2_HERMES_PERSONALITY") or "").strip(),
        "personality_note_it": (
            "Campo POST opzionale `personality` (es. bender_meeting) ha priorità su GO2_HERMES_PERSONALITY. "
            "OpenAI TTS non clona voci di personaggi protetti: con preset VIP si usa voce «robotica» "
            "(default onyx, override GO2_HERMES_TTS_VOICE_BENDER)."
        ),
        "capabilities_ui_it": (
            "Invia sempre la mappa capabilities dalla Hermes Console. Senza questo campo nel JSON POST, "
            "Hermes imposta permessi larghi legacy (solo laboratorio). Opzionale: campi "
            "`mission_context` (testo persistente) e `text` (comando corrente). "
            "`execution_mode`: `run` (default) esegue subito Sport/braccio; `preview` mostra il piano e richiede "
            "`POST /api/hermes/execute_intent` dopo approvazione UI. "
            "Memoria: eventi `POST /api/operator_session/memory` con tag che contiene `hermes` possono essere inclusi nel prompt "
            "(flag JSON `include_operator_memory`, default true; env `GO2_HERMES_OPERATOR_MEMORY_LINES`, default 14)."
        ),
    }
    payload.update(hermes_skills_status_payload())
    rtc = hermes_runtime_context_block()
    payload["hermes_runtime_context"] = {
        "enabled": (os.environ.get("GO2_HERMES_APPEND_RUNTIME_CONTEXT") or "1").strip().lower()
        not in {"0", "false", "no", "off"},
        "chars": len(rtc),
        "GO2_HERMES_RUNTIME_CONTEXT_MAX_CHARS": (os.environ.get("GO2_HERMES_RUNTIME_CONTEXT_MAX_CHARS") or "1800").strip(),
    }
    payload["hermes_knowledge_layers_it"] = (
        "Tre livelli: (1) **disk skills** `data/hermes_skills/` → system prompt (conoscenza stabile da markdown in repo); "
        "(2) **memoria operatore** JSONL + turn_log → user message se attivi dalla UI; "
        "(3) **live dashboard context** → user message ogni turno (ultimo Sport RPC di questo processo + stack NX). "
        "Il prodotto **Nous Hermes Agent** esterno resta opzionale: stesso spirito «skill persistenti + contesto» "
        "qui è locale su disco + snapshot HTTP; vedi `docs/HERMES_NOUS_INTEGRATION.md`."
    )
    payload["go2_local"] = go2_local()
    payload["go2_enable_base_motion_env"] = (os.environ.get("GO2_ENABLE_BASE_MOTION") or "").strip()
    payload["sport_integration_it"] = (
        "Nel JSON Hermes, ``base_motion`` viene eseguito con ``accompany_execute_json`` → RPC Sport/DDS sul Go2 "
        "(richiede GO2_LOCAL=1 e GO2_ENABLE_BASE_MOTION=1 sul processo dashboard)."
    )
    payload["turn_memory_logging_default"] = hermes_should_log_turn_to_memory({})
    payload["turn_memory_logging_hint_it"] = (
        "Se attivo (default), ogni turno chat viene appenduto in data/operator_session_memory.jsonl con tag "
        "`turn_log` e ripescato nel prompt quando «Includi memoria» è on. Disabilita env GO2_HERMES_LOG_TURNS_TO_MEMORY=0 "
        "o deseleziona dalla UI «Salva turno in memoria»."
    )
    play_go2 = (os.environ.get("GO2_HERMES_PLAY_ON_GO2") or "").strip().lower() in {"1", "true", "yes", "on"}
    play_wrtc = (os.environ.get("GO2_HERMES_PLAY_ON_GO2_WEBRTC") or "").strip().lower() in {"1", "true", "yes", "on"}
    webrtc_ip = (os.environ.get("GO2_WEBRTC_IP") or os.environ.get("UNITREE_ROBOT_IP") or "").strip()
    try:
        import unitree_webrtc_connect  # noqa: F401

        webrtc_import_ok = True
    except ImportError:
        webrtc_import_ok = False
    strict_browser = (os.environ.get("GO2_HERMES_SUPPRESS_BROWSER_MP3") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    robot_only_tts_env = (os.environ.get("GO2_HERMES_TTS_ROBOT_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}
    payload["tts_paths_it"] = (
        "Hermes è prima **chat testuale** (`assistant_reply_it`) + JSON azioni (Sport, braccio, jog giunti). "
        "La voce è opzionale: checkbox «Cloud TTS» → MP3 OpenAI; sul Go2 servono DDS (`GO2_HERMES_PLAY_ON_GO2`) "
        "e/o WebRTC (`GO2_HERMES_PLAY_ON_GO2_WEBRTC` + `GO2_WEBRTC_IP` + pip). "
        "Se il cane è muto e non hai `GO2_HERMES_TTS_ROBOT_ONLY` né `GO2_HERMES_SUPPRESS_BROWSER_MP3`, il browser può fare fallback."
    )
    payload["tts_env"] = {
        "GO2_HERMES_PLAY_ON_GO2": play_go2,
        "GO2_HERMES_PLAY_ON_GO2_WEBRTC": play_wrtc,
        "GO2_WEBRTC_IP_configured": bool(webrtc_ip),
        "unitree_webrtc_connect_import_ok": webrtc_import_ok,
        "GO2_HERMES_SUPPRESS_BROWSER_MP3_strict": strict_browser,
        "GO2_HERMES_TTS_ROBOT_ONLY": robot_only_tts_env,
    }
    return jsonify(payload)


def _hermes_snapshot_robot_jpeg(vcam: int) -> bytes | None:
    """JPEG da cache V4L locale (NX) con fallback HTTP snapshot."""
    CAMERA_CACHE.start()
    jpg: bytes | None = None
    try:
        jpg = CAMERA_CACHE.peek_jpeg(vcam)
        if not jpg:
            jpg = CAMERA_CACHE.get_jpeg(vcam, wait_s=2.0)
    except Exception:
        jpg = None
    if not jpg:
        try:
            jpg = _robot_camera_jpeg(vcam)
        except Exception:
            jpg = None
    return jpg


@bp.route("/api/hermes/command", methods=["POST"])
def api_hermes_command() -> Any:
    if not hermes_enabled():
        return (
            jsonify(
                merge_http_timing_into_json_dict(
                    {
                        "ok": False,
                        "reason": "GO2_ENABLE_HERMES_AGENT_not_enabled",
                        "hint_it": "Esporta GO2_ENABLE_HERMES_AGENT=1 sul processo dashboard (nx_dashboard_env.sh) e riavvia.",
                    }
                )
            ),
            503,
        )
    if not openai_api_key():
        return (
            jsonify(
                merge_http_timing_into_json_dict(
                    {
                        "ok": False,
                        "reason": "missing_OPENAI_API_KEY",
                        "hint_it": "Imposta OPENAI_API_KEY (o GO2_OPENAI_API_KEY) nell'ambiente — mai nel codice o git.",
                    }
                )
            ),
            503,
        )
    body = request.get_json(silent=True) or {}
    base_cmd = str(body.get("text") or body.get("command") or "").strip()
    mission_ctx = str(body.get("mission_context") or "").strip()
    sections: list[str] = []
    if mission_ctx:
        sections.append(
            "--- Mission context (operator UI) ---\n"
            + mission_ctx
            + "\n--- End mission context ---"
        )
    if bool(body.get("include_operator_memory", True)):
        mem_blk = hermes_operator_memory_block_for_prompt()
        if mem_blk:
            sections.append(mem_blk)
    sections.append("Operator request:\n" + base_cmd)
    text = "\n\n".join(s for s in sections if s)
    if not base_cmd.strip():
        return jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "missing_text", "hint_it": 'JSON {"text":"..."}'})), 400
    dry_run = bool(body.get("dry_run"))
    exec_mode_raw = str(body.get("execution_mode") or "run").strip().lower()
    preview_only = exec_mode_raw in {"preview", "confirm", "approve", "approve_first"}
    caps = _hermes_capabilities_from_body(body)
    personality_eff = hermes_resolve_personality(body_value=body.get("personality"), use_env=True)

    routing_note = _hermes_routing_note_for_caps(caps)
    if preview_only:
        routing_note += (
            "\n\n─── Hermes **PREVIEW / APPROVAL** mode ───\n"
            "Your JSON motor commands will **not** run until the operator approves in the UI. "
            "In `assistant_reply_it`, clearly list what you propose (Sport `base_motion`, arm preset, "
            "`arm_joint_delta`, `arm_tool_target`). Output the same JSON you expect after approval."
        )

    vision_pairs: list[tuple[str, bytes]] | None = None
    vision_jpeg_legacy: bytes | None = None
    if bool(body.get("attach_camera")) and go2_local():
        dual = os.environ.get("GO2_HERMES_VISION_DUAL_CAMERA", "1").lower() in {"1", "true", "yes", "on"}
        if dual:
            vision_pairs = []
            for label, vcam in (
                ("Robot head forward camera (logical slot 6)", 6),
                ("Robot wrist / tool camera (logical slot 0)", 0),
            ):
                jpg = _hermes_snapshot_robot_jpeg(vcam)
                if jpg:
                    vision_pairs.append((label, jpg))
            if not vision_pairs:
                vision_pairs = None
        else:
            raw_vc = body.get("logical_camera_for_vision")
            try:
                vcam = int(str(raw_vc).strip()) if raw_vc is not None and str(raw_vc).strip() != "" else int(
                    (os.environ.get("GO2_HERMES_VISION_CAMERA") or "6").strip() or "6"
                )
            except (TypeError, ValueError):
                vcam = 6
            if vcam not in (0, 6):
                vcam = 6
            vision_jpeg_legacy = _hermes_snapshot_robot_jpeg(vcam)

    try:
        intent_llm = route_natural_language(
            text,
            routing_note=routing_note,
            personality=personality_eff,
            vision_jpeg_bytes=vision_jpeg_legacy,
            vision_jpeg_pairs=vision_pairs,
        )
    except Exception as exc:
        return jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "hermes_llm_failed", "detail": repr(exc)})), 502

    intent_llm, lex_notes = hermes_apply_go2_base_lexicon_from_user_text(base_cmd, intent_llm, caps)
    intent_llm, grasp_notes = hermes_apply_grasp_full_lexicon_from_user_text(base_cmd, intent_llm, caps)
    intent_llm, inject_notes = hermes_inject_arm_joint_delta_from_user_text(base_cmd, intent_llm, caps)
    intent_eff, warnings_it = _hermes_sanitize_intent(intent_llm, caps)
    server_notes = [str(x) for x in list(lex_notes) + list(grasp_notes) + list(inject_notes) if str(x).strip()]
    if server_notes:
        warnings_it = server_notes + list(warnings_it)
        br = str(intent_eff.get("assistant_reply_it") or "").strip()
        tag = " · ".join(server_notes)
        intent_eff["assistant_reply_it"] = (br + " · " + tag) if br else tag
    hermes_normalize_intent_reply(intent_eff)

    if preview_only:
        applied = {"ok": True, "dry_run": False, "steps": [], "intent": intent_eff}
    else:
        applied = _hermes_apply_intent(intent_eff, dry_run=dry_run)
    reply = str(intent_eff.get("assistant_reply_it") or "").strip()
    if not reply and warnings_it:
        reply = " ".join(str(w) for w in warnings_it if str(w).strip()).strip()
    if not reply:
        reply = "Empty narration — open Technical JSON to inspect Hermes intent."

    cap_report = {
        k: v
        for k, v in caps.items()
        if not str(k).startswith("_")
    }

    payload: dict[str, Any] = {
        "ok": bool(applied.get("ok")),
        "assistant_reply_it": reply,
        "capabilities_effective": cap_report,
        "legacy_capabilities": bool(caps.get("_legacy")),
        "intent_llm": intent_llm,
        "intent": intent_eff,
        "warnings_it": warnings_it,
        "dry_run": applied.get("dry_run"),
        "steps": applied.get("steps"),
        "personality_effective": personality_eff,
        "execution_mode_requested": exec_mode_raw,
        "preview_only": preview_only,
        "action_digest_it": hermes_summarize_intent_it(intent_eff),
    }

    if bool(body.get("tts_openai")) and reply:
        try:
            voice_eff = hermes_effective_tts_voice(body_voice=body.get("tts_voice"), personality=personality_eff)
            payload["tts_voice_used"] = voice_eff
            b64_audio = openai_tts_mp3_base64(text=reply, voice=voice_eff)
            payload["tts_audio_mp3_base64"] = b64_audio
            payload["tts_format"] = "mp3"
            robot_only_tts = (os.environ.get("GO2_HERMES_TTS_ROBOT_ONLY") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            prefer_webrtc = (os.environ.get("GO2_HERMES_PREFER_WEBRTC") or "1").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if prefer_webrtc:
                played_go2_webrtc = hermes_try_play_tts_mp3_on_go2_webrtc(b64_audio)
                played_go2_dds = False if played_go2_webrtc else hermes_try_play_tts_mp3_on_go2_speaker(b64_audio)
            else:
                played_go2_dds = hermes_try_play_tts_mp3_on_go2_speaker(b64_audio)
                played_go2_webrtc = False if played_go2_dds else hermes_try_play_tts_mp3_on_go2_webrtc(b64_audio)
            played_go2 = played_go2_dds or played_go2_webrtc
            played_nx = False
            if not played_go2 and not robot_only_tts:
                played_nx = hermes_try_play_tts_mp3_on_local_host(b64_audio)
            payload["tts_playback_go2"] = played_go2
            payload["tts_playback_go2_dds"] = played_go2_dds
            payload["tts_playback_go2_webrtc"] = played_go2_webrtc
            payload["tts_playback_nx"] = played_nx
            playback_remote = played_go2 or played_nx
            # Solo se esplicitamente GO2_HERMES_SUPPRESS_BROWSER_MP3=1 si muta il browser anche quando il cane non riproduce nulla.
            force_suppress_browser = (os.environ.get("GO2_HERMES_SUPPRESS_BROWSER_MP3") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            payload["tts_suppress_client_audio"] = bool(
                playback_remote or force_suppress_browser or robot_only_tts
            )
            payload["tts_robot_only_env"] = robot_only_tts
            if not playback_remote:
                if robot_only_tts:
                    payload["tts_playback_hint_it"] = (
                        "GO2_HERMES_TTS_ROBOT_ONLY=1: niente fallback NX né browser; sul Go2 non è partito nulla. "
                        "Verifica DDS voice oppure WebRTC (GO2_HERMES_PLAY_ON_GO2_WEBRTC, GO2_WEBRTC_IP, "
                        "pip unitree-webrtc-connect, AES se firmware ≥ 1.1.15)."
                    )
                elif force_suppress_browser:
                    payload["tts_playback_hint_it"] = (
                        "TTS cloud ok ma nessuna riproduzione su cane/NX; GO2_HERMES_SUPPRESS_BROWSER_MP3=1 "
                        "tiene anche il browser muto. Metti GO2_HERMES_SUPPRESS_BROWSER_MP3=0 per sentire il MP3 nel PC, "
                        "oppure sistema DDS (`GO2_LOCAL`, `GO2_HERMES_PLAY_ON_GO2`) / WebRTC (`GO2_HERMES_PLAY_ON_GO2_WEBRTC`, "
                        "`GO2_WEBRTC_IP`, pip unitree-webrtc-connect, AES se firmware ≥ 1.1.15)."
                    )
                else:
                    payload["tts_playback_hint_it"] = (
                        "Fallback: audio nel browser (Cloud TTS). Sul cane non è partito nulla — "
                        "verifica RPC voice + env oppure WebRTC come sopra."
                    )
        except Exception as exc:
            payload["tts_openai_error"] = repr(exc)

    if not preview_only:
        hermes_append_turn_log_memory(
            base_cmd=base_cmd,
            reply=reply,
            intent_eff=intent_eff,
            applied=applied,
            dry_run=dry_run,
            body=body,
        )

    return jsonify(merge_http_timing_into_json_dict(payload))


@bp.route("/api/hermes/execute_intent", methods=["POST"])
def api_hermes_execute_intent() -> Any:
    """Esegue un intent Hermes già sanitizzato — tipicamente dopo ``preview_only`` e approvazione UI."""
    if not hermes_enabled():
        return (
            jsonify(
                merge_http_timing_into_json_dict(
                    {
                        "ok": False,
                        "reason": "GO2_ENABLE_HERMES_AGENT_not_enabled",
                        "hint_it": "Esporta GO2_ENABLE_HERMES_AGENT=1 sul processo dashboard (nx_dashboard_env.sh) e riavvia.",
                    }
                )
            ),
            503,
        )
    body = request.get_json(silent=True) or {}
    raw_intent = body.get("intent")
    if not isinstance(raw_intent, dict):
        return (
            jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "missing_intent", "hint_it": 'JSON {"intent":{...}}'})),
            400,
        )
    dry_run = bool(body.get("dry_run"))
    caps = _hermes_capabilities_from_body(body)
    intent_eff, warnings_it = _hermes_sanitize_intent(raw_intent, caps)
    hermes_normalize_intent_reply(intent_eff)
    applied = _hermes_apply_intent(intent_eff, dry_run=dry_run)
    reply = str(intent_eff.get("assistant_reply_it") or "").strip()
    if not reply and warnings_it:
        reply = " ".join(str(w) for w in warnings_it if str(w).strip()).strip()
    if not reply:
        reply = "Approved intent executed — see Technical JSON."

    cap_report = {k: v for k, v in caps.items() if not str(k).startswith("_")}
    payload: dict[str, Any] = {
        "ok": bool(applied.get("ok")),
        "assistant_reply_it": reply,
        "capabilities_effective": cap_report,
        "legacy_capabilities": bool(caps.get("_legacy")),
        "intent": intent_eff,
        "warnings_it": warnings_it,
        "dry_run": applied.get("dry_run"),
        "steps": applied.get("steps"),
        "action_digest_it": hermes_summarize_intent_it(intent_eff),
        "approved_execute": True,
    }

    src_cmd = str(body.get("source_command") or body.get("text") or "").strip()
    hermes_append_turn_log_memory(
        base_cmd=src_cmd or "hermes_approve_execute",
        reply=reply,
        intent_eff=intent_eff,
        applied=applied,
        dry_run=dry_run,
        body=body,
    )

    return jsonify(merge_http_timing_into_json_dict(payload))


@bp.route("/api/grasp_coach/status", methods=["GET"])
def api_grasp_coach_status() -> Any:
    """Stato trial Grasp Coach (OpenAI Chat Completions + vision)."""
    tail = read_recent_grasp_coach_events(200)
    return jsonify(
        merge_http_timing_into_json_dict(
            {
                "ok": True,
                "enabled": grasp_coach_enabled(),
                "model": grasp_coach_model(),
                "go2_local": go2_local(),
                "openai_configured": bool(openai_api_key()),
                "openai_api": "POST /v1/chat/completions",
                "note_it": "Vision multimodale + response_format json_object. Stessa chiave di Hermes.",
                "model_ladder_it": grasp_coach_model_ladder_it(),
                "depth_policy_hint_it": (
                    "GO2_GRASP_COACH_DEPTH_POLICY=alternate consiglia ~1 Hz: RGB ogni step, depth ogni due; "
                    "`always` / `rgb_only` per override."
                ),
                "memory_events_loaded": len(tail),
                "feedback_hint_it": (
                    "Dopo uno step, POST /api/grasp_coach/feedback con feedback_text (e opz. code_correction_note) "
                    "appende alla memoria rolling usata al passo successivo."
                ),
                "timing_hint_it": "Log server: logger go2_dashboard.operator_api.timing (GO2_HTTP_TIMING_LOG). Header X-Dashboard-Server-Ms.",
            }
        )
    )


@bp.route("/api/grasp_coach/step", methods=["POST"])
def api_grasp_coach_step() -> Any:
    """Un passo: cattura RGB (+ depth V4L se configurato), chiama OpenAI, opzionale IK parziale D1."""
    body = request.get_json(silent=True) or {}
    return jsonify(merge_http_timing_into_json_dict(grasp_coach_step(body)))


@bp.route("/api/grasp_coach/preview", methods=["POST"])
def api_grasp_coach_preview() -> Any:
    """Anteprima metrica dalla posa servo attuale (polso Orbbec) — senza LLM e senza movimento."""
    body = request.get_json(silent=True) or {}
    out = grasp_coach_preview_metric(
        instruction=str(body.get("instruction") or body.get("task") or ""),
        start_variant=body.get("start_variant"),
    )
    return jsonify(merge_http_timing_into_json_dict(out))


@bp.route("/api/grasp_coach/feedback", methods=["POST"])
def api_grasp_coach_feedback() -> Any:
    """Feedback operatore sulla sessione (memoria JSONL, incluso nel prompt dello step successivo)."""
    body = request.get_json(silent=True) or {}
    return jsonify(merge_http_timing_into_json_dict(grasp_coach_feedback(body)))


@bp.route("/api/grasp_coach/teach_calib/status", methods=["GET"])
def api_grasp_coach_teach_calib_status() -> Any:
    return jsonify(merge_http_timing_into_json_dict(teach_calib_status()))


@bp.route("/api/grasp_coach/teach_calib/start", methods=["POST"])
def api_grasp_coach_teach_calib_start() -> Any:
    """Calibrazione manuale: detection → 4s hold → rilascio giunti → 15s teach → salva posa."""
    body = request.get_json(silent=True) or {}
    hold_s = body.get("hold_s")
    manual_s = body.get("manual_s")
    try:
        hold_f = float(hold_s) if hold_s is not None else None
    except (TypeError, ValueError):
        hold_f = None
    try:
        manual_f = float(manual_s) if manual_s is not None else None
    except (TypeError, ValueError):
        manual_f = None
    out = teach_calib_start(
        instruction=str(body.get("instruction") or ""),
        hold_s=hold_f,
        manual_s=manual_f,
        require_detection=body.get("require_detection", True) is not False,
    )
    code = 200 if out.get("ok") else 409 if out.get("reason") == "teach_session_active" else 400
    return jsonify(merge_http_timing_into_json_dict(out)), code


@bp.route("/api/grasp_coach/teach_calib/cancel", methods=["POST"])
def api_grasp_coach_teach_calib_cancel() -> Any:
    return jsonify(merge_http_timing_into_json_dict(teach_calib_cancel()))


@bp.route("/api/grasp_coach/teach_calib/samples", methods=["GET", "DELETE"])
def api_grasp_coach_teach_calib_samples() -> Any:
    if request.method == "DELETE":
        return jsonify(merge_http_timing_into_json_dict(teach_calib_clear()))
    return jsonify(merge_http_timing_into_json_dict(teach_calib_list_samples()))


@bp.route("/api/operator_session/memory", methods=["GET"])
def api_operator_session_memory_get() -> Any:
    """Ultime righe memoria missione (JSONL)."""
    try:
        n = int(request.args.get("lines", "80"))
    except ValueError:
        n = 80
    n = max(1, min(n, 400))
    return jsonify(
        merge_http_timing_into_json_dict(
            {"ok": True, "path": "data/operator_session_memory.jsonl", "events": read_recent_operator_session_events(n)}
        )
    )


@bp.route("/api/operator_session/memory", methods=["POST"])
def api_operator_session_memory_post() -> Any:
    """Append evento memoria missione (note operatore, pose, JSON arbitrario limitato)."""
    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "").strip()
    note = str(body.get("note") or body.get("text") or "").strip()
    if not note and not title:
        return jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "missing_note_or_title"})), 400
    tags = body.get("tags")
    if tags is not None and not isinstance(tags, list):
        tags = [str(tags)]
    extra = body.get("data")
    if extra is not None and not isinstance(extra, dict):
        return jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "data_must_be_object"})), 400
    rec: dict[str, Any] = {
        "title": title[:200] if title else None,
        "note": note[:8000] if note else "",
        "tags": [str(t)[:80] for t in tags][:24] if isinstance(tags, list) else None,
    }
    if isinstance(extra, dict):
        rec["data"] = extra
    append_operator_session_event(rec)
    return jsonify(merge_http_timing_into_json_dict({"ok": True, "appended": True}))


@bp.route("/api/hermes/tts", methods=["POST"])
def api_hermes_tts() -> Any:
    """Sintesi vocale (MP3 Base64) per ripetere l'ultimo messaggio dalla UI."""
    if not openai_api_key():
        return (
            jsonify(
                merge_http_timing_into_json_dict(
                    {
                        "ok": False,
                        "reason": "missing_OPENAI_API_KEY",
                        "hint_it": "Serve OPENAI_API_KEY sul processo per TTS cloud.",
                    }
                )
            ),
            503,
        )
    body = request.get_json(silent=True) or {}
    t = str(body.get("text") or "").strip()
    if not t:
        return jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "missing_text"})), 400
    voice = body.get("tts_voice")
    personality_eff = hermes_resolve_personality(body_value=body.get("personality"), use_env=True)
    voice_eff = hermes_effective_tts_voice(body_voice=voice, personality=personality_eff)
    try:
        b64 = openai_tts_mp3_base64(text=t, voice=voice_eff)
    except Exception as exc:
        return jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "tts_failed", "detail": repr(exc)})), 502
    return jsonify(
        merge_http_timing_into_json_dict({"ok": True, "format": "mp3", "audio_base64": b64, "tts_voice_used": voice_eff})
    )


@bp.route("/api/cameras/status", methods=["GET"])
def api_cameras_status() -> Any:
    if go2_local():
        CAMERA_CACHE.start()
    vla_snap = (os.environ.get("GO2_VLA_SNAPSHOT_V4L_INDEX") or "").strip()
    try:
        http_origin = (request.url_root or "").rstrip("/")
    except Exception:
        http_origin = ""
    payload: dict[str, Any] = {
        "ok": True,
        "go2_local": go2_local(),
        "mode": "local-cache" if go2_local() else "ssh-snapshot",
        "cameras": CAMERA_CACHE.stats(),
        "dashboard_http_origin": http_origin or None,
        "http_streams_note_it": (
            "Stream completi solo per log.0 e log.6 (MJPEG/JPEG). "
            "Sotto le due anteprime: frecce per scegliere **quale** nodo V4L Orbbec/Sonix (log.0) o RealSense (log.6) è la sorgente "
            "(``POST /api/cameras/runtime_map``). Se ``GO2_VIDEO_INDEX_0`` / ``_6`` sono nell'ambiente NX, la UI non può sovrascriverli. "
            "Elenco esteso: ``v4l_nodes_detail`` + ``GET /api/cameras/v4l/<N>/preview.jpg``."
        ),
        "openvla_jpeg_urls": {
            "logical_0_jpg": (
                f"{http_origin}/api/robot/camera/0.jpg" if http_origin else "/api/robot/camera/0.jpg"
            ),
            "logical_6_jpg": (
                f"{http_origin}/api/robot/camera/6.jpg" if http_origin else "/api/robot/camera/6.jpg"
            ),
            "vla_frame_jpg": (
                f"{http_origin}/api/robot/vla_frame.jpg" if http_origin else "/api/robot/vla_frame.jpg"
            ),
            "vla_frame_configured": bool(vla_snap),
            "note_it": (
                "Il worker RTX deve raggiungere questi URL via LAN. "
                "Se apri la dashboard come localhost, imposta «Base JPEG» all'IP LAN della NX (es. 192.168.123.18:5052)."
            ),
        },
    }
    if vla_snap:
        try:
            vla_snap_i = int(vla_snap, 10)
        except ValueError:
            vla_snap_i = vla_snap
        payload["openvla_vla_frame"] = {
            "path": "/api/robot/vla_frame.jpg",
            "v4l_index": vla_snap_i,
            "note_it": "Sul worker RTX: WORKER_CAMERA_JPG_URL=http://<NX_IP>:<PORT>/api/robot/vla_frame.jpg",
        }
    if os.environ.get("GO2_ALLOW_RAW_V4L_DEBUG", "0").lower() in ("1", "true", "yes", "on"):
        payload["raw_v4l_debug_note_it"] = (
            "GET /api/debug/robot/v4l/<v4l_index>.jpg — un JPEG da qualsiasi /dev/videoN "
            "presente in v4l_usb_inventory (confronta RGB vs depth/IR senza SDK). "
            "Disattivare GO2_ALLOW_RAW_V4L_DEBUG in produzione."
        )
    if go2_local():
        try:
            inv = v4l_usb_inventory()
            payload["v4l_usb_inventory"] = inv
            payload["camera_summary"] = _operator_camera_summary(payload["cameras"], inv)
            sr_inv = (request.script_root or "").rstrip("/")
            payload["depth_sysfs_hint_nodes"] = _depth_sysfs_hint_rows(
                inv, http_origin=http_origin or "", script_root=sr_inv
            )
        except Exception as exc:
            payload["v4l_usb_inventory_error"] = repr(exc)
            try:
                payload["camera_summary"] = _operator_camera_summary(payload["cameras"], None)
            except Exception:
                pass
    if go2_local() and cv2 is not None:
        ro = get_runtime_v4l_overrides()
        if ro:
            payload["runtime_v4l_by_logical"] = {str(k): int(v) for k, v in sorted(ro.items())}
        payload["video_index_env_lock"] = {
            "0": ("GO2_VIDEO_INDEX_0" in os.environ),
            "6": ("GO2_VIDEO_INDEX_6" in os.environ),
        }
        payload["v4l_pick_candidates"] = {
            "0": v4l_candidates_for_logical_slot(0),
            "6": v4l_candidates_for_logical_slot(6),
        }
        payload["v4l_pick_note_it"] = (
            "Indici ammessi per log.0: tutti i nodi USB Orbbec 2bc5:080b o Sonix 0735:0269. "
            "Per log.6: tutti i nodi Intel RealSense 8086:0b3a. "
            "Scegli di solito il nodo con stream RGB (se vedi IR/depth, prova il successivo)."
        )
        payload["v4l_index_by_logical"] = {str(d): _v4l_index_for_logical_camera(d) for d in CAMERA_DEVICES}
        payload["sysfs_card_name_by_logical"] = {
            str(d): _v4l_sysfs_card_name(_v4l_index_for_logical_camera(d)) for d in CAMERA_DEVICES
        }
        depth_map = {str(d): _depth_v4l_index_for_logical_camera(d) for d in CAMERA_DEVICES}
        if any(v is not None for v in depth_map.values()):
            payload["depth_v4l_index_by_logical"] = depth_map
        auto_m = usb_auto_v4l_mapping()
        if auto_m:
            payload["v4l_usb_auto_map"] = {str(k): int(v) for k, v in sorted(auto_m.items())}
    payload["orbbec_logical_0_probe_debug"] = orbbec_logical0_probe_debug()
    inv_now = payload.get("v4l_usb_inventory")
    if isinstance(inv_now, list):
        payload["orbbec_rgb_v4l_sysfs_hints"] = _orbbec_rgb_sysfs_hints(inv_now)
    rgb_hints = payload.get("orbbec_rgb_v4l_sysfs_hints") or []
    summary = payload.get("camera_summary") or {}
    v4l_by_log = payload.get("v4l_index_by_logical") or {}
    s0 = summary.get("0")
    if (
        rgb_hints
        and isinstance(s0, dict)
        and s0.get("color_ok") is False
        and isinstance(v4l_by_log, dict)
        and "0" in v4l_by_log
    ):
        try:
            cur_idx = int(v4l_by_log["0"])
        except (TypeError, ValueError):
            cur_idx = -1
        for h in rgb_hints:
            try:
                hi = int(h["v4l_index"])
            except (TypeError, ValueError, KeyError):
                continue
            if hi != cur_idx:
                payload["fix_log0_export_hint_sh"] = f"export GO2_VIDEO_INDEX_0={hi}"
                payload["fix_log0_sysfs_name"] = str(h.get("sysfs_name") or "")
                break
    if isinstance(inv_now, list) and go2_local():
        try:
            sr = (request.script_root or "").rstrip("/")
            payload["v4l_nodes_detail"] = _enrich_v4l_nodes_detail(
                inv_now,
                v4l_by_log=payload.get("v4l_index_by_logical"),
                depth_by_log=payload.get("depth_v4l_index_by_logical"),
                http_origin=http_origin or "",
                script_root=sr,
            )
            payload["v4l_nodes_detail_note_it"] = (
                "Tutti i nodi /dev/video* USB: famiglia (Orbbec/RealSense), sysfs, mapping log. 0/6, anteprima JPEG. "
                "depth: solo se imposti GO2_DEPTH_VIDEO_INDEX_0 / _6 sulla NX."
            )
        except Exception as exc:
            payload["v4l_nodes_detail_error"] = repr(exc)
    return jsonify(payload)


@bp.route("/api/cameras/runtime_map", methods=["POST"])
def api_cameras_runtime_map() -> Any:
    """Imposta da UI quali ``/dev/videoN`` usare per log.0 / log.6 (senza ``GO2_VIDEO_INDEX_*`` in env)."""
    if not go2_local():
        return jsonify({"ok": False, "reason": "requires_GO2_LOCAL", "hint_it": "Solo sulla NX con GO2_LOCAL=1."}), 503
    body = request.get_json(silent=True) or {}
    updates: dict[int, int | None] = {}

    if body.get("clear_all"):
        updates = {0: None, 6: None}
    else:
        for k in ("0", "6"):
            if k not in body:
                continue
            v = body[k]
            if v is None or v == "":
                updates[int(k)] = None
            else:
                try:
                    updates[int(k)] = int(v)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "errors": [f"log.{k}: v4l_index non valido: {v!r}"]}), 400
        log_raw = body.get("logical")
        if log_raw is not None and "v4l_index" in body:
            try:
                lg = int(log_raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "errors": [f"logical non valido: {log_raw!r}"]}), 400
            v4l_raw = body.get("v4l_index")
            if v4l_raw is None or v4l_raw == "":
                updates[lg] = None
            else:
                try:
                    updates[lg] = int(v4l_raw)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "errors": [f"v4l_index non valido: {v4l_raw!r}"]}), 400
        if not updates:
            return jsonify(
                {
                    "ok": False,
                    "hint_it": 'JSON es: {"logical":0,"v4l_index":4} oppure {"0":4,"6":10} oppure {"clear_all":true}',
                }
            ), 400

    result = set_runtime_v4l_overrides(updates)
    if not result["ok"]:
        return jsonify(result), 400
    try:
        CAMERA_CACHE.start(0)
        CAMERA_CACHE.start(6)
    except Exception:
        pass
    result["v4l_index_by_logical"] = {str(d): _v4l_index_for_logical_camera(d) for d in CAMERA_DEVICES}
    return jsonify(result)



@bp.route("/api/cameras/v4l/<int:v4l_index>/preview.jpg", methods=["GET"])
def api_cameras_v4l_preview_jpg(v4l_index: int) -> Any:
    """Un JPEG da ``/dev/videoN`` (solo se ``N`` è nell'inventario USB). Senza ``GO2_ALLOW_RAW_V4L_DEBUG``."""
    if not go2_local():
        return Response(
            "requires GO2_LOCAL=1 — see GET /api/health (camera_jpeg_hint)",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "go2_local_off"},
        )
    if cv2 is None:
        return Response(
            "cv2 unavailable — see GET /api/health",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "no_cv2"},
        )
    if not v4l_index_in_usb_inventory(v4l_index):
        return Response("v4l index not in usb inventory", status=404, mimetype="text/plain; charset=utf-8")
    image = debug_v4l_snapshot_jpeg(v4l_index)
    if image is None:
        return Response(
            "frame capture failed",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "capture_failed"},
        )
    return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@bp.route("/api/robot/camera/<int:device>.jpg")
def api_robot_camera_jpg(device: int) -> Any:
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)
    if not go2_local():
        return Response(
            "camera frame unavailable (GO2_LOCAL!=1) — see GET /api/health",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "go2_local_off"},
        )
    if cv2 is None:
        return Response(
            "camera frame unavailable (no cv2) — see GET /api/health",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "no_cv2"},
        )
    image = _robot_camera_jpeg(device)
    if image is None:
        return Response(
            "camera frame unavailable (no frame from cache)",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "no_frame"},
        )
    return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


def _json_safe_for_voice_report(obj: Any) -> Any:
    """Flask ``jsonify`` non accetta bytes / oggetti arbitrari restituiti da RPC."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe_for_voice_report(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe_for_voice_report(x) for x in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")[:8000]
    return str(obj)


@bp.route("/api/robot/voice_test", methods=["POST"])
def api_robot_voice_test() -> Any:
    """Self-test audio Go2: tone sintetico PCM (ffmpeg se c'è, altrimenti generatore Python) e codici RPC ``PlayStream``.

    Richiede ``GO2_VOICE_SELF_TEST_HTTP=1``. Utile da PC: ``python scripts/verify_go2_voice_playback.py http://<NX>:5052``.
    """
    if (os.environ.get("GO2_VOICE_SELF_TEST_HTTP") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return (
            jsonify(
                merge_http_timing_into_json_dict(
                    {
                        "ok": False,
                        "reason": "voice_test_disabled",
                        "hint_it": "Imposta GO2_VOICE_SELF_TEST_HTTP=1 sulla NX e riavvia la dashboard.",
                    }
                )
            ),
            403,
        )
    if not go2_local():
        return (
            jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "needs_go2_local"})),
            503,
        )
    from go2_dashboard.go2_voice_playback import (
        go2_voice_playback_report,
        go2_voice_ttsmaker_report,
        pure_python_beep_pcm_s16le_mono,
        synthetic_beep_pcm_s16le_mono_bytes,
    )

    try:
        body = request.get_json(silent=True) or {}
        mode = str(body.get("mode") or "pcm_beep").strip().lower()
        if mode in {"ttsmaker", "tts_maker", "sdk_tts"}:
            text_def = "Hello. Dashboard voice test. Speaker check."
            text_in = body.get("text") if body.get("text") is not None else body.get("tts_text")
            text_use = text_def if text_in is None else str(text_in)
            sid_raw = body.get("speaker_id")
            sid_use: int | None = None
            if sid_raw is not None:
                try:
                    sid_use = int(sid_raw)
                except (TypeError, ValueError):
                    sid_use = None
            report = go2_voice_ttsmaker_report(text_use, speaker_id=sid_use)
        else:
            b64_in = body.get("audio_mp3_base64")
            if isinstance(b64_in, str) and b64_in.strip():
                report = go2_voice_playback_report(b64_in.strip())
            else:
                try:
                    sr = int((os.environ.get("GO2_GO2_VOICE_SAMPLE_RATE") or "16000").strip())
                except ValueError:
                    sr = 16000
                pcm_b = synthetic_beep_pcm_s16le_mono_bytes(sample_rate=sr)
                if not pcm_b:
                    pcm_b = pure_python_beep_pcm_s16le_mono(sample_rate=sr)
                report = go2_voice_playback_report(pcm_s16le_mono=pcm_b)
        played = bool(report.get("success"))
        rpc_ack = bool(report.get("robot_voice_rpc_ack"))
        tsm_ack = bool(report.get("robot_ttsmaker_ack"))
        payload = {
            "ok": played,
            "played_on_go2": played,
            "robot_voice_rpc_ack": rpc_ack,
            "robot_ttsmaker_ack": tsm_ack,
            "hint_it": (
                "PCM stream: ``robot_voice_rpc_ack`` = tutti ``PlayStream`` codice 0. "
                "Modalità ``mode=ttsmaker``: ``robot_ttsmaker_ack`` = codice ``TtsMaker`` 0 (SDK come esempi G1). "
                "Il repo biscuit-voice-service non usa questo stack: lì il TTS è un callback (es. Piper su PC)."
            ),
            "report": _json_safe_for_voice_report(report),
        }
        return jsonify(merge_http_timing_into_json_dict(payload)), (200 if played else 502)
    except Exception as exc:
        return (
            jsonify(
                merge_http_timing_into_json_dict(
                    {"ok": False, "reason": "voice_test_exception", "detail": repr(exc)}
                )
            ),
            500,
        )


def _webrtc_beep_job() -> None:
    from go2_dashboard.go2_voice_playback import synthetic_beep_mp3_bytes
    from go2_dashboard.go2_voice_webrtc import kill_stale_webrtc_play_procs

    kill_stale_webrtc_play_procs()
    time.sleep(0.5)
    with _LAST_WEBRTC_BEEP_LOCK:
        _LAST_WEBRTC_BEEP.clear()
        _LAST_WEBRTC_BEEP.update(
            {
                "status": "running",
                "started_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "finished_at": None,
                "ok": None,
                "error": None,
            }
        )
    ok = False
    err: str | None = None
    try:
        mp3 = synthetic_beep_mp3_bytes(duration_s=0.45, freq_hz=880)
        if not mp3:
            err = "synthetic_beep_mp3_failed"
        else:
            b64 = base64.b64encode(mp3).decode("ascii")
            prev_retries = os.environ.get("GO2_WEBRTC_AUDIO_RETRIES")
            prev_timeout = os.environ.get("GO2_WEBRTC_AUDIO_SUBPROCESS_TIMEOUT_S")
            prev_max_wait = os.environ.get("GO2_WEBRTC_AUDIO_MAX_WAIT_S")
            os.environ["GO2_WEBRTC_AUDIO_RETRIES"] = "1"
            os.environ["GO2_WEBRTC_AUDIO_SUBPROCESS_TIMEOUT_S"] = "85"
            os.environ["GO2_WEBRTC_AUDIO_MAX_WAIT_S"] = "8"
            try:
                ok = bool(hermes_try_play_tts_mp3_on_go2_webrtc(b64))
            finally:
                if prev_retries is None:
                    os.environ.pop("GO2_WEBRTC_AUDIO_RETRIES", None)
                else:
                    os.environ["GO2_WEBRTC_AUDIO_RETRIES"] = prev_retries
                if prev_timeout is None:
                    os.environ.pop("GO2_WEBRTC_AUDIO_SUBPROCESS_TIMEOUT_S", None)
                else:
                    os.environ["GO2_WEBRTC_AUDIO_SUBPROCESS_TIMEOUT_S"] = prev_timeout
                if prev_max_wait is None:
                    os.environ.pop("GO2_WEBRTC_AUDIO_MAX_WAIT_S", None)
                else:
                    os.environ["GO2_WEBRTC_AUDIO_MAX_WAIT_S"] = prev_max_wait
            if not ok:
                err = "webrtc_playback_failed"
    except Exception as exc:
        err = repr(exc)
        exc_name = type(exc).__name__
        if exc_name in {"AesKeyRequiredError", "AesKeyRejectedError"}:
            err = f"{exc_name}: firmware Go2 ≥1.1.15 richiede UNITREE_AES_128_KEY (unitree-fetch-aes-key)"
    with _LAST_WEBRTC_BEEP_LOCK:
        hint = None
        if err == "webrtc_playback_failed":
            hint = (
                "Chiudi app Unitree sul telefono (un solo client WebRTC), aspetta 15 s, riprova. "
                "Se firmware Go2 ≥ 1.1.15: export UNITREE_AES_128_KEY in nx_secrets_dashboard.sh "
                "(unitree-fetch-aes-key --email ... --device-type Go2 -q)."
            )
        _LAST_WEBRTC_BEEP.update(
            {
                "status": "done",
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "ok": ok,
                "error": err,
                "hint_it": hint,
            }
        )


@bp.route("/api/robot/webrtc_beep", methods=["POST"])
def api_robot_webrtc_beep() -> Any:
    """Beep sul Go2 via WebRTC — eseguito **sulla NX** (processo dashboard), non sul PC.

    Default **async**: risponde subito (202) e riproduce in background (handshake WebRTC ~5–60 s).
    Chiudi l'app Unitree se il cane resta muto (un solo client WebRTC).
    """
    if not go2_local():
        return jsonify(merge_http_timing_into_json_dict({"ok": False, "reason": "needs_go2_local"})), 503
    play_wrtc = (os.environ.get("GO2_HERMES_PLAY_ON_GO2_WEBRTC") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not play_wrtc:
        return (
            jsonify(
                merge_http_timing_into_json_dict(
                    {
                        "ok": False,
                        "reason": "GO2_HERMES_PLAY_ON_GO2_WEBRTC_disabled",
                        "hint_it": "Imposta GO2_HERMES_PLAY_ON_GO2_WEBRTC=1 in nx_dashboard_env.sh",
                    }
                )
            ),
            503,
        )
    body = request.get_json(silent=True) or {}
    async_mode = body.get("async", True)
    if isinstance(async_mode, str):
        async_mode = async_mode.strip().lower() not in {"0", "false", "no", "off", "sync"}
    force = body.get("force") in {True, 1, "1", "true", "yes", "on"}
    _webrtc_beep_expire_stale_running()
    with _LAST_WEBRTC_BEEP_LOCK:
        if _LAST_WEBRTC_BEEP.get("status") == "running" and not force:
            return (
                jsonify(
                    merge_http_timing_into_json_dict(
                        {
                            "ok": False,
                            "reason": "webrtc_beep_already_running",
                            "hint_it": (
                                "Un beep è già in corso sulla NX (~60 s). Attendi e GET /api/robot/webrtc_beep/last, "
                                "oppure POST con {\"force\": true} se è bloccato."
                            ),
                            "last": dict(_LAST_WEBRTC_BEEP),
                        }
                    )
                ),
                409,
            )
    if async_mode:
        threading.Thread(target=_webrtc_beep_job, name="webrtc_beep", daemon=True).start()
        return (
            jsonify(
                merge_http_timing_into_json_dict(
                    {
                        "ok": True,
                        "started": True,
                        "async": True,
                        "host": "nx_dashboard",
                        "hint_it": (
                            "Richiesta accettata sulla Jetson. Il suono può arrivare dopo 5–60 s "
                            "(WebRTC). Poll: GET /api/robot/webrtc_beep/last — chiudi app Unitree se muto."
                        ),
                    }
                )
            ),
            202,
        )
    _webrtc_beep_job()
    with _LAST_WEBRTC_BEEP_LOCK:
        snap = dict(_LAST_WEBRTC_BEEP)
    return jsonify(merge_http_timing_into_json_dict({"ok": bool(snap.get("ok")), **snap})), (200 if snap.get("ok") else 502)


@bp.route("/api/robot/webrtc_beep/last", methods=["GET"])
def api_robot_webrtc_beep_last() -> Any:
    _webrtc_beep_expire_stale_running()
    with _LAST_WEBRTC_BEEP_LOCK:
        snap = dict(_LAST_WEBRTC_BEEP)
    return jsonify(merge_http_timing_into_json_dict({"ok": True, **snap}))


@bp.route("/api/robot/vla_frame.jpg")
def api_robot_vla_frame_jpg() -> Any:
    """Un JPEG da un ``/dev/videoN`` a scelta (es. altro RGB Orbbec) per ``WORKER_CAMERA_JPG_URL`` sul worker."""
    raw = (os.environ.get("GO2_VLA_SNAPSHOT_V4L_INDEX") or "").strip()
    if not raw:
        txt = (
            "Set GO2_VLA_SNAPSHOT_V4L_INDEX to a v4l index listed in GET /api/cameras/status "
            "(v4l_usb_inventory), then point WORKER_CAMERA_JPG_URL to "
            "http://<NX_HOST>:<PORT>/api/robot/vla_frame.jpg"
        )
        return Response(txt, status=404, mimetype="text/plain; charset=utf-8")
    try:
        idx = int(raw, 10)
    except ValueError:
        return Response("invalid GO2_VLA_SNAPSHOT_V4L_INDEX", status=400, mimetype="text/plain; charset=utf-8")
    if not go2_local():
        return Response(
            "vla_frame requires GO2_LOCAL=1 — see GET /api/health",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "go2_local_off"},
        )
    if cv2 is None:
        return Response(
            "cv2 unavailable — see GET /api/health",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "no_cv2"},
        )
    if not v4l_index_in_usb_inventory(idx):
        return Response("v4l index not in usb inventory", status=404, mimetype="text/plain; charset=utf-8")
    image = debug_v4l_snapshot_jpeg(idx)
    if image is None:
        return Response(
            "frame capture failed",
            status=503,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Go2-Reason": "capture_failed"},
        )
    return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@bp.route("/stream/robot/camera/<int:device>.mjpg")
def stream_robot_camera_mjpg(device: int) -> Any:
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)
    period = float(os.environ.get("GO2_MJPEG_FRAME_PERIOD_S", "0.05"))
    if not go2_local():
        period = max(period, 0.12)

    def generate():
        last: bytes | None = None
        first_wait_s = float(os.environ.get("GO2_MJPEG_FIRST_FRAME_WAIT_S", "1.8"))
        while True:
            if go2_local() and cv2 is not None:
                jpg = CAMERA_CACHE.peek_jpeg(device)
                if jpg is None:
                    jpg = _robot_camera_jpeg(device)
                if jpg is None and last is None:
                    jpg = CAMERA_CACHE.get_jpeg(device, wait_s=first_wait_s)
            else:
                jpg = _robot_camera_jpeg(device)
            if jpg is None:
                jpg = last
            if jpg is not None:
                last = jpg
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + jpg + b"\r\n"
                )
            time.sleep(period)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@bp.route("/api/debug/robot/v4l/<int:v4l_index>.jpg", methods=["GET"])
def api_debug_v4l_raw_jpg(v4l_index: int) -> Any:
    """Lab: ispeziona ``/dev/videoN`` quando la dashboard espone solo log. 0/6 (attiva con env)."""
    if os.environ.get("GO2_ALLOW_RAW_V4L_DEBUG", "0").lower() not in ("1", "true", "yes", "on"):
        return Response("disabled", status=404)
    if not go2_local() or cv2 is None:
        return Response("requires GO2_LOCAL=1 and cv2", status=503)
    if not v4l_index_in_usb_inventory(v4l_index):
        return Response("v4l index not in usb inventory", status=404)
    image = debug_v4l_snapshot_jpeg(v4l_index)
    if image is None:
        return Response("frame unavailable", status=503)
    return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@bp.route("/api/vision/box_detect", methods=["GET"])
def api_vision_box_detect() -> Any:
    """Un frame dalla cache camera → ``box_object_detector`` (YOLO se ``GO2_YOLO_MODEL``, altrimenti euristica OpenCV)."""
    if not go2_local() or cv2 is None or np is None:
        return jsonify({"ok": False, "reason": "requires_nx_cv2_numpy"}), 503
    try:
        dev = int(request.args.get("camera", "6"))
    except ValueError:
        return jsonify({"ok": False, "reason": "bad_camera"}), 400
    if dev not in CAMERA_DEVICES:
        return jsonify({"ok": False, "reason": "camera_not_allowed"}), 400
    CAMERA_CACHE.start(dev)
    jpg = CAMERA_CACHE.get_jpeg(dev, wait_s=2.5)
    if not jpg:
        return jsonify({"ok": False, "reason": "no_frame", "logical_camera": dev}), 503
    buf = np.frombuffer(jpg, dtype=np.uint8)
    frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify({"ok": False, "reason": "jpeg_decode_failed"}), 503
    h, w = int(frame.shape[0]), int(frame.shape[1])
    s_scripts = str(PROJECT_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    try:
        from box_object_detector import detect_box_object, detector_status
    except Exception as exc:
        return jsonify({"ok": False, "reason": "detector_import_failed", "detail": repr(exc)}), 503
    det = detect_box_object(frame)
    st = detector_status()
    slim = {k: st.get(k) for k in ("model_path", "model_exists", "model_family", "training_scope", "classic_fallback_enabled")}
    return jsonify(
        {
            "ok": True,
            "logical_camera": dev,
            "frame_size_px": [w, h],
            "detection": det,
            "detector_status": slim,
        }
    )


@bp.route("/api/nx/stack/status", methods=["GET"])
def api_nx_stack_status() -> Any:
    return jsonify({"ok": True, **nx_stack_status()})


@bp.route("/api/nx/stack/start", methods=["POST"])
def api_nx_stack_start() -> Any:
    if not go2_local():
        return jsonify({"ok": False, "reason": "GO2_LOCAL!=1", **nx_stack_status()}), 400
    CAMERA_CACHE.start(0)
    CAMERA_CACHE.start(6)
    return jsonify({"ok": True, "message": "Camera cache avviata (0,6).", **nx_stack_status()})


@bp.route("/api/base/sport_last", methods=["GET"])
def api_base_sport_last() -> Any:
    return jsonify(sport_last_payload())


@bp.route("/api/base/accompany_mode", methods=["GET", "POST"])
def api_base_accompany_mode() -> Any:
    payload, code = accompany_mode_handle(request)
    return jsonify(payload), code


@bp.route("/api/alignment/start_pose", methods=["GET", "POST"])
def api_alignment_start_pose() -> Any:
    if request.method == "GET":
        variant = normalize_start_variant(request.args.get("start_variant"))
        start_path = resolve_start_alignment_path(variant)
        if not start_path.exists():
            return (
                jsonify(
                    {
                        "ok": False,
                        "reason": "no_saved_start_pose",
                        "start_variant": variant,
                        "path": str(start_path),
                        "status": start_alignment_status(),
                    }
                ),
                404,
            )
        try:
            data = json.loads(start_path.read_text(encoding="utf-8"))
            return jsonify(
                {
                    "ok": True,
                    "start_variant": variant,
                    "path": str(start_path),
                    "start_pose": data,
                    "status": start_alignment_status(),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "reason": repr(exc)}), 500
    body = request.get_json(silent=True) or {}
    variant = normalize_start_variant(body.get("start_variant"))
    sd = body.get("servo_deg")
    if isinstance(sd, list) and len(sd) >= 6:
        out = save_start_alignment_json(servo_deg=[float(x) for x in sd], start_variant=variant)
    else:
        out = save_start_alignment_json(start_variant=variant)
    code = 200 if out.get("ok") else 503
    return jsonify(out), code


@bp.route("/api/arm/scene_meshes/<kind>/<path:filename>", methods=["GET"])
def api_arm_scene_meshes(kind: str, filename: str) -> Any:
    """Serve .obj (Go2) o .STL (D1) per Three.js — stesso schema del monolite."""
    return send_scene_mesh_file(kind, filename)


@bp.route("/api/arm/scene_3d", methods=["GET"])
def api_arm_scene_3d() -> Any:
    fast = request.args.get("fast", "").strip().lower() in ("1", "true", "yes")
    resp = jsonify(build_scene_3d_payload(geometry_fast=fast))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@bp.route("/api/arm/last_plan_debug", methods=["GET"])
def api_arm_last_plan_debug() -> Any:
    """Riassunto ultimo piano in cache: target IK, backend, avviso stub (senza muovere il braccio)."""
    plan = get_last_grasp_plan()
    if not isinstance(plan, dict):
        return jsonify({"ok": False, "reason": "no_cached_plan"})
    tgt = pick_tool_target_base_link_m_from_plan(plan)
    be = plan.get("backend")
    stub = be == "stub"
    hint = None
    if stub:
        hint = (
            "Worker in modalità stub: grasp_display_base_link_m è fisso / fittizio, NON dalla camera. "
            "Non eseguire IK finché non usi planner o openvla sulla RTX."
        )
    od = plan.get("openvla_debug")
    if not isinstance(od, dict):
        od = None
    hm_png = plan.get("openvla_heatmap_png_b64")
    has_act = isinstance(plan.get("openvla_action_7dof"), (list, tuple)) and len(
        plan.get("openvla_action_7dof") or []
    ) >= 6
    d1_rad = plan.get("openvla_joint_space") == "d1_rad"
    ik_executable = bool(plan.get("ok")) and tgt is not None
    openvla_d1_executable = bool(plan.get("ok")) and d1_rad and has_act
    motion_hint = None
    if not stub and bool(plan.get("ok")) and not ik_executable and not openvla_d1_executable:
        motion_hint = (
            "Il piano non contiene né un punto 3D base_link usabile per IK "
            "(grasp_display_base_link_m / openvla_fk_tool_tip_base_link_m / …) "
            "né openvla_joint_space=d1_rad con openvla_action_7dof — il braccio non può eseguire da questo JSON."
        )
    return jsonify(
        {
            "ok": bool(plan.get("ok")),
            "plan_ok": bool(plan.get("ok")),
            "backend": be,
            "stub_plan": stub,
            "hint_it": hint,
            "ik_executable": ik_executable,
            "openvla_d1_executable": openvla_d1_executable,
            "hint_motion_it": motion_hint,
            "grasp_display_base_link_m": plan.get("grasp_display_base_link_m"),
            "openvla_fk_tool_tip_base_link_m": plan.get("openvla_fk_tool_tip_base_link_m"),
            "picked_target_base_link_m_for_ik": tgt,
            "image_url_used": plan.get("image_url_used"),
            "openvla_joint_space": plan.get("openvla_joint_space"),
            "has_openvla_action_7dof": isinstance(plan.get("openvla_action_7dof"), (list, tuple))
            and len(plan.get("openvla_action_7dof") or []) >= 6,
            "openvla_debug": od,
            "operators_debug_bbox_norm": plan.get("operators_debug_bbox_norm"),
            "openvla_bbox_norm": plan.get("openvla_bbox_norm"),
            "openvla_bbox_xyxy_pixels": plan.get("openvla_bbox_xyxy_pixels"),
            "has_openvla_heatmap_gaussian": bool(plan.get("openvla_heatmap_gaussian")),
            "has_openvla_heatmap_png": bool(isinstance(hm_png, str) and hm_png.strip()),
        }
    )


@bp.route("/api/arm/goto_home", methods=["POST"])
def api_arm_goto_home() -> Any:
    """Riporta i 7 servo alla posa home (default 0°). Config remota: ``D1_HOME_SERVO_DEG_7``."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "ARM_GOTO_HOME":
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "confirm_required",
                    "hint_it": 'Invia JSON {"confirm":"ARM_GOTO_HOME"}.',
                }
            ),
            400,
        )
    out = goto_home_servo_deg(delay_ms=_arm_post_delay_ms(body))
    code = 200 if out.get("ok") else 503
    return jsonify(out), code


@bp.route("/api/arm/servo_snapshot", methods=["GET"])
def api_arm_servo_snapshot() -> Any:
    """Solo lettura angoli servo D1 (feedback DDS) — come ``/api/arm/servo_snapshot`` del monolite.

    Query ``diag=1``: include ``servo_feedback_diag`` (backend subprocess, righe ``servo_angles``,
    spread intra-lettura) per capire se i numeri sono plausibili o «congelati».
    """
    cur, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    want_diag = (request.args.get("diag") or "").strip().lower() in {"1", "true", "yes", "on"}
    if cur is None:
        out: dict[str, Any] = {
            "ok": False,
            "reason": "no_servo_feedback",
            "hint": "Verifica bin/d1_sdk_feedback (o d1_arm_feedback_helper) e DDS sul robot.",
            "motion_backend": d1_arm_motion.motion_backend_name(),
        }
        if want_diag:
            out["servo_feedback_diag"] = diag
        return jsonify(out), 503
    out_ok: dict[str, Any] = {
        "ok": True,
        "servo_deg": [round(float(v), 3) for v in cur[:7]],
        "saved_at": _op_now_iso(),
    }
    if want_diag:
        safe_diag = {k: v for k, v in diag.items()}
        out_ok["servo_feedback_diag"] = safe_diag
    return jsonify(out_ok)


@bp.route("/api/arm/joints/session_begin", methods=["POST"])
def api_arm_joints_session_begin() -> Any:
    """Avvia sessione jog DDS persistente (come dashboard ``d1_jog`` su 5053)."""
    body = request.get_json(silent=True) or {}
    sd_raw = body.get("servo_deg")
    servo: list[float] | None = None
    if isinstance(sd_raw, list) and len(sd_raw) >= 6:
        try:
            servo = [float(x) for x in sd_raw[:7]]
            while len(servo) < 7:
                servo.append(servo[-1])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "servo_deg must be numeric"}), 400
    out = d1_arm_motion.begin_live_session(servo_deg=servo)
    code = 200 if out.get("ok") else 502
    return jsonify(out), code


@bp.route("/api/arm/joints/session_end", methods=["POST"])
def api_arm_joints_session_end() -> Any:
    """Chiude sessione jog e mantiene coppia sulla posa cache."""
    out = d1_arm_motion.end_live_session()
    return jsonify(out), 200


@bp.route("/api/arm/joints/release", methods=["POST"])
def api_arm_joints_release() -> Any:
    """Rilascia coppia motori (funcode 5 mode 0) — giunti liberi per teach manuale."""
    from go2_dashboard.d1_jog import service as jog_svc

    out = jog_svc.motor_release()
    code = 200 if out.get("ok") else 409
    return jsonify(out), code


@bp.route("/api/arm/joints/couple", methods=["POST"])
def api_arm_joints_couple() -> Any:
    """Abilita coppia motori (funcode 5) — equivalente «Coppia ON» jog."""
    from go2_dashboard.d1_jog import service as jog_svc

    body = request.get_json(silent=True) or {}
    with_power = bool(body.get("with_power", False))
    if with_power:
        out = jog_svc.ensure_coupled(with_power=True, force=bool(body.get("force")))
    else:
        out = d1_arm_motion.ensure_coupled_for_motion()
    code = 200 if out.get("ok") or out.get("skipped") else 502
    return jsonify(out), code


@bp.route("/api/arm/joints/live_deg", methods=["POST"])
def api_arm_joints_live_deg() -> Any:
    """Slider real-time: burst DDS (funcode 5 + 2), senza interpolazione."""
    body = request.get_json(silent=True) or {}
    sd_raw = body.get("servo_deg")
    if not isinstance(sd_raw, list) or len(sd_raw) < 6:
        return jsonify({"ok": False, "reason": "servo_deg must be list of 6–7 numbers"}), 400
    try:
        sd = [float(x) for x in sd_raw[:7]]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "servo_deg must be numeric"}), 400
    while len(sd) < 7:
        sd.append(sd[-1])
    try:
        out = publish_live_pose_deg7(sd)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify({"ok": bool(out.get("ok")) or bool(out.get("skipped")), **out}), code
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 502


@bp.route("/api/arm/joints/goto_deg", methods=["POST"])
def api_arm_joints_goto_deg() -> Any:
    """POST JSON {\"servo_deg\":[7 floats], \"delay_ms\":optional, \"max_step_deg\":\"a,b,...\" optional}."""
    body = request.get_json(silent=True) or {}
    sd_raw = body.get("servo_deg")
    if not isinstance(sd_raw, list) or len(sd_raw) < 6:
        return jsonify({"ok": False, "reason": "servo_deg must be a list of at least 6 numbers (deg)"}), 400
    try:
        sd = [float(x) for x in sd_raw[:7]]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "servo_deg values must be numeric"}), 400
    while len(sd) < 7:
        sd.append(sd[-1])
    mstep = _parse_goto_max_step_deg(body)
    delay_ms = _arm_post_delay_ms(body)
    try:
        out = publish_goto_servo_deg7(sd, max_step_deg=mstep, delay_ms=delay_ms)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify({"ok": bool(out.get("ok")) or bool(out.get("skipped")), **out}), code
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 502


@bp.route("/api/arm/joints/move_one", methods=["POST"])
def api_arm_joints_move_one() -> Any:
    """POST {\"joint_index\":0-6, \"angle_deg\":float}."""
    body = request.get_json(silent=True) or {}
    try:
        ji = int(body.get("joint_index"))
        ad = float(body.get("angle_deg"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "joint_index (int) and angle_deg (float) required"}), 400
    try:
        out = publish_move_one_joint_deg(ji, ad)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify({"ok": bool(out.get("ok")) or bool(out.get("skipped")), **out}), code
    except Exception as exc:
        return jsonify({"ok": False, "reason": repr(exc)}), 502


@bp.route("/api/arm/true_zero", methods=["GET", "POST"])
def api_arm_true_zero() -> Any:
    """
    Posa ZERO (file ``data/true_zero_pose.json``): GET metadati; POST op save | goto_zero | goto_start | goto_saved_start.
    """
    if request.method == "GET":
        exists = TRUE_ZERO_POSE_PATH.is_file()
        out: dict[str, Any] = {"ok": True, "exists": exists, "path": str(TRUE_ZERO_POSE_PATH)}
        if exists:
            try:
                raw = json.loads(TRUE_ZERO_POSE_PATH.read_text(encoding="utf-8"))
                out["saved_at"] = raw.get("saved_at")
                out["label"] = raw.get("label")
                arm = raw.get("arm") or raw.get("arm_at_start") or {}
                if isinstance(arm.get("servo_deg"), list):
                    out["servo_deg"] = arm["servo_deg"]
            except Exception as exc:
                out["read_error"] = repr(exc)
        return jsonify(out)

    body = request.get_json(silent=True) or {}
    op = str(body.get("op") or "").strip().lower()
    if op == "save":
        if not go2_local():
            return (
                jsonify(
                    {
                        "ok": False,
                        "reason": "GO2_LOCAL=1 required on the robot to write true_zero_pose.json.",
                    }
                ),
                400,
            )
        try:
            body_sd = body.get("servo_deg")
            ovr = [float(x) for x in body_sd] if isinstance(body_sd, list) and len(body_sd) >= 6 else None
            payload = save_true_zero_pose_json(servo_deg_override=ovr, angle2_deg=None)
            if not payload.get("ok"):
                return jsonify(payload), 400
            return jsonify(payload)
        except Exception as exc:
            return jsonify({"ok": False, "reason": repr(exc)}), 500

    if op not in {"goto_zero", "goto_start", "goto_saved_start"}:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "unknown_op",
                    "hint": "op: save | goto_zero | goto_start | goto_saved_start",
                }
            ),
            400,
        )

    if op == "goto_zero":
        result = goto_true_zero_from_json(delay_ms=_arm_post_delay_ms(body))
        payload, code = _true_zero_motion_http_response_lite(result)
        return jsonify(payload), code

    if op == "goto_saved_start":
        result = goto_saved_start_from_json(
            delay_ms=_arm_post_delay_ms(body),
            start_variant=body.get("start_variant"),
        )
        payload, code = _true_zero_motion_http_response_lite(result)
        return jsonify(payload), code

    result = goto_true_zero_then_saved_start_from_json(delay_ms=_arm_post_delay_ms(body))
    payload, code = _true_zero_motion_http_response_lite(result)
    return jsonify(payload), code


@bp.route("/api/arm/file_poses_status", methods=["GET"])
def api_arm_file_poses_status() -> Any:
    """Stato dei file ``true_zero_pose.json`` e preset START (laterale/frontale) sulla macchina."""
    tz = TRUE_ZERO_POSE_PATH.is_file()
    st_status = start_alignment_status()
    return jsonify(
        {
            "ok": True,
            "true_zero_pose_json": tz,
            "true_zero_pose_path": str(TRUE_ZERO_POSE_PATH),
            "start_alignment_json": st_status["variants"]["lateral"]["resolved_exists"],
            "start_alignment_path": str(ALIGNMENT_START_PATH),
            "start_alignment": st_status,
        }
    )


@bp.route("/api/arm/at_start_check", methods=["GET"])
def api_arm_at_start_check() -> Any:
    """Il braccio è sulla START salvata? (confronto servo feedback vs preset scelto)."""
    variant = normalize_start_variant(request.args.get("start_variant"))
    out = check_at_saved_start_pose(start_variant=variant)
    code = 200 if out.get("ok") else 409
    return jsonify(out), code


@bp.route("/api/arm/goto_true_zero", methods=["POST"])
def api_arm_goto_true_zero() -> Any:
    """Vai a ``data/true_zero_pose.json`` (posa ZERO registrata)."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "ARM_GOTO_TRUE_ZERO":
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "confirm_required",
                    "hint_it": 'Invia JSON {"confirm":"ARM_GOTO_TRUE_ZERO"}.',
                }
            ),
            400,
        )
    out = goto_true_zero_from_json(delay_ms=_arm_post_delay_ms(body))
    code = 200 if out.get("ok") else 503
    return jsonify(out), code


@bp.route("/api/arm/goto_saved_start", methods=["POST"])
def api_arm_goto_saved_start() -> Any:
    """Vai a ``data/start_alignment.json`` → ``arm_at_start``."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "ARM_GOTO_SAVED_START":
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "confirm_required",
                    "hint_it": 'Invia JSON {"confirm":"ARM_GOTO_SAVED_START"}.',
                }
            ),
            400,
        )
    out = goto_saved_start_from_json(
        delay_ms=_arm_post_delay_ms(body),
        start_variant=body.get("start_variant"),
    )
    code = 200 if out.get("ok") else 503
    return jsonify(out), code


@bp.route("/api/arm/goto_zero_then_start", methods=["POST"])
def api_arm_goto_zero_then_start() -> Any:
    """Esegue in sequenza movimento verso ZERO file poi verso START file."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "ARM_GOTO_ZERO_THEN_START":
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "confirm_required",
                    "hint_it": 'Invia JSON {"confirm":"ARM_GOTO_ZERO_THEN_START"}.',
                }
            ),
            400,
        )
    out = goto_true_zero_then_saved_start_from_json(
        delay_ms=_arm_post_delay_ms(body),
        start_variant=body.get("start_variant"),
    )
    code = 200 if out.get("ok") else 503
    return jsonify(out), code


@bp.route("/api/arm/emergency_hold", methods=["POST"])
def api_arm_emergency_hold() -> Any:
    """E-stop software braccio D1: interrompe helper in volo e ripete comandi alla posa letta (best-effort)."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "ARM_ESTOP_HOLD":
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "confirm_required",
                    "hint_it": 'Invia JSON {"confirm":"ARM_ESTOP_HOLD"}.',
                }
            ),
            400,
        )
    out = arm_emergency_stop_hold()
    code = 200 if out.get("ok") else 503
    return jsonify(out), code


@bp.route("/api/arm/execute_last_plan_ik", methods=["POST"])
def api_arm_execute_last_plan_ik() -> Any:
    """IK verso il punto 3D dell'ultimo ``/api/grasp/plan`` (``grasp_display_base_link_m`` o FK)."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "MOVE_IK_CACHED":
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "confirm_required",
                    "hint_it": 'Invia JSON {"confirm":"MOVE_IK_CACHED"}.',
                }
            ),
            400,
        )
    plan = get_last_grasp_plan()
    if not isinstance(plan, dict) or not plan.get("ok"):
        return jsonify({"ok": False, "reason": "no_cached_plan", "hint_it": "Prima POST /api/grasp/plan ok."}), 400
    xyz = pick_tool_target_base_link_m_from_plan(plan)
    if xyz is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "no_target_xyz_in_plan",
                    "hint_it": "Serve grasp_display_base_link_m, openvla_fk_tool_tip_base_link_m o operators_grasp_points_base_link_m.",
                }
            ),
            400,
        )
    out = goto_tool_target_base_link_m(xyz, delay_ms=None)
    code = 200 if out.get("ok") else 503
    return jsonify(out), code


@bp.route("/api/arm/openvla_execute_last_plan_d1", methods=["POST"])
def api_arm_openvla_execute_last_plan_d1() -> Any:
    """Esegue su DDS gli ultimi 6 giunti ``openvla_action_7dof`` se il piano ha ``openvla_joint_space=d1_rad``.

    POST JSON: ``{"confirm":"MOVE_D1_OPENVLA"}`` (obbligatorio). Richiede sulla NX:
    ``GO2_LOCAL=1``, ``GO2_ENABLE_REAL_ARM=1`` e uno tra
    ``GO2_ENABLE_ARM_PLAN_EXECUTE=1`` (consigliato) / ``GO2_ENABLE_OPENVLA_ARM_EXECUTE=1``.
    Il worker deve avere ``OPENVLA_ACTION_FK_JOINTS=1`` così i primi 6 numeri sono q in radianti D1.
    """
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "MOVE_D1_OPENVLA":
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "confirm_required",
                    "hint_it": 'Invia JSON {"confirm":"MOVE_D1_OPENVLA"}.',
                }
            ),
            400,
        )
    plan = get_last_grasp_plan()
    if not isinstance(plan, dict) or not plan.get("ok"):
        return jsonify({"ok": False, "reason": "no_cached_plan", "hint_it": "Prima POST /api/grasp/plan ok."}), 400
    if plan.get("openvla_joint_space") != "d1_rad":
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "plan_not_d1_rad_joint_space",
                    "hint_it": "Worker con OPENVLA_ACTION_FK_JOINTS=1 così il piano include openvla_joint_space=d1_rad.",
                }
            ),
            400,
        )
    act = plan.get("openvla_action_7dof")
    if not isinstance(act, (list, tuple)) or len(act) < 6:
        return jsonify({"ok": False, "reason": "no_openvla_action_7dof"}), 400
    try:
        jr = [float(act[i]) for i in range(6)]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "bad_joint_numbers"}), 400
    out = goto_joints_rad_clamped_six(jr)
    code = 200 if out.get("ok") else 503
    return jsonify(out), code


@bp.route("/api/arm/grasp_pipeline", methods=["GET"])
def api_arm_grasp_pipeline() -> Any:
    resp = jsonify(build_grasp_pipeline_stub())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@bp.route("/api/mission/box_pick_cycle", methods=["POST"])
def api_mission_box_pick_cycle() -> Any:
    """Disabilitato: il ciclo richiedeva piano grasp esterno (rimosso dalla build)."""
    return (
        jsonify(
            {
                "ok": False,
                "reason": "box_pick_cycle_disabled",
                "hint_it": "Ciclo laboratorio dismesso: usa Hermes e il tab Moto (giunti).",
            }
        ),
        410,
    )


@bp.route("/api/mission/console", methods=["GET"])
def api_mission_console() -> Any:
    """Stazione di controllo: dashboard + stack NX + stato proxy grasp + env sicure."""
    grasp = grasp_health_payload()
    stack = nx_stack_status()
    summary = {
        "dashboard_pid": os.getpid(),
        "stack_ok": bool(stack.get("command_stack", {}).get("ok")),
        "grasp_worker": _mission_worker_summary(grasp),
    }
    return jsonify(
        {
            "ok": True,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": summary,
            "dashboard": {
                "operator_dashboard": True,
                "pid": os.getpid(),
                "process_started_at": _PROCESS_STARTED_AT,
            },
            "nx_stack": stack,
            "grasp": grasp,
            "env": _mission_env_safe(),
            "restart": _mission_restart_instructions(),
        }
    )


@bp.route("/api/mission/dashboard_restart", methods=["POST"])
def api_mission_dashboard_restart() -> Any:
    """Riavvio soft: termina solo ``serve_dashboard_lite.py``; ``nx_dashboard_supervise.sh`` lo rilancia."""
    if not go2_local():
        return jsonify({"ok": False, "reason": "only_on_nx_GO2_LOCAL"}), 400
    if not (os.environ.get("GO2_MISSION_ADMIN_TOKEN") or "").strip():
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "GO2_MISSION_ADMIN_TOKEN_not_set",
                    "hint_it": "Imposta GO2_MISSION_ADMIN_TOKEN in nx_dashboard_env.sh poi redeploy / riavvio manuale.",
                }
            ),
            501,
        )
    if not _mission_admin_token_matches():
        return jsonify({"ok": False, "reason": "invalid_or_missing_token"}), 403
    threading.Thread(target=_nx_dashboard_delayed_pkill, name="mission-dashboard-restart", daemon=True).start()
    return (
        jsonify(
            {
                "ok": True,
                "accepted": True,
                "async": True,
                "hint_it": "Il processo dashboard verrà terminato; il supervisore NX lo rilancerà entro pochi secondi.",
            }
        ),
        202,
    )


@bp.route("/api/arm/calibration_flow", methods=["GET"])
def api_arm_calibration_flow() -> Any:
    """Guida testuale + stato file calibrazione tag5 (stesso contratto della monolite, senza importarla)."""
    resp = jsonify(t5.calibration_flow_payload())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@bp.route("/api/arm/tag5_calibration", methods=["GET", "POST", "DELETE"])
def api_arm_tag5_calibration() -> Any:
    if request.method == "GET":
        dual = request.args.get("dual_probe", "").lower() in ("1", "true", "yes", "on")
        payload, code = t5.handle_tag5_calibration_get(dual_probe=dual)
        return jsonify(payload), code
    if request.method == "DELETE":
        payload, code = t5.handle_tag5_calibration_delete()
        return jsonify(payload), code
    body = request.get_json(silent=True) or {}
    payload, code = t5.handle_tag5_calibration_post(body)
    return jsonify(payload), code


@bp.route("/api/arm/tag5_preview.jpg", methods=["GET"])
def api_arm_tag5_preview_jpg() -> Any:
    """Anteprima JPEG con overlay AprilTag (id 5 arancione) — ``device`` = 0 (polso) o 6 (RealSense)."""
    try:
        dev = int(request.args.get("device", 0))
    except (TypeError, ValueError):
        dev = 0
    if not go2_local():
        r = make_response(
            jsonify({"ok": False, "error": "go2_local_off", "logical_device": dev}),
            503,
        )
        r.headers["X-Go2-Reason"] = "go2_local_off"
        return r
    if cv2 is None:
        r = make_response(
            jsonify({"ok": False, "error": "no_cv2", "logical_device": dev}),
            503,
        )
        r.headers["X-Go2-Reason"] = "no_cv2"
        return r
    blob, meta = t5.tag5_preview_jpeg_and_meta(logical_device=dev)
    if blob is None:
        r = make_response(jsonify(meta), 503)
        r.headers["X-Go2-Reason"] = str(meta.get("error", "unknown"))
        return r
    resp = Response(blob, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["X-Tag5-Seen"] = "1" if meta.get("tag5_seen") else "0"
    resp.headers["X-Logical-Camera"] = str(meta.get("logical_device", dev))
    return resp


@bp.route("/api/arm/tag_calibration_shared_dual", methods=["GET", "POST"])
def api_arm_tag_calibration_shared_dual() -> Any:
    if request.method == "GET":
        payload, code = t5.handle_tag_calibration_shared_dual_get()
        return jsonify(payload), code
    body = request.get_json(silent=True) or {}
    payload, code = t5.handle_tag_calibration_shared_dual_post(body)
    return jsonify(payload), code
