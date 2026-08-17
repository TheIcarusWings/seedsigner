"""
Wrapper that runs the synthetic-touch end-to-end scenarios in a clean
subprocess.

The scenarios live in tests/touch_e2e_runner.py (not collected by pytest) and
exercise real Screens + real TouchButtons + real TouchInput with injected
events. A subprocess is required because the rest of the suite (tests/base.py)
replaces gui.renderer / hardware.buttons with MagicMocks at import time, which
must not leak into (or out of) these real-module tests.
"""
import os
import subprocess
import sys


def test_touch_e2e_scenarios():
    runner = os.path.join(os.path.dirname(__file__), "touch_e2e_runner.py")
    result = subprocess.run(
        [sys.executable, runner],
        capture_output=True,
        text=True,
        timeout=120,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    assert result.returncode == 0, "touch e2e scenarios failed (see output above)"
