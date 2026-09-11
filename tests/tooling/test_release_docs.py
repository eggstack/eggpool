"""Deterministic release docs public metadata and documentation contract tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.validate_release_docs import validate_release_docs

ROOT = Path(__file__).parents[2]


def test_public_metadata_and_docs_are_consistent() -> None:
    result = validate_release_docs()

    assert result == {
        "status": "pass",
        "version": "0.8.0",
        "targets": ["linux-aarch64", "linux-x86_64", "macos-arm64"],
        "docs_checked": 7,
        "production_release": "published 0.8.0",
        "python_reference": "historical external artifacts",
    }


def test_guard_is_machine_readable_and_rejects_root_release_builds() -> None:
    completed = subprocess.run(
        ["uv", "run", "python", "scripts/validate_release_docs.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "pass"
    assert report["production_release"] == "published 0.8.0"
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "uv build" not in workflow
    assert "uv publish" not in workflow
