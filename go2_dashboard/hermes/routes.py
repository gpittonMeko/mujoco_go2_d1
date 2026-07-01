"""Route Hermes: pagina chat + API chat/voce."""

from __future__ import annotations

import os
import time
from typing import Any

from flask import Blueprint, Response, jsonify, request

from go2_dashboard.hermes.agent import hermes_reply
from go2_dashboard.hermes.context import build_robot_context, hermes_capabilities, operator_base
from go2_dashboard.hermes.speech import speak

bp = Blueprint("hermes", __name__)

_CHAT_HISTORY: list[dict[str, Any]] = []
_MAX_HISTORY = int(os.environ.get("HERMES_CHAT_HISTORY_MAX", "40"))


def _standalone_service() -> bool:
    return os.environ.get("GO2_HERMES_STANDALONE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _dashboard_port() -> int:
    return int(os.environ.get("GO2_DASHBOARD_PORT", os.environ.get("HERMES_PORT", "5052")))


# NON registrare GET / su questo blueprint: è montato anche su :5052 (lite_app).
# La home standalone sta in create_hermes_app().


@bp.route("/api/hermes/health", methods=["GET"])
def health() -> Response:
    from go2_dashboard.hermes.speech import last_speak_status, list_canned_status
    from go2_dashboard.hermes.tts_local import tts_status

    from go2_dashboard.hermes.context import integrated_on_operator_dashboard

    ctx = build_robot_context()
    caps = hermes_capabilities()
    host = os.environ.get("GO2_HOST", "127.0.0.1").strip()
    port = _dashboard_port()
    if integrated_on_operator_dashboard():
        dash_url = f"http://{host}:{port}/operators/hermes"
    elif _standalone_service():
        dash_url = f"http://{host}:{int(os.environ.get('HERMES_PORT', '5054'))}/"
    else:
        dash_url = f"http://{host}:{port}/operators/hermes"
    return jsonify(
        {
            "ok": True,
            "service": "hermes",
            "integrated": integrated_on_operator_dashboard(),
            "standalone": _standalone_service() and not integrated_on_operator_dashboard(),
            "dashboard_url": dash_url,
            "operator_url": operator_base(),
            "operator_reachable": ctx.get("operator_reachable"),
            "operator_required": caps.get("operator_required"),
            "standalone_mode": caps.get("standalone"),
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
