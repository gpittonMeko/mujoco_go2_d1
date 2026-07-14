#!/usr/bin/env python3
"""Emergency: release rt/arm_sdk + stop audio playback."""
from __future__ import annotations

import argparse

from h2_common import ensure_sdk_path, init_dds, emergency_stop_arm_sdk


def _stop_audio() -> None:
    ensure_sdk_path()
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    audio = AudioClient()
    audio.SetTimeout(5.0)
    audio.Init()
    for name in (
        "h2_casoria_tts",
        "h2_casoria_meme",
        "h2_cyberpunk",
        "h2_pellegrino",
        "h2_demo",
    ):
        try:
            audio.PlayStop(name)
        except Exception:
            pass
    print("[stop] audio PlayStop inviato")


def main() -> int:
    parser = argparse.ArgumentParser(description="H2 emergency stop (arm + audio)")
    parser.add_argument("--iface", default=None)
    parser.add_argument("--skip-audio", action="store_true")
    parser.add_argument("--skip-arm", action="store_true")
    args = parser.parse_args()

    init_dds(args.iface)
    if not args.skip_audio:
        _stop_audio()
    if not args.skip_arm:
        emergency_stop_arm_sdk()
    print("[stop] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
