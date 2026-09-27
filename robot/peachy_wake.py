#!/usr/bin/env python3
"""Wake word on the robot: openWakeWord's three ONNX models, streaming.

Audio comes from the shared ALSA capture device (dsnoop, 16 kHz stereo S16),
so it runs next to the conversation app without taking the mic away.
Every 80 ms: melspectrogram over the last 1760 samples, one speech embedding
over the last 76 mel frames, then the wake word classifier over the last
16 embeddings. That's the same streaming path as openwakeword.Model.

  python3 wake.py --bench            # print scores and CPU use for 20 s
"""

from __future__ import annotations

import os
import subprocess
import time
from collections import deque
from pathlib import Path

import numpy as np

RATE = 16000
CHUNK = 1280
DEVICE = "reachymini_audio_src"
MODELS = Path(os.environ.get("PEACHY_WAKE_DIR", Path.home() / ".peachy" / "models"))


def _session(path: Path):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


def voice_rms(pcm: np.ndarray) -> float:
    """RMS in 300-3400 Hz, so air conditioning hum doesn't count as sound."""
    spec = np.fft.rfft(pcm.astype(np.float32))
    f = np.fft.rfftfreq(pcm.size, 1 / RATE)
    spec[(f < 300) | (f > 3400)] = 0
    return float(np.sqrt(np.mean(np.fft.irfft(spec, pcm.size) ** 2)))


class Detector:
    """Feed int16 mono chunks; returns the wake word score for each 80 ms.

    The speech embedding is most of the cost, so in a quiet room it is skipped:
    a chunk louder than LOUD x the noise floor turns the models on for HOT_S,
    first rebuilding the mel frames and the last BACKFILL embeddings from the
    raw audio so the start of the phrase isn't lost. While quiet the classifier
    input keeps the last (quiet) embeddings, which is what it would see anyway.
    """

    LOUD = 3.0
    MIN_RMS = 60.0
    HOT_S = 2.0
    BACKFILL = 3

    def __init__(self, model: str, gate: bool = True):
        self.mel = _session(MODELS / "melspectrogram.onnx")
        self.emb = _session(MODELS / "embedding_model.onnx")
        self.ww = _session(MODELS / model)
        self.ww_in = self.ww.get_inputs()[0].name
        self.frames = self.ww.get_inputs()[0].shape[1]
        self.gate = gate
        self.reset()

    def reset(self) -> None:
        self.ring = np.zeros((76 + 8 * (self.BACKFILL - 1) + 3) * 160, np.int16)
        self.pending = np.zeros(0, np.int16)
        self.mels = np.ones((76, 32), np.float32)
        self.feats = deque(maxlen=self.frames)
        noise = np.random.randint(-1000, 1000, RATE * 4).astype(np.int16)
        spec = self._mel(noise)
        for i in range(0, spec.shape[0] - 75, 8):
            self.feats.append(self._embed(spec[i:i + 76]))
        self.warm = 5
        self.floor = self.MIN_RMS
        self.level = 0.0
        self.hot = 0
        self.cold = True
        self.ran = self.skipped = 0

    def _mel(self, pcm: np.ndarray) -> np.ndarray:
        out = self.mel.run(None, {"input": pcm[None].astype(np.float32)})[0]
        return np.squeeze(out) / 10 + 2

    def _embed(self, window: np.ndarray) -> np.ndarray:
        return self.emb.run(None, {"input_1": window[None, :, :, None].astype(np.float32)})[0].reshape(-1)

    def feed(self, pcm: np.ndarray) -> list[float]:
        scores = []
        buf = np.concatenate((self.pending, pcm))
        n = len(buf) // CHUNK * CHUNK
        self.pending = buf[n:]
        for i in range(0, n, CHUNK):
            chunk = buf[i:i + CHUNK]
            self.ring = np.concatenate((self.ring[CHUNK:], chunk))
            if self.gate and not self._hot(chunk):
                self.cold = True
                self.skipped += 1
                scores.append(0.0)
                continue
            self.ran += 1
            if self.cold:
                self.cold = False
                spec = self._mel(self.ring)
                for k in range(self.BACKFILL - 1, 0, -1):
                    self.feats.append(self._embed(spec[len(spec) - 76 - 8 * k:len(spec) - 8 * k]))
                self.mels = spec[-76:]
            else:
                self.mels = np.vstack((self.mels, self._mel(self.ring[-(CHUNK + 480):])))[-76:]
            self.feats.append(self._embed(self.mels))
            x = np.array(self.feats, np.float32)[None]
            s = float(np.squeeze(self.ww.run(None, {self.ww_in: x})[0]))
            if self.warm:
                self.warm -= 1
                s = 0.0
            scores.append(s)
        return scores

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
    best, n = 0.0, 0
    try:
        while time.time() - t0 < seconds:
            pcm = mic.read()
            if pcm is None:
                print("mic closed")
                return
            for s in det.feed(pcm):
                n += 1
                best = max(best, s)
                if s > 0.3:
                    print(f"{time.time() - t0:5.1f}s score {s:.2f}", flush=True)
    finally:
        mic.close()
    wall, cpu = time.time() - t0, time.process_time() - c0
    print(f"{n} frames in {wall:.1f}s ({det.ran} run, {det.skipped} skipped), best {best:.2f}, "
          f"floor {det.floor:.0f}, CPU {100 * cpu / wall:.1f}% of one core")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--model", default="hey_jarvis_v0.1.onnx")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--no-gate", action="store_true", help="run the models on every chunk")
    a = ap.parse_args()
    _bench(a.model, a.seconds, not a.no_gate)
