"""World heading: body angles relative to the room instead of the base (importable lib).

The body encoder measures the body against the base, and the base turns whenever
someone bumps it. cal-heading.py matches the camera against reference frames of
the room and stores how far the base has turned in .run/heading.json:

    world = encoder + offset        (degrees, clockwise-positive, seen from above)

Console degrees everywhere (tape, Dozing direction, room watch) are world degrees;
only the motor commands use encoder degrees, limited to ±160°.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / ".run" / "heading.json"
ENC_LIMIT = 160.0
_cache = {"mtime": None, "data": {}, "at": 0.0}


def state() -> dict:
    now = time.monotonic()
    if now - _cache["at"] < 0.5:
        return _cache["data"]
    _cache["at"] = now
    try:
        mtime = FILE.stat().st_mtime
        if mtime != _cache["mtime"]:
            d = json.loads(FILE.read_text())
            _cache["data"], _cache["mtime"] = (d if isinstance(d, dict) else {}), mtime
    except (OSError, ValueError):
        _cache["data"], _cache["mtime"] = {}, None
    return _cache["data"]


def offset() -> float:
    try:
        return float(state().get("offset_deg", 0.0))
    except (TypeError, ValueError):
        return 0.0


def wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def to_world(enc_deg: float) -> float:
    return wrap(enc_deg + offset())


def to_enc(world_deg: float) -> float:
    """Encoder target for a world direction, clamped to the body's range."""
    return max(-ENC_LIMIT, min(ENC_LIMIT, wrap(world_deg - offset())))


def world_range() -> tuple[float, float]:
    off = offset()
    return -ENC_LIMIT + off, ENC_LIMIT + off


def enc_rad(world_deg: float) -> float:
    """Robot body_yaw (radians, counter-clockwise) for a world direction."""
    return -math.radians(to_enc(world_deg))


def save(offset_deg: float, **extra) -> dict:
    d = {**state(), **extra, "offset_deg": round(wrap(offset_deg), 2),
         "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(FILE)
    _cache["at"] = 0.0
    return d
