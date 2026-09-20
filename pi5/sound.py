#!/usr/bin/env python3
"""
Sound effects through a USB speaker, with the motor horn as the fallback.

    python3 sound.py            # play one random effect
    python3 sound.py --all      # audition everything
    python3 sound.py --check    # is a USB speaker even connected?

The effects are SYNTHESIZED, not shipped: six little waveforms written with
the standard library's wave module the first time anything plays, cached in
~/sounds/. No downloads, no copyright, no internet, and nothing to forget to
copy to the Pi -- the code is the asset.

Playback goes through aplay (ALSA, preinstalled on Pi OS Lite) on whatever
card calls itself USB, non-blocking. No USB speaker plugged in means
available() is False and the caller falls back to playing the horn on the
drive motors, which is what this project did before it had a speaker at all.
"""

from __future__ import annotations

import math
import os
import random
import struct
import subprocess
import wave

SOUND_DIR = os.path.expanduser("~/sounds")
RATE = 22050


def _synth(name, seconds, freq_at, amp_at=None):
    """Write one effect: freq_at(t) gives Hz, amp_at(t) gives 0..1."""
    path = os.path.join(SOUND_DIR, name + ".wav")
    if os.path.exists(path):
        return path
    os.makedirs(SOUND_DIR, exist_ok=True)
    n = int(RATE * seconds)
    frames = bytearray()
    phase = 0.0
    for i in range(n):
        t = i / RATE
        phase += 2 * math.pi * freq_at(t) / RATE
        amp = amp_at(t) if amp_at else 1.0
        # a touch of 3rd harmonic so it sounds like a horn, not a sine test
        sample = 0.8 * math.sin(phase) + 0.2 * math.sin(3 * phase)
        frames += struct.pack("<h", int(20000 * amp * sample))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return path


def _effects():
    """Six personalities. Each returns a wav path, synthesized on demand."""
    fade = lambda t, dur: min(1.0, 10 * t, 10 * (dur - t))          # declick
    return {
        # the classic two-tone klaxon
        "klaxon": lambda: _synth("klaxon", 0.7,
            lambda t: 440 if (t % 0.35) < 0.175 else 330,
            lambda t: fade(t, 0.7)),
        # descending zap
        "laser": lambda: _synth("laser", 0.35,
            lambda t: 1800 * (1 - t / 0.35) + 200,
            lambda t: fade(t, 0.35)),
        # police-ish sweep up and down
        "siren": lambda: _synth("siren", 1.2,
            lambda t: 600 + 500 * math.sin(2 * math.pi * t / 0.6),
            lambda t: fade(t, 1.2)),
        # rising ta-da arpeggio
        "tada": lambda: _synth("tada", 0.55,
            lambda t: [523, 659, 784, 1047][min(3, int(t / 0.14))],
            lambda t: fade(t, 0.55)),
        # wobbling boing
        "boing": lambda: _synth("boing", 0.6,
            lambda t: 300 + 180 * math.sin(2 * math.pi * 9 * t) * (1 - t / 0.6),
            lambda t: fade(t, 0.6) * (1 - 0.6 * t)),
        # impatient triple beep
        "triple": lambda: _synth("triple", 0.6,
            lambda t: 880,
            lambda t: (1.0 if (t % 0.2) < 0.12 else 0.0) * fade(t, 0.6)),
    }


class Speaker:
    """A USB audio device, if one is plugged in."""

    def __init__(self):
        self._card = self._find_card()
        self._last = None

    @staticmethod
    def _find_card():
        try:
            with open("/proc/asound/cards") as f:
                for line in f:
                    # " 1 [Device    ]: USB-Audio - USB2.0 Device"
                    if "USB" in line and "[" in line:
                        return line.split("[")[0].strip().split()[0]
        except OSError:
            pass
        return None

    def available(self) -> bool:
        # Re-probe each time: the speaker can be plugged in after boot and
        # should start working without anyone restarting anything.
        self._card = self._find_card()
        return self._card is not None

    def play_random(self) -> str | None:
        """Fire one effect, non-blocking. Returns its name, or None."""
        if not self.available():
            return None
        effects = _effects()
        # random, but never the same twice running -- "random" that repeats
        # reads as broken.
        name = random.choice([k for k in effects if k != self._last]
                             or list(effects))
        self._last = name
        path = effects[name]()
        try:
            subprocess.Popen(
                ["aplay", "-q", "-D", f"plughw:{self._card}", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return None
        return name


if __name__ == "__main__":
    import sys
    import time
    spk = Speaker()
    if "--check" in sys.argv:
        print(f"usb audio card: {spk._card or 'NONE (falls back to motor horn)'}")
        sys.exit(0 if spk.available() else 1)
    if not spk.available():
        sys.exit("no USB speaker; the motor horn would play instead")
    if "--all" in sys.argv:
        for name, make in _effects().items():
            print(f"  {name}")
            subprocess.run(["aplay", "-q", "-D", f"plughw:{spk._card}", make()])
            time.sleep(0.2)
    else:
        print(f"  {spk.play_random()}")
        time.sleep(1.5)
