#!/usr/bin/env python3
"""Print/save a QR code for the Peachy dashboard URL.

Tape the PNG (or scan the terminal QR) next to Peachy — anyone can point a phone
at it. For a **constant** printable QR, pin a stable URL once (funnel hostname
or your chosen address); the access token in .run/peachy_token stays the same
across reboots unless you delete it.

  python scripts/tool-qr.py                    # best URL we know
  python scripts/tool-qr.py --from-run         # used by ./run.sh (reads .run/)
  python scripts/tool-qr.py --pin              # save current URL → .run/peachy_qr_url
  python scripts/tool-qr.py --url 'http://…'   # explicit URL (+ --pin to keep it)

Resolution order:
  1. --url  or  PEACHY_QR_URL  or  .run/peachy_qr_url   (pinned — use for print)
  2. https://{peachy_fqdn}/?k=…   (Tailscale Funnel — stable after net-deploy)
  3. http://{LAN-ip}:{port}/?k=…  (session; run with --pin when IP is final)

Env: PEACHY_FQDN, PEACHY_QR_URL, PEACHY_LAN_IP (set by run.sh), PEACHY_PORT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import qrcode

_RUN = Path(__file__).resolve().parent.parent / ".run"
_QR_URL_FILE = _RUN / "peachy_qr_url"
_FQDN_FILE = _RUN / "peachy_fqdn"
_TOKEN_FILE = _RUN / "peachy_token"
_PORT_FILE = _RUN / "peachy_dashboard.port"


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _token() -> str:
    return os.environ.get("PEACHY_TOKEN", "").strip() or _read(_TOKEN_FILE)


def _with_token(base: str, token: str) -> str:
    base = base.rstrip("/")
    if "?k=" in base or "&k=" in base:
        return base
    if not token:
        return f"{base}/"
    return f"{base}/?k={token}"


def resolve_url(*, port: int = 8080, lan_ip: str | None = None) -> tuple[str, str]:
    """Return (url, source) where source is pinned|funnel|lan."""
    explicit = os.environ.get("PEACHY_QR_URL", "").strip() or _read(_QR_URL_FILE)
    if explicit:
        return explicit, "pinned"

    token = _token()
    fqdn = os.environ.get("PEACHY_FQDN", "").strip() or _read(_FQDN_FILE)
    if fqdn:
        host = fqdn if fqdn.startswith("http") else f"https://{fqdn}"
        return _with_token(host, token), "funnel"

    ip = (lan_ip or os.environ.get("PEACHY_LAN_IP", "").strip()
          or os.environ.get("PEACHY_QR_HOST", "").strip())
    if not ip:
        ip = _detect_lan_ip()
    if not ip:
        raise SystemExit(
            "No dashboard URL known. Use --url, set PEACHY_QR_URL, run "
            "./scripts/net-deploy.sh (Funnel), or ./run.sh on the LAN first."
        )
    return _with_token(f"http://{ip}:{port}", token), "lan"


def _detect_lan_ip() -> str:
    import subprocess

    for iface in ("en0", "en1"):
        try:
            out = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=2,
            )
            ip = (out.stdout or "").strip()
            if ip:
                return ip
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ""


def save_qr_png(url: str, dest: Path | None = None) -> Path:
    """Write QR PNG for *url*; returns path (needs qrcode + pillow)."""
    dest = dest or (_RUN / "peachy_qr.png")
    _RUN.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.make_image().save(dest)
    return dest


def ensure_dashboard_qr(*, port: int = 8080, lan_ip: str | None = None) -> tuple[str, str, Path]:
    """Resolve URL, optionally pin funnel, write PNG. Returns (url, source, png_path)."""
    maybe_pin_funnel_url()
    url, source = resolve_url(port=port, lan_ip=lan_ip)
    png = save_qr_png(url)
    return url, source, png


def _emit_qr(url: str, *, source: str) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)

    label = {"pinned": "pinned (print this)", "funnel": "Tailscale Funnel",
             "lan": "LAN (session — use --pin to keep for print)"}.get(source, source)
    print(f"\n  {url}\n  [{label}]\n")
    qr.print_ascii(invert=True)

    png = _RUN / "peachy_qr.png"
    _RUN.mkdir(parents=True, exist_ok=True)
    try:
        qr.make_image().save(png)
        print(f"\nSaved {png}  — print and tape on Peachy / whiteboard.")
    except Exception:
        print("\n(No PNG — pillow missing; terminal QR scans fine.)")
    if source == "lan":
        print("Tip: when this IP is the one you want forever, run:")
        print("  python scripts/tool-qr.py --pin")


def pin_url(url: str) -> Path:
    _RUN.mkdir(parents=True, exist_ok=True)
    _QR_URL_FILE.write_text(url.strip())
    return _QR_URL_FILE


def maybe_pin_funnel_url() -> bool:
    """If funnel FQDN exists and nothing pinned yet, write stable https URL."""
    if _QR_URL_FILE.exists() or os.environ.get("PEACHY_QR_URL", "").strip():
        return False
    fqdn = _read(_FQDN_FILE)
    if not fqdn:
        return False
    pin_url(resolve_url()[0])
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="QR for the Peachy dashboard URL")
    ap.add_argument("--url", help="explicit URL (for --pin or one-off print)")
    ap.add_argument("--port", type=int, default=0, help="dashboard port (default from .run)")
    ap.add_argument("--lan-ip", help="LAN IP for fallback URL")
    ap.add_argument("--from-run", action="store_true",
                    help="read port/token from .run (called by dashboard/run.sh)")
    ap.add_argument("--write-json", action="store_true",
                    help="write peachy_qr.png and print {url,source} JSON (no ASCII)")
    ap.add_argument("--pin", action="store_true",
                    help="save resolved URL to .run/peachy_qr_url (constant QR)")
    args = ap.parse_args()

    port = args.port
    if port <= 0:
        try:
            port = int(_read(_PORT_FILE) or "8080")
        except ValueError:
            port = 8080
    if os.environ.get("PEACHY_PORT"):
        try:
            port = int(os.environ["PEACHY_PORT"])
        except ValueError:
            pass

    if args.from_run:
        maybe_pin_funnel_url()

    if args.write_json:
        url, source = resolve_url(port=port, lan_ip=args.lan_ip)
        try:
            ensure_dashboard_qr(port=port, lan_ip=args.lan_ip)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "msg": str(e)}))
            return 1
        print(json.dumps({"ok": True, "url": url, "source": source}))
        return 0

    if args.pin:
        url = args.url or resolve_url(port=port, lan_ip=args.lan_ip)[0]
        path = pin_url(url)
        print(f"Pinned → {path}\n  {url}\n(Re-run tool-qr.py to print; same QR after reboot.)")
        _emit_qr(url, source="pinned")
        return 0

    if args.url:
        _emit_qr(args.url, source="pinned" if _read(_QR_URL_FILE) == args.url else "explicit")
        return 0

    if args.from_run:
        maybe_pin_funnel_url()

    url, source = resolve_url(port=port, lan_ip=args.lan_ip)
    _emit_qr(url, source=source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
