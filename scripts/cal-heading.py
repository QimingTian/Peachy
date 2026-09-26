#!/usr/bin/env python3
"""World heading from the head camera: which way the base points in the room.

  python scripts/cal-heading.py capture          # add a room scan
  python scripts/cal-heading.py capture --fresh  # forget old scans, keep the world frame
  python scripts/cal-heading.py capture --fresh --zero   # … and world 0 = the base now
  python scripts/cal-heading.py anchor           # after a bump: re-find world 0
  python scripts/cal-heading.py anchor --dry-run # measure only, keep the offset
  python scripts/cal-heading.py refit            # re-measure the camera from saved scans
  python scripts/cal-heading.py status

The body encoder is relative to the base; bump the base and every angle is off.
`capture` lifts the head in the Dozing pose and photographs the room every 20°
across the body's range (one scan, .run/heading_ref/pass_*/), head tilted up
TILT_DEG so the tabletop, bags and seated people stay mostly out of the frame.
The first scan uses the current world frame (encoder + the stored offset; with
--zero, world 0 = the base now); later scans are aligned to the earlier ones by
the photos themselves, so the base may have moved, and they recalibrate as a
side effect. Up to MAX_PASSES scans are kept.

People move between scans; walls, lights and windows don't. With two or more
scans only landmarks are used: features of a photo that also match (RANSAC-
consistent) a photo from another scan at about the same angle. Scans taken at
different times, with different people around, therefore sharpen the landmark
set.

`anchor` lifts the head and looks at the current direction, then ±15°, and
further out (±35°, ±55°) until at least three views agree within 3° — a view
blocked by someone matches nothing or disagrees and just costs another turn. It
matches each view against the landmarks (ORB, RANSAC, pinhole yaw with the
fitted focal length) and stores the median offset = world − encoder of the
agreeing views in .run/heading.json, which the console and room watch apply to
every body angle (heading.py).

Needs the console running and Peachy Dozing (it goes there if not), lights on.
Room watch is paused while the head is up.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heading  # noqa: E402

_RUN = Path(__file__).resolve().parent.parent / ".run"
REF_DIR = _RUN / "heading_ref"
REF_JSON = REF_DIR / "refs.json"
LAST_DIR = REF_DIR / "last_anchor"
DOZE_DEG = float(os.environ.get("PEACHY_DOZE_DEG", "-100"))
WORK_W = 640
MIN_INLIERS = 20
MAX_PASSES = 4
AGREE_DEG = 3.0
VIEW_STEPS = (0.0, -15.0, 15.0, -35.0, 35.0, -55.0, 55.0)
# Looking up keeps the tabletop, bags and seated people mostly out of the frame.
TILT_DEG = 15.0


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
    base = os.environ.get("PEACHY_DASH_URL")
    if not base:
        try:
            port = int((_RUN / "peachy_dashboard.port").read_text().strip())
        except (OSError, ValueError):
            port = 8080
        base = f"http://127.0.0.1:{port}"
    tok = os.environ.get("PEACHY_TOKEN") or (_RUN / "peachy_token").read_text().strip()
    return _http(f"{base}{path}", method, body, timeout, {"X-Peachy-Token": tok})


def encoder_deg(host: str) -> float:
    return -math.degrees(float(_http(f"http://{host}:8000/api/state/present_body_yaw", timeout=4)))


# ------------------------------------------------------------------ vision

def features(img):
    import cv2

    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    if g.shape[1] != WORK_W:
        g = cv2.resize(g, (WORK_W, round(g.shape[0] * WORK_W / g.shape[1])), interpolation=cv2.INTER_AREA)
    g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    kp, des = cv2.ORB_create(nfeatures=2500, fastThreshold=12).detectAndCompute(g, None)
    return list(kp), des


def subset(f, idx):
    kp, des = f
    idx = sorted(idx)
    return [kp[i] for i in idx], (des[idx] if des is not None and idx else None)


def match(a, b) -> dict | None:
    """RANSAC-consistent matches: x in WORK_W pixels in each view, and the indices
    of the matched keypoints of `a`."""
    import cv2
    import numpy as np

    (ka, da), (kb, db) = a, b
    if da is None or db is None or len(ka) < 10 or len(kb) < 10:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < 8:
        return None
    pa = np.float32([ka[m.queryIdx].pt for m in good])
    pb = np.float32([kb[m.trainIdx].pt for m in good])
    M, mask = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if M is None or mask is None:
        return None
    m = mask.ravel().astype(bool)
    return {"xa": pa[m, 0], "xb": pb[m, 0], "ia": [g.queryIdx for g, k in zip(good, m) if k]}


def turn_between(xa, xb, focal: float) -> float:
    """Degrees the camera turned clockwise from view a to view b (pinhole, yaw only)."""
    import numpy as np

    c = WORK_W / 2
    return float(np.median(np.degrees(np.arctan2(xa - c, focal) - np.arctan2(xb - c, focal))))


def fit_focal(pairs: list) -> tuple[float, float]:
    """Focal length (WORK_W px) that best explains the encoder turns between photos."""
    import numpy as np

    best = (0.0, float("inf"))
    for f in range(250, 1000, 5):
        rms = float(np.sqrt(np.mean([(turn_between(xa, xb, f) - dd) ** 2 for dd, xa, xb in pairs])))
        if rms < best[1]:
            best = (float(f), rms)
    return best


def consensus(offs: list[float]) -> tuple[list[float], float | None]:
    """Largest group of offsets within AGREE_DEG of one of them, and its median."""
    best: list[float] = []
    for o in offs:
        group = [x for x in offs if abs(heading.wrap(x - o)) <= AGREE_DEG]
        if len(group) > len(best):
            best = group
    if not best:
        return [], None
    rel = sorted(heading.wrap(x - best[0]) for x in best)
    n = len(rel)
    med = rel[n // 2] if n % 2 else (rel[n // 2 - 1] + rel[n // 2]) / 2
    return best, heading.wrap(best[0] + med)


# ------------------------------------------------------------------ scans

def load_meta() -> dict | None:
    """refs.json, migrating the single-scan layout (refs at the top level)."""
    try:
        meta = json.loads(REF_JSON.read_text())
    except (OSError, ValueError):
        return None
    if "passes" in meta:
        return meta
    if "refs" not in meta:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.strptime(meta.get("at", "2000-01-01 00:00:00"),
                                                        "%Y-%m-%d %H:%M:%S"))
    pdir = REF_DIR / f"pass_{stamp}"
    pdir.mkdir(parents=True, exist_ok=True)
    refs = []
    for r in meta["refs"]:
        src = REF_DIR / r["file"]
        if src.exists():
            shutil.move(str(src), str(pdir / src.name))
        refs.append({"file": f"{pdir.name}/{Path(r['file']).name}", "deg": r["deg"]})
    meta = {"focal_px": meta.get("focal_px"), "fit_rms_deg": meta.get("fit_rms_deg"),
            "passes": [{"at": meta.get("at"), "offset_deg": 0.0, "refs": refs}]}
    save_meta(meta)
    return meta


def save_meta(meta: dict) -> None:
    REF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REF_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(REF_JSON)


def scan_features(meta: dict) -> list[list]:
    import cv2

    out = []
    for p in meta["passes"]:
        row = []
        for r in p["refs"]:
            img = cv2.imread(str(REF_DIR / r["file"]))
            row.append(None if img is None else features(img))
        out.append(row)
    return out


def landmark_refs(meta: dict, feats: list[list]) -> list[tuple[float, tuple]]:
    """(world_deg, features) per photo; with 2+ scans only the keypoints that match
    a photo of another scan within 12° (all of them where no other scan looked)."""
    passes = meta["passes"]
    out = []
    for p, pas in enumerate(passes):
        for i, r in enumerate(pas["refs"]):
            f = feats[p][i]
            if f is None:
                continue
            if len(passes) < 2:
                out.append((r["deg"], f))
                continue
            keep: set[int] = set()
            compared = False
            for q, other in enumerate(passes):
                if q == p:
                    continue
                j = min(range(len(other["refs"])),
                        key=lambda k: abs(heading.wrap(other["refs"][k]["deg"] - r["deg"])))
                if abs(heading.wrap(other["refs"][j]["deg"] - r["deg"])) > 12 or feats[q][j] is None:
                    continue
                compared = True
                m = match(f, feats[q][j])
                if m is not None and len(m["ia"]) >= MIN_INLIERS:
                    keep.update(m["ia"])
            if not compared:
                out.append((r["deg"], f))  # only this scan saw this direction
            elif len(keep) >= MIN_INLIERS:
                out.append((r["deg"], subset(f, keep)))
    return out


def locate(frame, refs: list, focal: float) -> dict | None:
    live = features(frame)
    rows = []
    for deg, f in refs:
        m = match(f, live)
        if m is not None and len(m["ia"]) >= MIN_INLIERS:
            rows.append({"ref_deg": deg, "inliers": len(m["ia"]),
                         "world": heading.wrap(deg + turn_between(m["xa"], m["xb"], focal))})
    rows.sort(key=lambda r: -r["inliers"])
    if not rows:
        return None
    best = rows[0]
    near = [r for r in rows if r["inliers"] >= best["inliers"] * 0.4
            and abs(heading.wrap(r["world"] - best["world"])) < 6]
    w = sum(r["inliers"] for r in near)
    world = best["world"] + sum(r["inliers"] * heading.wrap(r["world"] - best["world"]) for r in near) / w
    return {"world": heading.wrap(world), "inliers": best["inliers"], "ref_deg": best["ref_deg"],
            "used": len(near)}


def refit_focal(meta: dict, feats: list[list]) -> bool:
    pairs = []
    for p, pas in enumerate(meta["passes"]):
        refs = pas["refs"]
        for i in range(len(refs)):
            for j in range(i + 1, min(i + 3, len(refs))):
                if feats[p][i] is None or feats[p][j] is None:
                    continue
                m = match(feats[p][i], feats[p][j])
                if m is not None and len(m["ia"]) >= MIN_INLIERS:
                    pairs.append((heading.wrap(refs[j]["deg"] - refs[i]["deg"]), m["xa"], m["xb"]))
    if len(pairs) < 4:
        return False
    meta["focal_px"], rms = fit_focal(pairs)
    meta["fit_rms_deg"] = round(rms, 2)
    return True


# ------------------------------------------------------------------ robot

def ensure_dozing() -> bool:
    if dash("/api/status").get("state") == "semi":
        return True
    print("→ Dozing (head tucked)…", flush=True)
    r = dash("/api/do/semi", "POST", timeout=120)
    if not r.get("ok"):
        print(f"✗ {r.get('msg')}", file=sys.stderr)
        return False
    time.sleep(2.5)
    return True


class WatchPaused:
    def __enter__(self):
        try:
            self.was = bool(dash("/api/sense").get("watch"))
            if self.was:
                print("→ room watch paused", flush=True)
                dash("/api/sense/watch/off", "POST", timeout=20)
        except OSError:
            self.was = False
        return self

    def __exit__(self, *exc):
        if self.was:
            try:
                dash("/api/sense/watch/on", "POST", timeout=20)
                print("→ room watch back on", flush=True)
            except OSError as e:
                print(f"! could not turn room watch back on: {e}", file=sys.stderr)


def pose(head: str | None = None, yaw: float | None = None, enc: bool = False,
         tilt: float | None = None) -> dict:
    body: dict = {"wait": True}
    if head:
        body["head"] = head
    if tilt is not None:
        body["tilt_deg"] = tilt
    if yaw is not None:
        body["yaw_deg"], body["enc"] = yaw, enc
    r = dash("/api/doze/pose", "POST", body, timeout=40)
    if not r.get("ok"):
        raise RuntimeError(r.get("msg") or "doze pose failed")
    return r


def tuck_quietly(yaw: float | None = DOZE_DEG) -> None:
    try:
        pose("tucked", yaw, tilt=0.0)
    except (RuntimeError, OSError) as e:
        print(f"! could not tuck: {e}", file=sys.stderr)


class Camera:
    def __init__(self, host: str):
        from rtcmedia import RtcMedia

        self.m = RtcMedia(host, 1280, 720, fps=10, audio=False).start()

    def __enter__(self):
        end = time.time() + 15
        while self.m.frame(max_age=2.0) is None and time.time() < end and not self.m.error:
            time.sleep(0.1)
        if self.m.frame(max_age=2.0) is None:
            self.m.stop()
            raise RuntimeError(f"no camera frame ({self.m.error or 'timed out'})")
        return self

    def __exit__(self, *exc):
        self.m.stop()

    def fresh(self, after: float):
        """First frame that arrived after `after` (time.time())."""
        end = time.time() + 4.0
        while time.time() < end:
            time.sleep(0.15)
            if time.time() - after > 0.3:
                f = self.m.frame(max_age=0.25)
                if f is not None:
                    return f
        raise RuntimeError(f"camera stalled ({self.m.error or 'no fresh frame'})")


# ------------------------------------------------------------------ commands

def capture(args) -> int:
    import cv2

    from hostfind import resolve_host

    meta = None if args.fresh else load_meta()
    if meta is not None and not meta.get("focal_px"):
        meta = None
    tilt = float(meta.get("tilt_deg", 0.0)) if meta is not None else args.tilt
    host = resolve_host()
    if not ensure_dozing():
        return 1
    angles = list(range(-160, 161, args.step))
    if angles[-1] != 160:
        angles.append(160)
    if meta is not None:
        print(f"→ scan {len(meta['passes']) + 1}, aligning to the {len(meta['passes'])} before", flush=True)
        refs = landmark_refs(meta, scan_features(meta))
    shots = []
    with WatchPaused(), Camera(host) as cam:
        try:
            pose("lifted", angles[0], enc=True, tilt=tilt)
            time.sleep(2.0)  # auto-exposure on the lifted view
            for a in angles:
                pose(yaw=a, enc=True)
                time.sleep(args.settle)
                f = cam.fresh(time.time())
                e = encoder_deg(host)
                shots.append((a, e, f))
                print(f"  body {e:+6.1f}°  {len(features(f)[0]):4d} features", flush=True)
        except (RuntimeError, OSError):
            tuck_quietly()
            raise

        offset = 0.0 if args.zero else heading.offset()
        if meta is not None:
            offs = []
            for _, e, f in shots:
                found = locate(f, refs, float(meta["focal_px"]))
                if found is not None:
                    offs.append(heading.wrap(found["world"] - e))
            group, offset = consensus(offs)
            if len(group) < max(4, len(shots) // 3):
                print(f"✗ this scan doesn't line up with the earlier ones ({len(group)} of {len(shots)} "
                      "photos agree) — lights on? room rearranged? use --fresh to start over",
                      file=sys.stderr)
                tuck_quietly()
                return 1
            print(f"  aligned: base turned {offset:+.1f}° ({len(group)} of {len(shots)} photos agree)",
                  flush=True)
        heading.save(offset, inliers=None, ref_deg=None, residual_deg=None, views=None)
        tuck_quietly(DOZE_DEG)

    pdir = REF_DIR / f"pass_{time.strftime('%Y%m%d-%H%M%S')}"
    pdir.mkdir(parents=True, exist_ok=True)
    scan = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "offset_deg": round(offset, 2), "refs": []}
    for a, e, f in shots:
        name = f"ref_{a:+04d}.jpg"
        cv2.imwrite(str(pdir / name), f, [cv2.IMWRITE_JPEG_QUALITY, 92])
        scan["refs"].append({"file": f"{pdir.name}/{name}", "deg": round(heading.wrap(e + offset), 2)})
    if meta is None:
        for old in REF_DIR.glob("pass_*"):
            if old != pdir:
                shutil.rmtree(old, ignore_errors=True)
        for old in REF_DIR.glob("ref_*.jpg"):
            old.unlink()
        meta = {"passes": [], "tilt_deg": tilt}
    meta["passes"].append(scan)
    while len(meta["passes"]) > MAX_PASSES:
        gone = meta["passes"].pop(0)
        shutil.rmtree(REF_DIR / Path(gone["refs"][0]["file"]).parent, ignore_errors=True)
    feats = scan_features(meta)
    if not refit_focal(meta, feats):
        print("✗ too few overlapping photos to measure the camera — lights on? "
              "try a smaller --step", file=sys.stderr)
        return 1
    save_meta(meta)
    n = len(meta["passes"])
    marks = sum(len(f[0]) for _, f in landmark_refs(meta, feats)) if n > 1 else 0
    hfov = 2 * math.degrees(math.atan(WORK_W / 2 / meta["focal_px"]))
    heading.save(offset, refs=sum(len(p["refs"]) for p in meta["passes"]), scans=n,
                 ref_at=scan["at"])
    tail = f" · {marks} landmarks" if n > 1 else f" · base at {offset:+.1f}° in the world"
    print(f"✓ {n} scan{'s' if n > 1 else ''} · camera {hfov:.1f}° wide ±{meta['fit_rms_deg']:.1f}°{tail}")
    return 0


def anchor(args) -> int:
    import cv2

    from hostfind import resolve_host

    meta = load_meta()
    if meta is None or not meta.get("focal_px"):
        print("✗ no room scans — run: python scripts/cal-heading.py capture", file=sys.stderr)
        return 1
    host = resolve_host()
    if not ensure_dozing():
        return 1
    refs = landmark_refs(meta, scan_features(meta))
    if not refs:
        print("✗ the scans share no landmarks — run capture --fresh", file=sys.stderr)
        return 1
    focal = float(meta["focal_px"])
    before = heading.offset()
    offs: list[float] = []
    looked = 0
    best = None
    with WatchPaused(), Camera(host) as cam:
        try:
            pose("lifted", tilt=float(meta.get("tilt_deg", 0.0)))
            time.sleep(args.settle + 1.0)
            e0 = encoder_deg(host)
            done: list[float] = []
            for step in VIEW_STEPS[:max(1, args.views)]:
                v = max(-heading.ENC_LIMIT, min(heading.ENC_LIMIT, e0 + step))
                if any(abs(v - d) < 5 for d in done):
                    continue
                done.append(v)
                if step:
                    pose(yaw=v, enc=True)
                    time.sleep(args.settle)
                frame = cam.fresh(time.time())
                e = encoder_deg(host)
                looked += 1
                if looked == 1:
                    shutil.rmtree(LAST_DIR, ignore_errors=True)
                    LAST_DIR.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(LAST_DIR / f"view_{looked}_{e:+06.1f}.jpg"), frame,
                            [cv2.IMWRITE_JPEG_QUALITY, 90])
                found = locate(frame, refs, focal)
                if found is None:
                    print(f"  encoder {e:+6.1f}°  no landmarks in view", flush=True)
                    continue
                off = heading.wrap(found["world"] - e)
                offs.append(off)
                if best is None or found["inliers"] > best["inliers"]:
                    best = found
                print(f"  encoder {e:+6.1f}°  world {found['world']:+6.1f}°  offset {off:+5.1f}°  "
                      f"({found['inliers']} inliers, {found['used']} photos)", flush=True)
                group, _ = consensus(offs)
                if len(group) >= 3 and len(group) >= 0.6 * len(offs):
                    break
        except (RuntimeError, OSError):
            tuck_quietly()
            raise
        group, off = consensus(offs)
        if len(group) < 2:
            why = "no view matched" if not offs else "the views disagree"
            print(f"✗ {why} ({looked} looked) — lights on? someone right in front? "
                  "add a scan if the room changed", file=sys.stderr)
            tuck_quietly()
            return 1
        spread = max(group) - min(group) if len(group) > 1 else 0.0
        if args.dry_run:
            print(f"✓ offset would be {off:+.1f}° (now {before:+.1f}°) — dry run, not saved")
            tuck_quietly()
            return 0
        heading.save(off, inliers=best["inliers"], ref_deg=best["ref_deg"],
                     residual_deg=round(spread, 2), views=len(group))
        tuck_quietly(DOZE_DEG)
    print(f"✓ base turned {off:+.1f}° (was {before:+.1f}°) · {len(group)} of {looked} views agree "
          f"to ±{spread / 2:.1f}°")
    return 0


def refit(_args) -> int:
    meta = load_meta()
    if meta is None:
        print("✗ no room scans — run: python scripts/cal-heading.py capture", file=sys.stderr)
        return 1
    feats = scan_features(meta)
    if not refit_focal(meta, feats):
        print("✗ too few overlapping photos to measure the camera", file=sys.stderr)
        return 1
    save_meta(meta)
    hfov = 2 * math.degrees(math.atan(WORK_W / 2 / meta["focal_px"]))
    marks = landmark_refs(meta, feats)
    print(f"✓ {len(meta['passes'])} scans · camera {hfov:.1f}° wide ±{meta['fit_rms_deg']:.1f}° · "
          f"{sum(len(f[0]) for _, f in marks)} landmark features in {len(marks)} photos")
    return 0


def status(_args) -> int:
    meta = load_meta() or {}
    print(json.dumps({**heading.state(), "focal_px": meta.get("focal_px"),
                      "fit_rms_deg": meta.get("fit_rms_deg"),
                      "scans": [{"at": p["at"], "offset_deg": p["offset_deg"], "photos": len(p["refs"])}
                                for p in meta.get("passes", [])]}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--fresh", action="store_true", help="forget earlier scans (keeps the current world frame)")
    c.add_argument("--zero", action="store_true", help="with --fresh or no scans: world 0 = the base now")
    c.add_argument("--tilt", type=float, default=TILT_DEG,
                   help="head tilt up for a fresh set of scans (later scans reuse it)")
    c.add_argument("--step", type=int, default=20)
    c.add_argument("--settle", type=float, default=1.2)
    a = sub.add_parser("anchor")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--settle", type=float, default=1.2)
    a.add_argument("--views", type=int, default=len(VIEW_STEPS), help="most views to look at")
    sub.add_parser("refit")
    sub.add_parser("status")
    args = ap.parse_args()
    try:
        return {"capture": capture, "anchor": anchor, "refit": refit, "status": status}[args.cmd](args)
    except (RuntimeError, OSError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
