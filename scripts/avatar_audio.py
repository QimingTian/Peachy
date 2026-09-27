"""Two-way audio between this Mac and the robot over the daemon's WebRTC.

The robot's mic plays on this Mac's default output (the AirPods when they are
connected); this Mac's built-in mic plays on the robot's speaker. One WebRTC
consumer of the daemon's "reachymini" producer (ws://<robot>:8443) carries both:
the daemon sends Opus from the robot mic and plays whatever audio the consumer
sends back (its own jitter buffer, speaker EQ and echo reference for the robot
mic). The video track is taken and dropped.

    audio = AvatarAudio(host).start()
    audio.status()                  # {"audio": "live", ...}
    audio.stop()

The built-in mic is picked by name so the AirPods stay in their high-quality
output mode (opening their mic would switch them to the call profile).

poke() sends a harmless command on the daemon's data channel. Any message there
cancels the daemon's sleep-after-app, which is how ctl-avatar.py keeps Peachy
awake once the avatar app has stopped. A LAN session like this one never takes
the robot's app slot, so it doesn't hold anything up.
"""

from __future__ import annotations

import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from rtcmedia import find_producer  # noqa: E402

Gst.init(None)

RATE = 16000                # the daemon's Opus leg: 16 kHz stereo
CHANNELS = 2
RX_LATENCY_MS = 60          # our jitter buffer for the robot mic (webrtcbin default 200)
RETRY_S = 3.0
BUILTIN_MIC = "BuiltInMicrophoneDevice"
POKE = '{"type": "get_version"}'


def mic_element(name: str | None) -> tuple[Gst.Element, str]:
    """This Mac's mic: *name* (display name or CoreAudio unique id), else the built-in one."""
    mon = Gst.DeviceMonitor()
    mon.add_filter("Audio/Source", None)
    mon.start()
    try:
        devs = mon.get_devices() or []
    finally:
        mon.stop()

    def uid(d) -> str:
        p = d.get_properties()
        return (p.get_string("unique-id") if p else None) or ""

    want = [d for d in devs if name and name in (d.get_display_name(), uid(d))] if name else \
        [d for d in devs if uid(d) == BUILTIN_MIC]
    if not want:
        have = ", ".join(d.get_display_name() for d in devs) or "none"
        raise RuntimeError(f"no mic {name or 'built-in'!r} on this Mac (have: {have})")
    return want[0].create_element(None), want[0].get_display_name()


class AvatarAudio:
    def __init__(self, host: str, mic: str | None = None, send: bool = True, recv: bool = True,
                 volume: float = 1.0):
        self.host = host
        self.mic_name = mic
        self.send, self.recv = send, recv
        self.volume = volume
        self._loop = GLib.MainLoop()
        self._pipe: Gst.Pipeline | None = None
        self._webrtcbin = None
        self._channel = None
        self._tx_ready = False
        self._stop = threading.Event()
        self._restart = threading.Event()
        self.state = "connecting"
        self.mic_label = ""
        self.rx = False
        self.tx = False
        self.error = ""
        self.since = 0.0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> "AvatarAudio":
        threading.Thread(target=self._loop.run, name="avatar-audio-loop", daemon=True).start()
        threading.Thread(target=self._supervise, name="avatar-audio", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        self._teardown()
        self._loop.quit()

    def status(self) -> dict:
        return {"audio": self.state, "mic": self.mic_label, "to_robot": self.tx,
                "from_robot": self.rx, "audio_err": self.error}

    def poke(self) -> bool:
        ch = self._channel
        if ch is None:
            return False
        try:
            ch.emit("send-string", POKE)
            return True
        except Exception:  # noqa: BLE001 - channel closing
            return False

    def _supervise(self) -> None:
        while not self._stop.is_set():
            self._restart.clear()
            try:
                self._build()
            except Exception as e:  # noqa: BLE001 - keep retrying while the session runs
                self._fail(str(e))
            self._restart.wait()
            self._teardown()
            if not self._stop.is_set():
                self.state = "reconnecting"
                self._stop.wait(RETRY_S)

    def _fail(self, why: str) -> None:
        self.error = why[:200]
        self.state = "error"
        self._restart.set()

    def _teardown(self) -> None:
        pipe, self._pipe = self._pipe, None
        self._webrtcbin, self._channel, self._tx_ready = None, None, False
        self.rx = self.tx = False
        if pipe is not None:
            pipe.set_state(Gst.State.NULL)

    # ------------------------------------------------------------------ pipeline

    def _build(self) -> None:
        peer = find_producer(self.host)
        pipe = Gst.Pipeline.new("avatar-audio")
        src = Gst.ElementFactory.make("webrtcsrc")
        if src is None:
            raise RuntimeError("GStreamer webrtcsrc missing (gst-plugins-rs)")
        sig = src.get_property("signaller")
        sig.set_property("producer-peer-id", peer)
        sig.set_property("uri", f"ws://{self.host}:8443")
        src.connect("deep-element-added", self._on_deep_element)
        src.connect("pad-added", self._on_pad)
        pipe.add(src)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        bus.connect("message::eos", lambda *_: self._fail("stream ended"))
        self._pipe = pipe
        self.error = ""
        self.state = "connecting"
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("could not start the WebRTC pipeline")

    def _on_deep_element(self, _bin, _sub, element) -> None:
        f = element.get_factory()
        if f is not None and f.get_name() == "webrtcbin" and self._webrtcbin is None:
            self._webrtcbin = element
            element.set_property("latency", RX_LATENCY_MS)
            element.connect("on-new-transceiver", self._on_transceiver)
            element.connect("on-data-channel", self._on_data_channel)

    def _on_data_channel(self, _webrtcbin, channel) -> None:
        if channel.get_property("label") == "data":
            self._channel = channel

    @staticmethod
    def _on_transceiver(_webrtcbin, trans) -> None:
        # The daemon offers sendrecv audio; answer sendrecv so it plays what we send.
        caps = trans.get_property("codec-preferences")
        if caps is not None and caps.get_size() > 0 \
                and caps.get_structure(0).get_string("media") != "audio":
            return
        trans.set_property("direction", 4)  # GstWebRTCRTPTransceiverDirection.SENDRECV

    def _add(self, desc: str) -> Gst.Bin:
        bin_ = Gst.parse_bin_from_description(desc, True)
        self._pipe.add(bin_)
        return bin_

    def _on_pad(self, _src, pad: Gst.Pad) -> None:
        if self._pipe is None:
            return
        name = pad.get_name()
        if name.startswith("audio") and self.recv:
            bin_ = self._add(
                "queue max-size-time=200000000 leaky=downstream ! audioconvert ! audioresample ! "
                f"volume volume={self.volume} ! osxaudiosink buffer-time=40000 latency-time=10000")
            pad.link(bin_.get_static_pad("sink"))
            bin_.sync_state_with_parent()
            self.rx = True
        else:
            bin_ = self._add("fakesink sync=false async=false")
            pad.link(bin_.get_static_pad("sink"))
            bin_.sync_state_with_parent()
        if name.startswith("audio"):
            if self.send:
                try:
                    self._setup_tx()
                except Exception as e:  # noqa: BLE001
                    self._fail(f"mic: {e}")
                    return
            self.state = "live"
            self.since = time.time()

    def _setup_tx(self) -> None:
        """Mac mic -> Opus -> the webrtcbin's audio sink pad (same leg the daemon offers)."""
        if self._tx_ready or self._webrtcbin is None:
            return
        sink_pad, pt = None, 96
        it = self._webrtcbin.iterate_sink_pads()
        while True:
            res, pad = it.next()
            if res != Gst.IteratorResult.OK:
                break
            if pad.is_linked():
                continue
            caps = pad.query_caps(None)
            if caps and caps.get_size() > 0:
                s = caps.get_structure(0)
                if (s.get_string("encoding-name") or "").upper() == "OPUS":
                    sink_pad = pad
                    ok, val = s.get_int("payload")
                    pt = val if ok else pt
                    break
        if sink_pad is None:
            raise RuntimeError("the robot offered no audio return channel")
        mic, self.mic_label = mic_element(self.mic_name)
        mic.set_property("buffer-time", 40000)
        mic.set_property("latency-time", 10000)
        enc = self._add(
            f"audioconvert ! audioresample ! audio/x-raw,rate={RATE},channels={CHANNELS} ! "
            "opusenc audio-type=restricted-lowdelay frame-size=10 inband-fec=true "
            f"packet-loss-percentage=10 ! rtpopuspay pt={pt}")
        self._pipe.add(mic)
        mic.link(enc)
        if enc.get_static_pad("src").link_full(sink_pad, Gst.PadLinkCheck.NOTHING) != Gst.PadLinkReturn.OK:
            raise RuntimeError("could not link the mic to the WebRTC audio track")
        enc.sync_state_with_parent()
        mic.sync_state_with_parent()
        self._pipe.recalculate_latency()
        self._tx_ready = True
        self.tx = True

    def _on_error(self, _bus, msg) -> None:
        err, _dbg = msg.parse_error()
        f = msg.src.get_factory() if msg.src is not None and hasattr(msg.src, "get_factory") else None
        # webrtcsrc's internal video appsrc is never negotiated (we answer sendrecv); harmless.
        if f is not None and f.get_name() == "appsrc" and "not-negotiated" in str(err):
            return
        self._fail(str(err))


if __name__ == "__main__":
    import os
    import sys

    from hostfind import resolve_host

    a = AvatarAudio(os.environ.get("REACHY_HOST") or resolve_host(),
                    mic=sys.argv[1] if len(sys.argv) > 1 else None).start()
    try:
        while True:
            time.sleep(1)
            print(a.status(), flush=True)
    except KeyboardInterrupt:
        a.stop()
