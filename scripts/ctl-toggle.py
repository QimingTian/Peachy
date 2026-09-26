#!/usr/bin/env python3
"""Sleep/wake TOGGLE for Reachy Mini over the LAN (daemon REST — no SSH, no Control).

Runs from your laptop; commands are beamed to the daemon at REACHY_HOST:8000.
"Sleep" = goto_sleep (head + antennae down; camera/mic stay live so it can
still "see light"). "Wake" = wake_up. The daemon keeps running either way.

State lives in a small JSON file (default .run/reachy_toggle_state.json) so
``toggle`` always does "the next thing in the sequence" reliably, even though
a motors-disabled head rests under gravity and is ambiguous from pose alone.
``calibrate`` records this robot's real sleep vs wake poses so ``status`` can
cross-check the file against what the robot is actually doing.

Environment:
  REACHY_HOST   daemon IP / hostname (default: auto via hostfind)
  REACHY_PORT   default 8000

Examples:
  python scripts/ctl-toggle.py            # toggle: do the opposite of last
  python scripts/ctl-toggle.py status     # show state, no movement
  python scripts/ctl-toggle.py sleep      # force sleep
  python scripts/ctl-toggle.py wake       # force wake
  python scripts/ctl-toggle.py calibrate  # record sleep & wake poses
  REACHY_HOST=reachy-mini.local python scripts/ctl-toggle.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STATE_FILE = _REPO / ".run" / "reachy_toggle_state.json"

# Head height (metres) gap below which we treat the pose as "asleep", measured
# relative to the calibrated wake/sleep z. Used only as a sanity cross-check.
_Z_MARGIN = 0.004


def _http(host: str, port: int, path: str, method: str, timeout: float,
          body: dict | None = None) -> dict:
    url = f"http://{host}:{port}{path}"
    if method == "POST":
        data = json.dumps(body).encode() if body is not None else b"{}"
    else:
        data = None
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} on {path}: {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise SystemExit(
            f"Cannot reach daemon at {host}:{port} ({e.reason}). "
            f"Is the robot on and routable? Try REACHY_HOST=reachy-mini.local"
        ) from e


def _get_state(host: str, port: int, timeout: float = 5.0) -> dict:
    return _http(host, port, "/api/state/full", "GET", timeout)


def _play(host: str, port: int, move: str, timeout: float = 120.0) -> dict:
    return _http(host, port, f"/api/move/play/{move}", "POST", timeout)


def _enable(host: str, port: int) -> None:
    """Motors MUST be enabled or play/goto_sleep/wake_up are silent no-ops
    (the daemon accepts the move but nothing actuates)."""
    _http(host, port, "/api/motors/set_mode/enabled", "POST", 10.0)


def _wait_move_done(host: str, port: int, max_s: float = 18.0) -> None:
    """Poll /api/move/running until empty (or timeout). `play/wake_up` returns
    its UUID immediately and the canned animation runs ASYNC — issuing another
    move while it's still running makes the robot fight itself (jerky head,
    antennae spazzing). Always wait before chaining moves."""
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            running = _http(host, port, "/api/move/running", "GET", 4.0)
            if isinstance(running, list) and not running:
                return
        except SystemExit:
            return
        time.sleep(0.2)


def _settle_centered(host: str, port: int, fdata: dict, dur: float = 0.8) -> None:
    """After wake_up, snap to saved head *home* (dashboard Head tune / cal-head)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from head_pose import settle_home

    if settle_home(_http, host, port, fdata, dur=dur):
        return
    # Fallback: no home saved — legacy wake + offset delta
    try:
        cal = fdata["calibration"]["wake"]
        hp = dict(cal.get("head_pose", {}))
        ant = list(cal.get("antennas", [0.0, 0.0]))
    except (KeyError, TypeError):
        try:
            st = _get_state(host, port, timeout=5.0)
            hp = dict(st.get("head_pose", {}))
            ant = list(st.get("antennas_position") or [0.0, 0.0])
        except SystemExit:
            return
    _wait_move_done(host, port)
    for k, v in fdata.get("head_offset", {}).items():
        if k in hp and fdata.get("head_offset_mode") == "delta":
            hp[k] = float(hp[k]) + float(v)
    try:
        _http(host, port, "/api/move/goto", "POST", 30.0,
              body={"head_pose": hp, "antennas": ant,
                    "duration": dur, "interpolation": "minjerk"})
    except SystemExit:
        pass


# How to sleep, via REACHY_SLEEP_MODE:
#   gravcomp (default) — hold the asleep pose (antennae+head DOWN) with
#                        gravity-compensation: low torque, quiet, pose kept.
#   hold               — keep motors enabled holding the pose (firmest;
#                        loudest if it whines).
#   limp               — release the move + cut torque: silent, zero wear,
#                        but antennae spring back UP and head relaxes.
_SLEEP_MODE = os.environ.get("REACHY_SLEEP_MODE", "gravcomp").strip().lower()


def _true_sleep(host: str, port: int, fdata: dict) -> str:
    """Slow asymmetric droop + random farewell + snore, then settle per
    REACHY_SLEEP_MODE. Do NOT /api/move/stop unless going limp — stopping the
    move releases the held target and the springy antennae pop back up."""
    from sleep_gentle import gentle_sleep

    def http(path: str, method: str, body: dict | None = None, timeout: float = 10.0):
        return _http(host, port, path, method, timeout, body)

    return gentle_sleep(http, host, port, fdata=fdata)


def _load_file() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_file(data: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _release_for_motion(host: str, port: int) -> str:
    """Conversation app holds the SDK — stop it before wake/sleep."""
    notes: list[str] = []
    convo = _REPO / "scripts" / "app-conversation.sh"
    if convo.is_file():
        p = subprocess.run(
            [str(convo), "status"], cwd=_REPO, capture_output=True, text=True, timeout=15)
        if p.returncode == 0:
            subprocess.run([str(convo), "stop"], cwd=_REPO,
                         capture_output=True, text=True, timeout=25)
            notes.append("conversation stopped")
            time.sleep(1.8)
    try:
        _http(host, port, "/api/move/stop", "POST", 6.0)
    except SystemExit:
        pass
    _enable(host, port)
    time.sleep(0.25)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from motion_ready import ensure_motion_ready

    revived = ensure_motion_ready(host, port)
    if revived:
        notes.append(revived)
    return " · ".join(notes)


def _pose_guess(state: dict, cal: dict) -> str | None:
    """Best-effort 'asleep'/'awake' from pose, only if we have calibration."""
    if not cal or "sleep" not in cal or "wake" not in cal:
        return None
    z = state.get("head_pose", {}).get("z")
    if z is None:
        return None
    sleep_z = cal["sleep"]["head_pose"]["z"]
    wake_z = cal["wake"]["head_pose"]["z"]
    midpoint = (sleep_z + wake_z) / 2.0
    if abs(wake_z - sleep_z) < _Z_MARGIN:
        return None  # poses too close to tell apart
    # Whichever calibrated z the live z is nearer to.
    return "asleep" if abs(z - sleep_z) < abs(z - wake_z) else "awake"


def _do(host: str, port: int, target: str, fdata: dict) -> None:
    prep = _release_for_motion(host, port)
    if prep:
        print(f"  ({prep})")
    if target == "asleep":
        print(f"→ sleep @ {host}:{port} (gentle droop + snore, mode={_SLEEP_MODE})…")
        move = _true_sleep(host, port, fdata)
        kept = "limp (antennae up)" if _SLEEP_MODE == "limp" \
            else "pose held (antennae down)"
        print(f"  ✓ asleep — {kept}")
    else:
        move = "wake_up"
        print(f"→ {move} @ {host}:{port} (this can take a few seconds)…")
        _enable(host, port)                   # else wake_up won't actuate
        _play(host, port, move)
        _wait_move_done(host, port)
        _settle_centered(host, port, fdata)   # apply head_offset (play/wake_up
                                              # itself ignores it → drift)
        if fdata.get("head_offset") or fdata.get("head_home"):
            print("  (settled to saved head home)")
        from sleep_gentle import play_wake_greeting
        greeting = play_wake_greeting(host, port)
        if greeting:
            print(f"  (greeting: {greeting.replace('_', ' ')})")
    fdata["state"] = target
    fdata["last_move"] = move
    fdata["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_file(fdata)
    print(f"✓ now: {target}  (saved → {_STATE_FILE.relative_to(_REPO)})")


def main() -> int:
    p = argparse.ArgumentParser(description="Reachy Mini sleep/wake toggle (LAN REST)")
    p.add_argument(
        "command",
        nargs="?",
        default="toggle",
        choices=("toggle", "sleep", "wake", "status", "calibrate", "calibrate-sleep"),
        help="default: toggle (opposite of last known state)",
    )
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from hostfind import resolve_host
    p.add_argument("--host", default=resolve_host())
    p.add_argument("--port", type=int, default=int(os.environ.get("REACHY_PORT", "8000")))
    args = p.parse_args()
    host, port = args.host, args.port

    fdata = _load_file()
    file_state = fdata.get("state")  # 'asleep' | 'awake' | None

    if args.command == "status":
        live = _get_state(host, port)
        cal = fdata.get("calibration", {})
        guess = _pose_guess(live, cal)
        hp = live.get("head_pose", {})
        ant = live.get("antennas_position") or [0.0, 0.0]
        if isinstance(ant, (list, tuple)) and len(ant) >= 2:
            ant_s = f"[{float(ant[0]):+.2f}, {float(ant[1]):+.2f}]"
        else:
            ant_s = str(ant)
        print(f"host             : {host}:{port}")
        print(f"control_mode     : {live.get('control_mode')}")
        print(f"head z / pitch   : {hp.get('z'):+.4f} m / {hp.get('pitch'):+.4f} rad")
        print(f"antennas         : {ant_s}")
        print(f"file state       : {file_state or '(unknown)'}")
        if fdata.get("updated"):
            print(f"last updated     : {fdata['updated']}")
        print(f"pose guess       : {guess or '(no calibration — run: calibrate)'}")
        if guess and file_state and guess != file_state:
            print("⚠ file vs pose disagree — toggle trusts file; use sleep/wake or calibrate.")
        return 0

    if args.command in ("calibrate", "calibrate-sleep"):
        lower_mm = float(os.environ.get("REACHY_SLEEP_LOWER_MM", "5"))
        if args.command == "calibrate-sleep":
            print(f"Re-recording sleep pose only (lower head {lower_mm:.0f} mm vs goto_sleep).")
            _enable(host, port)
            if fdata.get("state") != "awake":
                print("→ wake_up …")
                _play(host, port, "wake_up")
                time.sleep(3.0)
            print("→ goto_sleep …")
            _play(host, port, "goto_sleep")
            time.sleep(3.0)
            sleep_state = _get_state(host, port)
            hp = dict(sleep_state.get("head_pose") or {})
            if lower_mm > 0:
                hp["z"] = float(hp.get("z", 0.0)) - lower_mm / 1000.0
            fdata.setdefault("calibration", {})
            fdata["calibration"]["sleep"] = {
                "head_pose": hp,
                "antennas": sleep_state.get("antennas_position"),
            }
            fdata["state"] = "asleep"
            fdata["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save_file(fdata)
            print(f"✓ sleep calibrated. z={hp.get('z'):+.4f}  pitch={hp.get('pitch'):+.4f} rad")
            print("  Run `sleep` to preview; set REACHY_SLEEP_LOWER_MM=0 to skip z tweak.")
            return 0

        print("Calibration moves the robot. It will wake, then sleep.")
        _enable(host, port)                   # else the moves are no-ops
        print("→ wake_up …")
        _play(host, port, "wake_up")
        time.sleep(3.0)                       # canned moves take ~2.5s
        wake_state = _get_state(host, port)
        print("→ goto_sleep …")
        _play(host, port, "goto_sleep")
        time.sleep(3.0)
        sleep_state = _get_state(host, port)
        sleep_hp = dict(sleep_state.get("head_pose") or {})
        if lower_mm > 0:
            sleep_hp["z"] = float(sleep_hp.get("z", 0.0)) - lower_mm / 1000.0
        fdata.setdefault("calibration", {})
        fdata["calibration"]["wake"] = {"head_pose": wake_state.get("head_pose", {}),
                                        "antennas": wake_state.get("antennas_position")}
        fdata["calibration"]["sleep"] = {"head_pose": sleep_hp,
                                         "antennas": sleep_state.get("antennas_position")}
        fdata["state"] = "asleep"
        fdata["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_file(fdata)
        wz = wake_state["head_pose"]["z"]
        sz = sleep_hp["z"]
        print(f"✓ calibrated. wake z={wz:+.4f}  sleep z={sz:+.4f}  Δ={wz - sz:+.4f} m")
        if lower_mm > 0:
            print(f"  (sleep z lowered {lower_mm:.0f} mm — REACHY_SLEEP_LOWER_MM=0 for stock goto_sleep)")
        if abs(wz - sz) < _Z_MARGIN:
            print("⚠ poses very close in z — pose cross-check disabled; "
                  "toggle still works off the state file.")
        return 0

    if args.command == "sleep":
        _do(host, port, "asleep", fdata)
        return 0
    if args.command == "wake":
        _do(host, port, "awake", fdata)
        return 0

    # toggle: trust the file; if unknown, peek at pose; else assume awake → sleep.
    if file_state in ("asleep", "awake"):
        nxt = "awake" if file_state == "asleep" else "asleep"
    else:
        guess = _pose_guess(_get_state(host, port), fdata.get("calibration", {}))
        if guess:
            nxt = "awake" if guess == "asleep" else "asleep"
            print(f"(no saved state; pose looks {guess} → going {nxt})")
        else:
            nxt = "asleep"
            print("(no saved state and no calibration; assuming awake → sleeping. "
                  "Run `calibrate` once for reliable auto-detect.)")
    _do(host, port, nxt, fdata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
