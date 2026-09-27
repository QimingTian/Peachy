#!/usr/bin/env python3
"""Peachy as your avatar somewhere else: its head moves with yours (AirPods head
tracking), your voice plays from its speaker (this Mac's built-in mic) and what
it hears plays in your AirPods (its mic).

  python scripts/ctl-avatar.py              # start; q or Ctrl-C to stop
  python scripts/ctl-avatar.py --no-audio   # head only

Nothing else runs meanwhile. The robot half, the Reachy Mini app
"peachy_avatar" (scripts/avatar-robot.sh install), takes over from whatever app
is running (the conversation included) and holds the robot, so the daemon does
not put Peachy to sleep and peachy-senses' room watch, Follow and wake word stay
paused. Head sync is scripts/ctl-airpods.py (same keys: c recenter, l level,
space pause, q quit; SIGUSR1 / SIGUSR2 from the console). Audio is one WebRTC
link to the daemon (avatar_audio.py).

Stopping glides the head home and stops the app, and Peachy stays awake with
the head at home, so Follow takes over. The daemon puts Peachy to sleep 1.5 s
after any app exits unless something talks to it on its WebRTC data channel, so
the audio link pokes it until that window has passed (with --no-audio the link
is still opened, just silent).

Env: REACHY_HOST (auto via hostfind), REACHY_PORT (8000)
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import sys
import threading
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hostfind import resolve_host  # noqa: E402

APP = "peachy_avatar"
RUNNING = ("starting", "running", "stopping", "error")
POKE_EVERY_S = 0.2
HOLD_AWAKE_S = 2.0          # after stop-current-app returns; the daemon's sleep-after-app waits 1.5 s


def _airpods():
    spec = importlib.util.spec_from_file_location("ctl_airpods", Path(__file__).with_name("ctl-airpods.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ap = _airpods()


def app_status(host: str, port: int) -> tuple[str | None, str | None, str]:
    st = ap.http_json(host, port, "/api/apps/current-app-status") or {}
    return (st.get("info") or {}).get("name"), st.get("state"), st.get("error") or ""


def take_robot(host: str, port: int) -> None:
    """Swap the running app for the avatar app, fast enough that the daemon's
    sleep-after-app (1.5 s after the old app exits) never starts."""
    try:
        name, state, _ = app_status(host, port)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SystemExit(f"Cannot reach the daemon at {host}:{port} ({e}). Try ./scripts/net-connect.sh")
    if name == APP and state in ("starting", "running"):
        return
    if state in RUNNING:
        print(f"stopping {name} ...", file=sys.stderr)
        try:
            # Returns once the app has exited and the daemon's 1 s return-to-zero is done.
            ap.http_json(host, port, "/api/apps/stop-current-app", "POST", timeout=40)
        except urllib.error.HTTPError as e:
            if e.code != 400:  # 400: already stopping
                raise
        for _ in range(100):
            name, state, _ = app_status(host, port)
            if state not in RUNNING:
                break
            time.sleep(0.05)
        else:
            raise SystemExit(f"{name} did not stop (still {state})")
    try:
        ap.http_json(host, port, f"/api/apps/start-app/{APP}", "POST", timeout=20)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        if e.code == 400 and "already running" in detail:
            raise SystemExit("another app started meanwhile; try again")
        raise SystemExit(f"could not start {APP}: {detail or e}. "
                         "Is it installed? ./scripts/avatar-robot.sh install")
    for _ in range(150):
        name, state, err = app_status(host, port)
        if name == APP and state == "running":
            return
        if state in ("error", "done", None):
            raise SystemExit(f"{APP} did not start: {err.strip()[-300:] or state}")
        time.sleep(0.1)
    raise SystemExit(f"{APP} is still starting after 15 s")


def give_robot(host: str, port: int, link) -> bool:
    """Stop the avatar app, poking the daemon over *link* (AvatarAudio) from before
    the app exits until its sleep-after-app has been called off. True if Peachy
    stays awake."""
    try:
        name, state, _ = app_status(host, port)
        if not (name == APP and state in ("starting", "running")):
            return False
        done, stopped = threading.Event(), threading.Event()
        late = [0]              # pokes after the app has gone

        def poke():
            while not done.wait(POKE_EVERY_S):
                if link is not None and link.poke() and stopped.is_set():
                    late[0] += 1

        threading.Thread(target=poke, name="avatar-poke", daemon=True).start()
        try:
            # Returns once the app has exited and the daemon's 1 s return-to-zero is done.
            ap.http_json(host, port, "/api/apps/stop-current-app", "POST", timeout=40)
            stopped.set()
            time.sleep(HOLD_AWAKE_S)
        finally:
            done.set()
        return late[0] >= HOLD_AWAKE_S / POKE_EVERY_S / 2
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"warning: could not stop {APP} ({e}); stop it from the console", file=sys.stderr)
        return False


def head_home(host: str, port: int) -> None:
    from head_pose import settle_home

    def http(h, p, path, method="GET", timeout=5.0, body=None):
        return ap.http_json(h, p, path, method, body, timeout)

    try:
        settle_home(http, host, port, dur=1.0)
    except (urllib.error.URLError, OSError, ValueError):
        pass


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-audio", action="store_true", help="head sync only")
    p.add_argument("--mic", help="this Mac's mic by name (default: the built-in one)")
    p.add_argument("--volume", type=float, default=1.0, help="Peachy's mic on this Mac, 0-2 (default 1)")
    p.add_argument("--no-body", action="store_true", help="keep the body still (head yaw clamps at 63 deg)")
    p.add_argument("--smooth", type=float, default=1.0, help="head One Euro smoothing (see ctl-airpods.py)")
    p.add_argument("--lead", type=float, default=0.0, help="head prediction in ms (see ctl-airpods.py)")
    p.add_argument("--status-file", help="write live status JSON here (the console reads it)")
    args = p.parse_args()

    def interrupt(*_):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)

    host = resolve_host()
    port = int(os.environ.get("REACHY_PORT", "8000"))
    audio = None
    try:
        take_robot(host, port)
        print("avatar app running on Peachy", file=sys.stderr)
        from avatar_audio import AvatarAudio
        audio = AvatarAudio(host, mic=args.mic, volume=args.volume,
                            send=not args.no_audio, recv=not args.no_audio).start()
        fargs = argparse.Namespace(
            dry_run=False, stop_app=False, keep_app=APP, no_body=args.no_body, smooth=args.smooth,
            lead=args.lead, flip=[], relative_tilt=False, status_file=args.status_file,
            extra_status=(lambda: {"audio": "off"}) if args.no_audio else audio.status)
        ap.follow(fargs)
    except KeyboardInterrupt:
        pass
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        awake = give_robot(host, port, audio)
        if audio is not None:
            audio.stop()
        if awake:
            head_home(host, port)
        if args.status_file:
            ap.write_status(Path(args.status_file), {"status": "ended", "at": time.time(), "pid": os.getpid(),
                                                     "after": "awake" if awake else "asleep"})
        print("avatar stopped — Peachy stays awake" if awake
              else "avatar stopped — the daemon puts Peachy to sleep", file=sys.stderr)


if __name__ == "__main__":
    main()
