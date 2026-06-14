"""Tests for database operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner

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
    await db.execute(
        "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
        ("test_exec", "TEST_ENV"),
    )
    row = await db.fetch_one("SELECT * FROM accounts WHERE name = ?", ("test_exec",))
    assert row is not None
    assert row["name"] == "test_exec"


@pytest.mark.asyncio()
async def test_fetch_all_returns_rows(db: Database) -> None:
    await db.execute(
        "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
        ("row1", "ENV1"),
    )
    await db.execute(
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
