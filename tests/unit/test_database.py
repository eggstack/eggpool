"""Tests for database operations."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

import aiosqlite
import pytest

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.errors import DatabaseError


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_account(db: Database, name: str = "test_account") -> int:
    cursor = await db.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) VALUES (?, ?, ?, ?)",
        (name, "TEST_KEY", 1, 1.0),
    )
    await db.connection.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def _seed_model(db: Database, model_id: str = "gpt-4o") -> None:
    await db.execute(
        "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
        (model_id, "GPT-4o"),
    )
    await db.connection.commit()


@pytest.mark.asyncio()
async def test_migration_creates_all_tables() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        rows = await database.fetch_all(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' "
            "AND name NOT LIKE '\\_%' ESCAPE '\\'"
        )
        table_names = {row["name"] for row in rows}

        expected = {
            "accounts",
            "models",
            "account_models",
            "requests",
            "reservations",
            "model_price_snapshots",
            "account_events",
        }
        assert expected.issubset(table_names)
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_migration_is_idempotent() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        rows_after_first = await database.fetch_all("SELECT version FROM _migrations")
        count_after_first = len(rows_after_first)

        await _run_migrations(database)
        rows_after_second = await database.fetch_all("SELECT version FROM _migrations")
        count_after_second = len(rows_after_second)

        assert count_after_second == count_after_first
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_wal_mode_enabled(tmp_path: object) -> None:
    import pathlib

    db_path = str(pathlib.Path(str(tmp_path)) / "wal_test.sqlite3")
    database = Database(path=db_path)
    await database.connect()
    try:
        row = await database.fetch_one("PRAGMA journal_mode")
        assert row is not None
        assert row["journal_mode"].lower() == "wal"
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_foreign_keys_enabled() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        row = await database.fetch_one("PRAGMA foreign_keys")
        assert row is not None
        assert row["foreign_keys"] == 1
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_insert_and_query_account() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        await database.execute(
            "INSERT INTO accounts "
            "(name, api_key_env, enabled, weight) "
            "VALUES (?, ?, ?, ?)",
            ("acct1", "ENV1", 1, 2.5),
        )
        await database.connection.commit()

        row = await database.fetch_one(
            "SELECT * FROM accounts WHERE name = ?",
            ("acct1",),
        )
        assert row is not None
        assert row["name"] == "acct1"
        assert row["api_key_env"] == "ENV1"
        assert row["enabled"] == 1
        assert row["weight"] == 2.5
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_insert_request_with_foreign_key() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        account_id = await _seed_account(database)
        await _seed_model(database)

        await database.execute(
            "INSERT INTO requests "
            "(account_id, model_id, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?)",
            (account_id, "gpt-4o", 100, 50),
        )
        await database.connection.commit()

        row = await database.fetch_one(
            "SELECT * FROM requests WHERE account_id = ?",
            (account_id,),
        )
        assert row is not None
        assert row["account_id"] == account_id
        assert row["model_id"] == "gpt-4o"
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_insert_request_invalid_account_fails() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        await _seed_model(database)

        with pytest.raises(DatabaseError):
            await database.execute(
                "INSERT INTO requests (account_id, model_id) VALUES (?, ?)",
                (9999, "gpt-4o"),
            )
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_concurrent_readers_during_write() -> None:
    db_uri = "file::memory:?cache=shared"
    conn1 = await aiosqlite.connect(db_uri, uri=True)
    conn2 = await aiosqlite.connect(db_uri, uri=True)
    try:
        for conn in (conn1, conn2):
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA journal_mode = WAL")
        await conn1.commit()
        await conn2.commit()

        db1 = Database.__new__(Database)
        db1._conn = conn1  # type: ignore[reportPrivateUsage]
        db1._path = db_uri  # type: ignore[reportPrivateUsage]
        db1._connection_lock = asyncio.Lock()
        db1._transaction_depth = ContextVar("database_transaction_depth", default=0)
        db1._transaction_owner = None
        db2 = Database.__new__(Database)
        db2._conn = conn2  # type: ignore[reportPrivateUsage]
        db2._path = db_uri  # type: ignore[reportPrivateUsage]
        db2._connection_lock = asyncio.Lock()
        db2._transaction_depth = ContextVar("database_transaction_depth2", default=0)
        db2._transaction_owner = None

        await _run_migrations(db1)

        await db1.execute(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            ("concurrent_test", "ENV"),
        )
        await db1.connection.commit()

        rows = await db2.fetch_all(
            "SELECT * FROM accounts WHERE name = ?",
            ("concurrent_test",),
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "concurrent_test"
    finally:
        await conn1.close()
        await conn2.close()


@pytest.mark.asyncio()
async def test_price_snapshot_insert_and_retrieve() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        account_id = await _seed_account(database)
        await _seed_model(database, "claude-3")

        await database.execute(
            "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
            (account_id, "claude-3"),
        )

        await database.execute(
            "INSERT INTO model_price_snapshots "
            "(model_id, input_price_per_1k, output_price_per_1k) "
            "VALUES (?, ?, ?)",
            ("claude-3", 15.0, 75.0),
        )
        await database.connection.commit()

        row = await database.fetch_one(
            "SELECT * FROM model_price_snapshots WHERE model_id = ?",
            ("claude-3",),
        )
        assert row is not None
        assert row["input_price_per_1k"] == 15.0
        assert row["output_price_per_1k"] == 75.0
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_price_snapshot_immutability() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        account_id = await _seed_account(database)
        await _seed_model(database, "gpt-4")

        await database.execute(
            "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
            (account_id, "gpt-4"),
        )

        await database.execute(
            "INSERT INTO model_price_snapshots "
            "(model_id, input_price_per_1k,"
            " output_price_per_1k, captured_at) "
            "VALUES (?, ?, ?, ?)",
            ("gpt-4", 10.0, 30.0, "2025-01-01T00:00:00"),
        )
        await database.connection.commit()

        await database.execute(
            "INSERT INTO model_price_snapshots "
            "(model_id, input_price_per_1k,"
            " output_price_per_1k, captured_at) "
            "VALUES (?, ?, ?, ?)",
            ("gpt-4", 12.0, 36.0, "2025-06-01T00:00:00"),
        )
        await database.connection.commit()

        latest = await database.fetch_one(
            "SELECT * FROM model_price_snapshots "
            "WHERE model_id = ? "
            "ORDER BY captured_at DESC LIMIT 1",
            ("gpt-4",),
        )
        assert latest is not None
        assert latest["input_price_per_1k"] == 12.0

        all_rows = await database.fetch_all(
            "SELECT * FROM model_price_snapshots WHERE model_id = ?",
            ("gpt-4",),
        )
        assert len(all_rows) == 2
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_price_snapshots_since_filtering() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        account_id = await _seed_account(database)
        await _seed_model(database, "gpt-4")

        await database.execute(
            "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
            (account_id, "gpt-4"),
        )

        await database.execute(
            "INSERT INTO model_price_snapshots "
            "(model_id, input_price_per_1k,"
            " output_price_per_1k, captured_at) "
            "VALUES (?, ?, ?, ?)",
            ("gpt-4", 10.0, 30.0, "2025-01-01T00:00:00"),
        )
        await database.connection.commit()

        await database.execute(
            "INSERT INTO model_price_snapshots "
            "(model_id, input_price_per_1k,"
            " output_price_per_1k, captured_at) "
            "VALUES (?, ?, ?, ?)",
            ("gpt-4", 12.0, 36.0, "2025-06-01T00:00:00"),
        )
        await database.connection.commit()

        rows = await database.fetch_all(
            "SELECT * FROM model_price_snapshots "
            "WHERE model_id = ? AND captured_at >= ?",
            ("gpt-4", "2025-03-01T00:00:00"),
        )
        assert len(rows) == 1
        assert rows[0]["input_price_per_1k"] == 12.0
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_requests_aggregate_query() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        account_id = await _seed_account(database)
        await _seed_model(database)

        for inp, out in [(100, 50), (200, 80), (150, 70)]:
            await database.execute(
                "INSERT INTO requests "
                "(account_id, model_id,"
                " input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?)",
                (account_id, "gpt-4o", inp, out),
            )
        await database.connection.commit()

        row = await database.fetch_one(
            "SELECT COUNT(*) as cnt,"
            " SUM(input_tokens) as total_input "
            "FROM requests WHERE account_id = ?",
            (account_id,),
        )
        assert row is not None
        assert row["cnt"] == 3
        assert row["total_input"] == 450
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_model_upsert() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        await database.execute(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            ("gpt-4o", "GPT-4o"),
        )
        await database.connection.commit()

        await database.execute(
            "INSERT INTO models (model_id, display_name)"
            " VALUES (?, ?) "
            "ON CONFLICT(model_id) DO UPDATE "
            "SET display_name = excluded.display_name",
            ("gpt-4o", "GPT-4o Turbo"),
        )
        await database.connection.commit()

        row = await database.fetch_one(
            "SELECT * FROM models WHERE model_id = ?",
            ("gpt-4o",),
        )
        assert row is not None
        assert row["display_name"] == "GPT-4o Turbo"

        count_row = await database.fetch_one(
            "SELECT COUNT(*) as cnt FROM models WHERE model_id = ?",
            ("gpt-4o",),
        )
        assert count_row is not None
        assert count_row["cnt"] == 1
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_account_model_relationship() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        account_id = await _seed_account(database)
        await _seed_model(database)

        await database.execute(
            "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
            (account_id, "gpt-4o"),
        )
        await database.connection.commit()

        row = await database.fetch_one(
            "SELECT * FROM account_models WHERE account_id = ? AND model_id = ?",
            (account_id, "gpt-4o"),
        )
        assert row is not None
        assert row["account_id"] == account_id
        assert row["model_id"] == "gpt-4o"
        assert row["enabled"] == 1
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_cascade_delete_account_removes_account_models() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        account_id = await _seed_account(database)
        await _seed_model(database)

        await database.execute(
            "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
            (account_id, "gpt-4o"),
        )
        await database.connection.commit()

        before = await database.fetch_one(
            "SELECT COUNT(*) as cnt FROM account_models WHERE account_id = ?",
            (account_id,),
        )
        assert before is not None
        assert before["cnt"] == 1

        await database.execute(
            "DELETE FROM accounts WHERE id = ?",
            (account_id,),
        )
        await database.connection.commit()

        after = await database.fetch_one(
            "SELECT COUNT(*) as cnt FROM account_models WHERE account_id = ?",
            (account_id,),
        )
        assert after is not None
        assert after["cnt"] == 0
    finally:
        await database.disconnect()
