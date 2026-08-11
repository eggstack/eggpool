"""Rollback failure invalidation tests.

Verifies that rollback failures after body exceptions or commit failures
correctly invalidate the connection, raise the right typed error, and
leave diagnostics in a consistent state.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from eggpool.db.connection import Database, DatabaseLifecycleState
from eggpool.db.migrations import MigrationRunner
from eggpool.errors import DatabaseCommitError, DatabaseRollbackError
from tests.support.database_faults import fail_commit, fail_rollback

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def test_db() -> Database:
    """Provide a fresh in-memory database with migrations applied."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    return db


async def test_rollback_failure_invalidates_connection(
    test_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body raises + rollback fails → DatabaseRollbackError, connection invalidated."""
    fail_rollback(monkeypatch, test_db, RuntimeError("forced rollback fail"))
    with pytest.raises(DatabaseRollbackError) as exc_info:
        async with test_db.transaction():
            await test_db.execute_returning("SELECT 1")
            raise RuntimeError("body failure")

    err = exc_info.value
    assert err.rollback_attempted is True
    assert err.rollback_succeeded is False
    assert err.connection_invalidated is True
    assert isinstance(err.original_exception, RuntimeError)

    assert test_db.lifecycle_state is DatabaseLifecycleState.FAILED_CLOSED
    assert test_db.writes_admitted is False


async def test_successful_rollback_preserves_connection(
    test_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit fails + rollback succeeds → DatabaseCommitError, connection usable."""
    fail_commit(monkeypatch, test_db, RuntimeError("forced commit fail"))
    with pytest.raises(DatabaseCommitError) as exc_info:
        async with test_db.transaction():
            await test_db.execute_returning("SELECT 1")

    err = exc_info.value
    assert err.outcome == "rolled_back"
    assert err.rollback_attempted is True
    assert err.rollback_succeeded is True
    assert err.connection_invalidated is False

    assert test_db.lifecycle_state is DatabaseLifecycleState.READY
    assert test_db.writes_admitted is True

    # Verify the connection is still usable.
    async with test_db.transaction():
        await test_db.execute_returning("SELECT 1")


async def test_indeterminate_when_in_transaction_false(
    test_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit fails + in_transaction already False → indeterminate, invalidated."""
    fail_commit(
        monkeypatch,
        test_db,
        RuntimeError("forced commit fail"),
        commit_first=True,
    )
    with pytest.raises(DatabaseCommitError) as exc_info:
        async with test_db.transaction():
            await test_db.execute_returning("SELECT 1")

    err = exc_info.value
    assert err.outcome == "indeterminate"
    assert err.connection_invalidated is True
    assert test_db.lifecycle_state is DatabaseLifecycleState.FAILED_CLOSED
    assert test_db.writes_admitted is False


async def test_rollback_error_carrying_original_exception(
    test_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Body raises + rollback fails → original_exception is the rollback error."""
    original = RuntimeError("forced rollback fail")
    fail_rollback(monkeypatch, test_db, original)
    with pytest.raises(DatabaseRollbackError) as exc_info:
        async with test_db.transaction():
            await test_db.execute_returning("SELECT 1")
            raise RuntimeError("body failure")

    err = exc_info.value
    assert err.original_exception is original


async def test_diagnostics_after_rollback_failure(
    test_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After rollback failure, diagnostics expose lifecycle and reason class."""
    fail_rollback(monkeypatch, test_db, RuntimeError("forced rollback fail"))
    with pytest.raises(DatabaseRollbackError):
        async with test_db.transaction():
            await test_db.execute_returning("SELECT 1")
            raise RuntimeError("body failure")

    diags = test_db.diagnostics()
    assert diags["lifecycle_state"] == "failed_closed"
    assert diags["invalidated_reason_class"] == "rollback_failure"
    assert diags["invalidated_reason"] is not None
    assert "rollback" in diags["invalidated_reason"]
    assert diags["rollback_failure_count"] == 1


async def test_rollback_failure_count_increments(
    test_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback failure increments the rollback_failure_count counter."""
    assert test_db.rollback_failure_count == 0

    fail_rollback(monkeypatch, test_db, RuntimeError("forced rollback fail"))
    with pytest.raises(DatabaseRollbackError):
        async with test_db.transaction():
            await test_db.execute_returning("SELECT 1")
            raise RuntimeError("body failure")

    assert test_db.rollback_failure_count == 1


async def test_body_exception_with_rollback_failure_raises_rollback_error(
    test_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body exception + rollback failure → DatabaseRollbackError raised."""
    fail_rollback(monkeypatch, test_db, RuntimeError("forced rollback fail"))
    with pytest.raises(DatabaseRollbackError) as exc_info:
        async with test_db.transaction():
            await test_db.execute_returning("SELECT 1")
            raise ValueError("body error")

    err = exc_info.value
    assert isinstance(err.original_exception, RuntimeError)
    assert err.rollback_succeeded is False
    assert err.connection_invalidated is True


async def test_commit_error_with_successful_rollback_raises_commit_error(
    test_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit failure + rollback succeeds → DatabaseCommitError with rolled_back."""
    fail_commit(monkeypatch, test_db, RuntimeError("forced commit fail"))
    with pytest.raises(DatabaseCommitError) as exc_info:
        async with test_db.transaction():
            await test_db.execute_returning("SELECT 1")

    err = exc_info.value
    assert err.outcome == "rolled_back"
    assert err.rollback_succeeded is True
    assert err.connection_invalidated is False
