#!/usr/bin/env python3
"""Record Peachy's mic (the XVF3800 output the conversation app hears) and measure it.

  python scripts/mic-record.py noise                 # 30 s, quiet room baseline
  python scripts/mic-record.py speech-2m -s 15       # talk from 2 m for 15 s
  python scripts/mic-record.py --compare noise speech-2m

Writes .run/mic/<label>.wav (16 kHz mono int16) and <label>.json: level per
second, 100 ms block percentiles, band levels, tonal peaks and the chip's AGC
gain sampled every second; a label is overwritten when recorded again.
--compare prints how far a recording sits above the baseline, per band. Audio comes over its own receive-only WebRTC stream, so the
conversation app keeps running.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hostfind import resolve_host  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_OUT = _REPO / ".run" / "mic"
RATE = 16000
BANDS = ((20, 100), (100, 300), (300, 1000), (1000, 3000), (3000, 8000))


def db(x: float) -> float:
    return round(20 * math.log10(max(x, 1e-9) / 32768.0), 1)


def agc_sampler(host: str, stop: threading.Event, out: list) -> None:
    while not stop.is_set():
        try:
            with urllib.request.urlopen(
                    f"http://{host}:8000/api/audio/config/parameter/PP_AGCGAIN", timeout=3) as r:
                out.append(round(float(json.loads(r.read())["values"][0]), 2))
        except Exception:  # noqa: BLE001
            out.append(None)
        stop.wait(1.0)


def record(host: str, seconds: float) -> np.ndarray:
    from rtcmedia import RtcMedia

    m = RtcMedia(host, 320, 180, fps=1, video=False).start()
    try:
        if m.audio(RATE // 2, timeout=12) is None:
            raise SystemExit(f"no audio from {host}: {m.error or 'timeout'}")
        chunks, got = [], 0
        need = int(seconds * RATE)
        while got < need:
            pcm = m.audio(RATE // 10, timeout=3)
            if pcm is None:
                raise SystemExit(f"audio stopped: {m.error or 'timeout'}")
            chunks.append(pcm)
            got += pcm.size
            print(f"\r  {got / RATE:5.1f} / {seconds:.0f} s   {db(float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))):6.1f} dBFS",
                  end="", flush=True)
        print()
        return np.concatenate(chunks)[:need]
    finally:
        m.stop()


def analyse(pcm: np.ndarray) -> dict:
    x = pcm.astype(np.float64)
    rms = float(np.sqrt(np.mean(x ** 2)))
    per_s = [db(float(np.sqrt(np.mean(x[i:i + RATE] ** 2)))) for i in range(0, x.size - RATE + 1, RATE)]
    blk = x[: x.size // 1600 * 1600].reshape(-1, 1600)
    blk_db = np.array([db(float(v)) for v in np.sqrt(np.mean(blk ** 2, axis=1))])

    n = 2048
    win = np.hanning(n)
    frames = [x[i:i + n] * win for i in range(0, x.size - n + 1, n // 2)]
    psd = np.mean([np.abs(np.fft.rfft(f)) ** 2 for f in frames], axis=0)
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    norm = (np.sum(win ** 2) * n) / 2

    bands = {}
    for lo, hi in BANDS:
        sel = (freqs >= lo) & (freqs < hi)
        bands[f"{lo}-{hi}"] = db(math.sqrt(float(np.sum(psd[sel])) / norm))

    spec = np.abs(np.fft.rfft(blk * np.hanning(1600), axis=1)) ** 2
    bf = np.fft.rfftfreq(1600, 1 / RATE)
    voice = (bf >= 300) & (bf < 3400)
    vb = 10 * np.log10(spec[:, voice].sum(axis=1) / ((np.hanning(1600) ** 2).sum() * 1600 / 2) + 1e-12) \
        - 20 * math.log10(32768.0)
    vp = {str(p): round(float(np.percentile(vb, p)), 1) for p in (10, 50, 90)}

    lp = 10 * np.log10(psd + 1e-12)
    peaks = []
    for i in range(3, lp.size - 3):
        if freqs[i] < 40:
            continue
        around = np.median(np.r_[lp[max(0, i - 25):i - 2], lp[i + 3:i + 26]])
        if lp[i] == lp[i - 2:i + 3].max() and lp[i] - around >= 10:
            peaks.append((round(float(lp[i] - around), 1), round(float(freqs[i]))))
    peaks.sort(reverse=True)

    return {
        "seconds": round(x.size / RATE, 1),
        "rms_dbfs": db(rms),
        "peak_dbfs": db(float(np.max(np.abs(x)))),
        "block100ms_dbfs": {str(p): round(float(np.percentile(blk_db, p)), 1) for p in (10, 50, 90, 99)},
        "per_second_dbfs": per_s,
        "bands_dbfs": bands,
        "voiceband_blocks_dbfs": vp,
        "voiceband_snr_db": round(vp["90"] - vp["10"], 1),
        "tonal_peaks": [{"hz": f, "above_floor_db": d} for d, f in peaks[:6]],
    }


def save(label: str, pcm: np.ndarray, info: dict) -> Path:
    _OUT.mkdir(parents=True, exist_ok=True)
    wav = _OUT / f"{label}.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())
    (_OUT / f"{label}.json").write_text(json.dumps(info, indent=2))
    return wav


def show(label: str, info: dict) -> None:
    b = info["block100ms_dbfs"]
    print(f"{label}: {info['seconds']} s   RMS {info['rms_dbfs']} dBFS   peak {info['peak_dbfs']} dBFS")
    print(f"  100 ms blocks  p10 {b['10']}  median {b['50']}  p90 {b['90']}  p99 {b['99']} dBFS")
    print("  per second     " + " ".join(f"{v:.0f}" for v in info["per_second_dbfs"]))
    print("  bands          " + "   ".join(f"{k} Hz {v}" for k, v in info["bands_dbfs"].items()))
    if "voiceband_blocks_dbfs" in info:
        v = info["voiceband_blocks_dbfs"]
        print(f"  300-3400 Hz    quiet p10 {v['10']}  loud p90 {v['90']}  → SNR {info['voiceband_snr_db']} dB")
    if info["tonal_peaks"]:
        print("  tonal peaks    " + ", ".join(f"{p['hz']} Hz (+{p['above_floor_db']} dB)" for p in info["tonal_peaks"]))
    a = [v for v in info.get("agc_gain", []) if v is not None]
    if a:
        print(f"  AGC gain       {min(a)} … {max(a)} (max allowed {info.get('agc_max')})")


def compare(noise: str, other: str) -> None:
    n = json.loads((_OUT / f"{noise}.json").read_text())
    o = json.loads((_OUT / f"{other}.json").read_text())
    print(f"{other} vs {noise} (dB above the baseline):")
    print(f"  overall  {o['rms_dbfs'] - n['rms_dbfs']:+.1f}   loudest 10% of blocks "
          f"{o['block100ms_dbfs']['90'] - n['block100ms_dbfs']['50']:+.1f}")
    for k, v in o["bands_dbfs"].items():
        print(f"  {k:>9} Hz  {v - n['bands_dbfs'][k]:+.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Record and measure Peachy's mic")
    ap.add_argument("label", nargs="?", default="noise")
    ap.add_argument("-s", "--seconds", type=float, default=30.0)
    ap.add_argument("--compare", nargs=2, metavar=("BASELINE", "OTHER"))
    args = ap.parse_args()
    if args.compare:
        compare(*args.compare)
        return 0

    host = resolve_host()
    try:
        with urllib.request.urlopen(
                f"http://{host}:8000/api/audio/config/parameter/PP_AGCMAXGAIN", timeout=3) as r:
            agc_max = float(json.loads(r.read())["values"][0])
    except Exception:  # noqa: BLE001
        agc_max = None
    print(f"recording {args.seconds:.0f} s from {host} (AGC max {agc_max}) — keep the room as it is")
    stop, agc = threading.Event(), []
    threading.Thread(target=agc_sampler, args=(host, stop, agc), daemon=True).start()
    try:
        pcm = record(host, args.seconds)
    finally:
        stop.set()
    info = {"label": args.label, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **analyse(pcm), "agc_max": agc_max, "agc_gain": agc}
    wav = save(args.label, pcm, info)
    show(args.label, info)
    print(f"saved {wav.relative_to(_REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
