#!/usr/bin/env python3
"""Warm-up bridge: stretch, greet, and fill ~10–15s while conversation boots.

Runs on AWAKE (lights + speech) before the conversation app is ready so visitors
aren't staring at a silent robot. Uses Portal-turret WAVs + macOS TTS on Peachy's
speaker (same pattern as cal-light).

  python scripts/warmup_bridge.py              # live test (motors + audio)
  python scripts/warmup_bridge.py --dry-run    # print the sequence only

Env:
  REACHY_WARMUP=1                 enable in watch-room (default on)
  REACHY_WARMUP_S=19              target seconds before handoff (was 14)
  REACHY_WARMUP_TTS=1             macOS say during bridge (0=turret only)
  REACHY_WARMUP_MOTION=1          look-around / neck roll / body sway while waiting
  REACHY_WARMUP_MIN_READY_S=5     keep stalling after convo ready before handoff
  REACHY_WARMUP_OPENING=…         override opening TTS line
  REACHY_CONVO=1                  start conversation app during bridge
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CONVO_PID = _REPO / ".run" / "conversation_app.pid"
_CONVO_SH = _REPO / "scripts" / "app-conversation.sh"
_TURRET_PACK = "portalturret"

WARMUP_ON = os.environ.get("REACHY_WARMUP", "1") != "0"
WARMUP_S = float(os.environ.get("REACHY_WARMUP_S", "19"))
MIN_READY_S = float(os.environ.get("REACHY_WARMUP_MIN_READY_S", "5"))
TTS_ON = os.environ.get("REACHY_WARMUP_TTS", "1") != "0"
MOTION_ON = os.environ.get("REACHY_WARMUP_MOTION", "1") != "0"
LOCAL_AUDIO = os.environ.get("REACHY_WARMUP_LOCAL_AUDIO", "0") != "0"
CONVO_ON = os.environ.get("REACHY_CONVO", "1") != "0"
DEFAULT_CONVO_VOICE = os.environ.get("REACHY_CONVO_VOICE", "ballad")
OPENING = os.environ.get(
    "REACHY_WARMUP_OPENING",
    "Hi! Have we met?",
)
WARMUP_MSG = (
    "Conversation mode is starting shortly — give me a moment while I get ready."
)
STARTING_SOON = os.environ.get(
    "REACHY_WARMUP_STARTING",
    "Still waking up my conversation brain — almost there.",
)
COUNTDOWN = "Three. Two. One."
HOWTO = (
    "When I'm listening, talk to me like a friend. "
    "Wait for me to finish, then jump in."
)
READY = "Okay — I'm listening. Go ahead!"

_READY_LOG = re.compile(
    r"Realtime session (restarted and )?connected|listening — talk to Peachy",
    re.I,
)
_PITCH_ROLL = math.radians(38)
_HEAD_YAW = math.radians(60)
_READY_EXPR = ("lookaround", "stretch", "curious", "listen")


def _http(host: str, port: int, path: str, method: str = "GET",
          body: dict | None = None, timeout: float = 30.0) -> dict:
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def _enable_motors(host: str, port: int) -> None:
    try:
        _http(host, port, "/api/motors/set_mode/enabled", "POST", timeout=8.0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass


def _clamp_head(hp: dict) -> dict:
    hp = dict(hp)
    hp["pitch"] = max(-_PITCH_ROLL, min(_PITCH_ROLL, hp.get("pitch", 0.0)))
    hp["roll"] = max(-_PITCH_ROLL, min(_PITCH_ROLL, hp.get("roll", 0.0)))
    hp["yaw"] = max(-_HEAD_YAW, min(_HEAD_YAW, hp.get("yaw", 0.0)))
    return hp


def _goto(host: str, port: int, *, head_pose: dict | None = None,
          body_yaw: float | None = None, antennas: list[float] | None = None,
          duration: float = 0.85) -> None:
    payload: dict = {"duration": duration, "interpolation": "minjerk"}
    if head_pose is not None:
        payload["head_pose"] = _clamp_head(head_pose)
    if body_yaw is not None:
        payload["body_yaw"] = body_yaw
    if antennas is not None:
        payload["antennas"] = antennas
    _http(host, port, "/api/move/goto", "POST", payload, timeout=max(20.0, duration + 10.0))


def _present_body_yaw(host: str, port: int) -> float:
    try:
        return float(_http(host, port, "/api/state/present_body_yaw", timeout=5.0))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            TypeError, ValueError):
        return 0.0


def _load_home() -> tuple[dict, list[float]] | None:
    sys.path.insert(0, str(_REPO / "scripts"))
    from head_pose import resolve_home

    return resolve_home()


def _body_sway(host: str, port: int, home_hp: dict, home_ant: list[float]) -> None:
    """Small turntable wiggle while the conversation app boots."""
    base = _present_body_yaw(host, port)
    for delta in (16, -16, 0):
        _goto(host, port, body_yaw=base + math.radians(delta),
              head_pose=home_hp, antennas=home_ant, duration=0.95)
        time.sleep(1.0)


def _neck_roll(host: str, port: int, home_hp: dict, home_ant: list[float]) -> None:
    """Quick neck stretch — roll side to side."""
    base = dict(home_hp)
    a_l, a_r = home_ant
    perk = [a_l + 0.35, a_r - 0.35]
    for roll_deg in (14, -14, 0):
        hp = dict(base)
        hp["roll"] = base.get("roll", 0.0) + math.radians(roll_deg)
        _goto(host, port, head_pose=hp, antennas=perk, duration=0.55)
        time.sleep(0.58)


def _play_turret(host: str, port: int, sid: str, *, wait_s: float = 2.2) -> bool:
    sys.path.insert(0, str(_REPO / "scripts"))
    from sound_sync import load_manifest, play_file, sync_pack

    try:
        manifest = load_manifest(_TURRET_PACK)
        meta = manifest.get("sounds", {}).get(sid)
        if not meta:
            return False
        sync_pack(host, port, _TURRET_PACK, only=[sid])
        play_file(host, port, meta["file"])
        print(f"  ♪ {sid.replace('_', ' ')}")
        time.sleep(wait_s)
        return True
    except Exception as e:
        print(f"  (turret {sid} failed: {e})")
        return False


def _warmup_turret_cues() -> dict[str, str]:
    manifest_path = _REPO / "sounds" / _TURRET_PACK / "manifest.json"
    defaults = {
        "opening": "whos_there",
        "waiting": "searching",
        "countdown": "deploying",
        "ready": "activated",
    }
    try:
        data = json.loads(manifest_path.read_text())
        cues = data.get("warmup_bridge", {})
        if isinstance(cues, dict):
            return {**defaults, **{k: str(v) for k, v in cues.items()}}
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    return defaults


def _run_expression(name: str) -> None:
    py = sys.executable
    script = _REPO / "scripts" / "ctl-express.py"
    print(f"  → {name}")
    subprocess.run(
        [py, str(script), "express", name],
        cwd=_REPO,
        capture_output=True,
        timeout=45,
        check=False,
    )


def _conversation_running() -> bool:
    try:
        pid = int(_CONVO_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass
    try:
        p = subprocess.run(
            [str(_CONVO_SH), "status"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=12,
        )
        return p.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _tail_convo_log(n: int = 60) -> str:
    try:
        p = subprocess.run(
            [str(_CONVO_SH), "log", str(n)],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return p.stdout or ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _conversation_ready() -> bool:
    if not _conversation_running():
        return False
    log = _tail_convo_log(80)
    if _READY_LOG.search(log):
        return True
    base = os.environ.get("PEACHY_CONVERSATION_GRADIO_URL", "http://127.0.0.1:7860").rstrip("/")
    for path in ("/voices", "/personalities"):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=2.5) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            continue
    # PID up for a while — good enough if we've already filled the minimum beat.
    return False


def _start_conversation_async() -> None:
    if not CONVO_ON:
        return
    if _conversation_running():
        print("  (conversation already running)")
        return
    subprocess.Popen(
        [str(_CONVO_SH), "start"],
        cwd=_REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("  (starting conversation app…)")


def _apply_convo_voice(voice: str = DEFAULT_CONVO_VOICE) -> bool:
    """Set Ballad (or REACHY_CONVO_VOICE) once :7860 is up."""
    import urllib.parse

    base = os.environ.get("PEACHY_CONVERSATION_GRADIO_URL", "http://127.0.0.1:7860").rstrip("/")
    q = urllib.parse.urlencode({"voice": voice})
    for attempt in range(12):
        if attempt:
            time.sleep(1.5)
        try:
            req = urllib.request.Request(
                f"{base}/voices/apply?{q}",
                method="POST",
                data=b"{}",
            )
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read().decode()
                data = json.loads(raw) if raw else {}
            if isinstance(data, dict) and data.get("ok") is False:
                continue
            print(f"  (conversation voice → {voice})")
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                json.JSONDecodeError):
            continue
    print(f"  (voice apply failed — still starting as saved default)")
    return False


def _wait_with_readying(
    host: str,
    port: int,
    deadline: float,
    cues: dict,
    voice,
    home: tuple[dict, list[float]] | None,
) -> bool:
    """Fill the boot wait with motion, turret cues, and spoken progress."""
    t0 = time.time()
    last_move = 0.0
    said_soon = False
    move_i = 0
    home_hp, home_ant = home if home else ({}, [0.0, 0.0])

    while time.time() < deadline:
        if _conversation_ready():
            return True

        elapsed = time.time() - t0
        if TTS_ON and not said_soon and elapsed > 3.5:
            _say(voice, STARTING_SOON)
            said_soon = True

        if MOTION_ON and home and time.time() - last_move > 3.2:
            kind = move_i % 4
            if kind == 0:
                _run_expression(_READY_EXPR[move_i // 4 % len(_READY_EXPR)])
            elif kind == 1:
                _neck_roll(host, port, home_hp, home_ant)
            elif kind == 2:
                _body_sway(host, port, home_hp, home_ant)
            else:
                _play_turret(host, port, cues["waiting"], wait_s=1.2)
            last_move = time.time()
            move_i += 1
        elif not MOTION_ON and move_i == 0:
            _play_turret(host, port, cues["waiting"], wait_s=1.6)
            move_i += 1

        time.sleep(0.35)

    return _conversation_ready()


def _apply_speaker_volume(host: str, port: int, vol: int = 100) -> None:
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/volume/set",
            method="POST",
            data=json.dumps({"volume": vol}).encode(),
        )
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=8)
        print(f"  (speaker {vol}%)")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass


def _say(voice, text: str) -> None:
    if not TTS_ON or not text:
        return
    from spoken_line import SpokenLine

    voice.say(text)
    time.sleep(SpokenLine.estimate_duration(text) * 0.92)


def _finish_handoff(host: str, port: int, cues: dict, voice) -> None:
    """Apply Ballad, optional extra stall, turret ready cue."""
    if CONVO_ON:
        _apply_convo_voice()
    if MIN_READY_S > 0:
        _play_turret(host, port, cues["waiting"], wait_s=min(MIN_READY_S, 3.0))
        remain = max(0.0, MIN_READY_S - 3.0)
        if remain > 0:
            time.sleep(remain)
    if TTS_ON:
        _say(voice, HOWTO)
    _play_turret(host, port, cues["ready"], wait_s=1.6)
    if TTS_ON:
        _say(voice, READY)


def run_warmup_bridge(
    host: str,
    port: int,
    *,
    dry_run: bool = False,
    convo_on: bool | None = None,
) -> None:
    """Stretch → greet → boot conversation → verbal bridge → handoff."""
    use_convo = CONVO_ON if convo_on is None else convo_on
    cues = _warmup_turret_cues()

    if dry_run:
        mode = "turret+tts" if TTS_ON else "turret only"
        print(f"  [warmup dry-run — {mode}]")
        for line in (
            "stretch",
            f"turret:{cues['opening']}",
            *( [f"tts:{OPENING}"] if TTS_ON else [] ),
            "listen pose",
            "start conversation" if use_convo else "(convo off)",
            *( [f"tts:{WARMUP_MSG}"] if TTS_ON else [f"turret:{cues['waiting']}"] ),
            "readying loop: lookaround / neck roll / body sway / turret",
            *( [f"tts:{STARTING_SOON}"] if TTS_ON else [] ),
            f"wait ready (up to {WARMUP_S:.0f}s)",
            *( [f"tts:{COUNTDOWN}"] if TTS_ON else [f"turret:{cues['countdown']}"] ),
            f"apply voice {DEFAULT_CONVO_VOICE}",
            f"stall {MIN_READY_S:.0f}s after ready",
            f"turret:{cues['ready']}",
            *( [f"tts:{READY}"] if TTS_ON else [] ),
        ):
            print(f"    · {line}")
        return

    sys.path.insert(0, str(_REPO / "scripts"))
    from spoken_line import SpokenLine

    voice = SpokenLine(host, port, local=LOCAL_AUDIO)
    if TTS_ON:
        voice.prepare([OPENING, WARMUP_MSG, STARTING_SOON, COUNTDOWN, HOWTO, READY])
    _apply_speaker_volume(host, port)
    _enable_motors(host, port)
    home = _load_home()

    if use_convo and _conversation_ready():
        print("  (conversation already ready — quick hello)")
        _run_expression("listen")
        _play_turret(host, port, cues["opening"], wait_s=1.8)
        _say(voice, OPENING)
        _apply_convo_voice()
        _play_turret(host, port, cues["ready"], wait_s=1.6)
        _say(voice, READY)
        return

    t0 = time.time()
    deadline = t0 + WARMUP_S

    _run_expression("stretch")
    _play_turret(host, port, cues["opening"])
    _say(voice, OPENING)

    if use_convo:
        _start_conversation_async()

    _run_expression("listen")
    if TTS_ON:
        _say(voice, WARMUP_MSG)
    else:
        _play_turret(host, port, cues["waiting"], wait_s=1.8)

    _wait_with_readying(host, port, deadline, cues, voice, home)

    if TTS_ON:
        _say(voice, COUNTDOWN)
    else:
        _play_turret(host, port, cues["countdown"], wait_s=1.8)

    _finish_handoff(host, port, cues, voice)
    elapsed = time.time() - t0
    print(f"  (warmup bridge done — {elapsed:.1f}s)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reachy AWAKE → conversation warm-up bridge")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-convo", action="store_true", help="skip starting conversation app")
    ap.add_argument("--host", default=os.environ.get("REACHY_HOST"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("REACHY_PORT", "8000")))
    args = ap.parse_args()

    sys.path.insert(0, str(_REPO / "scripts"))
    from hostfind import resolve_host

    host = args.host or resolve_host()
    run_warmup_bridge(
        host,
        args.port,
        dry_run=args.dry_run,
        convo_on=not args.no_convo,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
