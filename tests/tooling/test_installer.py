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
    assert len(report["cases"]) == 14
