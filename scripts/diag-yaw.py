#!/usr/bin/env python3
"""Body-yaw diagnostic over the LAN (no SSH, no opening the robot).

Commands small yaw moves via the daemon REST API and compares COMMANDED vs
MEASURED (`present_body_yaw`). Also moves HEAD yaw as a control test so we can
tell "whole motor path dead" from "only the body-yaw servo".

Verdict logic:
  - head yaw tracks + body yaw tracks .......... body-yaw motor is FINE
       (if it tracks in software but the base doesn't physically turn →
        MECHANICAL: yaw horn / grub-screw / coupling loose, not wiring)
  - head yaw tracks + body yaw does NOT ........ body-yaw servo not responding
       → ELECTRICAL: that servo / its daisy-chain cable
  - neither tracks ............................. motors not enabling or whole
       Dynamixel bus down — recheck with daemon stopped (reachyminios_check)

Small amplitudes, returns to centre, read-mostly. Safe to run repeatedly.

  python scripts/diag-yaw.py
  python scripts/diag-yaw.py --amp 0.20 --no-restore
Env: REACHY_HOST (auto via hostfind), REACHY_PORT (8000)
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hostfind import resolve_host
HOST = resolve_host()
PORT = int(os.environ.get("REACHY_PORT", "8000"))


def _http(path: str, method: str, body: dict | None = None, timeout: float = 30.0):
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
        raise SystemExit(f"Cannot reach daemon {HOST}:{PORT} ({e.reason}). "
                         f"Try REACHY_HOST=reachy-mini.local") from e


def present_body_yaw() -> float:
    return float(_http("/api/state/present_body_yaw", "GET", timeout=5.0))


def full() -> dict:
    return _http("/api/state/full", "GET", timeout=5.0)


def goto(*, body_yaw=None, head_pose=None, duration=1.2):
    payload = {"duration": duration, "interpolation": "minjerk"}
    if body_yaw is not None:
        payload["body_yaw"] = body_yaw
    if head_pose is not None:
        payload["head_pose"] = head_pose
    _http("/api/move/goto", "POST", payload)


def _settle(duration: float):
    time.sleep(duration + 0.6)


def _axis_test(name: str, amp: float, dur: float, command, measure) -> bool:
    base = measure()
    print(f"\n[{name}] baseline measured = {base:+.4f} rad")
    moved = []
    for target in (+amp, -amp, 0.0):
        command(target)
        _settle(dur)
        m = measure()
        d = m - base
        print(f"  commanded {target:+.3f}  →  measured {m:+.4f}  (Δ {d:+.4f})")
        if abs(target) > 1e-6:
            moved.append(abs(d))
    tracks = any(x >= amp * 0.4 for x in moved)
    print(f"  {name}: {'TRACKS ✔' if tracks else 'NO RESPONSE ✘'} "
          f"(max |Δ| {max(moved):.4f} vs amp {amp:.3f})")
    return tracks


def main() -> int:
    ap = argparse.ArgumentParser(description="Reachy body-yaw network diagnostic")
    ap.add_argument("--amp", type=float, default=0.15,
                    help="test amplitude rad (default 0.15 ≈ 8.6°)")
    ap.add_argument("--dur", type=float, default=1.2, help="move duration s")
    ap.add_argument("--skip-head", action="store_true",
                    help="skip the head-yaw control test")
    ap.add_argument("--no-restore", action="store_true",
                    help="leave motors enabled / don't recentre at end")
    args = ap.parse_args()

    print(f"Daemon {HOST}:{PORT}")
    st = full()
    print(f"control_mode={st.get('control_mode')}  "
          f"body_yaw(cmd)={st.get('body_yaw')}  "
          f"present_body_yaw={present_body_yaw():+.4f}")

    print("\nEnabling motors…")
    _http("/api/motors/set_mode/enabled", "POST")
    time.sleep(0.5)

    head_ok = None
    if not args.skip_head:
        head_ok = _axis_test(
            "HEAD-YAW (control)", args.amp, args.dur,
            command=lambda y: goto(head_pose={"x": 0, "y": 0, "z": 0,
                                              "roll": 0, "pitch": 0, "yaw": y},
                                   duration=args.dur),
            measure=lambda: float(full().get("head_pose", {}).get("yaw", 0.0)),
        )

    body_ok = _axis_test(
        "BODY-YAW", args.amp, args.dur,
        command=lambda y: goto(body_yaw=y, duration=args.dur),
        measure=present_body_yaw,
    )

    if not args.no_restore:
        print("\nRecentring & disabling motors…")
        goto(body_yaw=0.0, head_pose={"x": 0, "y": 0, "z": 0,
                                      "roll": 0, "pitch": 0, "yaw": 0},
             duration=args.dur)
        _settle(args.dur)
        _http("/api/motors/set_mode/disabled", "POST")

    print("\n================ VERDICT ================")
    if body_ok:
        print("Body-yaw motor RESPONDS in software (present_body_yaw tracked).")
        print("→ If the BASE did NOT physically rotate while numbers moved:")
        print("  MECHANICAL — yaw horn / grub-screw / coupling loose. No rewiring.")
        print("→ If the base did rotate: body yaw is actually fine.")
    elif head_ok:
        print("HEAD yaw tracks but BODY yaw does NOT.")
        print("→ Isolated body-yaw servo problem — ELECTRICAL: that servo or its")
        print("  Dynamixel daisy-chain cable (consistent with a build/assembly")
        print("  mistake). Fix needs opening the base + reseating that cable.")
    elif head_ok is False:
        print("NEITHER head nor body yaw responded.")
        print("→ Motors not enabling or whole Dynamixel bus down. Re-run")
        print("  reachyminios_check with the daemon STOPPED for a clean read.")
    else:
        print("Body yaw did not respond (head test skipped — rerun without")
        print("--skip-head to localise).")
    print("=========================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
