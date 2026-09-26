#!/usr/bin/env python3
"""Read Reachy Mini CM4 head fan + thermal sysfs (JSON), from the laptop over SSH.

  python scripts/fan_read.py
  python scripts/fan_read.py --json
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path


def _rd(path: str | Path) -> str:
    return Path(path).read_text().strip()


def _tc(path: str | Path) -> float:
    return round(int(_rd(path)) / 1000.0, 2)


def read_sysfs(base: Path | str = "/") -> dict:
    """Read fan/thermal stats from sysfs (run on the robot)."""
    root = Path(base)
    tz = root / "sys/class/thermal/thermal_zone0"
    cd = root / "sys/class/thermal/cooling_device0"
    try:
        temp = _tc(tz / "temp")
        trip0 = _tc(tz / "trip_point_0_temp")
        trip1 = _tc(tz / "trip_point_1_temp")
        trip0_type = _rd(tz / "trip_point_0_type")
        trip1_type = _rd(tz / "trip_point_1_type")
        hyst = {}
        for p in sorted(glob.glob(str(tz / "trip_point_*_hyst"))):
            hyst[Path(p).name] = _tc(p)
        cur = int(_rd(cd / "cur_state"))
        mx = int(_rd(cd / "max_state"))
        pwm = None
        for p in glob.glob(str(root / "sys/class/hwmon/hwmon*/pwm1")):
            try:
                pwm = int(_rd(p))
                break
            except (OSError, ValueError):
                continue
        svc = subprocess.run(
            ["systemctl", "is-active", "peachy-fan.service"],
            capture_output=True, text=True, timeout=3)
        peachy_fan = (svc.stdout or "").strip() or "not-installed"
        fan_on = (pwm or 0) > 0 or cur > 0
        trip_default = trip1 >= 44.5 and trip1 <= 45.5
        trip_calm = trip1 >= 45.5 and trip1 <= 47.0
        trip_lowered = trip1 < 44.5 or (peachy_fan == "active" and not trip_calm)
        cusp_risk = trip_default and temp is not None and temp >= trip1 - 1.5
        quiet_at_default = trip_default and (not fan_on or (pwm or 0) <= 85)
        calm_at_trip = trip_calm and not fan_on
        trip_too_low = trip1 < 44.0 and fan_on and (pwm or 0) > 85
        return {
            "ok": True,
            "temp_c": temp,
            "fan_state": cur,
            "fan_max": mx,
            "fan_pct": round(100 * cur / mx, 1) if mx else 0,
            "pwm": pwm,
            "pwm_pct": round(100 * pwm / 255, 1) if pwm is not None else None,
            "trip_0_c": trip0,
            "trip_0_type": trip0_type,
            "trip_1_c": trip1,
            "trip_1_type": trip1_type,
            "hyst_c": hyst,
            "peachy_fan_service": peachy_fan,
            "fan_on": fan_on,
            "quiet_at_default": quiet_at_default,
            "calm_at_trip": calm_at_trip,
            "cusp_risk": cusp_risk,
            "trip_lowered": trip_lowered,
            "trip_too_low": trip_too_low,
        }
    except (OSError, ValueError) as e:
        return {"ok": False, "msg": f"sysfs read failed: {e}"}


# Self-contained snippet for SSH — no fan_read.py required on the robot.
_REMOTE_READ_PY = r"""
import glob, json, subprocess
def rd(p):
    with open(p) as f: return f.read().strip()
def tc(p):
    return round(int(rd(p)) / 1000.0, 2)
tz = "/sys/class/thermal/thermal_zone0"
cd = "/sys/class/thermal/cooling_device0"
temp = tc(f"{tz}/temp")
trip0 = tc(f"{tz}/trip_point_0_temp")
trip1 = tc(f"{tz}/trip_point_1_temp")
trip0_type = rd(f"{tz}/trip_point_0_type")
trip1_type = rd(f"{tz}/trip_point_1_type")
hyst = {}
for p in sorted(glob.glob(f"{tz}/trip_point_*_hyst")):
    hyst[p.split("/")[-1]] = tc(p)
cur = int(rd(f"{cd}/cur_state"))
mx = int(rd(f"{cd}/max_state"))
pwm = None
for p in glob.glob("/sys/class/hwmon/hwmon*/pwm1"):
    try:
        pwm = int(rd(p)); break
    except (OSError, ValueError):
        pass
svc = subprocess.run(["systemctl", "is-active", "peachy-fan.service"],
                       capture_output=True, text=True, timeout=3)
peachy_fan = (svc.stdout or "").strip() or "not-installed"
fan_on = (pwm or 0) > 0 or cur > 0
trip_default = trip1 >= 44.5 and trip1 <= 45.5
trip_calm = trip1 >= 45.5 and trip1 <= 47.0
trip_lowered = trip1 < 44.5 or (peachy_fan == "active" and not trip_calm)
cusp_risk = trip_default and temp >= trip1 - 1.5
quiet_at_default = trip_default and (not fan_on or (pwm or 0) <= 85)
calm_at_trip = trip_calm and not fan_on
trip_too_low = trip1 < 44.0 and fan_on and (pwm or 0) > 85
print(json.dumps({"ok": True, "temp_c": temp, "fan_state": cur, "fan_max": mx,
    "fan_pct": round(100 * cur / mx, 1) if mx else 0, "pwm": pwm,
    "pwm_pct": round(100 * pwm / 255, 1) if pwm is not None else None,
    "trip_0_c": trip0, "trip_0_type": trip0_type, "trip_1_c": trip1,
    "trip_1_type": trip1_type, "hyst_c": hyst, "peachy_fan_service": peachy_fan,
    "fan_on": fan_on, "quiet_at_default": quiet_at_default, "calm_at_trip": calm_at_trip,
    "cusp_risk": cusp_risk, "trip_lowered": trip_lowered, "trip_too_low": trip_too_low}))
"""


def read_remote() -> dict:
    """Read the robot's fan/thermal sysfs over key-only SSH (robotssh)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from robotssh import blocked, ssh_run

    p = ssh_run("/venvs/apps_venv/bin/python3 -", input_text=_REMOTE_READ_PY, timeout=14)
    if p is None:
        return {"ok": False, "msg": blocked()[1]}
    out, err = (p.stdout or "").strip(), (p.stderr or "").strip()
    if p.returncode != 0:
        return {"ok": False, "msg": (out + err)[-300:]}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "msg": out[-300:] or err[-300:] or "invalid JSON from robot"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Fan/thermal JSON readout")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = read_remote()
    if args.json:
        print(json.dumps(data, indent=2))
    elif not data.get("ok", True):
        print(data.get("msg", "failed"))
        return 1
    else:
        print(
            f"temp={data['temp_c']}°C  fan={data['fan_state']}/{data['fan_max']}  "
            f"pwm={data.get('pwm')}  trip_1={data['trip_1_c']}°C  "
            f"cusp={'yes' if data.get('cusp_risk') else 'no'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
