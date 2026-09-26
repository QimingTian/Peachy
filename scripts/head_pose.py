"""Peachy head *home* pose — one absolute target every wake/reset should reach.

Stored in ``.run/reachy_toggle_state.json`` as ``head_home`` (head_pose + antennas).
The dashboard Head tune panel writes this; ``ctl-toggle wake``, expressions, and
room-watch should all settle here — not wake_up's canned pose, and not
wake+offset double-counting.

Legacy: ``head_offset`` from the dashboard is treated as absolute home. Older
cal-head deltas (added to wake) migrate on read when ``head_offset_mode`` is
not ``absolute``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_AXES = ("x", "y", "z", "roll", "pitch", "yaw")
_REPO = Path(__file__).resolve().parent.parent
STATE = _REPO / ".run" / "reachy_toggle_state.json"


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(fdata: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(fdata, indent=2) + "\n")


def _wake_cal(fdata: dict) -> tuple[dict, list] | None:
    try:
        w = fdata["calibration"]["wake"]
        return dict(w["head_pose"]), list(w["antennas"])
    except (KeyError, TypeError):
        return None


def resolve_home(fdata: dict | None = None) -> tuple[dict, list] | None:
    """Return absolute (head_pose, antennas) or None if nothing saved."""
    fdata = fdata or load_state()
    if "head_home" in fdata:
        h = fdata["head_home"]
        hp = {k: float(h.get("head_pose", {}).get(k, 0.0)) for k in _AXES}
        ant = list(h.get("antennas") or [0.0, 0.0])
        return hp, ant

    off = fdata.get("head_offset") or {}
    if not off:
        return _wake_cal(fdata)

    if fdata.get("head_offset_mode") == "delta":
        cal = _wake_cal(fdata)
        if cal:
            wake_hp, wake_ant = cal
            hp = dict(wake_hp)
            for k in _AXES:
                if k in off:
                    hp[k] = float(hp.get(k, 0.0)) + float(off[k])
            return hp, list(wake_ant)

    cal = _wake_cal(fdata)
    wake_ant = list(cal[1]) if cal else [0.0, 0.0]
    hp = {k: float(off.get(k, 0.0)) for k in _AXES}
    return hp, wake_ant


def write_home(
    head_pose: dict,
    antennas: list | None = None,
    *,
    fdata: dict | None = None,
) -> dict:
    """Persist absolute home; sync legacy head_offset for older readers."""
    fdata = dict(fdata or load_state())
    ant = antennas if antennas is not None else [0.0, 0.0]
    hp = {k: round(float(head_pose.get(k, 0.0)), 5) for k in _AXES}
    fdata["head_home"] = {
        "head_pose": hp,
        "antennas": [round(float(ant[0]), 5), round(float(ant[1]), 5)],
    }
    fdata["head_offset"] = dict(hp)
    fdata["head_offset_mode"] = "absolute"
    fdata["head_offset_saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_state(fdata)
    return fdata


def wait_move_done(http_get, host: str, port: int, max_s: float = 18.0) -> None:
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            running = http_get(host, port, "/api/move/running", "GET", 4.0)
            if isinstance(running, list) and not running:
                return
        except Exception:
            return
        time.sleep(0.2)


def settle_home(
    http,
    host: str,
    port: int,
    fdata: dict | None = None,
    *,
    dur: float = 0.8,
) -> bool:
    """After wake_up (or any reset), goto saved home. Returns True if moved."""
    resolved = resolve_home(fdata)
    if not resolved:
        return False
    hp, ant = resolved
    wait_move_done(http, host, port)
    try:
        http(host, port, "/api/move/goto", "POST", 30.0,
             body={"head_pose": hp, "antennas": ant,
                   "duration": dur, "interpolation": "minjerk"})
        return True
    except Exception:
        return False
