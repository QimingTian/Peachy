#!/usr/bin/env python3
"""Safe body-yaw CLI — test turntable hold vs head-coupled motion.

Run only when Peachy is AWAKE with head UP (not sleep pose).

  python scripts/ctl-body-yaw.py status
  python scripts/ctl-body-yaw.py suite          # guided hypothesis test (~20s)
  python scripts/ctl-body-yaw.py body 20        # body-only goto +20° (hold head pose)
  python scripts/ctl-body-yaw.py head 15        # head-yaw only (control — did base follow?)
  python scripts/ctl-body-yaw.py hold 25        # goto +25° then hold — try twisting base by hand
  python scripts/ctl-body-yaw.py turntable   # body_yaw ONLY — head removed OK

Verbal checks (suite prints these):
  - Body command: did the BASE rotate on the desk?
  - Hold: with motors on, can you still spin the base freely by hand?
  - Head command: did the BASE rotate when only head yaw changed?

Env: REACHY_HOST, REACHY_PORT (8000)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hostfind import resolve_host

HOST = resolve_host()
PORT = int(os.environ.get("REACHY_PORT", "8000"))
_STATE = Path(__file__).resolve().parent.parent / ".run" / "reachy_toggle_state.json"
# Refuse moves if head pitch exceeds this (sleep pose is ~0.42 rad down).
_MAX_SLEEP_PITCH = float(os.environ.get("REACHY_BODY_MAX_PITCH", "0.20"))


def _http(path: str, method: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t = r.read().decode()
            return json.loads(t) if t else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} {path}: {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"Cannot reach daemon {HOST}:{PORT} ({e.reason})") from e


def _file_state() -> str | None:
    try:
        return json.loads(_STATE.read_text()).get("state")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _home_pose() -> tuple[dict, list] | None:
    from head_pose import resolve_home
    return resolve_home()


def full() -> dict:
    return _http("/api/state/full", "GET", timeout=8.0)


def present_body_yaw() -> float:
    return float(_http("/api/state/present_body_yaw", "GET", timeout=5.0))


def enable() -> None:
    _http("/api/motors/set_mode/enabled", "POST", timeout=10.0)


def wait_moves(max_s: float = 20.0) -> None:
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            running = _http("/api/move/running", "GET", timeout=4.0)
            if isinstance(running, list) and not running:
                return
        except SystemExit:
            return
        time.sleep(0.2)


def goto(*, body_yaw: float | None = None, head_pose: dict | None = None,
         duration: float = 1.5) -> None:
    payload: dict = {"duration": duration, "interpolation": "minjerk"}
    if body_yaw is not None:
        payload["body_yaw"] = body_yaw
    if head_pose is not None:
        payload["head_pose"] = head_pose
    _http("/api/move/goto", "POST", payload, timeout=max(30.0, duration + 15.0))


def goto_turntable(body_yaw: float, duration: float = 1.5) -> None:
    """Body yaw only — no head_pose in the REST payload (head-off chassis test)."""
    goto(body_yaw=body_yaw, duration=duration)


def _head_dict(st: dict) -> dict:
    return dict(st.get("head_pose") or {})


def _safe_to_move(st: dict | None = None) -> tuple[bool, str]:
    st = st or full()
    pitch = float(_head_dict(st).get("pitch", 0.0))
    if _file_state() == "asleep":
        return False, "file state is asleep — run: python scripts/ctl-toggle.py wake"
    if pitch > _MAX_SLEEP_PITCH:
        return False, (f"head pitch {math.degrees(pitch):+.1f}° looks too low — "
                       "wake and settle first")
    if st.get("control_mode") == "disabled":
        return False, "motors disabled — will enable before move"
    return True, "ok"


def _report_line(label: str, before: float, after: float, unit: str = "rad") -> None:
    d = after - before
    if unit == "deg":
        print(f"  {label}: {math.degrees(before):+.1f}° → {math.degrees(after):+.1f}°  "
              f"(Δ {math.degrees(d):+.1f}°)")
    else:
        print(f"  {label}: {before:+.4f} → {after:+.4f}  (Δ {d:+.4f} rad / "
              f"{math.degrees(d):+.1f}°)")


def cmd_status(_: argparse.Namespace) -> int:
    st = full()
    hp = _head_dict(st)
    by = present_body_yaw()
    ok, msg = _safe_to_move(st)
    print(f"Daemon {HOST}:{PORT}")
    print(f"  file state     : {_file_state() or '(unknown)'}")
    print(f"  control_mode   : {st.get('control_mode')}")
    print(f"  body commanded : {float(st.get('body_yaw') or 0):+.4f} rad "
          f"({math.degrees(float(st.get('body_yaw') or 0)):+.1f}°)")
    print(f"  body measured  : {by:+.4f} rad ({math.degrees(by):+.1f}°)")
    print(f"  head pitch     : {float(hp.get('pitch', 0)):+.4f} rad "
          f"({math.degrees(float(hp.get('pitch', 0))):+.1f}°)")
    hy = float(hp.get("yaw", 0))
    print(f"  head yaw       : {hy:+.4f} rad ({math.degrees(hy):+.1f}°)")
    print(f"  safe to test   : {'YES' if ok else 'NO — ' + msg}")
    return 0


def _require_safe() -> dict:
    st = full()
    ok, msg = _safe_to_move(st)
    if not ok and "will enable" not in msg:
        raise SystemExit(f"Unsafe to move: {msg}")
    if not ok:
        print(f"  ({msg})")
    return st


def cmd_turntable(args: argparse.Namespace) -> int:
    """Open-chassis test: command foot motor only (no head_pose in API call)."""
    print("\n→ TURNTABLE-ONLY test (head off OK — foot motor / body_yaw only)")
    print("  Keep the head assembly clear of moving parts.\n")
    enable()
    wait_moves()
    base = present_body_yaw()
    target = base + math.radians(args.deg)
    print(f"  turn +{args.deg:.0f}° …")
    goto_turntable(target, duration=args.dur)
    wait_moves()
    time.sleep(args.dur + 0.5)
    by1 = present_body_yaw()
    _report_line("body (software)", base, by1)
    print("\n  >>> WATCH NOW: did the base plate physically rotate?")
    print("  >>> Try a gentle twist by hand — stiff or free spin?")
    time.sleep(max(0.0, args.pause))
    print(f"\n  return to center …")
    goto_turntable(0.0, duration=args.dur)
    wait_moves()
    time.sleep(args.dur + 0.4)
    print(f"  body now: {math.degrees(present_body_yaw()):+.1f}°")
    print("\n  If the plate rotated and holds torque → safe to close up.")
    print("  Emergency stop: curl -X POST http://<host>:8000/api/motors/set_mode/disabled")
    return 0


def cmd_body(args: argparse.Namespace) -> int:
    st = _require_safe()
    hp = _head_dict(st)
    enable()
    wait_moves()
    base = present_body_yaw()
    yaw0 = float(hp.get("yaw", 0.0))
    target = base + math.radians(args.deg)
    print(f"\n→ body-only goto {args.deg:+.0f}° (holding current head pose)")
    goto(body_yaw=target, head_pose=hp, duration=args.dur)
    wait_moves()
    time.sleep(args.dur + 0.4)
    st2 = full()
    by2 = present_body_yaw()
    yaw2 = float(_head_dict(st2).get("yaw", 0.0))
    _report_line("body (software)", base, by2)
    _report_line("head yaw (world)", yaw0, yaw2)
    print("\n  REPORT: Did the physical BASE rotate on the desk?")
    print("  REPORT: Can you spin the base by hand right now (motors on)?")
    return 0


def cmd_head(args: argparse.Namespace) -> int:
    st = _require_safe()
    hp = _head_dict(st)
    enable()
    wait_moves()
    by0 = present_body_yaw()
    yaw0 = float(hp.get("yaw", 0.0))
    target_yaw = yaw0 + math.radians(args.deg)
    hp_move = dict(hp)
    hp_move["yaw"] = target_yaw
    print(f"\n→ head-yaw only {args.deg:+.0f}° (body not commanded)")
    goto(head_pose=hp_move, duration=args.dur)
    wait_moves()
    time.sleep(args.dur + 0.4)
    st2 = full()
    by2 = present_body_yaw()
    yaw2 = float(_head_dict(st2).get("yaw", 0.0))
    _report_line("body (software)", by0, by2)
    _report_line("head yaw (world)", yaw0, yaw2)
    print("\n  REPORT: Did the physical BASE rotate when only HEAD moved?")
    print("  (If yes → body follows head coupling / automatic body yaw.)")
    return 0


def cmd_hold(args: argparse.Namespace) -> int:
    st = _require_safe()
    hp = _head_dict(st)
    enable()
    wait_moves()
    base = present_body_yaw()
    target = base + math.radians(args.deg)
    print(f"\n→ body goto {args.deg:+.0f}° then HOLD (motors stay enabled)")
    goto(body_yaw=target, head_pose=hp, duration=args.dur)
    wait_moves()
    time.sleep(args.dur + 0.3)
    by_cmd = present_body_yaw()
    print(f"  body measured after move: {math.degrees(by_cmd):+.1f}°")
    print("\n  *** TRY NOW: twist the base left/right with your hand ***")
    print("  Hold test:")
    print("    • Stiff / resists  → turntable motor IS holding torque")
    print("    • Spins freely     → horn/screw/coupling or motor not holding")
    if args.wait > 0:
        print(f"  (waiting {args.wait:.0f}s — twist now, then we re-read encoder…)")
        time.sleep(args.wait)
        by_after = present_body_yaw()
        _report_line("body after hand twist", by_cmd, by_after)
        if abs(by_after - by_cmd) > math.radians(2):
            print("  → Encoder changed — you moved it; software sees the new angle.")
        else:
            print("  → Encoder unchanged — either you didn't move it or no feedback.")
    return 0


def cmd_center(_: argparse.Namespace) -> int:
    enable()
    wait_moves()
    home = _home_pose()
    print("\n→ center: body 0° + saved head home")
    if home:
        hp, ant = home
        goto(body_yaw=0.0, head_pose=hp, duration=1.2)
    else:
        goto(body_yaw=0.0, duration=1.2)
    wait_moves()
    time.sleep(1.5)
    st = full()
    print(f"  body: {math.degrees(present_body_yaw()):+.1f}°  "
          f"head pitch: {math.degrees(float(_head_dict(st).get('pitch', 0))):+.1f}°")
    return 0


def cmd_suite(_: argparse.Namespace) -> int:
    print("=" * 52)
    print("  BODY YAW HYPOTHESIS TEST")
    print("  (awake, head up, small moves, then center)")
    print("=" * 52)
    cmd_status(_)
    _require_safe()
    enable()
    time.sleep(0.4)

    st = full()
    hp = _head_dict(st)
    by0 = present_body_yaw()
    hy0 = float(hp.get("yaw", 0.0))

    # 1 — body only
    print("\n--- Step 1/3: BODY command (+15°) ---")
    goto(body_yaw=by0 + math.radians(15), head_pose=hp, duration=1.5)
    wait_moves()
    time.sleep(1.9)
    by1 = present_body_yaw()
    hy1 = float(_head_dict(full()).get("yaw", 0.0))
    _report_line("body", by0, by1)
    _report_line("head yaw", hy0, hy1)
    print("  REPORT A: Did the BASE rotate on the desk?")
    print("  REPORT B: Twist base by hand — free spin or stiff?")

    time.sleep(2.0)
    by_hand = present_body_yaw()
    if abs(by_hand - by1) > math.radians(3):
        print(f"  (encoder drifted to {math.degrees(by_hand):+.1f}° — hand moved it)")

    # 2 — head only
    print("\n--- Step 2/3: HEAD yaw only (+15°) ---")
    st = full()
    hp = _head_dict(st)
    by2a = present_body_yaw()
    hy2a = float(hp.get("yaw", 0.0))
    hp2 = dict(hp)
    hp2["yaw"] = hy2a + math.radians(15)
    goto(head_pose=hp2, duration=1.5)
    wait_moves()
    time.sleep(1.9)
    by2b = present_body_yaw()
    hy2b = float(_head_dict(full()).get("yaw", 0.0))
    _report_line("body", by2a, by2b)
    _report_line("head yaw", hy2a, hy2b)
    print("  REPORT C: Did the BASE rotate when only HEAD moved?")

    # 3 — center
    print("\n--- Step 3/3: CENTER ---")
    cmd_center(_)

    print("\n" + "=" * 52)
    print("  INTERPRETATION (tell me A/B/C):")
    print("  • A=no base move, B=free spin, C=base follows head")
    print("    → software OK, turntable NOT holding (horn/screw/motor)")
    print("  • A=base moves, B=stiff")
    print("    → body yaw works; dashboard may need different command mode")
    print("  • body software Δ large but A=no physical move")
    print("    → encoder/joint moves, coupling slips on base (mechanical)")
    print("=" * 52)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Safe Reachy Mini body-yaw CLI")
    ap.add_argument("--dur", type=float, default=1.5, help="move duration seconds")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="read state + safety gate").set_defaults(func=cmd_status)
    sub.add_parser("suite", help="full hypothesis test").set_defaults(func=cmd_suite)
    sub.add_parser("center", help="body 0 + head home").set_defaults(func=cmd_center)

    p_hold = sub.add_parser("hold", help="goto offset and hold — hand twist test")
    p_hold.add_argument("deg", type=float, help="offset degrees from current body yaw")
    p_hold.add_argument("--wait", type=float, default=8.0,
                        help="seconds to wait for hand twist before re-read")
    p_hold.set_defaults(func=cmd_hold)

    p_body = sub.add_parser("body", help="body-only goto (hold head pose)")
    p_body.add_argument("deg", type=float, help="delta degrees")
    p_body.set_defaults(func=cmd_body)

    p_head = sub.add_parser("head", help="head-yaw only goto")
    p_head.add_argument("deg", type=float, help="delta degrees")
    p_head.set_defaults(func=cmd_head)

    p_tt = sub.add_parser("turntable",
                          help="body_yaw ONLY — for open chassis (head removed)")
    p_tt.add_argument("--deg", type=float, default=30.0,
                      help="degrees to turn (default 30)")
    p_tt.add_argument("--pause", type=float, default=5.0,
                      help="seconds to watch/hold before returning to 0")
    p_tt.set_defaults(func=cmd_turntable)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
