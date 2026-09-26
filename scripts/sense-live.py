#!/usr/bin/env python3
"""Peachy's senses, run on this Mac: follow faces / voices, and "Hey Reachy".

Face tracking runs in the daemon when it supports it (1.11+), otherwise here
on the WebRTC video (rtcmedia.py); the mic stream feeds the wake word. Body
and voice turns are driven through the daemon REST API. Everything
yields while something else owns the robot (conversation, an app, a move, a
dashboard action) and resumes after.

  python scripts/sense-live.py --follow --dry-run   # detect only, no motion
  python scripts/sense-live.py --follow --wake      # live

--follow  track the nearest face; prefer whoever is talking (mic direction);
          turn the body toward a voice when nobody is in view
--wake    "Hey Reachy" / "Hey Peachy" starts the conversation app

Status is written to .run/sense_state.json for the dashboard.
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parent.parent
_RUN = _REPO / ".run"
_MODELS = _RUN / "models"
_STATE_FILE = _RUN / "reachy_toggle_state.json"
_CONVO_PID = _RUN / "conversation_app.pid"
_OUT = _RUN / "sense_state.json"
_YUNET = _MODELS / "face_detection_yunet_2023mar.onnx"
_YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
              "face_detection_yunet/face_detection_yunet_2023mar.onnx")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hostfind import resolve_host  # noqa: E402

HOST = resolve_host()
PORT = int(os.environ.get("REACHY_PORT", "8000"))
DASH = os.environ.get("PEACHY_DASH_URL", "")
CONVO_APP = os.environ.get("PEACHY_CONVO_APP", "reachy_mini_conversation_app")

FRAME_W, FRAME_H = 640, 360
FOCAL = 0.521 * FRAME_W
HEAD_YAW_MAX = math.radians(30)
PITCH_MIN, PITCH_MAX = math.radians(-22), math.radians(25)
BODY_MAX = math.radians(160)
BODY_STEP = math.radians(5)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _http(url: str, method: str = "GET", body: dict | None = None,
          timeout: float = 5.0, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


def daemon(path: str, method: str = "GET", body: dict | None = None, timeout: float = 5.0) -> dict:
    return _http(f"http://{HOST}:{PORT}{path}", method, body, timeout)


def _dash_url() -> str:
    if DASH:
        return DASH.rstrip("/")
    try:
        port = int((_RUN / "peachy_dashboard.port").read_text().strip())
    except (OSError, ValueError):
        port = 8080
    return f"http://127.0.0.1:{port}"


def _dash_token() -> str:
    tok = os.environ.get("PEACHY_TOKEN", "")
    if tok:
        return tok
    try:
        return (_RUN / "peachy_token").read_text().strip()
    except OSError:
        return ""


def dash(path: str, method: str = "GET", body: dict | None = None, timeout: float = 5.0) -> dict:
    return _http(_dash_url() + path, method, body, timeout, {"X-Peachy-Token": _dash_token()})


def _pid_alive(p: Path) -> bool:
    try:
        os.kill(int(p.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


class Robot:
    """Latest daemon state (websocket) plus who-owns-the-robot gate."""

    def __init__(self) -> None:
        self.state: dict = {}
        self.state_ts = 0.0
        self.gate = {"awake": False, "owner": "starting"}
        self.gate_ts = 0.0
        self._stop = threading.Event()
        threading.Thread(target=self._ws_loop, daemon=True).start()
        threading.Thread(target=self._gate_loop, daemon=True).start()

    def _ws_loop(self) -> None:
        from websockets.sync.client import connect
        url = (f"ws://{HOST}:{PORT}/api/state/ws/full?with_head_pose=true&with_body_yaw=true"
               "&with_doa=true&with_control_mode=true")
        while not self._stop.is_set():
            try:
                with connect(url, open_timeout=5) as ws:
                    while not self._stop.is_set():
                        self.state = json.loads(ws.recv(timeout=5))
                        self.state_ts = time.time()
            except Exception:  # noqa: BLE001
                time.sleep(2)

    def _local_gate(self) -> dict:
        try:
            awake = json.loads(_STATE_FILE.read_text()).get("state") == "awake"
        except (OSError, json.JSONDecodeError):
            awake = False
        owner = ""
        if _pid_alive(_CONVO_PID):
            owner = "conversation"
        try:
            st = daemon("/api/apps/current-app-status", timeout=3) or {}
            if st and st.get("state") in ("starting", "running", "stopping"):
                name = (st.get("info") or {}).get("name", "")
                owner = "conversation" if name == CONVO_APP else "app"
        except (urllib.error.URLError, OSError, ValueError):
            owner = owner or "offline"
        return {"awake": awake, "owner": owner}

    def _gate_loop(self) -> None:
        while not self._stop.is_set():
            try:
                g = dash("/api/sense/gate", timeout=3)
            except (urllib.error.URLError, OSError, ValueError):
                g = self._local_gate()
            try:
                running = daemon("/api/move/running", timeout=3)
                if running and not g.get("owner"):
                    g["owner"] = "move"
            except (urllib.error.URLError, OSError, ValueError):
                g["owner"] = g.get("owner") or "offline"
            self.gate = g
            self.gate_ts = time.time()
            time.sleep(1.0)

    def fresh(self) -> bool:
        return time.time() - self.state_ts < 2.0

    def doa(self) -> tuple[float | None, bool]:
        d = self.state.get("doa") or {}
        return d.get("angle"), bool(d.get("speech_detected"))

    def stop(self) -> None:
        self._stop.set()


class Media:
    """Opens the WebRTC stream only while some sense needs it."""

    def __init__(self) -> None:
        self.m = None
        self.state = "off"
        self.last_need = 0.0
        self.retry_at = 0.0
        self.error = ""

    def get(self, need: bool):
        now = time.time()
        if need:
            self.last_need = now
        if self.m is not None:
            if self.m.error:
                self.error = self.m.error
                log(f"media error: {self.error}")
                self.close()
                self.retry_at = now + 5
                return None
            if not need and now - self.last_need > 20:
                self.close()
            return self.m
        if need and now >= self.retry_at:
            try:
                from rtcmedia import RtcMedia
                self.state = "connecting"
                self.m = RtcMedia(HOST, FRAME_W, FRAME_H, fps=15).start()
                self.state = "on"
                self.error = ""
                log("media on")
            except Exception as e:  # noqa: BLE001
                self.error = str(e)[:160]
                self.state = "error"
                self.retry_at = now + 8
                log(f"media unavailable: {self.error}")
        return self.m

    def close(self) -> None:
        if self.m is not None:
            try:
                self.m.stop()
            except Exception:  # noqa: BLE001
                pass
            log("media off")
        self.m = None
        self.state = "off"


class Follower:
    needs_media = True

    def close(self) -> None:
        pass

    def __init__(self, robot: Robot, dry: bool) -> None:
        import cv2
        if not _YUNET.is_file():
            _MODELS.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(_YUNET_URL, _YUNET)
        self.cv2 = cv2
        self.det = cv2.FaceDetectorYN.create(str(_YUNET), "", (FRAME_W, FRAME_H), 0.7, 0.3, 20)
        self.robot = robot
        self.dry = dry
        self.status = "off"
        self.faces: list[dict] = []
        self.target: tuple[float, float] | None = None
        self.cmd: dict | None = None
        self.body: float | None = None
        self.base: dict | None = None
        self.last_face = 0.0
        self.engaged = False
        self.yield_until = 0.0
        self.drift_since = 0.0
        self.speech_since = 0.0
        self.voice_turn_at = 0.0
        self.off_center_since = 0.0

    def allowed(self) -> str:
        g = self.robot.gate
        if time.time() - self.robot.gate_ts > 5:
            return "starting"
        if g.get("owner"):
            return g["owner"]
        if not g.get("awake"):
            return "asleep"
        if not self.robot.fresh():
            return "offline"
        if self.robot.state.get("control_mode") != "enabled":
            return "motors off"
        if time.time() < self.yield_until:
            return "yielding"
        return ""

    def _detect(self, frame) -> list[dict]:
        _, res = self.det.detect(frame)
        out = []
        for f in (res if res is not None else []):
            x, y, w, h = [float(v) for v in f[:4]]
            out.append({"u": x + w / 2, "v": y + h * 0.45, "w": w, "score": float(f[14])})
        return out

    def _pick(self, faces: list[dict], doa: float | None, speaking: bool) -> dict | None:
        if not faces:
            return None
        if speaking and doa is not None and len(faces) > 1:
            want = math.pi / 2 - doa
            return min(faces, key=lambda f: abs(math.atan((FRAME_W / 2 - f["u"]) / FOCAL) - want))
        if self.target is not None:
            tu, tv = self.target
            near = min(faces, key=lambda f: (f["u"] - tu) ** 2 + (f["v"] - tv) ** 2)
            if abs(near["u"] - tu) < FRAME_W * 0.18:
                return near
        return max(faces, key=lambda f: f["w"])

    def _send(self, head: dict, body: float) -> None:
        self.cmd, self.body = head, body
        if self.dry:
            return
        daemon("/api/move/set_target", "POST",
               {"target_head_pose": head, "target_body_yaw": body}, timeout=2)

    def _engage(self) -> None:
        hp = self.robot.state.get("head_pose") or {}
        self.base = {k: float(hp.get(k, 0.0)) for k in ("x", "y", "z", "roll", "pitch", "yaw")}
        self.cmd = dict(self.base)
        self.body = float(self.robot.state.get("body_yaw") or 0.0)
        self.engaged = True

    def _release(self, why: str) -> None:
        if self.engaged:
            log(f"follow released ({why})")
        self.engaged = False
        self.target = None
        self.cmd = None
        self.drift_since = 0.0

    def step(self, frame) -> None:
        why = self.allowed()
        doa, speaking = self.robot.doa()
        now = time.time()
        if frame is not None:
            self.faces = self._detect(frame)
        else:
            self.faces = []
        if why:
            self.status = f"paused:{why}"
            self._release(why)
            return
        if frame is None:
            self.status = "waiting"
            return

        if self.engaged and not self.dry and self.cmd is not None:
            hp = self.robot.state.get("head_pose") or {}
            drift = abs(float(hp.get("yaw", 0.0)) - self.cmd["yaw"])
            if drift > math.radians(14):
                self.drift_since = self.drift_since or now
                if now - self.drift_since > 0.8:
                    self.yield_until = now + 4
                    self._release("moved by someone else")
                    return
            else:
                self.drift_since = 0.0

        face = self._pick(self.faces, doa, speaking)
        self.speech_since = (self.speech_since or now) if speaking else 0.0

        if face is None:
            self.target = None
            if (speaking and doa is not None and now - self.speech_since > 0.6
                    and now - self.voice_turn_at > 3.0):
                rel = math.pi / 2 - doa
                if abs(rel) > math.radians(25):
                    if not self.engaged:
                        self._engage()
                    self.voice_turn_at = now
                    body = max(-BODY_MAX, min(BODY_MAX, self.body + rel))
                    head = dict(self.cmd, yaw=body)
                    log(f"voice at {math.degrees(rel):+.0f}° → turning")
                    self.status = "voice"
                    if not self.dry:
                        daemon("/api/move/goto", "POST", {
                            "head_pose": head, "body_yaw": body, "duration": 1.2,
                            "interpolation": "minjerk"}, timeout=3)
                    self.cmd, self.body = head, body
                    return
            if self.engaged and now - self.last_face > 6 and now - self.voice_turn_at > 6:
                self._release("nobody in view")
            self.status = "watching" if not self.engaged else self.status
            return

        if not self.engaged:
            self._engage()
            log(f"face found ({len(self.faces)} in view)")
        self.last_face = now
        self.target = (face["u"], face["v"])
        self.status = "tracking"

        ex = math.atan((FRAME_W / 2 - face["u"]) / FOCAL)
        ey = math.atan((face["v"] - FRAME_H / 2) / FOCAL)
        if abs(ex) < math.radians(2):
            ex = 0.0
        if abs(ey) < math.radians(2):
            ey = 0.0
        hp = self.robot.state.get("head_pose") or self.cmd
        want_yaw = float(hp.get("yaw", self.cmd["yaw"])) + ex
        want_pitch = float(hp.get("pitch", self.cmd["pitch"])) + ey
        a = 0.35
        yaw = self.cmd["yaw"] + a * (want_yaw - self.cmd["yaw"])
        pitch = self.cmd["pitch"] + a * (want_pitch - self.cmd["pitch"])
        pitch = max(PITCH_MIN, min(PITCH_MAX, pitch))

        body = self.body
        rel = yaw - body
        if abs(rel) > math.radians(18):
            self.off_center_since = self.off_center_since or now
            if now - self.off_center_since > 0.8:
                body += max(-BODY_STEP, min(BODY_STEP, rel * 0.5))
                body = max(-BODY_MAX, min(BODY_MAX, body))
        else:
            self.off_center_since = 0.0
        yaw = body + max(-HEAD_YAW_MAX, min(HEAD_YAW_MAX, yaw - body))
        self._send(dict(self.cmd, yaw=yaw, pitch=pitch), body)


class DaemonFollower:
    """Head tracking runs in the daemon (1.11+); this adds body follow and voice turns."""

    needs_media = False

    def __init__(self, robot: Robot, dry: bool) -> None:
        self.robot = robot
        self.dry = dry
        self.status = "off"
        self.faces: list[dict] = []
        self.target: tuple[float, float] | None = None
        self.tracking = False
        self.body: float | None = None
        self.last_face = 0.0
        self.speech_since = 0.0
        self.voice_turn_at = 0.0
        self.off_center_since = 0.0
        self.yield_until = 0.0

    allowed = Follower.allowed

    def _track(self, on: bool) -> None:
        if on == self.tracking:
            return
        if on:
            daemon("/api/media/tracking/enable", "POST",
                   {"weight": 0.0 if self.dry else 1.0}, timeout=3)
            self.body = float(self.robot.state.get("body_yaw") or 0.0)
            log("daemon tracking on" + (" (weight 0)" if self.dry else ""))
        else:
            daemon("/api/media/tracking/disable", "POST", timeout=3)
            log("daemon tracking off")
        self.tracking = on

    def close(self) -> None:
        try:
            self._track(False)
        except (urllib.error.URLError, OSError):
            pass

    def step(self, _frame=None) -> None:
        why = self.allowed()
        why = "" if why == "yielding" else why
        now = time.time()
        if why:
            self.status = f"paused:{why}"
            self.faces, self.target = [], None
            if why not in ("starting", "offline"):
                self._track(False)
            return
        self._track(True)

        f = (daemon("/api/media/tracking/face", timeout=2) or {}).get("face_target") or {}
        doa, speaking = self.robot.doa()
        self.speech_since = (self.speech_since or now) if speaking else 0.0
        hp = self.robot.state.get("head_pose") or {}
        body_now = float(self.robot.state.get("body_yaw") or 0.0)
        if self.body is None:
            self.body = body_now

        if f.get("detected") and f.get("x") is not None:
            x, y = float(f["x"]), float(f.get("y") or 0.0)
            self.faces = [{"x": x, "y": y}]
            self.target = ((x + 1) / 2 * FRAME_W, (y + 1) / 2 * FRAME_H)
            if now - self.last_face > 6:
                log("face found")
            self.last_face = now
            self.status = "tracking"
            rel = float(hp.get("yaw", 0.0)) - body_now
            if abs(rel) > math.radians(18):
                self.off_center_since = self.off_center_since or now
                if now - self.off_center_since > 0.8:
                    self.body = max(-BODY_MAX, min(BODY_MAX,
                                    body_now + max(-3 * BODY_STEP, min(3 * BODY_STEP, rel * 0.7))))
                    if not self.dry:
                        daemon("/api/move/set_target", "POST",
                               {"target_body_yaw": self.body}, timeout=2)
            else:
                self.off_center_since = 0.0
            return

        self.faces, self.target = [], None
        self.off_center_since = 0.0
        if (speaking and doa is not None and now - self.speech_since > 0.6
                and now - self.voice_turn_at > 3.0 and now - self.last_face > 1.5):
            rel = math.pi / 2 - doa
            if abs(rel) > math.radians(25):
                self.voice_turn_at = now
                self.body = max(-BODY_MAX, min(BODY_MAX, body_now + rel))
                log(f"voice at {math.degrees(rel):+.0f}° → turning")
                self.status = "voice"
                if not self.dry:
                    head = {k: float(hp.get(k, 0.0)) for k in ("x", "y", "z", "roll", "pitch")}
                    head["yaw"] = self.body
                    daemon("/api/move/goto", "POST", {
                        "head_pose": head, "body_yaw": self.body, "duration": 1.2,
                        "interpolation": "minjerk"}, timeout=3)
                return
        if now - self.voice_turn_at > 2:
            self.status = "watching"


def daemon_tracking_available() -> bool:
    try:
        return "face_target" in daemon("/api/media/tracking/face", timeout=3)
    except (urllib.error.URLError, OSError, ValueError):
        return False


_GREET = {"hey", "hi", "hello", "ok", "okay", "yo"}
_NAMES = ["reachy", "peachy", "reachie", "richie", "ritchie", "peachie", "reechy"]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower())


def heard_wake(text: str) -> bool:
    """A greeting followed by Peachy's name, at the start of the utterance."""
    words = _words(text)[:4]
    for i, w in enumerate(words[:-1]):
        if w in _GREET:
            nxt = words[i + 1]
            if any(difflib.SequenceMatcher(None, nxt, n).ratio() >= 0.72 for n in _NAMES):
                return True
    return False


def starts_with_greeting(text: str) -> bool:
    return any(w in _GREET for w in _words(text)[:2])


class WakeWord:
    """ "Hey Peachy" → start a conversation.

    Listens only while the robot is idle and has been quiet for SETTLE_S: any
    gate owner (dashboard action, motion, robot speaker, conversation, app)
    pauses it and drops the buffered audio, so Peachy never hears itself.
    A hit from the name-biased pass must be confirmed by an unbiased pass that
    also starts with a greeting — Whisper echoes its prompt on unclear audio.
    """

    RATE = 16000
    SETTLE_S = 4.0
    PROMPT = "Hey Reachy. Hey Peachy."

    def __init__(self, robot: Robot, dry: bool) -> None:
        os.environ.setdefault("HF_HUB_CACHE", str(_MODELS / "hf"))
        from faster_whisper import WhisperModel
        self.asr = WhisperModel(os.environ.get("PEACHY_WAKE_MODEL", "base.en"),
                                device="cpu", compute_type="int8")
        self.robot = robot
        self.dry = dry
        self.status = "off"
        self.ring = np.zeros(int(self.RATE * 2.6), dtype=np.int16)
        self.floor = 300.0
        self.voice_until = 0.0
        self.next_asr = 0.0
        self.last_text = ""
        self.heard_at = 0.0
        self.cooldown_until = 0.0
        self.quiet_since = 0.0
        self.level = 0.0

    def allowed(self) -> str:
        now = time.time()
        g = self.robot.gate
        why = ""
        if now - self.robot.gate_ts > 5:
            why = "starting"
        elif g.get("owner"):
            why = g["owner"]
        elif g.get("speaking"):
            why = "speaker"
        elif now < self.cooldown_until:
            why = "cooldown"
        if why:
            self.quiet_since = 0.0
            return why
        if not self.quiet_since:
            self.quiet_since = now
        if now - self.quiet_since < self.SETTLE_S:
            return "settling"
        return ""

    def feed(self, pcm: np.ndarray) -> None:
        n = pcm.size
        self.ring = np.concatenate([self.ring[n:], pcm])
        rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if n else 0.0
        self.level = rms
        now = time.time()
        if rms > max(3.0 * self.floor, 250.0):
            self.voice_until = now + 0.9
        else:
            self.floor = 0.97 * self.floor + 0.03 * max(rms, 50.0)
        why = self.allowed()
        if why:
            self.status = f"paused:{why}"
            self.ring[:] = 0
            self.voice_until = 0.0
            return
        self.status = "listening"
        if now < self.voice_until and now >= self.next_asr:
            self.next_asr = now + 0.7
            self._check()

    def _transcribe(self, audio: np.ndarray, prompt: str | None) -> str:
        segs, _ = self.asr.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                      without_timestamps=True, condition_on_previous_text=False,
                                      initial_prompt=prompt)
        return " ".join(s.text for s in segs if s.no_speech_prob < 0.6).strip()

    def _check(self) -> None:
        audio = self.ring.astype(np.float32) / 32768.0
        text = self._transcribe(audio, self.PROMPT)
        if not text:
            return
        self.last_text = text
        if not heard_wake(text):
            return
        plain = self._transcribe(audio, None)
        if not starts_with_greeting(plain):
            log(f'ignored "{text}" (unbiased: "{plain}")')
            return
        if self.allowed():
            return
        self.heard_at = time.time()
        self.cooldown_until = time.time() + 8
        self.ring[:] = 0
        log(f'wake word: "{text}" / "{plain}"')
        self._trigger()

    def _trigger(self) -> None:
        if self.dry:
            log("(dry run — would start the conversation)")
            return
        try:
            r = dash("/api/converse/start", "POST", timeout=150)
            log(f"conversation: {r.get('msg', r)}")
        except urllib.error.HTTPError as e:
            log(f"dashboard declined ({e.code}); not waking")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"dashboard unreachable ({e}); starting directly")
            subprocess.run([str(_REPO / "scripts" / "ctl-toggle.py"), "wake"], cwd=_REPO, timeout=90)
            subprocess.run([str(_REPO / "scripts" / "app-conversation.sh"), "start"], cwd=_REPO, timeout=90)


def main() -> int:
    ap = argparse.ArgumentParser(description="Peachy senses: follow faces/voices, wake word")
    ap.add_argument("--follow", action="store_true", help="track faces and turn to voices")
    ap.add_argument("--wake", action="store_true", help='"Hey Reachy" starts a conversation')
    ap.add_argument("--dry-run", action="store_true", help="detect only; no motion, no conversation")
    ap.add_argument("--rate", type=float, default=10.0, help="follow control rate (Hz)")
    ap.add_argument("--engine", choices=("auto", "daemon", "laptop"), default="auto",
                    help="face tracking on the robot (daemon 1.11+) or here from the video")
    args = ap.parse_args()
    if not (args.follow or args.wake):
        ap.error("pick --follow and/or --wake")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    robot = Robot()
    media = Media()
    follower = None
    engine = ""
    if args.follow:
        engine = args.engine
        if engine == "auto":
            engine = "daemon" if daemon_tracking_available() else "laptop"
        follower = (DaemonFollower if engine == "daemon" else Follower)(robot, args.dry_run)
    wake = WakeWord(robot, args.dry_run) if args.wake else None
    log(f"sense-live on {HOST} — follow={engine or 'off'} wake={bool(wake)}"
        f"{' (dry run)' if args.dry_run else ''}")

    if wake is not None:
        def audio_loop() -> None:
            while not stop.is_set():
                m = media.m
                if m is None:
                    time.sleep(0.3)
                    continue
                pcm = m.audio(4000, timeout=1.5)
                if pcm is not None:
                    try:
                        wake.feed(pcm)
                    except Exception as e:  # noqa: BLE001
                        log(f"wake error: {e}")
                        time.sleep(1)
        threading.Thread(target=audio_loop, daemon=True).start()

    period = 1.0 / max(1.0, args.rate)
    last_write = 0.0
    while not stop.is_set():
        t0 = time.time()
        need_follow = follower is not None and follower.allowed() in ("", "yielding")
        need_wake = wake is not None and wake.allowed() in ("", "cooldown")
        m = media.get((need_follow and follower.needs_media) or need_wake)
        if follower is not None:
            try:
                follower.step(m.frame() if (m is not None and need_follow) else None)
            except (urllib.error.URLError, OSError) as e:
                follower.status = "error"
                log(f"follow error: {e}")
                time.sleep(1)
        if wake is not None and (m is None or not need_wake):
            wake.status = f"paused:{wake.allowed() or 'no audio'}"

        if t0 - last_write > 0.5:
            last_write = t0
            angle, speaking = robot.doa()
            out = {
                "pid": os.getpid(), "ts": t0, "dry_run": args.dry_run, "host": HOST,
                "media": media.state, "media_error": media.error,
                "doa": {"angle": angle, "speech": speaking},
                "follow": None if follower is None else {
                    "engine": engine,
                    "status": follower.status, "faces": len(follower.faces),
                    "target": follower.target, "size": [FRAME_W, FRAME_H]},
                "wake": None if wake is None else {
                    "status": wake.status, "heard_at": wake.heard_at or None,
                    "last_text": wake.last_text[-120:], "level": round(wake.level)},
            }
            try:
                tmp = _OUT.with_suffix(".tmp")
                tmp.write_text(json.dumps(out))
                tmp.replace(_OUT)
            except OSError:
                pass
        stop.wait(max(0.0, period - (time.time() - t0)))

    if follower is not None:
        follower.close()
    media.close()
    robot.stop()
    _OUT.unlink(missing_ok=True)
    log("sense-live stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
