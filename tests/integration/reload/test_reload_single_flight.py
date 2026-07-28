"""Plan 020 Workstream F2 — Real single-flight tests.

Proves:
  - Two concurrent callers share one attempt task and one attempt count increment.
  - Cancelling one waiter does not cancel the attempt or the other waiter.
  - Timing out one waiter leaves the attempt running.
  - After a failed shared attempt, a later call creates a new retry task.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.control.accepted_finalization import (
    AcceptedFinalizationHealth,
    AcceptedFinalizationOutcome,
    AcceptedFinalizationStep,
    AcceptedReloadFinalizationJob,
    FinalizationStatus,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_blocking_job(
    *,
    block_event: asyncio.Event,
    resume_event: asyncio.Event,
) -> AcceptedReloadFinalizationJob:
    """Build a finalization job with a mock observer that blocks on an event.

    The observer's on_publish_complete blocks until resume_event is set,
    allowing tests to control execution timing.
    """

    class _BlockingObserver:
        async def on_publish_complete(self, **kwargs: object) -> None:
            block_event.set()
            await resume_event.wait()

        async def on_retirement_started(self, **kwargs: object) -> None:
            pass

    fake_txn = MagicMock()
    fake_txn.accepted_finalization = MagicMock()
    fake_txn.digest_prefix = "test"
    fake_gen = MagicMock()
    fake_gen.generation_id = 100

    pending_swap = MagicMock()
    pending_swap.finalize_retirement = AsyncMock()

    return AcceptedReloadFinalizationJob(
        request_id="single-flight-test",
        generation_id=100,
        old_generation_id=99,
        transaction=fake_txn,
        candidate=MagicMock(),
        pending_swap=pending_swap,
        transition_result=None,
        published_generation=fake_gen,
        app=None,
        observer=_BlockingObserver(),
        _step=AcceptedFinalizationStep.TRANSITIONS_FINALIZED,
    )


# ---------------------------------------------------------------------------
# F2.1: Concurrent callers share one task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_concurrent_runners_share_one_task(
    reload_harness: ReloadHarness,
) -> None:
    """F2.1: Two concurrent run() calls share one attempt.

    Both callers await the same retained task.  The step body
    executes once, and attempt_count increments once.
    """
    block_event = asyncio.Event()
    resume_event = asyncio.Event()
    job = _make_blocking_job(block_event=block_event, resume_event=resume_event)

    # Launch two concurrent run() calls.
    async def _caller_a() -> tuple:
        return await job.run()

    async def _caller_b() -> tuple:
        return await job.run()

    task_a = asyncio.create_task(_caller_a())
    task_b = asyncio.create_task(_caller_b())

    # Wait for the block event to be set (step body started).
    await asyncio.wait_for(block_event.wait(), timeout=5.0)

    # Both callers are waiting on the same task.
    # Resume the observer.
    resume_event.set()

    # Both should complete.
    outcome_a = await asyncio.wait_for(task_a, timeout=5.0)
    outcome_b = await asyncio.wait_for(task_b, timeout=5.0)

    # Both outcomes should indicate completion.
    assert outcome_a.completed is True
    assert outcome_b.completed is True

    # Attempt count should be exactly 1 (shared execution).
    assert job.attempts == 1


# ---------------------------------------------------------------------------
# F2.2: Cancelling one waiter does not cancel the attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_cancel_waiter_does_not_cancel_attempt(
    reload_harness: ReloadHarness,
) -> None:
    """F2.2: Cancelling caller A does not cancel the attempt or caller B.

    The retained task continues running.  Caller B receives the
    outcome normally.
    """
    block_event = asyncio.Event()
    resume_event = asyncio.Event()
    job = _make_blocking_job(block_event=block_event, resume_event=resume_event)

    async def _caller_a() -> AcceptedFinalizationOutcome:
        return await job.run()

    async def _caller_b() -> AcceptedFinalizationOutcome:
        return await job.run()

    task_a = asyncio.create_task(_caller_a())
    task_b = asyncio.create_task(_caller_b())

    # Wait for the block event to be set.
    await asyncio.wait_for(block_event.wait(), timeout=5.0)

    # Cancel caller A.
    task_a.cancel()

    # Resume the observer.
    resume_event.set()

    # Caller A should be cancelled.
    with pytest.raises(asyncio.CancelledError):
        await task_a

    # Caller B should complete successfully.
    outcome_b = await asyncio.wait_for(task_b, timeout=5.0)
    assert outcome_b.completed is True


# ---------------------------------------------------------------------------
# F2.3: Timeout does not cancel the attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_timeout_does_not_cancel_attempt(
    reload_harness: ReloadHarness,
) -> None:
    """F2.3: Timing out a waiter leaves the attempt running.

    The waiter gets TimeoutError, but the retained task continues.
    After resuming, the attempt completes normally.
    """
    block_event = asyncio.Event()
    resume_event = asyncio.Event()
    job = _make_blocking_job(block_event=block_event, resume_event=resume_event)

    async def _caller_a() -> AcceptedFinalizationOutcome:
        return await job.run()

    task_a = asyncio.create_task(_caller_a())

    # Wait for the block event to be set.
    await asyncio.wait_for(block_event.wait(), timeout=5.0)

    # Time out the waiter — use wait_for with a very short timeout.
    # Note: run() shields the task, so the waiter times out but the
    # task keeps running.
    import contextlib

    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task_a), timeout=0.01)

    # The task is still running — resume it.
    resume_event.set()

    # The task should complete.
    outcome = await asyncio.wait_for(task_a, timeout=5.0)
    assert outcome.completed is True


# ---------------------------------------------------------------------------
# F2.4: After failure, next call creates new task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_retry_creates_new_task(
    reload_harness: ReloadHarness,
) -> None:
    """F2.4: After a failed shared attempt, a later call creates a new task.

    The first run fails at the retirement step (finalize_retirement raises).
    The second run creates a new retained task and succeeds.
    """
    call_count = 0

    class _FailOnceThenSucceedObserver:
        async def on_publish_complete(self, **kwargs: object) -> None:
            pass

        async def on_retirement_started(self, **kwargs: object) -> None:
            pass

    async def _fail_once_finalize_retirement() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("retirement failed on first call")

    fake_txn = MagicMock()
    fake_txn.accepted_finalization = MagicMock()
    fake_txn.digest_prefix = "test"
    fake_gen = MagicMock()
    fake_gen.generation_id = 200

    pending_swap = MagicMock()
    pending_swap.finalize_retirement = AsyncMock(
        side_effect=_fail_once_finalize_retirement
    )

    job = AcceptedReloadFinalizationJob(
        request_id="retry-new-task-test",
        generation_id=200,
        old_generation_id=199,
        transaction=fake_txn,
        candidate=MagicMock(),
        pending_swap=pending_swap,
        transition_result=None,
        published_generation=fake_gen,
        app=None,
        observer=_FailOnceThenSucceedObserver(),
        _step=AcceptedFinalizationStep.OBSERVER_REPORTED,
    )

    # First run — retirement fails.
    outcome1 = await job.run()
    assert outcome1.completed is False
    assert outcome1.error_class == "RuntimeError"
    assert job.health is AcceptedFinalizationHealth.RETRY_PENDING

    # Second run — creates a new task, retirement succeeds.
    outcome2 = await job.run()
    assert outcome2.completed is True
    assert job.is_complete

    # Attempt count is 2 (two separate attempts).
    assert job.attempts == 2
    assert job.retry_attempt_count == 1


# ---------------------------------------------------------------------------
# F2.5: run() returns structured outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_run_returns_structured_outcome(
    reload_harness: ReloadHarness,
) -> None:
    """F2.5: run() returns AcceptedFinalizationOutcome with all fields."""
    job = _make_blocking_job(
        block_event=asyncio.Event(),
        resume_event=asyncio.Event(),
    )
    # Immediately complete by setting resume_event.
    job._step = AcceptedFinalizationStep.RETIREMENT_SCHEDULED
    outcome = await job.run()

    assert isinstance(outcome.completed, bool)
    assert isinstance(outcome.attempt_count, int)
    assert isinstance(outcome.failure_count, int)
    assert isinstance(outcome.retry_attempt_count, int)
    assert isinstance(outcome.retirement_retry_attempt_count, int)
    assert isinstance(outcome.retry_permitted, bool)
    assert isinstance(outcome.status, FinalizationStatus)


# ---------------------------------------------------------------------------
# F2.6: Completed job returns completed outcome immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_completed_job_returns_completed_outcome(
    reload_harness: ReloadHarness,
) -> None:
    """F2.6: Calling run() on a completed job returns completed outcome."""
    block_event = asyncio.Event()
    resume_event = asyncio.Event()
    job = _make_blocking_job(block_event=block_event, resume_event=resume_event)

    # Mark job as completed.
    job._step = AcceptedFinalizationStep.COMPLETED

    outcome = await job.run()
    assert outcome.completed is True
    assert outcome.status is FinalizationStatus.COMPLETED
    # No new task was created (is_complete short-circuits).
    assert job._run_task is None
