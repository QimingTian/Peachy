"""Receive-only WebRTC media from the robot (camera + mic) on this machine.

The daemon's GStreamer webrtcsink publishes a producer named "reachymini" on
ws://<robot>:8443. Each consumer costs the robot one encoder, so a process
should open a single RtcMedia and share it.

    media = RtcMedia(host).start()
    frame = media.frame()          # BGR ndarray or None
    pcm = media.audio(1600)        # int16 mono @16 kHz, blocks until ready
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque

import numpy as np

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

AUDIO_RATE = 16000


def find_producer(host: str, port: int = 8443, name: str = "reachymini",
                  timeout: float = 5.0) -> str:
    from websockets.sync.client import connect

    with connect(f"ws://{host}:{port}", open_timeout=timeout) as ws:
        deadline = time.time() + timeout
        ws.send(json.dumps({"type": "setPeerStatus", "roles": ["listener"]}))
        ws.send(json.dumps({"type": "list"}))
        while time.time() < deadline:
            msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
            if msg.get("type") == "list":
                for p in msg.get("producers", []):
                    if (p.get("meta") or {}).get("name") == name:
                        return p["id"]
                break
    raise RuntimeError(f"no WebRTC producer '{name}' on {host}:{port}")


class RtcMedia:
    def __init__(self, host: str, width: int = 640, height: int = 360,
                 fps: int = 15, video: bool = True, audio: bool = True):
        self.host = host
        self.size = (width, height)
        self.fps = fps
        self.want_video = video
        self.want_audio = audio
        self._pipe: Gst.Pipeline | None = None
        self._loop = GLib.MainLoop()
        self._frame: np.ndarray | None = None
        self._frame_ts = 0.0
        self._pcm: deque[np.ndarray] = deque()
        self._pcm_len = 0
        self._cv = threading.Condition()
        self.error: str | None = None

    def start(self) -> "RtcMedia":
        peer = find_producer(self.host)
        pipe = Gst.Pipeline.new("rtc")
        src = Gst.ElementFactory.make("webrtcsrc")
        if src is None:
            raise RuntimeError("GStreamer webrtcsrc missing (gst-plugins-rs)")
        sig = src.get_property("signaller")
        sig.set_property("producer-peer-id", peer)
        sig.set_property("uri", f"ws://{self.host}:8443")
        src.connect("pad-added", self._on_pad)
        pipe.add(src)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self._pipe = pipe
        threading.Thread(target=self._loop.run, daemon=True).start()
        pipe.set_state(Gst.State.PLAYING)
        return self

    def _chain(self, pad: Gst.Pad, desc: str, on_sample=None) -> None:
        bin_ = Gst.parse_bin_from_description(desc, True)
        self._pipe.add(bin_)
        if on_sample is not None:
            bin_.get_by_name("out").connect("new-sample", on_sample)
        pad.link(bin_.get_static_pad("sink"))
        bin_.sync_state_with_parent()

    def _on_pad(self, _src, pad: Gst.Pad) -> None:
        name = pad.get_name()
        if name.startswith("video"):
            if not self.want_video:
                return self._chain(pad, "fakesink sync=false")
            w, h = self.size
            self._chain(
                pad,
                "queue leaky=downstream max-size-buffers=2 ! videoconvert ! videoscale ! "
                f"videorate drop-only=true ! video/x-raw,format=BGR,width={w},height={h},"
                f"framerate={self.fps}/1 ! appsink name=out emit-signals=true drop=true "
                "max-buffers=1 sync=false",
                self._on_video,
            )
        elif name.startswith("audio"):
            if not self.want_audio:
                return self._chain(pad, "fakesink sync=false")
            self._chain(
                pad,
                "queue ! audioconvert ! audioresample ! "
                f"audio/x-raw,format=S16LE,rate={AUDIO_RATE},channels=1,layout=interleaved ! "
                "appsink name=out emit-signals=true sync=false",
                self._on_audio,
            )

    def _on_video(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            w, h = self.size
            frame = np.frombuffer(info.data, dtype=np.uint8).copy()
            buf.unmap(info)
            if frame.size == w * h * 3:
                with self._cv:
                    self._frame = frame.reshape(h, w, 3)
                    self._frame_ts = time.time()
        return Gst.FlowReturn.OK

    def _on_audio(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            chunk = np.frombuffer(info.data, dtype=np.int16).copy()
            buf.unmap(info)
            with self._cv:
                self._pcm.append(chunk)
                self._pcm_len += chunk.size
                while self._pcm_len > AUDIO_RATE * 4:
                    self._pcm_len -= self._pcm.popleft().size
                self._cv.notify_all()
        return Gst.FlowReturn.OK

    def _on_error(self, _bus, msg) -> None:
        err, _dbg = msg.parse_error()
        src = msg.src.get_factory().get_name() if msg.src and msg.src.get_factory() else ""
        if src == "appsrc" and "not-negotiated" in str(err):
            return
        self.error = str(err)
        with self._cv:
            self._cv.notify_all()

    def frame(self, max_age: float = 1.0) -> np.ndarray | None:
        with self._cv:
            if self._frame is None or time.time() - self._frame_ts > max_age:
                return None
            return self._frame

    def audio(self, n: int, timeout: float = 2.0) -> np.ndarray | None:
        deadline = time.time() + timeout
        with self._cv:
            while self._pcm_len < n and not self.error:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(left)
            if self.error:
                return None
            out = np.concatenate(list(self._pcm))
            rest = out[n:]
            self._pcm.clear()
            if rest.size:
                self._pcm.append(rest)
            self._pcm_len = rest.size
            return out[:n]

    def stop(self) -> None:
        if self._pipe is not None:
            self._pipe.set_state(Gst.State.NULL)
            self._pipe = None
        self._loop.quit()
