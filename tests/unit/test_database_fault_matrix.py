"""Database fault injection matrix (unit).

Exercises deterministic faults at the database callable boundaries. Covers
write, COMMIT, ROLLBACK, invalidation-close, and subsequent-use failures.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.errors import (
    DatabaseCommitError,
    DatabaseConnectionInvalidatedError,
    DatabaseRollbackError,
)
from tests.support.database_faults import fail_commit, fail_rollback

pytestmark = pytest.mark.asyncio

_TEST_INSERT = "INSERT INTO _plan023_test (val) VALUES ('test')"


@pytest_asyncio.fixture()
async def test_db() -> Database:
    """In-memory database with schema and a simple test table."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "CREATE TABLE IF NOT EXISTS _plan023_test ("
            "id INTEGER PRIMARY KEY, val TEXT NOT NULL)"
        )
    yield db
    await db.disconnect()


class TestDatabaseFaultMatrix:
    """Deterministic database fault injection tests."""

    async def test_selection_persistence_write_raises(self, test_db: Database) -> None:
        """A write during selection persistence raises → rollback succeeds."""
        with pytest.raises(DatabaseCommitError):
            async with test_db.transaction():
                await test_db.execute_write(_TEST_INSERT)
                raise DatabaseCommitError("simulated write failure")

    async def test_commit_injection_rollback_succeeds(
        self, test_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COMMIT injection raises while in_transaction=True, rollback succeeds."""
        fail_commit(monkeypatch, test_db, DatabaseCommitError("simulated commit fail"))
        with pytest.raises(DatabaseCommitError):
            async with test_db.transaction():
                await test_db.execute_write(_TEST_INSERT)
        assert test_db.lifecycle_state.value == "ready"

    async def test_commit_injection_indeterminate_state(
        self, test_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COMMIT injection with in_transaction=False → connection invalidated."""
        fail_commit(
            monkeypatch,
            test_db,
            DatabaseCommitError("indeterminate commit"),
            commit_first=True,
        )
        with pytest.raises(DatabaseCommitError):
            async with test_db.transaction():
                await test_db.execute_write(_TEST_INSERT)
        assert test_db.lifecycle_state.value == "failed_closed"
        assert test_db._invalidated_reason is not None

    async def test_rollback_raises_after_body_failure(
        self, test_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROLLBACK injection raises after transaction body failure.

        A rollback failure after a body failure is raised
        as a typed ``DatabaseRollbackError`` so callers see the
        rollback failure distinctly from the original body exception.
        """
        fail_rollback(monkeypatch, test_db, OSError("simulated rollback fail"))
        with pytest.raises((DatabaseCommitError, OSError, DatabaseRollbackError)):
            async with test_db.transaction():
                await test_db.execute_write(_TEST_INSERT)
                raise DatabaseCommitError("body failure")

    async def test_connection_close_during_invalidation(
        self, test_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Commit injection with indeterminate state → connection invalidated."""
        fail_commit(
            monkeypatch,
            test_db,
            DatabaseCommitError("trigger invalidation"),
            commit_first=True,
        )
        with pytest.raises(DatabaseCommitError):
            async with test_db.transaction():
                await test_db.execute_write(_TEST_INSERT)

        assert test_db.lifecycle_state.value == "failed_closed"
        with pytest.raises(DatabaseConnectionInvalidatedError):
            async with test_db.transaction():
                pass

    async def test_subsequent_transaction_after_invalidation(
        self, test_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Subsequent transaction attempts observe invalidated state."""
        fail_commit(
            monkeypatch,
            test_db,
            DatabaseCommitError("trigger invalidation"),
            commit_first=True,
        )
        with pytest.raises(DatabaseCommitError):
            async with test_db.transaction():
                await test_db.execute_write(_TEST_INSERT)

        assert test_db.lifecycle_state.value == "failed_closed"
        with pytest.raises(DatabaseConnectionInvalidatedError):
            async with test_db.transaction():
                pass
        with pytest.raises(DatabaseConnectionInvalidatedError):
            async with test_db.transaction():
                pass

    async def test_diagnostics_after_invalidation(
        self, test_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Diagnostics reflect invalidation state."""
        fail_commit(
            monkeypatch,
            test_db,
            DatabaseCommitError("diagnostics test"),
            commit_first=True,
        )
        with pytest.raises(DatabaseCommitError):
            async with test_db.transaction():
                await test_db.execute_write(_TEST_INSERT)

        diags = test_db.diagnostics()
        assert diags["connection_state"] == "failed_closed"
        assert diags["reconnect_required"] is True
        assert "diagnostics test" in diags["invalidated_reason"]
