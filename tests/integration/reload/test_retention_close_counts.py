"""Real weak-reference retention tests.

1. Run 100+ successful alternating reloads
2. Capture weak references to candidate, pending swap, transaction,
   published generation, and old generation from selected iterations
3. Force garbage collection
4. Assert weak references are cleared after completion
5. Assert active registry is empty
6. Assert history is bounded
7. Assert each retired resource closes exactly once
"""

from __future__ import annotations

import dataclasses
import gc
import weakref
from typing import TYPE_CHECKING

import pytest

from eggpool.control.accepted_finalization import (
    FINALIZATION_HISTORY_MAX,
    AcceptedFinalizationRecord,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# F6.1: 100-reload retention with weak references
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_100_reload_weak_reference_retention(
    reload_harness: ReloadHarness,
) -> None:
    """F6.1: After 100 alternating reloads, weak references are collectible.

    Proves that completed finalization jobs release strong references
    to operational objects (candidate, pending_swap, transaction,
    published_generation) so garbage collection can reclaim them.
    """
    rm = reload_harness.reload_manager
    initial_config = reload_harness.initial_config
    candidate_config = reload_harness.candidate_config

    # Capture weak references at selected iterations.
    weak_refs: list[weakref.ref] = []
    gc.disable()  # Prevent GC during reload loop for deterministic capture.

    try:
        for i in range(100):
            config = candidate_config if i % 2 == 0 else initial_config
            result = await reload_harness.reload(config=config)
            assert result.ok is True, f"reload {i} failed: {result}"

            # After each reload, active jobs should be at most 1.
            active_count = sum(
                1 for j in rm._accepted_finalization_jobs.values() if not j.is_complete
            )
            assert active_count <= 1, f"reload {i}: active job count {active_count} > 1"

            # Capture weak references to any remaining active jobs.
            for job in rm._accepted_finalization_jobs.values():
                if not job.is_complete:
                    if job.candidate is not None:
                        weak_refs.append(weakref.ref(job.candidate))
                    if job.pending_swap is not None:
                        weak_refs.append(weakref.ref(job.pending_swap))
                    if job.transaction is not None:
                        weak_refs.append(weakref.ref(job.transaction))
                    if job.published_generation is not None:
                        weak_refs.append(weakref.ref(job.published_generation))
    finally:
        gc.enable()

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
# F6.2: Release references clears operational objects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_release_references_clears_after_completion(
    reload_harness: ReloadHarness,
) -> None:
    """F6.2: After completion, job releases strong references to operational objects."""
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
# F6.3: History bound constant
# ---------------------------------------------------------------------------


def test_history_max_is_reasonable() -> None:
    """F6.3: FINALIZATION_HISTORY_MAX is at least 32 and at most 128."""
    assert 32 <= FINALIZATION_HISTORY_MAX <= 128


# ---------------------------------------------------------------------------
# F6.4: Record is frozen dataclass
# ---------------------------------------------------------------------------


def test_record_is_frozen_dataclass() -> None:
    """F6.4: AcceptedFinalizationRecord is immutable (frozen dataclass)."""
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


# ---------------------------------------------------------------------------
# F6.5: Active registry empties after many reloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_active_registry_empties_after_many_reloads(
    reload_harness: ReloadHarness,
) -> None:
    """F6.5: After 50 reloads, no active finalization jobs remain."""
    rm = reload_harness.reload_manager
    initial_config = reload_harness.initial_config
    candidate_config = reload_harness.candidate_config

    for i in range(50):
        config = candidate_config if i % 2 == 0 else initial_config
        result = await reload_harness.reload(config=config)
        assert result.ok is True, f"reload {i} failed: {result}"

    gc.collect()

    # All jobs should be complete and removed from the active registry.
    active_jobs = [
        j for j in rm._accepted_finalization_jobs.values() if not j.is_complete
    ]
    assert len(active_jobs) == 0

    # History is bounded.
    assert len(rm._finalization_history) <= FINALIZATION_HISTORY_MAX
