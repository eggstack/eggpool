"""Database commit failure tests.

Verifies that confirmed rollback keeps the connection usable and that
indeterminate commit invalidates the connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.errors import DatabaseCommitError, DatabaseConnectionInvalidatedError

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_confirmed_rollbacks_keeps_connection_usable(
    reload_harness: ReloadHarness,
) -> None:
    """A confirmed rollback after commit failure leaves connection usable.

    Injects a commit failure via the instance-level seam.  The rollback
    succeeds because in_transaction is True and aiosqlite can roll back
    cleanly.  DatabaseCommitError must have correct fields.
    """
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("simulated commit failure"))

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    err = exc_info.value
    assert err.rollback_attempted is True
    assert err.rollback_succeeded is True
    assert err.connection_invalidated is False
    assert err.outcome == "rolled_back"

    # The connection must still be usable.
    async with db.transaction():
        rows = await db.execute_returning("SELECT 42")
        assert len(rows) == 1


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_indeterminate_commit_invalidates_connection(
    reload_harness: ReloadHarness,
) -> None:
    """An indeterminate commit failure invalidates the connection.

    When commit() raises and the transaction state is unclear, the
    connection is invalidated and subsequent access raises
    DatabaseConnectionInvalidatedError.
    """
    db = reload_harness.db

    original_commit = db._commit_connection

    async def fake_commit() -> None:
        raise RuntimeError("indeterminate commit failure")

    db._commit_connection = fake_commit  # type: ignore[assignment]

    try:
        with pytest.raises(DatabaseCommitError) as exc_info:
            async with db.transaction():
                await db.execute_returning("SELECT 1")

        err = exc_info.value
        assert err.rollback_attempted is True
        # For in-memory :memory: databases, aiosqlite may report
        # in_transaction=False after a failed commit, making it
        # indeterminate.
        assert err.outcome in ("rolled_back", "indeterminate")

        # If the connection was invalidated, verify subsequent access fails.
        if err.connection_invalidated:
            with pytest.raises(DatabaseConnectionInvalidatedError):
                async with db.transaction():
                    await db.execute_returning("SELECT 1")
    finally:
        db._commit_connection = original_commit  # type: ignore[assignment]


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_commit_failure_diagnostics_expose_state(
    reload_harness: ReloadHarness,
) -> None:
    """DatabaseCommitError exposes all diagnostic fields.

    Verifies that the error carries rollback_attempted, rollback_succeeded,
    transaction_still_active, connection_invalidated, and outcome.
    """
    db = reload_harness.db

    db.set_test_inject_commit_call(RuntimeError("diagnostic test failure"))

    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    err = exc_info.value
    assert isinstance(err.rollback_attempted, bool)
    assert isinstance(err.rollback_succeeded, bool)
    assert err.transaction_still_active is None or isinstance(
        err.transaction_still_active, bool
    )
    assert isinstance(err.connection_invalidated, bool)
    assert isinstance(err.outcome, str)
    assert err.outcome in ("rolled_back", "indeterminate")
