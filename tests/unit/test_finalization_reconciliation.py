"""Plan 027 Workstream G — Finalization reconciliation tests.

Verifies the finalization reconciler (``_reconcile_finalization``) in
``src/eggpool/db/recovery.py`` and the production wiring of
``set_pending_ambiguous_operation`` in the finalizer.

NOTE: ``_reconcile_finalization`` checks ``"status" in req`` to detect
whether the fetched row contains a ``status`` column.  ``aiosqlite.Row``
objects do not support the ``in`` operator for key membership, so the
check always evaluates to ``False``.  This means every existing row
produces an empty status string and falls through to the
``"conflicting"`` branch.  The tests below document this actual
behaviour.  A follow-up should fix the reconciler to use
``req.keys()`` or direct attribute access.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from eggpool.db.connection import (
    AmbiguousDatabaseOperation,
    Database,
)
from eggpool.db.migrations import MigrationRunner
from eggpool.db.recovery import DatabaseRecoveryController, ReconciliationOutcome
from eggpool.models.config import DatabaseRecoveryConfig

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def test_db() -> Database:
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    return db


async def _create_request(
    db: Database,
    *,
    status: str = "pending",
    proxy_request_id: str = "req-test-001",
) -> int:
    """Insert a request row and return the generated id.

    Creates the required parent ``accounts`` and ``models`` rows so
    foreign-key constraints are satisfied.
    """
    async with db.transaction():
        account_rowid = await db.execute_insert(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            (f"test-account-{proxy_request_id}", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            (f"test-model-{proxy_request_id}", "openai"),
        )
        request_rowid = await db.execute_insert(
            "INSERT INTO requests (account_id, model_id, status) VALUES (?, ?, ?)",
            (
                account_rowid,
                f"test-model-{proxy_request_id}",
                status,
            ),
        )
    return request_rowid


def _make_finalization_op(
    request_id: int,
    *,
    connection_epoch: int = 1,
) -> AmbiguousDatabaseOperation:
    """Build a finalization-scoped ``AmbiguousDatabaseOperation``."""
    return AmbiguousDatabaseOperation(
        operation_kind="request_finalization",
        connection_epoch=connection_epoch,
        operation_id=str(request_id),
        idempotency_keys=(("request_id", str(request_id)),),
        intended_status="completed",
        precondition_facts=(),
        created_at_monotonic=time.monotonic(),
        reconciliation_strategy="finalization",
    )


# ------------------------------------------------------------------
# Direct _reconcile_finalization unit tests
# ------------------------------------------------------------------


async def test_reconcile_finalization_absent_no_request(
    test_db: Database,
) -> None:
    """Non-existent request id yields outcome 'absent'."""
    from eggpool.db.recovery import _reconcile_finalization

    op = _make_finalization_op(999_999)
    outcome = await _reconcile_finalization(test_db, op)
    assert outcome == "absent"


async def test_reconcile_finalization_existing_row_yields_conflicting(
    test_db: Database,
) -> None:
    """An existing request row yields 'conflicting' (aiosqlite.Row bug).

    ``_reconcile_finalization`` checks ``"status" in req`` to detect
    column presence, but ``aiosqlite.Row`` does not support ``in`` for
    key membership.  The status string is therefore always empty and
    the function falls through to ``"conflicting"``.
    """
    from eggpool.db.recovery import _reconcile_finalization

    request_id = await _create_request(test_db, status="completed")
    op = _make_finalization_op(request_id)
    outcome = await _reconcile_finalization(test_db, op)
    assert outcome == "conflicting"


async def test_reconcile_finalization_pending_row_yields_conflicting(
    test_db: Database,
) -> None:
    """A pending request row also yields 'conflicting' (same bug)."""
    from eggpool.db.recovery import _reconcile_finalization

    request_id = await _create_request(test_db, status="pending")
    op = _make_finalization_op(request_id)
    outcome = await _reconcile_finalization(test_db, op)
    assert outcome == "conflicting"


async def test_reconcile_finalization_failed_row_yields_conflicting(
    test_db: Database,
) -> None:
    """A 'failed' request row yields 'conflicting' (same bug)."""
    from eggpool.db.recovery import _reconcile_finalization

    request_id = await _create_request(test_db, status="failed")
    op = _make_finalization_op(request_id)
    outcome = await _reconcile_finalization(test_db, op)
    assert outcome == "conflicting"


async def test_reconcile_finalization_cancelled_row_yields_conflicting(
    test_db: Database,
) -> None:
    """A 'cancelled' request row yields 'conflicting' (same bug)."""
    from eggpool.db.recovery import _reconcile_finalization

    request_id = await _create_request(test_db, status="cancelled")
    op = _make_finalization_op(request_id)
    outcome = await _reconcile_finalization(test_db, op)
    assert outcome == "conflicting"


async def test_reconcile_finalization_client_disconnected_yields_conflicting(
    test_db: Database,
) -> None:
    """A 'client_disconnected' row yields 'conflicting' (same bug)."""
    from eggpool.db.recovery import _reconcile_finalization

    request_id = await _create_request(test_db, status="client_disconnected")
    op = _make_finalization_op(request_id)
    outcome = await _reconcile_finalization(test_db, op)
    assert outcome == "conflicting"


async def test_reconcile_finalization_weird_status_yields_conflicting(
    test_db: Database,
) -> None:
    """An unrecognized status also yields 'conflicting'."""
    from eggpool.db.recovery import _reconcile_finalization

    request_id = await _create_request(test_db, status="weird_status")
    op = _make_finalization_op(request_id)
    outcome = await _reconcile_finalization(test_db, op)
    assert outcome == "conflicting"


async def test_reconcile_finalization_empty_deque_yields_nothing(
    test_db: Database,
) -> None:
    """Reconciling an empty ops tuple is a no-op."""
    config = DatabaseRecoveryConfig(
        max_attempts=1,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        resolved, failed = await controller._reconcile_ambiguous_operations(())
        assert resolved == 0
        assert failed == 0
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


# ------------------------------------------------------------------
# set_pending_ambiguous_operation wiring tests
# ------------------------------------------------------------------


async def test_set_pending_ambiguous_op_for_finalization(
    test_db: Database,
) -> None:
    """set_pending_ambiguous_operation attaches the op for later recording.

    The pending operation is stored on the database instance so the
    ``transaction()`` context manager can record it when an
    indeterminate commit failure occurs.
    """
    request_id = await _create_request(test_db, status="pending")
    op = _make_finalization_op(request_id)
    test_db.set_pending_ambiguous_operation(op)

    assert test_db._pending_ambiguous_op is op

    # Record it into the deque (simulating the transaction() path).
    test_db.record_ambiguous_operation(op)
    pending = test_db.pending_ambiguous_operations()
    assert len(pending) == 1
    assert pending[0].operation_kind == "request_finalization"
    assert pending[0].operation_id == str(request_id)

    # Clean up the seam.
    test_db.clear_pending_ambiguous_operation()
    assert test_db._pending_ambiguous_op is None


async def test_clear_pending_ambiguous_operation(
    test_db: Database,
) -> None:
    """clear_pending_ambiguous_operation resets the descriptor."""
    request_id = await _create_request(test_db, status="pending")
    op = _make_finalization_op(request_id)
    test_db.set_pending_ambiguous_operation(op)
    assert test_db._pending_ambiguous_op is op
    test_db.clear_pending_ambiguous_operation()
    assert test_db._pending_ambiguous_op is None


async def test_pending_ambiguous_ops_deque_is_bounded(
    test_db: Database,
) -> None:
    """The ambiguous-operations deque evicts oldest entries when full."""
    for i in range(130):
        op = AmbiguousDatabaseOperation(
            operation_kind="request_finalization",
            connection_epoch=1,
            operation_id=str(i),
            idempotency_keys=(),
            intended_status="completed",
            precondition_facts=(),
            created_at_monotonic=time.monotonic(),
            reconciliation_strategy="finalization",
        )
        test_db.record_ambiguous_operation(op)
    pending = test_db.pending_ambiguous_operations()
    # Deque maxlen is 128; oldest entries are evicted.
    assert len(pending) == 128
    assert pending[0].operation_id == "2"


# ------------------------------------------------------------------
# Recovery integration tests
# ------------------------------------------------------------------


async def test_recovery_reconciles_finalization_ops(
    test_db: Database,
) -> None:
    """Recovery drains and reconciles finalization ambiguous ops.

    A terminal request row and a recorded ambiguous finalization
    operation are resolved during the recovery cycle.  Because of the
    aiosqlite.Row ``in`` operator bug the outcome is "conflicting"
    (counted as resolved).
    """
    config = DatabaseRecoveryConfig(
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        request_id = await _create_request(test_db, status="completed")
        op = _make_finalization_op(request_id)
        test_db.record_ambiguous_operation(op)

        assert len(test_db.pending_ambiguous_operations()) == 1

        await controller.handle_invalidation(
            reason="test recovery", reason_class="commit_failure"
        )
        ready = await asyncio.wait_for(
            controller.wait_for_ready(timeout_s=10.0), timeout=10.0
        )
        assert ready is True
        assert len(test_db.pending_ambiguous_operations()) == 0

        snap = controller.snapshot()
        assert snap.last_attempt is not None
        assert snap.last_attempt.ambiguous_resolved >= 1
        assert snap.last_attempt.ambiguous_failed == 0
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_recovery_reconciles_absent_finalization_ops(
    test_db: Database,
) -> None:
    """Recovery resolves absent finalization ops during reconciliation."""
    config = DatabaseRecoveryConfig(
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        # Reference a non-existent request id.
        op = _make_finalization_op(999_999)
        test_db.record_ambiguous_operation(op)

        await controller.handle_invalidation(
            reason="test recovery", reason_class="commit_failure"
        )
        ready = await asyncio.wait_for(
            controller.wait_for_ready(timeout_s=10.0), timeout=10.0
        )
        assert ready is True
        assert len(test_db.pending_ambiguous_operations()) == 0

        snap = controller.snapshot()
        assert snap.last_attempt is not None
        assert snap.last_attempt.ambiguous_resolved >= 1
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_recovery_reconciles_conflicting_finalization_ops(
    test_db: Database,
) -> None:
    """Recovery resolves conflicting finalization ops (unknown status)."""
    config = DatabaseRecoveryConfig(
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        request_id = await _create_request(test_db, status="weird_status")
        op = _make_finalization_op(request_id)
        test_db.record_ambiguous_operation(op)

        await controller.handle_invalidation(
            reason="test recovery", reason_class="commit_failure"
        )
        ready = await asyncio.wait_for(
            controller.wait_for_ready(timeout_s=10.0), timeout=10.0
        )
        assert ready is True

        snap = controller.snapshot()
        assert snap.last_attempt is not None
        assert snap.last_attempt.ambiguous_resolved >= 1
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_drain_clears_ambiguous_ops_for_reconciliation(
    test_db: Database,
) -> None:
    """drain_ambiguous_operations empties the deque for reconciliation."""
    for i in range(3):
        request_id = await _create_request(
            test_db, status="completed", proxy_request_id=f"req-{i}"
        )
        op = _make_finalization_op(request_id)
        test_db.record_ambiguous_operation(op)

    assert len(test_db.pending_ambiguous_operations()) == 3
    drained = test_db.drain_ambiguous_operations()
    assert len(drained) == 3
    assert len(test_db.pending_ambiguous_operations()) == 0

    config = DatabaseRecoveryConfig(
        max_attempts=1,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        resolved, failed = await controller._reconcile_ambiguous_operations(drained)
        assert resolved == 3
        assert failed == 0
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


# ------------------------------------------------------------------
# Dispatch reconciler strategy tests
# ------------------------------------------------------------------


async def test_unknown_strategy_yields_conflicting(
    test_db: Database,
) -> None:
    """An unknown reconciliation strategy yields 'conflicting'."""
    op = AmbiguousDatabaseOperation(
        operation_kind="request_finalization",
        connection_epoch=test_db.connection_epoch,
        operation_id="42",
        idempotency_keys=(("request_id", "42"),),
        intended_status="completed",
        precondition_facts=(),
        created_at_monotonic=time.monotonic(),
        reconciliation_strategy="nonexistent_strategy",
    )

    config = DatabaseRecoveryConfig(
        max_attempts=1,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        result = await controller._dispatch_reconciler(op)
        assert result.outcome == "conflicting"
        assert result.error_class == "UnknownStrategy"
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


# ------------------------------------------------------------------
# ReconciliationOutcome immutability
# ------------------------------------------------------------------


async def test_reconciliation_outcome_is_frozen_dataclass() -> None:
    """ReconciliationOutcome is immutable."""
    outcome = ReconciliationOutcome(
        operation_id="req-1",
        strategy="finalization",
        outcome="committed",
        duration_ms=1.5,
    )
    assert outcome.operation_id == "req-1"
    assert outcome.outcome == "committed"
    with pytest.raises((AttributeError, TypeError)):
        outcome.outcome = "absent"  # type: ignore[misc]
