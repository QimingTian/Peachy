"""Detect silent daemon backend (REST ok but moves don't actuate) and revive it over REST."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def _http(host: str, port: int, path: str, method: str, timeout: float,
          body: dict | None = None) -> dict:
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body is not None and method == "POST" else None
    if method == "POST" and data is None:
        data = b"{}"
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def stop_moves(host: str, port: int) -> int:
    """Stop every running move (POST /api/move/stop takes one move's uuid; a bare
    call is rejected with 422). Returns how many it stopped."""
    n = 0
    try:
        running = _http(host, port, "/api/move/running", "GET", 4.0) or []
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return 0
    for move in running if isinstance(running, list) else []:
        uuid = move.get("uuid") if isinstance(move, dict) else move
        try:
            _http(host, port, "/api/move/stop", "POST", 6.0, body={"uuid": uuid})
            n += 1
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
            pass
    return n


def wait_app_reset(host: str, port: int, max_s: float = 12.0) -> bool:
    """Call right after stopping an app. 1.5 s after an app exits the daemon puts
    the robot to sleep on its own (lift, sleep pose, motors off), and moves sent
    meanwhile are ignored or undone. Waits for that to end; True if it did."""
    time.sleep(2.0)
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            if _http(host, port, "/api/motors/status", "GET", 4.0).get("mode") == "disabled":
                time.sleep(0.3)
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
            pass
        time.sleep(0.3)
    return False


def motion_actuates(host: str, port: int, *, threshold: float = 0.012) -> bool:
    """True if the move queue accepts and runs a tiny head goto."""
    try:
        st = _http(host, port, "/api/state/full", "GET", 5.0)
        hp = dict(st.get("head_pose") or {})
        pitch0 = float(hp.get("pitch", 0.0))
        hp["pitch"] = pitch0 + 0.05 if pitch0 < 0.35 else pitch0 - 0.05
        _http(host, port, "/api/motors/set_mode/enabled", "POST", 8.0)
        _http(host, port, "/api/move/goto", "POST", 20.0,
              body={"head_pose": hp, "duration": 0.45, "interpolation": "minjerk"})
        deadline = time.time() + 2.5
        while time.time() < deadline:
            running = _http(host, port, "/api/move/running", "GET", 4.0)
            if isinstance(running, list) and running:
                hp["pitch"] = pitch0
                try:
                    _http(host, port, "/api/move/goto", "POST", 20.0,
                          body={"head_pose": hp, "duration": 0.3,
                                "interpolation": "minjerk"})
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
                    pass
                return True
            pitch = float(_http(host, port, "/api/state/full", "GET", 4.0)
                           .get("head_pose", {}).get("pitch", pitch0))
            if abs(pitch - pitch0) >= threshold:
                hp["pitch"] = pitch0
                try:
                    _http(host, port, "/api/move/goto", "POST", 20.0,
                          body={"head_pose": hp, "duration": 0.3,
                                "interpolation": "minjerk"})
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
                    pass
                return True
            time.sleep(0.12)
        return False
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            TypeError, ValueError, KeyError):
        return True  # don't block wake/sleep on probe failure


def restart_daemon(host: str, port: int) -> bool:
    try:
        _http(host, port, "/api/daemon/restart", "POST", 30.0)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def wait_daemon(host: str, port: int, max_s: float = 120.0) -> bool:
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            st = _http(host, port, "/api/state/full", "GET", 4.0)
            if st.get("head_pose"):
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            pass
        time.sleep(5.0)
    return False


def ensure_motion_ready(host: str, port: int) -> str:
    """Revive silent backend if needed. Returns a short status note for logs."""
    if motion_actuates(host, port):
        return ""
    if not restart_daemon(host, port):
        return "motion backend may be stuck — try ./scripts/net-connect.sh --fix"
    if not wait_daemon(host, port):
        return "daemon restart timed out — try ./scripts/net-connect.sh --fix"
    try:
        _http(host, port, "/api/motors/set_mode/enabled", "POST", 10.0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass
    time.sleep(2.0)
    if motion_actuates(host, port):
        return "daemon restarted (motion backend was stuck)"
    return "daemon restarted — retry wake/sleep if head did not move"
