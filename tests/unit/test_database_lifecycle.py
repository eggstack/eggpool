"""Database fail-closed lifecycle tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database, DatabaseLifecycleState, _classify_error_kind
from eggpool.db.migrations import EXPECTED_SCHEMA_VERSION, MigrationRunner
from eggpool.errors import DatabaseConnectionInvalidatedError, DatabaseError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def test_db() -> AsyncGenerator[Database, None]:
    """Provide a fresh in-memory database with migrations applied."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        yield db
    finally:
        await db.disconnect()


async def test_initial_lifecycle_state_is_ready(test_db: Database) -> None:
    """A connected database is ready and admitted."""
    assert test_db.lifecycle_state is DatabaseLifecycleState.READY
    assert test_db.writes_admitted is True
    assert test_db.reads_admitted is True


async def test_disconnect_transitions_to_shutting_down(test_db: Database) -> None:
    """Orderly shutdown is terminal for the instance."""
    await test_db.disconnect()
    assert test_db.lifecycle_state is DatabaseLifecycleState.SHUTTING_DOWN
    assert test_db.writes_admitted is False
    assert test_db.reads_admitted is False


async def test_failed_closed_state_blocks_future_operations(test_db: Database) -> None:
    """A failed-closed instance cannot be reopened in process."""
    await test_db._invalidate_connection("test invalidation")  # type: ignore[reportPrivateUsage]
    assert test_db.lifecycle_state is DatabaseLifecycleState.FAILED_CLOSED
    assert test_db.writes_admitted is False
    assert test_db.reads_admitted is False
    with pytest.raises(DatabaseConnectionInvalidatedError):
        async with test_db.transaction():
            pass
    with pytest.raises(DatabaseConnectionInvalidatedError):
        await test_db.connect()


async def test_fatal_handler_runs_once(test_db: Database) -> None:
    """Repeated fatal notifications do not duplicate worker shutdown."""
    reasons: list[str] = []
    test_db.set_fatal_handler(reasons.append)
    await test_db._invalidate_connection("first failure")  # type: ignore[reportPrivateUsage]
    await test_db._invalidate_connection("second failure")  # type: ignore[reportPrivateUsage]
    assert reasons == ["first failure"]


async def test_invalidated_reason_class_is_classified(test_db: Database) -> None:
    """Fatal diagnostics retain a coarse reason category."""
    await test_db._invalidate_connection("rollback failure — simulated")  # type: ignore[reportPrivateUsage]
    diags = test_db.diagnostics()
    assert diags["lifecycle_state"] == "failed_closed"
    assert diags["connection_state"] == "failed_closed"
    assert diags["invalidated_reason_class"] == "rollback_failure"


async def test_sqlite_error_classification_uses_code_and_message() -> None:
    class SQLiteFaultError(RuntimeError):
        def __init__(self, message: str, code: int) -> None:
            super().__init__(message)
            self.sqlite_errorcode = code

    locked = SQLiteFaultError("database is locked", 5)
    assert _classify_error_kind(locked) == "busy"

    corrupt = SQLiteFaultError("file is not a database", 26)
    assert _classify_error_kind(corrupt) == "corruption"


async def test_diagnostics_expose_bounded_state(test_db: Database) -> None:
    """Diagnostics expose state without removed recovery machinery."""
    diags = test_db.diagnostics()
    assert diags["lifecycle_state"] == "ready"
    assert diags["writes_admitted"] is True
    assert diags["reads_admitted"] is True
    assert "connection_epoch" not in diags
    assert "pending_ambiguous_operations" not in diags


async def test_expected_schema_version_is_positive() -> None:
    """The expected schema version is a positive integer."""
    assert EXPECTED_SCHEMA_VERSION > 0


async def test_foreign_loop_access_is_rejected(tmp_path: object) -> None:
    """A database instance belongs to the loop that connected it."""
    from pathlib import Path

    db_path = str(Path(str(tmp_path)) / "foreign-loop.sqlite3")
    db = Database(path=db_path)

    async def first_loop() -> None:
        await db.connect()

    async def second_loop() -> None:
        with pytest.raises(DatabaseError, match="foreign event loop"):
            await db.fetch_one("SELECT 1")

    await first_loop()
    await asyncio.to_thread(asyncio.run, second_loop())
