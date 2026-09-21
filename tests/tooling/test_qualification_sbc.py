"""Contract tests for the guarded SBC qualification runner."""

from __future__ import annotations

import argparse
import json
import platform
import stat
from pathlib import Path

import pytest

from scripts.qualification_sbc import (
    BENCHMARK_MAX_SAMPLES,
    DEFAULT_FIXTURE,
    SCHEMA_VERSION,
    _benchmark_sample_count,
    _percentile,
    _root_block_device,
    _storage_device_class,
    _timing_summary,
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
    assert "__SBC_UPSTREAM__" in text
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


def test_q008_storage_metadata_uses_root_device_without_identity() -> None:
    assert _root_block_device("/dev/mmcblk0p2") == "mmcblk0"
    assert _root_block_device("/dev/nvme0n1p3") == "nvme0n1"
    assert _storage_device_class("mmcblk0") == "mmc"
    assert _storage_device_class("nvme0n1") == "nvme"


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


def test_q008_benchmark_sample_contract_is_bounded_and_has_no_p99() -> None:
    assert _benchmark_sample_count("1") == 1
    assert _benchmark_sample_count(str(BENCHMARK_MAX_SAMPLES)) == BENCHMARK_MAX_SAMPLES
    with pytest.raises(argparse.ArgumentTypeError):
        _benchmark_sample_count("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _benchmark_sample_count(str(BENCHMARK_MAX_SAMPLES + 1))

    assert _percentile([1, 2, 3, 4], 50) == 2
    assert _percentile([1, 2, 3, 4], 95) == 4
    summary = _timing_summary([1, 2, 3, 4], [1, 2, 3, 4])
    assert summary["sample_count"] == 4
    assert "p99_elapsed_ms" not in summary
    assert summary["p50_ttft_ms"] == 2
