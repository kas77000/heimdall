"""Synthesize Gjallarhorn's horn calls, one per severity, into ``app/sounds``.

    python -m heimdall.gjallarhorn.make_sounds

Brass-like additive synthesis (harmonics that brighten as the note swells, a
small scoop up into pitch, vibrato on long notes, a touch of room echo), with
the standard library only. The calls grow more urgent with severity:

* info      one short, soft call
* warning   two blasts, the second a fifth higher
* critical  three loud blasts, the last long and held, with a fifth above
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 22050
OUT = Path(__file__).with_name("app") / "sounds"

A2, D3, E3, A3 = 110.0, 146.83, 164.81, 220.0


def note(freq: float, secs: float, loud: float, *, fifth: bool = False, vibrato: bool = False) -> list[float]:
    n = int(secs * RATE)
    attack, release = 0.07, min(0.18, secs * 0.35)
    out = []
    phases = {}
    for i in range(n):
        t = i / RATE
        env = min(1.0, t / attack) if t < attack else 1.0
        if t > secs - release:
            env *= max(0.0, (secs - t) / release)
        swell = 0.85 + 0.15 * min(1.0, t / max(secs * 0.6, 0.01))
        env *= swell
        scoop = 1.0 - 0.03 * math.exp(-t / 0.05)                    # lips settling onto the note
        vib = 1.0 + (0.004 * math.sin(2 * math.pi * 5.2 * t) * min(1.0, max(0.0, t - 0.3) / 0.3) if vibrato else 0.0)
        s = 0.0
        voices = [(freq, 1.0), (freq * 1.0023, 0.6)]                # a slightly detuned second horn
        if fifth:
            voices.append((freq * 1.5, 0.55))
        for vi, (f0, va) in enumerate(voices):
            f = f0 * scoop * vib
            for h in range(1, 13):
                key = (vi, h)
                phases[key] = phases.get(key, 0.0) + 2 * math.pi * f * h / RATE
                # brass: upper harmonics come in as it gets louder
                amp = (1.0 / h ** 0.75) * env ** (1.0 + 0.22 * h)
                s += va * amp * math.sin(phases[key])
        out.append(s * loud)
    return out


def silence(secs: float) -> list[float]:
    return [0.0] * int(secs * RATE)


def room(x: list[float]) -> list[float]:
    y = x + [0.0] * int(0.4 * RATE)
    for delay, gain in ((0.083, 0.28), (0.171, 0.16), (0.29, 0.08)):
        d = int(delay * RATE)
        for i in range(len(x)):
            y[i + d] += x[i] * gain
    return y


def write(name: str, samples: list[float], peak: float) -> Path:
    top = max(abs(s) for s in samples) or 1.0
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(s / top * peak * 32767)) for s in samples))
    return path


def main() -> None:
    write("horn-info.wav", room(note(D3, 0.75, 1.0)), 0.45)
    write("horn-warning.wav", room(note(A2, 0.5, 1.0) + silence(0.12) + note(E3, 0.85, 1.0, vibrato=True)), 0.7)
    write("horn-critical.wav", room(note(A2, 0.42, 1.0, fifth=True) + silence(0.09)
                                    + note(A2, 0.42, 1.0, fifth=True) + silence(0.09)
                                    + note(A3, 1.6, 1.0, fifth=True, vibrato=True)), 0.95)
    print("written to", OUT)


if __name__ == "__main__":
    main()
