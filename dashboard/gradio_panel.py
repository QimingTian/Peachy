"""Desktop Gradio panel — camera + dashboard buttons + conversation iframe.

Mounted at /desk on the Peachy dashboard (same port as the phone UI).
Uses the dashboard REST API so it stays a thin client over existing scripts.

  http://localhost:8080/desk?k=<token>
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import gradio as gr

from voice_catalog import OPENAI_VOICES, catalog_for_ids

_REPO = Path(__file__).resolve().parent.parent
_RUN = _REPO / ".run"
_TOKEN_FILE = _RUN / "peachy_token"
_DASH_PORT = int(os.environ.get("PEACHY_PORT", "8080"))
_BASE = os.environ.get("PEACHY_DASHBOARD_URL", f"http://127.0.0.1:{_DASH_PORT}").rstrip("/")
_CONVO_URL = os.environ.get("PEACHY_CONVERSATION_GRADIO_URL", "http://127.0.0.1:7860/").rstrip("/")

_EXPRESSIONS = (
    ("yes", "👍 Yes"),
    ("no", "🙅 No"),
    ("curious", "🤔 Curious"),
    ("excited", "🎉 Excited"),
    ("wink", "😉 Wink"),
    ("listen", "👂 Listen"),
)


def _token() -> str:
    tok = os.environ.get("PEACHY_TOKEN", "").strip()
    if tok:
        return tok
    if _TOKEN_FILE.is_file():
        return _TOKEN_FILE.read_text().strip()
    return ""


def _post(path: str, *, timeout: float = 120.0) -> tuple[bool, str]:
    tok = _token()
    url = f"{_BASE}{path}"
    if tok:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}k={tok}"
    req = urllib.request.Request(url, method="POST", data=b"{}")
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("X-Peachy-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            data = json.loads(raw) if raw else {}
            ok = data.get("ok") is not False
            msg = (data.get("msg") or "ok").strip().split("\n")[-1][:240]
            return ok, msg
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        return False, f"HTTP {e.code}: {body or e.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, str(e)[:200]


def _snap_url() -> str:
    """Relative URL — safe when desk is opened via LAN IP (avoids 127.0.0.1 validation)."""
    tok = _token()
    q = f"?k={tok}&" if tok else "?"
    return f"/static/snap.jpg{q}t={int(time.time())}"


def refresh_snap(_evt=None) -> tuple[str | None, str]:
    ok, msg = _post("/api/snap", timeout=45)
    if not ok:
        return None, msg or "camera unavailable"
    return _snap_url(), msg or "frame updated"


def snap_wake(_evt=None) -> tuple[str | None, str]:
    ok, msg = _post("/api/snap", timeout=90)
    if not ok:
        return None, msg
    return _snap_url(), msg or "picture taken"


def do_action(path: str, label: str) -> str:
    ok, msg = _post(path)
    return f"{'✓' if ok else '✗'} {label}: {msg}"


def _convo_json(path: str, *, method: str = "GET", timeout: float = 12.0) -> dict | list:
    url = f"{_CONVO_URL}{path}"
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def _current_voice_id() -> str | None:
    try:
        data = _convo_json("/voices/current")
        if isinstance(data, dict):
            v = data.get("voice")
            return str(v) if v else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError, TypeError):
        pass
    return None


def _current_voice_label() -> str:
    vid = _current_voice_id()
    if vid:
        cards = catalog_for_ids([vid])
        hint = f" — {cards[0]['hint']}" if cards else ""
        return f"**Current voice:** `{vid}`{hint}"
    return "**Current voice:** *(start conversation with Gradio on :7860)*"


def _voice_card_md(card: dict[str, str], current: str | None) -> str:
    active = current and card["id"].lower() == current.lower()
    badge = " · **✓ active**" if active else ""
    return (
        f"**{card['label']}**{badge}  \n"
        f"<span style='color:#6b5b6e;font-size:0.9em'>{card['hint']}</span>"
    )


def apply_convo_voice(voice_id: str) -> str:
    try:
        q = urllib.parse.urlencode({"voice": voice_id})
        data = _convo_json(f"/voices/apply?{q}", method="POST", timeout=15)
        if isinstance(data, dict) and data.get("ok") is False:
            return f"✗ Voice: {data.get('error', data.get('status', 'failed'))}"
        status = data.get("status", f"now using {voice_id}") if isinstance(data, dict) else voice_id
        return f"✓ Voice → **{voice_id}** — {status}\n\n{_current_voice_label()}"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return f"✗ Voice: {e}"


def load_voice_panel() -> tuple[str, list[dict[str, str]], str | None]:
    current = _current_voice_id()
    try:
        voices = _convo_json("/voices")
        if not isinstance(voices, list):
            voices = []
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError):
        voices = []
    if not voices:
        voices = [v["id"] for v in OPENAI_VOICES]
    return _current_voice_label(), catalog_for_ids([str(v) for v in voices]), current


def _idle_motion_hint(scale: float) -> str:
    pct = int(round(scale * 100))
    if pct <= 5:
        return "Nearly still — almost no idle neck/antenna motion"
    if pct <= 35:
        return "Calm — subtle breathing, less twitchy"
    if pct <= 85:
        return "Gentle idle sway"
    if pct <= 110:
        return "Factory default idle motion"
    return "Very lively — big antenna sweeps"


def _load_idle_motion_scale() -> float:
    try:
        data = _convo_json("/idle-motion")
        if isinstance(data, dict) and "scale" in data:
            return float(data["scale"])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError, TypeError, ValueError):
        pass
    scale_file = _RUN / "idle_motion_scale.json"
    try:
        if scale_file.is_file():
            return float(json.loads(scale_file.read_text()).get("scale", 1.0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return 1.0


def apply_idle_motion_pct(pct: float) -> tuple[float, str]:
    scale = max(0.0, min(1.5, float(pct) / 100.0))
    try:
        q = urllib.parse.urlencode({"scale": scale})
        data = _convo_json(f"/idle-motion/apply?{q}", method="POST", timeout=12)
        if isinstance(data, dict) and data.get("ok") is False:
            return scale, f"✗ Idle motion: {data.get('error', 'failed')}"
        applied = float(data.get("scale", scale)) if isinstance(data, dict) else scale
        return applied * 100.0, f"✓ Idle motion → **{int(round(applied * 100))}%** — {_idle_motion_hint(applied)}"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return pct, f"✗ Idle motion: {e} *(needs conversation on :7860 with Peachy patch)*"


def mount_peachy_desk(fastapi_app) -> None:
    """Attach Gradio blocks at /desk."""
    convo_src = _CONVO_URL

    with gr.Blocks(title="Peachy Desk", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            "# Peachy desk panel\n"
            "Camera + motion controls on the left; **conversation Gradio** (robot) embedded below. "
            f"Phone dashboard: [`{_BASE}/`]({_BASE}/)."
        )
        status = gr.Textbox(label="Status", interactive=False, lines=2)

        with gr.Row():
            with gr.Column(scale=4):
                snap = gr.Image(label="Peachy camera", type="filepath", height=320)
                with gr.Row():
                    btn_refresh = gr.Button("📷 Refresh", variant="secondary")
                    btn_picture = gr.Button("📷 Wake + picture", variant="primary")
                gr.Markdown("*Refresh* = one frame, no wake. *Wake + picture* = same as dashboard snap.")

            with gr.Column(scale=3):
                gr.Markdown("### Motion")
                with gr.Row():
                    b_wake = gr.Button("🌞 Wake")
                    b_sleep = gr.Button("😴 Sleep")
                    b_toggle = gr.Button("🔁 Toggle")
                with gr.Row():
                    b_abort = gr.Button("⛔ Abort")
                    b_shutdown = gr.Button("🌙 Shut down")

                gr.Markdown("### Talk")
                with gr.Row():
                    b_cv_start = gr.Button("💬 Start conversation")
                    b_cv_stop = gr.Button("⏹ Stop conversation")
                with gr.Row():
                    b_cv_gradio = gr.Button("🎛 Start + Gradio (robot)")

                gr.Markdown("### Expressions")
                ex_rows: list[gr.Button] = []
                with gr.Row():
                    for key, label in _EXPRESSIONS[:3]:
                        ex_rows.append(gr.Button(label, size="sm"))
                with gr.Row():
                    for key, label in _EXPRESSIONS[3:]:
                        ex_rows.append(gr.Button(label, size="sm"))

                gr.Markdown("### Voice")
                gr.Markdown(
                    "Pick a personality — each button switches Peachy’s realtime voice. "
                    "Descriptions are approximate; try a few."
                )
                voice_current = gr.Markdown(_current_voice_label())
                voice_status = gr.Markdown("")
                _init_label, _init_voices, _init_current = load_voice_panel()
                _desk_path = "/desk"
                _tok = _token()
                if _tok:
                    _desk_path = f"/desk?k={_tok}"
                gr.Button("↻ Reload voice list", link=_desk_path, size="sm")
                for pair in (_init_voices[i:i + 2] for i in range(0, len(_init_voices), 2)):
                    with gr.Row():
                        for card in pair:
                            with gr.Column(scale=1, min_width=220):
                                gr.Markdown(_voice_card_md(card, _init_current))
                                vb = gr.Button(
                                    f"Use {card['label']}",
                                    size="sm",
                                    variant="primary" if (
                                        _init_current
                                        and card["id"].lower() == _init_current.lower()
                                    ) else "secondary",
                                )
                                vb.click(
                                    lambda v=card["id"]: apply_convo_voice(v),
                                    outputs=voice_status,
                                ).then(
                                    lambda: _current_voice_label(),
                                    outputs=voice_current,
                                )

                gr.Markdown("### Idle motion")
                gr.Markdown(
                    "How much Peachy fidgets (neck breathing + antenna sway) while idle in conversation. "
                    "Slide down if it feels twitchy."
                )
                _init_idle_pct = int(round(_load_idle_motion_scale() * 100))
                idle_slider = gr.Slider(
                    minimum=0,
                    maximum=150,
                    value=_init_idle_pct,
                    step=5,
                    label="Idle fidget level (100% = factory default)",
                )
                idle_status = gr.Markdown(_idle_motion_hint(_init_idle_pct / 100.0))

        gr.Markdown("### Conversation UI (tunneled from robot :7860)")
        gr.Markdown(
            "**To talk:** click inside the embed below → allow **microphone** → wait for "
            "the stream to connect (~10s). Idle neck/antenna motion means the app is running; "
            "voice needs the mic link. If the embed misbehaves, open Gradio in its own tab "
            f"([`{convo_src}`]({convo_src})) — mic works more reliably there."
        )
        gr.HTML(
            f'<p><a href="{convo_src}" target="_blank" rel="noopener">Open conversation in new tab ↗</a></p>'
            f'<iframe src="{convo_src}" width="100%" height="640" '
            'allow="microphone; camera" '
            'style="border:2px solid #34203F;border-radius:12px" '
            'title="Reachy conversation Gradio"></iframe>'
        )

        outs = [status, snap]
        btn_refresh.click(refresh_snap, outputs=[snap, status])
        btn_picture.click(snap_wake, outputs=[snap, status])
        b_wake.click(lambda: do_action("/api/do/wake", "Wake"), outputs=status)
        b_sleep.click(lambda: do_action("/api/do/sleep", "Sleep"), outputs=status)
        b_toggle.click(lambda: do_action("/api/do/toggle", "Toggle"), outputs=status)
        b_abort.click(lambda: do_action("/api/abort", "Abort"), outputs=status)
        b_shutdown.click(lambda: do_action("/api/shutdown", "Shut down"), outputs=status)
        b_cv_start.click(lambda: do_action("/api/converse/start", "Start conversation"), outputs=status)
        b_cv_stop.click(lambda: do_action("/api/converse/stop", "Stop conversation"), outputs=status)
        b_cv_gradio.click(
            lambda: do_action("/api/converse/start-gradio", "Start + Gradio"),
            outputs=status,
        )

        for (key, _label), btn in zip(_EXPRESSIONS, ex_rows, strict=True):
            btn.click(
                lambda k=key: do_action(f"/api/express/{k}", k),
                outputs=status,
            )

        def _on_idle_change(pct: float) -> tuple[float, str]:
            new_pct, msg = apply_idle_motion_pct(pct)
            hint = msg if msg.startswith("✗") else _idle_motion_hint(new_pct / 100.0)
            if msg.startswith("✓"):
                hint = msg
            return new_pct, hint

        idle_slider.release(_on_idle_change, inputs=idle_slider, outputs=[idle_slider, idle_status])

        demo.load(refresh_snap, outputs=[snap, status])
        demo.load(lambda: _current_voice_label(), outputs=voice_current)
        demo.load(
            lambda: (_load_idle_motion_scale() * 100.0, _idle_motion_hint(_load_idle_motion_scale())),
            outputs=[idle_slider, idle_status],
        )

    gr.mount_gradio_app(fastapi_app, demo, path="/desk")
