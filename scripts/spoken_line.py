#!/usr/bin/env python3
"""MacOS ``say`` → WAV → Reachy speaker (or laptop ``afplay``).

Used by warmup_bridge and similar classroom prompts. Pre-upload with
``prepare()`` so playback during a live sequence has no TTS lag.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _post_json(host: str, port: int, path: str, body: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        method="POST",
        data=json.dumps(body).encode(),
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


class SpokenLine:
    """TTS through Reachy's speaker (default) or the laptop (--local-audio)."""

    def __init__(self, host: str, port: int, *, local: bool = False) -> None:
        self.host = host
        self.port = port
        self.local = local
        self._cache: dict[str, str] = {}
        self._tmp = Path("/tmp/reachy_spoken")
        self._tmp.mkdir(exist_ok=True)

    def _wav(self, text: str, idx: int) -> Path:
        aiff = self._tmp / f"p{idx}.aiff"
        wav = self._tmp / f"p{idx}.wav"
        subprocess.run(["say", "-o", str(aiff), text], check=True)
        subprocess.run(
            ["afconvert", str(aiff), str(wav), "-d", "LEI16", "-f", "WAVE"],
            check=True,
        )
        return wav

    def prepare(self, lines: list[str]) -> None:
        """Pre-generate + upload every line before a live sequence."""
        for i, text in enumerate(lines):
            if not text or text in self._cache:
                continue
            wav = self._wav(text, i)
            if self.local:
                self._cache[text] = str(wav)
                continue
            out = subprocess.run(
                [
                    "curl", "-s", "-m", "20", "-X", "POST",
                    f"http://{self.host}:{self.port}/api/media/sounds/upload",
                    "-F", f"file=@{wav};type=audio/wav",
                ],
                capture_output=True,
                text=True,
            )
            try:
                resp = json.loads(out.stdout)
                name = Path(resp.get("path", "")).name or wav.name
            except (json.JSONDecodeError, AttributeError):
                name = wav.name
            self._cache[text] = name

    @staticmethod
    def estimate_duration(text: str) -> float:
        """Rough seconds to wait after triggering playback."""
        return max(2.0, min(8.0, len(text) * 0.055 + 0.6))

    def say(self, text: str, *, block: bool = True) -> None:
        if not text:
            return
        print(f"  🔊 {text}")
        if self.local:
            path = self._cache.get(text, "")
            if not path:
                self.prepare([text])
                path = self._cache.get(text, "")
            if block:
                subprocess.run(["afplay", path], check=False)
            else:
                subprocess.Popen(["afplay", path])
            return
        try:
            _post_json(self.host, self.port, "/api/media/play_sound",
                       {"file": self._cache[text]})
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, KeyError) as e:
            print(f"  (speaker play failed: {e} — falling back to laptop)")
            self.local = True
            self.say(text, block=block)
