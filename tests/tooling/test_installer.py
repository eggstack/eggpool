"""Deterministic installer quick-installer qualification."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_quick_installer_harness_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/qualify_quick_installer.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "pass"
    assert len(report["cases"]) == 42
    # Regression guard: stale pipx must never block a fresh native install.
    cases = {item["case"] for item in report["cases"]}
    assert "fresh-linux-aarch64-stale-pipx-ignored" in cases
    assert "fresh-linux-aarch64-no-python" in cases
    assert "release-manifest-raw-contract" in cases
