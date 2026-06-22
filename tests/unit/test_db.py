"""Tests for database operations."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.errors import DatabaseError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest.mark.asyncio()
async def test_connect_disconnect(tmp_path: pytest.TempPathFactory) -> None:
    database = Database(path=str(tmp_path / "disconnect_test.sqlite3"))
    await database.connect()
    assert database.connection is not None
    await database.disconnect()


@pytest.mark.asyncio()
async def test_connect_rejects_duplicate_connection(
    tmp_path: pytest.TempPathFactory,
) -> None:
    database = Database(path=str(tmp_path / "duplicate.sqlite3"))
    await database.connect()
    try:
        with pytest.raises(DatabaseError, match="already connected"):
            await database.connect()
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_connect_closes_partially_initialized_connection(monkeypatch) -> None:
    connection = MagicMock()
    connection.execute = AsyncMock(side_effect=RuntimeError("pragma failed"))
    connection.close = AsyncMock()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr("eggpool.db.connection.aiosqlite.connect", connect)
    database = Database(path="broken.sqlite3")

    with pytest.raises(DatabaseError, match="pragma failed"):
        await database.connect()

    connection.close.assert_awaited_once_with()
    with pytest.raises(DatabaseError, match="not connected"):
        _ = database.connection


@pytest.mark.asyncio()
async def test_wal_mode_enabled(db: Database) -> None:
    cursor = await db.fetch_one("PRAGMA journal_mode")
    assert cursor is not None
    assert cursor["journal_mode"].lower() == "wal"


@pytest.mark.asyncio()
async def test_foreign_keys_enforced(db: Database) -> None:
    cursor = await db.fetch_one("PRAGMA foreign_keys")
    assert cursor is not None
    assert cursor["foreign_keys"] == 1


@pytest.mark.asyncio()
async def test_migrations_run_idempotently(db: Database) -> None:
    runner = MigrationRunner(db)
    applied_before = await runner._applied_versions()

    await runner.run()
    applied_after = await runner._applied_versions()

    assert applied_before == applied_after


@pytest.mark.asyncio()
async def test_migrations_create_tables(db: Database) -> None:
    tables = await db.fetch_all(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE '\\_%' ESCAPE '\\'"
    )
    table_names = {row["name"] for row in tables}
    assert "accounts" in table_names
    assert "models" in table_names
    assert "requests" in table_names
    assert "reservations" in table_names
    assert "account_models" in table_names


@pytest.mark.asyncio()
async def test_execute_and_fetch(db: Database) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            ("test_exec", "TEST_ENV"),
        )
    row = await db.fetch_one("SELECT * FROM accounts WHERE name = ?", ("test_exec",))
    assert row is not None
    assert row["name"] == "test_exec"


@pytest.mark.asyncio()
async def test_fetch_all_returns_rows(db: Database) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            ("row1", "ENV1"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            ("row2", "ENV2"),
        )
    rows = await db.fetch_all("SELECT * FROM accounts ORDER BY name")
    assert len(rows) == 2


@pytest.mark.asyncio()
async def test_fetch_one_returns_none(db: Database) -> None:
    row = await db.fetch_one("SELECT * FROM accounts WHERE name = ?", ("nonexistent",))
    assert row is None


@pytest.mark.asyncio()
async def test_busy_timeout_pragma(db: Database) -> None:
    cursor = await db.fetch_one("PRAGMA busy_timeout")
    assert cursor is not None
    assert cursor["timeout"] == 5000


@pytest.mark.asyncio()
async def test_synchronous_pragma(db: Database) -> None:
    cursor = await db.fetch_one("PRAGMA synchronous")
    assert cursor is not None
    assert cursor["synchronous"] == 1


@pytest.mark.asyncio()
async def test_crash_recovery_marks_stale_pending(db: Database) -> None:
    """Stale pending requests are marked as interrupted."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            ("acct1", "ENV1"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await db.execute_write(
            "INSERT INTO requests (account_id, model_id, status, protocol, streamed) "
            "VALUES (?, 'gpt-4', 'pending', 'openai', 0)",
            (1,),
        )

    # Simulate stale request (started more than 10 minutes ago)
    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET started_at = datetime('now', '-15 minutes') "
            "WHERE id = last_insert_rowid()"
        )

    # Run crash recovery
    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET status = 'interrupted', "
            "completed_at = CURRENT_TIMESTAMP "
            "WHERE status = 'pending' "
            "AND started_at < datetime('now', '-10 minutes')"
        )

    row = await db.fetch_one("SELECT status FROM requests")
    assert row is not None
    assert row["status"] == "interrupted"


@pytest.mark.asyncio()
async def test_crash_recovery_releases_stale_reservations(db: Database) -> None:
    """Active reservations for stale requests are released."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            ("acct1", "ENV1"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await db.execute_write(
            "INSERT INTO requests (account_id, model_id, status, protocol, streamed) "
            "VALUES (?, 'gpt-4', 'pending', 'openai', 0)",
            (1,),
        )
        req_id = 1
        await db.execute_write(
            "INSERT INTO reservations "
            "(request_id, account_id, model_id, estimated_tokens, "
            "reserved_microdollars, expires_at) "
            "VALUES (?, 1, 'gpt-4', 1000, 0, datetime('now', '+5 minutes'))",
            (req_id,),
        )

    # Simulate stale reservation
    async with db.transaction():
        await db.execute_write(
            "UPDATE reservations SET created_at = datetime('now', '-15 minutes') "
            "WHERE id = last_insert_rowid()"
        )

    # Run crash recovery
    async with db.transaction():
        await db.execute_write(
            "UPDATE reservations SET status = 'released', "
            "released_at = CURRENT_TIMESTAMP, release_reason = 'crash_recovery' "
            "WHERE status = 'active' "
            "AND created_at < datetime('now', '-10 minutes')"
        )

    row = await db.fetch_one("SELECT status FROM reservations")
    assert row is not None
    assert row["status"] == "released"
