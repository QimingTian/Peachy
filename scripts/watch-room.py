#!/usr/bin/env python3
"""Room-aware wake/rest state machine for Reachy Mini (laptop-driven, LAN REST).

EASY-WIN build: room brightness comes from **Peachy's head camera** (WebRTC
stream via light_sensor.py) — never the laptop webcam. Motion and speech go to the
daemon over the LAN. The light source is isolated in ``LightSensor`` so a
future build can swap algorithms without touching the state machine.

States (active hours 07:30–22:00 local):
  RESTING    dark/quiet room — relaxed pose (slightly down, antennae soft)
  SEMI_WAKE  lights just came on — peek up a little, antennae partway
  AWAKE      someone spoke (daemon speech_detected) — full up, nod hello,
             hand off to the conversation app; drop it when they leave
Outside active hours (22:00–07:30): DEEP_SLEEP — goto_sleep, ignore sensors.
Lights go back off during the day → back to RESTING.

Poses are interpolated between THIS robot's real goto_sleep / wake_up poses,
so run calibration once first:

  python scripts/ctl-toggle.py calibrate

Then:
  python scripts/watch-room.py --dry-run   # watch sensors, NO movement
  python scripts/watch-room.py             # live

Environment:
  REACHY_HOST / REACHY_PORT     daemon (default: auto via hostfind : 8000)
  REACHY_LIGHT_ON_DELTA         mean-luma rise over dark baseline = "lit" (25)
  REACHY_LIGHT_OFF_DELTA        fall back toward baseline = "dark"   (12)
  REACHY_NIGHT_START            deep-sleep start HH:MM (default 22:00)
  REACHY_NIGHT_END              deep-sleep end   HH:MM (default 07:30)
  REACHY_AWAKE_HOLD_S           stay AWAKE this long after last speech (30)
  REACHY_CONVO                  AWAKE→start conversation app (1=on, 0=off)
  REACHY_GREET                  little nod before the handoff (1=on, 0=off)
  REACHY_WARMUP                 AWAKE→stretch/TTS bridge while convo boots (1=on)
  REACHY_WARMUP_S               bridge target seconds (default 19)
  REACHY_WARMUP_LOCAL_AUDIO     speak from laptop instead of robot (0=off)
  REACHY_LIGHT_EMA              fast luma smoothing 0..1, lower = smoother (0.35)
  REACHY_LIGHT_SLOW_EMA         ambient-drift tracker rate (0.01; smaller = slower)
  REACHY_SETTLE_S               a state must hold this long before it commits (3.0)
  REACHY_IDLE_SLEEP_S           dark+quiet+still this long → DEEP_SLEEP (300)
  REACHY_MOTION_THRESH          mean camera frame Δ that counts as motion (4.0)

When the room empties (dark, no speech, no camera motion) for REACHY_IDLE_SLEEP_S
the watcher goes DEEP_SLEEP and stops the conversation app if it's running.

Light detection is ADAPTIVE by default: "lit" = fast signal jumps
REACHY_LIGHT_ON_DELTA above a slow ambient tracker, so gradual daylight /
shade / cloud drift does NOT trigger but the abrupt room-lights step does.
Use --fixed-baseline for the old one-time-dark-baseline behaviour.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CAL_FILE = _REPO / ".run" / "reachy_toggle_state.json"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hostfind import resolve_host
from light_sensor import LightSensor, OFF_DELTA, ON_DELTA

HOST = resolve_host()
PORT = int(os.environ.get("REACHY_PORT", "8000"))
AWAKE_HOLD_S = float(os.environ.get("REACHY_AWAKE_HOLD_S", "30"))
SETTLE_S = float(os.environ.get("REACHY_SETTLE_S", "3.0"))
_SLEEP_MODE = os.environ.get("REACHY_SLEEP_MODE", "gravcomp").strip().lower()
# Idle auto-sleep: dark + no speech + no camera motion for this long → DEEP_SLEEP
IDLE_SLEEP_S = float(os.environ.get("REACHY_IDLE_SLEEP_S", "300"))   # 5 min default
MOTION_THRESH = float(os.environ.get("REACHY_MOTION_THRESH", "4.0"))  # mean frame Δ (0–255)
# The dream: on AWAKE, greet + hand off to the conversation app.
CONVO_ON = os.environ.get("REACHY_CONVO", "1") != "0"   # AWAKE→start conversation
GREET_ON = os.environ.get("REACHY_GREET", "1") != "0"   # little nod before handoff
WARMUP_ON = os.environ.get("REACHY_WARMUP", "1") != "0"  # stretch + verbal bridge


def _parse_hhmm(s: str, default: dtime) -> dtime:
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


NIGHT_START = _parse_hhmm(os.environ.get("REACHY_NIGHT_START", "22:00"), dtime(22, 0))
NIGHT_END = _parse_hhmm(os.environ.get("REACHY_NIGHT_END", "07:30"), dtime(7, 30))


# ---------------------------------------------------------------- daemon REST

def _http(path: str, method: str, body: dict | None = None, timeout: float = 10.0) -> dict:
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
        raise SystemExit(
            f"Cannot reach daemon {HOST}:{PORT} ({e.reason}). Robot on / routable? "
            f"Try REACHY_HOST=reachy-mini.local"
        ) from e


def enable_motors() -> None:
    _http("/api/motors/set_mode/enabled", "POST")


_SLEEP_MODE = os.environ.get("REACHY_SLEEP_MODE", "gravcomp").strip().lower()


def speech_detected() -> bool:
    try:
        return bool(_http("/api/state/doa", "GET", timeout=4.0).get("speech_detected"))
    except SystemExit:
        return False  # transient network blip — don't crash the loop


def _head_offset() -> dict:
    """Neutral-pose correction from cal-head.py."""
    try:
        return json.loads(_CAL_FILE.read_text()).get("head_offset", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def goto(head_pose: dict, antennas: list[float], duration: float) -> None:
    _http("/api/move/goto", "POST",
          {"head_pose": dict(head_pose), "antennas": antennas,
           "duration": duration, "interpolation": "minjerk"})


def play(move: str) -> None:
    _http(f"/api/move/play/{move}", "POST", timeout=120.0)


_CONVO_PID = _REPO / ".run" / "conversation_app.pid"


def stop_conversation_if_running() -> bool:
    """When the room empties, also shut the conversation app down."""
    try:
        pid = int(_CONVO_PID.read_text().strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False
    subprocess.run([str(_REPO / "scripts" / "app-conversation.sh"), "stop"],
                   cwd=_REPO, capture_output=True)
    print("  (stopped conversation app — room idle)")
    return True


def start_conversation_if_needed() -> bool:
    """On AWAKE (someone's here): hand off to the official conversation app so
    it can actually talk with the person. Idempotent — the service script
    no-ops if it's already running."""
    if not CONVO_ON:
        return False
    try:
        pid = int(_CONVO_PID.read_text().strip())
        os.kill(pid, 0)
        return True  # already conversing
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass
    subprocess.run([str(_REPO / "scripts" / "app-conversation.sh"), "start"],
                   cwd=_REPO, capture_output=True)
    print("  (started conversation app — greeting visitor)")
    return True


# ----------------------------------------------------------------- poses

def _load_calibration() -> tuple[dict, list[float], dict, list[float]]:
    try:
        cal = json.loads(_CAL_FILE.read_text())["calibration"]
        s, w = cal["sleep"], cal["wake"]
        return (s["head_pose"], list(s["antennas"]),
                w["head_pose"], list(w["antennas"]))
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        raise SystemExit(
            "No pose calibration found. Run this once first:\n"
            "  python scripts/ctl-toggle.py calibrate\n"
            f"(looked in {_CAL_FILE})  [{e}]"
        )


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _pose_at(sleep_hp, sleep_ant, wake_hp, wake_ant, t: float):
    keys = ("x", "y", "z", "roll", "pitch", "yaw")
    hp = {k: _lerp(float(sleep_hp.get(k, 0)), float(wake_hp.get(k, 0)), t) for k in keys}
    ant = [_lerp(float(sleep_ant[i]), float(wake_ant[i]), t) for i in range(2)]
    return hp, ant


# ------------------------------------------------------------- scheduler

def is_night(now: datetime) -> bool:
    t = now.time()
    if NIGHT_START <= NIGHT_END:                 # same-day window
        return NIGHT_START <= t < NIGHT_END
    return t >= NIGHT_START or t < NIGHT_END      # wraps midnight (the usual case)


# ------------------------------------------------------------- main loop

def main() -> int:
    ap = argparse.ArgumentParser(description="Reachy Mini room-aware wake/rest watcher")
    ap.add_argument("--dry-run", action="store_true",
                    help="print sensors + intended state, never move the robot")
    ap.add_argument("--rest-t", type=float, default=0.22,
                    help="RESTING interp 0=sleep..1=wake (default 0.22)")
    ap.add_argument("--semi-t", type=float, default=0.55,
                    help="SEMI_WAKE interp 0..1 (default 0.55)")
    ap.add_argument("--period", type=float, default=0.6, help="sensor poll seconds")
    ap.add_argument("--move-dur", type=float, default=1.2, help="goto duration seconds")
    ap.add_argument("--settle", type=float, default=SETTLE_S,
                    help=f"seconds a state must hold before committing (default {SETTLE_S})")
    ap.add_argument("--lock-exposure", action="store_true",
                    help="(ignored — Peachy camera only; kept for CLI compat)")
    ap.add_argument("--fixed-baseline", action="store_true",
                    help="use a one-time dark baseline instead of the adaptive "
                         "ambient tracker (default: adaptive — better with daylight)")
    ap.add_argument("--idle-sleep", type=float, default=IDLE_SLEEP_S,
                    help=f"dark+quiet+still this long → DEEP_SLEEP (default {IDLE_SLEEP_S:.0f}s)")
    ap.add_argument("--motion-thresh", type=float, default=MOTION_THRESH,
                    help=f"mean frame Δ counting as motion (default {MOTION_THRESH})")
    args = ap.parse_args()

    # --dry-run never moves the robot, so it doesn't need pose calibration.
    POSES: dict = {}
    if not args.dry_run:
        s_hp, s_ant, w_hp, w_ant = _load_calibration()
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from head_pose import resolve_home
        h_res = resolve_home()
        if h_res:
            h_hp, h_ant = h_res
        else:
            h_hp, h_ant = w_hp, w_ant
        POSES = {
            "RESTING": _pose_at(s_hp, s_ant, h_hp, h_ant, args.rest_t),
            "SEMI_WAKE": _pose_at(s_hp, s_ant, h_hp, h_ant, args.semi_t),
            "AWAKE": (dict(h_hp), list(h_ant)),
        }

    stop = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("v", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("v", True))

    print(f"Daemon {HOST}:{PORT} | light=Peachy camera | night {NIGHT_START}-{NIGHT_END} "
          f"| {'DRY-RUN (no movement)' if args.dry_run else 'LIVE'}")
    sensor = LightSensor()
    sensor.adaptive = not args.fixed_baseline
    mode = "FIXED baseline" if args.fixed_baseline else "ADAPTIVE ambient tracker"
    print(f"Seeding light reference ({mode}) — keep lights as they are ~2s…")
    base = sensor.calibrate_dark(2.0)
    print(f"  reference mean-luma = {base:.1f}  "
          f"(lit if fast ≥ ref+{ON_DELTA:.0f}, dark if ≤ ref+{OFF_DELTA:.0f}; "
          f"ref {'drifts with daylight' if sensor.adaptive else 'fixed'})")

    if not args.dry_run:
        enable_motors()

    state = None
    awake_until = 0.0

    def apply(new_state: str) -> None:
        nonlocal state
        if new_state == state:
            return
        print(f"  → {new_state}")
        if args.dry_run:
            if new_state == "AWAKE" and WARMUP_ON:
                from warmup_bridge import run_warmup_bridge
                run_warmup_bridge(HOST, PORT, dry_run=True, convo_on=CONVO_ON)
            elif new_state == "AWAKE":
                print("  (would start conversation app)" if CONVO_ON else "")
            state = new_state
            return
        leaving_awake = state == "AWAKE" and new_state != "AWAKE"
        if new_state == "DEEP_SLEEP":
            stop_conversation_if_running()
            from sleep_gentle import gentle_sleep
            gentle_sleep(_http, HOST, PORT)
            print(f"  (asleep — mode={_SLEEP_MODE})")
        else:
            if state == "DEEP_SLEEP":   # waking from deep sleep — re-arm motors
                enable_motors()
            if leaving_awake:           # visitor left → drop the conversation
                stop_conversation_if_running()
            hp, ant = POSES[new_state]
            goto(hp, ant, args.move_dur)
            if new_state == "AWAKE":    # the dream: greet, bridge, hand off
                if WARMUP_ON:
                    from warmup_bridge import run_warmup_bridge
                    run_warmup_bridge(HOST, PORT, convo_on=CONVO_ON)
                else:
                    if GREET_ON:
                        nod = dict(hp)
                        nod["pitch"] = nod.get("pitch", 0.0) + 0.18
                        goto(nod, ant, 0.35)
                        goto(hp, ant, 0.35)
                    start_conversation_if_needed()
        state = new_state

    pending = None
    pending_since = 0.0
    last_log = 0.0
    last_active = time.time()
    while not stop["v"]:
        now = datetime.now()
        if is_night(now):
            apply("DEEP_SLEEP")          # schedule isn't noisy — switch at once
            pending = None
            time.sleep(min(30.0, args.period * 10))
            continue

        from light_sensor import apply_reanchor

        apply_reanchor(sensor)
        lit = sensor.lit()
        spoke = speech_detected() if lit else False
        tnow = time.time()
        if spoke:
            awake_until = tnow + AWAKE_HOLD_S

        # Activity = lights on, speech, or camera motion. No activity for
        # IDLE_SLEEP_S → the room emptied → deep sleep (+ stop conversation).
        moving = sensor.motion > args.motion_thresh
        if lit or spoke or moving:
            last_active = tnow
        idle = tnow - last_active

        if not lit and idle >= args.idle_sleep:
            target = "DEEP_SLEEP"
        elif not lit:
            target = "RESTING"
        elif tnow < awake_until:
            target = "AWAKE"
        else:
            target = "SEMI_WAKE"

        # Debounce: a new target must hold continuously for SETTLE_S before we
        # commit — kills webcam-noise flapping. AWAKE (someone spoke) is a
        # deliberate signal, so engage it immediately.
        if target == state:
            pending = None
        elif target == "AWAKE":
            apply("AWAKE")
            pending = None
        elif target == pending:
            if tnow - pending_since >= args.settle:
                apply(target)
                pending = None
        else:
            pending = target
            pending_since = tnow

        if tnow - last_log >= 5.0:
            pend = f" pending={pending}({tnow - pending_since:.0f}s)" if pending else ""
            print(f"[{now:%H:%M:%S}] raw={sensor.raw:.1f} ema={sensor.ema:.1f} "
                  f"ref={sensor.reference:.1f} Δ={sensor.ema - sensor.reference:+.1f} "
                  f"mot={sensor.motion:.1f} idle={idle:.0f}s "
                  f"lit={lit} speech={spoke} state={state}{pend}")
            last_log = tnow
        time.sleep(args.period)

    sensor.close()
    print("\nStopped. Robot left in its current pose; motors still enabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
