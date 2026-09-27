#!/usr/bin/env python3
"""Peachy's head follows yours: AirPods head tracking -> the robot's head.

Reads AirPods (Pro / Max / 3rd gen) orientation on this Mac through
airpods/PeachyHead.app (CoreMotion, macOS 14+) and streams it to the daemon
with set_target: yaw, pitch and roll, same direction as you (you turn to your
left, Peachy turns to its left). This is one of the few features that rolls
the head; it does not use head_pose.altaz. Past 55 deg of head yaw the body
turntable takes the rest.

  python scripts/ctl-airpods.py --probe     # AirPods only: check the axes first
  python scripts/ctl-airpods.py --dry-run   # full pipeline, nothing sent
  python scripts/ctl-airpods.py             # follow
  python scripts/ctl-airpods.py --stop-app  # stop a running app first

Pitch and roll are absolute (CoreMotion measures them against gravity): hold
your head level and Peachy's is level, whatever pose you start in. Yaw has no
reference (AirPods have no compass), so the direction you face at the start is
Peachy's straight ahead; it drifts slowly, so recenter now and then.

The buds sit a few degrees off your head's axes; press l once (3 s to look up
from the screen, then hold your head level, looking at the horizon, for 1 s)
and the offset is saved to .run/airpods_level.json for every later session.
Looking down at a screen or the desk makes Peachy look down too: that is
your real head angle.

Keys: c recenter yaw (face where Peachy should look straight ahead), l level,
space pause/resume, q quit. SIGUSR1 recenters, SIGUSR2 levels (the console's
Recenter and Set level buttons).

Taking the AirPods out holds the pose, then after 2 s Peachy glides home; put
them back and it recenters. Quitting glides home too. Follow and room watch on
the robot pause while this runs (peachy-senses /busy).

Env: REACHY_HOST (auto via hostfind), REACHY_PORT (8000)
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from head_pose import load_state, resolve_home  # noqa: E402
from hostfind import resolve_host  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "airpods"
_BIN = _REPO / ".run" / "airpods" / "PeachyHead.app" / "Contents" / "MacOS" / "PeachyHead"
_SENSES_TOKEN = _REPO / ".run" / "senses_token"
_LEVEL = _REPO / ".run" / "airpods_level.json"
_SENSES_PORT = 8767

D = math.radians
PITCH_MAX = D(35)      # daemon clamps pitch/roll at 40
ROLL_MAX = D(35)
HEAD_SOFT = D(55)      # head yaw from the body before the body starts to follow
HEAD_HARD = D(63)      # daemon: head-vs-body yaw <= 65
BODY_MAX = D(160)
BODY_RATE = D(120)     # turntable, rad/s
SEND_HZ = 60.0
HOLD_S = 2.0           # AirPods gone this long -> glide home
STALE_S = 0.5          # no samples this long counts as gone
SETTLE_S = 0.3         # a fresh stream opens with ~100 ms of a cached pose, then jumps
BLEND_S = 0.6          # glide into the live pose after recenter / resume / reconnect
BUSY_EVERY_S = 5.0
LEVEL_WAIT_S = 3.0     # level calibration: time to look up from the screen
LEVEL_AVG_S = 1.0

# AirPods frame (x right ear, y nose, z up) -> robot head frame (x forward, y left, z up).
M = np.array([[0.0, 1.0, 0.0],
              [-1.0, 0.0, 0.0],
              [0.0, 0.0, 1.0]])


# ------------------------------------------------------------------ math

def quat_mat(q) -> np.ndarray:
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def euler_xyz(r: np.ndarray) -> tuple[float, float, float]:
    """(yaw, pitch, roll) of R = Rz(yaw)·Ry(pitch)·Rx(roll), the daemon's convention."""
    pitch = math.asin(max(-1.0, min(1.0, -r[2, 0])))
    roll = math.atan2(r[2, 1], r[2, 2])
    yaw = math.atan2(r[1, 0], r[0, 0])
    return yaw, pitch, roll


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def clip(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class OneEuro:
    """One Euro filter (Casiez et al.): steady when still, little lag when fast."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float = 1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = None
        self.t = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self) -> None:
        self.x = self.dx = None

    def __call__(self, x: float, t: float) -> float:
        if self.x is None:
            self.x, self.dx, self.t = x, 0.0, t
            return x
        dt = max(1e-3, t - self.t)
        self.t = t
        d = (x - self.x) / dt
        self.dx += self._alpha(self.d_cutoff, dt) * (d - self.dx)
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        self.x += self._alpha(cutoff, dt) * (x - self.x)
        return self.x


# ------------------------------------------------------------------ AirPods reader

def ensure_built() -> Path:
    newest = max(p.stat().st_mtime for p in _SRC.iterdir() if p.is_file())
    if not _BIN.exists() or _BIN.stat().st_mtime < newest:
        print("building PeachyHead.app ...", file=sys.stderr)
        subprocess.run([str(_SRC / "build.sh")], check=True, stdout=subprocess.DEVNULL)
    return _BIN


class AirPods:
    """Runs PeachyHead and keeps only the newest sample."""

    def __init__(self):
        self.proc = subprocess.Popen([str(ensure_built())], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.lock = threading.Lock()
        self.fresh = threading.Event()
        self.sample: dict | None = None
        self.seq = 0
        self.connected = False
        self.gone_at = time.monotonic()
        self.flow_since = 0.0
        self.fatal: str | None = None
        self.hz = 0.0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        count, since = 0, time.monotonic()
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            now = time.monotonic()
            with self.lock:
                ev = msg.get("ev")
                if ev == "connected":
                    self.connected = True
                elif ev == "disconnected":
                    self.connected, self.gone_at = False, now
                elif ev in ("denied", "unavailable"):
                    self.fatal = ev
                elif "q" in msg:
                    if not self.connected or self.sample is None or now - self.sample["rx"] > STALE_S:
                        self.flow_since = now
                    self.connected = True
                    msg["rx"] = now
                    self.sample = msg
                    self.seq += 1
                    count += 1
                    if now - since >= 1.0:
                        self.hz, count, since = count / (now - since), 0, now
            self.fresh.set()
        with self.lock:
            code = self.proc.wait()
            self.fatal = self.fatal or {2: "denied", 3: "unavailable"}.get(code, f"reader exited ({code})")
        self.fresh.set()

    def latest(self) -> tuple[int, dict | None]:
        with self.lock:
            return self.seq, self.sample

    def live(self) -> bool:
        with self.lock:
            s = self.sample
            now = time.monotonic()
            ok = (self.connected and s is not None and now - s["rx"] < STALE_S
                  and now - self.flow_since >= SETTLE_S)
            if not ok and self.gone_at < (s["rx"] if s else 0.0):
                self.gone_at = s["rx"]
            return ok

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def explain_fatal(why: str) -> str:
    if why == "denied":
        return ("Motion access is off for PeachyHead. System Settings > Privacy & Security > "
                "Motion & Fitness: turn PeachyHead on, then run this again.")
    if why == "unavailable":
        return "This Mac reports no headphone motion support (needs macOS 14+)."
    return f"AirPods reader stopped: {why}"


# ------------------------------------------------------------------ head mapping

def rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def level_matrix(level: dict) -> np.ndarray:
    return (rot_y(level["pitch"]) @ rot_x(level["roll"])).T


def load_level() -> dict:
    try:
        d = json.loads(_LEVEL.read_text())
        return {"pitch": float(d["pitch"]), "roll": float(d["roll"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"pitch": 0.0, "roll": 0.0}


def save_level(level: dict) -> None:
    _LEVEL.parent.mkdir(parents=True, exist_ok=True)
    _LEVEL.write_text(json.dumps({**{k: round(v, 5) for k, v in level.items()},
                                  "at": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")


class Leveler:
    """Level calibration: a countdown to look up from the screen, then the
    buds' average tilt over LEVEL_AVG_S."""

    def __init__(self):
        self.until = 0.0
        self.samples: list[tuple[float, float]] = []

    def start(self, now: float) -> None:
        self.until, self.samples = now + LEVEL_WAIT_S, []

    def active(self) -> bool:
        return self.until > 0

    def feed(self, mapper: "Mapper", q, now: float) -> str | None:
        """Returns a status word while active; saves when done."""
        if now < self.until:
            return f"level in {math.ceil(self.until - now)}"
        self.samples.append(mapper.buds_tilt(q))
        if now < self.until + LEVEL_AVG_S:
            return "leveling"
        n = len(self.samples)
        mapper.set_level(sum(p for p, _ in self.samples) / n, sum(r for _, r in self.samples) / n)
        self.until = 0.0
        return None

class Mapper:
    """AirPods quaternion -> (yaw, pitch, roll) of your head, robot convention.

    CoreMotion's headphone attitude has z vertical: pitch and roll are
    absolute (against gravity), so a level head means a level Peachy wherever
    you start. Yaw has no reference (no compass) and starts at 0 with the
    stream, so it is taken relative to the recentered direction.

    How the buds sit in your ears tilts their frame against your head's by a
    few degrees; the level calibration measures that as a fixed rotation C
    (buds -> head, taken with the head level) and applies it on the right."""

    def __init__(self, flip: set[str], smooth: float, lead_s: float, relative_tilt: bool = False):
        self.ref: np.ndarray | None = None
        self.yaw_ref = 0.0
        self.relative_tilt = relative_tilt
        self.level = load_level()
        self.C = level_matrix(self.level)
        self.sign = {k: (-1.0 if k in flip else 1.0) for k in ("yaw", "pitch", "roll")}
        self.lead_s = lead_s
        self.filters = None
        if smooth > 0:
            self.filters = {k: OneEuro(min_cutoff=1.5 / smooth, beta=0.6) for k in self.sign}
        self.yaw_unwrapped = 0.0

    def recenter(self, q) -> None:
        self.ref = quat_mat(q)
        self.yaw_ref = euler_xyz(M @ self.ref @ M.T @ self.C)[0]
        self.yaw_unwrapped = 0.0
        if self.filters:
            for f in self.filters.values():
                f.reset()

    def set_level(self, pitch: float, roll: float) -> None:
        """The buds' pitch/roll (uncorrected) while the head was level."""
        self.level = {"pitch": pitch, "roll": roll}
        self.C = level_matrix(self.level)
        save_level(self.level)
        if self.ref is not None:
            self.yaw_ref = euler_xyz(M @ self.ref @ M.T @ self.C)[0]

    def buds_tilt(self, q) -> tuple[float, float]:
        _, pitch, roll = euler_xyz(M @ quat_mat(q) @ M.T)
        return pitch, roll

    def raw(self, q) -> tuple[float, float, float]:
        if self.relative_tilt:
            return euler_xyz(M @ (self.ref.T @ quat_mat(q)) @ M.T)
        yaw, pitch, roll = euler_xyz(M @ quat_mat(q) @ M.T @ self.C)
        return wrap(yaw - self.yaw_ref), pitch, roll

    def __call__(self, q, t: float) -> dict:
        if self.ref is None:
            self.recenter(q)
        yaw, pitch, roll = self.raw(q)
        self.yaw_unwrapped += wrap(yaw - wrap(self.yaw_unwrapped))
        out = {"yaw": self.yaw_unwrapped, "pitch": pitch, "roll": roll}
        for k in out:
            v = out[k] * self.sign[k]
            if self.filters:
                f = self.filters[k]
                v = f(v, t)
                if self.lead_s:
                    v += f.dx * self.lead_s
            out[k] = v
        return out


# ------------------------------------------------------------------ robot

def http_json(host: str, port: int, path: str, method: str = "GET",
              body: dict | None = None, timeout: float = 5.0, headers: dict | None = None):
    data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
    req = urllib.request.Request(f"http://{host}:{port}{path}", method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


class Streamer:
    """Sender thread: always the newest target, never a queue of old ones.

    Prefers the daemon's /api/move/ws/set_target websocket (no reply per
    target, so Wi-Fi round trips of 30-800 ms don't hold anything up); falls
    back to keep-alive POST /api/move/set_target and retries the socket."""

    WS_RETRY_S = 2.0

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending: str | None = None
        self.ws = None
        self.ws_failed_at = 0.0
        self.conn: http.client.HTTPConnection | None = None
        self.via = "-"
        self.ms = 0.0
        self.sent = 0
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, head: dict, body_yaw: float) -> None:
        with self.lock:
            self.pending = json.dumps({"target_head_pose": head, "target_body_yaw": body_yaw})
        self.wake.set()

    def _run(self) -> None:
        while self.running:
            self.wake.wait(0.5)
            self.wake.clear()
            with self.lock:
                payload, self.pending = self.pending, None
            if payload is None:
                continue
            t0 = time.monotonic()
            if self._send_ws(payload) or self._send_http(payload):
                self.sent += 1
                self.ms += ((time.monotonic() - t0) * 1000 - self.ms) * 0.1

    def _send_ws(self, payload: str) -> bool:
        if self.ws is None:
            if time.monotonic() - self.ws_failed_at < self.WS_RETRY_S:
                return False
            try:
                from websockets.sync.client import connect
                self.ws = connect(f"ws://{self.host}:{self.port}/api/move/ws/set_target",
                                  open_timeout=2, close_timeout=1)
            except Exception:
                self.ws, self.ws_failed_at = None, time.monotonic()
                return False
        try:
            self.ws.send(payload)
            self.via = "ws"
            return True
        except Exception:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws, self.ws_failed_at = None, time.monotonic()
            return False

    def _send_http(self, payload: str) -> bool:
        for _ in range(2):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port, timeout=1.0)
                self.conn.request("POST", "/api/move/set_target", payload,
                                  {"Content-Type": "application/json"})
                r = self.conn.getresponse()
                r.read()
                self.via = "http"
                return 200 <= r.status < 300
            except (OSError, http.client.HTTPException):
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
        return False

    def close(self) -> None:
        self.running = False
        self.wake.set()
        self.thread.join(timeout=2)
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.conn is not None:
            self.conn.close()


class Senses:
    """Tell peachy-senses the head is taken, so Follow and room watch pause."""

    def __init__(self, host: str):
        self.host = host
        try:
            self.token = _SENSES_TOKEN.read_text().strip()
        except OSError:
            self.token = ""
        self.stop = threading.Event()

    def _busy(self, on: bool, ttl: float = 20.0) -> None:
        if not self.token:
            return
        try:
            http_json(self.host, _SENSES_PORT, "/busy", "POST", {"on": on, "ttl": ttl},
                      timeout=2.0, headers={"X-Peachy-Senses": self.token})
        except (urllib.error.URLError, OSError, ValueError):
            pass

    def start(self) -> None:
        def beat():
            while not self.stop.is_set():
                self._busy(True)
                self.stop.wait(BUSY_EVERY_S)
        threading.Thread(target=beat, daemon=True).start()

    def release(self) -> None:
        self.stop.set()
        self._busy(False)


def preflight(host: str, port: int, stop_app: bool, keep_app: str | None = None) -> dict:
    """Refuse to fight an app for the head; enable motors. Returns the robot state.
    *keep_app* holds the robot for us (the avatar) and stays running."""
    try:
        st = http_json(host, port, "/api/apps/current-app-status") or {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SystemExit(f"Cannot reach the daemon at {host}:{port} ({e}). Try ./scripts/net-connect.sh")
    name = (st.get("info") or {}).get("name")
    if st.get("state") in ("starting", "running", "stopping") and not (keep_app and name == keep_app):
        if st.get("state") != "stopping":
            if not stop_app:
                raise SystemExit(f"{name} is running and moves the head itself. "
                                 "Stop it first, or pass --stop-app.")
            print(f"stopping {name} ...", file=sys.stderr)
            try:
                http_json(host, port, "/api/apps/stop-current-app", "POST", timeout=30)
            except urllib.error.HTTPError as e:
                if e.code != 400:  # 400: "No app is currently running" (already stopping)
                    raise
        for _ in range(60):
            st = http_json(host, port, "/api/apps/current-app-status") or {}
            if st.get("state") not in ("starting", "running", "stopping"):
                break
            time.sleep(0.5)
        else:
            print(f"note: the daemon still reports {name} as {st.get('state')}; going on "
                  "(its process may already be gone).", file=sys.stderr)
        time.sleep(1.0)
    for move in http_json(host, port, "/api/move/running") or []:
        uuid = move.get("uuid") if isinstance(move, dict) else move
        try:
            http_json(host, port, "/api/move/stop", "POST", {"uuid": uuid}, timeout=6)
        except urllib.error.HTTPError:
            pass
    http_json(host, port, "/api/motors/set_mode/enabled", "POST", timeout=10)
    return http_json(host, port, "/api/state/full")


def goto(host: str, port: int, head: dict, body_yaw: float, dur: float) -> None:
    try:
        http_json(host, port, "/api/move/goto", "POST",
                  {"head_pose": head, "body_yaw": body_yaw, "duration": dur,
                   "interpolation": "minjerk"}, timeout=10)
    except (urllib.error.URLError, OSError, ValueError):
        pass


def wait_moves(host: str, port: int, max_s: float = 4.0) -> None:
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            if not http_json(host, port, "/api/move/running", timeout=3):
                return
        except (urllib.error.URLError, OSError, ValueError):
            return
        time.sleep(0.1)


# ------------------------------------------------------------------ keys

class Keys:
    def __init__(self):
        self.tty = sys.stdin.isatty()
        self.old = None
        if self.tty:
            import termios
            import tty
            self.old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

    def poll(self) -> str | None:
        if not self.tty:
            return None
        r, _, _ = select.select([sys.stdin], [], [], 0)
        return sys.stdin.read(1) if r else None

    def restore(self) -> None:
        if self.old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old)


# ------------------------------------------------------------------ modes

def fmt(v: float) -> str:
    return f"{math.degrees(v):+6.1f}"


def probe(args) -> None:
    pods = AirPods()
    mapper = Mapper(set(args.flip), smooth=0, lead_s=0, relative_tilt=args.relative_tilt)
    keys = Keys()
    leveler = Leveler()
    print("Probe: shake (yaw), nod (pitch), tilt (roll). c recenter yaw, l level, q quit.")
    print("Pitch and roll are against gravity: a level head should read 0 / 0 "
          "(if not, press l and look level at the horizon).")
    print("Turning to YOUR left should give yaw +, looking DOWN pitch +, "
          "tilting toward your RIGHT shoulder roll +.")
    try:
        while True:
            pods.fresh.wait(0.2)
            pods.fresh.clear()
            if pods.fatal:
                raise SystemExit(explain_fatal(pods.fatal))
            k = keys.poll()
            if k in ("q", "Q"):
                break
            _, s = pods.latest()
            if s is None or not pods.live():
                sys.stdout.write("\rwaiting for AirPods (put them in, connected to this Mac) ...")
                sys.stdout.flush()
                continue
            if k in ("c", "C") or mapper.ref is None:
                mapper.recenter(s["q"])
            if k in ("l", "L"):
                leveler.start(time.monotonic())
            lv = leveler.feed(mapper, s["q"], time.monotonic()) if leveler.active() else None
            yaw, pitch, roll = mapper.raw(s["q"])
            sg = mapper.sign
            yaw, pitch, roll = yaw * sg["yaw"], pitch * sg["pitch"], roll * sg["roll"]
            words = []
            if abs(yaw) > D(8):
                words.append("left" if yaw > 0 else "right")
            if abs(pitch) > D(8):
                words.append("down" if pitch > 0 else "up")
            if abs(roll) > D(8):
                words.append("tilt-right" if roll > 0 else "tilt-left")
            sys.stdout.write(f"\ryaw {fmt(yaw)}  pitch {fmt(pitch)}  roll {fmt(roll)}   "
                             f"{pods.hz:4.0f} Hz  age {s.get('age', 0):4.0f} ms   "
                             f"{lv or ' '.join(words) or 'center':<24}")
            sys.stdout.flush()
    finally:
        keys.restore()
        pods.close()
        print()


def follow(args) -> None:
    dry = args.dry_run
    host = port = None
    fdata = load_state()
    home = resolve_home(fdata)
    home_hp = dict(home[0]) if home else {k: 0.0 for k in ("x", "y", "z", "roll", "pitch", "yaw")}

    body0 = 0.0
    senses = streamer = None
    if not dry:
        host = resolve_host()
        port = int(os.environ.get("REACHY_PORT", "8000"))
        st = preflight(host, port, args.stop_app, getattr(args, "keep_app", None))
        body0 = float(st.get("body_yaw") or 0.0)
        senses = Senses(host)
        if not senses.token:
            print("note: no .run/senses_token, so the robot's Follow / room watch won't pause "
                  "(./scripts/sense-robot.sh install).", file=sys.stderr)
        senses.start()
        if fdata.get("state") == "asleep":
            print("note: Peachy was asleep; waking the head to home first.", file=sys.stderr)
        goto(host, port, home_hp, body0, 1.0)
        wait_moves(host, port)
        streamer = Streamer(host, port)

    pods = AirPods()
    mapper = Mapper(set(args.flip), smooth=args.smooth, lead_s=args.lead / 1000.0,
                    relative_tilt=args.relative_tilt)
    keys = Keys()

    stop = threading.Event()
    recenter_req = threading.Event()
    level_req = threading.Event()
    leveler = Leveler()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    signal.signal(signal.SIGUSR1, lambda *_: recenter_req.set())
    signal.signal(signal.SIGUSR2, lambda *_: level_req.set())
    status_file = Path(args.status_file) if args.status_file else None
    extra_status = getattr(args, "extra_status", None)
    last_status = 0.0

    body = body0
    base_yaw = body0 + home_hp["yaw"]
    paused = False
    homed = False           # glided home after losing the AirPods
    need_recenter = False
    # Tilt is absolute, so the first target can be far from home: glide into it.
    blend_from: dict | None = {**home_hp, "_body": body0}
    blend_at: float | None = None       # None: starts with the first sample
    last_sent: dict | None = None
    last_seq = -1
    sent_n, sent_hz_at, send_hz = 0, time.monotonic(), 0.0
    last_draw = 0.0
    last_t = time.monotonic()
    status = "starting"

    print("Following. c recenter, l level, space pause, q quit."
          + ("  (dry run: nothing is sent)" if dry else ""))
    try:
        while not stop.is_set():
            pods.fresh.wait(1.0 / SEND_HZ)
            pods.fresh.clear()
            if pods.fatal:
                raise SystemExit(explain_fatal(pods.fatal))
            now = time.monotonic()
            dt, last_t = min(0.1, now - last_t), now

            k = keys.poll()
            if k in ("q", "Q"):
                break
            if k == " ":
                paused = not paused
                if not paused and last_sent:
                    blend_from, blend_at = dict(last_sent), now
            if k in ("c", "C") or recenter_req.is_set():
                recenter_req.clear()
                need_recenter = True
            if k in ("l", "L") or level_req.is_set():
                level_req.clear()
                leveler.start(now)

            seq, s = pods.latest()
            live = pods.live()
            if not live:
                settling = s is not None and pods.connected and now - s["rx"] < STALE_S
                status = ("no AirPods" if s is None else "AirPods connecting" if settling
                          else "AirPods gone, holding")
                if s is not None and not homed and now - pods.gone_at > HOLD_S:
                    status = "AirPods gone, home"
                    if not dry:
                        goto(host, port, home_hp, body0, 1.0)
                    body, homed, need_recenter = body0, True, True
                    last_sent = {**home_hp, "_body": body0}
                    mapper.recenter(s["q"])
            elif seq != last_seq and not paused:
                last_seq = seq
                if homed:
                    homed = False
                    blend_from, blend_at = dict(last_sent), now
                if need_recenter:
                    need_recenter = False
                    if last_sent:
                        blend_from, blend_at = dict(last_sent), now
                    mapper.recenter(s["q"])
                leveling = None
                if leveler.active():
                    leveling = leveler.feed(mapper, s["q"], now)
                    if leveling is None and last_sent:
                        blend_from, blend_at = dict(last_sent), now
                me = mapper(s["q"], s["t"])

                want = base_yaw + me["yaw"]
                if args.no_body:
                    body = body0
                else:
                    tbody = clip(want - clip(want - body, -HEAD_SOFT, HEAD_SOFT), -BODY_MAX, BODY_MAX)
                    body += clip(tbody - body, -BODY_RATE * dt, BODY_RATE * dt)
                head = dict(home_hp)
                head["yaw"] = body + clip(want - body, -HEAD_HARD, HEAD_HARD)
                tilt = home_hp if args.relative_tilt else {"pitch": 0.0, "roll": 0.0}
                head["pitch"] = clip(tilt["pitch"] + me["pitch"], -PITCH_MAX, PITCH_MAX)
                head["roll"] = clip(tilt["roll"] + me["roll"], -ROLL_MAX, ROLL_MAX)
                out_body = body

                if blend_from is not None:
                    if blend_at is None:
                        blend_at = now
                    a = (now - blend_at) / BLEND_S
                    if a >= 1.0:
                        blend_from = None
                    else:
                        a = a * a * (3 - 2 * a)
                        for key in ("roll", "pitch", "yaw"):
                            head[key] = blend_from[key] + (head[key] - blend_from[key]) * a
                        out_body = blend_from["_body"] + (body - blend_from["_body"]) * a

                if streamer is not None:
                    streamer.submit(head, out_body)
                last_sent = {**head, "_body": out_body}
                sent_n += 1
                status = leveling or "following"
            elif paused:
                status = "paused"

            if now - sent_hz_at >= 1.0:
                if streamer is not None:
                    sent_n = streamer.sent
                    streamer.sent = 0
                send_hz, sent_n, sent_hz_at = sent_n / (now - sent_hz_at), 0, now
            if now - last_draw > 0.1:
                last_draw = now
                line = f"\r{status:<22}"
                if last_sent:
                    rel = last_sent["yaw"] - last_sent["_body"]
                    line += (f" head yaw {fmt(rel)} pitch {fmt(last_sent['pitch'])} "
                             f"roll {fmt(last_sent['roll'])}  body {fmt(last_sent['_body'])}")
                age = s.get("age", 0.0) if s else 0.0
                line += f"  {pods.hz:3.0f}->{send_hz:3.0f} Hz  age {age:3.0f}"
                if streamer:
                    line += f" ms, send {streamer.ms:3.0f} ms {streamer.via}"
                else:
                    line += " ms"
                sys.stdout.write(line + "   ")
                sys.stdout.flush()
            if status_file and now - last_status > 0.2:
                last_status = now
                deg = (lambda v: round(math.degrees(v), 1))
                write_status(status_file, {
                    "status": status, "at": time.time(), "pid": os.getpid(),
                    "head": None if not last_sent else {
                        "yaw": deg(last_sent["yaw"] - last_sent["_body"]),
                        "pitch": deg(last_sent["pitch"]), "roll": deg(last_sent["roll"]),
                        "body": deg(last_sent["_body"])},
                    "hz_in": round(pods.hz), "hz_out": round(send_hz),
                    "age_ms": round(s.get("age", 0.0)) if s else None,
                    "via": streamer.via if streamer else "dry-run",
                    "level": {k: round(math.degrees(mapper.level[k]), 1) for k in ("pitch", "roll")},
                    **(extra_status() if extra_status else {}),
                })
    except KeyboardInterrupt:
        pass
    finally:
        keys.restore()
        print()
        if status_file:
            write_status(status_file, {"status": "stopping", "at": time.time(), "pid": os.getpid()})
        pods.close()
        if streamer is not None:
            streamer.close()
            print("gliding home ...")
            goto(host, port, home_hp, body0, 1.0)
            wait_moves(host, port)
        if senses:
            senses.release()
        if status_file:
            status_file.unlink(missing_ok=True)


def write_status(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="AirPods only: print angles, move nothing")
    ap.add_argument("--dry-run", action="store_true", help="run the pipeline without sending to the robot")
    ap.add_argument("--stop-app", action="store_true", help="stop a running daemon app first")
    ap.add_argument("--no-body", action="store_true", help="keep the body still (head yaw clamps at 63 deg)")
    ap.add_argument("--smooth", type=float, default=1.0,
                    help="One Euro smoothing: 0 = raw, 1 = default, higher = steadier but laggier")
    ap.add_argument("--lead", type=float, default=0.0,
                    help="predict this many ms ahead from head speed (e.g. 40) to hide robot lag")
    ap.add_argument("--flip", type=lambda s: [x for x in s.split(",") if x], default=[],
                    help="comma list of axes to invert: yaw,pitch,roll (yaw alone = mirror)")
    ap.add_argument("--relative-tilt", action="store_true",
                    help="pitch/roll relative to the recentered pose, added to home "
                         "(default: absolute against gravity)")
    ap.add_argument("--status-file", help="write live status JSON here (the console reads it)")
    args = ap.parse_args()
    bad = set(args.flip) - {"yaw", "pitch", "roll"}
    if bad:
        ap.error(f"--flip: unknown axis {', '.join(sorted(bad))}")
    if args.probe:
        probe(args)
    else:
        follow(args)


if __name__ == "__main__":
    main()
