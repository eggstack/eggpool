"""Contract tests for the guarded Q008 ARM64 SBC qualification runner."""

from __future__ import annotations

import json
import platform
import stat
from pathlib import Path

from scripts.qualification_sbc import (
    DEFAULT_FIXTURE,
    SCHEMA_VERSION,
    bounded,
    main,
    run_qualification,
)


def test_q008_refuses_non_linux_or_non_aarch64_without_claiming_pass() -> None:
    report = run_qualification(binary=Path("/not/a/candidate"))
    if platform.system().lower() != "linux" or platform.machine().lower() not in {
        "aarch64",
        "arm64",
    }:
        assert report["status"] == "blocked"
        assert "physical" in report["reason"] or "aarch64" in report["reason"]
    else:
        assert report["status"] in {"blocked", "fail"}


def test_q008_fixture_is_loopback_safe_and_secret_free() -> None:
    text = DEFAULT_FIXTURE.read_text(encoding="utf-8")
    assert "__Q008_UPSTREAM__" in text
    assert 'api_key = "q008-server-key"' in text
    assert 'api_key = "q008-provider-key"' in text
    assert "https://" not in text


def test_q008_redacts_credentials_and_bounds_diagnostics() -> None:
    value = bounded(
        "Bearer q008-provider-key token=secret https://user:pass@example.test/x "
        + "x" * 3000
    )
    assert "q008-provider-key" not in value
    assert "user:pass" not in value
    assert len(value.encode()) <= 768


def test_q008_missing_candidate_is_blocked_before_mutation_on_linux_sbc(
    tmp_path: Path,
) -> None:
    output = tmp_path / "q008.json"
    exit_code = main(["--binary", str(tmp_path / "missing"), "--output", str(output)])
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == SCHEMA_VERSION
    if report["status"] == "blocked":
        assert exit_code == 1
    else:
        assert report["status"] == "fail"


def test_q008_candidate_sha_is_checked_when_hardware_gate_is_available(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    report = run_qualification(binary=candidate, expected_sha256="0" * 64)
    if report["status"] != "blocked":
        assert report["status"] == "fail"
        assert "SHA-256" in report["reason"]
