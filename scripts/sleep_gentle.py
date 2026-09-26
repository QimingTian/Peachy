"""Gentle sleep — smooth asymmetric antenna droop, head down, farewell, then snore.

Replaces the daemon's canned ``goto_sleep`` (fast slam + snore at start) with a
few minjerk ``goto`` keyframes calibrated to this robot's sleep pose. Uses
gotos (not streamed set_target) so sleep stays fast over slow links.

Order:
  1. Random Portal-turret farewell line (goodnight, shutting down, …)
  2. Smooth droop — left/right antennae at different rates; head pans down after
  3. Classic ``go_sleep.wav`` snore once settled
  4. Motor mode per ``REACHY_SLEEP_MODE`` (gravcomp default; falls back to hold)

Env:
  REACHY_SLEEP_DUR      total motion seconds (default 2.8)
  REACHY_SLEEP_ANT_LAG  right-antenna start lag, 0..1 (default 0.12)
  REACHY_SLEEP_HEAD_LAG head start lag, 0..1 (default 0.18)
  REACHY_SLEEP_GENTLE=0 fall back to ``play/goto_sleep``
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Callable

_REPO = Path(__file__).resolve().parent.parent
_STATE = _REPO / ".run" / "reachy_toggle_state.json"
_TURRET_PACK = "portalturret"
_SNORE = "go_sleep.wav"

_SLEEP_FAREWELLS = (
    "goodnight",
    "shutting_down",
    "hibernating",
    "nap_time",
    "goodbye",
    "sleep_mode_activated",
)

_WAKE_GREETINGS = (
    "hello",
    "activated",
    "whos_there",
    "deploying",
    "your_business_is_appreciated",
)

_WAKE_GREET = os.environ.get("REACHY_WAKE_GREET", "1").lower() not in ("0", "false", "off")

# Keyframe times along the move (0..1); three minjerk segments.
_KEYFRAME_U = (0.38, 0.72, 1.0)

_SLEEP_MODE = os.environ.get("REACHY_SLEEP_MODE", "gravcomp").strip().lower()
_GENTLE = os.environ.get("REACHY_SLEEP_GENTLE", "1").lower() not in ("0", "false", "off")
_SLEEP_DUR = float(os.environ.get("REACHY_SLEEP_DUR", "2.8"))
_ANT_R_LAG = float(os.environ.get("REACHY_SLEEP_ANT_LAG", "0.12"))
_HEAD_LAG = float(os.environ.get("REACHY_SLEEP_HEAD_LAG", "0.18"))

HttpFn = Callable[..., dict]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _progress(u: float, lag: float) -> float:
    """Map move time u∈[0,1] to eased progress with optional start lag."""
    lag = max(0.0, min(0.85, lag))
    if u <= lag:
        return 0.0
    return _smoothstep((u - lag) / (1.0 - lag))


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _pool_ids(manifest_key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    manifest_path = _REPO / "sounds" / _TURRET_PACK / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text())
        pool = data.get(manifest_key)
        if pool:
            return tuple(pool)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    return default


def _farewell_ids() -> tuple[str, ...]:
    return _pool_ids("sleep_farewells", _SLEEP_FAREWELLS)


def _greeting_ids() -> tuple[str, ...]:
    return _pool_ids("wake_greetings", _WAKE_GREETINGS)


def _post_quiet(http: HttpFn, path: str, *,
                body: dict | None = None, timeout: float = 10.0) -> bool:
    """Best-effort POST — never abort the sleep sequence (ctl-toggle raises SystemExit)."""
    try:
        http(path, "POST", body, timeout)
        return True
    except BaseException:
        return False


def wait_move_done(http: HttpFn, max_s: float = 22.0) -> None:
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            running = http("/api/move/running", "GET", timeout=4.0)
            if isinstance(running, list) and not running:
                return
        except BaseException:
            return
        time.sleep(0.2)


def settle_asleep(http: HttpFn) -> None:
    """Hold the asleep pose per REACHY_SLEEP_MODE.

    gravcomp needs Placo on the daemon — if it 500s, fall back to enabled so
    the pose is still held (same as REACHY_SLEEP_MODE=hold).
    """
    if _SLEEP_MODE == "limp":
        steps = ("/api/move/stop", "/api/motors/set_mode/disabled")
    elif _SLEEP_MODE == "hold":
        steps = ("/api/motors/set_mode/enabled",)
    else:
        steps = ("/api/motors/set_mode/gravity_compensation",)
    ok = all(_post_quiet(http, p) for p in steps)
    if not ok and _SLEEP_MODE not in ("limp", "hold"):
        _post_quiet(http, "/api/motors/set_mode/enabled")


def _play_turret_line(host: str, port: int, pool: tuple[str, ...]) -> str | None:
    """Upload if needed and play a random line from a manifest sound pool."""
    import sys

    sys.path.insert(0, str(_REPO / "scripts"))
    from sound_sync import load_manifest, play_file, sync_pack

    sid = random.choice(pool)
    try:
        manifest = load_manifest(_TURRET_PACK)
        meta = manifest.get("sounds", {}).get(sid)
        if not meta:
            return None
        sync_pack(host, port, _TURRET_PACK, only=[sid])
        play_file(host, port, meta["file"])
        return sid
    except Exception:
        return None


def play_wake_greeting(host: str, port: int) -> str | None:
    """Random hello/greeting after wake_up settles. Returns sound id or None."""
    if not _WAKE_GREET:
        return None
    return _play_turret_line(host, port, _greeting_ids())


def _play_farewell(http: HttpFn, host: str, port: int) -> str | None:
    return _play_turret_line(host, port, _farewell_ids())


def _play_snore(http: HttpFn) -> None:
    _post_quiet(http, "/api/media/play_sound",
                body={"file": _SNORE}, timeout=12.0)


def _enable(http: HttpFn) -> None:
    http("/api/motors/set_mode/enabled", "POST", timeout=10.0)


def _goto(http: HttpFn, head_pose: dict, antennas: list[float], duration: float) -> None:
    http("/api/move/goto", "POST",
         {"head_pose": head_pose, "antennas": antennas,
          "duration": duration, "interpolation": "minjerk"},
         timeout=max(30.0, duration + 15.0))


def _interp_pose(cur_hp: dict, cur_ant: list[float],
                 sleep_hp: dict, sleep_ant: list[float],
                 ht: float, alt: float, art: float):
    keys = ("x", "y", "z", "roll", "pitch", "yaw")
    hp = {k: _lerp(float(cur_hp.get(k, 0)), float(sleep_hp.get(k, 0)), ht) for k in keys}
    ant = [_lerp(float(cur_ant[i]), float(sleep_ant[i]), t)
           for i, t in enumerate((alt, art))]
    return hp, ant


def _smooth_droop(http: HttpFn, cur_hp: dict, cur_ant: list[float],
                  sleep_hp: dict, sleep_ant: list[float], duration: float) -> None:
    """Three minjerk gotos — left antenna leads, right lags, head follows."""
    wait_move_done(http)
    seg = duration / len(_KEYFRAME_U)
    for u in _KEYFRAME_U:
        alt = _progress(u, lag=0.0)
        art = _progress(u, lag=_ANT_R_LAG)
        ht = _progress(u, lag=_HEAD_LAG)
        hp, ant = _interp_pose(cur_hp, cur_ant, sleep_hp, sleep_ant, ht, alt, art)
        wait_move_done(http)
        try:
            _goto(http, hp, ant, seg)
        except BaseException:
            pass
        time.sleep(seg + 0.05)


def gentle_sleep(http: HttpFn, host: str, port: int, *,
                 fdata: dict | None = None) -> str:
    """Run the gentle sleep sequence. Returns move label for state file."""
    fdata = fdata or _load_state()
    _enable(http)

    if not _GENTLE:
        http("/api/move/play/goto_sleep", "POST", timeout=120.0)
        settle_asleep(http)
        return "goto_sleep"

    try:
        cal = fdata["calibration"]["sleep"]
        sleep_hp = cal["head_pose"]
        sleep_ant = list(cal["antennas"])
    except (KeyError, TypeError):
        http("/api/move/play/goto_sleep", "POST", timeout=120.0)
        settle_asleep(http)
        return "goto_sleep"

    live = http("/api/state/full", "GET", timeout=8.0)
    cur_hp = dict(live.get("head_pose") or sleep_hp)
    cur_ant = list(live.get("antennas_position") or sleep_ant)

    farewell = _play_farewell(http, host, port)
    if farewell:
        time.sleep(0.25)

    _smooth_droop(http, cur_hp, cur_ant, sleep_hp, sleep_ant, _SLEEP_DUR)
    wait_move_done(http)
    _play_snore(http)
    settle_asleep(http)
    return "gentle_sleep"
