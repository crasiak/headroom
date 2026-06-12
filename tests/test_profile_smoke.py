"""Opt-in smoke test for `headroom wrap --profile`.

Skipped by default; set HEADROOM_PROFILE_SMOKE=1 to run. Invokes the real CLI
in a subprocess to confirm the --profile option is wired into both agents.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

RUN = os.environ.get("HEADROOM_PROFILE_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN, reason="set HEADROOM_PROFILE_SMOKE=1 to run")


def _help(cmd: str) -> str:
    out = subprocess.run(
        [sys.executable, "-m", "headroom.cli", "wrap", cmd, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return out.stdout


def test_profile_option_present_on_both_agents():
    assert "--profile" in _help("claude")
    assert "--profile" in _help("codex")
