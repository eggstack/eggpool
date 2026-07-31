"""Dispatch reconciliation tests.

Verifies the dispatch reconciler (``_reconcile_dispatch``), the
transaction-owned ambiguous-operation lifecycle, and the production
wiring in the transaction commit path.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import (
    AmbiguousDatabaseOperation,
    Database,
)
from eggpool.db.migrations import MigrationRunner
from eggpool.db.recovery import DatabaseRecoveryController
from eggpool.errors import DatabaseCommitError, DatabaseError
from eggpool.models.config import DatabaseRecoveryConfig

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


def _make_ambiguous_op(
    *,
    proxy_request_id: str = "req-001",
    attempt_number: int = 1,
    epoch: int = 1,
) -> AmbiguousDatabaseOperation:
    """Build a dispatch-selection ambiguous operation for tests."""
    return AmbiguousDatabaseOperation(
        operation_kind="dispatch_selection",
        connection_epoch=epoch,
        operation_id=proxy_request_id,
        idempotency_keys=(
            ("proxy_request_id", proxy_request_id),
            ("attempt_number", str(attempt_number)),
        ),
        intended_status="committed",
        precondition_facts=(("account_id", "1"),),
        created_at_monotonic=0.0,
        reconciliation_strategy="dispatch",
    )


@pytest_asyncio.fixture()
async def test_db() -> Database:
    """Provide a fresh in-memory database with migrations applied."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    return db


async def test_transaction_descriptor_records_on_indeterminate(
    test_db: Database,
) -> None:
    """Pending ambiguous op is recorded in the deque on indeterminate commit."""
    op = _make_ambiguous_op(proxy_request_id="req-indet")
    test_db.set_test_inject_commit_call(RuntimeError("simulated commit failure"))
    test_db.set_test_inject_in_transaction_before_rollback(False)

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with test_db.transaction(ambiguous_operation=op):
            await test_db.execute_returning("SELECT 1")

    assert exc_info.value.outcome == "indeterminate"

    pending = test_db.pending_ambiguous_operations()
    assert len(pending) == 1
    assert pending[0].operation_id == "req-indet"

    test_db.set_test_inject_commit_call(None)
    test_db.set_test_inject_in_transaction_before_rollback(None)


async def test_descriptor_is_not_recorded_on_successful_commit(
    test_db: Database,
) -> None:
    """A descriptor is not recorded on the happy path."""
    op = _make_ambiguous_op(proxy_request_id="req-happy")
    async with test_db.transaction(ambiguous_operation=op):
        await test_db.execute_returning("SELECT 1")

    # Happy path: no ambiguity is recorded.
    assert test_db.pending_ambiguous_operations() == ()


async def test_descriptor_is_recorded_once_on_indeterminate(
    test_db: Database,
) -> None:
    """On indeterminate failure the operation is recorded once."""
    op = _make_ambiguous_op(proxy_request_id="req-move")
    test_db.set_test_inject_commit_call(RuntimeError("simulated failure"))
    test_db.set_test_inject_in_transaction_before_rollback(False)

    with pytest.raises(DatabaseCommitError):
        async with test_db.transaction(ambiguous_operation=op):
            await test_db.execute_returning("SELECT 1")

    # Pending slot is cleared, operation is in the deque.
    assert len(test_db.pending_ambiguous_operations()) == 1
    pending = test_db.pending_ambiguous_operations()
    assert len(pending) == 1
    assert pending[0].operation_id == "req-move"

    test_db.set_test_inject_commit_call(None)
    test_db.set_test_inject_in_transaction_before_rollback(None)


async def test_concurrent_transactions_keep_descriptor_with_owner(
    test_db: Database,
) -> None:
    """A waiter cannot overwrite the descriptor of the lock owner."""
    owner_entered = asyncio.Event()
    waiter_started = asyncio.Event()
    release_owner = asyncio.Event()
    owner_op = _make_ambiguous_op(proxy_request_id="owner")
    waiter_op = _make_ambiguous_op(proxy_request_id="waiter")

    async def owner() -> None:
        test_db.set_test_inject_commit_call(RuntimeError("ambiguous owner"))
        test_db.set_test_inject_in_transaction_before_rollback(False)
        with pytest.raises(DatabaseCommitError):
            async with test_db.transaction(ambiguous_operation=owner_op):
                owner_entered.set()
                await release_owner.wait()

    async def waiter() -> None:
        await owner_entered.wait()
        waiter_started.set()
        with pytest.raises(DatabaseError):  # connection is invalidated by owner
            async with test_db.transaction(ambiguous_operation=waiter_op):
                pass

    owner_task = asyncio.create_task(owner())
    waiter_task = asyncio.create_task(waiter())
    await owner_entered.wait()
    await waiter_started.wait()
    release_owner.set()
    await asyncio.gather(owner_task, waiter_task)

    assert [op.operation_id for op in test_db.pending_ambiguous_operations()] == [
        "owner"
    ]


async def test_reconcile_dispatch_committed_complete(test_db: Database) -> None:
    """Reconciler returns 'committed' when request, attempt, and reservation exist."""
    # Seed the database with a complete request bundle.
    async with test_db.transaction():
        acct_id = await test_db.execute_insert(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            ("test-acct", "TEST_KEY"),
        )
        await test_db.execute_insert(
            "INSERT INTO models (model_id, display_name, protocol) VALUES (?, ?, ?)",
            ("test-model", "Test Model", "openai"),
        )
        await test_db.execute_insert(
            "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
            (acct_id, "test-model"),
        )
        req_id = await test_db.execute_insert(
            "INSERT INTO requests "
            "(account_id, model_id, proxy_request_id, status, provider_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (acct_id, "test-model", "req-disp-001", "selected", "test-provider"),
        )
        await test_db.execute_insert(
            "INSERT INTO request_attempts "
            "(request_id, attempt_number, account_id, status_code) "
            "VALUES (?, ?, ?, ?)",
            (req_id, 1, acct_id, 200),
        )
        await test_db.execute_insert(
            "INSERT INTO reservations "
            "(request_id, account_id, model_id, reserved_microdollars, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (req_id, acct_id, "test-model", 0, "active"),
        )

    op = _make_ambiguous_op(proxy_request_id="req-disp-001", attempt_number=1)

    from eggpool.db.recovery import _reconcile_dispatch  # noqa: PLC0415

    result = await _reconcile_dispatch(test_db, op)
    assert result == "committed"


async def test_reconcile_dispatch_absent(test_db: Database) -> None:
    """Reconciler returns 'absent' when the referenced request does not exist."""
    op = _make_ambiguous_op(proxy_request_id="nonexistent-req", attempt_number=1)

    from eggpool.db.recovery import _reconcile_dispatch  # noqa: PLC0415

    result = await _reconcile_dispatch(test_db, op)
    assert result == "absent"


async def test_recovery_controller_reconciles_dispatch_ops(
    tmp_path: Path,
) -> None:
    """Full recovery cycle drains and reconciles pending dispatch ops."""
    db_path = str(tmp_path / "recon_test.db")
    db = Database(path=db_path)
    await db.connect()
    await MigrationRunner(db).run()

    config = DatabaseRecoveryConfig(
        max_attempts=2,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=db, config=config)

    try:
        # Seed a complete request bundle that the reconciler will find.
        async with db.transaction():
            acct_id = await db.execute_insert(
                "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                ("recon-acct", "RECON_KEY"),
            )
            await db.execute_insert(
                "INSERT INTO models (model_id, display_name, protocol) "
                "VALUES (?, ?, ?)",
                ("recon-model", "Recon Model", "openai"),
            )
            await db.execute_insert(
                "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
                (acct_id, "recon-model"),
            )
            req_id = await db.execute_insert(
                "INSERT INTO requests "
                "(account_id, model_id, proxy_request_id, status, provider_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (acct_id, "recon-model", "req-recon-001", "selected", "prov"),
            )
            await db.execute_insert(
                "INSERT INTO request_attempts "
                "(request_id, attempt_number, account_id, status_code) "
                "VALUES (?, ?, ?, ?)",
                (req_id, 1, acct_id, 200),
            )
            await db.execute_insert(
                "INSERT INTO reservations "
                "(request_id, account_id, model_id, reserved_microdollars, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (req_id, acct_id, "recon-model", 0, "active"),
            )

        # Record an ambiguous dispatch operation in the deque.
        op = _make_ambiguous_op(proxy_request_id="req-recon-001", attempt_number=1)
        db.record_ambiguous_operation(op)
        assert len(db.pending_ambiguous_operations()) == 1

        # Trigger invalidation → recovery.
        await db._invalidate_connection("test recovery trigger")  # type: ignore[reportPrivateUsage]
        await asyncio.wait_for(
            controller.recover_blocking(timeout_s=10.0), timeout=10.0
        )

        # After recovery the ambiguous deque is drained.
        assert db.pending_ambiguous_operations() == ()
        snapshot = controller.snapshot()
        assert snapshot.pending_ambiguous_operations == 0
        assert snapshot.successful_recoveries >= 1
        assert snapshot.last_attempt is not None
        assert snapshot.last_attempt.ambiguous_resolved >= 1
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)
        await db.disconnect()


async def test_pending_ambiguous_ops_diagnostics(test_db: Database) -> None:
    """diagnostics() reports the correct pending_ambiguous_operations count."""
    diags = test_db.diagnostics()
    assert diags["pending_ambiguous_operations"] == 0

    op = _make_ambiguous_op(proxy_request_id="req-diag-001")
    test_db.record_ambiguous_operation(op)
    diags = test_db.diagnostics()
    assert diags["pending_ambiguous_operations"] == 1

    op2 = _make_ambiguous_op(proxy_request_id="req-diag-002")
    test_db.record_ambiguous_operation(op2)
    diags = test_db.diagnostics()
    assert diags["pending_ambiguous_operations"] == 2

    for op in test_db.pending_ambiguous_operations():
        test_db.acknowledge_ambiguous_operation(op)
    diags = test_db.diagnostics()
    assert diags["pending_ambiguous_operations"] == 0
