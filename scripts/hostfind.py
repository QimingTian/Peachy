"""Auto-resolve where Peachy's daemon is — no `source .run/reachy.env` needed.

Every control script calls resolve_host() for its default host, so they "just
work" wired / wireless / Tailscale / home, in any terminal. Order:

  1. REACHY_HOST env (explicit override — never probed)
  2. last-known-good cache (.run/reachy_host) if it still answers
  3. quick-probe the usual candidates; first daemon that answers wins + cached

Any HTTP reply (even 503 "backend down") counts as "found" — that's still the
right host; the script's own error/--fix flow then handles a down backend.
Set REACHY_QUIET=1 to silence the one-line stderr note.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import urllib.request
from pathlib import Path

_CACHE = Path(__file__).resolve().parent.parent / ".run" / "reachy_host"
_FQDN = Path(__file__).resolve().parent.parent / ".run" / "peachy_fqdn"
_DEFAULT = "127.0.0.1"
_CANDIDATES = ["127.0.0.1", "reachy-mini.local"]
_resolved: str | None = None  # memoize within a process


def _port() -> int:
    try:
        return int(os.environ.get("REACHY_PORT", "8000"))
    except ValueError:
        return 8000


def _answers(host: str, port: int, timeout: float = 1.5) -> bool:
    """True if a daemon HTTP-responds at host (any status, incl. 503)."""
    try:
        urllib.request.urlopen(
            f"http://{host}:{port}/api/state/full", timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True            # 503 etc. — daemon is there, just unhappy
    except Exception:
        return False           # refused / timeout / DNS — not here


def _pin_ip(host: str, port: int) -> str:
    """mDNS names (reachy-mini.local) resolve slowly and flakily on a busy LAN;
    cache the address they currently point at instead."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return host
    return ip if _answers(ip, port) else host


def _note(msg: str) -> None:
    if not os.environ.get("REACHY_QUIET"):
        print(f"[hostfind] {msg}", file=sys.stderr)


def resolve_host() -> str:
    global _resolved
    env = os.environ.get("REACHY_HOST", "").strip()
    if env:
        return env
    if _resolved:
        return _resolved
    port = _port()

    cached = ""
    try:
        cached = _CACHE.read_text().strip()
    except (FileNotFoundError, OSError):
        pass
    fqdn = ""
    try:
        fqdn = _FQDN.read_text().strip()
    except (FileNotFoundError, OSError):
        pass

    order: list[str] = []
    for h in ([cached] if cached else []) + ([fqdn] if fqdn else []) + _CANDIDATES:
        if h and h not in order:
            order.append(h)

    for h in order:
        if _answers(h, port):
            h = _pin_ip(h, port)
            _resolved = h
            try:
                _CACHE.parent.mkdir(parents=True, exist_ok=True)
                _CACHE.write_text(h)
            except OSError:
                pass
            _note(f"robot at {h}:{port}")
            return h

    fallback = cached or _DEFAULT
    _note(f"no robot found — using {fallback} (try ./scripts/net-connect.sh)")
    return fallback
