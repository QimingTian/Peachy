#!/usr/bin/env python3
"""Enhanced sleep/wake toggle + expression demo menu (laptop-driven, LAN REST).

Same toggle behaviour as ctl-toggle.py (shares .run/reachy_toggle_state.json
and the calibration captured by `ctl-toggle.py calibrate`), but when it
WAKES the robot it drops into a CLI menu of expressions you can demo. The
first menu item is "Back to CLI" — so after a demo you just press Enter to
leave the menu, then run this again to toggle back to sleep.

  python scripts/ctl-express.py            # toggle (wake → menu; or sleep)
  python scripts/ctl-express.py wake       # force wake → menu
  python scripts/ctl-express.py sleep      # force sleep
  python scripts/ctl-express.py menu       # just the menu (assumes awake)

Needs calibration once:  python scripts/ctl-toggle.py calibrate
Env: REACHY_HOST (auto via hostfind), REACHY_PORT (8000)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STATE = _REPO / ".run" / "reachy_toggle_state.json"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hostfind import resolve_host
HOST = resolve_host()
PORT = int(os.environ.get("REACHY_PORT", "8000"))

# Software safety clamps (radians) from the daemon core-concept doc.
_PITCH_ROLL = math.radians(38)   # ±40° limit, stay just inside
_HEAD_YAW = math.radians(60)


def _http(path: str, method: str, body: dict | None = None, timeout: float = 60.0) -> dict:
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


def _state_file() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(fdata: dict, state: str, move: str) -> None:
    fdata["state"] = state
    fdata["last_move"] = move
    fdata["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(fdata, indent=2) + "\n")


def _calibration(fdata: dict):
    try:
        cal = fdata["calibration"]
        w = cal["wake"]
        return dict(w["head_pose"]), list(w["antennas"])
    except (KeyError, TypeError):
        raise SystemExit("No calibration. Run once:  python scripts/ctl-toggle.py calibrate")


def _home_pose(fdata: dict):
    """Expression neutral = saved head home, else calibrated wake."""
    from head_pose import resolve_home

    resolved = resolve_home(fdata)
    if resolved:
        return resolved
    return _calibration(fdata)


def _clamp(hp: dict) -> dict:
    hp = dict(hp)
    hp["pitch"] = max(-_PITCH_ROLL, min(_PITCH_ROLL, hp.get("pitch", 0.0)))
    hp["roll"] = max(-_PITCH_ROLL, min(_PITCH_ROLL, hp.get("roll", 0.0)))
    hp["yaw"] = max(-_HEAD_YAW, min(_HEAD_YAW, hp.get("yaw", 0.0)))
    return hp


def _goto(hp: dict, ant: list[float], dur: float) -> None:
    _http("/api/move/goto", "POST",
          {"head_pose": _clamp(hp), "antennas": ant,
           "duration": dur, "interpolation": "minjerk"})


def _play(move: str) -> None:
    _http(f"/api/move/play/{move}", "POST", timeout=120.0)


def _enable() -> None:
    _http("/api/motors/set_mode/enabled", "POST")


def _sleep_quiet() -> str:
    """Slow droop + farewell + snore (see sleep_gentle.py)."""
    from sleep_gentle import gentle_sleep

    return gentle_sleep(_http, HOST, PORT, fdata=_state_file())


# ---- expressions: keyframes are (Δhead vs wake pose, absolute antennas, dur)

def _expr_frames(name: str, wake_hp: dict, wake_ant: list[float]):
    base = dict(wake_hp)
    aL, aR = wake_ant
    up = [aL + 0.5, aR - 0.5]      # antennae perked outward/up
    soft = [aL, aR]
    d = math.radians

    def hp(**dd):
        p = dict(base)
        for k, v in dd.items():
            p[k] = base.get(k, 0.0) + v
        return p

    if name == "yes":
        return [(hp(pitch=d(18)), soft, 0.35), (hp(pitch=d(-12)), soft, 0.35),
                (hp(pitch=d(18)), soft, 0.35), (hp(), soft, 0.4)]
    if name == "no":
        return [(hp(yaw=d(28)), soft, 0.35), (hp(yaw=d(-28)), soft, 0.45),
                (hp(yaw=d(28)), soft, 0.45), (hp(), soft, 0.4)]
    if name == "curious":
        return [(hp(roll=d(22), yaw=d(12)), up, 0.7), (hp(), soft, 0.7)]
    if name == "lookaround":
        return [(hp(yaw=d(45)), soft, 0.8), (hp(yaw=d(-45)), soft, 1.0),
                (hp(pitch=d(-14)), up, 0.7), (hp(), soft, 0.7)]
    if name == "excited":
        return [(hp(pitch=d(-10)), up, 0.3), (hp(roll=d(10)), up, 0.25),
                (hp(roll=d(-10)), up, 0.25), (hp(pitch=d(-10)), up, 0.25),
                (hp(), soft, 0.4)]
    if name == "shy":
        return [(hp(pitch=d(20), yaw=d(-22)), [aL - 0.4, aR + 0.4], 0.9),
                (hp(), soft, 0.8)]
    if name == "stretch":
        return [(hp(pitch=d(-20)), up, 0.9), (hp(roll=d(14)), up, 0.5),
                (hp(roll=d(-14)), up, 0.5), (hp(), soft, 0.7)]
    if name == "wink":
        return [(hp(roll=d(16), yaw=d(10)), [aL + 0.55, aR - 0.15], 0.45),
                (hp(roll=d(8)), soft, 0.35), (hp(), soft, 0.45)]
    if name == "listen":
        return [(hp(pitch=d(-18)), up, 0.55), (hp(pitch=d(-12), yaw=d(18)), up, 0.5),
                (hp(pitch=d(-12), yaw=d(-18)), up, 0.55), (hp(), soft, 0.5)]
    return []


_MENU = [
    ("Back to CLI", None),
    ("Nod  — “yes”", "yes"),
    ("Shake — “no”", "no"),
    ("Curious tilt", "curious"),
    ("Look around", "lookaround"),
    ("Excited wiggle", "excited"),
    ("Shy / bashful", "shy"),
    ("Wake-up stretch", "stretch"),
    ("Wink", "wink"),
    ("Listening", "listen"),
    ("Sleep now (exit)", "__sleep__"),
]


def _run_menu(fdata: dict) -> None:
    wake_hp, wake_ant = _home_pose(fdata)
    while True:
        print("\nExpressions — pick one (Enter = back to CLI):")
        for i, (label, _) in enumerate(_MENU):
            print(f"  {i}. {label}")
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if raw == "":
            return  # first item / Back to CLI
        if not raw.isdigit() or int(raw) >= len(_MENU):
            print("  ?")
            continue
        idx = int(raw)
        label, key = _MENU[idx]
        if key is None:
            return
        if key == "__sleep__":
            print("→ gentle sleep …")
            move = _sleep_quiet()
            _save(fdata, "asleep", move)
            print("✓ asleep — quiet")
            sys.exit(0)
        print(f"→ {label}")
        for hp, ant, dur in _expr_frames(key, wake_hp, wake_ant):
            _goto(hp, ant, dur)
            time.sleep(dur + 0.05)


_EXPR_KEYS = ("yes", "no", "curious", "lookaround", "excited", "shy", "stretch",
              "wink", "listen")


def _run_one(fdata: dict, key: str) -> None:
    """Non-interactive: play a single expression once (for the dashboard)."""
    wake_hp, wake_ant = _home_pose(fdata)
    for hp, ant, dur in _expr_frames(key, wake_hp, wake_ant):
        _goto(hp, ant, dur)
        time.sleep(dur + 0.05)


def main() -> int:
    ap = argparse.ArgumentParser(description="Reachy toggle + expression demo menu")
    ap.add_argument("command", nargs="?", default="toggle",
                    choices=("toggle", "wake", "sleep", "menu", "express"))
    ap.add_argument("name", nargs="?",
                    help=f"expression for `express`: {', '.join(_EXPR_KEYS)}")
    args = ap.parse_args()
    fdata = _state_file()
    cur = fdata.get("state")

    if args.command == "express":
        if args.name not in _EXPR_KEYS:
            print(f"unknown expression {args.name!r}; choose: {', '.join(_EXPR_KEYS)}")
            return 2
        print(f"→ {args.name}")
        _enable()
        _run_one(fdata, args.name)
        print("✓ done")
        return 0

    if args.command == "sleep" or (args.command == "toggle" and cur == "awake"):
        print("→ gentle sleep …")
        move = _sleep_quiet()
        _save(fdata, "asleep", move)
        print("✓ asleep")
        return 0

    if args.command == "menu":
        _run_menu(fdata)
        return 0

    # wake (explicit, or toggle from asleep/unknown)
    print("→ wake_up …")
    _enable()
    _play("wake_up")
    from head_pose import settle_home
    if settle_home(_http, HOST, PORT, fdata):
        print("  (settled to saved head home)")
    from sleep_gentle import play_wake_greeting
    greeting = play_wake_greeting(HOST, PORT)
    if greeting:
        print(f"  (greeting: {greeting.replace('_', ' ')})")
    _save(fdata, "awake", "wake_up")
    print("✓ awake")
    _run_menu(fdata)
    print("Back at CLI. Run again to toggle back to sleep.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
