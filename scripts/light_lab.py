#!/usr/bin/env python3
"""Peachy head-camera light experiments — ROI stats, compare, tune.

Used by the dashboard /lights lab and CLI:

  python scripts/light_lab.py analyze --json
  python scripts/light_lab.py baseline --label dark
  python scripts/light_lab.py compare --json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_RUN = _REPO / ".run"
_BASELINE_META = _RUN / "light_lab_baseline.json"
_SAMPLE_META = _RUN / "light_lab_sample.json"
_TUNE_FILE = _RUN / "light_tune.json"

# Normalized crop boxes (x0, y0, x1, y1) as fractions of frame size.
# ``lower`` targets the neck/body gap visible when the head is down.
ROIS: dict[str, tuple[float, float, float, float]] = {
    "full": (0.0, 0.0, 1.0, 1.0),
    "center": (0.30, 0.30, 0.70, 0.70),
    "lower": (0.08, 0.62, 0.92, 1.00),
    "upper": (0.08, 0.00, 0.92, 0.35),
    "left_edge": (0.00, 0.18, 0.18, 0.82),
    "right_edge": (0.82, 0.18, 1.00, 0.82),
}

BRIGHT_THRESH = 60  # luma 0–255 for bright-pixel fraction


def tune_path() -> Path:
    return _TUNE_FILE


def load_tune() -> dict:
    try:
        data = json.loads(_TUNE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}


def save_tune(**kwargs) -> dict:
    data = load_tune()
    data.update(kwargs)
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _RUN.mkdir(parents=True, exist_ok=True)
    _TUNE_FILE.write_text(json.dumps(data, indent=2) + "\n")
    return data


def effective_on_delta() -> float:
    t = load_tune()
    if "on_delta" in t:
        return float(t["on_delta"])
    return float(os.environ.get("REACHY_LIGHT_ON_DELTA", "25"))


def effective_off_delta() -> float:
    t = load_tune()
    if "off_delta" in t:
        return float(t["off_delta"])
    return float(os.environ.get("REACHY_LIGHT_OFF_DELTA", "12"))


def preferred_roi() -> str:
    roi = load_tune().get("preferred_roi", "full")
    return roi if roi in ROIS else "full"


def roi_mean_luma(path: Path, roi: str | None = None) -> float:
    """Mean luma for one ROI — enough for lights on/off; decodes small JPEGs fast."""
    from PIL import Image

    roi = roi or preferred_roi()
    box = ROIS.get(roi, ROIS["full"])
    img = Image.open(path).convert("L")
    # Downscale before crop — light level only, not scene detail.
    img.thumbnail((200, 150), Image.Resampling.LANCZOS)
    w, h = img.size
    gray = list(img.getdata())
    px = _crop_pixels(gray, w, h, box)
    if not px:
        return 0.0
    return float(statistics.fmean(px))


def _crop_pixels(gray, w: int, h: int, box: tuple[float, float, float, float]) -> list[int]:
    x0 = max(0, min(w - 1, int(box[0] * w)))
    y0 = max(0, min(h - 1, int(box[1] * h)))
    x1 = max(x0 + 1, min(w, int(box[2] * w)))
    y1 = max(y0 + 1, min(h, int(box[3] * h)))
    out: list[int] = []
    step_x = max(1, (x1 - x0) // 48)
    step_y = max(1, (y1 - y0) // 48)
    for y in range(y0, y1, step_y):
        row = y * w
        for x in range(x0, x1, step_x):
            out.append(gray[row + x])
    return out


def analyze_path(path: Path) -> dict:
    from PIL import Image

    img = Image.open(path).convert("L")
    w, h = img.size
    gray = list(img.getdata())
    regions: dict[str, dict] = {}
    for name, box in ROIS.items():
        px = _crop_pixels(gray, w, h, box)
        if not px:
            regions[name] = {"mean": 0.0, "std": 0.0, "p10": 0.0, "p90": 0.0, "bright_pct": 0.0}
            continue
        px_sorted = sorted(px)
        n = len(px_sorted)
        p10 = px_sorted[max(0, n // 10)]
        p90 = px_sorted[min(n - 1, (9 * n) // 10)]
        bright = sum(1 for v in px if v >= BRIGHT_THRESH) / n * 100.0
        regions[name] = {
            "mean": round(statistics.fmean(px), 1),
            "std": round(statistics.pstdev(px) if len(px) > 1 else 0.0, 1),
            "p10": round(float(p10), 1),
            "p90": round(float(p90), 1),
            "spread": round(float(p90 - p10), 1),
            "bright_pct": round(bright, 1),
        }
    full = regions["full"]
    dark = full["mean"] < 30 and full["std"] < 10
    return {
        "ok": True,
        "width": w,
        "height": h,
        "dark_frame": dark,
        "regions": regions,
        "path": str(path),
    }


def capture_frame(dest: Path) -> bool:
    import sys

    sys.path.insert(0, str(_REPO / "scripts"))
    from light_sensor import _fetch_robot_jpeg

    return _fetch_robot_jpeg(dest)


def _meta_path(label: str) -> tuple[Path, Path]:
    if label == "baseline":
        return _BASELINE_META, _RUN / "light_lab_baseline.jpg"
    if label == "sample":
        return _SAMPLE_META, _RUN / "light_lab_sample.jpg"
    live = _RUN / "light_lab_live.jpg"
    return _RUN / "light_lab_live.json", live


def save_capture(label: str, note: str = "") -> dict:
    meta_path, img_path = _meta_path(label)
    if not capture_frame(img_path):
        return {"ok": False, "msg": "camera read failed — is the WebRTC stream up?"}
    analysis = analyze_path(img_path)
    meta = {
        "label": label,
        "note": note,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "image": str(img_path.name),
        **analysis,
    }
    _RUN.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    annotate(img_path, _RUN / f"light_lab_{label}_annot.jpg")
    return {"ok": True, "label": label, **analysis}


def load_meta(label: str) -> dict | None:
    meta_path, _ = _meta_path(label)
    try:
        return json.loads(meta_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def compare_captures() -> dict:
    base = load_meta("baseline")
    sample = load_meta("sample")
    if not base or not sample:
        return {
            "ok": False,
            "msg": "Need both baseline (lights off) and sample (lights on) captures",
            "has_baseline": base is not None,
            "has_sample": sample is not None,
        }
    deltas: dict[str, dict] = {}
    best_roi = "full"
    best_delta = -1.0
    for name in ROIS:
        b = base.get("regions", {}).get(name, {})
        s = sample.get("regions", {}).get(name, {})
        d_mean = round(float(s.get("mean", 0) - b.get("mean", 0)), 1)
        d_std = round(float(s.get("std", 0) - b.get("std", 0)), 1)
        d_spread = round(float(s.get("spread", 0) - b.get("spread", 0)), 1)
        d_bright = round(float(s.get("bright_pct", 0) - b.get("bright_pct", 0)), 1)
        deltas[name] = {
            "mean_delta": d_mean,
            "std_delta": d_std,
            "spread_delta": d_spread,
            "bright_pct_delta": d_bright,
            "baseline_mean": b.get("mean", 0),
            "sample_mean": s.get("mean", 0),
        }
        if d_mean > best_delta:
            best_delta = d_mean
            best_roi = name
    on_delta = effective_on_delta()
    off_delta = effective_off_delta()
    suggested_on = max(8.0, round(best_delta * 0.55, 1)) if best_delta > 0 else on_delta
    pref = preferred_roi()
    sim = simulate_lit(base, pref, on_delta, off_delta)
    return {
        "ok": True,
        "deltas": deltas,
        "best_roi": best_roi,
        "best_mean_delta": round(best_delta, 1),
        "suggested_on_delta": suggested_on,
        "suggested_off_delta": max(5.0, round(suggested_on * 0.5, 1)),
        "preferred_roi": pref,
        "baseline_ts": base.get("ts"),
        "sample_ts": sample.get("ts"),
        "simulate_baseline": sim,
    }


def simulate_lit(capture: dict, roi: str, on_delta: float, off_delta: float) -> dict:
    """Would this capture read lit given ref=its own mean (fixed baseline test)?"""
    reg = capture.get("regions", {}).get(roi, {})
    mean = float(reg.get("mean", 0))
    ref = mean  # zero delta vs itself → dark
    return {
        "roi": roi,
        "mean": mean,
        "would_lit_at_on": mean >= ref + on_delta,
        "on_delta": on_delta,
        "off_delta": off_delta,
    }


def simulate_pair(base: dict, sample: dict, roi: str,
                  on_delta: float, off_delta: float) -> dict:
    """Hysteresis sim: ref from baseline, sample mean vs thresholds."""
    b = base.get("regions", {}).get(roi, {})
    s = sample.get("regions", {}).get(roi, {})
    ref = float(b.get("mean", 0))
    mean = float(s.get("mean", 0))
    delta = round(mean - ref, 1)
    lit_on = delta >= on_delta
    lit_off = delta <= off_delta
    return {
        "roi": roi,
        "ref": round(ref, 1),
        "mean": round(mean, 1),
        "delta": delta,
        "lit_if_was_dark": lit_on,
        "stay_lit_if_was_lit": not lit_off,
        "on_delta": on_delta,
        "off_delta": off_delta,
        "passes": lit_on and not lit_off,
    }


def lab_status() -> dict:
    import sys

    sys.path.insert(0, str(_REPO / "scripts"))
    from light_sensor import LightSensor, ON_DELTA, OFF_DELTA

    tune = load_tune()
    sensor = LightSensor()
    try:
        sensor.read()
        sensor.lit()
        snap = sensor.snapshot()
    finally:
        sensor.close()
    base = load_meta("baseline")
    sample = load_meta("sample")
    roi = preferred_roi()
    on_d = effective_on_delta()
    off_d = effective_off_delta()
    reg = snap  # full sensor uses full frame; overlay preferred roi from last read if we re-analyze
    live_path = _RUN / "light_lab_live.jpg"
    regions = {}
    if live_path.is_file():
        try:
            regions = analyze_path(live_path).get("regions", {})
        except OSError:
            pass
    if not regions and capture_frame(live_path):
        try:
            regions = analyze_path(live_path).get("regions", {})
            annotate(live_path, _RUN / "light_lab_live_annot.jpg")
        except OSError:
            pass
    pref_stats = regions.get(roi, {})
    compare = None
    if base and sample:
        compare = compare_captures()
    sim = simulate_pair(base, sample, roi, on_d, off_d) if base and sample else None
    dark_frame = False
    if regions.get("full"):
        fm = regions["full"].get("mean", 99)
        fs = regions["full"].get("std", 99)
        dark_frame = fm < 30 and fs < 10
    return {
        "ok": True,
        "sensor": snap,
        "dark_frame": dark_frame,
        "roomwatch": (_RUN / "dashboard_roomwatch.pid").is_file(),
        "tune": {
            "on_delta": on_d,
            "off_delta": off_d,
            "preferred_roi": roi,
            "env_on_delta": ON_DELTA,
            "env_off_delta": OFF_DELTA,
            **tune,
        },
        "regions": regions,
        "preferred_roi_stats": pref_stats,
        "has_baseline": base is not None,
        "has_sample": sample is not None,
        "baseline": {"ts": base.get("ts"), "note": base.get("note")} if base else None,
        "sample": {"ts": sample.get("ts"), "note": sample.get("note")} if sample else None,
        "compare": compare,
        "simulate": sim,
        "roi_defs": {k: list(v) for k, v in ROIS.items()},
    }


def annotate(src: Path, dest: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.open(src).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    colors = {
        "full": "#FF8A5B",
        "center": "#28D9A0",
        "lower": "#FFC247",
        "upper": "#FF5C8A",
        "left_edge": "#7B68EE",
        "right_edge": "#7B68EE",
    }
    for name, box in ROIS.items():
        x0, y0 = int(box[0] * w), int(box[1] * h)
        x1, y1 = int(box[2] * w), int(box[3] * h)
        c = colors.get(name, "#ffffff")
        draw.rectangle([x0, y0, x1, y1], outline=c, width=max(2, w // 160))
        draw.text((x0 + 4, y0 + 4), name, fill=c)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, quality=85)


def main() -> int:
    ap = argparse.ArgumentParser(description="Peachy light lab")
    ap.add_argument("cmd", choices=("analyze", "baseline", "sample", "compare", "status"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    if args.cmd == "analyze":
        dest = _RUN / "light_lab_live.jpg"
        if not capture_frame(dest):
            out = {"ok": False, "msg": "capture failed"}
        else:
            out = analyze_path(dest)
    elif args.cmd == "baseline":
        out = save_capture("baseline", args.note or "lights off")
    elif args.cmd == "sample":
        out = save_capture("sample", args.note or "lights on")
    elif args.cmd == "compare":
        out = compare_captures()
    else:
        out = lab_status()

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(out)
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
