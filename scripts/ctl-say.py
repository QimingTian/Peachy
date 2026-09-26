#!/usr/bin/env python3
"""Speak typed text on Peachy's speaker.

The laptop synthesises the speech (macOS `say`), uploads it as a WAV over the
daemon REST API, plays it, and deletes it afterwards.

  python scripts/ctl-say.py "Hello everyone"
  python scripts/ctl-say.py "大家好" --voice Tingting
  python scripts/ctl-say.py "Hello" --dry-run     # synthesise only, keep the WAV locally
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hostfind import resolve_host  # noqa: E402
from sound_sync import upload_wav  # noqa: E402

VOICE_ZH = "Tingting"
VOICE_EN = "Samantha"
MAX_CHARS = 500
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def pick_voice(text: str) -> str:
    return VOICE_ZH if _CJK.search(text) else VOICE_EN


def synthesise(text: str, voice: str, out_wav: Path) -> float:
    """Write a mono 16-bit 44.1 kHz WAV; return its length in seconds."""
    aiff = out_wav.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True, timeout=60)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@44100", "-c", "1", str(aiff), str(out_wav)],
                   check=True, timeout=60)
    aiff.unlink(missing_ok=True)
    with wave.open(str(out_wav)) as w:
        return w.getnframes() / float(w.getframerate())


def _daemon(host: str, port: int, path: str, method: str = "GET", body: dict | None = None) -> None:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    urllib.request.urlopen(req, timeout=15).read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("--voice", help=f"macOS voice (default: {VOICE_ZH} for Chinese, {VOICE_EN} otherwise)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    text = " ".join(args.text.split())[:MAX_CHARS]
    if not text:
        print("nothing to say", file=sys.stderr)
        return 2
    voice = args.voice or pick_voice(text)

    tmp = Path(tempfile.mkdtemp(prefix="peachy_say_"))
    wav = tmp / f"peachy_say_{int(time.time() * 1000)}.wav"
    try:
        secs = synthesise(text, voice, wav)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"speech synthesis failed: {e}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"{voice}: {secs:.1f}s → {wav}")
        return 0

    host_port = resolve_host()
    host, _, port = host_port.partition(":")
    port_i = int(port or 8000)
    try:
        name = upload_wav(host, port_i, wav)
        _daemon(host, port_i, "/api/media/play_sound", "POST", {"file": name})
        print(f"saying ({voice}, {secs:.1f}s): {text}")
        time.sleep(secs + 1.0)
        _daemon(host, port_i, f"/api/media/sounds/{name}", "DELETE")
    except OSError as e:
        print(f"robot unreachable: {e}", file=sys.stderr)
        return 1
    finally:
        wav.unlink(missing_ok=True)
        tmp.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
