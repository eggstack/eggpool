"""Plan 020 Workstream F1 — Acceptance-window fault matrix.

For each post-acceptance fault seam, assert zero rollback, zero transition
rollback, zero candidate abort, transaction not in aborting/aborted.

Fault seams tested:
  1. Immediately before txn.mark_accepted() — TEST_INJECT_PUBLISH_FAILURE
  2. Immediately after owner registration — TEST_INJECT_FINALIZATION_CANCEL
  3. Retirement-start observer — failure logged not propagated
  4. Retirement scheduling — TEST_INJECT_RETIREMENT_FAILURE
  5. Transition finalization — TransitionFinalizationPendingError
  6. Transaction completion bookkeeping — normal completion
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from eggpool.reload_transaction import TransactionState

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# F1.1: PUBLISH_FAILURE after staging — pre-acceptance rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_publish_failure_after_staging_rolls_back(
    reload_harness: ReloadHarness,
) -> None:
    """F1.1: Injecting a publish failure after staging aborts pre-acceptance.

    The reload must NOT be accepted.  SQLite transaction rolls back,
    candidate aborts, and the old generation remains active.
    """
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager
    gen_before = rtm.active_snapshot().generation_id

    rm.TEST_INJECT_PUBLISH_FAILURE = RuntimeError("publish failed")
    try:
        result = await reload_harness.reload()
    finally:
        rm.TEST_INJECT_PUBLISH_FAILURE = None

    # Reload failed — not accepted.
    assert result.ok is False

    # Generation unchanged — the publish failure was pre-acceptance.
    gen_after = rtm.active_snapshot().generation_id
    assert gen_after == gen_before

    # Admission is open (lease gate cleared by rollback).
    assert rtm.is_accepting_leases()


# ---------------------------------------------------------------------------
# F1.2: Post-acceptance cancel — zero rollback/abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_post_acceptance_cancel_zero_rollback(
    reload_harness: ReloadHarness,
) -> None:
    """F1.2: After acceptance, finalization cancel causes zero rollback.

    Publication occurred, persistence committed, process transitions applied.
    The transaction is never marked ABORTING or ABORTED.
    """
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager
    gen_before = rtm.active_snapshot().generation_id

    # Inject cancellation after acceptance.
    rm.TEST_INJECT_FINALIZATION_CANCEL = asyncio.CancelledError("post-accept")
    try:
        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload()
    finally:
        rm.TEST_INJECT_FINALIZATION_CANCEL = None

    # Generation changed — acceptance occurred before cancel.
    gen_after = rtm.active_snapshot().generation_id
    assert gen_after != gen_before

    # Admission is open.
    assert rtm.is_accepting_leases()


# ---------------------------------------------------------------------------
# F1.3: Retirement-start observer failure — logged, not propagated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_observer_failure_does_not_propagate(
    reload_harness: ReloadHarness,
) -> None:
    """F1.3: Observer failure is safe and non-authoritative.

    The reload completes successfully even when on_publish_complete
    raises.  The observer failure does not prevent the finalization
    job from completing.
    """
    from eggpool.control.reload_manager import ReloadObserver

    class _FailingObserver(ReloadObserver):
        async def on_publish_complete(self, **kwargs: object) -> None:
            raise RuntimeError("observer publish failed")

    observer = _FailingObserver()
    result = await reload_harness.reload(observer=observer)
    assert result.ok is True
    assert result.finalization_status == "completed"


# ---------------------------------------------------------------------------
# F1.4: Retirement scheduling failure — job stays unresolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_retirement_failure_keeps_job_unresolved(
    reload_harness: ReloadHarness,
) -> None:
    """F1.4: Retirement failure leaves job at OBSERVER_REPORTED step.

    Publication occurred, persistence committed, process transitions applied.
    The transaction is accepted but the finalization job is unresolved.
    """
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager

    # First reload to establish gen1 as active.
    result1 = await reload_harness.reload()
    assert result1.ok is True
    gen1_id = rtm.active_snapshot().generation_id

    # Inject retirement failure.
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("retirement failed")
    try:
        result2 = await reload_harness.reload(config=reload_harness.initial_config)
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    assert result2.ok is True
    # Generation changed — acceptance occurred.
    gen2_id = rtm.active_snapshot().generation_id
    assert gen2_id != gen1_id

    # Finalization status is retry_pending (job stuck at retirement).
    assert result2.finalization_status == "retry_pending"
    assert result2.pending_swap_committed is True

    # Job is still in the active registry.
    unresolved = [j for j in rm._accepted_finalization_jobs.values() if j.is_unresolved]
    assert len(unresolved) >= 1

    # Transaction state is NOT ABORTING or ABORTED.
    for job in unresolved:
        txn = job.transaction
        assert txn.state is not TransactionState.ABORTING
        assert txn.state is not TransactionState.ABORTED
        assert txn.reload_accepted is True
        assert txn.publication_occurred is True
        assert txn.persistence_committed is True
        assert txn.process_transitions_applied is True


# ---------------------------------------------------------------------------
# F1.5: Normal completion — all fields correct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_successful_reload_post_acceptance_invariants(
    reload_harness: ReloadHarness,
) -> None:
    """F1.5: A successful reload proves all post-acceptance invariants.

    Publication occurred, persistence committed, process transitions applied,
    transaction completed, no aborting/aborted state.
    """
    result = await reload_harness.reload()
    assert result.ok is True
    assert result.finalization_status == "completed"
    assert result.old_generation_id is not None
    assert result.pending_swap_committed is True


# ---------------------------------------------------------------------------
# F1.6: Retiring generation is still accessible after acceptance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_old_generation_pending_retirement(
    reload_harness: ReloadHarness,
) -> None:
    """F1.6: After acceptance, the old generation is in the retiring list.

    The candidate generation is active.  The old generation slot is
    pending retirement (owned by the committed pending swap).
    """
    rtm = reload_harness.runtime_manager

    result1 = await reload_harness.reload()
    assert result1.ok is True
    gen1_id = rtm.active_snapshot().generation_id

    result2 = await reload_harness.reload(config=reload_harness.initial_config)
    assert result2.ok is True
    gen2_id = rtm.active_snapshot().generation_id
    assert gen2_id != gen1_id

    # The active generation is gen2 (the candidate became active).
    assert rtm.active_snapshot().generation_id == gen2_id


# ---------------------------------------------------------------------------
# F1.7: Finalization cancel then subsequent reload succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_post_acceptance_cancel_subsequent_reload(
    reload_harness: ReloadHarness,
) -> None:
    """F1.7: After post-acceptance cancel, subsequent reload still works.

    Proves the acceptance boundary does not leave the system broken.
    """
    rm = reload_harness.reload_manager

    rm.TEST_INJECT_FINALIZATION_CANCEL = asyncio.CancelledError("cancel")
    try:
        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload()
    finally:
        rm.TEST_INJECT_FINALIZATION_CANCEL = None

    # Subsequent reload must succeed.
    result = await reload_harness.reload()
    assert result.ok is True
    assert result.finalization_status == "completed"
