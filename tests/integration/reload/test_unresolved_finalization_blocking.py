"""Plan 019 closure gate #8 — Admission blocking on unresolved finalization.

A new reload cannot bypass unresolved accepted finalization.  When a
finalization job remains non-complete, the admission path retries it
once and then rejects the new reload with ReloadInProgressError.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.control.accepted_finalization import (
    AcceptedFinalizationStep,
    AcceptedReloadFinalizationJob,
)
from eggpool.control.reload_manager import ReloadInProgressError

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Closure gate #8: unresolved finalization blocks new reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_unresolved_finalization_blocks_new_reload(
    reload_harness: ReloadHarness,
) -> None:
    """New reload is rejected when a finalization job is still unresolved.

    1. First reload succeeds — gen 0 → gen 1.
    2. Manually create an unresolved finalization job (stuck at retirement).
    3. Second reload must be rejected with ReloadInProgressError.
    """
    rm = reload_harness.reload_manager

    # First reload.
    result1 = await reload_harness.reload()
    assert result1.ok is True

    # Manually create an unresolved finalization job that will always fail.
    # The pending_swap.finalize_retirement mock always raises.
    fake_txn = MagicMock()
    fake_txn.accepted_finalization = MagicMock()
    fake_txn.digest_prefix = "test"
    fake_gen = MagicMock()
    fake_gen.generation_id = 99
    fake_pending_swap = MagicMock()
    fake_pending_swap.finalize_retirement = AsyncMock(
        side_effect=RuntimeError("permanent retirement failure")
    )

    job = AcceptedReloadFinalizationJob(
        request_id="admission-block-test",
        generation_id=99,
        old_generation_id=98,
        transaction=fake_txn,
        candidate=MagicMock(),
        pending_swap=fake_pending_swap,
        transition_result=None,
        published_generation=fake_gen,
        app=None,
        observer=MagicMock(),
        _step=AcceptedFinalizationStep.OBSERVER_REPORTED,
    )
    rm._accepted_finalization_jobs[job.request_id] = job

    # The job should be unresolved.
    assert job.is_unresolved

    # Second reload must be rejected.
    with pytest.raises(ReloadInProgressError, match="finalization still pending"):
        await reload_harness.reload(config=reload_harness.candidate_config)


# ---------------------------------------------------------------------------
# Closure gate #8: retry-before-admit completes resolved job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_retry_before_admit_completes_resolved_job(
    reload_harness: ReloadHarness,
) -> None:
    """When the pending job can be retried, admission succeeds after retry.

    1. First reload succeeds.
    2. Inject fail-once retirement failure.
    3. Second reload — retirement fails; job unresolved.
    4. Clear the seam.
    5. Third reload — admission retries the pending job, completes it,
       and admits the new reload.
    """
    rm = reload_harness.reload_manager

    # First reload.
    result1 = await reload_harness.reload()
    assert result1.ok is True

    # Inject fail-once retirement failure.
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("fail-once retirement")
    try:
        result2 = await reload_harness.reload(config=reload_harness.initial_config)
        assert result2.ok is True
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    # Unresolved job exists.
    unresolved = [j for j in rm._accepted_finalization_jobs.values() if j.is_unresolved]
    assert len(unresolved) >= 1

    # Third reload — admission retries the pending job and succeeds.
    result3 = await reload_harness.reload(config=reload_harness.candidate_config)
    assert result3.ok is True, f"third reload failed: {result3}"

    # All finalization jobs should now be complete.
    still_unresolved = [
        j for j in rm._accepted_finalization_jobs.values() if j.is_unresolved
    ]
    assert len(still_unresolved) == 0, (
        "all jobs should be resolved after admission retry"
    )
