#!/usr/bin/env python3
"""One-shot Peachy head-camera light read (JSON).

  python scripts/light_probe.py --json
  python scripts/light_probe.py --json --calibrate 2

Uses the robot camera only (preview JPEG or fast snap). Start dashboard Live
preview first for faster reads.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from light_sensor import LightSensor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Peachy camera light probe")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--calibrate", type=float, default=0.0,
                    help="seconds to seed baseline (0 = single frame)")
    args = ap.parse_args()
    try:
        sensor = LightSensor()
        try:
            if args.calibrate > 0:
                sensor.calibrate_dark(args.calibrate)
            else:
                sensor.read()
                sensor.lit()
            out = {"ok": True, **sensor.snapshot()}
        finally:
            sensor.close()
    except RuntimeError as e:
        out = {"ok": False, "msg": str(e)}
        if args.json:
            print(json.dumps(out))
        else:
            print(out.get("msg", "failed"))
        return 1
    if args.json:
        print(json.dumps(out))
    else:
        print(f"raw={out['raw']} ema={out['ema']} lit={out['lit']} (Peachy cam)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
