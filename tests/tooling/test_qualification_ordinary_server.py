"""Contract tests for the M002 ordinary-server corpus and summary guard."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scripts.qualification_ordinary_server import (
    PROVIDER_LATENCY_PROFILE,
    REPETITIONS,
    REQUESTS_PER_CELL,
    SCHEMA_VERSION,
    SUMMARY_VERSION,
    _conditions,
    _io_pressure_safe,
    summarize,
)


def _run(principals: int, concurrency: int, p95: int) -> dict[str, Any]:
    return {
        "status": "pass",
        "schema_version": "runtime-q008.v2",
        "candidate": {"sha256": "a" * 64},
        "environment": {
            "target_class": "ordinary-server",
            "qualification_database_storage": {
                "filesystem": "ext4",
                "storage_medium_class": "non-rotational",
                "device_class": "nvme",
            },
        },
        "benchmark": {
            "principal_count": principals,
            "budget_mode": "user-global",
            "provider_latency_profile": PROVIDER_LATENCY_PROFILE,
            "resource_summary": {"peak_rss_bytes": 4096},
            "runs": {
                "concurrent_native_responses_finite": {
                    "client_concurrency": concurrency,
                    "request_count": REQUESTS_PER_CELL,
                    "completed_count": REQUESTS_PER_CELL,
                    "failed_count": 0,
                    "requests_per_second": 100.0,
                    "cpu": {"cpu_percent": 50.0},
                    "timings": {
                        "sample_count": REQUESTS_PER_CELL,
                        "p50_elapsed_ms": 10,
                        "p95_elapsed_ms": p95,
                        "p99_elapsed_ms": p95 + 1,
                        "maximum_elapsed_ms": p95 + 2,
                    },
                }
            },
        },
        "durable": {"pending_requests": 0, "active_reservations": 0},
    }


def test_m002_matrix_separates_named_account_and_concurrency_levels() -> None:
    conditions = _conditions()
    assert {item["principals"] for item in conditions if item["concurrency"] == 1} == {
        1,
        5,
        10,
        25,
        49,
    }
    assert {item["concurrency"] for item in conditions if item["principals"] == 5} == {
        1,
        2,
        4,
        8,
        16,
    }


def test_m002_refuses_to_start_under_high_or_unavailable_io_pressure() -> None:
    assert _io_pressure_safe({"io_pressure": {"full": {"avg10": 5.0}}})
    assert not _io_pressure_safe({"io_pressure": {"full": {"avg10": 25.0}}})
    assert not _io_pressure_safe({"io_pressure": {"some": {"avg10": 1.0}}})


def test_m002_raw_to_summary_requires_three_durable_p99_runs(tmp_path: Path) -> None:
    names = [f"run-{index}.json" for index in range(1, REPETITIONS + 1)]
    for index, name in enumerate(names, start=1):
        (tmp_path / name).write_text(
            json.dumps(_run(5, 4, 20 + index)), encoding="utf-8"
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "plan": "on-prem-concurrency-performance M002",
        "candidate": {"sha256": "a" * 64},
        "lifecycle_artifact": "lifecycle.json",
        "target": {"architecture": "x86_64"},
        "workload": {"requests_per_condition": REQUESTS_PER_CELL},
        "conditions": [{"principals": 5, "concurrency": 4, "run_artifacts": names}],
    }
    (tmp_path / "lifecycle.json").write_text(
        json.dumps(
            {
                "schema_version": "runtime-q009.v1",
                "status": "pass",
                "candidate": {"sha256": "a" * 64},
                "phases": [
                    {"phase": "faults", "status": "pass"},
                    {"phase": "restart", "status": "pass"},
                ],
                "provider": {"request_count": 5},
                "ownership_convergence": {"pending_requests": 0},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "matrix.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    summary = summarize(manifest_path)

    assert summary["schema_version"] == SUMMARY_VERSION
    cell = summary["conditions"][0]
    assert cell["repetitions"] == 3
    assert cell["median_p95_ms"] == 22
    assert cell["median_p99_ms"] == 23
    assert cell["settlement"].startswith("zero pending")
    assert summary["lifecycle"]["phases"] == ["faults", "restart"]


def test_m002_raw_to_summary_rejects_incomplete_matrix(tmp_path: Path) -> None:
    path = tmp_path / "matrix.json"
    path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "status": "incomplete"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="incomplete"):
        summarize(path)


def test_m002_raw_to_summary_requires_passing_same_candidate_lifecycle(
    tmp_path: Path,
) -> None:
    names = [f"run-{index}.json" for index in range(1, REPETITIONS + 1)]
    for name in names:
        (tmp_path / name).write_text(json.dumps(_run(5, 4, 20)), encoding="utf-8")
    (tmp_path / "lifecycle.json").write_text(
        json.dumps(
            {
                "schema_version": "runtime-q009.v1",
                "status": "pass",
                "candidate": {"sha256": "b" * 64},
                "phases": [
                    {"phase": "faults", "status": "pass"},
                    {"phase": "restart", "status": "pass"},
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "matrix.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "complete",
                "candidate": {"sha256": "a" * 64},
                "lifecycle_artifact": "lifecycle.json",
                "conditions": [
                    {
                        "principals": 5,
                        "concurrency": 4,
                        "run_artifacts": names,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="lifecycle qualification did not pass"):
        summarize(manifest_path)
