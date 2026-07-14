#!/usr/bin/env python3
"""Generate placeholder 16kHz mono WAV (Cyberpunk-style synth sting) for lab demo."""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "audio" / "cyberpunk_meme.wav"
RATE = 16000


def _tone(freq: float, t: float, amp: float = 0.4) -> float:
    return amp * math.sin(2.0 * math.pi * freq * t)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    duration = 4.5
    samples = []
    n = int(RATE * duration)
    for i in range(n):
        t = i / RATE
        env = 1.0
        if t < 0.05:
            env = t / 0.05
        elif t > duration - 0.3:
            env = max(0.0, (duration - t) / 0.3)
        # "wake up" style rising synth + bass hit
        f = 180.0 + 420.0 * min(1.0, t / 1.2)
        s = _tone(f, t, 0.25)
        if t > 1.0:
            s += _tone(55.0, t, 0.5 * env)
        if t > 1.35:
            s += _tone(110.0, t, 0.35 * env)
        if t > 2.0:
            s += _tone(73.4, t, 0.6 * env)  # bass drop feel
        s = max(-1.0, min(1.0, s * env))
        samples.append(int(s * 28000))

    with wave.open(str(OUT), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        frames = b"".join(struct.pack("<h", s) for s in samples)
        wf.writeframes(frames)
    print(f"Wrote {OUT} ({duration}s, {RATE}Hz mono)")


if __name__ == "__main__":
    main()
