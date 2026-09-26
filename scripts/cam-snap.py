#!/usr/bin/env python3
"""Grab one still from Peachy's head camera over WebRTC (runs on the laptop).

  python scripts/cam-snap.py                   # → .run/peachy_snap.jpg
  python scripts/cam-snap.py --out shot.jpg --open

Doesn't wake or move the robot; if it's asleep you'll get whatever the
lowered head sees.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="One camera frame from Peachy (WebRTC)")
    ap.add_argument("--out", default=str(_REPO / ".run" / "peachy_snap.jpg"))
    ap.add_argument("--open", action="store_true", help="open the JPEG afterwards (macOS)")
    args = ap.parse_args()

    import cv2
    from hostfind import resolve_host
    from rtcmedia import RtcMedia

    m = RtcMedia(resolve_host(), 1280, 720, fps=10, audio=False).start()
    try:
        end = time.time() + 12
        frame = None
        while frame is None and time.time() < end and not m.error:
            time.sleep(0.1)
            frame = m.frame(max_age=2.0)
        if frame is None:
            print(f"✗ no frame ({m.error or 'timed out'})", file=sys.stderr)
            return 1
    finally:
        m.stop()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"✓ saved {out}")
    if args.open:
        subprocess.run(["open", str(out)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
