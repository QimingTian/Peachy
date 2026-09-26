"""Peachy head-camera brightness — never uses the laptop webcam.

Frames come from the robot's WebRTC stream (rtcmedia.py), opened on demand and
closed after 30 s idle. Used by watch-room.py, light_probe.py, dashboard.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

def _load_tune() -> dict:
    try:
        data = json.loads((_REPO / ".run" / "light_tune.json").read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}


def _tuned(key: str, env: str, default: str) -> float:
    tune = _load_tune()
    if key in tune:
        return float(tune[key])
    return float(os.environ.get(env, default))


ON_DELTA = _tuned("on_delta", "REACHY_LIGHT_ON_DELTA", "25")
OFF_DELTA = _tuned("off_delta", "REACHY_LIGHT_OFF_DELTA", "12")
EMA_ALPHA = float(os.environ.get("REACHY_LIGHT_EMA", "0.35"))
SLOW_ALPHA = float(os.environ.get("REACHY_LIGHT_SLOW_EMA", "0.01"))
_MIN_JPEG = int(os.environ.get("REACHY_LIGHT_MIN_JPEG", "180"))
_IDLE_CLOSE_S = 30.0

_rtc = None
_rtc_used = 0.0
_rtc_retry_at = 0.0
_rtc_opened = 0.0
_RTC_WARMUP_S = 2.0
_rtc_lock = threading.Lock()


def _jpeg_mean_luma(path: Path) -> float:
    sys.path.insert(0, str(_REPO / "scripts"))
    from light_lab import roi_mean_luma

    return roi_mean_luma(path)


def _jpeg_ok(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > _MIN_JPEG


def _rtc_close_locked() -> None:
    global _rtc
    if _rtc is not None:
        try:
            _rtc.stop()
        except Exception:  # noqa: BLE001
            pass
        _rtc = None


def _idle_reaper() -> None:
    while True:
        time.sleep(5)
        with _rtc_lock:
            if _rtc is not None and time.time() - _rtc_used > _IDLE_CLOSE_S:
                _rtc_close_locked()


threading.Thread(target=_idle_reaper, daemon=True).start()


def robot_frame(timeout: float = 8.0):
    """Latest BGR frame from the robot camera over WebRTC, or None."""
    global _rtc, _rtc_used, _rtc_retry_at, _rtc_opened
    with _rtc_lock:
        _rtc_used = time.time()
        if _rtc is not None and _rtc.error:
            _rtc_close_locked()
        if _rtc is None:
            if time.time() < _rtc_retry_at:
                return None
            try:
                sys.path.insert(0, str(_REPO / "scripts"))
                from hostfind import resolve_host
                from rtcmedia import RtcMedia
                _rtc = RtcMedia(resolve_host(), 640, 360, fps=5, audio=False).start()
                _rtc_opened = time.time()
            except Exception:  # noqa: BLE001
                _rtc = None
                _rtc_retry_at = time.time() + 10
                return None
        m = _rtc
        settle = _rtc_opened + _RTC_WARMUP_S
    end = max(time.time(), settle) + timeout
    while time.time() < settle and not m.error:
        time.sleep(0.1)
    frame = m.frame(max_age=2.0)
    while frame is None and time.time() < end and not m.error:
        time.sleep(0.1)
        frame = m.frame(max_age=2.0)
    return frame


def rtc_close() -> None:
    with _rtc_lock:
        _rtc_close_locked()


def _fetch_robot_jpeg(dest: Path, quality: int = 80) -> bool:
    """Write the latest robot camera frame to *dest* as JPEG."""
    frame = robot_frame()
    if frame is None:
        return False
    import cv2
    return bool(cv2.imwrite(str(dest), frame, [cv2.IMWRITE_JPEG_QUALITY, quality])) and _jpeg_ok(dest)


_REANCHOR_FILE = _REPO / ".run" / "light_reanchor.json"
_CONFIRM_LOG = _REPO / ".run" / "light_confirmations.jsonl"


def schedule_reanchor(*, ref: float, ema: float | None = None, lit: bool | None = None) -> None:
    """Ask watch-room / dashboard sensor to snap adaptive ref on next read."""
    _REANCHOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    _REANCHOR_FILE.write_text(json.dumps({
        "t": time.time(),
        "ref": round(ref, 2),
        "ema": round(ema, 2) if ema is not None else None,
        "lit": lit,
    }) + "\n")


def apply_reanchor(sensor: "LightSensor") -> bool:
    """Apply a pending teacher re-anchor (consumes the file)."""
    try:
        data = json.loads(_REANCHOR_FILE.read_text())
        if time.time() - float(data.get("t", 0)) > 120:
            _REANCHOR_FILE.unlink(missing_ok=True)
            return False
        sensor.slow = float(data["ref"])
        if data.get("ema") is not None:
            sensor.ema = float(data["ema"])
        if "lit" in data and data["lit"] is not None:
            sensor._lit = bool(data["lit"])
        _REANCHOR_FILE.unlink(missing_ok=True)
        return True
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return False


class LightSensor:
    """Head-camera mean-luma with hysteresis (lit vs dark)."""

    def __init__(self) -> None:
        self._tmp = Path(tempfile.gettempdir()) / "peachy_light_probe.jpg"
        self.dark_level: float | None = None
        self._lit = False
        self.raw: float = 0.0
        self.ema: float | None = None
        self.slow: float | None = None
        self.adaptive = True
        self.motion: float = 0.0
        self._prev_luma: float | None = None
        self.last_source: str = ""

    def _lock_exposure(self) -> None:
        """No-op — robot camera auto-exposure; kept for watch-room API compat."""

    def read(self) -> float | None:
        if not _fetch_robot_jpeg(self._tmp):
            return None
        self.last_source = "webrtc"
        try:
            self.raw = _jpeg_mean_luma(self._tmp)
        except OSError:
            return None
        if self._prev_luma is not None:
            self.motion = abs(self.raw - self._prev_luma)
        self._prev_luma = self.raw
        a = EMA_ALPHA
        self.ema = self.raw if self.ema is None else (1 - a) * self.ema + a * self.raw
        if self.slow is None:
            self.slow = self.raw
        elif not self._lit or self.raw < self.slow:
            self.slow += SLOW_ALPHA * (self.raw - self.slow)
        # Ref can stay high after lights-off; drag it down when clearly dark.
        on_d = _tuned("on_delta", "REACHY_LIGHT_ON_DELTA", "25")
        if (
            self.slow is not None
            and self.ema is not None
            and not self._lit
            and self.ema < self.slow - on_d * 0.35
        ):
            self.slow = 0.82 * self.slow + 0.18 * self.ema
        return self.ema

    def calibrate_dark(self, seconds: float = 2.0) -> float:
        vals: list[float] = []
        end = time.time() + seconds
        while time.time() < end:
            v = self.read()
            if v is not None:
                vals.append(v)
            time.sleep(max(0.15, seconds / 8))
        self.dark_level = (sum(vals) / len(vals)) if vals else 0.0
        self.ema = self.dark_level
        self.slow = self.dark_level
        return self.dark_level

    def lit(self, *, fresh: bool = True) -> bool:
        v = self.read() if fresh or self.ema is None else self.ema
        if v is None:
            return self._lit
        on_d = _tuned("on_delta", "REACHY_LIGHT_ON_DELTA", "25")
        off_d = _tuned("off_delta", "REACHY_LIGHT_OFF_DELTA", "12")
        ref = self.slow if self.adaptive else self.dark_level
        if ref is None:
            return self._lit
        if not self._lit and v >= ref + on_d:
            self._lit = True
        elif self._lit and v <= ref + off_d:
            self._lit = False
        return self._lit

    @property
    def reference(self) -> float:
        r = self.slow if self.adaptive else self.dark_level
        return r if r is not None else 0.0

    def snapshot(self) -> dict:
        sys.path.insert(0, str(_REPO / "scripts"))
        from light_lab import preferred_roi

        on_d = _tuned("on_delta", "REACHY_LIGHT_ON_DELTA", "25")
        off_d = _tuned("off_delta", "REACHY_LIGHT_OFF_DELTA", "12")
        return {
            "raw": round(self.raw, 1),
            "ema": round(self.ema or 0, 1),
            "ref": round(self.reference, 1),
            "delta": round((self.ema or 0) - self.reference, 1),
            "lit": self._lit,
            "motion": round(self.motion, 1),
            "on_delta": on_d,
            "off_delta": off_d,
            "source": "peachy_camera",
            "roi": preferred_roi(),
        }

    def close(self) -> None:
        try:
            self._tmp.unlink(missing_ok=True)
        except OSError:
            pass
