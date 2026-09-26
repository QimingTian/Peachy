#!/usr/bin/env python3
"""Live head-pose jog + neutral-offset tuner (laptop-driven, LAN REST).

The assembled mechanical zero can be slightly off, so commanded pitch/roll=0
looks tilted. Jog the head live until it looks level, then `save` — the
correction is written to .run/reachy_toggle_state.json as ``head_offset`` and
other scripts add it to every pose.

REPL commands (type, Enter):
  p <d> / r <d> / y <d>   nudge pitch / roll / yaw by <d> radians (rel)
  z <d> / x <d> / xy <d>  nudge z / x / y by <d> metres (rel)
  pitch=<v> ...            set an axis absolutely (e.g. pitch=-0.06)
  deg                      toggle: interpret p/r/y as DEGREES (default rad)
  show                     print current target
  zero                     all axes back to 0 and move there
  save                     write current pose as head_offset
  q                        recenter (0) + quit

Direct (non-interactive):
  python scripts/cal-head.py --set pitch=-0.06,roll=0.01 --save
Env: REACHY_HOST (auto via hostfind), REACHY_PORT (8000)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STATE = _REPO / ".run" / "reachy_toggle_state.json"
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hostfind import resolve_host
HOST = resolve_host()
PORT = int(os.environ.get("REACHY_PORT", "8000"))
_AXES = ("x", "y", "z", "roll", "pitch", "yaw")
_LIM_PR = math.radians(38.0)   # stay just inside the ±40° clamp


def _http(path, method, body=None, timeout=30.0):
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t = r.read().decode()
            return json.loads(t) if t else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} {path}: {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"Cannot reach daemon {HOST}:{PORT} ({e.reason}). "
                         f"Try REACHY_HOST=reachy-mini.local") from e


def _state() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _clampd(p: dict) -> dict:
    p = dict(p)
    p["pitch"] = max(-_LIM_PR, min(_LIM_PR, p["pitch"]))
    p["roll"] = max(-_LIM_PR, min(_LIM_PR, p["roll"]))
    return p


def _move(pose: dict, dur: float = 0.5, antennas: list | None = None):
    body = {"head_pose": _clampd(pose), "duration": dur,
            "interpolation": "minjerk"}
    if antennas is not None:
        body["antennas"] = antennas
    _http("/api/move/goto", "POST", body)


def _fmt(p: dict) -> str:
    return ("x={x:+.4f} y={y:+.4f} z={z:+.4f} "
            "roll={roll:+.4f} pitch={pitch:+.4f} yaw={yaw:+.4f}").format(**p)


def _save(pose: dict):
    try:
        live = _http("/api/state/full", "GET")
        hp = dict(live.get("head_pose") or pose)
        ant = list(live.get("antennas_position") or [0.0, 0.0])
    except SystemExit:
        hp, ant = pose, [0.0, 0.0]
    from head_pose import write_home
    write_home(hp, ant)
    print(f"✓ saved head home → {_STATE.relative_to(_REPO)}")
    print("  wake / expressions / room-watch settle here now.")


def _apply_set(pose: dict, expr: str):
    for part in expr.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k in _AXES:
            pose[k] = float(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="Reachy head jog / neutral-offset tuner")
    ap.add_argument("--set", help='absolute axes, e.g. "pitch=-0.06,roll=0.01"')
    ap.add_argument("--save", action="store_true", help="save after --set and exit")
    ap.add_argument("--from-saved", action="store_true",
                    help="start from the saved head_offset instead of zeros")
    args = ap.parse_args()

    pose = {k: 0.0 for k in _AXES}
    if args.from_saved:
        pose.update({k: float(v) for k, v in _state().get("head_offset", {}).items()
                     if k in _AXES})

    print(f"Daemon {HOST}:{PORT} — enabling motors")
    _http("/api/motors/set_mode/enabled", "POST")

    if args.set:
        _apply_set(pose, args.set)
        _move(pose, 0.8)
        print("→ " + _fmt(pose))
        if args.save:
            _save(pose)
        return 0

    deg = False
    ant_a = 0.0                       # symmetric antenna angle for the plumb line
    def _ants():                      # crossed/symmetric → visual vertical ref
        return [ant_a, -ant_a]
    _move(pose, 0.8, _ants())
    print("Jogging head. PLUMB TRICK: type `ant 0.3` (raise until the two")
    print("antennae just cross) — the crossing point should sit straight above")
    print("the neck/base when roll is level. Then `save`. `q` to quit.")
    print("→ " + _fmt(pose) + f"  ant=±{ant_a:+.3f}")
    while True:
        try:
            raw = input("jog> ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = "q"
        if raw == "":
            continue
        if raw == "q":
            _move({k: 0.0 for k in _AXES}, 0.8, [0.0, 0.0])
            print("recentred. bye.")
            return 0
        if raw == "show":
            print("→ " + _fmt(pose) + f"  ant=±{ant_a:+.3f}")
            continue
        if raw == "deg":
            deg = not deg
            print(f"  p/r/y/ant now in {'DEGREES' if deg else 'radians'}")
            continue
        if raw == "zero":
            pose = {k: 0.0 for k in _AXES}
            ant_a = 0.0
            _move(pose, 0.8, _ants())
            print("→ " + _fmt(pose) + f"  ant=±{ant_a:+.3f}")
            continue
        if raw == "save":
            _save(pose)
            continue
        if "=" in raw:
            _apply_set(pose, raw)
            _move(pose, antennas=_ants())
            print("→ " + _fmt(pose))
            continue
        parts = raw.split()
        if len(parts) != 2:
            print("  ? usage: p -0.03 | r 2 | y 5 | ant 0.3 | "
                  "pitch=-0.06 | show/save/zero/deg/q")
            continue
        cmd, val = parts[0], parts[1]
        try:
            d = float(val)
        except ValueError:
            print("  ? not a number")
            continue
        if cmd == "ant":                      # plumb-line antenna angle (±sym)
            if deg:
                d = math.radians(d)
            ant_a += d
            _move(pose, antennas=_ants())
            print(f"→ ant=±{ant_a:+.3f}  (raise until they just cross; the")
            print("  crossing point should be plumb above the base when level)")
            continue
        amap = {"p": "pitch", "r": "roll", "y": "yaw",
                "z": "z", "x": "x", "xy": "y"}
        if cmd not in amap:
            print("  ? axis: p r y z x xy ant")
            continue
        axis = amap[cmd]
        if deg and axis in ("pitch", "roll", "yaw"):
            d = math.radians(d)
        pose[axis] += d
        _move(pose, antennas=_ants())
        print("→ " + _fmt(pose) + f"  ant=±{ant_a:+.3f}")


if __name__ == "__main__":
    raise SystemExit(main())
