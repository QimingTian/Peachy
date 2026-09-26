class PeachyRTC {
  constructor(video, { onState } = {}) {
    this.video = video;
    this.onState = onState || (() => {});
    this.state = "off";
    this.ws = null;
    this.pc = null;
    this.session = null;
    this.stream = null;
  }

  _set(state, detail) {
    this.state = state;
    this.onState(state, detail);
  }

  start(host) {
    this.stop(true);
    this._set("connecting");
    const ws = new WebSocket(`ws://${host}:8443`);
    this.ws = ws;
    const fail = msg => { if (this.ws === ws) { this.stop(true); this._set("error", msg); } };
    this._timer = setTimeout(() => fail("No video from Peachy"), 15000);
    ws.onerror = () => fail("Can’t reach Peachy’s video server");
    ws.onclose = () => { if (this.ws === ws && this.state !== "off") fail("Video connection closed"); };
    ws.onmessage = async e => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      if (m.type === "welcome") ws.send(JSON.stringify({ type: "list" }));
      else if (m.type === "list") {
        const p = (m.producers || []).find(x => (x.meta || {}).name === "reachymini") || (m.producers || [])[0];
        if (!p) return fail("Peachy isn’t streaming");
        ws.send(JSON.stringify({ type: "startSession", peerId: p.id }));
      } else if (m.type === "sessionStarted") {
        this.session = m.sessionId;
      } else if (m.type === "peer") {
        if (m.sessionId && !this.session) this.session = m.sessionId;
        try {
          if (m.sdp) await this._offer(m.sdp);
          else if (m.ice && this.pc) await this.pc.addIceCandidate(m.ice);
        } catch (err) { fail(String(err && err.message || err)); }
      } else if (m.type === "endSession") {
        fail("Peachy ended the stream");
      } else if (m.type === "error") {
        fail(m.details || "Video server error");
      }
    };
  }

  async _offer(sdp) {
    const pc = new RTCPeerConnection({ iceServers: [] });
    this.pc = pc;
    this.stream = new MediaStream();
    this.video.srcObject = this.stream;
    pc.ontrack = e => {
      this.stream.addTrack(e.track);
      if (e.track.kind === "video") {
        const go = () => { clearTimeout(this._timer); if (this.state !== "live") this._set("live"); };
        this.video.onplaying = go;
        this.video.play().catch(() => {});
      }
    };
    pc.onicecandidate = e => {
      if (e.candidate && this.ws && this.ws.readyState === 1)
        this.ws.send(JSON.stringify({ type: "peer", sessionId: this.session, ice: e.candidate.toJSON() }));
    };
    pc.onconnectionstatechange = () => {
      if (pc !== this.pc) return;
      if (pc.connectionState === "failed") { this.stop(true); this._set("error", "Video link failed"); }
    };
    await pc.setRemoteDescription(sdp);
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    this.ws.send(JSON.stringify({ type: "peer", sessionId: this.session, sdp: pc.localDescription.toJSON() }));
  }

  get hasAudio() {
    return !!(this.stream && this.stream.getAudioTracks().length);
  }

  setMuted(m) {
    this.video.muted = m;
    if (!m) this.video.play().catch(() => {});
  }

  async stats() {
    if (!this.pc) return null;
    const r = await this.pc.getStats();
    let out = null;
    r.forEach(s => {
      if (s.type === "inbound-rtp" && s.kind === "video")
        out = { w: s.frameWidth, h: s.frameHeight, fps: s.framesPerSecond, bytes: s.bytesReceived, t: s.timestamp };
    });
    return out;
  }

  stop(silent) {
    clearTimeout(this._timer);
    const ws = this.ws, pc = this.pc;
    this.ws = null; this.pc = null;
    if (ws) {
      try { if (this.session && ws.readyState === 1) ws.send(JSON.stringify({ type: "endSession", sessionId: this.session })); } catch { }
      ws.onclose = null; ws.close();
    }
    if (pc) pc.close();
    this.session = null;
    if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    this.stream = null;
    this.video.srcObject = null;
    if (!silent) this._set("off");
    else this.state = "off";
  }
}
window.PeachyRTC = PeachyRTC;
