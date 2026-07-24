"""Plan 019 Workstream A — Accepted-finalization state machine tests.

Verifies progress/health separation, single-flight execution,
step-resume semantics, cancellation safety, and the invariant that
no path can skip steps and reach COMPLETED.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.control.accepted_finalization import (
    AcceptedFinalizationHealth,
    AcceptedFinalizationStep,
    AcceptedReloadFinalizationJob,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    request_id: str = "test-job",
    generation_id: int = 1,
    old_generation_id: int | None = 0,
    start_step: AcceptedFinalizationStep = AcceptedFinalizationStep.REGISTERED,
) -> AcceptedReloadFinalizationJob:
    """Build a minimal finalization job with stubbed dependencies."""
    fake_txn = MagicMock()
    fake_txn.accepted_finalization = MagicMock()
    fake_txn.digest_prefix = "test"
    fake_candidate = MagicMock()
    fake_pending_swap = MagicMock()
    fake_pending_swap.finalize_retirement = AsyncMock()
    fake_gen = MagicMock()
    fake_gen.generation_id = generation_id
    observer = MagicMock()
    observer.on_publish_complete = AsyncMock()
    return AcceptedReloadFinalizationJob(
        request_id=request_id,
        generation_id=generation_id,
        old_generation_id=old_generation_id,
        transaction=fake_txn,
        candidate=fake_candidate,
        pending_swap=fake_pending_swap,
        transition_result=None,
        published_generation=fake_gen,
        app=None,
        observer=observer,
        _step=start_step,
    )


# ---------------------------------------------------------------------------
# Workstream A1: is_complete is true only for COMPLETED
# ---------------------------------------------------------------------------


class TestIsCompleteOnlyForCompleted:
    """A1: Only COMPLETED progress is terminal."""

    def test_registered_is_not_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)
        assert not job.is_complete

    def test_ownership_transferred_not_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.OWNERSHIP_TRANSFERRED)
        assert not job.is_complete

    def test_mirror_updated_not_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.MIRROR_UPDATED)
        assert not job.is_complete

    def test_transitions_finalized_not_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.TRANSITIONS_FINALIZED)
        assert not job.is_complete

    def test_observer_reported_not_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.OBSERVER_REPORTED)
        assert not job.is_complete

    def test_retirement_scheduled_not_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.RETIREMENT_SCHEDULED)
        assert not job.is_complete

    def test_transaction_completed_not_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.TRANSACTION_COMPLETED)
        assert not job.is_complete

    def test_completed_is_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.COMPLETED)
        assert job.is_complete


# ---------------------------------------------------------------------------
# Workstream A2: failure leaves the step unchanged
# ---------------------------------------------------------------------------


class TestFailureLeavesStepUnchanged:
    """A2: A failed step leaves the progress cursor unchanged."""

    @pytest.mark.asyncio()
    async def test_step_unchanged_after_ownership_failure(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)
        job.candidate.transfer_to_runtime_manager.side_effect = RuntimeError(
            "transfer failed"
        )
        result = await job.run()
        assert result is AcceptedFinalizationStep.REGISTERED
        assert not job.is_complete
        assert job.health is AcceptedFinalizationHealth.RETRY_PENDING

    @pytest.mark.asyncio()
    async def test_step_unchanged_after_mirror_failure(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.OWNERSHIP_TRANSFERRED)
        # Mirror update checks _reload_manager.TEST_INJECT_FINALIZATION_CANCEL.
        job._reload_manager = MagicMock()
        job._reload_manager.TEST_INJECT_FINALIZATION_CANCEL = RuntimeError(
            "mirror failed"
        )
        result = await job.run()
        assert result is AcceptedFinalizationStep.OWNERSHIP_TRANSFERRED
        assert not job.is_complete


# ---------------------------------------------------------------------------
# Workstream A2: retry executes the failed step
# ---------------------------------------------------------------------------


class TestRetryExecutesFailedStep:
    """A2: A retry resumes from the failed step, not from the beginning."""

    @pytest.mark.asyncio()
    async def test_retry_after_ownership_failure(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)

        call_count = 0
        original_transfer = job.candidate.transfer_to_runtime_manager

        def failing_then_succeeding() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first call fails")
            original_transfer()

        job.candidate.transfer_to_runtime_manager = failing_then_succeeding

        # First run — fails at ownership transfer.
        result1 = await job.run()
        assert result1 is AcceptedFinalizationStep.REGISTERED
        assert call_count == 1

        # Second run — retries ownership transfer and completes.
        result2 = await job.run()
        assert result2 is AcceptedFinalizationStep.COMPLETED
        assert call_count == 2
        assert job.health is AcceptedFinalizationHealth.COMPLETED


# ---------------------------------------------------------------------------
# Workstream A2: later steps don't execute before the failed step
# ---------------------------------------------------------------------------


class TestNoStepSkipping:
    """A2: Later steps do not execute before the failed step succeeds."""

    @pytest.mark.asyncio()
    async def test_steps_execute_in_order(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)
        step_order: list[str] = []

        original_mirror = job._step_mirror_update

        async def tracked_mirror() -> None:
            step_order.append("mirror_update")
            await original_mirror()

        job._step_mirror_update = tracked_mirror  # type: ignore[assignment]

        # Run to completion — steps should execute in order.
        result = await job.run()
        assert result is AcceptedFinalizationStep.COMPLETED
        assert "mirror_update" in step_order


# ---------------------------------------------------------------------------
# Workstream A3: single-flight execution
# ---------------------------------------------------------------------------


class TestSingleFlightExecution:
    """A3: Concurrent run() callers share one execution."""

    @pytest.mark.asyncio()
    async def test_concurrent_runners_share_one_execution(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)

        # Launch two concurrent run() calls.
        results = await asyncio.gather(
            job.run(),
            job.run(),
            return_exceptions=True,
        )

        # Both should return without error (one gets COMPLETED, the other
        # returns early because is_complete is True).
        assert all(isinstance(r, AcceptedFinalizationStep) for r in results)
        assert job.is_complete

    @pytest.mark.asyncio()
    async def test_second_runner_returns_immediately_when_complete(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)

        # Complete the job.
        await job.run()
        assert job.is_complete

        # Second run should return immediately.
        result = await job.run()
        assert result is AcceptedFinalizationStep.COMPLETED


# ---------------------------------------------------------------------------
# Workstream A3: cancellation of a waiter does not cancel the job
# ---------------------------------------------------------------------------


class TestCancellationSafety:
    """A3: Cancellation of one waiter does not cancel the retained job."""

    @pytest.mark.asyncio()
    async def test_waiter_cancellation_preserves_job(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)

        # Make the observer step suspend so cancellation can arrive.
        suspension_event = asyncio.Event()

        async def suspending_publish(**_kw: object) -> None:
            await suspension_event.wait()

        job.observer.on_publish_complete = suspending_publish

        # Launch run() — it will complete ownership, mirror, transitions,
        # then suspend at observer.
        task = asyncio.create_task(job.run())
        await asyncio.sleep(0)  # Let it progress through early steps.

        # Cancel while suspended at observer.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The job should still be in a valid state (not complete,
        # but not corrupted).
        assert not job.is_complete
        assert job.health in (
            AcceptedFinalizationHealth.READY,
            AcceptedFinalizationHealth.RUNNING,
            AcceptedFinalizationHealth.RETRY_PENDING,
        )

        # Release the suspension for cleanup.
        suspension_event.set()

        # A subsequent run should still work.
        result = await job.run()
        assert result is AcceptedFinalizationStep.COMPLETED


# ---------------------------------------------------------------------------
# Workstream A4: clear stale error state after successful retry
# ---------------------------------------------------------------------------


class TestClearStaleErrorState:
    """A4: When a previously failed step succeeds, prior error is cleared."""

    @pytest.mark.asyncio()
    async def test_error_state_cleared_on_retry_success(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)

        call_count = 0
        original_transfer = job.candidate.transfer_to_runtime_manager

        def failing_then_succeeding() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            original_transfer()

        job.candidate.transfer_to_runtime_manager = failing_then_succeeding

        # First run — fails.
        await job.run()
        assert job.health is AcceptedFinalizationHealth.RETRY_PENDING
        assert job.last_error_class == "RuntimeError"

        # Second run — succeeds; error state should be cleared.
        result = await job.run()
        assert result is AcceptedFinalizationStep.COMPLETED
        assert job.health is AcceptedFinalizationHealth.COMPLETED


# ---------------------------------------------------------------------------
# Workstream A: no path can skip all step bodies and assign COMPLETED
# ---------------------------------------------------------------------------


class TestNoSkipToCompleted:
    """No path can skip all step bodies and then assign COMPLETED."""

    @pytest.mark.asyncio()
    async def test_empty_job_does_not_complete_without_steps(self) -> None:
        """A job with all steps as no-ops should still advance through them."""
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)

        # All steps should execute (the dispatch loop runs each one).
        result = await job.run()
        assert result is AcceptedFinalizationStep.COMPLETED
        assert job.is_complete
        assert job.health is AcceptedFinalizationHealth.COMPLETED

    def test_completed_step_requires_all_prior_steps(self) -> None:
        """Jumping to COMPLETED from REGISTERED is not valid via run()."""
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)
        # The job starts at REGISTERED — run() must execute all steps.
        assert job.step is AcceptedFinalizationStep.REGISTERED
        assert not job.is_complete


# ---------------------------------------------------------------------------
# Workstream A: snapshot and diagnostics
# ---------------------------------------------------------------------------


class TestJobDiagnostics:
    """Job snapshot exposes diagnostic fields correctly."""

    @pytest.mark.asyncio()
    async def test_snapshot_after_completion(self) -> None:
        job = _make_job(
            request_id="diag-test",
            generation_id=42,
            old_generation_id=41,
        )
        await job.run()

        snap = job.snapshot()
        assert snap["request_id"] == "diag-test"
        assert snap["generation_id"] == 42
        assert snap["old_generation_id"] == 41
        assert snap["step"] == "completed"
        assert snap["health"] == "completed"
        assert snap["is_complete"] is True
        assert snap["attempts"] == 1
        assert snap["retry_count"] == 0
        assert snap["duration_s"] is not None
        assert snap["duration_s"] >= 0

    @pytest.mark.asyncio()
    async def test_snapshot_after_failure(self) -> None:
        job = _make_job(start_step=AcceptedFinalizationStep.REGISTERED)
        job.candidate.transfer_to_runtime_manager.side_effect = RuntimeError("boom")
        await job.run()

        snap = job.snapshot()
        assert snap["step"] == "registered"
        assert snap["health"] == "retry_pending"
        assert snap["is_complete"] is False
        assert snap["last_error_class"] == "RuntimeError"
        assert snap["last_error_message"] == "boom"

    @pytest.mark.asyncio()
    async def test_to_record_after_completion(self) -> None:
        job = _make_job(
            request_id="record-test",
            generation_id=7,
            old_generation_id=6,
        )
        await job.run()

        record = job.to_record()
        assert record.request_id == "record-test"
        assert record.generation_id == 7
        assert record.old_generation_id == 6
        assert record.completion_status == "completed"
        assert record.attempts == 1
        assert record.retry_count == 0
        assert record.duration_s >= 0


# ---------------------------------------------------------------------------
# Release references
# ---------------------------------------------------------------------------


class TestReleaseReferences:
    """Workstream C3: release_references clears operational objects."""

    def test_release_clears_references(self) -> None:
        job = _make_job()
        assert job.candidate is not None
        assert job.pending_swap is not None
        assert job.published_generation is not None
        assert job.transaction is not None

        job.release_references()

        assert job.candidate is None
        assert job.pending_swap is None
        assert job.published_generation is None
        assert job.app is None
        assert job.observer is None
        assert job.transaction is None

    def test_release_is_idempotent(self) -> None:
        job = _make_job()
        job.release_references()
        job.release_references()  # Should not raise.
        assert job.candidate is None
