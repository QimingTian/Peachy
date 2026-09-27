"""Peachy avatar, the robot half: a Reachy Mini app that holds the robot for
scripts/ctl-avatar.py on the Mac (AirPods head sync + two-way audio).

While a daemon app runs, the daemon leaves the robot alone; with no app it
puts the robot to sleep 1.5 s after the last one stops. This app moves nothing
itself: it keeps the motors on, marks Peachy awake and keeps peachy-senses'
room watch, Follow and wake word paused (POST /busy), so the Mac can drive the
head over the daemon's set_target socket and talk through the daemon's WebRTC
audio.

Installed into /venvs/apps_venv by scripts/avatar-robot.sh.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from reachy_mini import ReachyMiniApp

DAEMON = "http://127.0.0.1:8000"
SENSES = "http://127.0.0.1:8767"
TOKEN_FILE = Path.home() / ".peachy" / "senses_token"
BUSY_EVERY_S = 5.0
BUSY_TTL_S = 20.0


def post(url: str, body: dict | None = None, headers: dict | None = None) -> None:
    try:
        req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        urllib.request.urlopen(req, timeout=5).read()
    except OSError:
        pass


def senses(path: str, body: dict) -> None:
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return
    post(SENSES + path, body, {"X-Peachy-Senses": token})


class PeachyAvatar(ReachyMiniApp):
    def wrapped_run(self, *args, **kwargs) -> None:
        # No ReachyMini(): its "no_media" mode makes the daemon release the
        # camera and audio, which ends the daemon's WebRTC (the avatar's audio),
        # and every other mode opens media this app doesn't use.
        self.run(None, self.stop_event)

    def run(self, reachy_mini, stop_event: threading.Event) -> None:
        post(DAEMON + "/api/motors/set_mode/enabled")
        senses("/state", {"state": "awake"})
        try:
            while True:
                senses("/busy", {"on": True, "ttl": BUSY_TTL_S})
                if stop_event.wait(BUSY_EVERY_S):
                    break
        finally:
            senses("/busy", {"on": False})


if __name__ == "__main__":
    app = PeachyAvatar()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
