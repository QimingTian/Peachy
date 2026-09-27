#!/usr/bin/env python3
"""Peachy's senses and daily routine, run on the robot (systemd unit peachy-senses,
installed by scripts/sense-robot.sh). Everything here works with the laptop off.

- State: asleep / semi (Dozing) / awake, kept in ~/.peachy/state.json. The console
  reports its own actions (POST /state); the routine below updates it too.
- Room watch (setting "watch"). By day ("day", 07:00-23:00) an asleep Peachy
  dozes: conversation app warm, head tucked, mic muted, body at the doze direction.
  When the lights come on (mean luma of the tucked camera view crosses the
  cal-light thresholds and jumps within 8 s) it lifts the head and sweeps the body;
  a face wakes it with a greeting, nobody means back to Dozing. A conversation
  nobody has spoken to for doze_after_s dozes again by day and sleeps by night.
  At night Dozing or an idle Awake goes to sleep.
- Follow (setting "follow"): awake with no app running, it follows the nearest
  face alt-azimuth (yaw and pitch; roll and position held at "level"), the body
  takes over past 18°. It doesn't turn toward voices: the mic array flags the
  robot's own motor noise as speech, from straight left or right, so every turn
  set off the next one.
- Wake word (setting "wake"): "Hey Peachy" while nothing else has the robot
  (asleep, Dozing, or awake with no app) wakes it into a conversation, turned
  toward the voice. openWakeWord models (wake.py) on the shared ALSA mic, only
  in those states and only while the room isn't quiet.

Faces: YuNet (reachy_mini.vision) on the daemon's camera over IPC, 320 px wide,
10 times a second at normal priority, only while following or looking around.
Motion goes through the local daemon REST API and the app patch's
~/.peachy/motion.json. Settings live in ~/.peachy/senses.json; the console pushes
them (POST /config) and they stay for when the laptop is off.

  GET  /status
  POST /config {...}              (token)
  POST /state  {"state": ...}     (token)
  POST /busy   {"on": bool, "ttl": s}  (token) the console is moving the robot
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8767
DAEMON = "http://127.0.0.1:8000"
RPC = "ws://127.0.0.1:7860/rpc"
CONVO_APP = "reachy_mini_conversation_app"
HOME = Path.home() / ".peachy"
CFG_FILE = HOME / "senses.json"
STATE_FILE = HOME / "state.json"
MOTION_FILE = HOME / "motion.json"
TOKEN_FILE = HOME / "senses_token"
DEFAULT_CFG = {
    "follow": False, "watch": False, "day": "07:00-23:00", "doze_after_s": 180.0,
    "doze_body": 0.0, "doze_deg": None, "scan_body": [],
    "light": {"dark_max": 30.0, "lit_min": 70.0, "jump": 35.0},
    "level": None, "sleep_pose": None, "greeting": "Hi! I'm here.", "volume": 100, "voice": "ballad",
    "wake": False, "wake_model": "hey_peachy.onnx", "wake_threshold": 0.7, "notify_url": "",
}
CONVO_PKG = Path("/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini_conversation_app")

CAMERA_IDLE_S = 20.0
CAMERA_WARMUP_S = 2.0             # auto-exposure after the camera opens
FACE_HZ = 10.0
LIGHT_HZ = 4.0
FACE_W = 320
FOCAL_N = 1.27                    # focal / half image width (cal-heading fit: 405 px at 640)
ASPECT = 16 / 9
LAG_S = 0.15                      # camera + detection; aim from where the head was then
GAIN = 0.7
DEAD = math.radians(2)
HEAD_MAX = math.radians(40)       # head yaw vs body
PITCH_MIN, PITCH_MAX = math.radians(-22), math.radians(25)
BODY_MAX = math.radians(160)
BODY_AT = math.radians(18)        # head this far off the body for BODY_S...
BODY_S = 0.8
BODY_STEP = math.radians(8)       # ...moves the body up to this much toward it
BODY_RATE = math.radians(90)      # rad/s
SMOOTH = 6.0                      # 1/s, head easing toward the aim
LEVEL_RATE = 3.0                  # 1/s, roll / position easing to the level pose
CONTROL_HZ = 25.0
RELEASE_S = 6.0                   # nobody in view this long: stop following (head stays)
DRIFT = math.radians(14)          # head moved by someone else this far for DRIFT_S: yield
DRIFT_S = 0.8
YIELD_S = 4.0
VOICE_MIN = math.radians(25)
SLEEP_S = 2.8

_AXES = ("x", "y", "z", "roll", "pitch", "yaw")
_LEVEL_KEYS = ("x", "y", "z", "roll")

_lock = threading.RLock()
_file_lock = threading.Lock()
robot = {"state": {}, "state_at": 0.0, "app": {}, "app_at": 0.0, "move": False}
eyes = {"mode": None, "faces": 0, "target": None, "seen_at": 0.0, "hits": deque(maxlen=40),
        "ms": None, "hz": None, "camera": "closed", "error": "", "luma": None, "luma_at": 0.0}
aim = {"engaged": False, "cmd": None, "body": 0.0, "tyaw": 0.0, "tpitch": 0.0, "tbody": 0.0,
       "level": None, "off_since": 0.0, "drift_since": 0.0, "busy_until": 0.0, "yield_until": 0.0,
       "hist": deque(maxlen=60), "status": "off"}
busy = {"until": 0.0}
events: deque = deque(maxlen=60)
_event_id = [0]
_act: dict = {"thread": None, "what": ""}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def note(msg: str, ok: bool = True) -> None:
    log(("" if ok else "✗ ") + msg)
    with _lock:
        _event_id[0] += 1
        events.append({"id": _event_id[0], "t": time.time(), "msg": msg, "ok": ok})
    if not ok:
        notify(msg)


_notified: dict = {}


def notify(msg: str) -> None:
    """Problems to the "notify_url" setting (ntfy.sh style: POST the text), each
    message at most every 30 min, so they reach you with the laptop off."""
    url = cfg().get("notify_url") or ""
    now = time.time()
    if not url or now - _notified.get(msg, 0.0) < 1800:
        return
    _notified[msg] = now

    def send() -> None:
        try:
            req = urllib.request.Request(url, data=msg.encode(), method="POST",
                                         headers={"Title": "Peachy", "Tags": "warning"})
            urllib.request.urlopen(req, timeout=10).read()
        except (urllib.error.URLError, OSError, ValueError):
            pass
    threading.Thread(target=send, daemon=True).start()


def clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def read_json(path: Path, default: dict) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else dict(default)
    except (OSError, ValueError):
        return dict(default)


def write_json(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, path)


def cfg() -> dict:
    return {**DEFAULT_CFG, **read_json(CFG_FILE, {})}


def motion(update: dict | None = None) -> dict:
    """~/.peachy/motion.json, which the app patch reads live."""
    with _file_lock:
        d = read_json(MOTION_FILE, {})
        if update:
            d.update(update)
            write_json(MOTION_FILE, d)
        return d


def life_state() -> str:
    return read_json(STATE_FILE, {}).get("state", "unknown")


def set_state(state: str) -> None:
    with _file_lock:
        write_json(STATE_FILE, {"state": state, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")})


def daemon(path: str, method: str = "GET", body: dict | None = None, timeout: float = 3.0):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(DAEMON + path, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


def rpc(method: str, params: dict | None = None, timeout: float = 10.0):
    from websockets.sync.client import connect
    with connect(RPC, open_timeout=min(timeout, 5.0)) as ws:
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while True:
            msg = json.loads(ws.recv(timeout=max(0.1, end - time.time())))
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:160])
                return msg.get("result")


def is_day(day: str) -> bool:
    try:
        a, b = ((int(h) * 60 + int(m)) for h, m in (x.strip().split(":") for x in day.split("-")))
    except ValueError:
        a, b = 7 * 60, 23 * 60
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return a <= t < b if a <= b else (t >= a or t < b)


# ------------------------------------------------------------------ who owns the robot

def console_busy() -> bool:
    return time.monotonic() < busy["until"]


def acting() -> str:
    t = _act["thread"]
    return _act["what"] if t is not None and t.is_alive() else ""


def act(what: str, fn) -> bool:
    """Run one robot action at a time, off the loop threads."""
    if acting():
        return False

    def run() -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            note(f"{what} failed: {type(e).__name__}: {e}"[:200], False)

    _act["what"] = what
    _act["thread"] = threading.Thread(target=run, name=f"act-{what}", daemon=True)
    _act["thread"].start()
    return True


def app_info() -> dict:
    """Current daemon app: {name, state} or {}."""
    a = robot["app"]
    if a and a.get("state") in ("starting", "running", "stopping"):
        return a
    return {}


def semi_active() -> bool:
    """Conversation app running with the head held (Dozing)."""
    return app_info().get("name") == CONVO_APP and motion().get("mode", "free") != "free"


def owner() -> str:
    if time.monotonic() - robot["app_at"] > 5:
        return "offline"
    a = app_info()
    if a:
        if a.get("name") == CONVO_APP:
            return "" if semi_active() else "conversation"
        return "app"
    if robot["move"] and time.monotonic() > aim["busy_until"]:
        return "move"
    return ""


def state_loop() -> None:
    q = "/api/state/full?with_head_pose=true&with_body_yaw=true&with_doa=true&with_control_mode=true"
    while True:
        t0 = time.monotonic()
        try:
            robot["state"], robot["state_at"] = daemon(q), time.monotonic()
            if t0 - robot["app_at"] > 1.0:
                st = daemon("/api/apps/current-app-status") or {}
                robot["app"] = ({"name": (st.get("info") or {}).get("name", ""), "state": st.get("state")}
                                if st else {})
                robot["move"] = bool(daemon("/api/move/running"))
                robot["app_at"] = time.monotonic()
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1.0)
        period = 0.1 if eyes["mode"] == "follow" else 0.5
        time.sleep(max(0.0, period - (time.monotonic() - t0)))


# ------------------------------------------------------------------ actions

def convo_running() -> bool:
    try:
        st = daemon("/api/apps/current-app-status") or {}
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return (st.get("info") or {}).get("name") == CONVO_APP and st.get("state") in ("starting", "running")


def start_convo() -> None:
    if convo_running():
        return
    c = cfg()
    daemon("/api/motors/set_mode/enabled", "POST", timeout=10)
    daemon(f"/api/apps/start-app/{CONVO_APP}", "POST", timeout=20)
    for _ in range(45):
        st = daemon("/api/apps/current-app-status") or {}
        if st.get("state") == "running":
            break
        time.sleep(1)
    else:
        raise RuntimeError("conversation app did not start")
    try:
        daemon("/api/volume/set", "POST", {"volume": int(c["volume"])}, timeout=12)
    except (urllib.error.URLError, OSError, ValueError):
        pass
    for _ in range(10):
        try:
            r = rpc("voices.apply", {"voice": c["voice"]}, 12)
            if not (isinstance(r, dict) and r.get("ok") is False):
                break
        except Exception:  # noqa: BLE001 - the app's /rpc takes a few seconds to come up
            pass
        time.sleep(1.5)


def set_body(rad: float, wait: bool = False) -> None:
    motion({"body_yaw": float(rad)})
    if not wait:
        return
    end = time.monotonic() + 10
    while time.monotonic() < end:
        try:
            b = float(daemon("/api/state/full?with_body_yaw=true").get("body_yaw") or 0.0)
            if abs(wrap(b - rad)) < math.radians(3):
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.2)


_patch = {"ok": True, "at": 0.0}


def patch_ok() -> bool:
    """The Peachy patch is still in the conversation app (an app update removes it,
    and without it Dozing would be the stock app: head free, mic live)."""
    if time.time() - _patch["at"] > 600:
        try:
            _patch["ok"] = ("import peachy_patch" in (CONVO_PKG / "main.py").read_text()
                            and (CONVO_PKG / "peachy_patch.py").exists())
        except OSError:
            _patch["ok"] = False
        _patch["at"] = time.time()
    return _patch["ok"]


def enter_semi() -> None:
    """Dozing: head tucked, mic muted, body at the doze direction, app warm."""
    if not patch_ok():
        raise RuntimeError("the conversation app lost the Peachy patch (app update?) — "
                           "run scripts/app-patch.sh apply")
    m = motion()
    if not (m.get("tucked") and m.get("lifted")):
        raise RuntimeError("no Dozing poses yet — use Doze once from the console")
    motion({"mode": "tucked", "body_yaw": float(cfg()["doze_body"]), "head_pitch": 0.0})
    start_convo()
    set_state("semi")
    note("dozing")


def wake_from_semi() -> None:
    motion({"mode": "free", "head_pitch": 0.0})
    set_state("awake")
    greet()


def greet() -> None:
    try:
        rpc("conversation.say", {"text": f'Say exactly this and nothing else: "{cfg()["greeting"]}"'}, 8)
    except Exception as e:  # noqa: BLE001
        note(f"greeting failed: {e}"[:160], False)


def wake_by_voice(rel: float | None) -> None:
    """Into a conversation from asleep, Dozing or an idle Awake, then face the voice."""
    state = life_state()
    if state == "semi" and semi_active():
        wake_from_semi()
    else:
        if state == "asleep":
            daemon("/api/motors/set_mode/enabled", "POST", timeout=10)
            aim["busy_until"] = time.monotonic() + 30
            try:
                turn_body(0.0)          # wake_up ends at body 0, in a move timed for the head
                daemon("/api/move/play/wake_up", "POST", timeout=30)
                time.sleep(1.0)
                wait_moves()
            finally:
                aim["busy_until"] = 0.0
        motion({"mode": "free", "head_pitch": 0.0})
        start_convo()
        set_state("awake")
        greet()
    if rel is not None and abs(rel) > VOICE_MIN:
        body = float(robot["state"].get("body_yaw") or 0.0)
        set_body(clip(body + rel, -BODY_MAX, BODY_MAX))


def wait_moves(max_s: float = 20.0) -> None:
    end = time.monotonic() + max_s
    while time.monotonic() < end:
        try:
            if not daemon("/api/move/running"):
                return
        except (urllib.error.URLError, OSError, ValueError):
            return
        time.sleep(0.2)


def rotated(head_pose: dict, angle: float) -> dict:
    """*head_pose* (base frame) turned about the vertical by *angle*, position too:
    the same pose relative to a body turned that much."""
    x, y = float(head_pose.get("x", 0.0)), float(head_pose.get("y", 0.0))
    c, s = math.cos(angle), math.sin(angle)
    return {**head_pose, "x": c * x - s * y, "y": s * x + c * y,
            "yaw": float(head_pose.get("yaw", 0.0)) + angle}


def turn_body(to: float) -> None:
    """Body to `to`, the head turning with it."""
    st = daemon("/api/state/full?with_head_pose=true&with_body_yaw=true")
    delta = to - float(st.get("body_yaw") or 0.0)
    if abs(delta) < math.radians(3):
        return
    hp = rotated(dict(st.get("head_pose") or {}), delta)
    dur = max(1.2, abs(delta) / math.radians(60))
    daemon("/api/move/goto", "POST", {"head_pose": hp, "body_yaw": to, "duration": dur,
                                      "interpolation": "minjerk"}, timeout=dur + 10)
    time.sleep(dur)
    wait_moves()


def stop_moves() -> None:
    """Stop every running move (/api/move/stop takes one move's uuid)."""
    try:
        running = daemon("/api/move/running") or []
    except (urllib.error.URLError, OSError, ValueError):
        return
    for m in running if isinstance(running, list) else []:
        try:
            daemon("/api/move/stop", "POST", {"uuid": m.get("uuid") if isinstance(m, dict) else m}, timeout=6)
        except (urllib.error.URLError, OSError, ValueError):
            pass


def stop_app() -> None:
    """Stop the running app and wait out what the daemon does next: 1.5 s after an
    app exits it puts the robot to sleep on its own (lift, sleep pose, motors off),
    ignoring or undoing moves sent meanwhile."""
    daemon("/api/apps/stop-current-app", "POST", timeout=30)
    for _ in range(20):
        if not app_info():
            break
        time.sleep(0.5)
    time.sleep(2.0)
    end = time.monotonic() + 12
    while time.monotonic() < end:
        try:
            if daemon("/api/motors/status").get("mode") == "disabled":
                time.sleep(0.3)
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.3)


def go_sleep() -> None:
    """App off, body to the Dozing direction, slow droop to the calibrated sleep
    pose, snore, motors off."""
    aim["busy_until"] = time.monotonic() + 90
    if app_info():
        stop_app()
    stop_moves()
    daemon("/api/motors/set_mode/enabled", "POST", timeout=10)
    pose = cfg()["sleep_pose"]
    try:
        if pose:
            body = float(cfg()["doze_body"])
            turn_body(body)
            hp = rotated(pose["head_pose"], body)   # calibrated at body 0
            daemon("/api/move/goto", "POST", {"head_pose": hp, "antennas": pose["antennas"],
                                             "duration": SLEEP_S, "interpolation": "minjerk"}, timeout=30)
        else:
            daemon("/api/move/play/goto_sleep", "POST", timeout=60)
        time.sleep(SLEEP_S)
        wait_moves()
        try:
            daemon("/api/media/play_sound", "POST", {"file": "go_sleep.wav"}, timeout=12)
        except (urllib.error.URLError, OSError, ValueError):
            pass
        daemon("/api/motors/set_mode/disabled", "POST", timeout=10)
    finally:
        aim["busy_until"] = 0.0
    set_state("asleep")
    note("asleep")


# ------------------------------------------------------------------ conversation activity

class ConvoActivity:
    """When someone last spoke to the conversation app, from its /rpc notifications."""

    USER = {"user_speech_started", "user_transcription_completed"}

    def __init__(self) -> None:
        self.last = 0.0
        self.ok_at = time.time()        # /rpc last reachable
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        from websockets.sync.client import connect
        while True:
            if app_info().get("name") != CONVO_APP:
                time.sleep(2)
                continue
            try:
                with connect(RPC, open_timeout=5) as ws:
                    while app_info().get("name") == CONVO_APP:
                        self.ok_at = time.time()
                        try:
                            msg = json.loads(ws.recv(timeout=5))
                        except TimeoutError:
                            continue
                        p = msg.get("params") or {}
                        if ((msg.get("method") == "conversation.activity" and p.get("reason") in self.USER)
                                or (msg.get("method") == "conversation.transcript" and p.get("role") == "user")):
                            self.last = time.time()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)


# ------------------------------------------------------------------ room watch

class Watch:
    """Day: Dozing at the doze direction; lights on → lift the head and sweep for a face."""

    SETTLE_S = 3.0
    WINDOW_S = 8.0

    def __init__(self) -> None:
        self.convo = ConvoActivity()
        self.status = "off"
        self.period: str | None = None
        self.state: str | None = None
        self.awake_since = 0.0
        self.lost_since = 0.0
        self.doze_in: float | None = None
        self.luma: float | None = None
        self.level: str | None = None
        self.last_level: str | None = None
        self.raw: deque = deque()
        self.smooth: deque = deque()
        self.settled_since = 0.0
        self.away_since = 0.0
        self.parked: float | None = None
        self.history: deque = deque(maxlen=120)
        self.history_at = 0.0
        self.event = ""
        self.event_at = 0.0
        self.scanning = False
        self.used_at = time.time()      # last sign of anyone using an Awake Peachy
        self.hold: tuple[str, str | None] | None = None
        self.retry_at = 0.0
        self.drift_since = 0.0
        self.patch_said = 0.0

    def note(self, msg: str, ok: bool = True) -> None:
        self.event, self.event_at = msg, time.time()
        note(msg, ok)

    def wants_light(self) -> bool:
        c = cfg()
        return (c["watch"] and not self.scanning and self.period == "day" and self.state == "semi"
                and not console_busy())

    def manual(self, state: str) -> None:
        """The console set *state*: keep it until the next day/night switch."""
        self.hold = (state, self.period)

    def step(self) -> None:
        now = time.time()
        c = cfg()
        if not c["watch"]:
            self.status, self.period, self.doze_in = "off", None, None
            self._reset_light()
            return
        if console_busy():
            self.status = "paused:busy"
            self.used_at = now
            return
        own = owner()
        if own == "offline":
            self.status = "paused:offline"
            return
        state = life_state()
        if state != self.state:
            if state == "awake":
                self.awake_since = self.used_at = now
            self.state = state
        period = "day" if is_day(c["day"]) else "night"
        if period != self.period:
            first = self.period is None
            self.period = period
            if not first:
                self.hold = None
                self._schedule(period, state, own)
        self.doze_in = None

        if self.scanning:
            self.status = "looking"
            return
        self._health(now, state, own)
        if self._drifted(now, state, own):
            return
        if state == "awake" and own == "conversation":
            self.status = "awake"
            self.used_at = now
            self._reset_light()
            after = float(c["doze_after_s"])
            idle = now - max(self.convo.last, self.awake_since)
            self.doze_in = max(0.0, after - idle)
            if idle >= after and not acting():
                self.awake_since = now - after + 30
                if period == "day":
                    self.note(f"nobody has spoken for {idle / 60:.0f} min — dozing")
                    act("doze", enter_semi)
                else:
                    self.note(f"nobody has spoken for {idle / 60:.0f} min — going to sleep")
                    act("sleep", go_sleep)
            return
        if period == "day" and state == "semi" and not own and not acting():
            if semi_active():
                self.lost_since = 0.0
                self._light(now)
                return
            self.lost_since = self.lost_since or now
            if now - self.lost_since > 30:
                self.lost_since = now
                self.note("the conversation app is gone while Dozing — restarting it", False)
                act("doze", enter_semi)
        self._reset_light()
        self._reconcile(now, period, state, own, float(c["doze_after_s"]))
        if own and own != "conversation":
            self.status = f"paused:{own}"
        else:
            self.status = {"asleep": "night" if period == "night" else "asleep",
                           "semi": "dozing", "awake": "awake"}.get(state, "waiting")

    def _schedule(self, period: str, state: str, own: str) -> None:
        """What changes right at 07:00 / 23:00; _reconcile keeps it that way."""
        if period == "day":
            if state == "asleep":
                self.note("morning — dozing")
                act("doze", enter_semi)
        elif state == "semi" or (state == "awake" and not own):
            self.note("night — going to sleep")
            act("sleep", go_sleep)

    def _reconcile(self, now: float, period: str, state: str, own: str, idle_after: float) -> None:
        """Every step: bring Peachy to where it should be for the time of day, so a
        failed or missed change is retried and a restart needs no special case. A
        state the console chose holds until the next day/night switch, except an
        idle Awake, which times out like a conversation nobody talks to."""
        if own or acting() or self.scanning:
            self.used_at = now
            return
        if state == "awake":
            if time.monotonic() - eyes["seen_at"] < 2.0:
                self.used_at = now
            idle = now - self.used_at
            self.doze_in = max(0.0, idle_after - idle)
            if idle < idle_after or now < self.retry_at:
                return
            self.retry_at = now + 60
            doze = period == "day" and patch_ok()
            self.note(f"awake with nobody around for {idle / 60:.0f} min — "
                      + ("dozing" if doze else "going to sleep"))
            act("doze" if doze else "sleep", enter_semi if doze else go_sleep)
            return
        want = "semi" if period == "day" else "asleep"
        if state == want or self.hold == (state, period) or now < self.retry_at:
            return
        if state not in ("asleep", "semi"):
            return
        if want == "semi" and not patch_ok():
            return
        self.retry_at = now + 60
        if want == "semi":
            self.note("day, and asleep — dozing")
            act("doze", enter_semi)
        else:
            self.note("night, and dozing — going to sleep")
            act("sleep", go_sleep)

    def _drifted(self, now: float, state: str, own: str) -> bool:
        """Motors off with no app while we think Peachy is up: the daemon put it to
        sleep behind our back (after an app or a remote session, a crash, a restart)."""
        if (state in ("awake", "semi") and not own and not acting()
                and robot["state"].get("control_mode") == "disabled"):
            self.drift_since = self.drift_since or now
            if now - self.drift_since > 10:
                self.drift_since = 0.0
                self.hold = None
                set_state("asleep")
                self.note(f"motors off with no app while {state} — the daemon put Peachy to sleep", False)
                return True
        else:
            self.drift_since = 0.0
        return False

    def _health(self, now: float, state: str, own: str) -> None:
        """Hourly: say so if the app patch is gone (Dozing then stays asleep). And
        the conversation app is up but its /rpc has been unreachable for 5 min:
        restart it while Dozing (nobody is talking to it), else just say so."""
        if not patch_ok() and now - self.patch_said > 3600:
            self.patch_said = now
            self.note("the conversation app lost the Peachy patch (app update?) — no Dozing until "
                      "scripts/app-patch.sh apply", False)
        if not (app_info().get("name") == CONVO_APP and app_info().get("state") == "running"):
            self.convo.ok_at = now
            return
        if now - self.convo.ok_at < 300 or acting():
            return
        self.convo.ok_at = now
        if state == "semi":
            self.note("the conversation app stopped answering while Dozing — restarting it", False)
            act("doze", lambda: (stop_app(), enter_semi()))
        else:
            self.note("the conversation app stopped answering", False)

    def _reset_light(self) -> None:
        self.level = None
        self.raw.clear()
        self.smooth.clear()
        self.settled_since = 0.0

    def _at_doze(self, body: float, doze: float) -> bool:
        """At the doze direction, or where the body stopped short of it last turn."""
        if abs(wrap(body - doze)) <= math.radians(4):
            return True
        p = self.parked
        return (p is not None and abs(wrap(p - doze)) <= math.radians(10)
                and abs(wrap(body - p)) <= math.radians(2))

    def _park(self, doze: float) -> None:
        motion({"mode": "tucked"})
        set_body(doze, True)
        time.sleep(0.5)
        self.parked = robot["state"].get("body_yaw")

    def _light(self, now: float) -> None:
        c = cfg()
        doze = float(c["doze_body"])
        body = robot["state"].get("body_yaw")
        if body is None or not self._at_doze(float(body), doze):
            self._reset_light()
            self.status = "settling"
            self.away_since = self.away_since or now
            if body is not None and now - self.away_since > 6:
                self.away_since = now
                self.note(f"body at {-math.degrees(float(body)):+.0f}° (encoder), turning back to the doze direction")
                act("face the doze direction", lambda: self._park(doze))
            return
        self.away_since = 0.0
        if eyes["luma"] is None or time.monotonic() - eyes["luma_at"] > 1.5:
            self.status = "waiting for camera"
            return
        self.settled_since = self.settled_since or now
        y = float(eyes["luma"])
        self.luma = y
        if now - self.settled_since < self.SETTLE_S:
            self.status = "settling"
            return
        self.status = "dozing"
        self.raw.append((now, y))
        while self.raw and now - self.raw[0][0] > 0.6:
            self.raw.popleft()
        vals = sorted(v for _, v in self.raw)
        cur = vals[len(vals) // 2]
        self.smooth.append((now, cur))
        while self.smooth and now - self.smooth[0][0] > self.WINDOW_S:
            self.smooth.popleft()
        if now - self.history_at >= 1.0:
            self.history_at = now
            self.history.append(round(cur, 1))
        th = c["light"]
        if self.level is None:
            if len(self.smooth) >= 5:
                self.level = "lit" if cur >= th["lit_min"] else "dark"
                log(f"watch: room is {self.level} ({cur:.0f})")
            return
        if self.level == "dark" and cur >= th["lit_min"]:
            self.level = "lit"
            base = min(v for _, v in self.smooth)
            if cur - base >= th["jump"]:
                self.note(f"lights on ({base:.0f} → {cur:.0f}) — looking around")
                self.start_scan()
            else:
                self.note(f"brightened slowly ({base:.0f} → {cur:.0f}) — daylight, staying put")
        elif self.level == "lit" and cur <= th["dark_max"]:
            self.level = "dark"
            self.note(f"lights off ({cur:.0f})")

    def start_scan(self) -> None:
        if acting():
            return
        self.scanning = True
        if not act("look around", self._scan):
            self.scanning = False

    @staticmethod
    def _face_seen() -> bool:
        now = time.monotonic()
        return sum(1 for t in list(eyes["hits"]) if now - t < 1.0) >= 2

    def _dwell(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._face_seen():
                return True
            time.sleep(0.1)
        return self._face_seen()

    def _scan(self) -> None:
        c = cfg()
        doze = float(c["doze_body"])
        found = done = False
        try:
            eyes["hits"].clear()
            motion({"mode": "lifted"})
            found = self._dwell(1.8)
            if not found:
                for b in c["scan_body"]:
                    if life_state() != "semi" or console_busy():
                        self.note("look-around interrupted")
                        done = True
                        return
                    set_body(float(b), wait=True)
                    if self._dwell(0.9):
                        found = True
                        break
            if found:
                self.note("someone's here — waking up")
                wake_from_semi()
            else:
                self.note("nobody around — back to dozing")
                self._park(doze)
            done = True
        finally:
            self.scanning = False
            if not done and not found and life_state() == "semi":
                motion({"mode": "tucked"})
                set_body(doze)

    def snapshot(self) -> dict:
        c = cfg()
        self.last_level = self.level or self.last_level
        return {
            "status": self.status, "period": self.period, "level": self.level,
            "last_level": self.last_level, "luma": None if self.luma is None else round(self.luma, 1),
            "dark_max": c["light"].get("dark_max"), "lit_min": c["light"].get("lit_min"),
            "doze_deg": c.get("doze_deg"), "day": c["day"], "doze_in": self.doze_in,
            "event": self.event, "event_at": self.event_at or None,
            "faces": eyes["faces"] if self.scanning else 0, "history": list(self.history),
        }


# ------------------------------------------------------------------ wake word

class Ears:
    """ "Hey Peachy" → a conversation. Listens only while nothing else has the
    robot and it has been left alone for SETTLE_S, so it never hears Peachy's
    own voice or sounds; otherwise the mic is closed and nothing runs."""

    SETTLE_S = 4.0
    COOLDOWN_S = 8.0
    PATIENCE = 1                   # frames at or over the threshold in a row; the model peaks for ~1 frame

    def __init__(self) -> None:
        self.status = "off"
        self.det = None
        self.det_model = ""
        self.mic = None
        self.since = 0.0
        self.cooldown_until = 0.0
        self.over = 0
        self.score = 0.0
        self.peak = (0.0, 0.0)
        self.heard_at = 0.0
        self.voice_rel: float | None = None

    def blocked(self) -> str:
        c = cfg()
        if not c["wake"]:
            return "off"
        if console_busy():
            return "busy"
        if acting():
            return "acting"
        if watch is not None and watch.scanning:
            return "looking"
        own = owner()
        if own:
            return own
        if time.monotonic() < self.cooldown_until:
            return "cooldown"
        if not (HOME / "models" / c["wake_model"]).exists():
            return "no model"
        return ""

    def _close(self) -> None:
        if self.mic is not None:
            self.mic.close()
            self.mic = None
            log("wake: mic closed")

    def loop(self) -> None:
        import wake as ww
        while True:
            why = self.blocked()
            if why:
                self._close()
                self.since = 0.0
                self.status = "off" if why == "off" else f"paused:{why}"
                time.sleep(0.25)
                continue
            now = time.monotonic()
            if not self.since:
                self.since = now
            if now - self.since < self.SETTLE_S:
                self.status = "settling"
                time.sleep(0.25)
                continue
            model = cfg()["wake_model"]
            if self.det is None or self.det_model != model:
                self.det, self.det_model = ww.Detector(model), model
                log(f"wake: model {model}")
            if self.mic is None:
                self.det.reset()
                self.mic = ww.Mic()
                self.over = 0
                log("wake: listening")
            pcm = self.mic.read()
            if pcm is None:
                self.status = "mic error"
                self._close()
                time.sleep(2.0)
                continue
            self.status = "listening"
            self._hear(self.det.feed(pcm))

    def _hear(self, scores: list[float]) -> None:
        doa = robot["state"].get("doa") or {}
        if doa.get("speech_detected") and doa.get("angle") is not None:
            self.voice_rel = wrap(math.pi / 2 - float(doa["angle"]))
        elif self.det.hot == 0:
            self.voice_rel = None
        thr = float(cfg()["wake_threshold"])
        for s in scores:
            self.score = s
            if s > self.peak[0] or time.monotonic() - self.peak[1] > 10:
                self.peak = (s, time.monotonic())
            self.over = self.over + 1 if s >= thr else 0
            if self.over >= self.PATIENCE:
                self.over = 0
                self._trigger(s)
                return

    def _trigger(self, score: float) -> None:
        self.cooldown_until = time.monotonic() + self.COOLDOWN_S
        self.heard_at = time.time()
        rel = self.voice_rel
        self._close()
        where = "" if rel is None else f", voice at {math.degrees(rel):+.0f}°"
        note(f'heard "Hey Peachy" ({score:.2f}{where}) — waking up')
        act("wake", lambda: wake_by_voice(rel))

    def snapshot(self) -> dict:
        c = cfg()
        d = self.det
        return {
            "status": self.status, "model": c["wake_model"], "threshold": c["wake_threshold"],
            "score": round(self.score, 3), "peak": round(self.peak[0], 3),
            "heard_at": self.heard_at or None,
            "level": None if d is None else round(d.level), "floor": None if d is None else round(d.floor),
            "hot": bool(d is not None and d.hot),
        }


ears = Ears()


# ------------------------------------------------------------------ follow

def follow_block() -> str:
    """Why Follow may not run now ("" = it may)."""
    if not cfg()["follow"]:
        return "off"
    if console_busy() or acting():
        return "busy"
    own = owner()
    if own:
        return own
    if life_state() != "awake":
        return "asleep"
    return ""


def motion_block() -> str:
    """Why following may not move the robot this instant ("" = free)."""
    now = time.monotonic()
    if now - robot["state_at"] > 1.0:
        return "offline"
    if robot["state"].get("control_mode") != "enabled":
        return "motors off"
    if now < aim["yield_until"]:
        return "yielding"
    return ""


def pick(faces, w: int, h: int, prev, doa, speaking: bool):
    """Nose of the face to follow in [-1, 1] of the image (+x right, +y down):
    whoever is talking when several are in view, else the one near the last
    pick, else the largest."""
    if not faces:
        return None
    nose = [(f.nose[0] / max(w - 1, 1) * 2 - 1, f.nose[1] / max(h - 1, 1) * 2 - 1) for f in faces]
    idx = range(len(faces))
    if speaking and doa is not None and len(faces) > 1:
        want = math.pi / 2 - doa
        return nose[min(idx, key=lambda k: abs(-math.atan(nose[k][0] / FOCAL_N) - want))]
    if prev is not None:
        i = min(idx, key=lambda k: (nose[k][0] - prev[0]) ** 2 + (nose[k][1] - prev[1]) ** 2)
        if abs(nose[i][0] - prev[0]) < 0.36:
            return nose[i]
    return nose[max(idx, key=lambda k: faces[k].bbox[2] * faces[k].bbox[3])]


def engage() -> None:
    """Start from wherever the head and body are now. Caller holds _lock."""
    st = robot["state"]
    hp = st.get("head_pose") or {}
    cmd = {k: float(hp.get(k, 0.0)) for k in _AXES}
    lv = cfg()["level"] or {k: (0.0 if k == "roll" else cmd[k]) for k in _LEVEL_KEYS}
    body = float(st.get("body_yaw") or 0.0)
    aim.update(engaged=True, cmd=cmd, level={k: float(lv[k]) for k in _LEVEL_KEYS}, body=body,
               tbody=body, tyaw=cmd["yaw"], tpitch=cmd["pitch"], off_since=0.0, drift_since=0.0)
    aim["hist"].clear()


def release(why: str) -> None:
    """Caller holds _lock. The head stays where it is."""
    if aim["engaged"]:
        log(f"follow released ({why})")
    aim["engaged"] = False
    aim["cmd"] = None


def then(t: float) -> tuple[float, float]:
    """Commanded head yaw and pitch at time t. Caller holds _lock."""
    for at, yaw, pitch in reversed(aim["hist"]):
        if at <= t:
            return yaw, pitch
    return aim["cmd"]["yaw"], aim["cmd"]["pitch"]


def see(face, t0: float) -> None:
    """One face (x, y in [-1, 1]) seen in a frame grabbed at t0. Caller holds _lock."""
    if not aim["engaged"]:
        engage()
        log("face found")
    ex = -math.atan(face[0] / FOCAL_N)
    ey = math.atan(face[1] / (FOCAL_N * ASPECT))
    yaw, pitch = then(t0 - LAG_S)
    if abs(ex) > DEAD:
        aim["tyaw"] = aim["body"] + clip(yaw + GAIN * ex - aim["body"], -HEAD_MAX, HEAD_MAX)
    if abs(ey) > DEAD:
        aim["tpitch"] = clip(pitch + GAIN * ey, PITCH_MIN, PITCH_MAX)


def decide(face, t0: float) -> None:
    """Follow, after each frame."""
    now = time.monotonic()
    with _lock:
        why = motion_block()
        if why:
            release(why)
            aim["status"] = f"paused:{why}"
            return
        if face is not None:
            see(face, t0)
            aim["status"] = "tracking"
            return
        if aim["engaged"] and now - eyes["seen_at"] > RELEASE_S:
            release("nobody in view")
        if not aim["engaged"]:
            aim["status"] = "watching"


def control_loop() -> None:
    """Eases the head and body toward the aim and streams set_target."""
    period = 1.0 / CONTROL_HZ
    last = time.monotonic()
    while True:
        t0 = time.monotonic()
        dt, last = min(0.1, t0 - last), t0
        head = body = None
        with _lock:
            live = eyes["mode"] == "follow" and aim["engaged"] and aim["cmd"] is not None
            if live and t0 > aim["busy_until"]:
                hp = robot["state"].get("head_pose") or {}
                if "yaw" in hp and abs(float(hp["yaw"]) - aim["cmd"]["yaw"]) > DRIFT:
                    aim["drift_since"] = aim["drift_since"] or t0
                    if t0 - aim["drift_since"] > DRIFT_S:
                        aim["yield_until"] = t0 + YIELD_S
                        release("moved by someone else")
                        live = False
                else:
                    aim["drift_since"] = 0.0
            if live and t0 > aim["busy_until"]:
                rel = aim["tyaw"] - aim["tbody"]
                if abs(rel) > BODY_AT:
                    aim["off_since"] = aim["off_since"] or t0
                    if t0 - aim["off_since"] > BODY_S:
                        step = clip(rel * 0.7, -BODY_STEP, BODY_STEP)
                        aim["tbody"] = clip(aim["tbody"] + step, -BODY_MAX, BODY_MAX)
                        aim["off_since"] = t0
                else:
                    aim["off_since"] = 0.0
                cmd = aim["cmd"]
                k = min(1.0, dt * SMOOTH)
                cmd["yaw"] += (aim["tyaw"] - cmd["yaw"]) * k
                cmd["pitch"] += (aim["tpitch"] - cmd["pitch"]) * k
                kl = min(1.0, dt * LEVEL_RATE)
                for key in _LEVEL_KEYS:
                    cmd[key] += (aim["level"][key] - cmd[key]) * kl
                aim["body"] += clip(aim["tbody"] - aim["body"], -BODY_RATE * dt, BODY_RATE * dt)
                cmd["yaw"] = aim["body"] + clip(cmd["yaw"] - aim["body"], -HEAD_MAX, HEAD_MAX)
                aim["hist"].append((t0, cmd["yaw"], cmd["pitch"]))
                head, body = dict(cmd), aim["body"]
        if head is not None:
            try:
                daemon("/api/move/set_target", "POST",
                       {"target_head_pose": head, "target_body_yaw": body}, timeout=1.0)
            except (urllib.error.URLError, OSError, ValueError):
                pass
        time.sleep(max(0.0, period - (time.monotonic() - t0)))


# ------------------------------------------------------------------ status + HTTP

watch: Watch | None = None


def follow_status() -> dict:
    why = follow_block()
    now = time.monotonic()
    with _lock:
        on = eyes["mode"] == "follow"
        return {
            "status": aim["status"] if on else ("off" if why == "off" else f"paused:{why or 'starting'}"),
            "faces": eyes["faces"] if on else 0,
            "target": eyes["target"] if on and now - eyes["seen_at"] < 0.5 else None,
            "size": [2, 2], "hz": eyes["hz"] if on else None, "ms": eyes["ms"] if on else None,
            "engaged": aim["engaged"],
            "cmd": None if aim["cmd"] is None else {
                **{k: round(math.degrees(aim["cmd"][k]), 2) for k in ("roll", "pitch", "yaw")},
                "body": round(math.degrees(aim["body"]), 2)},
        }


_cpu = {"t": time.monotonic(), "used": time.process_time(), "pct": None}


def cpu_pct() -> float | None:
    """This service's CPU use (% of one core), averaged over at least 5 s."""
    t, used = time.monotonic(), time.process_time()
    if t - _cpu["t"] >= 5:
        _cpu.update(pct=round(100 * (used - _cpu["used"]) / (t - _cpu["t"]), 1), t=t, used=used)
    return _cpu["pct"]


def status() -> dict:
    c = cfg()
    return {
        "ts": time.time(), "state": life_state(), "owner": owner(), "semi": semi_active(),
        "busy": console_busy(), "acting": acting(), "cpu": cpu_pct(),
        "cfg": {k: c[k] for k in ("follow", "watch", "wake", "day", "doze_after_s")},
        "camera": eyes["camera"], "error": eyes["error"], "eyes": eyes["mode"],
        "follow": follow_status(), "watch": watch.snapshot() if watch else None,
        "wake": ears.snapshot(), "events": list(events),
    }


def _token() -> str:
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _reply(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/status":
            return self._reply(status())
        self._reply({"error": "not found"}, 404)

    def do_POST(self):
        tok = _token()
        if not tok or self.headers.get("X-Peachy-Senses") != tok:
            return self._reply({"error": "bad token"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._reply({"error": "bad json"}, 400)
        path = self.path.split("?")[0]
        if path == "/config":
            with _file_lock:
                d = read_json(CFG_FILE, {})
                d.update({k: v for k, v in body.items() if k in DEFAULT_CFG})
                write_json(CFG_FILE, d)
            return self._reply(status())
        if path == "/state":
            s = body.get("state")
            if s not in ("asleep", "semi", "awake"):
                return self._reply({"error": "state must be asleep, semi or awake"}, 400)
            if s != life_state():
                set_state(s)
                log(f"state from the console: {s}")
            if watch is not None:
                watch.manual(s)
            return self._reply(status())
        if path == "/busy":
            on = bool(body.get("on"))
            busy["until"] = time.monotonic() + clip(float(body.get("ttl", 120)), 1, 600) if on else 0.0
            if on:
                with _lock:
                    release("console busy")
            return self._reply(status())
        self._reply({"error": "not found"}, 404)


def watch_loop() -> None:
    while True:
        try:
            watch.step()
        except Exception as e:  # noqa: BLE001 - the routine must keep running
            log(f"watch error: {type(e).__name__}: {e}")
            time.sleep(1.0)
        time.sleep(0.2)


def ears_loop() -> None:
    while True:
        try:
            ears.loop()
        except Exception as e:  # noqa: BLE001 - a mic or model hiccup must not end the wake word
            ears.status = "error"
            ears._close()
            log(f"wake error: {type(e).__name__}: {e}")
            time.sleep(5.0)


def main() -> None:
    global watch
    import cv2
    import numpy as np
    from reachy_mini.media.camera_gstreamer import GStreamerCamera
    from reachy_mini.vision.face_detector import FaceDetector

    watch = Watch()
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=state_loop, daemon=True).start()
    threading.Thread(target=control_loop, daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    threading.Thread(target=ears_loop, daemon=True).start()
    log(f"peachy-senses on :{PORT} — state {life_state()}")

    luma_w = np.array([0.114, 0.587, 0.299], np.float32)
    det = None
    cam = None
    opened_at = used_at = 0.0
    prev = None
    times: deque = deque(maxlen=20)
    while True:
        t0 = time.monotonic()
        mode = "look" if watch.scanning else ("follow" if not follow_block() else None)
        if mode != eyes["mode"]:
            with _lock:
                if eyes["mode"] == "follow":
                    release("follow paused")
                if mode == "follow":
                    aim["status"] = "watching"
                eyes["mode"] = mode
                prev = None
        light = watch.wants_light()
        if mode is None and not light:
            if cam is not None and t0 - used_at > CAMERA_IDLE_S:
                cam.close()
                cam = None
                eyes["camera"] = "closed"
                log("camera closed")
            time.sleep(0.1)
            continue
        used_at = t0
        try:
            if cam is None:
                cam = GStreamerCamera(log_level="WARNING")
                cam.open()
                opened_at = time.monotonic()
                eyes["camera"], eyes["error"] = "open", ""
                log("camera open")
            frame = cam.read()
            if frame is None:
                time.sleep(0.03)
                continue
            if time.monotonic() - opened_at > CAMERA_WARMUP_S:
                eyes["luma"] = float((frame[::8, ::8].astype(np.float32) @ luma_w).mean())
                eyes["luma_at"] = time.monotonic()
            if mode is not None:
                if det is None:
                    det = FaceDetector()
                h, w = frame.shape[:2]
                small = cv2.resize(frame, (FACE_W, round(FACE_W * h / w / 2) * 2),
                                   interpolation=cv2.INTER_AREA)
                faces = det.detect(small)
                doa = robot["state"].get("doa") or {}
                face = pick(faces, small.shape[1], small.shape[0], prev,
                            doa.get("angle"), bool(doa.get("speech_detected")))
                prev = face
                with _lock:
                    eyes["faces"] = len(faces)
                    if face is not None:
                        eyes["seen_at"] = time.monotonic()
                        eyes["target"] = [round(face[0] + 1, 4), round(face[1] + 1, 4)]
                        eyes["hits"].append(eyes["seen_at"])
                if mode == "follow":
                    decide(face, t0)
                times.append(time.monotonic() - t0)
                eyes["ms"] = round(1000 * sum(times) / len(times))
        except Exception as e:  # noqa: BLE001 - a camera hiccup must not end the service
            eyes["error"] = f"{type(e).__name__}: {e}"[:160]
            log(f"eyes error: {eyes['error']}")
            if cam is not None:
                try:
                    cam.close()
                except Exception:  # noqa: BLE001
                    pass
            cam = None
            eyes["camera"] = "error"
            time.sleep(1.0)
            continue
        hz = FACE_HZ if mode is not None else LIGHT_HZ
        elapsed = time.monotonic() - t0
        if mode is not None:
            eyes["hz"] = round(1.0 / max(elapsed, 1.0 / hz), 1)
        time.sleep(max(0.0, 1.0 / hz - elapsed))


if __name__ == "__main__":
    main()
