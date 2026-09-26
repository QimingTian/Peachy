#!/usr/bin/env python3
"""Post-repair / pre-class smoke panel — runs the main motion checks in order.

  python scripts/diag-panel.py           # full panel (~45s, moves robot)
  python scripts/diag-panel.py --quick   # status + diag-yaw only (~20s)
  python scripts/diag-panel.py --no-move # read-only status lines

Wakes first if the file state says asleep. Ends awake at head home.
Env: REACHY_HOST, REACHY_PORT
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hostfind import resolve_host

_REPO = Path(__file__).resolve().parent.parent
_STATE = _REPO / ".run" / "reachy_toggle_state.json"
HOST = resolve_host()
PORT = int(os.environ.get("REACHY_PORT", "8000"))
_PY = sys.executable


def _http(path: str, method: str = "GET", body: dict | None = None, timeout: float = 20.0) -> dict:
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


def _run(label: str, argv: list[str]) -> tuple[int, str]:
    print(f"\n{'─' * 52}\n▶ {label}\n{'─' * 52}")
    p = subprocess.run(argv, cwd=_REPO, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    print(out.rstrip())
    return p.returncode, out


def _file_state() -> str | None:
    try:
        return json.loads(_STATE.read_text()).get("state")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _ensure_awake() -> None:
    if _file_state() != "asleep":
        return
    print("→ waking (file state asleep)…")
    subprocess.run([_PY, "scripts/ctl-toggle.py", "wake"], cwd=_REPO, check=False)
    time.sleep(1.5)


def _status_block() -> dict:
    live = _http("/api/state/full")
    by = float(_http("/api/state/present_body_yaw", timeout=5.0))
    hp = live.get("head_pose") or {}
    return {
        "control_mode": live.get("control_mode"),
        "body_deg": math.degrees(by),
        "pitch_deg": math.degrees(float(hp.get("pitch", 0))),
        "z_m": float(hp.get("z", 0)),
        "file_state": _file_state(),
    }


def _reset_awake() -> None:
    """Motors on + saved head home (after diag-yaw disables / zeros head)."""
    _http("/api/motors/set_mode/enabled", "POST", timeout=10.0)
    time.sleep(0.4)
    from head_pose import resolve_home
    home = resolve_home()
    if home:
        hp, ant = home
        _http("/api/move/goto", "POST",
              {"head_pose": hp, "antennas": ant,
               "duration": 1.0, "interpolation": "minjerk"}, timeout=30.0)
        time.sleep(1.3)


def _sleep_pose_check() -> tuple[bool, str]:
    """Sleep via ctl-toggle, compare live pose to calibration."""
    cal = json.loads(_STATE.read_text()).get("calibration", {}).get("sleep", {})
    if not cal:
        return False, "no sleep calibration"
    subprocess.run([_PY, "scripts/ctl-toggle.py", "sleep"], cwd=_REPO, check=False)
    time.sleep(1.0)
    live = _http("/api/state/full")
    hp = live.get("head_pose") or {}
    tgt = cal.get("head_pose") or {}
    dz = abs(float(hp.get("z", 0)) - float(tgt.get("z", 0)))
    dp = abs(float(hp.get("pitch", 0)) - float(tgt.get("pitch", 0)))
    ok = dz < 0.004 and dp < 0.05
    msg = (f"sleep pose  pitch={math.degrees(float(hp.get('pitch', 0))):+.1f}° "
           f"z={float(hp.get('z', 0)):+.4f}m  "
           f"(Δpitch={math.degrees(dp):.1f}° Δz={dz*1000:.1f}mm)")
    subprocess.run([_PY, "scripts/ctl-toggle.py", "wake"], cwd=_REPO, check=False)
    time.sleep(1.5)
    return ok, msg


def main() -> int:
    ap = argparse.ArgumentParser(description="Reachy Mini post-repair test panel")
    ap.add_argument("--quick", action="store_true", help="status + diag-yaw only")
    ap.add_argument("--no-move", action="store_true", help="status lines only")
    args = ap.parse_args()

    print(f"Peachy test panel  →  {HOST}:{PORT}")
    results: list[tuple[str, bool, str]] = []

    try:
        st = _status_block()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"✗ cannot reach daemon: {e}")
        return 1

    print(f"  file={st['file_state']}  mode={st['control_mode']}  "
          f"body={st['body_deg']:+.1f}°  pitch={st['pitch_deg']:+.1f}°  z={st['z_m']:+.4f}m")
    results.append(("daemon reachable", True, f"{HOST}:{PORT}"))

    if args.no_move:
        for name, ok, detail in results:
            print(f"{'✓' if ok else '✗'} {name}: {detail}")
        return 0

    _ensure_awake()
    _reset_awake()

    rc, out = _run("ctl-toggle status", [_PY, "scripts/ctl-toggle.py", "status"])
    results.append(("toggle status", rc == 0, "ok" if rc == 0 else f"exit {rc}"))

    if not args.quick:
        rc, out = _run("body-yaw suite", [_PY, "scripts/ctl-body-yaw.py", "suite"])
        suite_ok = rc == 0 and "Unsafe" not in out
        results.append(("body-yaw suite", suite_ok, "ran — confirm base moved + resists twist"))

    rc, out = _run("body-yaw diag-yaw", [_PY, "scripts/diag-yaw.py", "--amp", "0.25"])
    body_tracks = "BODY-YAW: TRACKS" in out
    head_tracks = "HEAD-YAW (control): TRACKS" in out
    diag_ok = rc == 0 and body_tracks and head_tracks
    results.append(("diag-yaw", diag_ok,
                    "body+head track" if diag_ok else "see verdict above"))

    _reset_awake()

    if not args.quick:
        ok, msg = _sleep_pose_check()
        results.append(("gentle sleep pose", ok, msg))

        rc, _ = _run("center + status", [_PY, "scripts/ctl-body-yaw.py", "center"])
        results.append(("center home", rc == 0, "body 0° + head home"))

    print(f"\n{'=' * 52}\n  SUMMARY\n{'=' * 52}")
    fails = 0
    for name, ok, detail in results:
        print(f"  {'✓' if ok else '✗'} {name:<18} {detail}")
        if not ok:
            fails += 1
    print(f"\n  {len(results) - fails}/{len(results)} passed")
    if not args.quick:
        print("  Manual: during suite, did the BASE rotate and resist hand twist?")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
