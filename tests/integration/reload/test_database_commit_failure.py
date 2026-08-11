"""Database commit failure tests.

Verifies that confirmed rollback keeps the connection usable and that
indeterminate commit invalidates the connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.errors import DatabaseCommitError, DatabaseConnectionInvalidatedError
from tests.support.database_faults import fail_commit

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_confirmed_rollbacks_keeps_connection_usable(
    reload_harness: ReloadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed rollback after commit failure leaves connection usable.

    Injects a commit failure via the instance-level seam.  The rollback
    succeeds because in_transaction is True and aiosqlite can roll back
    cleanly.  DatabaseCommitError must have correct fields.
    """
    db = reload_harness.db

    fail_commit(monkeypatch, db, RuntimeError("simulated commit failure"))

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
async def test_indeterminate_commit_invalidates_connection(
    reload_harness: ReloadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An indeterminate commit failure invalidates the connection.

    When commit() raises and the transaction state is unclear, the
    connection is invalidated and subsequent access raises
    DatabaseConnectionInvalidatedError.
    """
    db = reload_harness.db

    fail_commit(
        monkeypatch, db, RuntimeError("indeterminate commit failure"), commit_first=True
    )
    with pytest.raises(DatabaseCommitError) as exc_info:
        async with db.transaction():
            await db.execute_returning("SELECT 1")

    err = exc_info.value
    assert err.rollback_attempted is True
    assert err.outcome == "indeterminate"
    assert err.connection_invalidated is True
    with pytest.raises(DatabaseConnectionInvalidatedError):
        async with db.transaction():
            await db.execute_returning("SELECT 1")


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_commit_failure_diagnostics_expose_state(
    reload_harness: ReloadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DatabaseCommitError exposes all diagnostic fields.

    Verifies that the error carries rollback_attempted, rollback_succeeded,
    transaction_still_active, connection_invalidated, and outcome.
    """
    db = reload_harness.db

    fail_commit(monkeypatch, db, RuntimeError("diagnostic test failure"))

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
