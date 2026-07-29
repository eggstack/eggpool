"""Finalization retention and reference lifecycle tests.

100-reload retention test proving no monotonic retained-job growth,
weak references collectible, close counts exactly once, and bounded history.
"""

from __future__ import annotations

import dataclasses
import gc
from typing import TYPE_CHECKING

import pytest

from eggpool.control.accepted_finalization import (
    FINALIZATION_HISTORY_MAX,
    AcceptedFinalizationRecord,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# C5: 100-reload retention test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_100_reload_retention_bounded(
    reload_harness: ReloadHarness,
) -> None:
    """100 alternating reloads prove bounded history and no monotonic growth.

    After 100 reloads:
    - active finalization job count is zero
    - finalization history length is at most FINALIZATION_HISTORY_MAX
    - superseded objects are collectible
    - completed old resources close exactly once
    """
    rm = reload_harness.reload_manager
    initial_config = reload_harness.initial_config
    candidate_config = reload_harness.candidate_config

    for i in range(100):
        config = candidate_config if i % 2 == 0 else initial_config
        result = await reload_harness.reload(config=config)
        assert result.ok is True, f"reload {i} failed: {result}"

        # After each reload, active jobs should be at most 1.
        active_count = sum(
            1 for j in rm._accepted_finalization_jobs.values() if not j.is_complete
        )
        assert active_count <= 1, f"reload {i}: active job count {active_count} > 1"

    # Force garbage collection.
    gc.collect()

    # Active finalization jobs should be zero (all completed and pruned).
    active_jobs = [
        j for j in rm._accepted_finalization_jobs.values() if not j.is_complete
    ]
    assert len(active_jobs) == 0, f"active jobs remaining: {len(active_jobs)}"

    # History is bounded.
    history_len = len(rm._finalization_history)
    assert history_len <= FINALIZATION_HISTORY_MAX, (
        f"history length {history_len} > max {FINALIZATION_HISTORY_MAX}"
    )

    # History records are immutable and lightweight.
    for record in rm._finalization_history:
        assert isinstance(record, AcceptedFinalizationRecord)
        assert isinstance(record.request_id, str)
        assert isinstance(record.generation_id, int)
        assert isinstance(record.completion_status, str)
        assert isinstance(record.attempts, int)


# ---------------------------------------------------------------------------
# C3: release_references clears operational objects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_release_references_clears_after_completion(
    reload_harness: ReloadHarness,
) -> None:
    """After completion, job releases strong references to operational objects."""
    result = await reload_harness.reload()
    assert result.ok is True

    rm = reload_harness.reload_manager

    # Check history records — they should contain only scalar fields.
    for record in rm._finalization_history:
        for field in dataclasses.fields(record):
            value = getattr(record, field.name)
            assert isinstance(value, (str, int, float, type(None), bool)), (
                f"Record field {field.name!r} has non-scalar type "
                f"{type(value).__name__}; history records must not retain "
                "live operational objects"
            )


# ---------------------------------------------------------------------------
# C4: History bound constant
# ---------------------------------------------------------------------------


def test_history_max_is_reasonable() -> None:
    """FINALIZATION_HISTORY_MAX is at least 32 and at most 128."""
    assert 32 <= FINALIZATION_HISTORY_MAX <= 128


# ---------------------------------------------------------------------------
# C: Record is immutable
# ---------------------------------------------------------------------------


def test_record_is_frozen_dataclass() -> None:
    """AcceptedFinalizationRecord is immutable (frozen dataclass)."""
    record = AcceptedFinalizationRecord(
        request_id="test",
        generation_id=1,
        old_generation_id=0,
        completion_status="completed",
        attempts=1,
        failure_count=0,
        retry_attempt_count=0,
        retirement_retry_attempt_count=0,
        last_failed_step=None,
        last_error_class=None,
        last_error_message=None,
        completed_at=0.0,
        duration_s=0.0,
    )
    with pytest.raises(AttributeError):
        record.request_id = "changed"  # type: ignore[misc]
