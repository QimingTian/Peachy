"""Motion checks against a local daemon (hardware or ``reachy-mini-daemon --sim``).

Skipped when nothing responds on the configured host/port (stdlib only — no pytest).

Run::

    reachy-mini-daemon --sim    # other terminal

    python -m unittest tests.test_sim_motion -v
"""

from __future__ import annotations

import os
import unittest
import urllib.error
import urllib.request


def _daemon_listening(host: str, port: int, timeout: float = 0.75) -> bool:
    try:
        urllib.request.urlopen(f"http://{host}:{port}/docs", timeout=timeout)
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


@unittest.skipUnless(
    _daemon_listening(
        os.environ.get("REACHY_TEST_HOST", "127.0.0.1"),
        int(os.environ.get("REACHY_TEST_PORT", "8000")),
    ),
    "Local Reachy daemon not reachable (start reachy-mini-daemon --sim)",
)
class TestSimMotion(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.host = os.environ.get("REACHY_TEST_HOST", "127.0.0.1")
        cls.port = int(os.environ.get("REACHY_TEST_PORT", "8000"))

    def test_connect_and_goto_neutral(self) -> None:
        from reachy_mini import ReachyMini
        from reachy_mini.utils import create_head_pose

        with ReachyMini(
            connection_mode="localhost_only",
            host=self.host,
            port=self.port,
            media_backend="no_media",
        ) as mini:
            mini.enable_motors()
            mini.goto_target(
                head=create_head_pose(degrees=True),
                duration=0.8,
            )


if __name__ == "__main__":
    unittest.main()
