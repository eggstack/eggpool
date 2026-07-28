"""Plan 021 acceptance accounting and process-owned reconciliation proofs."""

from __future__ import annotations

import asyncio
import gc
import weakref
from typing import TYPE_CHECKING

import pytest

from eggpool.control.reload_manager import ReloadObserver

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


class _BlockingPublishObserver(ReloadObserver):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def on_publish_complete(self, **_: object) -> None:
        self.entered.set()
        await self.release.wait()

    async def on_retirement_started(self, **_: object) -> None:
        return None


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_cancelled_waiter_is_reconciled_by_process_owned_callback(
    reload_harness: ReloadHarness,
) -> None:
    """Cancelling the reload waiter cannot suppress accepted accounting."""
    rm = reload_harness.reload_manager
    observer = _BlockingPublishObserver()
    before = rm.snapshot()["counters"]
    reload_task = asyncio.create_task(
        reload_harness.reload(observer=observer),
    )
    await asyncio.wait_for(observer.entered.wait(), timeout=10.0)

    reload_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reload_task

    after_acceptance = rm.snapshot()["counters"]
    assert after_acceptance["accepted_reloads"] == before["accepted_reloads"] + 1
    assert after_acceptance["committed_reloads"] == before["committed_reloads"] + 1
    job = next(iter(rm._accepted_finalization_jobs.values()))
    captured_refs = [
        weakref.ref(job),
        weakref.ref(job.transaction),
        weakref.ref(job.candidate),
        weakref.ref(job.pending_swap),
    ]

    observer.release.set()
    for _ in range(100):
        await asyncio.sleep(0)
        if rm.snapshot()["unresolved_finalization_count"] == 0:
            break
    final = rm.snapshot()
    assert final["unresolved_finalization_count"] == 0
    assert (
        final["counters"]["fully_finalized_reloads"]
        >= before["fully_finalized_reloads"] + 1
    )
    del job
    for _ in range(10):
        gc.collect()
        if all(reference() is None for reference in captured_refs):
            break
        await asyncio.sleep(0)
    assert all(reference() is None for reference in captured_refs)


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_completed_job_is_swept_before_admission(
    reload_harness: ReloadHarness,
) -> None:
    """Admission removes a done-but-not-yet-callback-reconciled job."""
    rm = reload_harness.reload_manager
    result = await reload_harness.reload()
    assert result.ok is True
    assert rm.snapshot()["unresolved_finalization_count"] == 0

    # The callback and inline waiter race is idempotent: a second reload
    # cannot be blocked by a completed operational job.
    result2 = await reload_harness.reload(config=reload_harness.initial_config)
    assert result2.ok is True
    assert rm.snapshot()["unresolved_finalization_count"] == 0


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_failure_and_retry_counters_use_per_attempt_deltas(
    reload_harness: ReloadHarness,
) -> None:
    """Two failed observations are both counted before eventual recovery."""
    rm = reload_harness.reload_manager
    await reload_harness.reload()

    rm.TEST_PERSISTENT_RETIREMENT_FAILURE = RuntimeError("persistent retirement")
    try:
        result = await reload_harness.reload(config=reload_harness.initial_config)
        assert result.finalization_status == "retirement_schedule_failed"
        first = rm.snapshot()["counters"]
        await rm.drain_finalization_jobs(timeout_s=0.01)
        second = rm.snapshot()["counters"]
    finally:
        rm.TEST_PERSISTENT_RETIREMENT_FAILURE = None

    assert second["accepted_finalization_failures"] == (
        first["accepted_finalization_failures"] + 1
    )
    assert second["accepted_finalization_retries"] == (
        first["accepted_finalization_retries"] + 1
    )


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
@pytest.mark.slow()
async def test_one_hundred_accepted_cancellations_do_not_retain_jobs(
    reload_harness: ReloadHarness,
) -> None:
    """Repeated post-acceptance cancellation leaves a bounded registry."""
    rm = reload_harness.reload_manager
    before = rm.snapshot()["counters"]
    for index in range(100):
        observer = _BlockingPublishObserver()
        config = (
            reload_harness.candidate_config
            if index % 2 == 0
            else reload_harness.initial_config
        )
        task = asyncio.create_task(
            reload_harness.reload(config=config, observer=observer),
        )
        await asyncio.wait_for(observer.entered.wait(), timeout=10.0)
        task.cancel()
        # Release the retained process-owned attempt immediately so the
        # cancellation handler does not spend its bounded critical-prefix
        # budget waiting on an intentionally blocked observer.
        observer.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            await asyncio.sleep(0)
            if rm.snapshot()["unresolved_finalization_count"] == 0:
                break
        assert rm.snapshot()["unresolved_finalization_count"] == 0

    final = rm.snapshot()
    assert final["counters"]["accepted_reloads"] == (before["accepted_reloads"] + 100)
    assert final["counters"]["committed_reloads"] == (before["committed_reloads"] + 100)
    assert final["counters"]["fully_finalized_reloads"] == (
        before["fully_finalized_reloads"] + 100
    )
    assert final["accepted_finalization_jobs"] == []
    assert len(final["finalization_history"]) <= 32
