"""Orchestrazione voce + azione: una sessione WebRTC per ack-pausa-done."""

from __future__ import annotations

import os
import re
import threading
from typing import Any

from go2_dashboard.hermes.locomotion import (
    locomotion_to_reply,
    matches_locomotion_intent,
    parse_locomotion_intent,
)
from go2_dashboard.hermes.phrases import PHRASES
from go2_dashboard.hermes.sdk_bridge import (
    fetch_camera_jpeg,
    sport_command,
    sport_move_command,
    sport_simple_command,
)
from go2_dashboard.hermes.speech import (
    clear_voice_queue,
    enqueue_text,
    interaction_voice_enabled,
    play_script_async,
    play_wav_paths_async,
)
from go2_dashboard.hermes.tts_local import synthesize_wav
from go2_dashboard.hermes.vision import describe_jpeg

_STAND_RE = re.compile(
    r"\b("
    r"alzat[ioe]|rialz|in\s+piedi|stand\s*up|standing|"
    r"esci\s+dal\s+crouch|dal\s+crouch|fuori\s+dal\s+crouch|"
    r"mettiti\s+in\s+piedi|in\s+standing"
    r")\b",
    re.I,
)
_CROUCH_RE = re.compile(
    r"\b("
    r"accucci[a-z]*|crouch|abbassat[ioe]|sedut[ioe]|stand\s*down|inginocchia|"
    r"mettiti\s+giu|abbassati"
    r")\b",
    re.I,
)
_STAND_PRIORITY_RE = re.compile(
    r"\b(alzat[ioe]|rialz|in\s+piedi|stand\s*up|standing|esci\s+dal\s+crouch|dal\s+crouch)\b",
    re.I,
)
_VISION_RE = re.compile(
    r"\b("
    r"cosa\s+vedi|che\s+cosa\s+vedi|cosa\s+c'?è\s+davanti|cosa\s+vedi\s+adesso|"
    r"guarda\s+(avanti|davanti|fronte)|descrivi\s+(la\s+)?(scena|vista|stanza)|"
    r"cosa\s+hai\s+davanti|vedi\s+davanti|telecamera|"
    r"guarda\s+con\s+la\s+camera|usa\s+la\s+camera"
    r")\b",
    re.I,
)
_WRIST_RE = re.compile(r"\b(polso|braccio|wrist|orbbec)\b", re.I)


def matches_action_intent(message: str) -> bool:
    q = (message or "").strip()
    if not q:
        return False
    return bool(
        _VISION_RE.search(q)
        or matches_locomotion_intent(q)
        or _STAND_PRIORITY_RE.search(q)
        or _STAND_RE.search(q)
        or _CROUCH_RE.search(q)
    )


def _sport_ok(result: dict[str, Any]) -> bool:
    if result.get("http_status") == 403:
        return False
    if result.get("accepted") and result.get("async"):
        return True
    return bool(result.get("ok"))


def _run_sport_voice(mode: str) -> None:
    """Una sessione WebRTC: ack → pausa → done (Sport già inviato)."""
    phrases = PHRASES[mode]
    pause_s = float(os.environ.get("HERMES_SPORT_PAUSE_S", "8"))
    play_script_async(
        [phrases["ack_key"], phrases["done_key"]],
        pauses_after=[0.0, pause_s],
    )


def _run_locomotion_voice(phrase_key: str, *, pause_s: float) -> None:
    phrases = PHRASES.get(phrase_key) or PHRASES["move"]
    play_script_async(
        [phrases["ack_key"], phrases["done_key"]],
        pauses_after=[0.0, pause_s],
    )


def handle_locomotion(q: str) -> dict[str, Any] | None:
    intent = parse_locomotion_intent(q)
    if not intent:
        return None

    if interaction_voice_enabled():
        clear_voice_queue(kill_webrtc=True)

    if intent.kind == "move":
        sport_meta = sport_move_command(
            vx=intent.vx,
            vy=intent.vy,
            vyaw=intent.vyaw,
            duration_s=intent.duration_s,
        )
    else:
        sport_meta = sport_simple_command(intent.kind)

    ok = _sport_ok(sport_meta)
    reply = locomotion_to_reply(intent, ok=ok)
    phrase_key = "move" if intent.kind == "move" else intent.kind
    if phrase_key not in PHRASES:
        phrase_key = "move"

    if interaction_voice_enabled():
        pause_s = float(intent.duration_s) + 0.8 if intent.kind == "move" else float(
            os.environ.get("HERMES_SPORT_PAUSE_S", "8")
        )
        threading.Thread(
            target=_run_locomotion_voice,
            args=(phrase_key,),
            kwargs={"pause_s": pause_s},
            name=f"hermes-loco-voice-{intent.kind}",
            daemon=True,
        ).start()

    return {
        "reply": reply,
        "action": f"locomotion_{intent.kind}",
        "locomotion": {
            "kind": intent.kind,
            "label_it": intent.label_it,
            "vx": intent.vx,
            "vy": intent.vy,
            "vyaw": intent.vyaw,
            "duration_s": intent.duration_s,
            "steps": intent.steps,
        },
        "sport": sport_meta,
        "sport_ok": ok,
        "speech_handled": True,
        "speech_script": [PHRASES[phrase_key]["ack_key"], PHRASES[phrase_key]["done_key"]],
        "hint_it": "Locomozione Sport SDK (Move/StopMove/Hello/…) via DDS diretto.",
    }


def handle_sport(mode: str) -> dict[str, Any]:
    phrases = PHRASES[mode]
    if interaction_voice_enabled():
        clear_voice_queue(kill_webrtc=True)
    sport_meta = sport_command(mode)
    ok = _sport_ok(sport_meta)
    reply = phrases["ack"] if ok else phrases["fail"]

    if interaction_voice_enabled():
        threading.Thread(
            target=_run_sport_voice,
            args=(mode,),
            name=f"hermes-sport-voice-{mode}",
            daemon=True,
        ).start()

    return {
        "reply": reply,
        "action": f"sport_{mode}",
        "sport": sport_meta,
        "sport_ok": ok,
        "speech_handled": True,
        "speech_script": [phrases["ack_key"], phrases["done_key"]],
        "hint_it": "Voce: ack+done. Sport via DDS diretto (5052 opzionale).",
    }


def _camera_device_for_query(q: str) -> int:
    if _WRIST_RE.search(q):
        return 0
    try:
        return int((os.environ.get("HERMES_GO2_CAMERA") or "6").strip())
    except ValueError:
        return 6


def _camera_label(device: int) -> str:
    return "polso Orbbec" if device == 0 else "RealSense frontale"


def _vision_speak_enabled() -> bool:
    return os.environ.get("HERMES_VISION_SPEAK_DETAIL", "1").lower() in {"1", "true", "yes", "on"}


def _vision_voice_mode() -> str:
    return (os.environ.get("HERMES_VISION_VOICE_MODE") or "playlist").strip().lower()


def _voice_from_vision(text: str) -> str:
    """Fino a N frasi da chat, per voce più fedele al testo mostrato."""
    t = (text or "").strip()
    if not t:
        return ""
    max_chars = int(os.environ.get("HERMES_VOICE_DESC_MAX_CHARS", "200"))
    max_sents = max(1, int(os.environ.get("HERMES_VOICE_MAX_SENTENCES", "3")))
    parts: list[str] = []
    buf = ""
    for ch in t:
        buf += ch
        if ch in ".!?" and len(buf.strip()) >= 8:
            parts.append(buf.strip())
            buf = ""
            if len(parts) >= max_sents:
                break
    if buf.strip() and len(parts) < max_sents:
        parts.append(buf.strip())
    if not parts:
        parts = [t]
    out = " ".join(parts)
    if len(out) <= max_chars:
        return out
    cut = out[:max_chars].rsplit(" ", 1)[0] if " " in out[:max_chars] else out[:max_chars]
    return cut.rstrip(".,; ") + "."


def _run_vision_voice(phrases: dict[str, str], description: str, fast_ack: bool) -> None:
    """Voce descrizione: TTS veloce (espeak/gTTS). Ack già in coda se fast_ack."""
    import time as _time

    voice_line = _voice_from_vision(description)
    if not voice_line:
        if not fast_ack:
            play_script_async([phrases["ack_key"]], [0.0])
        return

    t0 = _time.perf_counter()
    mode = _vision_voice_mode()
    if mode == "combined" and not fast_ack:
        spoken = f"{phrases['ack']} {voice_line}".strip()
        wav = synthesize_wav(spoken)
        if wav:
            play_wav_paths_async([wav], [0.0])
            return
        enqueue_text(spoken)
        return

    tts_wav = synthesize_wav(voice_line)
    synth_s = round(_time.perf_counter() - t0, 2)
    if tts_wav:
        if fast_ack:
            play_wav_paths_async([tts_wav], [0.0])
        else:
            play_script_async(
                [phrases["ack_key"]],
                pauses_after=[0.0, 0.12],
                wav_paths=[tts_wav],
            )
        return

    if not fast_ack:
        play_script_async([phrases["ack_key"]], [0.0])
    enqueue_text(voice_line)


def handle_vision(q: str) -> dict[str, Any]:
    import time as _time

    t_total = _time.perf_counter()
    phrases = PHRASES["vision"]
    device = _camera_device_for_query(q)
    label = _camera_label(device)

    if interaction_voice_enabled():
        clear_voice_queue(kill_webrtc=True)
    fast_ack = os.environ.get("HERMES_FAST_ACK", "0").lower() in {"1", "true", "yes", "on"}
    if interaction_voice_enabled() and fast_ack:
        play_script_async([phrases["ack_key"]], [0.0])

    t_cam = _time.perf_counter()
    jpg, cam_meta = fetch_camera_jpeg(device)
    cam_s = round(_time.perf_counter() - t_cam, 2)
    if not jpg:
        if interaction_voice_enabled():
            play_script_async([phrases["fail_key"]], [0.0])
        return {
            "reply": (
                f"Non riesco a leggere la camera {label}. "
                "Controlla che la dashboard 5056 sia attiva per la camera RGB del polso."
            ),
            "action": "vision",
            "camera": cam_meta,
            "speech_handled": True,
        }

    t_vis = _time.perf_counter()
    text, vmeta = describe_jpeg(jpg, camera_label=label)
    vision_s = round(_time.perf_counter() - t_vis, 2)
    voice_line = _voice_from_vision(text) if _vision_speak_enabled() else ""
    if interaction_voice_enabled():
        threading.Thread(
            target=_run_vision_voice,
            args=(phrases, text, fast_ack),
            name="hermes-vision-voice",
            daemon=True,
        ).start()

    chat_reply = f"{phrases['ack']}\n\n{text}"
    return {
        "reply": chat_reply,
        "action": "vision",
        "camera": cam_meta,
        "vision": vmeta,
        "voice_spoken": voice_line or None,
        "timing": {
            "camera_s": cam_s,
            "vision_api_s": vision_s,
            "http_s": round(_time.perf_counter() - t_total, 2),
            "note": "WebRTC voce aggiunge ~10-90s (connessione Go2); vedi hermes_speak.log elapsed_s",
        },
        "speech_handled": True,
        "speech_script": [phrases["ack_key"], "tts:" + voice_line] if voice_line else [phrases["ack_key"]],
    }


def try_action(user_message: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
    q = (user_message or "").strip()
    if not q:
        return None

    if _VISION_RE.search(q):
        return handle_vision(q)
    if _STAND_PRIORITY_RE.search(q) or (_STAND_RE.search(q) and not _CROUCH_RE.search(q)):
        return handle_sport("stand_up")
    if _CROUCH_RE.search(q):
        return handle_sport("crouch")
    loco = handle_locomotion(q)
    if loco is not None:
        return loco
    return None
