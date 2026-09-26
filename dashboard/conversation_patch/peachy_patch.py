"""Peachy patch for the official conversation app (installed by scripts/app-patch.sh).

Reads ~/.peachy/motion.json on the robot (re-read live, no restart needed):

  body_yaw      held body direction, radians. The app's whole world is rotated so
                its "forward" is this direction: every pose it sends is turned by
                it and every pose it reads back is turned back. Changes glide at
                _YAW_RATE so the 100 Hz pose stream never jumps.
  head_pitch    extra head pitch, radians (positive looks down), added in the
                head's own frame on top of whatever the app does, while "free".
                Glides at _PITCH_RATE; back to 0 while tucked/lifted.
  breath_scale  size of the idle breathing (head bob + antenna sway), 1.0 = stock.
  idle_every_s  seconds of silence before an idle action (dance / emotion / look);
                stock 180, 0 = never.
  mode          "free" (stock: the app drives head and antennas), "tucked" or
                "lifted": the app keeps running but the head holds that pose,
                the mic is muted and idle actions stop. Semi-awake = "tucked".
  tucked/lifted {"head": [x, y, z, roll, pitch, yaw], "antennas": [l, r]}, metres
                and radians relative to the body.
  mic_ns        XVF3800 stationary noise floor PP_MIN_NS written at app start,
                default 0.15 (stock 0.8).
  agc_max       XVF3800 PP_AGCMAXGAIN written at app start, default 10 (stock).

Loaded by one import line in reachy_mini_conversation_app/main.py.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import threading
import time
from pathlib import Path

import numpy as np
from reachy_mini import ReachyMini
from scipy.spatial.transform import Rotation

from reachy_mini_conversation_app.console import LocalStream
from reachy_mini_conversation_app.conversation_handler import ConversationHandler
from reachy_mini_conversation_app.huggingface_realtime import HuggingFaceRealtimeHandler
from reachy_mini_conversation_app.moves import BreathingMove

_FILE = Path.home() / ".peachy" / "motion.json"
_YAW_LIMIT = math.radians(160)
_YAW_RATE = math.radians(90)
_PITCH_LIMIT = math.radians(30)
_PITCH_RATE = math.radians(60)
# Pose spring stiffness (rad/s), critically damped: tucking ~1.2 s, waking ~0.8 s.
_OMEGA_HOLD = 4.0
_OMEGA_WAKE = 6.0

_lock = threading.Lock()
_hold_lock = threading.Lock()
_cache: dict = {"at": 0.0, "mtime": None, "data": {}}
_yaw = {"cur": None, "at": 0.0}
_pitch = {"cur": 0.0, "at": 0.0}
# Held-pose spring over [x, y, z, roll, pitch, yaw, ant_l, ant_r, blend].
_hold: dict = {"x": None, "v": np.zeros(9), "at": 0.0}


def settings() -> dict:
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < 0.1:
            return _cache["data"]
        _cache["at"] = now
        try:
            mtime = _FILE.stat().st_mtime
            if mtime != _cache["mtime"]:
                data = json.loads(_FILE.read_text())
                _cache["data"] = data if isinstance(data, dict) else {}
                _cache["mtime"] = mtime
        except (OSError, ValueError):
            _cache["data"], _cache["mtime"] = {}, None
        return _cache["data"]


def _number(key: str, default: float) -> float:
    try:
        return float(settings().get(key, default))
    except (TypeError, ValueError):
        return default


def held_yaw() -> float:
    """Current held direction, slewed toward the file's target."""
    target = max(-_YAW_LIMIT, min(_YAW_LIMIT, _number("body_yaw", 0.0)))
    now = time.monotonic()
    with _lock:
        cur = _yaw["cur"]
        if cur is None:
            cur = target
        else:
            step = _YAW_RATE * min(0.2, now - _yaw["at"])
            cur += max(-step, min(step, target - cur))
        _yaw["cur"], _yaw["at"] = cur, now
        return cur


def held_pitch(md: str) -> float:
    """Current extra head pitch, slewed toward the file's target (0 unless free)."""
    target = 0.0
    if md == "free":
        target = max(-_PITCH_LIMIT, min(_PITCH_LIMIT, _number("head_pitch", 0.0)))
    now = time.monotonic()
    with _lock:
        step = _PITCH_RATE * min(0.2, now - _pitch["at"])
        _pitch["cur"] += max(-step, min(step, target - _pitch["cur"]))
        _pitch["at"] = now
        return _pitch["cur"]


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 1], m[1, 0], m[1, 1] = c, -s, s, c
    return m


def _ry(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    return m


def mode() -> str:
    m = settings().get("mode", "free")
    return m if m in ("tucked", "lifted") else "free"


def _pose_vec(m: np.ndarray) -> np.ndarray:
    return np.r_[m[:3, 3], Rotation.from_matrix(m[:3, :3]).as_euler("xyz")]


def _pose_mat(v: np.ndarray) -> np.ndarray:
    """4x4 from [x, y, z, roll, pitch, yaw] (extrinsic xyz, as Rotation.from_euler("xyz"))."""
    cr, sr = math.cos(v[3]), math.sin(v[3])
    cp, sp = math.cos(v[4]), math.sin(v[4])
    cy, sy = math.cos(v[5]), math.sin(v[5])
    m = np.eye(4)
    m[:3, :3] = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                 [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                 [-sp, cp * sr, cp * cr]]
    m[:3, 3] = v[:3]
    return m


def _target(robot, md: str, h: float) -> np.ndarray | None:
    """[pose(6), antennas(2), blend] the head should glide to, or None to pass through.
    Caller holds _hold_lock."""
    if md == "free":
        return None if _hold["x"] is None or _hold["x"][8] < 1e-3 else np.r_[_hold["x"][:8], 0.0]
    p = settings().get(md) or {}
    try:
        head = np.asarray(p["head"], dtype=np.float64)[:6]
        ant = np.asarray(p["antennas"], dtype=np.float64)[:2]
    except (KeyError, TypeError, ValueError):
        return None
    if _hold["x"] is None:  # first tick: start from where the robot really is
        cur = _rz(-h) @ np.asarray(_orig_head_pose(robot), dtype=np.float64)
        _, cur_ant = _orig_joints(robot)
        _hold["x"] = np.r_[_pose_vec(cur), np.asarray(cur_ant, dtype=np.float64)[:2], 1.0]
        _hold["v"][:] = 0.0
        _hold["at"] = time.monotonic()
    elif _hold["x"][8] < 1e-3:  # coming from free: blend from the app's pose instead
        _hold["x"] = np.r_[head, ant, 0.0]
        _hold["v"][:] = 0.0
    return np.r_[head, ant, 1.0]


def _step(target: np.ndarray) -> np.ndarray:
    now = time.monotonic()
    dt = min(0.05, max(0.0, now - _hold["at"]))
    _hold["at"] = now
    x, v = _hold["x"], _hold["v"]
    if x is None:
        x = np.r_[target[:8], 0.0]
    err = target - x
    err[3:6] = (err[3:6] + math.pi) % (2 * math.pi) - math.pi
    w = _OMEGA_WAKE if target[8] < 0.5 else _OMEGA_HOLD
    v += (w ** 2 * err - 2 * w * v) * dt
    x = x + v * dt
    _hold["x"], _hold["v"] = x, v
    return x


def _blend_head(app_head, held: np.ndarray) -> np.ndarray:
    b = min(1.0, max(0.0, float(held[8])))
    hold = _pose_mat(held[:6])
    if app_head is None or b >= 0.999:
        return hold
    a = np.asarray(app_head, dtype=np.float64)
    rel = hold[:3, :3] @ a[:3, :3].T
    angle = math.acos(max(-1.0, min(1.0, (np.trace(rel) - 1.0) / 2.0)))
    out = np.eye(4)
    if angle < 1e-6:
        out[:3, :3] = a[:3, :3]
    else:
        k = np.array([rel[2, 1] - rel[1, 2], rel[0, 2] - rel[2, 0], rel[1, 0] - rel[0, 1]])
        k /= np.linalg.norm(k) or 1.0
        kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        t = angle * b
        out[:3, :3] = (np.eye(3) + math.sin(t) * kx + (1 - math.cos(t)) * kx @ kx) @ a[:3, :3]
    out[:3, 3] = a[:3, 3] + (hold[:3, 3] - a[:3, 3]) * b
    return out


# Daemon-side face tracking (1.11+) at weight 1 discards the app's head target
# entirely, so a held pose needs it off. Remember what the app asked for and
# restore that when the mode goes back to free.
_track: dict = {"weight": None, "suspended": False}


def _start_head_tracking(self, weight: float = 1.0) -> None:
    _track["weight"] = weight
    if not _track["suspended"]:
        _orig_start_tracking(self, weight)


def _stop_head_tracking(self) -> None:
    _track["weight"] = None
    _orig_stop_tracking(self)


def _sync_tracking(robot, md: str) -> None:
    suspend = md != "free"
    if suspend == _track["suspended"]:
        return
    _track["suspended"] = suspend
    try:
        if suspend:
            _orig_stop_tracking(robot)
        elif _track["weight"] is not None:
            _orig_start_tracking(robot, _track["weight"])
    except Exception:  # noqa: BLE001 - never break the control loop
        _track["suspended"] = not suspend


def _wrap_command(orig):
    sig = inspect.signature(orig)

    def wrapped(self, *args, **kwargs):
        h = held_yaw()
        md = mode()
        p = held_pitch(md)
        _sync_tracking(self, md)
        with _hold_lock:
            target = _target(self, md, h)
            held = _step(target) if target is not None else None
        if abs(h) <= 1e-4 and abs(p) <= 1e-4 and held is None:
            return orig(self, *args, **kwargs)
        ba = sig.bind(self, *args, **kwargs)
        ba.apply_defaults()
        a = ba.arguments
        if abs(p) > 1e-4 and a.get("head") is not None:
            a["head"] = np.asarray(a["head"], dtype=np.float64) @ _ry(p)
        if held is not None:
            b = min(1.0, max(0.0, float(held[8])))
            a["head"] = _blend_head(a.get("head"), held)
            app_ant = a.get("antennas")
            ant = held[6:8] if app_ant is None else (
                np.asarray(app_ant, dtype=np.float64)[:2] * (1 - b) + held[6:8] * b)
            a["antennas"] = [float(ant[0]), float(ant[1])]
        if a.get("head") is not None:
            a["head"] = _rz(h) @ np.asarray(a["head"], dtype=np.float64)
        a["body_yaw"] = float(a.get("body_yaw") or 0.0) + h
        return orig(*ba.args, **ba.kwargs)

    wrapped.__wrapped__ = orig
    return wrapped


_orig_head_pose = ReachyMini.get_current_head_pose
_orig_joints = ReachyMini.get_current_joint_positions
_orig_start_tracking = ReachyMini.start_head_tracking
_orig_stop_tracking = ReachyMini.stop_head_tracking


def _get_current_head_pose(self):
    pose = _orig_head_pose(self)
    h = held_yaw()
    p = _pitch["cur"]
    if abs(h) <= 1e-4 and abs(p) <= 1e-4:
        return pose
    pose = np.asarray(pose, dtype=np.float64)
    if abs(h) > 1e-4:
        pose = _rz(-h) @ pose
    return pose @ _ry(-p) if abs(p) > 1e-4 else pose


def _get_current_joint_positions(self):
    head, antennas = _orig_joints(self)
    h = held_yaw()
    if abs(h) > 1e-4 and head is not None and len(head):
        head = [float(head[0]) - h, *head[1:]]
    return head, antennas


if not getattr(ReachyMini, "_peachy_patched", False):
    ReachyMini.set_target = _wrap_command(ReachyMini.set_target)
    ReachyMini.goto_target = _wrap_command(ReachyMini.goto_target)
    ReachyMini.get_current_head_pose = _get_current_head_pose
    ReachyMini.get_current_joint_positions = _get_current_joint_positions
    ReachyMini.start_head_tracking = _start_head_tracking
    ReachyMini.stop_head_tracking = _stop_head_tracking
    ReachyMini._peachy_patched = True

    _orig_breath_init = BreathingMove.__init__

    def _breath_init(self, *args, **kwargs):
        _orig_breath_init(self, *args, **kwargs)
        k = max(0.0, min(2.0, _number("breath_scale", 1.0)))
        self.breathing_z_amplitude *= k
        self.antenna_sway_amplitude *= k

    BreathingMove.__init__ = _breath_init

    def _idle_threshold(self) -> float:
        s = _number("idle_every_s", 180.0)
        return float("inf") if s <= 0 or mode() != "free" else max(20.0, s)

    ConversationHandler.IDLE_BEHAVIOR_THRESHOLD_S = property(_idle_threshold)

    def _get_muted(self) -> bool:
        return self.__dict__.get("_peachy_muted", False) or mode() != "free"

    def _set_muted(self, v: bool) -> None:
        self.__dict__["_peachy_muted"] = bool(v)

    LocalStream._mic_muted = property(_get_muted, _set_muted)

    _orig_greeting = HuggingFaceRealtimeHandler._send_startup_greeting_prompt

    async def _greeting(self) -> None:
        if mode() != "free":
            self._startup_greeting_sent = True
            return
        await _orig_greeting(self)

    HuggingFaceRealtimeHandler._send_startup_greeting_prompt = _greeting

    # The /rpc server runs on the ui-server thread's own event loop; the stock
    # conversation.say awaits the session websocket from there, which drops the
    # session. Run say on the loop that owns the connection instead.
    _orig_session = HuggingFaceRealtimeHandler._run_realtime_session
    _orig_say = HuggingFaceRealtimeHandler.say

    async def _session(self) -> None:
        self._peachy_loop = asyncio.get_running_loop()
        await _orig_session(self)

    async def _say(self, text: str) -> None:
        loop = getattr(self, "_peachy_loop", None)
        if loop is None or loop.is_closed() or loop is asyncio.get_running_loop():
            return await _orig_say(self, text)
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_orig_say(self, text), loop))

    HuggingFaceRealtimeHandler._run_realtime_session = _session
    HuggingFaceRealtimeHandler.say = _say

    # The room's air conditioning hums at 117/180 Hz; stock PP_MIN_NS 0.8 barely
    # suppresses it. 0.15 (XMOS default) lowered the voice-band floor ~13 dB with
    # no loss of speech level. Read when the app starts (it writes the chip then).
    from reachy_mini_conversation_app.audio import startup_config as _audio_cfg

    _orig_audio_apply = _audio_cfg.apply_audio_startup_config

    def _audio_apply(*args, **kwargs):
        over = {"PP_MIN_NS": (max(0.0, min(1.0, _number("mic_ns", 0.15))),),
                "PP_AGCMAXGAIN": (max(1.0, min(1000.0, _number("agc_max", 10.0))),)}
        _audio_cfg.AUDIO_STARTUP_CONFIG = tuple(
            (name, over.get(name, values)) for name, values in _audio_cfg.AUDIO_STARTUP_CONFIG)
        return _orig_audio_apply(*args, **kwargs)

    _audio_cfg.apply_audio_startup_config = _audio_apply
    import reachy_mini_conversation_app.console as _console_mod
    _console_mod.apply_audio_startup_config = _audio_apply
