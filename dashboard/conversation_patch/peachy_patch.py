"""Peachy patch for the official conversation app (installed by scripts/app-patch.sh).

Reads ~/.peachy/motion.json on the robot (re-read live, no restart needed):

  body_yaw      held body direction, radians. The app's whole world is rotated so
                its "forward" is this direction: every pose it sends is turned by
                it and every pose it reads back is turned back. Changes glide at
                _YAW_RATE so the 100 Hz pose stream never jumps.
  head_pitch    extra head pitch, radians (positive looks down), added in the
                head's own frame on top of whatever the app does, while "free".
                Glides at _PITCH_RATE; back to 0 while tucked/lifted.
  breath_scale  size of the idle breathing head bob, 1.0 = stock. The stock
                antenna sway is always off.
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
  facts         list of fixed facts (where Peachy lives, ...) put before the
                session instructions; the app's forget tool can't remove them.
                A change reaches the live session within 2 s.

The instructions also get _TOOL_NOTE, so Peachy says a word before a lookup
instead of going quiet until the tool returns.

Loaded by one import line in reachy_mini_conversation_app/main.py.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np
from reachy_mini import ReachyMini
from scipy.spatial.transform import Rotation

import reachy_mini_conversation_app.huggingface_realtime as _hr
import reachy_mini_conversation_app.prompts as _prompts
from reachy_mini_conversation_app.console import LocalStream
from reachy_mini_conversation_app.conversation_handler import ConversationHandler
from reachy_mini_conversation_app.huggingface_realtime import HuggingFaceRealtimeHandler
from reachy_mini_conversation_app.moves import BreathingMove, MovementManager

_FILE = Path.home() / ".peachy" / "motion.json"
_log = logging.getLogger(__name__)
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


_TOOL_NOTE = (
    "When you call a tool that takes a moment (web search, weather, time, the camera, "
    "anything that looks something up), first say one short natural line in the same "
    "reply, like \"Let me check.\" or \"One sec, I'll look that up.\", then call the tool "
    "right away. Never end your turn with only that line."
)


def _facts() -> list[str]:
    f = settings().get("facts")
    if not isinstance(f, list):
        return []
    return [s for s in (str(x).strip()[:280] for x in f) if s][:20]


async def _watch_facts(handler) -> None:
    """Push changed facts into the live session (the app only reads instructions
    when a session starts or the personality changes)."""
    seen = _facts()
    while True:
        await asyncio.sleep(2.0)
        now = _facts()
        if now == seen or handler.connection is None:
            continue
        seen = now
        try:
            await handler.connection.session.update(session=_hr.RealtimeSessionCreateRequestParam(
                type="realtime", instructions=_hr.get_session_instructions(handler.instance_path)))
            _log.info("peachy: fixed facts updated (%d)", len(now))
        except Exception as e:  # noqa: BLE001
            _log.warning("peachy: couldn't update the session's facts: %s", e)


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


# Face tracking, alt-azimuth, on for every conversation (the app's head_tracking
# tool only turns it off and on again). The app's tool would turn on the
# daemon's tracker, which at weight 1 discards the app's head target (held body
# yaw included, so the neck twists when the body turns), rolls with the face,
# and runs at the lowest priority: 3-4 detections a second on the busy CM4.
# It stays off. The patch finds faces itself instead, with the same YuNet model
# at normal priority on the app's own camera frames (~38 ms each, run at
# _FACE_HZ), and aims: yaw about the vertical, pitch in the head frame, and the
# body takes over when the head is turned far. Dozing pauses it and each wake
# starts it again; the console turning the body stops it until the next wake.
#
# Who to look at: while the user talks (the backend's VAD) and Peachy is silent,
# the mic array's direction of arrival picks the face nearest the voice, and
# the pick sticks after they stop. The array is a line of four mics in the head:
# 0 = left, pi/2 = ahead, pi = right, and front and back look alike. Its speech
# flag also fires on Peachy's own motors (from 0 or pi), hence the VAD gate and
# the agreement test. A voice with no face near it turns the head (and body)
# that way, then only a face near it is taken for _SEEK_S.
_FACE_HZ = 10.0
_FACE_W = 320
_FOCAL_N = 1.27            # focal / half image width (cal-heading fit: 405 px at 640)
_ASPECT = 16 / 9
_AIM_LAG = 0.15            # s, camera + detection; aim from where the head was then
_AIM_GAIN = 0.7
_AIM_DEAD = math.radians(2)
_AIM_HEAD_MAX = math.radians(40)    # head yaw vs body
_AIM_PITCH_MAX = math.radians(25)
_AIM_LOST_S = 4.0
_AIM_SMOOTH = 6.0                   # 1/s, head easing toward the aim
_AIM_BODY_AT = math.radians(18)     # head this far off the body for _AIM_BODY_S...
_AIM_BODY_S = 0.8
_AIM_BODY_STEP = math.radians(8)    # ...moves the body up to this much toward it
_DOA_URL = "http://127.0.0.1:8000/api/state/doa"
_VOICE_WINDOW_S = 0.8               # speech directions this recent...
_VOICE_MIN_N = 4                    # ...at least this many (~0.4 s at _FACE_HZ)...
_VOICE_SPREAD = math.radians(20)    # ...agreeing this closely make a voice bearing
_VOICE_MATCH = math.radians(20)     # a face this close to it is the speaker
_VOICE_STALE_S = 30.0               # a speech_started with no stop after this is ignored
_SEEK_S = 2.5
_SEEK_EVERY_S = 3.0
_track: dict = {"want": True, "suspended": False, "body_yaw": None}
# az / body: head azimuth and body direction, offsets from the held yaw.
_aim: dict = {"on": False, "robot": None, "ts": None, "seen": 0.0, "at": 0.0, "off_since": 0.0,
              "az": 0.0, "taz": 0.0, "pitch": 0.0, "tpitch": 0.0, "body": 0.0, "tbody": 0.0,
              "seek_az": 0.0, "seek_until": 0.0, "seek_at": 0.0}
_aim_hist: deque = deque(maxlen=120)   # (t, az, pitch), last ~1.2 s
_voice: dict = {"user": False, "user_since": 0.0, "user_at": 0.0, "peachy": False}
_doa: deque = deque(maxlen=30)         # (t, bearing): speech-flagged, head frame, +left


def _clip(v: float, lim: float) -> float:
    return max(-lim, min(lim, v))


def _bearing(x: float) -> float:
    """Image x in [-1, 1] (+right) → bearing from the camera axis (+left)."""
    return -math.atan(x / _FOCAL_N)


def _read_doa(now: float) -> None:
    try:
        with urllib.request.urlopen(_DOA_URL, timeout=0.3) as r:
            d = json.loads(r.read() or b"null")
    except (OSError, ValueError):
        return
    if d and d.get("speech_detected") and d.get("angle") is not None:
        _doa.append((now, math.pi / 2 - float(d["angle"])))


def _voice_bearing(now: float) -> tuple[float, float] | None:
    """(time, bearing) of the user's voice, head frame, or None unless the backend
    hears the user, Peachy is silent and the recent speech directions agree."""
    v = _voice
    talking = v["user"] and now - v["user_since"] < _VOICE_STALE_S
    if v["peachy"] or not (talking or now - v["user_at"] < 0.3):
        return None
    s = [(t, a) for t, a in _doa if now - t <= _VOICE_WINDOW_S]
    if len(s) < _VOICE_MIN_N:
        return None
    a = sorted(b for _, b in s)
    if a[-1] - a[0] > _VOICE_SPREAD:
        return None
    return s[len(s) // 2][0], a[len(a) // 2]


def _pick_face(faces, w: int, h: int, prev: tuple[float, float] | None,
               voice: float | None = None, only_voice: bool = False) -> tuple[tuple[float, float] | None, bool]:
    """(nose of the face to follow in [-1, 1] of the image, picked by the voice):
    the one nearest the voice bearing if it is close, else (unless only_voice)
    the one near the last pick if it is still there, else the largest."""
    if not faces:
        return None, False
    nose = [(f.nose[0] / max(w - 1, 1) * 2 - 1, f.nose[1] / max(h - 1, 1) * 2 - 1) for f in faces]
    idx = range(len(faces))
    if voice is not None:
        i = min(idx, key=lambda k: abs(_bearing(nose[k][0]) - voice))
        if abs(_bearing(nose[i][0]) - voice) <= _VOICE_MATCH:
            return nose[i], True
    if only_voice:
        return None, False
    if prev is not None:
        i = min(idx, key=lambda k: (nose[k][0] - prev[0]) ** 2 + (nose[k][1] - prev[1]) ** 2)
        if abs(nose[i][0] - prev[0]) < 0.36:
            return nose[i], False
    return nose[max(idx, key=lambda k: faces[k].bbox[2] * faces[k].bbox[3])], False


def _face_loop() -> None:
    det = None
    prev = None
    while True:
        robot = _aim["robot"]
        if not _aim["on"] or robot is None:
            prev = None
            time.sleep(0.2)
            continue
        t0 = time.monotonic()
        try:
            if det is None:
                import cv2
                from reachy_mini.vision.face_detector import FaceDetector
                det = FaceDetector()
            frame = robot.media.get_frame()
            _read_doa(time.monotonic())
            if frame is not None:
                h, w = frame.shape[:2]
                small = cv2.resize(frame, (_FACE_W, round(_FACE_W * h / w / 2) * 2),
                                   interpolation=cv2.INTER_AREA)
                faces = det.detect(small)
                now = time.monotonic()
                vb = _voice_bearing(now)
                with _lock:
                    az_now = _aim_then(t0)[0]
                    seeking = now < _aim["seek_until"]
                    voice = _aim["seek_az"] - az_now if seeking else (
                        None if vb is None else vb[1] + _aim_then(vb[0])[0] - az_now)
                face, heard = _pick_face(faces, small.shape[1], small.shape[0], prev, voice, seeking)
                if face is not None:
                    prev = face
                    if heard and seeking:
                        with _lock:
                            _aim["seek_until"] = 0.0
                    _aim_see({"detected": True, "x": face[0], "y": face[1], "ts": t0})
                elif not seeking:
                    prev = None
                if voice is not None and not heard and not seeking and now - _aim["seek_at"] > _SEEK_EVERY_S:
                    _aim_seek(az_now + voice)
        except Exception:  # noqa: BLE001 - a camera hiccup must not end tracking for good
            time.sleep(1.0)
        time.sleep(max(0.0, 1.0 / _FACE_HZ - (time.monotonic() - t0)))


def _aim_seek(az: float) -> None:
    """Turn toward a voice with no face near it: head azimuth az (offset from the
    held yaw); the body turns under it."""
    with _lock:
        now = time.monotonic()
        _aim["seek_az"], _aim["seek_until"], _aim["seek_at"] = az, now + _SEEK_S, now
        _aim["seen"] = now


def _aim_then(t: float) -> tuple[float, float]:
    """Head azimuth and pitch offsets at time t (monotonic). Caller holds _lock."""
    for at, az, pitch in reversed(_aim_hist):
        if at <= t:
            return az, pitch
    return _aim["az"], _aim["pitch"]


def _aim_see(f: dict) -> None:
    """One face: x, y in [-1, 1] of the image, +x right, +y down."""
    if not _aim["on"] or not f.get("detected") or f.get("x") is None or f.get("ts") == _aim["ts"]:
        return
    ex = -math.atan(float(f["x"]) / _FOCAL_N)
    ey = math.atan(float(f.get("y") or 0.0) / (_FOCAL_N * _ASPECT))
    with _lock:
        _aim["ts"], _aim["seen"] = f.get("ts"), time.monotonic()
        az, pitch = _aim_then(_aim["seen"] - _AIM_LAG)
        if abs(ex) > _AIM_DEAD:
            _aim["taz"] = _aim["body"] + _clip(az + _AIM_GAIN * ex - _aim["body"], _AIM_HEAD_MAX)
        if abs(ey) > _AIM_DEAD:
            _aim["tpitch"] = _clip(pitch + _AIM_GAIN * ey, _AIM_PITCH_MAX)


def aim(md: str, h: float) -> tuple[float, float, float]:
    """(head azimuth, head pitch, body) offsets for this tick, eased."""
    now = time.monotonic()
    with _lock:
        dt = min(0.05, max(0.0, now - _aim["at"]))
        _aim["at"] = now
        if md != "free" or not _aim["on"] or now - _aim["seen"] > _AIM_LOST_S:
            _aim["taz"] = _aim["tpitch"] = _aim["tbody"] = 0.0
            _aim["off_since"] = _aim["seek_until"] = 0.0
        elif now < _aim["seek_until"]:
            _aim["tbody"] = _aim["seek_az"]
            _aim["taz"] = _aim["body"] + _clip(_aim["seek_az"] - _aim["body"], _AIM_HEAD_MAX)
            _aim["off_since"] = 0.0
        else:
            rel = _aim["taz"] - _aim["body"]
            if abs(rel) > _AIM_BODY_AT:
                _aim["off_since"] = _aim["off_since"] or now
                if now - _aim["off_since"] > _AIM_BODY_S:
                    _aim["tbody"] = _aim["body"] + _clip(rel * 0.7, _AIM_BODY_STEP)
                    _aim["off_since"] = now
            else:
                _aim["off_since"] = 0.0
        _aim["tbody"] = _clip(h + _aim["tbody"], _YAW_LIMIT) - h
        k = min(1.0, dt * _AIM_SMOOTH)
        _aim["az"] += (_aim["taz"] - _aim["az"]) * k
        _aim["pitch"] += (_aim["tpitch"] - _aim["pitch"]) * k
        step = _YAW_RATE * dt
        _aim["body"] += _clip(_aim["tbody"] - _aim["body"], step)
        _aim_hist.append((now, _aim["az"], _aim["pitch"]))
        return _aim["az"], _aim["pitch"], _aim["body"]


def _start_head_tracking(self, weight: float = 1.0) -> None:
    _track["want"] = True
    _aim["robot"] = self
    _aim["on"] = not _track["suspended"]


def _stop_head_tracking(self) -> None:
    _track["want"] = False
    _aim["on"] = False
    _orig_stop_tracking(self)


def _sync_tracking(robot, md: str) -> None:
    _aim["robot"] = robot
    body = settings().get("body_yaw")
    if body != _track["body_yaw"]:
        if _track["body_yaw"] is not None and not _track["suspended"]:
            _track["want"] = False
        _track["body_yaw"] = body
    suspend = md != "free"
    if suspend != _track["suspended"]:
        _track["suspended"] = suspend
        if not suspend:
            _track["want"] = True
    _aim["on"] = _track["want"] and not suspend


def _wrap_command(orig):
    sig = inspect.signature(orig)

    def wrapped(self, *args, **kwargs):
        h = held_yaw()
        md = mode()
        _sync_tracking(self, md)
        az, ap, ab = aim(md, h)
        p = held_pitch(md) + ap
        with _hold_lock:
            target = _target(self, md, h)
            held = _step(target) if target is not None else None
        if max(abs(h), abs(p), abs(az), abs(ab)) <= 1e-4 and held is None:
            return orig(self, *args, **kwargs)
        ba = sig.bind(self, *args, **kwargs)
        ba.apply_defaults()
        a = ba.arguments
        if a.get("head") is not None and max(abs(p), abs(az - ab)) > 1e-4:
            a["head"] = _rz(az - ab) @ np.asarray(a["head"], dtype=np.float64) @ _ry(p)
        if held is not None:
            b = min(1.0, max(0.0, float(held[8])))
            a["head"] = _blend_head(a.get("head"), held)
            app_ant = a.get("antennas")
            ant = held[6:8] if app_ant is None else (
                np.asarray(app_ant, dtype=np.float64)[:2] * (1 - b) + held[6:8] * b)
            a["antennas"] = [float(ant[0]), float(ant[1])]
        if a.get("head") is not None:
            a["head"] = _rz(h + ab) @ np.asarray(a["head"], dtype=np.float64)
        a["body_yaw"] = float(a.get("body_yaw") or 0.0) + h + ab
        return orig(*ba.args, **ba.kwargs)

    wrapped.__wrapped__ = orig
    return wrapped


_orig_head_pose = ReachyMini.get_current_head_pose
_orig_joints = ReachyMini.get_current_joint_positions
_orig_stop_tracking = ReachyMini.stop_head_tracking


def _get_current_head_pose(self):
    pose = _orig_head_pose(self)
    h = held_yaw()
    p = _pitch["cur"] + _aim["pitch"]
    yaw = h + _aim["az"]
    if abs(yaw) <= 1e-4 and abs(p) <= 1e-4:
        return pose
    pose = _rz(-yaw) @ np.asarray(pose, dtype=np.float64)
    return pose @ _ry(-p) if abs(p) > 1e-4 else pose


def _get_current_joint_positions(self):
    head, antennas = _orig_joints(self)
    yaw = held_yaw() + _aim["body"]
    if abs(yaw) > 1e-4 and head is not None and len(head):
        head = [float(head[0]) - yaw, *head[1:]]
    return head, antennas


if not getattr(ReachyMini, "_peachy_patched", False):
    ReachyMini.set_target = _wrap_command(ReachyMini.set_target)
    ReachyMini.goto_target = _wrap_command(ReachyMini.goto_target)
    ReachyMini.get_current_head_pose = _get_current_head_pose
    ReachyMini.get_current_joint_positions = _get_current_joint_positions
    ReachyMini.start_head_tracking = _start_head_tracking
    ReachyMini.stop_head_tracking = _stop_head_tracking
    ReachyMini._peachy_patched = True
    threading.Thread(target=_face_loop, name="peachy-faces", daemon=True).start()

    _orig_breath_init = BreathingMove.__init__

    def _breath_init(self, *args, **kwargs):
        _orig_breath_init(self, *args, **kwargs)
        k = max(0.0, min(2.0, _number("breath_scale", 1.0)))
        self.breathing_z_amplitude *= k
        self.antenna_sway_amplitude = 0.0   # the antenna motors' whine gets into the mic

    BreathingMove.__init__ = _breath_init

    def _idle_threshold(self) -> float:
        s = _number("idle_every_s", 180.0)
        return float("inf") if s <= 0 or mode() != "free" else max(20.0, s)

    ConversationHandler.IDLE_BEHAVIOR_THRESHOLD_S = property(_idle_threshold)

    _orig_mark_activity = ConversationHandler._mark_activity

    def _mark_activity(self, reason: str) -> None:
        now = time.monotonic()
        if reason == "user_speech_started":
            _voice.update(user=True, user_since=now)
        elif reason == "user_speech_stopped":
            _voice.update(user=False, user_at=now)
        _orig_mark_activity(self, reason)

    ConversationHandler._mark_activity = _mark_activity

    _orig_set_speaking = MovementManager.set_speaking

    def _set_speaking(self, speaking: bool) -> None:
        _voice["peachy"] = bool(speaking)
        _orig_set_speaking(self, speaking)

    MovementManager.set_speaking = _set_speaking

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
        watch = asyncio.ensure_future(_watch_facts(self))
        try:
            await _orig_session(self)
        finally:
            watch.cancel()

    async def _say(self, text: str) -> None:
        loop = getattr(self, "_peachy_loop", None)
        if loop is None or loop.is_closed() or loop is asyncio.get_running_loop():
            return await _orig_say(self, text)
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_orig_say(self, text), loop))

    HuggingFaceRealtimeHandler._run_realtime_session = _session
    HuggingFaceRealtimeHandler.say = _say

    _orig_instructions = _prompts.get_session_instructions

    def _instructions(instance_path=None) -> str:
        facts = _facts()
        head = ("Fixed facts (always true; don't ask the user about them):\n"
                + "\n".join(f"- {f}" for f in facts)) if facts else ""
        return "\n\n".join(p for p in (head, _orig_instructions(instance_path), _TOOL_NOTE) if p)

    _prompts.get_session_instructions = _instructions
    _hr.get_session_instructions = _instructions

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
