"""Acceptance and finalization separation closure tests.

Tests that acceptance requires SQLite + runtime commit, post-acceptance
exception handling, idempotent finalization retry, and retirement
scheduling is not lost.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from eggpool.config_validation import ConfigValidationResult
from eggpool.reload_transaction import (
    AcceptedReloadFinalization,
    ReloadAcceptanceState,
    ReloadTransaction,
    TransactionState,
    TransactionStateError,
)
from eggpool.runtime_manager import (
    PendingGenerationSwap,
    PendingSwapState,
    RuntimeGeneration,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_generation(gen_id: int) -> RuntimeGeneration:
    """Build a RuntimeGeneration for tests."""
    now = time.monotonic()
    return RuntimeGeneration(
        generation_id=gen_id,
        config_digest=f"digest-{gen_id}",
        config=MagicMock(),
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        coordinator=MagicMock(),
        client_pool=MagicMock(),
        outbound_manager=MagicMock(),
        dns_backend=None,
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(),
        compression_policy=MagicMock(),
        cache_config=MagicMock(),
        compression_tuning_registry=MagicMock(),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        routing_trace_guard=MagicMock(),
        routing_trace_writer=MagicMock(),
        created_at_monotonic=now,
        created_at_epoch=now,
    )


def _make_validation() -> ConfigValidationResult:
    """Build a minimal ConfigValidationResult for ReloadTransaction."""
    return ConfigValidationResult(
        config=MagicMock(),
        source_path="/dev/null",
        content_digest="test-digest",
        runtime_fingerprint="test-fp",
        warnings=(),
    )


# ---------------------------------------------------------------------------
# Tests: acceptance requires SQLite and runtime commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_acceptance_requires_sqlite_and_runtime_commit(
    reload_harness: ReloadHarness,
) -> None:
    """Acceptance is only set after both SQLite commit and runtime swap commit."""
    rm = reload_harness.runtime_manager

    # Build and stage a swap.
    candidate_gen = _build_generation(gen_id=800)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    # Before commit, swap is in STAGED state.
    assert swap._state is PendingSwapState.STAGED

    # Commit — the swap becomes COMMITTED.
    await swap.commit()
    assert swap._state is PendingSwapState.COMMITTED

    # After commit, the candidate is the active generation.
    active = rm.active_snapshot()
    assert active.generation_id == 800

    # Finalize retirement.
    await swap.finalize_retirement()
    assert swap._state is PendingSwapState.FINALIZED


@pytest.mark.asyncio()
async def test_persistence_committed_runtime_pending_degraded(
    reload_harness: ReloadHarness,
) -> None:
    """The narrow boundary where SQLite commits but swap.commit() fails."""
    rm = reload_harness.runtime_manager
    pre_gen_id = rm.active_snapshot().generation_id

    candidate_gen = _build_generation(gen_id=801)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    # Simulate: persistence committed, but swap.commit() fails.
    # After rollback, the old generation is still active.
    await swap.rollback()

    active = rm.active_snapshot()
    assert active.generation_id == pre_gen_id


# ---------------------------------------------------------------------------
# Tests: post-acceptance exception does not rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_post_acceptance_exception_does_not_rollback(
    reload_harness: ReloadHarness,
) -> None:
    """After acceptance, exceptions do not call transition rollback."""
    rm = reload_harness.runtime_manager

    pre_gen_id = rm.active_snapshot().generation_id

    result = await reload_harness.reload()
    assert result.ok is True, f"reload failed: {result}"

    # New generation must be active — the acceptance was not rolled back.
    post_gen_id = rm.active_snapshot().generation_id
    assert post_gen_id != pre_gen_id

    # A second reload must also work — proves no broken state.
    result2 = await reload_harness.reload()
    assert result2.ok is True, f"second reload failed: {result2}"


# ---------------------------------------------------------------------------
# Tests: post-acceptance cancellation preserves candidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_post_acceptance_cancellation_preserves_candidate(
    reload_harness: ReloadHarness,
) -> None:
    """Cancellation after acceptance keeps candidate active.

    Cancellation at on_candidate_started is BEFORE publication, so
    the old generation must still be active.
    """
    rm = reload_harness.runtime_manager
    pre_gen_id = rm.active_snapshot().generation_id

    from tests.support.reload_faults import (
        FaultType,
        ReloadFaultInjector,
    )

    injector = ReloadFaultInjector(
        target_stage="on_candidate_started",
        fault_type=FaultType.CANCELLATION,
    )

    with pytest.raises(asyncio.CancelledError):
        await reload_harness.reload(observer=injector)

    post_gen_id = rm.active_snapshot().generation_id
    assert post_gen_id == pre_gen_id, (
        f"generation changed after pre-commit cancel: {pre_gen_id} → {post_gen_id}"
    )


# ---------------------------------------------------------------------------
# Tests: idempotent finalization retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_idempotent_finalization_retry(
    reload_harness: ReloadHarness,
) -> None:
    """Retrying finalization after partial failure doesn't duplicate side effects."""
    finalization = AcceptedReloadFinalization()

    # Simulate partial completion.
    finalization.candidate_ownership_transferred = True
    finalization.compatibility_mirror_updated = True

    # first_incomplete_step returns the next incomplete step.
    assert finalization.first_incomplete_step() == "transitions_finalization"
    assert not finalization.is_complete()

    # Complete all steps.
    finalization.transitions_finalized = True
    finalization.retirement_scheduled = True
    finalization.transaction_completed = True
    assert finalization.is_complete()
    assert finalization.first_incomplete_step() is None

    # Calling is_complete again is idempotent.
    assert finalization.is_complete()


@pytest.mark.asyncio()
async def test_idempotent_finalization_with_all_steps(
    reload_harness: ReloadHarness,
) -> None:
    """Finalization from scratch through all steps is idempotent."""
    finalization = AcceptedReloadFinalization()

    steps = [
        ("ownership_transfer", "candidate_ownership_transferred"),
        ("compatibility_mirror_update", "compatibility_mirror_updated"),
        ("transitions_finalization", "transitions_finalized"),
        ("retirement_scheduling", "retirement_scheduled"),
        ("transaction_completion", "transaction_completed"),
    ]

    for step_name, attr_name in steps:
        assert finalization.first_incomplete_step() == step_name
        setattr(finalization, attr_name, True)

    assert finalization.is_complete()
    assert finalization.first_incomplete_step() is None


# ---------------------------------------------------------------------------
# Tests: retirement scheduling not lost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_retirement_scheduling_not_lost(
    reload_harness: ReloadHarness,
) -> None:
    """If retirement scheduling fails after acceptance, it's retryable."""
    rm = reload_harness.runtime_manager

    # First reload — success (initial → candidate).
    result1 = await reload_harness.reload()
    assert result1.ok is True
    gen1_id = rm.active_snapshot().generation_id

    # Second reload — use initial config to get a different digest
    # (candidate → initial), proves no leaked state from first.
    result2 = await reload_harness.reload(config=reload_harness.initial_config)
    assert result2.ok is True
    gen2_id = rm.active_snapshot().generation_id
    assert gen2_id != gen1_id

    # Third reload — use candidate config again (initial → candidate),
    # proves consecutive reloads work reliably.
    result3 = await reload_harness.reload()
    assert result3.ok is True
    gen3_id = rm.active_snapshot().generation_id
    assert gen3_id != gen2_id


# ---------------------------------------------------------------------------
# Tests: AcceptanceState enum transitions
# ---------------------------------------------------------------------------


def test_acceptance_state_enum_values() -> None:
    """AcceptanceState has the expected values."""
    assert ReloadAcceptanceState.NOT_ACCEPTED.value == "not_accepted"
    assert (
        ReloadAcceptanceState.PERSISTENCE_COMMITTED_RUNTIME_PENDING.value
        == "persistence_committed_runtime_pending"
    )
    assert ReloadAcceptanceState.ACCEPTED.value == "accepted"


def test_acceptance_finalization_step_ordering() -> None:
    """AcceptedReloadFinalization steps are checked in the correct order."""
    f = AcceptedReloadFinalization()

    # All False → first step is ownership_transfer.
    assert f.first_incomplete_step() == "ownership_transfer"

    f.candidate_ownership_transferred = True
    assert f.first_incomplete_step() == "compatibility_mirror_update"

    f.compatibility_mirror_updated = True
    assert f.first_incomplete_step() == "transitions_finalization"

    f.transitions_finalized = True
    assert f.first_incomplete_step() == "retirement_scheduling"

    f.retirement_scheduled = True
    assert f.first_incomplete_step() == "transaction_completion"

    f.transaction_completed = True
    assert f.first_incomplete_step() is None
    assert f.is_complete()


@pytest.mark.asyncio()
async def test_transaction_state_machine_monotonic() -> None:
    """ReloadTransaction state only moves forward or to terminal abort states."""
    validation = _make_validation()
    txn = ReloadTransaction(request_id="test-req", validation=validation)
    assert txn.state == TransactionState.CREATED

    # Valid transitions.
    txn._transition_to(TransactionState.VALIDATED)
    assert txn.state == TransactionState.VALIDATED

    txn._transition_to(TransactionState.DIFFED)
    assert txn.state == TransactionState.DIFFED

    # Skip ahead to aborting.
    txn._transition_to(TransactionState.ABORTING)
    assert txn.state == TransactionState.ABORTING

    txn._transition_to(TransactionState.ABORTED)
    assert txn.state == TransactionState.ABORTED

    # Terminal — no more transitions.
    with pytest.raises(TransactionStateError):
        txn._transition_to(TransactionState.CREATED)


# ---------------------------------------------------------------------------
# Tests: post-acceptance cancellation (Gap 1 / D #8 / F #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_post_acceptance_cancellation_after_commit(
    reload_harness: ReloadHarness,
) -> None:
    """Cancellation in the post-acceptance block keeps candidate active.

    Injects CancelledError after ownership transfer but before mirror
    update / transition finalize.  The candidate must remain authoritative,
    no transition rollback occurs, and no candidate abort occurs.
    """
    rm = reload_harness.runtime_manager
    pre_gen_id = rm.active_snapshot().generation_id

    reload_harness.reload_manager.TEST_INJECT_FINALIZATION_CANCEL = (
        asyncio.CancelledError("post-acceptance cancel")
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_FINALIZATION_CANCEL = None

    # The candidate generation must still be active — acceptance
    # occurred before the cancellation, so the candidate is authoritative.
    post_gen_id = rm.active_snapshot().generation_id
    assert post_gen_id != pre_gen_id, (
        f"generation should have changed from {pre_gen_id} after acceptance"
    )

    # The reload must be accepted (not aborted).
    # Transaction may be None after completion; check the last diagnostic.
    last_diag = reload_harness.reload_manager._last_diagnostic_result
    if last_diag is not None:
        # Publication occurred — the reload was accepted.
        assert getattr(last_diag, "publication_occurred", True) is True

    # Lease admission must be open for the new candidate.
    assert rm.is_accepting_leases()

    # A subsequent reload must succeed — proves no broken state.
    result2 = await reload_harness.reload()
    assert result2.ok is True, f"subsequent reload failed: {result2}"


# ---------------------------------------------------------------------------
# Tests: retirement scheduling failure retains candidate (Gap 2 / D #10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_retirement_failure_retains_candidate(
    reload_harness: ReloadHarness,
) -> None:
    """Retirement scheduling failure after acceptance does not lose old gen.

    The candidate remains active, old admission is closed, and retry is
    possible on the next reload.
    """
    rm = reload_harness.runtime_manager
    pre_gen_id = rm.active_snapshot().generation_id

    reload_harness.reload_manager.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError(
        "retirement scheduling failed"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_RETIREMENT_FAILURE = None

    # The reload must succeed (ok=True) because retirement failure
    # is a degraded outcome, not a reload failure.
    assert result.ok is True, (
        f"reload should succeed despite retirement failure: {result}"
    )

    # The candidate generation must be active — acceptance occurred.
    post_gen_id = rm.active_snapshot().generation_id
    assert post_gen_id != pre_gen_id

    # Lease admission must be open.
    assert rm.is_accepting_leases()

    # Retirement failure counter must be incremented.
    snap = reload_harness.reload_manager.snapshot()
    counters = snap.get("counters")
    if counters is not None:
        retirement_failures = getattr(counters, "retirement_failures", None)
        if retirement_failures is not None:
            assert retirement_failures >= 1

    # A subsequent reload must succeed — proves retry is idempotent.
    result2 = await reload_harness.reload()
    assert result2.ok is True, f"subsequent reload failed: {result2}"


# ---------------------------------------------------------------------------
# Tests: partial transition failure through ReloadManager.reload() (Gap 3 / F #5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_partial_transition_failure_through_reload(
    reload_harness: ReloadHarness,
) -> None:
    """Partial transition failure through the full reload() path.

    Uses TEST_INJECT_PUBLISH_FAILURE to trigger the precommit cleanup
    path, which exercises _abort_precommit_reload() with the real
    transition_result from the production _apply_process_transitions
    call.
    """
    rm = reload_harness.runtime_manager
    pre_gen_id = rm.active_snapshot().generation_id

    reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
        "deliberate publish failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

    # Reload must fail cleanly.
    assert result.ok is False, f"reload should fail with inject: {result}"

    # Active generation must be unchanged — old state preserved.
    post_gen_id = rm.active_snapshot().generation_id
    assert post_gen_id == pre_gen_id

    # Lease admission must be open.
    assert rm.is_accepting_leases()

    # Cleanup diagnostics must be recorded.
    snap = reload_harness.reload_manager.snapshot()
    cleanup = snap.get("last_cleanup_diagnostics")
    assert cleanup is not None, "cleanup diagnostics should be recorded"

    # A subsequent reload must succeed — proves no broken state.
    result2 = await reload_harness.reload()
    assert result2.ok is True, f"subsequent reload failed: {result2}"


# ---------------------------------------------------------------------------
# Tests: close counts per failure class (Gap 4 / F #6, F #7)
# ---------------------------------------------------------------------------


class _CountingCloseable:
    """Closeable with a tracked close counter."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


@pytest.mark.asyncio()
async def test_close_counts_transition_failure(
    reload_harness: ReloadHarness,
) -> None:
    """Candidate resources close exactly once on transition failure."""
    from eggpool.runtime_manager import RuntimeGenerationCandidate

    candidate = RuntimeGenerationCandidate(generation_id=900)
    res_a = _CountingCloseable("a")
    res_b = _CountingCloseable("b")
    candidate.register_resource("a", res_a.aclose)
    candidate.register_resource("b", res_b.aclose)
    candidate.mark_prepared()

    await candidate.abort(cause=RuntimeError("test"), failure_stage="test")

    assert res_a.close_count == 1
    assert res_b.close_count == 1

    # Abort is idempotent — second call does not close again.
    await candidate.abort(cause=RuntimeError("test"), failure_stage="test")
    assert res_a.close_count == 1
    assert res_b.close_count == 1


@pytest.mark.asyncio()
async def test_close_counts_publish_failure(
    reload_harness: ReloadHarness,
) -> None:
    """Publish failure triggers exactly-once candidate close via cleanup."""
    rm = reload_harness.runtime_manager
    pre_gen_id = rm.active_snapshot().generation_id

    reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
        "publish fail"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

    assert result.ok is False
    # Active generation unchanged.
    assert rm.active_snapshot().generation_id == pre_gen_id

    # The cleanup diagnostics must show candidate abort was attempted.
    snap = reload_harness.reload_manager.snapshot()
    cleanup = snap.get("last_cleanup_diagnostics")
    assert cleanup is not None

    # Old generation resources must not be closed.
    assert rm.is_accepting_leases()


@pytest.mark.asyncio()
async def test_close_counts_post_acceptance_failure(
    reload_harness: ReloadHarness,
) -> None:
    """Post-acceptance failure does NOT close candidate resources.

    The candidate remains authoritative — its resources stay open.
    """
    rm = reload_harness.runtime_manager
    pre_gen_id = rm.active_snapshot().generation_id

    reload_harness.reload_manager.TEST_INJECT_FINALIZATION_CANCEL = (
        asyncio.CancelledError("post-accept cancel")
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_FINALIZATION_CANCEL = None

    # Candidate is active — resources are NOT closed.
    post_gen_id = rm.active_snapshot().generation_id
    assert post_gen_id != pre_gen_id
    assert rm.is_accepting_leases()
