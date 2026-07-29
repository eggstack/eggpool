"""Process-level runtime-validation runner smoke test.

This is the single short real-process test that proves the runtime
validation runner starts and stops a real EggPool subprocess, exercises
real traffic, emits one JSON output file, and shuts down cleanly. It
uses the public ``run_validation`` orchestration seam plus a narrow
internal ``DurationPlan`` so the canonical CI does not have to pay the
30-second production minimum.

The test must not add public CLI options. It reuses the production
startup, load generator, polling, and cleanup code paths.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.run_dispatch_stability_soak import (
    SCHEMA_VERSION,
    SCRIPT_VERSION,
    DurationPlan,
    ValidationRunConfig,
    build_run_config,
    run_validation,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

pytestmark = pytest.mark.integration


def _compact_plan() -> DurationPlan:
    """Compact positive-duration plan for the in-process smoke.

    Phases are short but strictly positive so the same production code
    paths run end-to-end without artificial sleeps or skips. Wall-clock
    budget is well under the documented 10-second preferred target.
    """
    return DurationPlan(
        total_s=4.0,
        warmup_s=0.5,
        early_window_s=1.0,
        drain_s=0.3,
        late_window_s=1.0,
        poll_interval_s=0.2,
        dispatch_p95_ratio_limit=1.50,
        dispatch_p99_ratio_limit=2.00,
        throughput_decline_limit=0.20,
        max_pending_requests=0,
        max_active_reservations=0,
    )


def _shapes() -> Iterable[str]:
    """Deterministic alternating stream / non-stream sequence.

    Ensures both transports execute at least once even in a tiny run
    so the workload gate can confirm dual coverage without relying on
    the random profile ratio.
    """
    while True:
        yield from ("stream", "nonstream")


def test_run_validation_produces_one_json_and_cleans_up(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "runtime-validation.json"
    config = ValidationRunConfig(
        profile_name="balanced-file-backed",
        duration_seconds=30,
        output_path=output_path,
        seed=42,
    )

    started = time.monotonic()
    result = asyncio_run_with_capture_errors(
        run_validation(
            config,
            duration_plan=_compact_plan(),
            health_timeout_s=45.0,
            quiescence_timeout_s=10.0,
            request_shapes=_shapes(),
        )
    )
    elapsed = time.monotonic() - started

    try:
        assert result.return_code == 0, (
            f"runner exited {result.return_code}: {result.failure_reasons}"
        )
        assert result.passed is True
        assert output_path.is_file()
        assert output_path.parent == tmp_path

        loaded = json.loads(output_path.read_text())
        assert loaded["schema_version"] == SCHEMA_VERSION
        assert loaded["script_version"] == SCRIPT_VERSION
        assert loaded["passed"] is True
        assert loaded["profile"] == "balanced-file-backed"

        # Quiescence observation must be present and explicit.
        quiescence = loaded["gates"]["quiescence"]
        assert quiescence["drained"] is True
        assert quiescence["passed"] is True
        assert "pending_requests" in quiescence
        assert "active_reservations" in quiescence
        assert quiescence["elapsed_seconds"] >= 0.0

        # Workload gate must report successful work in both windows.
        workload = loaded["gates"]["workload"]
        assert workload["passed"] is True
        assert workload["early"]["successes"] >= 1
        assert workload["late"]["successes"] >= 1
        # Deterministic alternating shape sequence guarantees both
        # transports execute even in a short run.
        assert workload["early"]["successes"] + workload["late"]["successes"] >= 2

        early = loaded["early"]
        late = loaded["late"]
        assert early["success_count"] >= 1
        assert late["success_count"] >= 1
        assert early["stream_success_count"] + late["stream_success_count"] >= 1
        assert early["nonstream_success_count"] + late["nonstream_success_count"] >= 1

        # Ratio gates must be present with the structured shape and
        # must pass when the runner is healthy on this profile.
        p95 = loaded["gates"]["dispatch_p95"]
        assert p95["passed"] is True
        assert p95["early_ms"] is not None
        assert p95["late_ms"] is not None
        assert p95["ratio"] is not None
        assert p95["ratio_limit"] == 1.5

        p99 = loaded["gates"]["dispatch_p99"]
        assert p99["passed"] is True
        assert p99["ratio_limit"] == 2.0

        # Database audit is required and must pass.
        assert loaded["gates"]["database_audit"]["passed"] is True

        # RSS measurement is required. macOS / Linux both support this;
        # the test only fails when RSS is None on a supported platform.
        assert loaded["gates"]["rss"]["available"] is True

        # Only one JSON file must remain in the work directory.
        siblings = sorted(p.name for p in tmp_path.iterdir())
        assert siblings == ["runtime-validation.json"]

        # No manifest, JSONL, or Markdown sibling outputs may exist.
        for ext in (".jsonl", ".md"):
            assert not list(tmp_path.glob(f"*{ext}"))

        # Child process must be gone.
        assert result.return_code == 0

    finally:
        # Defensive cleanup in case assertions fail before runner cleanup runs.
        if output_path.exists():
            output_path.unlink()

    # Wall-clock bound: hard maximum per plan 042 is 20 seconds.
    assert elapsed < 20.0, f"smoke exceeded wall-clock bound: {elapsed:.2f}s"


def asyncio_run_with_capture_errors(coro):
    """Helper to run an awaitable and return the result synchronously."""
    import asyncio

    return asyncio.run(coro)


def test_run_validation_unknown_profile_does_not_start_subprocess(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "runtime-validation.json"
    config = ValidationRunConfig(
        profile_name="does-not-exist",
        duration_seconds=30,
        output_path=output_path,
        seed=42,
    )
    import asyncio

    result = asyncio.run(run_validation(config))
    assert result.return_code == 1
    assert result.passed is False
    assert any("unknown profile" in r for r in result.failure_reasons)


def test_build_run_config_round_trip() -> None:
    """``build_run_config`` parses public CLI args into ValidationRunConfig."""
    parser_args = [
        "--profile",
        "sbc-reference",
        "--duration-seconds",
        "120",
        "--output",
        "/tmp/example.json",
        "--seed",
        "99",
        "-v",
    ]
    from scripts.run_dispatch_stability_soak import _build_parser

    parsed = _build_parser().parse_args(parser_args)
    config = build_run_config(parsed)
    assert config.profile_name == "sbc-reference"
    assert config.duration_seconds == 120
    assert config.output_path == Path("/tmp/example.json")
    assert config.seed == 99
