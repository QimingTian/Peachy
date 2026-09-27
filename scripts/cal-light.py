#!/usr/bin/env python3
"""Labelled light samples from Peachy's head camera, taken in the Dozing pose.

  python scripts/cal-light.py capture dark              # current spot, 5 frames
  python scripts/cal-light.py capture dark --sweep      # body −150°…+150°, 3 frames each
  python scripts/cal-light.py capture dark --angles -150,-50
  python scripts/cal-light.py capture lit --sweep --dry-run   # no moving, current pose
  python scripts/cal-light.py list

Frames go to .run/light_samples/<label>/, one row per frame in samples.jsonl
(brightness stats + head pitch + body yaw). The console must be running: it
owns Dozing and body turns. The camera's auto-exposure is left alone, so the
numbers are what room watch will see (the robot's own camera reads within 2%).
Room watch takes its dark/lit thresholds from the dark and lit samples nearest
PEACHY_DOZE_DEG; the console pushes new ones to the robot within seconds.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heading  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_RUN = _REPO / ".run"
_OUT = _RUN / "light_samples"
SWEEP = (-150, -100, -50, 0, 50, 100, 150)


def _http(url: str, method: str = "GET", body: dict | None = None, timeout: float = 10.0,
          headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


def dash(path: str, method: str = "GET", body: dict | None = None, timeout: float = 10.0):
    try:
        port = int((_RUN / "peachy_dashboard.port").read_text().strip())
    except (OSError, ValueError):
        port = 8080
    tok = os.environ.get("PEACHY_TOKEN") or (_RUN / "peachy_token").read_text().strip()
    return _http(f"http://127.0.0.1:{port}{path}", method, body, timeout, {"X-Peachy-Token": tok})


def stats(frame) -> dict:
    import cv2
    import numpy as np

    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    c = g[h // 4:3 * h // 4, w // 4:3 * w // 4]
    p50, p90, p99 = np.percentile(g, (50, 90, 99))
    return {"mean": round(float(g.mean()), 2), "p50": float(p50), "p90": float(p90),
            "p99": float(p99), "center_mean": round(float(c.mean()), 2)}


def capture(args) -> int:
    import cv2

    from hostfind import resolve_host
    from rtcmedia import RtcMedia

    host = resolve_host()
    if not args.dry_run:
        st = dash("/api/status")
        if st.get("state") != "semi":
            print("→ Dozing (head tucked)…", flush=True)
            r = dash("/api/do/semi", "POST", timeout=120)
            if not r.get("ok"):
                print(f"✗ {r.get('msg')}", file=sys.stderr)
                return 1
            time.sleep(2.5)

    out = _OUT / args.label
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    angles = (None,)
    if not args.dry_run and (args.sweep or args.angles):
        angles = tuple(int(a) for a in args.angles.split(",")) if args.angles else SWEEP
    frames = args.frames or (5 if angles == (None,) else 3)

    m = RtcMedia(host, 1280, 720, fps=10, audio=False).start()
    rows = []
    try:
        end = time.time() + 15
        while m.frame(max_age=2.0) is None and time.time() < end and not m.error:
            time.sleep(0.1)
        if m.frame(max_age=2.0) is None:
            print(f"✗ no frame ({m.error or 'timed out'})", file=sys.stderr)
            return 1
        time.sleep(2.0)  # let auto-exposure settle on the tucked view
        for yaw in angles:
            if yaw is not None:
                r = dash("/api/body/yaw", "POST", {"yaw_deg": yaw}, timeout=20)
                if not r.get("ok"):
                    print(f"✗ body {yaw:+d}°: {r.get('msg')}", file=sys.stderr)
                    continue
                time.sleep(args.settle)
            for i in range(frames):
                time.sleep(0.6)
                f = m.frame(max_age=1.0)
                end = time.time() + 3.0
                while f is None and time.time() < end:
                    time.sleep(0.2)
                    f = m.frame(max_age=1.0)
                if f is None:
                    print(f"  ! no fresh frame ({m.error or 'stream stalled'})", file=sys.stderr, flush=True)
                    continue
                s = _http(f"http://{host}:8000/api/state/full", timeout=4)
                hp = s.get("head_pose") or {}
                body_deg = heading.to_world(-math.degrees(float(s.get("body_yaw") or 0.0)))
                name = f"{stamp}_body{body_deg:+04.0f}_{i}.jpg"
                cv2.imwrite(str(out / name), f, [cv2.IMWRITE_JPEG_QUALITY, 90])
                row = {"file": name, "label": args.label, "t": time.time(),
                       "body_deg": round(body_deg, 1),
                       "head_pitch_deg": round(math.degrees(float(hp.get("pitch", 0.0))), 1),
                       **stats(f)}
                rows.append(row)
                print(f"  body {body_deg:+5.0f}°  frame {i}  mean {row['mean']:6.1f}  "
                      f"center {row['center_mean']:6.1f}  p99 {row['p99']:5.0f}", flush=True)
    finally:
        m.stop()
        if angles != (None,):
            try:
                dash("/api/body/yaw", "POST", {"yaw_deg": 0}, timeout=20)
            except OSError:
                pass

    with (out / "samples.jsonl").open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    if rows:
        means = [r["mean"] for r in rows]
        print(f"✓ {len(rows)} frames → {out}  (mean {min(means):.1f}–{max(means):.1f})")
    return 0 if rows else 1


def list_samples(_args) -> int:
    if not _OUT.is_dir():
        print("no samples yet")
        return 0
    for d in sorted(p for p in _OUT.iterdir() if p.is_dir()):
        rows = [json.loads(ln) for ln in (d / "samples.jsonl").read_text().splitlines() if ln.strip()] \
            if (d / "samples.jsonl").exists() else []
        if not rows:
            print(f"{d.name}: empty")
            continue
        means = sorted(r["mean"] for r in rows)
        print(f"{d.name}: {len(rows)} frames, mean {means[0]:.1f}–{means[-1]:.1f} "
              f"(median {means[len(means) // 2]:.1f})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Labelled light samples in the Dozing pose")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("label", help="e.g. dark, lit, daylight")
    c.add_argument("--sweep", action="store_true", help="turn the body through −150°…+150°")
    c.add_argument("--angles", default="", help="body angles instead of the sweep, e.g. -150,-50")
    c.add_argument("--frames", type=int, default=0, help="frames per spot")
    c.add_argument("--settle", type=float, default=2.0, help="seconds after each turn")
    c.add_argument("--dry-run", action="store_true", help="don't doze or turn; current pose")
    c.set_defaults(fn=capture)
    sub.add_parser("list").set_defaults(fn=list_samples)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
