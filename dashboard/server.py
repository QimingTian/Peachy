#!/usr/bin/env python3
"""Peachy control server — serves the phone dashboard on the LAN.

Runs on the laptop (later: on the robot as the Peachy app's web UI). Phones on
the same Wi-Fi open http://<laptop-ip>:8080/ and tap big buttons; the server
relays to the daemon by invoking the existing repo scripts (single source of
truth — no duplicated motion logic).

  ./dashboard/run.sh                 # prints the LAN URL
  python dashboard/server.py --port 8080
Env: REACHY_HOST (auto via hostfind), REACHY_PORT (8000)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

import secrets

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
import uvicorn

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"
_STATIC = Path(__file__).resolve().parent / "static"
_RUN = _REPO / ".run"
_RUN.mkdir(exist_ok=True)


def _load_reachy_env() -> None:
    """Match dashboard/run.sh — subprocess scripts read REACHY_HOST from .run/reachy.env."""
    envfile = _RUN / "reachy.env"
    if not envfile.is_file():
        return
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_reachy_env()

sys.path.insert(0, str(_SCRIPTS))
from hostfind import resolve_host  # noqa: E402
import heading  # noqa: E402

HOST = os.environ.get("REACHY_HOST") or resolve_host()
PORT = int(os.environ.get("REACHY_PORT", "8000"))

_CONVO_SH = _SCRIPTS / "app-conversation.sh"
_STATE_FILE = _RUN / "reachy_toggle_state.json"
_VOLUME_FILE = _RUN / "speaker_volume.json"
_DEFAULT_VOLUME = 100
_DEFAULT_CONVO_VOICE = "ballad"
_volume_boot_done = False

_HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")
_LIM_PR = math.radians(38.0)

# --- access token (REQUIRED before exposing via Tailscale Funnel) -----------
# PEACHY_TOKEN env wins; else a stable secret in .run/peachy_token (auto-made).
# Set PEACHY_TOKEN=off to disable auth (trusted LAN / tunnel only).
_TOKEN_FILE = _RUN / "peachy_token"
_TOKEN = os.environ.get("PEACHY_TOKEN", "").strip()
if not _TOKEN:
    if _TOKEN_FILE.exists():
        _TOKEN = _TOKEN_FILE.read_text().strip()
    else:
        _TOKEN = secrets.token_urlsafe(18)
        _TOKEN_FILE.write_text(_TOKEN)
_AUTH_ON = _TOKEN.lower() != "off"

app = FastAPI(title="Peachy Control")
_robot_lock = threading.Lock()       # one motion action at a time
_last: dict = {"action": None, "ok": None, "msg": "", "at": 0.0}
_LOG: deque = deque(maxlen=200)      # ring buffer for verbose analysis


def _logrec(action: str, ok, msg: str = "") -> None:
    _LOG.append({
        "t": time.strftime("%H:%M:%S"),
        "action": action,
        "ok": ok,
        "msg": (msg or "")[-400:],
    })


@app.middleware("http")
async def _gate(request: Request, call_next):
    """When auth is on (Funnel/public), require the token via ?k=, the
    X-Peachy-Token header, or the peachy_token cookie. One valid ?k= visit
    drops a cookie so the SPA's fetches are authorized thereafter."""
    if not _AUTH_ON:
        return await call_next(request)
    if request.url.path in ("/api/qr", "/api/qr.png"):
        return await call_next(request)
    q = request.query_params.get("k")
    hdr = request.headers.get("x-peachy-token")
    cookie = request.cookies.get("peachy_token")
    if q and secrets.compare_digest(q, _TOKEN):
        # clean the URL and persist via cookie
        if request.method == "GET" and request.url.path == "/":
            r = RedirectResponse("/", status_code=303)
        else:
            r = await call_next(request)
        r.set_cookie("peachy_token", _TOKEN, max_age=60 * 60 * 24 * 90,
                     httponly=True, samesite="lax")
        return r
    if (hdr and secrets.compare_digest(hdr, _TOKEN)) or \
       (cookie and secrets.compare_digest(cookie, _TOKEN)):
        return await call_next(request)
    return PlainTextResponse(
        "Peachy: access token required. Open the link with ?k=<token>.",
        status_code=401)


def _script_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("REACHY_HOST", HOST)
    env.setdefault("REACHY_PORT", str(PORT))
    return env


def _run_script(args: list[str], timeout: float = 140.0) -> tuple[bool, str]:
    try:
        p = subprocess.run([sys.executable, *args], cwd=_REPO, env=_script_env(),
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        return p.returncode == 0, out[-600:]
    except subprocess.TimeoutExpired:
        return False, "timed out (robot slow or unreachable)"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# Read-only daemon queries that every console tab and sense-live poll. Shared so
# the robot sees at most one request per path per second however many clients
# are open; concurrent callers wait for the one request in flight. Failures are
# cached too, so an offline robot doesn't queue up timeouts.
_SHARED_TTL = 1.0
_SHARED_PATHS = ("/api/state/full", "/api/apps/current-app-status",
                 "/api/state/present_body_yaw", "/api/move/running")
_shared: dict[str, tuple[float, object, BaseException | None]] = {}
_shared_lock = threading.Lock()
_shared_flight = {p: threading.Lock() for p in _SHARED_PATHS}


def _daemon_get(path: str, timeout: float = 4.0):
    def hit():
        c = _shared.get(path)
        return c if c and time.monotonic() - c[0] < _SHARED_TTL else None

    with _shared_flight[path]:
        c = hit()
        if c is None:
            try:
                c = (time.monotonic(), _daemon_json(path, timeout=timeout), None)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                    ValueError) as e:
                c = (time.monotonic(), None, e)
            with _shared_lock:
                _shared[path] = c
    if c[2] is not None:
        raise c[2]
    return c[1]


def _shared_forget(*paths: str) -> None:
    with _shared_lock:
        for p in paths or tuple(_shared):
            _shared.pop(p, None)


def _daemon_up() -> bool:
    try:
        _daemon_get("/api/state/full", timeout=3)
        return True
    except Exception:  # noqa: BLE001
        return False


def _daemon_json(path: str, method: str = "GET", body: dict | None = None,
                 timeout: float = 12.0) -> dict:
    if method != "GET":
        _shared_forget()
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _head_offset() -> dict[str, float]:
    sys.path.insert(0, str(_SCRIPTS))
    from head_pose import resolve_home

    resolved = resolve_home()
    if resolved:
        return resolved[0]
    off = _load_state().get("head_offset", {})
    return {k: float(off.get(k, 0.0)) for k in _HEAD_AXES}


def _clamp_head(p: dict) -> dict:
    out = {k: float(p.get(k, 0.0)) for k in _HEAD_AXES}
    out["pitch"] = max(-_LIM_PR, min(_LIM_PR, out["pitch"]))
    out["roll"] = max(-_LIM_PR, min(_LIM_PR, out["roll"]))
    return out


def _enable_motors() -> None:
    _daemon_json("/api/motors/set_mode/enabled", "POST", {})


def _head_goto(pose: dict, dur: float = 0.45) -> None:
    _enable_motors()
    body = {"head_pose": _clamp_head(pose), "duration": dur, "interpolation": "minjerk",
            "antennas": [0.0, 0.0]}
    _daemon_json("/api/move/goto", "POST", body, timeout=max(12.0, dur + 8))


def _settle_head_home(dur: float = 1.0) -> bool:
    """Goto saved head home before a snap so the camera view matches Head tune."""
    sys.path.insert(0, str(_SCRIPTS))
    from head_pose import settle_home

    def _http(_h, _p, path, method, timeout=30.0, body=None):
        return _daemon_json(path, method, body, timeout=timeout)

    if settle_home(_http, HOST, PORT, dur=dur):
        time.sleep(0.35)  # brief hold so the head stops moving before capture
        return True
    return False


def _jpeg_dark(path: Path) -> bool:
    """True when the frame is near-uniform black (head down / lights off)."""
    try:
        from PIL import Image
        import statistics

        px = list(Image.open(path).convert("L").getdata())
        if not px:
            return False
        step = max(1, len(px) // 4000)
        sample = px[::step]
        m = statistics.mean(sample)
        s = statistics.pstdev(sample) if len(sample) > 1 else 0.0
        return m < 30 and s < 10
    except Exception:
        return False


def _annotate_frame(payload: dict, path: Path) -> dict:
    if _jpeg_dark(path):
        payload["dark"] = True
        hint = "very dark — raise head pitch or turn on lights"
        payload["msg"] = f"{payload.get('msg', '').strip(' · ')} · {hint}".strip(" · ")
    return payload


def _fetch_robot_jpeg(out: Path, quality: int = 85) -> bool:
    """One camera frame over its own short-lived WebRTC stream."""
    sys.path.insert(0, str(_SCRIPTS))
    import cv2
    from rtcmedia import RtcMedia

    try:
        m = RtcMedia(HOST, 1280, 720, fps=10, audio=False).start()
    except Exception:  # noqa: BLE001
        return False
    try:
        end = time.time() + 12
        frame = None
        while frame is None and time.time() < end and not m.error:
            time.sleep(0.1)
            frame = m.frame(max_age=2.0)
        if frame is None:
            return False
        time.sleep(1.0)  # auto-exposure settles on the first frames
        frame = m.frame(max_age=2.0)
        if frame is None:
            return False
    finally:
        m.stop()
    return bool(cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, quality]))


def _fetch_snap_frame(out: Path) -> dict:
    """Latest robot camera frame (WebRTC) written to *out*. No _robot_lock."""
    t0 = time.time()
    if not _fetch_robot_jpeg(out):
        return {"ok": False, "msg": "no camera frame (WebRTC stream unavailable)"}
    ms = int((time.time() - t0) * 1000)
    payload = {"ok": True, "msg": f"({ms} ms) {out.stat().st_size // 1024}KB",
               "pull_ms": ms, "url": f"/static/{out.name}?t={int(time.time() * 1000)}"}
    return _annotate_frame(payload, out)


def _release_robot_control() -> str:
    """Stop conversation / in-flight moves so wake-sleep owns the head."""
    notes: list[str] = []
    if _conversation_running():
        _convo_sh("stop", 25)
        notes.append("conversation stopped")
        if _hold_mode() != "free":
            _motion({"mode": "free", "body_yaw": heading.enc_rad(0.0), "head_pitch": 0.0})
        time.sleep(1.8)
    try:
        _daemon_json("/api/move/stop", "POST", {}, timeout=6.0)
        notes.append("move halted")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass
    try:
        _daemon_json("/api/motors/set_mode/enabled", "POST", {}, timeout=8.0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass
    time.sleep(0.25)
    sys.path.insert(0, str(_SCRIPTS))
    from motion_ready import ensure_motion_ready

    revived = ensure_motion_ready(HOST, PORT)
    if revived:
        notes.append(revived)
    return " · ".join(notes)


def _stop_services(*, sleep: bool = False) -> tuple[bool, str]:
    """Stop conversation, other apps and in-flight moves."""
    msgs: list[str] = []
    try:
        _daemon_json("/api/move/stop", "POST", {}, timeout=6.0)
        msgs.append("move halted")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass
    if _conversation_running():
        _convo_sh("stop", 25)
        msgs.append("conversation stopped")
    else:
        _convo_sh("stop", 25)
    cur = _current_app()
    if cur and cur.get("name") != _CONVO_APP_NAME and cur.get("state") in ("starting", "running"):
        try:
            _daemon_json("/api/apps/stop-current-app", "POST", timeout=30)
            msgs.append(f"{cur.get('name')} stopped")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            pass
    if sleep:
        released = _release_robot_control()
        ok, smsg = _run_script([str(_SCRIPTS / "ctl-toggle.py"), "sleep"], 120)
        if released:
            smsg = f"{released} · {smsg}" if smsg else released
        msgs.append("asleep" if ok else (smsg or "sleep failed"))
    note = " · ".join(msgs) or "nothing running — Peachy idle"
    return True, note


def _toggle_state() -> str:
    try:
        return json.loads(_STATE_FILE.read_text()).get("state", "unknown")
    except (FileNotFoundError, json.JSONDecodeError):
        return "unknown"


def _lan_ip() -> str:
    """Best-guess LAN IP via routing table (no packets sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "localhost"
    finally:
        s.close()


def _conversation_running() -> bool:
    cur = _current_app()
    return bool(cur) and cur.get("name") == _CONVO_APP_NAME and \
        cur.get("state") in ("starting", "running")


def _convo_sh(cmd: str, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(_CONVO_SH), cmd], cwd=_REPO, env=_script_env(),
                              capture_output=True, text=True, timeout=timeout)
    finally:
        _shared_forget("/api/apps/current-app-status")


def _conversation_ui_url() -> str | None:
    return f"http://{HOST}:7860/" if _conversation_running() else None


def _convo_base_url() -> str:
    return os.environ.get("PEACHY_CONVERSATION_GRADIO_URL", "http://127.0.0.1:7860").rstrip("/")


_CONVO_RPC_METHODS = {
    ("GET", "/voices"): "voices.list",
    ("GET", "/voices/current"): "voices.current",
    ("POST", "/voices/apply"): "voices.apply",
    ("GET", "/personalities"): "personalities.list",
    ("POST", "/personalities/apply"): "personalities.apply",
}


def _convo_rpc_url() -> str:
    base = os.environ.get("PEACHY_CONVERSATION_GRADIO_URL", "").rstrip("/")
    if not base:
        base = f"http://{HOST}:7860"
    return base.replace("https://", "wss://").replace("http://", "ws://") + "/rpc"


def _convo_rpc(method: str, params: dict | None = None, timeout: float = 10.0) -> dict | list:
    from websockets.sync.client import connect
    with connect(_convo_rpc_url(), open_timeout=min(timeout, 5.0)) as ws:
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while True:
            msg = json.loads(ws.recv(timeout=max(0.1, end - time.time())))
            if msg.get("id") != 1:
                continue
            if "error" in msg:
                err = msg["error"] or {}
                return {"ok": False,
                        "error": (err.get("data") or {}).get("reason") or err.get("message", "rpc error")}
            return msg.get("result")


def _convo_api_json(path: str, *, method: str = "GET", params: dict | None = None,
                    timeout: float = 10.0) -> dict | list:
    rpc = _CONVO_RPC_METHODS.get((method, path))
    if rpc:
        p = dict(params or {})
        if "persist" in p:
            p["persist"] = str(p["persist"]).lower() == "true"
        try:
            return _convo_rpc(rpc, p, timeout)
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001
            pass
    q = urllib.parse.urlencode(params or {})
    url = f"{_convo_base_url()}{path}"
    if q:
        url = f"{url}{'&' if '?' in url else '?'}{q}"
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def _apply_convo_voice(voice: str = _DEFAULT_CONVO_VOICE) -> tuple[bool, str]:
    """Set conversation voice after start — API may need a few seconds to come up."""
    last_err = ""
    for attempt in range(10):
        if attempt:
            time.sleep(1.5)
        try:
            voices = _convo_api_json("/voices", timeout=5.0)
            if isinstance(voices, list) and voices and voice not in voices:
                return True, "robot default voice"
            data = _convo_api_json(
                "/voices/apply", method="POST", params={"voice": voice}, timeout=12.0,
            )
            if isinstance(data, dict) and data.get("ok") is False:
                last_err = str(data.get("error") or data.get("status") or "failed")
                continue
            status = data.get("status", voice) if isinstance(data, dict) else voice
            return True, str(status)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                json.JSONDecodeError) as e:
            last_err = str(e)[:200]
    return False, last_err or "voice apply timed out"


_QR_PNG = _RUN / "peachy_qr.png"
_DASH_PORT_FILE = _RUN / "peachy_dashboard.port"


def _dashboard_port() -> int:
    try:
        return int(_DASH_PORT_FILE.read_text().strip())
    except (OSError, ValueError):
        return int(os.environ.get("PEACHY_PORT", "8080"))


def _refresh_qr() -> dict:
    """Regenerate .run/peachy_qr.png; return {ok, url, source, msg}."""
    try:
        p = subprocess.run(
            [sys.executable, str(_SCRIPTS / "tool-qr.py"), "--from-run", "--write-json"],
            capture_output=True, text=True, timeout=15, cwd=_REPO,
        )
        if p.returncode == 0 and p.stdout.strip():
            return json.loads(p.stdout.strip())
        msg = (p.stdout + p.stderr).strip()[-200:] or "QR generation failed"
        return {"ok": False, "msg": msg}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return {"ok": False, "msg": str(e)}


@app.get("/api/qr")
def qr_info() -> JSONResponse:
    """Dashboard URL + source for the scan-to-open QR (same as terminal QR)."""
    data = _refresh_qr() if not _QR_PNG.exists() else {}
    if not data.get("url"):
        try:
            url = (_RUN / "peachy_qr_url").read_text().strip()
            if url:
                data = {"ok": True, "url": url, "source": "pinned"}
        except OSError:
            pass
    if not data.get("url"):
        data = _refresh_qr()
    data.setdefault("ok", bool(data.get("url")))
    data["auth"] = _AUTH_ON
    data["one_driver"] = True
    return JSONResponse(data)


@app.get("/api/qr.png")
def qr_png() -> FileResponse:
    if not _QR_PNG.exists():
        _refresh_qr()
    if not _QR_PNG.exists():
        raise HTTPException(404, "QR not available — run ./run.sh or tool-qr.py --pin")
    return FileResponse(_QR_PNG, media_type="image/png")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "console.html")


@app.get("/api/status")
def status() -> JSONResponse:
    up = _daemon_up()
    state = _toggle_state()
    return JSONResponse({
        "daemon": up,
        "state": state,                           # asleep | semi | awake | unknown
        # Dozing keeps the app warm but muted; the console treats it as off.
        "conversation": state != "semi" and _conversation_running(),
        "gradio_url": _conversation_ui_url(),
        "busy": _robot_lock.locked(),
        "app": _current_app() if up else None,
        "sense": _sense_summary(),
        "last": _last,
        "host": f"{HOST}:{PORT}",
    })


@app.get("/api/log", response_model=None)
def log_view(n: int = 80, fmt: str = ""):
    """Verbose action history (newest last). Ring buffer of 200 entries.

    ?n=50 limits rows returned. ?fmt=text returns plain lines for copy/paste."""
    n = max(1, min(n, 200))
    rows = list(_LOG)[-n:]
    if fmt.lower() in ("text", "txt", "plain"):
        lines = [
            f"{r['t']}  {r['action']:<16}  {'OK' if r['ok'] else 'FAIL'}  {r['msg']}"
            for r in rows
        ]
        return PlainTextResponse("\n".join(lines) if lines else "(no activity yet)\n")
    return JSONResponse(rows)


def _action(name: str, args: list[str]) -> JSONResponse:
    if not _robot_lock.acquire(blocking=False):
        _logrec(name, False, "busy — concurrent action rejected")
        raise HTTPException(409, "Peachy is busy with another action — wait a sec")
    try:
        t0 = time.time()
        ok, msg = _run_script(args)
        _last.update(action=name, ok=ok, msg=msg, at=time.time())
        _logrec(name, ok, f"({time.time()-t0:.1f}s) {msg}")
        return JSONResponse({"ok": ok, "msg": msg})
    finally:
        _robot_lock.release()


@app.post("/api/do/{cmd}")
def do(cmd: str) -> JSONResponse:
    if cmd not in ("wake", "sleep", "toggle", "semi"):
        raise HTTPException(404, "unknown command")
    if not _robot_lock.acquire(blocking=False):
        _logrec(cmd, False, "busy — concurrent action rejected")
        raise HTTPException(409, "Peachy is busy with another action — wait a sec")
    try:
        if cmd == "semi":
            t0 = time.time()
            ok, msg = _enter_semi()
            _last.update(action=cmd, ok=ok, msg=msg, at=time.time())
            _logrec(cmd, ok, f"({time.time()-t0:.1f}s) {msg}")
            return JSONResponse({"ok": ok, "msg": msg})
        released = _release_robot_control()
        t0 = time.time()
        ok, msg = _run_script([str(_SCRIPTS / "ctl-toggle.py"), cmd])
        if released:
            msg = f"{released} · {msg}" if msg else released
        _last.update(action=cmd, ok=ok, msg=msg, at=time.time())
        _logrec(cmd, ok, f"({time.time()-t0:.1f}s) {msg}")
        return JSONResponse({"ok": ok, "msg": msg})
    finally:
        _robot_lock.release()


@app.get("/api/head")
def head_status() -> JSONResponse:
    """Saved head_offset + live daemon pose (for wake-pose tuning UI)."""
    off = _head_offset()
    live: dict = {}
    try:
        live = _daemon_get("/api/state/full").get("head_pose", {})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        pass
    ang = ("roll", "pitch", "yaw")
    return JSONResponse({
        "offset": off,
        "offset_deg": {k: round(math.degrees(off[k]), 2) for k in ang},
        "live": live,
        "live_deg": {k: round(math.degrees(float(live.get(k, 0))), 2) for k in ang},
        "saved_at": _load_state().get("head_offset_saved"),
    })


@app.post("/api/head/move")
async def head_move(request: Request) -> JSONResponse:
    """Move to a head_offset pose (degrees for roll/pitch/yaw). Body: pitch, roll, yaw
    (optional x,y,z metres). Persists in session until save."""
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy — wait a sec")
    try:
        body = await request.json()
        pose = _head_offset()
        for k in ("x", "y", "z"):
            if k in body:
                pose[k] = float(body[k])
        for k in ("roll", "pitch", "yaw"):
            if k in body:
                pose[k] = math.radians(float(body[k]))
            elif f"{k}_deg" in body:
                pose[k] = math.radians(float(body[f"{k}_deg"]))
        t0 = time.time()
        try:
            _head_goto(pose)
            msg = (f"roll={math.degrees(pose['roll']):+.1f}° "
                   f"pitch={math.degrees(pose['pitch']):+.1f}° "
                   f"yaw={math.degrees(pose['yaw']):+.1f}°")
            _logrec("head:move", True, f"({time.time()-t0:.1f}s) {msg}")
            return JSONResponse({"ok": True, "msg": msg, "pose": pose,
                                 "pose_deg": {k: round(math.degrees(pose[k]), 2)
                                               for k in ("roll", "pitch", "yaw")}})
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError) as e:
            m = str(e)[-200:]
            _logrec("head:move", False, m)
            return JSONResponse({"ok": False, "msg": m})
    finally:
        _robot_lock.release()


@app.post("/api/head/save")
async def head_save(request: Request) -> JSONResponse:
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy — wait a sec")
    try:
        body = await request.json()
        sys.path.insert(0, str(_SCRIPTS))
        from head_pose import write_home

        if body.get("from_live"):
            live = _daemon_json("/api/state/full")
            hp = dict(live.get("head_pose") or {})
            ant = list(live.get("antennas_position") or [0.0, 0.0])
        else:
            pose = _head_offset()
            for k in ("x", "y", "z"):
                if k in body:
                    pose[k] = float(body[k])
            for k in ("roll", "pitch", "yaw"):
                if k in body:
                    pose[k] = math.radians(float(body[k]))
                elif f"{k}_deg" in body:
                    pose[k] = math.radians(float(body[f"{k}_deg"]))
            hp = _clamp_head(pose)
            ant = [0.0, 0.0]
        write_home(hp, ant)
        msg = "head home saved → .run/reachy_toggle_state.json"
        _logrec("head:save", True, msg)
        return JSONResponse({"ok": True, "msg": msg})
    finally:
        _robot_lock.release()


@app.post("/api/head/capture")
def head_capture() -> JSONResponse:
    """Save the robot's current live pose as head home (after manual positioning)."""
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy — wait a sec")
    try:
        sys.path.insert(0, str(_SCRIPTS))
        from head_pose import write_home

        live = _daemon_json("/api/state/full")
        hp = dict(live.get("head_pose") or {})
        ant = list(live.get("antennas_position") or [0.0, 0.0])
        write_home(hp, ant)
        msg = "captured live pose as head home"
        _logrec("head:capture", True, msg)
        return JSONResponse({"ok": True, "msg": msg,
                             "pose_deg": {k: round(math.degrees(float(hp.get(k, 0))), 2)
                                          for k in ("roll", "pitch", "yaw")}})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError) as e:
        m = str(e)[-200:]
        _logrec("head:capture", False, m)
        return JSONResponse({"ok": False, "msg": m})
    finally:
        _robot_lock.release()


@app.post("/api/head/zero")
def head_zero() -> JSONResponse:
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy — wait a sec")
    try:
        pose = {k: 0.0 for k in _HEAD_AXES}
        _head_goto(pose)
        _logrec("head:zero", True, "moved to 0° (saved home unchanged — use Save home to update)")
        return JSONResponse({"ok": True, "msg": "moved to neutral (saved home unchanged)"})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError) as e:
        _logrec("head:zero", False, str(e)[-200:])
        return JSONResponse({"ok": False, "msg": str(e)})
    finally:
        _robot_lock.release()


@app.post("/api/express/{name}")
def express(name: str) -> JSONResponse:
    return _action(f"express:{name}",
                   [str(_SCRIPTS / "ctl-express.py"), "express", name])


_TURRET_PACK = "portalturret"
_TURRET_DIR = _REPO / "sounds" / _TURRET_PACK
_TURRET_MANIFEST = _TURRET_DIR / "manifest.json"
_TURRET_CUES = os.environ.get("PEACHY_TURRET_CUES", "1").lower() not in (
    "0", "false", "off")


def _load_turret_manifest() -> dict:
    if not _TURRET_MANIFEST.is_file():
        return {"sounds": {}, "cues": {}}
    return json.loads(_TURRET_MANIFEST.read_text())


def _robot_sound_files() -> set[str]:
    sys.path.insert(0, str(_SCRIPTS))
    from sound_sync import list_robot_sounds

    return list_robot_sounds(HOST, PORT)


def _ensure_wav_on_robot(filename: str) -> None:
    if filename in _robot_sound_files():
        return
    local = _TURRET_DIR / filename
    if not local.is_file():
        raise FileNotFoundError(f"missing {local}")
    sys.path.insert(0, str(_SCRIPTS))
    from sound_sync import upload_wav

    upload_wav(HOST, PORT, local)


_speaker_until = 0.0


def _speaking(seconds: float) -> None:
    """Mark the robot speaker busy so the wake word does not hear Peachy itself."""
    global _speaker_until
    _speaker_until = max(_speaker_until, time.time() + seconds)


def _wav_seconds(path: Path, default: float = 4.0) -> float:
    try:
        import wave
        with wave.open(str(path)) as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, EOFError, ValueError):
        return default


def _play_wav_file(filename: str) -> None:
    _ensure_wav_on_robot(filename)
    _speaking(_wav_seconds(_TURRET_DIR / filename) + 1.0)
    _daemon_json("/api/media/play_sound", "POST", {"file": filename}, timeout=12)


def _play_turret_id(sound_id: str) -> tuple[bool, str]:
    manifest = _load_turret_manifest()
    meta = manifest.get("sounds", {}).get(sound_id)
    if not meta:
        return False, f"unknown turret sound {sound_id!r}"
    fname = meta["file"]
    _play_wav_file(fname)
    return True, fname


def _play_turret_cue(cue: str) -> None:
    manifest = _load_turret_manifest()
    sid = manifest.get("cues", {}).get(cue)
    if not sid:
        return
    ok, msg = _play_turret_id(sid)
    _logrec(f"turret:{cue}", ok, msg)


def _turret_cue_async(cue: str) -> None:
    threading.Thread(target=_play_turret_cue, args=(cue,), daemon=True).start()


@app.get("/api/sounds/turret")
def turret_catalog() -> JSONResponse:
    manifest = _load_turret_manifest()
    on_robot = _robot_sound_files()
    sounds = []
    for sid, meta in manifest.get("sounds", {}).items():
        fname = meta["file"]
        sounds.append({
            "id": sid,
            "file": fname,
            "label": meta.get("label", sid.replace("_", " ")),
            "on_robot": fname in on_robot,
        })
    return JSONResponse({
        "pack": _TURRET_PACK,
        "cues": manifest.get("cues", {}),
        "sounds": sounds,
    })


@app.post("/api/sounds/turret/sync")
def turret_sync() -> JSONResponse:
    sys.path.insert(0, str(_SCRIPTS))
    from sound_sync import sync_pack

    try:
        result = sync_pack(HOST, PORT, _TURRET_PACK)
        msg = f"uploaded {len(result['uploaded'])}, skipped {len(result['skipped'])}"
        _logrec("turret:sync", True, msg)
        return JSONResponse({"ok": True, "msg": msg, **result})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, FileNotFoundError, ValueError) as e:
        msg = str(e)[-200:]
        _logrec("turret:sync", False, msg)
        return JSONResponse({"ok": False, "msg": msg})


def _saved_volume() -> int:
    try:
        return max(0, min(100, int(json.loads(_VOLUME_FILE.read_text()).get("volume", _DEFAULT_VOLUME))))
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return _DEFAULT_VOLUME


def _persist_volume(vol: int) -> None:
    _VOLUME_FILE.write_text(json.dumps({"volume": vol}, indent=2) + "\n")


def _apply_speaker_volume(vol: int) -> dict:
    """Set daemon speaker volume and persist. Returns daemon response dict."""
    vol = max(0, min(100, int(vol)))
    snap = _daemon_json("/api/volume/set", "POST", {"volume": vol}, timeout=12.0)
    _persist_volume(vol)
    return snap if isinstance(snap, dict) else {"volume": vol}


def _ensure_volume_boot() -> None:
    """Once per dashboard process: apply saved speaker level (default max)."""
    global _volume_boot_done
    if _volume_boot_done or not _daemon_up():
        return
    _volume_boot_done = True
    vol = _saved_volume()
    try:
        cur = int(_daemon_json("/api/volume/current", timeout=6.0).get("volume", -1))
        if cur != vol:
            _daemon_json("/api/volume/set", "POST", {"volume": vol}, timeout=10.0)
            _logrec("volume:boot", True, f"{vol}%")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        _volume_boot_done = False
        _logrec("volume:boot", False, str(e)[-200:])


@app.post("/api/sound/stop")
def stop_sound() -> JSONResponse:
    try:
        _daemon_json("/api/media/stop_sound", "POST", {}, timeout=8)
        _logrec("sound:stop", True, "")
        return JSONResponse({"ok": True, "msg": "stopped"})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError) as e:
        msg = str(e)[-200:]
        _logrec("sound:stop", False, msg)
        return JSONResponse({"ok": False, "msg": msg})


@app.get("/api/volume")
def volume_get() -> JSONResponse:
    """Current robot speaker volume (0–100). Applies saved level on first read."""
    _ensure_volume_boot()
    try:
        snap = _daemon_json("/api/volume/current", timeout=6.0)
        return JSONResponse({"ok": True, **snap, "saved": _saved_volume()})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return JSONResponse({"ok": False, "msg": str(e)[-200:]})


@app.post("/api/volume")
async def volume_set(request: Request) -> JSONResponse:
    """Set speaker volume 0–100 (daemon plays a short test blip on set)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    vol = max(0, min(100, int(body.get("volume", _DEFAULT_VOLUME))))
    _speaking(2.5)
    try:
        snap = _apply_speaker_volume(vol)
        _logrec("volume:set", True, f"{vol}%")
        return JSONResponse({"ok": True, **snap})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        msg = str(e)[-200:]
        _logrec("volume:set", False, msg)
        return JSONResponse({"ok": False, "msg": msg})


@app.post("/api/volume/test")
def volume_test() -> JSONResponse:
    """Play test sound at current volume without changing level."""
    _speaking(3.0)
    try:
        snap = _daemon_json("/api/volume/test-sound", "POST", {}, timeout=12.0)
        _logrec("volume:test", True, snap.get("message", "ok"))
        return JSONResponse({"ok": True, **snap})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        msg = str(e)[-200:]
        _logrec("volume:test", False, msg)
        return JSONResponse({"ok": False, "msg": msg})


@app.post("/api/say")
def say_text(body: dict) -> JSONResponse:
    """Speak typed text on the robot speaker (ctl-say.py)."""
    global _speaker_until
    text = " ".join(str(body.get("text", "")).split())[:500]
    if not text:
        return JSONResponse({"ok": False, "msg": "Type something first"})
    t0 = time.time()
    _speaking(130)
    try:
        ok, msg = _run_script([str(_SCRIPTS / "ctl-say.py"), text], timeout=120)
    finally:
        _speaker_until = time.time() + 1.5
    _logrec("say", ok, f"({time.time()-t0:.1f}s) {msg.splitlines()[-1] if msg else ''}")
    return JSONResponse({"ok": ok, "msg": msg})


def converse(cmd: str, keep_body: int = 0) -> JSONResponse:
    """Start/stop the conversation app. Use ``end`` or ``/api/shutdown`` to sleep too.
    From Dozing the body turns to face front unless ``keep_body`` (room watch found
    someone where it is looking)."""
    if cmd == "start":
        if _conversation_running():
            if _hold_mode() == "free":
                return JSONResponse({"ok": True, "msg": "already listening — talk to Peachy"})
            if not _robot_lock.acquire(blocking=False):
                raise HTTPException(409, "Peachy is busy — wait a sec")
            try:
                t0 = time.time()
                ok, msg = _wake_from_semi(keep_body=bool(keep_body))
                _last.update(action="converse:start", ok=ok, msg=msg, at=time.time())
                _logrec("converse:start", ok, f"({time.time()-t0:.1f}s) {msg}")
                return JSONResponse({"ok": ok, "msg": msg, "gradio_url": _conversation_ui_url()})
            finally:
                _robot_lock.release()
        if not _robot_lock.acquire(blocking=False):
            raise HTTPException(409, "Peachy is busy — wait a sec")
        try:
            if _patch_installed() and _hold_mode() != "free":
                _motion({"mode": "free"})
            released = _release_robot_control()
            wok, wmsg = _run_script([str(_SCRIPTS / "ctl-toggle.py"), "wake"], 90)
            c = _convo_sh("start", 60)
            ok = wok and _conversation_running()
            if ok:
                msg = "listening — talk to Peachy"
            else:
                msg = (c.stdout + c.stderr + " | " + wmsg).strip()[-300:]
            if ok:
                try:
                    _apply_speaker_volume(_DEFAULT_VOLUME)
                    _logrec("volume:convo-start", True, f"{_DEFAULT_VOLUME}%")
                    msg = f"{msg} · speaker {_DEFAULT_VOLUME}%"
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
                    _logrec("volume:convo-start", False, str(e)[-200:])
                vok, vmsg = _apply_convo_voice(_DEFAULT_CONVO_VOICE)
                _logrec("voice:convo-start", vok, vmsg[:200])
                if vok:
                    msg = f"{msg} · {vmsg}"
            if released and ok:
                msg = f"{released} · {msg}"
            action = "converse:start"
            _last.update(action=action, ok=ok, msg=msg, at=time.time())
            _logrec(action, ok, msg)
            return JSONResponse({"ok": ok, "msg": msg, "gradio_url": _conversation_ui_url()})
        finally:
            _robot_lock.release()
    if cmd == "stop":
        _convo_sh("stop", 25)
        ok = not _conversation_running()
        msg = "conversation stopped" if ok else "stop sent — check log"
        _last.update(action="converse:stop", ok=ok, msg=msg, at=time.time())
        _logrec("converse:stop", ok, msg)
        return JSONResponse({"ok": ok, "msg": msg})
    if cmd == "end":
        _convo_sh("stop", 25)
        if not _robot_lock.acquire(blocking=False):
            raise HTTPException(409, "Peachy is busy — wait a sec")
        try:
            released = _release_robot_control()
            ok, msg = _run_script([str(_SCRIPTS / "ctl-toggle.py"), "sleep"], 120)
            if released:
                msg = f"{released} · {msg}" if msg else released
            msg = f"conversation stopped · {msg}" if msg else "conversation stopped · asleep"
            _last.update(action="converse:end", ok=ok, msg=msg, at=time.time())
            _logrec("converse:end", ok, msg)
            return JSONResponse({"ok": ok, "msg": msg})
        finally:
            _robot_lock.release()
    raise HTTPException(404, "unknown command")


@app.get("/api/converse/voices")
def converse_voices() -> JSONResponse:
    """Available voices from conversation app (:7860)."""
    from voice_catalog import OPENAI_VOICES, catalog_for_ids

    fallback = [v["id"] for v in OPENAI_VOICES]
    if not _conversation_running():
        return JSONResponse({
            "ok": False,
            "error": "conversation_not_running",
            "voices": fallback,
            "cards": catalog_for_ids(fallback),
        })
    try:
        data = _convo_api_json("/voices")
        voices = [str(v) for v in data] if isinstance(data, list) else []
        return JSONResponse({
            "ok": True,
            "voices": voices,
            "cards": catalog_for_ids(voices),
        })
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError) as e:
        return JSONResponse({
            "ok": False,
            "error": str(e)[:200],
            "voices": fallback,
            "cards": catalog_for_ids(fallback),
        })


@app.get("/api/converse/voices/current")
def converse_voice_current() -> JSONResponse:
    if not _conversation_running():
        return JSONResponse({"ok": False, "error": "conversation_not_running"})
    try:
        data = _convo_api_json("/voices/current")
        if isinstance(data, dict):
            return JSONResponse({"ok": True, **data})
        return JSONResponse({"ok": False, "error": "invalid_response"})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError) as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]})


@app.post("/api/converse/voices/apply")
def converse_voice_apply(voice: str = Query(...)) -> JSONResponse:
    if not _conversation_running():
        return JSONResponse({"ok": False, "error": "conversation_not_running"})
    try:
        data = _convo_api_json("/voices/apply", method="POST", params={"voice": voice})
        if isinstance(data, dict) and data.get("ok") is False:
            return JSONResponse(data)
        return JSONResponse({"ok": True, **data} if isinstance(data, dict) else {"ok": True})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError) as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]})


@app.get("/api/converse/personalities")
def converse_personalities() -> JSONResponse:
    """Personality modes from conversation app (:7860)."""
    from personality_catalog import DEFAULT_OPTION, catalog_for_ids

    if not _conversation_running():
        preview_ids = [DEFAULT_OPTION, "hype_bot", "victorian_butler", "mad_scientist_assistant",
                       "noir_detective", "nature_documentarian", "captain_circuit", "chess_coach"]
        preview = catalog_for_ids(preview_ids)
        return JSONResponse({
            "ok": False,
            "error": "conversation_not_running",
            "choices": [p["id"] for p in preview],
            "cards": preview,
            "current": DEFAULT_OPTION,
        })
    try:
        data = _convo_api_json("/personalities")
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "error": "invalid_response"})
        choices = [str(c) for c in data.get("choices", [])]
        return JSONResponse({
            "ok": True,
            "choices": choices,
            "cards": catalog_for_ids(choices),
            "current": data.get("current"),
            "startup": data.get("startup"),
            "locked": data.get("locked"),
        })
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError) as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]})


@app.post("/api/converse/personalities/apply")
def converse_personality_apply(
    name: str = Query(...),
    persist: bool = Query(False),
) -> JSONResponse:
    if not _conversation_running():
        return JSONResponse({"ok": False, "error": "conversation_not_running"})
    try:
        data = _convo_api_json(
            "/personalities/apply",
            method="POST",
            params={"name": name, "persist": "true" if persist else "false"},
            timeout=15.0,
        )
        if isinstance(data, dict) and data.get("ok") is False:
            return JSONResponse(data)
        return JSONResponse({"ok": True, **data} if isinstance(data, dict) else {"ok": True})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError) as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]})


@app.post("/api/snap")
def snap() -> JSONResponse:
    """Wake if asleep, settle to head home, then grab a still."""
    if not _daemon_up():
        return JSONResponse({"ok": False, "msg": "Peachy is offline"})
    out = _STATIC / "snap.jpg"
    if _toggle_state() == "asleep":
        if _robot_lock.acquire(blocking=False):
            try:
                ok, msg = _run_script([str(_SCRIPTS / "ctl-toggle.py"), "wake"], timeout=120)
            finally:
                _robot_lock.release()
            if not ok:
                return JSONResponse({"ok": False, "msg": msg})
        else:
            return JSONResponse({"ok": False, "msg": "Peachy is busy — wait for wake to finish"})
    _settle_head_home()
    if _TURRET_CUES:
        _turret_cue_async("snap")
    payload = _fetch_snap_frame(out)
    ok = payload.get("ok") is not False
    _logrec("snap", ok, payload.get("msg", ""))
    _last.update(action="snap", ok=ok, msg=payload.get("msg", ""), at=time.time())
    return JSONResponse(payload)


@app.post("/api/abort")
def abort_action() -> JSONResponse:
    """Emergency halt — stop moves, background apps, Follow and room watch; robot stays put."""
    cfg = _sense_cfg()
    if cfg["follow"] or cfg["watch"]:
        _sense_set(follow=False, watch=False)
    ok, m = _stop_services(sleep=False)
    _logrec("ABORT", ok, m)
    _last.update(action="abort", ok=ok, msg=m, at=time.time())
    return JSONResponse({"ok": ok, "msg": m})


@app.post("/api/shutdown")
def shutdown_action() -> JSONResponse:
    """Stop everything Peachy-ish (conversation, watch) and sleep."""
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy — wait a sec")
    try:
        t0 = time.time()
        if any(_sense_cfg().values()):
            _sense_set(follow=False, wake=False, watch=False)
        ok, m = _stop_services(sleep=True)
        _logrec("shutdown", ok, f"({time.time()-t0:.1f}s) {m}")
        _last.update(action="shutdown", ok=ok, msg=m, at=time.time())
        return JSONResponse({"ok": ok, "msg": m})
    finally:
        _robot_lock.release()


@app.get("/favicon.ico")
def favicon() -> PlainTextResponse:
    return PlainTextResponse("", status_code=204)


# ---------------------------------------------------------------- head fan (SSH)
_FAN_HIST: deque = deque(maxlen=120)


def _fan_snapshot() -> dict:
    sys.path.insert(0, str(_SCRIPTS))
    from fan_read import read_remote

    snap = read_remote()
    if snap.get("ok", True):
        snap["ts"] = time.time()
        snap["t"] = time.strftime("%H:%M:%S")
        _FAN_HIST.append({k: snap.get(k) for k in ("t", "ts", "temp_c", "pwm", "fan_state", "fan_pct")})
    return snap


def _fan_tool(cmd: str, arg: str = "") -> tuple[bool, str]:
    args = ["bash", str(_SCRIPTS / "tool-fan.sh"), cmd] + ([arg] if arg else [])
    try:
        p = subprocess.run(args, cwd=_REPO, env=_script_env(), capture_output=True, text=True, timeout=45)
        return p.returncode == 0, (p.stdout + p.stderr).strip()[-400:]
    except subprocess.TimeoutExpired:
        return False, "fan command timed out"


@app.get("/api/fan")
def fan_status() -> JSONResponse:
    try:
        snap = _fan_snapshot()
    except (subprocess.TimeoutExpired, OSError) as e:
        return JSONResponse({"ok": False, "msg": f"robot unreachable: {e}"[-200:]})
    hist = list(_FAN_HIST)
    pwms = [h["pwm"] for h in hist if h.get("pwm") is not None]
    spikes = sum(1 for a, b in zip(pwms, pwms[1:]) if abs(b - a) >= 20)
    snap["history"] = hist
    snap["pwm_spikes"] = spikes
    snap["recommend_restore"] = bool(snap.get("trip_lowered"))
    snap["recommend_calm"] = bool(
        snap.get("cusp_risk")
        or (snap.get("quiet_at_default")
            and snap.get("temp_c", 0) >= snap.get("trip_1_c", 45) - 1.5
            and spikes >= 2))
    snap["recommend_anti_pulse"] = bool(
        snap.get("quiet_at_default") is False
        and not snap.get("trip_lowered")
        and not snap.get("calm_at_trip")
        and snap.get("trip_1_c", 0) >= 44.5
        and spikes >= 3)
    return JSONResponse(snap)


@app.post("/api/fan/calm")
def fan_calm() -> JSONResponse:
    """Raise trip_1 to 46°C — fan stays off at idle (~44–45°C), stops cusp pulsing."""
    ok, msg = _fan_tool("calm")
    _logrec("fan:calm", ok, msg)
    return JSONResponse({"ok": ok, "msg": msg, "trip_c": 46})


@app.post("/api/fan/steady")
def fan_steady(trip: int | None = None) -> JSONResponse:
    """Lower trip to 44°C — fan stays on at idle (steady hum, louder than calm)."""
    t = max(43, min(44, trip if trip is not None else 44))
    ok, msg = _fan_tool("persist", str(t))
    _logrec("fan:anti-pulse", ok, f"trip_1={t}°C {msg}")
    return JSONResponse({"ok": ok, "msg": msg, "trip_c": t})


@app.post("/api/fan/restore")
def fan_restore() -> JSONResponse:
    ok, msg = _fan_tool("restore")
    _logrec("fan:restore", ok, msg)
    return JSONResponse({"ok": ok, "msg": msg})


# ---------------------------------------------------------------- conversation patch (SSH)
_MOTION_PY = r"""
import json, os, sys
p = os.path.expanduser('~/.peachy/motion.json')
pkg = '/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini_conversation_app'
try:
    d = json.load(open(p))
except (OSError, ValueError):
    d = {}
upd = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
if upd:
    d.update(upd)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p + '.new', 'w').write(json.dumps(d))
    os.replace(p + '.new', p)
try:
    installed = 'import peachy_patch' in open(pkg + '/main.py').read() and os.path.exists(pkg + '/peachy_patch.py')
except OSError:
    installed = False
print(json.dumps({'installed': installed, 'settings': d}))
"""
_patch_seen: dict = {"at": 0.0, "installed": None}


def _motion(update: dict | None = None) -> dict | None:
    """Read (and optionally merge into) ~/.peachy/motion.json on the robot. None if SSH is out."""
    sys.path.insert(0, str(_SCRIPTS))
    from robotssh import ssh_run

    arg = " '" + json.dumps(update) + "'" if update else ""
    p = ssh_run("python3 -" + arg, input_text=_MOTION_PY, timeout=12)
    if p is None or p.returncode != 0:
        return None
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    _patch_seen.update(at=time.time(), installed=bool(out.get("installed")),
                       mode=(out.get("settings") or {}).get("mode", "free"))
    return out


def _patch_installed() -> bool:
    if time.time() - _patch_seen["at"] > 120 or _patch_seen["installed"] is None:
        _motion()
    return bool(_patch_seen["installed"])


_hold_refresh = threading.Lock()
_hold_retry = {"at": 0.0}


def _refresh_hold() -> None:
    if not _hold_refresh.acquire(blocking=False):
        return
    try:
        if _motion() is None:
            _hold_retry["at"] = time.time() + 15
    finally:
        _hold_refresh.release()


def _hold_mode(block: bool = True) -> str:
    """Patch head mode: 'free' (app drives), 'tucked' (semi-awake) or 'lifted'. Cached;
    with block=False a stale cache refreshes in the background (SSH can take seconds)."""
    if time.time() - _patch_seen["at"] > 60 or _patch_seen.get("mode") is None:
        if block:
            _motion()
        elif time.time() >= _hold_retry["at"] and not _hold_refresh.locked():
            threading.Thread(target=_refresh_hold, daemon=True).start()
    m = _patch_seen.get("mode")
    return m if m in ("tucked", "lifted") else "free"


# ---------------------------------------------------------------- semi-awake
# The conversation app stays running with the head tucked and the mic muted, so
# waking is a mode flip (~1 s) instead of a cold start (~30 s).
_GREETING = os.environ.get("PEACHY_GREETING", "Hi! I'm here.")
# Body direction while dozing (world degrees, clockwise-positive; see heading.py).
# Room watch's light thresholds come from samples taken here (.run/light_samples);
# lights on/off differ most at −100°.
_DOZE_DEG = max(-160.0, min(160.0, float(os.environ.get("PEACHY_DOZE_DEG", "-100"))))
_POSE_KEYS = ("x", "y", "z", "roll", "pitch", "yaw")


def _semi_poses() -> dict | None:
    cal = _load_state().get("calibration") or {}
    try:
        s = cal["sleep"]
        tucked = {"head": [float(s["head_pose"].get(k, 0.0)) for k in _POSE_KEYS],
                  "antennas": [float(a) for a in s["antennas"]][:2]}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    sys.path.insert(0, str(_SCRIPTS))
    from head_pose import resolve_home

    home = resolve_home()
    if home:
        hp, ant = home
    else:
        w = cal.get("wake") or {}
        hp, ant = w.get("head_pose") or {}, w.get("antennas") or [0.0, 0.0]
    lifted = {"head": [float(hp.get(k, 0.0)) for k in _POSE_KEYS],
              "antennas": [float(a) for a in ant][:2]}
    return {"tucked": tucked, "lifted": lifted}


def _set_toggle_state(state: str) -> None:
    d = _load_state()
    d["state"] = state
    d["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _STATE_FILE.write_text(json.dumps(d, indent=2) + "\n")


def _enter_semi() -> tuple[bool, str]:
    """Head tucked, mic muted, conversation app warm. Caller holds _robot_lock."""
    poses = _semi_poses()
    if poses is None:
        return False, "no pose calibration — run: python scripts/ctl-toggle.py calibrate"
    if not _patch_installed():
        return False, "the app patch is not installed — run: scripts/app-patch.sh apply"
    if _motion({"mode": "tucked", "body_yaw": heading.enc_rad(_DOZE_DEG), "head_pitch": 0.0, **poses}) is None:
        return False, "robot SSH unavailable"
    notes = [f"semi-awake — head tucked, mic off, facing {_DOZE_DEG:+.0f}°"]
    if not _conversation_running():
        _enable_motors()
        c = _convo_sh("start", 60)
        if not _conversation_running():
            return False, ("conversation app did not start: " + (c.stdout + c.stderr).strip())[-300:]
        try:
            _apply_speaker_volume(_DEFAULT_VOLUME)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            pass
        _apply_convo_voice(_DEFAULT_CONVO_VOICE)
        notes.append("conversation app warm")
    _set_toggle_state("semi")
    return True, " · ".join(notes)


def _wake_from_semi(keep_body: bool = False) -> tuple[bool, str]:
    """Head up, mic live, fixed greeting. Caller holds _robot_lock."""
    upd = {"mode": "free", "head_pitch": 0.0}
    if not keep_body:
        upd["body_yaw"] = heading.enc_rad(0.0)
    if _motion(upd) is None:
        return False, "robot SSH unavailable"
    _set_toggle_state("awake")

    def greet() -> None:
        try:
            r = _convo_rpc("conversation.say",
                           {"text": f'Say exactly this and nothing else: "{_GREETING}"'}, timeout=8)
            if isinstance(r, dict) and r.get("ok") is False:
                _logrec("converse:greet", False, str(r.get("error")))
        except Exception as e:  # noqa: BLE001
            _logrec("converse:greet", False, str(e)[-200:])

    threading.Thread(target=greet, daemon=True).start()
    return True, "listening — talk to Peachy"


@app.post("/api/doze/pose")
def doze_pose(body: dict) -> JSONResponse:
    """Room watch looking around while Dozing: head "lifted" / "tucked" and/or the
    body direction (yaw_deg, clockwise-positive world degrees, or encoder degrees with
    ``"enc": true``). ``tilt_deg`` (up-positive) tilts the lifted pose; 0 restores it.
    ``wait`` returns once the body is there."""
    if _toggle_state() != "semi":
        raise HTTPException(409, "not dozing")
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy — wait a sec")
    enc = bool(body.get("enc"))
    try:
        upd: dict = {}
        if body.get("head") in ("lifted", "tucked"):
            upd["mode"] = body["head"]
        if body.get("tilt_deg") is not None:
            poses = _semi_poses()
            if poses is None:
                return JSONResponse({"ok": False, "msg": "no pose calibration"})
            lifted = poses["lifted"]
            head = list(lifted["head"])
            head[4] -= math.radians(max(-_PITCH_LIM_DEG, min(_PITCH_LIM_DEG, float(body["tilt_deg"]))))
            upd["lifted"] = {"head": head, "antennas": lifted["antennas"]}
        yaw_rad = None
        if body.get("yaw_deg") is not None:
            yaw_deg = float(body["yaw_deg"])
            yaw_deg = max(-160.0, min(160.0, yaw_deg)) if enc else heading.to_enc(yaw_deg)
            yaw_rad = upd["body_yaw"] = -math.radians(yaw_deg)
        if not upd:
            return JSONResponse({"ok": False, "msg": "nothing to change"})
        if _motion(upd) is None:
            _logrec("doze:pose", False, "robot SSH unavailable")
            return JSONResponse({"ok": False, "msg": "robot SSH unavailable"})
        if "mode" in upd:
            _logrec("doze:pose", True, "head " + upd["mode"])
        out: dict = {"ok": True}
        if yaw_rad is not None and body.get("wait"):
            present = _wait_body(yaw_rad, 2.5 + abs(yaw_rad - float(
                _daemon_json("/api/state/present_body_yaw", timeout=4))) / math.radians(90))
            present_enc = -math.degrees(present)
            out["present_deg"] = round(present_enc if enc else heading.to_world(present_enc), 1)
        return JSONResponse(out)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        return JSONResponse({"ok": False, "msg": str(e)[-200:]})
    finally:
        _robot_lock.release()


@app.get("/api/converse/idle-motion")
def idle_motion_get() -> JSONResponse:
    m = _motion()
    if m is None:
        return JSONResponse({"ok": False, "msg": "robot SSH unavailable"})
    s = m.get("settings") or {}
    return JSONResponse({
        "ok": True,
        "installed": m.get("installed", False),
        "breath_scale": float(s.get("breath_scale", 1.0)),
        "idle_every_s": float(s.get("idle_every_s", 180)),
    })


@app.post("/api/converse/idle-motion")
def idle_motion_set(body: dict) -> JSONResponse:
    upd = {}
    if "breath_scale" in body:
        upd["breath_scale"] = round(max(0.0, min(1.5, float(body["breath_scale"]))), 2)
    if "idle_every_s" in body:
        s = float(body["idle_every_s"])
        upd["idle_every_s"] = 0 if s <= 0 else round(max(20.0, min(900.0, s)))
    m = _motion(upd)
    if m is None:
        return JSONResponse({"ok": False, "msg": "robot SSH unavailable"})
    if not m.get("installed"):
        return JSONResponse({"ok": False, "msg": "saved, but the app patch is not installed — run scripts/app-patch.sh apply"})
    every = upd.get("idle_every_s")
    tail = "" if every is None else (" · idle actions off" if every == 0 else f" · idle action after {every}s quiet")
    msg = f"idle motion {int(upd.get('breath_scale', 1) * 100)}%{tail}" if "breath_scale" in upd else f"idle motion saved{tail}"
    _logrec("converse:idle", True, msg)
    return JSONResponse({"ok": True, "msg": msg, **upd})


_BODY_YAW_LIM = math.radians(160.0)


def _wait_body(target: float, limit_s: float) -> float:
    end = time.time() + limit_s
    present = float(_daemon_json("/api/state/present_body_yaw", timeout=4))
    while abs(target - present) > math.radians(1.5) and time.time() < end:
        time.sleep(0.3)
        present = float(_daemon_json("/api/state/present_body_yaw", timeout=4))
    return present


@app.get("/api/body")
def body_status() -> JSONResponse:
    """Current body yaw — commanded and measured. *_deg are clockwise-positive world
    degrees (seen from above, see heading.py); *_rad are the robot's
    counter-clockwise-positive encoder values."""
    try:
        state = _daemon_get("/api/state/full")
        present = float(_daemon_get("/api/state/present_body_yaw"))
        commanded = float(state.get("body_yaw", 0.0) or 0.0)
        lo, hi = heading.world_range()
        pitch = float((state.get("head_pose") or {}).get("pitch", 0.0) or 0.0)
        return JSONResponse({
            "ok": True,
            "pitch_deg": round(-math.degrees(pitch), 1),
            "yaw_rad": commanded,
            "yaw_deg": round(heading.to_world(-math.degrees(commanded)), 1),
            "present_rad": present,
            "present_deg": round(heading.to_world(-math.degrees(present)), 1),
            "offset_deg": round(heading.offset(), 1),
            "range_deg": [round(lo, 1), round(hi, 1)],
        })
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        return JSONResponse({"ok": False, "msg": str(e)[-200:]})


@app.post("/api/body/yaw")
def body_yaw_move(body: dict) -> JSONResponse:
    """Turn the whole robot: body to yaw_deg (clockwise-positive, seen from above),
    head keeps its angle relative to the body.

    The conversation app streams its own pose every tick. With the app patch it
    holds the direction in ~/.peachy/motion.json, so that is all we change; the
    stock app would undo the turn, so it is stopped first.
    """
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy — wait a sec")
    try:
        enc_deg = heading.to_enc(float(body.get("yaw_deg", 0.0)))
        yaw_deg = round(heading.to_world(enc_deg), 1)
        yaw_rad = -math.radians(enc_deg)
        dur = max(0.4, min(4.0, float(body.get("duration", 0.8))))
        t0 = time.time()
        note = ""
        try:
            running = _conversation_running()
            patched = _patch_installed()
            if running and patched and _motion({"body_yaw": yaw_rad}) is not None:
                present = _wait_body(yaw_rad, 3.5 + abs(enc_deg) / 90.0)
                msg = (f"body yaw → {yaw_deg:+.1f}° (at {heading.to_world(-math.degrees(present)):+.1f}°)"
                       " · conversation kept running")
                _logrec("body:yaw", True, f"({time.time()-t0:.1f}s) {msg}")
                return JSONResponse({"ok": True, "msg": msg, "yaw_deg": yaw_deg})
            if patched:
                threading.Thread(target=_motion, args=({"body_yaw": yaw_rad},), daemon=True).start()
            if running:
                _convo_sh("stop", 25)
                # The app's shutdown drives back to neutral over ~2 s; wait it out.
                stopped_at = time.time()
                last = None
                while time.time() - stopped_at < 10.0:
                    time.sleep(0.4)
                    now = float(_daemon_json("/api/state/present_body_yaw", timeout=4))
                    if (time.time() - stopped_at > 2.5 and not _conversation_running()
                            and last is not None and abs(now - last) < math.radians(0.3)):
                        break
                    last = now
                note = " · conversation paused"
            _enable_motors()
            state = _daemon_json("/api/state/full", timeout=4)
            hp = state.get("head_pose") or {}
            head = {k: float(hp.get(k, 0.0) or 0.0) for k in ("x", "y", "z", "roll", "pitch", "yaw")}
            rel = 0.0

            def goto(body_cmd: float, head_yaw: float, step: float) -> float:
                _daemon_json("/api/move/goto", "POST", {
                    "head_pose": {**head, "yaw": head_yaw},
                    "body_yaw": max(-_BODY_YAW_LIM, min(_BODY_YAW_LIM, body_cmd)),
                    "duration": step,
                    "interpolation": "minjerk",
                }, timeout=max(12.0, step + 8))
                time.sleep(step + 0.4)
                return float(_daemon_json("/api/state/present_body_yaw", timeout=4))

            # With the body motor's integral gain (tool-body-pid.sh) the turntable
            # creeps the last few degrees in over ~2 s; wait for it, then line the
            # head up with wherever the body actually settled.
            present = goto(yaw_rad, yaw_rad + rel, dur)
            settle_by = time.time() + 2.5
            while abs(yaw_rad - present) > math.radians(1.0) and time.time() < settle_by:
                time.sleep(0.25)
                present = float(_daemon_json("/api/state/present_body_yaw", timeout=4))
            if abs(yaw_rad - present) > math.radians(1.0):
                present = goto(yaw_rad, present + rel, 0.4)
            msg = f"body yaw → {yaw_deg:+.1f}° (at {heading.to_world(-math.degrees(present)):+.1f}°){note}"
            _logrec("body:yaw", True, f"({time.time()-t0:.1f}s) {msg}")
            return JSONResponse({"ok": True, "msg": msg, "yaw_deg": yaw_deg})
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            m = str(e)[-200:]
            _logrec("body:yaw", False, m)
            return JSONResponse({"ok": False, "msg": m})
    finally:
        _robot_lock.release()


_PITCH_LIM_DEG = 30.0


@app.post("/api/head/pitch")
def head_pitch(body: dict) -> JSONResponse:
    """Tilt the head: pitch_deg, up-positive (the robot's pitch is down-positive).
    With the patched conversation app running it holds the tilt in motion.json on
    top of the app's own head motion; otherwise the head goes there directly."""
    if _toggle_state() != "awake":
        return JSONResponse({"ok": False, "msg": "head tilt works while Awake"})
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy — wait a sec")
    try:
        up = max(-_PITCH_LIM_DEG, min(_PITCH_LIM_DEG, float(body.get("pitch_deg", 0.0))))
        pitch = -math.radians(up)
        try:
            if _conversation_running() and _patch_installed():
                if _motion({"head_pitch": pitch}) is None:
                    raise OSError("robot SSH unavailable")
                msg = f"head tilt → {up:+.0f}° · conversation kept running"
            else:
                _enable_motors()
                hp = _daemon_json("/api/state/full", timeout=4).get("head_pose") or {}
                pose = {k: float(hp.get(k, 0.0) or 0.0) for k in _HEAD_AXES}
                pose["pitch"] = pitch
                dur = max(0.4, min(1.5, abs(pitch - float(hp.get("pitch", 0.0) or 0.0)) / math.radians(40)))
                _daemon_json("/api/move/goto", "POST", {"head_pose": _clamp_head(pose), "duration": dur,
                                                        "interpolation": "minjerk"}, timeout=12)
                msg = f"head tilt → {up:+.0f}°"
            _logrec("head:pitch", True, msg)
            return JSONResponse({"ok": True, "msg": msg, "pitch_deg": up})
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
            m = str(e)[-200:]
            _logrec("head:pitch", False, m)
            return JSONResponse({"ok": False, "msg": m})
    finally:
        _robot_lock.release()


@app.post("/api/body/diag")
def body_diag() -> JSONResponse:
    """Run diag-yaw.py (±17° servo test, ~15 s) and return the full output."""
    return _action("body:diag", [str(_SCRIPTS / "diag-yaw.py"), "--amp", "0.30"])


# ---------------------------------------------------------------- move library
_MOVE_DATASETS = {
    "emotions": "pollen-robotics/reachy-mini-emotions-library",
    "dances": "pollen-robotics/reachy-mini-dances-library",
}
_MOVES_CACHE = _RUN / "moves_cache.json"
_MOVE_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9_\-]{1,64}$")
_SPACE_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_.\-]{1,96}/[A-Za-z0-9_.\-]{1,96}$")


def _load_moves_cache() -> dict[str, list[str]]:
    try:
        return json.loads(_MOVES_CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@app.get("/api/moves")
def moves_list() -> JSONResponse:
    """Recorded moves from the official emotion + dance datasets (cached in .run/)."""
    cache = _load_moves_cache()
    out: dict[str, list[str]] = {}
    for kind, ds in _MOVE_DATASETS.items():
        try:
            names = _daemon_json(f"/api/move/recorded-move-datasets/list/{ds}", timeout=90)
            if isinstance(names, list) and names:
                out[kind] = sorted(str(n) for n in names)
                continue
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            pass
        out[kind] = cache.get(kind, [])
    if all(out.values()) and out != cache:
        _MOVES_CACHE.write_text(json.dumps(out))
    return JSONResponse({"ok": True, **out})


def _wait_moves_idle(timeout: float) -> bool:
    t_end = time.time() + timeout
    time.sleep(0.4)
    while time.time() < t_end:
        try:
            if not _daemon_json("/api/move/running", timeout=4):
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return False
        time.sleep(0.35)
    return False


@app.post("/api/moves/play")
def moves_play(body: dict) -> JSONResponse:
    kind = str(body.get("kind", ""))
    name = str(body.get("name", ""))
    ds = _MOVE_DATASETS.get(kind)
    if not ds or not _MOVE_NAME_RE.match(name):
        raise HTTPException(400, "unknown move")
    if _toggle_state() != "awake":
        return JSONResponse({"ok": False, "msg": "Wake Peachy first"})
    action = f"{kind[:-1] if kind.endswith('s') else kind}:{name}"
    if not _robot_lock.acquire(blocking=False):
        _logrec(action, False, "busy — concurrent action rejected")
        raise HTTPException(409, "Peachy is busy with another action — wait a sec")
    try:
        t0 = time.time()
        try:
            _enable_motors()
            _daemon_json(f"/api/move/play/recorded-move-dataset/{ds}/{name}", "POST", timeout=60)
            _wait_moves_idle(45)
            ok, msg = True, f"({time.time()-t0:.1f}s) {name}"
        except urllib.error.HTTPError as e:
            ok, msg = False, f"HTTP {e.code}: {e.read().decode(errors='replace')[-200:]}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            ok, msg = False, str(e)[-200:]
        _last.update(action=action, ok=ok, msg=msg, at=time.time())
        _logrec(action, ok, msg)
        return JSONResponse({"ok": ok, "msg": msg})
    finally:
        _robot_lock.release()


# ---------------------------------------------------------------------- apps
_CONVO_APP_NAME = os.environ.get("PEACHY_CONVO_APP", "reachy_mini_conversation_app")
_HF_STORE_URL = ("https://huggingface.co/api/spaces?filter=reachy_mini_python_app"
                 "&sort=likes&direction=-1&limit=300"
                 "&expand[]=cardData&expand[]=likes&expand[]=lastModified&expand[]=author")
_HF_OFFICIAL_URL = ("https://huggingface.co/datasets/pollen-robotics/"
                    "reachy-mini-official-app-store/raw/main/app-list.json")
_store_cache: dict = {"at": 0.0, "apps": []}


def _http_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "peachy-console"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _current_app() -> dict | None:
    try:
        st = _daemon_get("/api/apps/current-app-status")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    if not st:
        return None
    info = st.get("info") or {}
    return {"name": info.get("name"), "state": st.get("state"), "error": st.get("error")}


def _app_url(extra: dict) -> str | None:
    u = (extra or {}).get("custom_app_url")
    if not u:
        return None
    return u.replace("0.0.0.0", HOST).replace("127.0.0.1", HOST).replace("localhost", HOST)


# The daemon answers list-available/installed by spawning a Python process to
# scan entry points, which freezes its state stream for ~1 s (the conversation
# app then drops every motion command). The list only changes on install/remove.
_installed_cache: dict = {"at": 0.0, "raw": None}
_INSTALLED_TTL = 3600.0


def _installed_forget() -> None:
    _installed_cache["raw"] = None


@app.get("/api/apps")
def apps_installed() -> JSONResponse:
    raw = _installed_cache["raw"]
    if raw is None or time.time() - _installed_cache["at"] > _INSTALLED_TTL:
        try:
            raw = _daemon_json("/api/apps/list-available/installed", timeout=20)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            return JSONResponse({"ok": False, "msg": str(e)[-200:], "apps": [], "current": None})
        _installed_cache.update(at=time.time(), raw=raw)
    apps = []
    for a in raw or []:
        extra = a.get("extra") or {}
        card = extra.get("cardData") or {}
        apps.append({
            "name": a.get("name"),
            "id": extra.get("id"),
            "title": card.get("title") or a.get("name"),
            "description": a.get("description") or card.get("short_description") or "",
            "url": _app_url(extra),
            "conversation": a.get("name") == _CONVO_APP_NAME,
        })
    apps.sort(key=lambda x: (not x["conversation"], (x["title"] or "").lower()))
    return JSONResponse({"ok": True, "apps": apps, "current": _current_app()})


@app.get("/api/apps/store")
def apps_store(refresh: bool = False) -> JSONResponse:
    if refresh or time.time() - _store_cache["at"] > 600 or not _store_cache["apps"]:
        try:
            spaces = _http_json(_HF_STORE_URL)
            try:
                official = set(_http_json(_HF_OFFICIAL_URL, timeout=10))
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
                official = set()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            if not _store_cache["apps"]:
                return JSONResponse({"ok": False, "msg": str(e)[-200:], "apps": []})
        else:
            apps = []
            for s in spaces:
                card = s.get("cardData") or {}
                sid = s.get("id") or ""
                apps.append({
                    "id": sid,
                    "author": s.get("author") or sid.split("/")[0],
                    "title": card.get("title") or sid.split("/")[-1],
                    "emoji": card.get("emoji") or "",
                    "description": card.get("short_description") or "",
                    "likes": s.get("likes") or 0,
                    "updated": (s.get("lastModified") or "")[:10],
                    "official": sid in official,
                })
            _store_cache.update(at=time.time(), apps=apps)
    return JSONResponse({"ok": True, "apps": _store_cache["apps"]})


@app.post("/api/apps/install")
def apps_install(body: dict) -> JSONResponse:
    sid = str(body.get("id", ""))
    if not _SPACE_ID_RE.match(sid):
        raise HTTPException(400, "bad space id")
    meta = next((a for a in _store_cache["apps"] if a["id"] == sid), {})
    info = {
        "name": sid.split("/")[-1],
        "source_kind": "hf_space",
        "description": meta.get("description", ""),
        "url": f"https://huggingface.co/spaces/{sid}",
        "extra": {"id": sid, "cardData": {"title": meta.get("title", ""),
                                           "short_description": meta.get("description", "")}},
    }
    try:
        r = _daemon_json("/api/apps/install", "POST", info, timeout=20)
    except urllib.error.HTTPError as e:
        m = e.read().decode(errors="replace")[-200:]
        _logrec(f"app:install:{sid}", False, m)
        return JSONResponse({"ok": False, "msg": m})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return JSONResponse({"ok": False, "msg": str(e)[-200:]})
    _installed_forget()
    _logrec(f"app:install:{sid}", True, f"job {r.get('job_id')}")
    return JSONResponse({"ok": True, "job_id": r.get("job_id")})


@app.get("/api/apps/job/{job_id}")
def apps_job(job_id: str) -> JSONResponse:
    try:
        j = _daemon_json(f"/api/apps/job-status/{urllib.parse.quote(job_id)}", timeout=8)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return JSONResponse({"ok": False, "msg": str(e)[-200:]})
    logs = j.get("logs") or []
    if j.get("status") in ("done", "failed"):
        _installed_forget()
    return JSONResponse({"ok": True, "status": j.get("status"),
                         "last": (logs[-1] if logs else "")[-200:]})


@app.post("/api/apps/remove/{name}")
def apps_remove(name: str) -> JSONResponse:
    if not _MOVE_NAME_RE.match(name) or name == _CONVO_APP_NAME:
        raise HTTPException(400, "can't remove that app")
    try:
        r = _daemon_json(f"/api/apps/remove/{name}", "POST", timeout=20)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return JSONResponse({"ok": False, "msg": str(e)[-200:]})
    _installed_forget()
    _logrec(f"app:remove:{name}", True, f"job {r.get('job_id')}")
    return JSONResponse({"ok": True, "job_id": r.get("job_id")})


@app.post("/api/apps/start/{name}")
def apps_start(name: str) -> JSONResponse:
    if not _MOVE_NAME_RE.match(name):
        raise HTTPException(400, "bad app name")
    if name == _CONVO_APP_NAME:
        return converse("start")
    if not _robot_lock.acquire(blocking=False):
        raise HTTPException(409, "Peachy is busy with another action — wait a sec")
    try:
        _release_robot_control()
        wok, wmsg = _run_script([str(_SCRIPTS / "ctl-toggle.py"), "wake"], 90)
        try:
            st = _daemon_json(f"/api/apps/start-app/{name}", "POST", timeout=60)
            ok = (st or {}).get("state") in ("starting", "running")
            msg = f"{name} {(st or {}).get('state', '')}".strip()
        except urllib.error.HTTPError as e:
            ok, msg = False, e.read().decode(errors="replace")[-200:]
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            ok, msg = False, str(e)[-200:]
        if not wok:
            msg = f"{msg} · wake: {wmsg[-120:]}"
        _last.update(action=f"app:{name}", ok=ok, msg=msg, at=time.time())
        _logrec(f"app:start:{name}", ok, msg)
        return JSONResponse({"ok": ok, "msg": msg})
    finally:
        _robot_lock.release()


@app.post("/api/apps/stop")
def apps_stop() -> JSONResponse:
    cur = _current_app()
    if cur and cur.get("name") == _CONVO_APP_NAME:
        return converse("stop")
    try:
        _daemon_json("/api/apps/stop-current-app", "POST", timeout=30)
        ok, msg = True, f"{(cur or {}).get('name') or 'app'} stopped"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        ok, msg = False, str(e)[-200:]
    _last.update(action="app:stop", ok=ok, msg=msg, at=time.time())
    _logrec("app:stop", ok, msg)
    return JSONResponse({"ok": ok, "msg": msg})


# -------------------------------------------------------------------- senses
_SENSE_CFG = _RUN / "sense_config.json"
_SENSE_PID = _RUN / "sense_live.pid"
_SENSE_LOG = _RUN / "sense_live.log"
_SENSE_STATE = _RUN / "sense_state.json"
_SENSE_FEATURES = ("follow", "wake", "watch")
_sense_lock = threading.Lock()
_sense_started = {"at": 0.0}


def _sense_cfg() -> dict:
    try:
        d = json.loads(_SENSE_CFG.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    return {k: bool(d.get(k)) for k in _SENSE_FEATURES}


def _sense_pid() -> int | None:
    try:
        pid = int(_SENSE_PID.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=3).stdout
    except (subprocess.TimeoutExpired, OSError):
        cmd = ""
    return pid if "sense-live.py" in cmd else None


def _sense_stop() -> None:
    pid = _sense_pid()
    if pid is not None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
    _SENSE_PID.unlink(missing_ok=True)
    _SENSE_STATE.unlink(missing_ok=True)


def _sense_apply(cfg: dict) -> bool:
    with _sense_lock:
        _sense_stop()
        flags = [f"--{k}" for k in _SENSE_FEATURES if cfg.get(k)]
        if not flags:
            return True
        try:
            if json.loads(_SENSE_CFG.read_text()).get("dry"):
                flags.append("--dry-run")
        except (OSError, json.JSONDecodeError):
            pass
        logf = open(_SENSE_LOG, "w", buffering=1)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "REACHY_HOST": HOST,
               "PEACHY_DASH_URL": f"http://127.0.0.1:{_dashboard_port()}",
               "PEACHY_TOKEN": _TOKEN}
        proc = subprocess.Popen([sys.executable, "-u", str(_SCRIPTS / "sense-live.py"), *flags],
                                cwd=_REPO, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True, env=env)
        _SENSE_PID.write_text(str(proc.pid))
        _sense_started["at"] = time.time()
        time.sleep(0.5)
        return proc.poll() is None


def _sense_set(**changes: bool) -> dict:
    cfg = {**_sense_cfg(), **changes}
    try:
        extra = {k: v for k, v in json.loads(_SENSE_CFG.read_text()).items() if k not in _SENSE_FEATURES}
    except (OSError, json.JSONDecodeError):
        extra = {}
    _SENSE_CFG.write_text(json.dumps({**extra, **cfg}))
    _sense_apply(cfg)
    return cfg


def _sense_summary() -> dict:
    cfg = _sense_cfg()
    pid = _sense_pid()
    if any(cfg.values()) and pid is None and time.time() - _sense_started["at"] > 30:
        threading.Thread(target=_sense_apply, args=(cfg,), daemon=True).start()
    live: dict = {}
    if pid is not None:
        try:
            live = json.loads(_SENSE_STATE.read_text())
            if time.time() - float(live.get("ts", 0)) > 10:
                live = {"stale": True}
        except (OSError, json.JSONDecodeError, ValueError):
            live = {}
    return {**cfg, "running": pid is not None, "live": live}


@app.get("/api/sense")
def sense_status() -> JSONResponse:
    s = _sense_summary()
    try:
        tail = [ln for ln in _SENSE_LOG.read_text(errors="replace").splitlines()
                if ln.startswith("[") and not ln.startswith("[ WARN")][-8:]
    except OSError:
        tail = []
    return JSONResponse({"ok": True, **s, "log": tail})


@app.get("/api/sense/gate")
def sense_gate() -> JSONResponse:
    """Who owns the robot. A semi-awake conversation app (head tucked, mic muted)
    owns nothing, so the wake word keeps listening; Follow stays off (not awake)."""
    app_owner = ""
    up = _daemon_up()
    cur = _current_app() if up else None
    if cur and cur.get("state") in ("starting", "running", "stopping"):
        app_owner = "conversation" if cur.get("name") == _CONVO_APP_NAME else "app"
    semi = app_owner == "conversation" and _hold_mode(block=False) != "free"
    if semi:
        app_owner = ""
    if not up:
        app_owner = "offline"
    elif not app_owner:
        try:
            if _daemon_get("/api/move/running", timeout=3):
                app_owner = "move"
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
            pass
    owner = "busy" if _robot_lock.locked() else app_owner
    state = _toggle_state()
    return JSONResponse({"awake": state == "awake", "state": state, "owner": owner, "semi": semi,
                         "speaking": time.time() < _speaker_until})


@app.post("/api/sense/{feature}/{onoff}")
def sense_toggle(feature: str, onoff: str) -> JSONResponse:
    if feature not in _SENSE_FEATURES or onoff not in ("on", "off"):
        raise HTTPException(404, "unknown sense")
    cfg = _sense_set(**{feature: onoff == "on"})
    running = _sense_pid() is not None
    ok = running or not any(cfg.values())
    msg = f"{feature} {onoff}" if ok else "sense-live failed to start — see .run/sense_live.log"
    _logrec(f"sense:{feature}:{onoff}", ok, msg)
    return JSONResponse({"ok": ok, "msg": msg, **cfg, "running": running})


@app.post("/api/sense/note")
def sense_note(body: dict) -> JSONResponse:
    """Room watch events land in the activity log."""
    msg = str(body.get("msg") or "").strip()[:300]
    if msg:
        _logrec("watch", body.get("ok", True) is not False, msg)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- heading
# World heading: scripts/cal-heading.py matches the head camera against landmarks
# from room scans; heading.py turns its offset into world degrees everywhere.
# The script moves the robot through /api/doze/pose, so it runs without _robot_lock.
_heading_lock = threading.Lock()
_HEADING_REFS = _RUN / "heading_ref" / "refs.json"


@app.get("/api/heading")
def heading_status() -> JSONResponse:
    d = heading.state()
    try:
        meta = json.loads(_HEADING_REFS.read_text())
    except (OSError, ValueError):
        meta = {}
    scans = meta.get("passes") or ([{"refs": meta["refs"], "at": meta.get("at")}] if meta.get("refs") else [])
    return JSONResponse({"ok": True, "offset_deg": round(heading.offset(), 1), "at": d.get("at"),
                         "inliers": d.get("inliers"), "residual_deg": d.get("residual_deg"),
                         "views": d.get("views"), "scans": len(scans),
                         "refs": sum(len(s.get("refs", [])) for s in scans),
                         "ref_at": scans[-1].get("at") if scans else None,
                         "busy": _heading_lock.locked()})


@app.post("/api/heading/{what}")
def heading_run(what: str) -> JSONResponse:
    if what not in ("capture", "anchor"):
        raise HTTPException(404, "unknown heading action")
    if _toggle_state() != "semi":
        return JSONResponse({"ok": False, "msg": "heading calibration runs while Dozing"})
    if not _heading_lock.acquire(blocking=False):
        raise HTTPException(409, "heading calibration already running")
    try:
        p = subprocess.run([sys.executable, str(_SCRIPTS / "cal-heading.py"), what], cwd=_REPO,
                           env=_script_env(), capture_output=True, text=True,
                           timeout=300 if what == "capture" else 150)
        ok, out = p.returncode == 0, p.stdout + "\n" + p.stderr
    except subprocess.TimeoutExpired:
        ok, out = False, "✗ timed out"
    finally:
        _heading_lock.release()
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    msg = next((ln.lstrip("✓✗ ") for ln in reversed(lines) if ln.startswith(("✓", "✗"))),
               "failed — see scripts/cal-heading.py" if not ok else "done")
    _logrec(f"heading:{what}", ok, msg)
    return JSONResponse({"ok": ok, "msg": msg, "offset_deg": round(heading.offset(), 1)})


# ---------------------------------------------------------------- mic scope
# Its own audio-only WebRTC consumer, open only while the console is polling:
# sense-live closes its stream during conversations, which is when the mic matters.
_MIC_BLOCK = 320                     # 20 ms at 16 kHz


class _MicScope:
    IDLE_S = 10.0

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.env: deque = deque(maxlen=600)
        self.seq = 0
        self.want = 0.0
        self.state = "off"
        self.error = ""
        self.agc: list = [None, None]
        self.retry_at = 0.0
        self.thread: threading.Thread | None = None

    def touch(self) -> None:
        self.want = time.time()
        with self.lock:
            alive = self.thread is not None and self.thread.is_alive()
            if not alive and time.time() >= self.retry_at:
                self.state = "connecting"
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def _agc(self) -> None:
        for i, name in enumerate(("PP_AGCGAIN", "PP_AGCMAXGAIN")):
            try:
                v = _daemon_json(f"/api/audio/config/parameter/{name}", timeout=3)
                self.agc[i] = round(float(v["values"][0]), 1)
            except Exception:  # noqa: BLE001
                self.agc[i] = None

    def _run(self) -> None:
        import numpy as np
        sys.path.insert(0, str(_SCRIPTS))
        from rtcmedia import RtcMedia

        m = None
        try:
            m = RtcMedia(HOST, 320, 180, fps=1, video=False).start()
            next_agc = 0.0
            while time.time() - self.want < self.IDLE_S:
                pcm = m.audio(_MIC_BLOCK * 5, timeout=2.0)
                if m.error:
                    raise RuntimeError(m.error)
                if pcm is None:
                    self.state = "no audio"
                    continue
                self.state = "on"
                blocks = pcm[: pcm.size // _MIC_BLOCK * _MIC_BLOCK].reshape(-1, _MIC_BLOCK)
                rms = np.sqrt(np.mean(blocks.astype(np.float32) ** 2, axis=1))
                with self.lock:
                    for b, r in zip(blocks, rms):
                        self.env.append((int(b.min()), int(b.max()), int(r)))
                        self.seq += 1
                if time.time() >= next_agc:
                    next_agc = time.time() + 2.0
                    threading.Thread(target=self._agc, daemon=True).start()
            self.state = "off"
        except Exception as e:  # noqa: BLE001
            self.error = str(e)[:160]
            self.state = "error"
            self.retry_at = time.time() + 5
        finally:
            if m is not None:
                m.stop()

    def since(self, seq: int) -> dict:
        with self.lock:
            n = min(len(self.env), max(0, self.seq - seq))
            blocks = list(self.env)[len(self.env) - n:] if n else []
            cur = self.seq
        return {"state": self.state, "error": self.error if self.state == "error" else "",
                "seq": cur, "rate": 16000 // _MIC_BLOCK,
                "env": [v for b in blocks for v in b], "agc": self.agc[0], "agc_max": self.agc[1]}


_mic = _MicScope()


@app.get("/api/mic")
def mic_scope(since: int = 0) -> JSONResponse:
    """Mic envelope (min, max, rms per 20 ms, int16) newer than *since*. Polling keeps it open."""
    _mic.touch()
    d = _mic.since(since)
    d["app_mic"] = "muted" if _patch_seen.get("mode") in ("tucked", "lifted") else "open"
    return JSONResponse({"ok": True, **d})


# Registered last: a single-segment {cmd} would otherwise shadow the fixed
# /api/converse/* routes above.
app.post("/api/converse/{cmd}")(converse)

app.mount("/static", StaticFiles(directory=_STATIC), name="static")

if os.environ.get("PEACHY_GRADIO_PANEL", "0").lower() not in ("0", "false", "off"):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from gradio_panel import mount_peachy_desk

        mount_peachy_desk(app)
    except Exception as exc:  # noqa: BLE001 — desk panel is optional
        print(f"Peachy desk panel (/desk) not mounted: {exc}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Peachy control server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PEACHY_PORT", "8080")))
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()
    uvicorn.run(app, host=args.bind, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
