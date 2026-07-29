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

import asyncio
import json
import time
from typing import TYPE_CHECKING

import pytest

from scripts.run_dispatch_stability_soak import (
    SCHEMA_VERSION,
    SCRIPT_VERSION,
    DurationPlan,
    ValidationRunConfig,
    _pid_is_alive,
    run_validation,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

pytestmark = pytest.mark.integration


def _compact_plan() -> DurationPlan:
    """Compact positive-duration plan for the in-process smoke.

    Phases are short but strictly positive so the same production code
    paths run end-to-end without artificial sleeps or skips. Wall-clock
    budget stays well under the documented 20-second hard maximum.

    The ratio limits are widened from production defaults. Production
    caps are calibrated for runs of 60 seconds or longer after steady
    state; a 12-second cold-start smoke on CI runners produces genuine
    early/late latency variance that is well within healthy behavior
    but exceeds the production cap. The unit tests in
    ``test_runtime_validation_runner.py`` already pin the production
    limits; this smoke exists to prove real-process lifecycle only.
    """
    return DurationPlan(
        total_s=12.0,
        warmup_s=1.0,
        early_window_s=4.0,
        drain_s=1.0,
        late_window_s=4.0,
        poll_interval_s=0.5,
        dispatch_p95_ratio_limit=10.0,
        dispatch_p99_ratio_limit=10.0,
        throughput_decline_limit=1.0,
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
    """Start, exercise, drain, and clean up a real EggPool subprocess."""
    output_path = tmp_path / "runtime-validation.json"
    config = ValidationRunConfig(
        profile_name="balanced-file-backed",
        duration_seconds=30,
        output_path=output_path,
        seed=42,
    )

    started = time.monotonic()
    try:
        result = asyncio.run(
            run_validation(
                config,
                duration_plan=_compact_plan(),
                health_timeout_s=45.0,
                quiescence_timeout_s=10.0,
                request_shapes=_shapes(),
            )
        )
    except Exception:
        # Surface the bounded log tail even when the runner itself raises.
        pytest.fail(
            "run_validation raised unexpectedly; see prior test log for traceback"
        )
    elapsed = time.monotonic() - started

    # Failure messages must include the bounded redacted log tail when an
    # assertion below fails so the operator or CI logs include the
    # EggPool-tail context.
    diag_tail = result.process_log_tail
    try:
        assert result.passed is True, (
            f"runner reported failure: {result.failure_reasons}\n"
            f"--- process log tail ---\n{diag_tail or '(empty)'}"
        )
        assert result.return_code == 0, (
            f"runner exited {result.return_code}: {result.failure_reasons}\n"
            f"--- process log tail ---\n{diag_tail or '(empty)'}"
        )
        assert result.child_pid is not None, "child_pid missing from result"
        assert result.process_stopped is True, (
            f"process did not stop cleanly: {result.process_log_tail}"
        )
        assert result.work_dir is not None, "work_dir missing from result"
        assert not result.work_dir.exists(), (
            f"work directory not removed: {result.work_dir}"
        )
        assert result.work_dir_removed is True
        assert result.cleanup_error is None, (
            f"cleanup failed: {result.cleanup_error}\n"
            f"--- process log tail ---\n{result.process_log_tail}"
        )

        # The actual PID recorded by the runner must be gone.
        assert not _pid_is_alive(result.child_pid), (
            f"child PID {result.child_pid} is still alive after run"
        )

        # Output JSON must be retained and valid.
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
        assert workload["early"]["successes"] + workload["late"]["successes"] >= 2

        early = loaded["early"]
        late = loaded["late"]
        assert early["success_count"] >= 1
        assert late["success_count"] >= 1
        assert early["stream_success_count"] + late["stream_success_count"] >= 1
        assert early["nonstream_success_count"] + late["nonstream_success_count"] >= 1

        # Ratio gates must be present with the structured shape and
        # pass under the test-seam ratio limits defined by
        # ``_compact_plan()``. Production limits (1.5/2.0) are pinned
        # by the unit tests in ``test_runtime_validation_runner.py``.
        p95 = loaded["gates"]["dispatch_p95"]
        assert p95["passed"] is True
        assert p95["early_ms"] is not None
        assert p95["late_ms"] is not None
        assert p95["ratio"] is not None
        assert p95["ratio_limit"] == 10.0

        p99 = loaded["gates"]["dispatch_p99"]
        assert p99["passed"] is True
        assert p99["ratio_limit"] == 10.0

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

    finally:
        # Defensive cleanup in case assertions fail before runner cleanup runs.
        if output_path.exists():
            output_path.unlink()

    # Wall-clock bound: hard maximum per plan 042 is 20 seconds.
    assert elapsed < 20.0, f"smoke exceeded wall-clock bound: {elapsed:.2f}s"
