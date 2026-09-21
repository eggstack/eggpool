"""Contract tests for the guarded SBC qualification runner."""

from __future__ import annotations

import argparse
import json
import platform
import stat
from pathlib import Path

import pytest

from scripts.qualification_sbc import (
    BENCHMARK_FIXTURE,
    BENCHMARK_MAX_SAMPLES,
    DEFAULT_FIXTURE,
    NATIVE_FINITE_CASE,
    NATIVE_STREAMING_CASE,
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_VERSION,
    TRANSLATED_STREAMING_CASE,
    LoopbackProvider,
    _benchmark_sample_count,
    _percentile,
    _root_block_device,
    _storage_device_class,
    _timing_summary,
    benchmark_cadence_facts,
    bounded,
    main,
    resource_sample,
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


def test_q008_default_mode_selects_v1_and_benchmark_mode_selects_v2() -> None:
    assert SCHEMA_V1 == "runtime-q008.v1"
    assert SCHEMA_V2 == "runtime-q008.v2"
    assert SCHEMA_VERSION == SCHEMA_V1
    ordinary = run_qualification(binary=Path("/not/a/candidate"), benchmark_samples=0)
    assert ordinary["schema_version"] == SCHEMA_V1
    assert "benchmark" not in ordinary
    extended = run_qualification(binary=Path("/not/a/candidate"), benchmark_samples=30)
    assert extended["schema_version"] == SCHEMA_V2
    assert "benchmark" not in extended


def test_q008_ordinary_fixture_retains_aggressive_lifecycle_cadence() -> None:
    text = DEFAULT_FIXTURE.read_text(encoding="utf-8")
    assert "refresh_interval_s = 1" in text
    assert "flush_interval_s = 2" in text
    assert "enabled = true" in text
    assert "interval_s = 2" in text
    assert "startup_delay_s = 1" in text


def test_q008_benchmark_fixture_uses_low_wear_steady_state_cadence() -> None:
    assert BENCHMARK_FIXTURE.is_file()
    text = BENCHMARK_FIXTURE.read_text(encoding="utf-8")
    assert "refresh_interval_s = 300" in text
    assert "refresh_interval_s = 1" not in text
    assert "flush_interval_s = 120" in text
    assert "enabled = false" in text
    assert "interval_s = 86400" in text
    assert "access_log = false" in text
    assert "threads = 1" in text
    assert 'synchronous = "NORMAL"' in text
    assert "worker_threads = 1" in text
    assert "aggregate_only = true" in text
    assert "max_buffered_events = 250" in text
    assert "event_loop_lag_enabled = false" in text
    assert "cleanup_interval_s = 86400" in text
    assert 'mode = "off"' in text
    # Same synthetic provider/models/wire surfaces/loopback placeholders remain.
    assert "__SBC_UPSTREAM__" in text
    assert "__SBC_PORT__" in text
    assert "__SBC_DATABASE__" in text
    assert "__SBC_BACKUP_DIR__" in text
    assert 'preferred_surface = "anthropic_messages"' in text
    assert 'id = "q008-messages"' in text


def test_q008_benchmark_fixture_is_loopback_safe_and_secret_free() -> None:
    text = BENCHMARK_FIXTURE.read_text(encoding="utf-8")
    assert 'api_key = "q008-server-key"' in text
    assert 'api_key = "q008-provider-key"' in text
    assert "https://" not in text


def test_q008_translated_case_expects_client_terminal_and_messages_proof() -> None:
    assert NATIVE_FINITE_CASE.client_surface == "responses"
    assert NATIVE_FINITE_CASE.model == "q008-responses"
    assert NATIVE_STREAMING_CASE.terminal_marker == b"response.completed"
    assert NATIVE_STREAMING_CASE.expected_upstream_path is None
    # Translated case stays on the Responses client surface but expects the
    # downstream Responses terminal marker, never the provider grammar.
    assert TRANSLATED_STREAMING_CASE.client_surface == "responses"
    assert TRANSLATED_STREAMING_CASE.model == "q008-messages"
    assert TRANSLATED_STREAMING_CASE.streaming is True
    assert TRANSLATED_STREAMING_CASE.terminal_marker == b"response.completed"
    assert b"message_stop" not in TRANSLATED_STREAMING_CASE.terminal_marker
    assert TRANSLATED_STREAMING_CASE.expected_upstream_path == "/messages"


def test_q008_loopback_provider_counts_fixed_upstream_buckets() -> None:
    provider = LoopbackProvider()
    try:
        provider.record("/chat/completions")
        provider.record("/responses")
        provider.record("/responses")
        provider.record("/messages")
        provider.record("/not/a/retained/url")
        counts = provider.path_counts()
        assert counts == {
            "/chat/completions": 1,
            "/responses": 2,
            "/messages": 1,
        }
        assert provider.upstream_count("/messages") == 1
        assert provider.requests == 5
        # No arbitrary URLs, bodies, headers, or prompts are retained.
        assert "/not/a/retained/url" not in str(counts)
    finally:
        provider.server.server_close()


def test_q008_benchmark_cadence_facts_record_background_scalars() -> None:
    text = BENCHMARK_FIXTURE.read_text(encoding="utf-8")
    facts = benchmark_cadence_facts(text)
    assert facts["models_refresh_interval_s"] == "300"
    assert facts["metrics_flush_interval_s"] == "120"
    assert facts["backup_enabled"] == "false"
    assert facts["backup_interval_s"] == "86400"
    assert facts["server_threads"] == "1"
    ordinary_text = DEFAULT_FIXTURE.read_text(encoding="utf-8")
    ordinary_facts = benchmark_cadence_facts(ordinary_text)
    assert ordinary_facts["models_refresh_interval_s"] == "1"
    assert ordinary_facts["metrics_flush_interval_s"] == "2"
    assert ordinary_facts["backup_enabled"] == "true"
    payload = json.dumps(facts, sort_keys=True)
    assert "q008-provider-key" not in payload
    assert "q008-server-key" not in payload


def test_q008_ordinary_samples_omit_peak_while_benchmark_captures_it(
    tmp_path: Path,
) -> None:
    import os

    class _Process:
        pid = os.getpid()

    database = tmp_path / "usage.sqlite3"
    ordinary_sample = resource_sample(
        "ordinary",
        _Process(),  # type: ignore[arg-type]
        database,
        "http://127.0.0.1:9/api/stats/runtime",
        duration=0.01,
        include_peak=False,
    )
    assert "peak_rss_bytes" not in ordinary_sample
    benchmark_sample = resource_sample(
        "benchmark",
        _Process(),  # type: ignore[arg-type]
        database,
        "http://127.0.0.1:9/api/stats/runtime",
        duration=0.01,
        include_peak=True,
    )
    assert "peak_rss_bytes" in benchmark_sample


def test_q008_reports_contain_no_credentials_or_bodies(tmp_path: Path) -> None:
    output = tmp_path / "q008.json"
    exit_code = main(["--binary", str(tmp_path / "missing"), "--output", str(output)])
    assert exit_code == 1
    payload = output.read_text(encoding="utf-8")
    assert "q008-provider-key" not in payload
    assert "q008-server-key" not in payload
    report = json.loads(payload)
    assert report["schema_version"] == SCHEMA_V1
    assert "benchmark" not in report
