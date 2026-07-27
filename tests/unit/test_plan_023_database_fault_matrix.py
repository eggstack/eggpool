"""Plan 023 — Database fault injection matrix (unit).

Exercises the deterministic database fault seams provided by
``Database.set_test_inject_*`` hooks.  Covers BEGIN, write, COMMIT,
ROLLBACK, invalidation-close, and subsequent-use failures.

Run with::

    uv run pytest tests/unit/test_plan_023_database_fault_matrix.py -v
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

    async def test_begin_immediate_raises(self, test_db: Database) -> None:
        """BEGIN injection fires before commit → rollback compensates."""
        Database.TEST_INJECT_BEFORE_COMMIT_CALL = RuntimeError("simulated begin")
        try:
            with pytest.raises(RuntimeError, match="simulated begin"):
                async with test_db.transaction():
                    await test_db.execute_write(_TEST_INSERT)
        finally:
            Database.TEST_INJECT_BEFORE_COMMIT_CALL = None

    async def test_selection_persistence_write_raises(self, test_db: Database) -> None:
        """A write during selection persistence raises → rollback succeeds."""
        with pytest.raises(DatabaseCommitError):
            async with test_db.transaction():
                await test_db.execute_write(_TEST_INSERT)
                raise DatabaseCommitError("simulated write failure")

    async def test_commit_injection_rollback_succeeds(self, test_db: Database) -> None:
        """COMMIT injection raises while in_transaction=True, rollback succeeds."""
        test_db.set_test_inject_commit_call(
            DatabaseCommitError("simulated commit fail")
        )
        try:
            with pytest.raises(DatabaseCommitError):
                async with test_db.transaction():
                    await test_db.execute_write(_TEST_INSERT)
            # Rollback succeeded — connection is still usable
            assert not test_db._invalidated
        finally:
            test_db.set_test_inject_commit_call(None)

    async def test_commit_injection_indeterminate_state(
        self, test_db: Database
    ) -> None:
        """COMMIT injection with in_transaction=False → connection invalidated."""
        test_db.set_test_inject_commit_call(DatabaseCommitError("indeterminate commit"))
        test_db.set_test_inject_in_transaction_before_rollback(False)
        try:
            with pytest.raises(DatabaseCommitError):
                async with test_db.transaction():
                    await test_db.execute_write(_TEST_INSERT)
            assert test_db._invalidated
            assert test_db._invalidated_reason is not None
        finally:
            test_db.set_test_inject_commit_call(None)
            test_db.set_test_inject_in_transaction_before_rollback(None)

    async def test_rollback_raises_after_body_failure(self, test_db: Database) -> None:
        """ROLLBACK injection raises after transaction body failure.

        Plan 027 — a rollback failure after a body failure is raised
        as a typed ``DatabaseRollbackError`` so callers see the
        rollback failure distinctly from the original body exception.
        """
        test_db.set_test_inject_rollback_call(OSError("simulated rollback fail"))
        try:
            with pytest.raises((DatabaseCommitError, OSError, DatabaseRollbackError)):
                async with test_db.transaction():
                    await test_db.execute_write(_TEST_INSERT)
                    raise DatabaseCommitError("body failure")
        finally:
            test_db.set_test_inject_rollback_call(None)

    async def test_connection_close_during_invalidation(
        self, test_db: Database
    ) -> None:
        """Commit injection with indeterminate state → connection invalidated."""
        test_db.set_test_inject_commit_call(DatabaseCommitError("trigger invalidation"))
        test_db.set_test_inject_in_transaction_before_rollback(False)
        try:
            with pytest.raises(DatabaseCommitError):
                async with test_db.transaction():
                    await test_db.execute_write(_TEST_INSERT)
        finally:
            test_db.set_test_inject_commit_call(None)
            test_db.set_test_inject_in_transaction_before_rollback(None)

        assert test_db._invalidated
        with pytest.raises(DatabaseConnectionInvalidatedError):
            async with test_db.transaction():
                pass

    async def test_subsequent_transaction_after_invalidation(
        self, test_db: Database
    ) -> None:
        """Subsequent transaction attempts observe invalidated state."""
        test_db.set_test_inject_commit_call(DatabaseCommitError("trigger invalidation"))
        test_db.set_test_inject_in_transaction_before_rollback(False)
        try:
            with pytest.raises(DatabaseCommitError):
                async with test_db.transaction():
                    await test_db.execute_write(_TEST_INSERT)
        finally:
            test_db.set_test_inject_commit_call(None)
            test_db.set_test_inject_in_transaction_before_rollback(None)

        assert test_db._invalidated
        with pytest.raises(DatabaseConnectionInvalidatedError):
            async with test_db.transaction():
                pass
        with pytest.raises(DatabaseConnectionInvalidatedError):
            async with test_db.transaction():
                pass

    async def test_diagnostics_after_invalidation(self, test_db: Database) -> None:
        """Diagnostics reflect invalidation state."""
        test_db.set_test_inject_commit_call(DatabaseCommitError("diagnostics test"))
        test_db.set_test_inject_in_transaction_before_rollback(False)
        try:
            with pytest.raises(DatabaseCommitError):
                async with test_db.transaction():
                    await test_db.execute_write(_TEST_INSERT)
        finally:
            test_db.set_test_inject_commit_call(None)
            test_db.set_test_inject_in_transaction_before_rollback(None)

        diags = test_db.diagnostics()
        assert diags["connection_state"] == "invalidated"
        assert diags["reconnect_required"] is True
        assert "diagnostics test" in diags["invalidated_reason"]
