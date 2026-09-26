"""Peachy patch for the official conversation app (installed by scripts/app-patch.sh).

Reads ~/.peachy/motion.json on the robot (re-read live, no restart needed):

  body_yaw      held body direction, radians. The app's whole world is rotated so
                its "forward" is this direction: every pose it sends is turned by
                it and every pose it reads back is turned back. Changes glide at
                _YAW_RATE so the 100 Hz pose stream never jumps.
  breath_scale  size of the idle breathing (head bob + antenna sway), 1.0 = stock.
  idle_every_s  seconds of silence before an idle action (dance / emotion / look);
                stock 180, 0 = never.

Loaded by one import line in reachy_mini_conversation_app/main.py.
"""

from __future__ import annotations

import inspect
import json
import math
import threading
import time
from pathlib import Path

import numpy as np
from reachy_mini import ReachyMini

from reachy_mini_conversation_app.conversation_handler import ConversationHandler
from reachy_mini_conversation_app.moves import BreathingMove

_FILE = Path.home() / ".peachy" / "motion.json"
_YAW_LIMIT = math.radians(160)
_YAW_RATE = math.radians(90)

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "mtime": None, "data": {}}
_yaw = {"cur": None, "at": 0.0}


def settings() -> dict:
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < 0.3:
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


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 1], m[1, 0], m[1, 1] = c, -s, s, c
    return m


def _wrap_command(orig):
    sig = inspect.signature(orig)

    def wrapped(self, *args, **kwargs):
        h = held_yaw()
        if abs(h) > 1e-4:
            ba = sig.bind(self, *args, **kwargs)
            ba.apply_defaults()
            a = ba.arguments
            if a.get("head") is not None:
                a["head"] = _rz(h) @ np.asarray(a["head"], dtype=np.float64)
            a["body_yaw"] = float(a.get("body_yaw") or 0.0) + h
            return orig(*ba.args, **ba.kwargs)
        return orig(self, *args, **kwargs)

    wrapped.__wrapped__ = orig
    return wrapped


_orig_head_pose = ReachyMini.get_current_head_pose
_orig_joints = ReachyMini.get_current_joint_positions


def _get_current_head_pose(self):
    pose = _orig_head_pose(self)
    h = held_yaw()
    return _rz(-h) @ np.asarray(pose, dtype=np.float64) if abs(h) > 1e-4 else pose


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
        return float("inf") if s <= 0 else max(20.0, s)

    ConversationHandler.IDLE_BEHAVIOR_THRESHOLD_S = property(_idle_threshold)
