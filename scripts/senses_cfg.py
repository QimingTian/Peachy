"""Settings for Peachy's senses on the robot (robot/peachy_senses.py), built here
from the laptop's calibration: world heading, head home, Dozing and sleep poses,
light samples, plus the wake word model and threshold. The console pushes them (POST /config) whenever they change; the
robot keeps the last copy for when the laptop is off."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import heading
from head_pose import altaz, load_state

_REPO = Path(__file__).resolve().parent.parent
_SAMPLES = _REPO / ".run" / "light_samples"

DOZE_DEG = max(-160.0, min(160.0, float(os.environ.get("PEACHY_DOZE_DEG", "-100"))))
DAY = os.environ.get("PEACHY_DAY", "07:00-23:00")
DOZE_AFTER_S = float(os.environ.get("PEACHY_DOZE_AFTER_S", "180"))
GREETING = os.environ.get("PEACHY_GREETING", "Hi! I'm here.")
WAKE_MODEL = os.environ.get("PEACHY_WAKE_MODEL", "vosk-model-small-en-us-0.15")
WAKE_THRESHOLD = float(os.environ.get("PEACHY_WAKE_THRESHOLD", "0.6"))
NOTIFY_URL = os.environ.get("PEACHY_NOTIFY_URL", "")   # e.g. https://ntfy.sh/<your topic>; problems go here


def light_thresholds(deg: float) -> dict:
    """Cut-offs at this body direction from the cal-light.py samples. At or below
    dark_max is dark, at or above lit_min is lit, in between keeps the last call.
    A dark→lit change counts as a lamp only if it rose by `jump` within 8 s;
    daylight through the window is far slower."""
    ref = {}
    for label in ("dark", "lit"):
        try:
            rows = [json.loads(ln) for ln in (_SAMPLES / label / "samples.jsonl").read_text().splitlines()
                    if ln.strip()]
        except (OSError, ValueError):
            rows = []
        if not rows:
            continue
        gap = min(abs(float(r["body_deg"]) - deg) for r in rows)
        if gap > 15:
            continue
        vals = sorted(float(r["mean"]) for r in rows if abs(float(r["body_deg"]) - deg) <= gap + 3)
        ref[label] = vals[len(vals) // 2]
    dark, lit = ref.get("dark"), ref.get("lit")
    if dark is None or lit is None or lit - dark < 15:
        return {"dark_max": 30.0, "lit_min": 70.0, "jump": 35.0, "dark": dark, "lit": lit}
    span = lit - dark
    return {"dark_max": round(dark + 0.25 * span, 1), "lit_min": round(dark + 0.7 * span, 1),
            "jump": round(0.5 * span, 1), "dark": round(dark, 1), "lit": round(lit, 1)}


def scan_path(start: float, step: float = 55.0) -> list[float]:
    """Body stops for one look-around from `start` (world degrees): out to the far
    end of the body's ±160° encoder range, then the near end unless the ~90° camera
    saw it."""
    def leg(a: float, b: float) -> list[float]:
        n = max(1, math.ceil(abs(b - a) / step))
        return [a + (b - a) * i / n for i in range(1, n + 1)]
    s = heading.to_enc(start)
    far = heading.ENC_LIMIT if s <= 0 else -heading.ENC_LIMIT
    path = leg(s, far)
    if abs(-far - s) > 60:
        path += leg(s, -far)
    return [round(heading.to_world(a)) for a in path]


def build(watch: bool, wake: bool = False) -> dict:
    cal = load_state().get("calibration") or {}
    sleep = cal.get("sleep") or {}
    sleep_pose = None
    if sleep.get("head_pose") and sleep.get("antennas"):
        sleep_pose = {"head_pose": {k: float(v) for k, v in sleep["head_pose"].items()},
                      "antennas": [float(a) for a in sleep["antennas"]][:2]}
    return {
        "follow": False, "watch": bool(watch), "day": DAY, "doze_after_s": DOZE_AFTER_S,
        "doze_body": heading.enc_rad(DOZE_DEG), "doze_deg": DOZE_DEG,
        "scan_body": [heading.enc_rad(w) for w in scan_path(DOZE_DEG)],
        "light": light_thresholds(DOZE_DEG),
        "level": {k: v for k, v in altaz(0.0, 0.0).items() if k in ("x", "y", "z", "roll")},
        "sleep_pose": sleep_pose, "greeting": GREETING,
        "wake": bool(wake), "wake_model": WAKE_MODEL, "wake_threshold": WAKE_THRESHOLD,
        "notify_url": NOTIFY_URL,
    }
