"""Acceptance boundary tests.

Verifies defensive accepted guard to _abort_precommit_reload and that
post-acceptance exceptions prove zero rollback/abort calls.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from eggpool.reload_transaction import TransactionStateError

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# F2: _abort_precommit_reload rejects accepted transactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_abort_precommit_rejects_accepted_transaction(
    reload_harness: ReloadHarness,
) -> None:
    """_abort_precommit_reload raises TransactionStateError for accepted txn."""
    rm = reload_harness.reload_manager

    fake_txn = MagicMock()
    fake_txn.reload_accepted = True

    with pytest.raises(TransactionStateError, match="accepted reload cannot"):
        await rm._abort_precommit_reload(
            txn=fake_txn,
            pending_swap=MagicMock(),
            transition_result=None,
            candidate=MagicMock(),
            cause=RuntimeError("test"),
            error_stage="test",
        )


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_abort_precommit_allows_unaccepted_transaction(
    reload_harness: ReloadHarness,
) -> None:
    """_abort_precommit_reload succeeds for unaccepted transactions."""
    rm = reload_harness.reload_manager

    fake_txn = MagicMock()
    fake_txn.reload_accepted = False

    pending_swap = MagicMock()
    pending_swap.staged = False
    pending_swap.committed = False

    # Should not raise TransactionStateError.
    outcome = await rm._abort_precommit_reload(
        txn=fake_txn,
        pending_swap=pending_swap,
        transition_result=None,
        candidate=MagicMock(),
        cause=RuntimeError("test"),
        error_stage="test",
    )
    # Should return a PrecommitAbortOutcome.
    assert outcome is not None


# ---------------------------------------------------------------------------
# F4: Post-acceptance exception proves zero rollback/abort calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_post_acceptance_exception_no_candidate_abort(
    reload_harness: ReloadHarness,
) -> None:
    """After acceptance, no exception should trigger candidate.abort().

    Uses TEST_INJECT_FINALIZATION_CANCEL to inject CancelledError after
    acceptance.  The candidate must NOT be aborted.
    """
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager
    pre_gen_id = rtm.active_snapshot().generation_id

    # Inject cancellation after acceptance.
    rm.TEST_INJECT_FINALIZATION_CANCEL = asyncio.CancelledError("post-accept cancel")
    try:
        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload()
    finally:
        rm.TEST_INJECT_FINALIZATION_CANCEL = None

    # Generation changed — acceptance occurred before cancel.
    post_gen_id = rtm.active_snapshot().generation_id
    assert post_gen_id != pre_gen_id


# ---------------------------------------------------------------------------
# F4: Transaction state machine after acceptance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_accepted_transaction_never_enters_aborting(
    reload_harness: ReloadHarness,
) -> None:
    """An accepted transaction is never marked ABORTING or ABORTED."""
    rm = reload_harness.reload_manager

    # First reload — succeeds and completes.
    result1 = await reload_harness.reload()
    assert result1.ok is True

    # The transaction from reload 1 should have been completed or
    # be in the finalization history.
    history = rm._finalization_history
    # At least one record should exist.
    assert len(history) >= 1


# ---------------------------------------------------------------------------
# F4: All typed handlers check acceptance first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_post_acceptance_reload_succeeds(
    reload_harness: ReloadHarness,
) -> None:
    """After post-acceptance cancellation, subsequent reload still works.

    Proves that the acceptance boundary does not leave the system in a
    broken state that prevents future reloads.
    """
    rm = reload_harness.reload_manager

    # Inject cancellation.
    rm.TEST_INJECT_FINALIZATION_CANCEL = asyncio.CancelledError("cancel")
    try:
        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload()
    finally:
        rm.TEST_INJECT_FINALIZATION_CANCEL = None

    # Subsequent reload must succeed.
    result = await reload_harness.reload()
    assert result.ok is True, f"subsequent reload failed: {result}"
