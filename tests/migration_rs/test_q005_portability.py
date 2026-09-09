"""Pure runner and target-contract tests for Q005 portability qualification."""

from __future__ import annotations

import json
import os
import stat
import sys
from typing import TYPE_CHECKING

from scripts.qualification_portability import (
    DEFAULT_FIXTURE,
    TARGETS,
    _run,
    main,
    run_qualification,
    target_for_platform,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_q005_target_mapping_matches_frozen_q001_matrix() -> None:
    assert target_for_platform("Linux", "x86_64") == "linux-x86_64"
    assert target_for_platform("Linux", "aarch64") == "linux-aarch64"
    assert target_for_platform("Darwin", "arm64") == "macos-arm64"
    assert target_for_platform("Darwin", "x86_64") == "other-unix"
    assert target_for_platform("Windows", "AMD64") == "windows"
    assert TARGETS["windows"]["classification"] == "unsupported"
    assert TARGETS["other-unix"]["classification"] == "not-qualified"


def test_q005_fake_candidate_crash_is_failure_not_timeout(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("#!/bin/sh\nexit 37\n", encoding="utf-8")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    output = tmp_path / "report.json"
    exit_code = main(
        [
            "--binary",
            str(candidate),
            "--config-fixture",
            str(DEFAULT_FIXTURE),
            "--target-id",
            "macos-arm64",
            "--output",
            str(output),
        ]
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert result["status"] == "fail"
    assert "37" in str(result["reason"])
    assert "timeout" not in str(result["reason"])


def test_q005_unhealthy_candidate_is_reported_without_waiting(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "import sys\nsys.exit(23)\n",
        encoding="utf-8",
    )
    result = _run(
        "fake-crash",
        (sys.executable, str(candidate)),
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=2,
    )
    assert result.returncode == 23
    assert result.timed_out is False
    assert result.status == "fail"


def test_q005_unsupported_target_is_explicit_and_bounded(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"not an executable")
    report = run_qualification(binary=candidate, target_id="windows")
    assert report["status"] == "not-applicable"
    assert report["target"]["classification"] == "unsupported"
    assert "explicit" in report["reason"]
    assert len(json.dumps(report).encode()) < 32 * 1024


def test_q005_default_fixture_has_no_secret_or_unresolved_marker() -> None:
    text = DEFAULT_FIXTURE.read_text(encoding="utf-8")
    assert "__Q005_" in text
    assert 'api_key = "q005-server-key"' in text
    assert "OPENAI_API_KEY" not in text
