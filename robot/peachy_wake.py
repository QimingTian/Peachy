#!/usr/bin/env python3
"""Wake word on the robot: a small offline speech recogniser (Vosk) limited to a
handful of words, so "Hey Peachy" is picked out of whatever is said.

Audio comes from the shared ALSA capture device (dsnoop, 16 kHz stereo S16),
so it runs next to the conversation app without taking the mic away.
In a quiet room nothing is recognised: a chunk louder than LOUD x the noise
floor turns the recogniser on for HOT_S (with PREROLL_S of audio from before,
so the start of the phrase isn't lost), and when it goes quiet again the
utterance is closed and the recogniser reset.

A hit is the word "peachy" right after hey / hi / hello / okay, or at the start
of an utterance, with the recogniser's confidence as the score. The near-miss
words in WORDS are there so "Hey Petey" or "Hey Richie" have somewhere to go.

  python3 wake.py --bench            # print what it hears and CPU use for 20 s
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

RATE = 16000
CHUNK = 1280
DEVICE = "reachymini_audio_src"
HOME = Path(os.environ.get("PEACHY_HOME", Path.home() / ".peachy"))
MODELS = Path(os.environ.get("PEACHY_WAKE_DIR", HOME / "models"))
sys.path.insert(0, str(HOME / "pylib"))

LEAD = {"hey", "hi", "hello", "okay"}
WORDS = ["peachy", "hey", "hi", "hello", "okay",
         "peach", "peaches", "preachy", "itchy", "pete", "petey", "peter", "patty", "patchy", "petty",
         "pizza", "piece", "peace", "pitch", "richie", "reach",
         "each", "speech", "feature", "creature", "keychain", "jarvis", "siri", "buddy", "baby"]


def voice_rms(pcm: np.ndarray) -> float:
    """RMS in 300-3400 Hz, so air conditioning hum doesn't count as sound."""
    spec = np.fft.rfft(pcm.astype(np.float32))
    f = np.fft.rfftfreq(pcm.size, 1 / RATE)
    spec[(f < 300) | (f > 3400)] = 0
    return float(np.sqrt(np.mean(np.fft.irfft(spec, pcm.size) ** 2)))


def score(result: dict) -> float:
    """Confidence of a "Hey Peachy" in one recogniser result, else 0."""
    words = result.get("result", [])
    best = 0.0
    for k, w in enumerate(words):
        if w["word"] == "peachy" and (k == 0 or words[k - 1]["word"] in LEAD):
            best = max(best, float(w["conf"]))
    return best


class Detector:
    """Feed int16 mono chunks; returns a score whenever an utterance ends
    (and 0.0 for chunks that end nothing). `heard` is the last utterance."""

    LOUD = 3.0
    MIN_RMS = 60.0
    HOT_S = 1.5
    PREROLL_S = 0.5

    def __init__(self, model: str, gate: bool = True):
        import vosk
        vosk.SetLogLevel(-1)
        self.vosk = vosk
        self.model = vosk.Model(str(MODELS / model))
        self.gate = gate
        self.reset()

    def reset(self) -> None:
        self.rec = self.vosk.KaldiRecognizer(self.model, RATE, json.dumps(WORDS + ["[unk]"]))
        self.rec.SetWords(True)
        self.pending = np.zeros(0, np.int16)
        self.pre = deque(maxlen=max(1, round(self.PREROLL_S * RATE / CHUNK)))
        self.floor = self.MIN_RMS
        self.level = 0.0
        self.hot = 0
        self.open = False
        self.heard = ""
        self.ran = self.skipped = 0

    def feed(self, pcm: np.ndarray) -> list[float]:
        scores = []
        buf = np.concatenate((self.pending, pcm))
        n = len(buf) // CHUNK * CHUNK
        self.pending = buf[n:]
        for i in range(0, n, CHUNK):
            chunk = buf[i:i + CHUNK]
            if self.gate and not self._hot(chunk):
                self.pre.append(chunk)
                self.skipped += 1
                scores.append(self._close() if self.open else 0.0)
                continue
            self.ran += 1
            if not self.open:
                self.open = True
                for c in self.pre:
                    self.rec.AcceptWaveform(c.tobytes())
                self.pre.clear()
            if self.rec.AcceptWaveform(chunk.tobytes()):
                scores.append(self._result(self.rec.Result()))
            else:
                scores.append(0.0)
        return scores

    def _close(self) -> float:
        self.open = False
        s = self._result(self.rec.FinalResult())
        self.rec.Reset()
        return s

    def _result(self, text: str) -> float:
        r = json.loads(text)
        if r.get("text"):
            self.heard = r["text"]
        return score(r)

    def _hot(self, chunk: np.ndarray) -> bool:
        rms = voice_rms(chunk)
        self.level = rms
        if rms > max(self.LOUD * self.floor, self.MIN_RMS):
            self.hot = int(self.HOT_S * RATE / CHUNK)
        else:
            self.floor = 0.97 * self.floor + 0.03 * max(rms, self.MIN_RMS / self.LOUD)
            self.hot = max(0, self.hot - 1)
        return self.hot > 0


class Mic:
    """arecord on the shared capture device; yields int16 mono (channel 0)."""

    def __init__(self, device: str = DEVICE):
        self.proc = subprocess.Popen(
            ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", "2", "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def read(self, samples: int = CHUNK) -> np.ndarray | None:
        want = samples * 4
        data = b""
        while len(data) < want:
            part = self.proc.stdout.read(want - len(data))
            if not part:
                return None
            data += part
        return np.frombuffer(data, np.int16).reshape(-1, 2)[:, 0].copy()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _bench(model: str, seconds: float, gate: bool) -> None:
    det = Detector(model, gate)
    mic = Mic()
    t0, c0 = time.time(), time.process_time()
    heard = ""
    try:
        while time.time() - t0 < seconds:
            pcm = mic.read()
            if pcm is None:
                print("mic closed")
                return
            for s in det.feed(pcm):
                if det.heard != heard or s:
                    heard = det.heard
                    print(f"{time.time() - t0:5.1f}s {heard!r} score {s:.2f}", flush=True)
    finally:
        mic.close()
    wall, cpu = time.time() - t0, time.process_time() - c0
    print(f"{wall:.1f}s ({det.ran} chunks run, {det.skipped} skipped), floor {det.floor:.0f}, "
          f"CPU {100 * cpu / wall:.1f}% of one core")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--model", default="vosk-model-small-en-us-0.15")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--no-gate", action="store_true", help="recognise every chunk")
    a = ap.parse_args()
    _bench(a.model, a.seconds, not a.no_gate)
