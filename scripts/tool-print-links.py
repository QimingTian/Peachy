#!/usr/bin/env python3
"""Print dashboard URLs — one link per line, easy to copy from the terminal."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_RUN = _REPO / ".run"


def _lan_ip() -> str:
    for iface in ("en0", "en1"):
        try:
            out = subprocess.check_output(
                ["ipconfig", "getifaddr", iface],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if out:
                return out
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            pass
    return "<laptop-ip>"


def _load_token(run: Path) -> str:
    tok = os.environ.get("PEACHY_TOKEN", "").strip()
    if tok:
        return tok
    f = run / "peachy_token"
    return f.read_text().strip() if f.is_file() else ""


def _port(run: Path) -> int:
    env = os.environ.get("PEACHY_PORT", "").strip()
    if env:
        return int(env)
    pf = run / "peachy_dashboard.port"
    if pf.is_file():
        return int(pf.read_text().strip())
    return 8080


def print_links(
    *,
    run: Path = _RUN,
    port: int | None = None,
    prefix: str | None = None,
    show_meta: bool = True,
) -> None:
    port = port or _port(run)
    tok = _load_token(run)
    ip = _lan_ip()
    q = f"?k={tok}" if tok else ""
    bot = os.environ.get("REACHY_HOST", "reachy-mini.local")
    bot_port = os.environ.get("REACHY_PORT", "8000")

    sep = "──────────────────────────────────────────────"
    print(sep)
    if prefix:
        print(f"  {prefix}")
        print()

    def _urls(title: str, path: str) -> None:
        print(f"  {title}")
        for host in ("localhost", ip):
            print(f"  http://{host}:{port}{path}{q}")
        print()

    _urls("Dashboard", "/")

    qr = run / "peachy_qr_url"
    if qr.is_file():
        print("  Remote (pinned QR)")
        print(f"  {qr.read_text().strip()}")
        print()

    if show_meta:
        if tok:
            print(f"  Token   {tok}")
            print("  Tip     open once with ?k= (90-day cookie), then bookmark without it")
        print(f"  Daemon  {bot}:{bot_port}   port {port}")
        print("  QR      ./run.sh --qr")
        print("  Stop    Ctrl+C  |  ./run.sh --restart")
    print(sep)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, default=_RUN)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--no-meta", action="store_true")
    args = ap.parse_args()
    print_links(
        run=args.run,
        port=args.port or None,
        prefix=args.prefix or None,
        show_meta=not args.no_meta,
    )


if __name__ == "__main__":
    main()
