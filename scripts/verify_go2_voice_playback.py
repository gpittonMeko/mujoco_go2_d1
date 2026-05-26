#!/usr/bin/env python3
"""Prova audio sul Go2 via dashboard sulla NX (POST ``/api/robot/voice_test``).

Modalità default (**pcm**): beep PCM sintetico → RPC ``PlayStream`` (vedi ``robot_voice_rpc_ack``).

Modalità **ttsmaker**: testo breve → RPC ``TtsMaker`` come negli esempi SDK G1 (vedi ``robot_ttsmaker_ack``).
Questo è l’approccio più vicino allo stack Unitree ``voice``; il repo GitHub *biscuit-voice-service-unitree-go2*
**non** usa DDS/SDK per l’altoparlante del cane: espone solo frasi e un callback TTS (es. Piper su PC).

Richiede sulla NX: ``GO2_VOICE_SELF_TEST_HTTP=1``, ``GO2_LOCAL=1``, ``GO2_HERMES_PLAY_ON_GO2=1``.

Esempi:

  python scripts/verify_go2_voice_playback.py http://192.168.123.18:5052
  python scripts/verify_go2_voice_playback.py http://192.168.123.18:5052 --mode ttsmaker
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser(description="HTTP self-test audio Go2 (SDK voice RPC)")
    ap.add_argument(
        "base_url",
        nargs="?",
        default="http://127.0.0.1:5052",
        help="URL dashboard es. http://192.168.123.18:5052",
    )
    ap.add_argument(
        "--mode",
        choices=("pcm", "ttsmaker"),
        default="pcm",
        help="pcm=beep+PlayStream; ttsmaker=TtsMaker RPC (testo inglese consigliato)",
    )
    ap.add_argument(
        "--text",
        default="Hello. Dashboard voice test.",
        help="Solo con --mode ttsmaker",
    )
    args = ap.parse_args()
    base = args.base_url.strip().rstrip("/")
    url = base + "/api/robot/voice_test"
    if args.mode == "ttsmaker":
        payload_obj: dict = {"mode": "ttsmaker", "text": args.text}
    else:
        payload_obj = {}
    payload = json.dumps(payload_obj).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"VERIFY_VOICE_FAIL: request_error:{exc}", file=sys.stderr)
        return 2

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        print(f"VERIFY_VOICE_FAIL: http_{status} non_JSON:{raw[:500]}", file=sys.stderr)
        return 2

    print(json.dumps(obj, indent=2, ensure_ascii=False))

    ok = bool(obj.get("ok"))
    ack = bool(obj.get("robot_voice_rpc_ack"))
    tsm = bool(obj.get("robot_ttsmaker_ack"))
    rep = obj.get("report") if isinstance(obj.get("report"), dict) else {}
    dds_done = bool(rep.get("robot_voice_dds_completed"))

    if ok and tsm:
        print(
            "VERIFY_VOICE_OK: RPC TtsMaker ha restituito codice 0 (ACK sintesi onboard SDK).",
            file=sys.stderr,
        )
        return 0
    if ok and ack:
        print("VERIFY_VOICE_OK: tutti i chunk PlayStream con codice 0 (ACK stream PCM).", file=sys.stderr)
        return 0
    if ok and not ack and dds_done:
        print(
            "VERIFY_VOICE_OK: stream completato via DDS (robot_voice_dds_completed); "
            "nessun ACK RPC PlayStream — verifica firmware/volume.",
            file=sys.stderr,
        )
        return 0
    if ok and not ack and not tsm:
        print(
            "VERIFY_VOICE_WARN: ok aggregato senza robot_voice_rpc_ack / robot_ttsmaker_ack / DDS — controlla ``report``.",
            file=sys.stderr,
        )
        return 0
    print(f"VERIFY_VOICE_FAIL: http_{status} ok=false — vedi ``report`` nell'JSON sopra.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
