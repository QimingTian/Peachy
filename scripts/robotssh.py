#!/usr/bin/env python3
"""Key-only SSH to the robot, with a lockout breaker (importable lib).

The robot's OpenSSH (10.x) penalises an IP after repeated failed logins and then
refuses *every* connection for minutes ("Not allowed at this time"). So:
  - key auth only (BatchMode, no password / keyboard-interactive prompts);
  - the first auth rejection stops all SSH from this laptop for 10 minutes
    (state in .run/ssh_block, shared by every process).

    from robotssh import ssh_run
    p = ssh_run("uptime")            # CompletedProcess, or None while blocked

One-time key install (needs the robot password, typed once by a human):
    ssh-copy-id <user>@<robot>

The login user comes from REACHY_SSH_USER or .run/ssh_user (kept out of git);
with neither set, ssh falls back to ~/.ssh/config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_BLOCK_FILE = _REPO / ".run" / "ssh_block"
BLOCK_S = 600.0
_AUTH_FAIL = ("Permission denied", "Not allowed at this time", "Too many authentication failures")


def _user() -> str:
    env = os.environ.get("REACHY_SSH_USER", "").strip()
    if env:
        return env
    try:
        return (_REPO / ".run" / "ssh_user").read_text().strip()
    except OSError:
        return ""


def _host() -> str:
    sys.path.insert(0, str(_REPO / "scripts"))
    from hostfind import resolve_host

    return resolve_host().split(":")[0]


def blocked() -> tuple[bool, str]:
    """(is_blocked, reason) — reason names the cause and the seconds left."""
    try:
        d = json.loads(_BLOCK_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False, ""
    left = float(d.get("until", 0)) - time.time()
    if left <= 0:
        return False, ""
    return True, f"SSH paused {int(left)}s after: {d.get('reason', '?')}"


def _block(reason: str) -> None:
    _BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BLOCK_FILE.write_text(json.dumps({"until": time.time() + BLOCK_S, "reason": reason[:200]}))


def clear_block() -> None:
    _BLOCK_FILE.unlink(missing_ok=True)


def ssh_argv(host: str | None = None) -> list[str]:
    # One shared connection (10 min) turns each call from ~0.7 s into ~50 ms.
    # The socket path must stay short (Unix-socket limit), hence /tmp.
    return ["ssh", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8", "-o", "ControlMaster=auto",
            "-o", "ControlPath=/tmp/peachy-ssh-%C", "-o", "ControlPersist=600",
            _target(host or _host())]


def _target(host: str) -> str:
    user = _user()
    return f"{user}@{host}" if user else host


def ssh_run(remote_cmd: str, *, input_text: str | None = None, timeout: float = 20.0,
            host: str | None = None) -> subprocess.CompletedProcess[str] | None:
    """Run *remote_cmd* on the robot. None while the breaker is open."""
    if blocked()[0]:
        return None
    try:
        p = subprocess.run([*ssh_argv(host), remote_cmd], input=input_text,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 124, "", "ssh timed out")
    if p.returncode == 255 and any(s in (p.stderr or "") for s in _AUTH_FAIL):
        _block(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "auth rejected")
    return p


def main() -> int:
    b, why = blocked()
    if b:
        print(why)
        return 1
    p = ssh_run(" ".join(sys.argv[1:]) or "echo ok")
    if p is None:
        print(blocked()[1])
        return 1
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


if __name__ == "__main__":
    raise SystemExit(main())
