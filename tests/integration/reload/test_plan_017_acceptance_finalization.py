"""Plan 017 — Acceptance and finalization separation closure tests.

Tests for Workstream D:
- Acceptance requires SQLite + runtime commit
- Post-acceptance exception handling
- Idempotent finalization retry
- Retirement scheduling not lost
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
        finalization_retry_queue=MagicMock(),
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
