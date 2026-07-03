"""Route Hermes: pagina chat + API chat/voce."""

from __future__ import annotations

import os
import time
from typing import Any

from flask import Blueprint, Response, jsonify, render_template, request

from go2_dashboard.hermes.agent import hermes_reply
from go2_dashboard.hermes.context import build_robot_context, hermes_capabilities, operator_base
from go2_dashboard.hermes.speech import speak

bp = Blueprint("hermes", __name__)

_CHAT_HISTORY: list[dict[str, Any]] = []
_MAX_HISTORY = int(os.environ.get("HERMES_CHAT_HISTORY_MAX", "40"))


@bp.route("/")
def index() -> str:
    integrated = os.environ.get("GO2_HERMES_INTEGRATED", "0").lower() in {"1", "true", "yes", "on"}
    return render_template(
        "hermes.html",
        port=int(os.environ.get("D1_JOG_PORT", "5056")) if integrated else int(os.environ.get("HERMES_PORT", "5056")),
    )


@bp.route("/api/hermes/health", methods=["GET"])
def health() -> Response:
    from go2_dashboard.hermes.speech import last_speak_status, list_canned_status
    from go2_dashboard.hermes.tts_local import tts_status

    ctx = build_robot_context()
    caps = hermes_capabilities()
    return jsonify(
        {
            "ok": True,
            "service": "hermes",
            "dashboard_url": f"http://{os.environ.get('GO2_HOST', '127.0.0.1')}:{os.environ.get('D1_JOG_PORT', '5056')}/",
            "operator_url": operator_base(),
            "operator_reachable": ctx.get("operator_reachable"),
            "operator_required": caps.get("operator_required"),
            "standalone": caps.get("standalone"),
            "integrated": caps.get("integrated"),
            "capabilities": caps,
            "tts": tts_status(),
            "interaction_voice": os.environ.get("HERMES_INTERACTION_VOICE", "1"),
            "vision_speak_detail": os.environ.get("HERMES_VISION_SPEAK_DETAIL", "1"),
            "canned_audio": list_canned_status(),
            "last_speak": last_speak_status(),
        }
    )


@bp.route("/api/hermes/chat", methods=["POST"])
def chat() -> Response:
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "reason": "message required"}), 400

    history = body.get("history")
    if not isinstance(history, list):
        history = [{"role": t["role"], "content": t["content"]} for t in _CHAT_HISTORY[-16:]]

    t0 = time.perf_counter()
    out = hermes_reply(message, history)
    out["latency_s"] = round(time.perf_counter() - t0, 3)

    speak_async = body.get("speak_async")
    if speak_async is None:
        speak_async = os.environ.get("HERMES_SPEAK_ASYNC", "1").lower() in {"1", "true", "yes", "on"}

    if out.get("speech_handled") or (out.get("meta") or {}).get("speech_handled"):
        out["speech"] = {"ok": True, "script": True, "queued": True, "interaction": True}
    elif body.get("speak", True) and out.get("reply"):
        from go2_dashboard.hermes.interaction import _voice_from_vision

        voice_text = _voice_from_vision(str(out["reply"]))
        out["speech"] = speak(voice_text or str(out["reply"]), async_mode=bool(speak_async))
        out["voice_spoken"] = voice_text
    else:
        out["speech"] = {"ok": True, "skipped": True}

    _CHAT_HISTORY.append({"role": "user", "content": message, "ts": time.time()})
    _CHAT_HISTORY.append({"role": "assistant", "content": out.get("reply", ""), "ts": time.time()})
    del _CHAT_HISTORY[: -_MAX_HISTORY]

    return jsonify(out)


@bp.route("/api/hermes/speech/stop", methods=["POST"])
def speech_stop() -> Response:
    from go2_dashboard.hermes.speech import clear_voice_queue, voice_queue_status

    dropped = clear_voice_queue(kill_webrtc=True)
    return jsonify({"ok": True, "dropped": dropped, **voice_queue_status()})
