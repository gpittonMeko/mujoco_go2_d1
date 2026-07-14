#!/usr/bin/env python3
"""Demo: TTS → Cyberpunk WAV + braccio sinistro + presa mano sinistra (in parallelo)."""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from h2_common import (
    GentleArmConfig,
    REPO_ROOT,
    ensure_sdk_path,
    init_dds,
    run_gentle_left_arm,
)
from h2_hand_util import probe_left_hand, run_gentle_left_grip
from h2_wav_util import play_pcm_stream, read_wav

PELLEGRINO_WAV = REPO_ROOT / "data" / "audio" / "pellegrino_tts.wav"
CYBERPUNK_WAV = REPO_ROOT / "data" / "audio" / "cyberpunk_meme.wav"


def _audio_client(volume: int):
    ensure_sdk_path()
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    audio = AudioClient()
    audio.SetTimeout(20.0)
    audio.Init()
    audio.SetVolume(volume)
    return audio


def _play_wav_file(audio, wav: Path, stream_name: str) -> bool:
    pcm, rate, ch, ok = read_wav(str(wav))
    if not ok or rate != 16000 or ch != 1:
        print(f"[demo] bad wav {wav.name} rate={rate} ch={ch}", file=sys.stderr)
        return False
    print(f"[demo] play {wav.name} (~{len(pcm) / 32000:.1f}s)")
    play_pcm_stream(audio, pcm, stream_name=stream_name, sample_rate=rate, num_channels=ch)
    audio.PlayStop(stream_name)
    return True


def _wav_duration_s(wav: Path) -> float:
    pcm, rate, ch, ok = read_wav(str(wav))
    if not ok or rate <= 0 or ch <= 0:
        return 30.0
    return len(pcm) / float(rate * ch * 2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="H2 demo: TTS poi Cyberpunk + braccio SX + mano SX"
    )
    parser.add_argument("--iface", default=None)
    parser.add_argument("--dry-arm", action="store_true")
    parser.add_argument("--dry-hand", action="store_true")
    parser.add_argument("--dry-audio", action="store_true")
    parser.add_argument("--rise", type=float, default=8.0, help="salita braccio (s)")
    parser.add_argument("--hold", type=float, default=None, help="hold braccio (default: durata meme)")
    parser.add_argument("--lower", type=float, default=8.0)
    parser.add_argument("--hand-close", type=float, default=0.72, help="chiusura dita 0-1")
    parser.add_argument("--hand-delay", type=float, default=None, help="ritardo presa mano dopo audio (s)")
    parser.add_argument("--tts-volume", type=int, default=85)
    parser.add_argument("--meme-volume", type=int, default=90)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if not args.yes and not args.dry_arm:
        print(
            "Demo:\n"
            "  1) TTS «Pellegrino Casoria di Accenture…»\n"
            "  2) Cyberpunk meme + braccio SINISTRO che sale + mano SINISTRA che si stringe\n"
            "Robot in piedi, area libera, operatore presente. Nessun ReleaseMode.\n"
            "Prerequisito: brainco_hand_server attivo sul PC2 (.162)."
        )
        input("Enter per avviare...")

    iface = init_dds(args.iface)
    print(f"[demo] DDS iface={iface}")

    if not args.dry_audio:
        tts = PELLEGRINO_WAV.resolve()
        if not tts.is_file():
            print(f"[demo] missing {tts}", file=sys.stderr)
            return 1
        print("[demo] step 1/2 — TTS intro...")
        audio = _audio_client(args.tts_volume)
        if not _play_wav_file(audio, tts, "h2_casoria_tts"):
            return 1
        time.sleep(0.3)

    meme = CYBERPUNK_WAV.resolve()
    if not args.dry_audio and not meme.is_file():
        print(f"[demo] missing {meme}", file=sys.stderr)
        return 1

    meme_dur = _wav_duration_s(meme) if meme.is_file() else 30.0
    hold_s = args.hold if args.hold is not None else max(2.0, meme_dur - args.rise - args.lower - 2.0)
    hand_delay = args.hand_delay if args.hand_delay is not None else args.rise * 0.5
    hand_hold = max(2.0, hold_s * 0.75)
    arm_cfg = GentleArmConfig(rise_s=args.rise, hold_s=hold_s, lower_s=args.lower)

    audio_err: list[str] = []
    audio_done = threading.Event()
    motion_err: list[str] = []

    def _meme_thread() -> None:
        try:
            a = _audio_client(args.meme_volume)
            if not _play_wav_file(a, meme, "h2_casoria_meme"):
                audio_err.append("meme playback failed")
        except Exception as exc:
            audio_err.append(str(exc))
        finally:
            audio_done.set()

    def _hand_thread() -> None:
        try:
            time.sleep(hand_delay)
            if not args.dry_hand:
                _, msg = probe_left_hand(timeout_s=4.0)
                if msg is None:
                    motion_err.append("left hand DDS non disponibile")
                    return
                print("[demo] mano sinistra — presa lenta...")
                run_gentle_left_grip(
                    close_fraction=args.hand_close,
                    rise_s=3.0,
                    hold_s=hand_hold,
                    lower_s=3.0,
                )
        except Exception as exc:
            motion_err.append(f"hand: {exc}")

    if args.dry_arm and args.dry_audio and args.dry_hand:
        print("[demo] dry-run completo")
        return 0

    if not args.dry_audio:
        print(f"[demo] step 2/2 — Cyberpunk (~{meme_dur:.1f}s) + braccio SX + mano SX...")
        threading.Thread(target=_meme_thread, daemon=True).start()
        time.sleep(0.2)
    elif not args.dry_arm or not args.dry_hand:
        print("[demo] step 2/2 — solo movimento (dry-audio)...")

    if not args.dry_hand:
        threading.Thread(target=_hand_thread, daemon=True).start()

    if not args.dry_arm:
        last_phase = {"v": ""}

        def _arm_phase(p: str) -> None:
            if p != last_phase["v"]:
                print(f"  arm: {p}")
                last_phase["v"] = p

        run_gentle_left_arm(arm_cfg, on_phase=_arm_phase)

    if not args.dry_audio:
        audio_done.wait(timeout=max(180.0, meme_dur + 30.0))
        if audio_err:
            print(f"[demo] audio error: {audio_err[0]}", file=sys.stderr)
            return 1

    if motion_err:
        print(f"[demo] motion error: {motion_err[0]}", file=sys.stderr)
        return 1

    print("[demo] completato")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
