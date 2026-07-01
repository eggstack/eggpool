"""Tests for database operations."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import ContextVar

import aiosqlite
import pytest

from eggpool.catalog.pricing import PriceRepository
from eggpool.constants import SQLITE_INTEGER_MAX
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import PriceSnapshotRepository
from eggpool.errors import DatabaseError


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_account(db: Database, name: str = "test_account") -> int:
    async with db.transaction():
        last_id = await db.execute_insert(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, ?, ?)",
            (name, "TEST_KEY", 1, 1.0),
        )
    return int(last_id)


async def _seed_model(db: Database, model_id: str = "gpt-4o") -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, "GPT-4o"),
        )


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

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO accounts "
                "(name, api_key_env, enabled, weight) "
                "VALUES (?, ?, ?, ?)",
                ("acct1", "ENV1", 1, 2.5),
            )

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

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO requests "
                "(account_id, model_id, input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?)",
                (account_id, "gpt-4o", 100, 50),
            )

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
            async with database.transaction():
                await database.execute_write(
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
        db1._read_only = False  # type: ignore[reportPrivateUsage]
        db1._connection_lock = asyncio.Lock()
        db1._transaction_depth = ContextVar("database_transaction_depth", default=0)
        db1._transaction_owner = ContextVar("database_transaction_owner", default=None)
        db1._in_transaction_context = ContextVar(
            "database_in_transaction_context1", default=False
        )
        db1._total_nested_transactions = 0  # type: ignore[reportPrivateUsage]
        db1._write_ops = 0  # type: ignore[reportPrivateUsage]
        db1._read_ops = 0  # type: ignore[reportPrivateUsage]
        db1._total_transactions = 0  # type: ignore[reportPrivateUsage]
        db1._last_operation_error_class = None  # type: ignore[reportPrivateUsage]
        db1._cumulative_lock_wait_s = 0.0  # type: ignore[reportPrivateUsage]
        db1._max_lock_wait_s = 0.0  # type: ignore[reportPrivateUsage]
        db2 = Database.__new__(Database)
        db2._conn = conn2  # type: ignore[reportPrivateUsage]
        db2._path = db_uri  # type: ignore[reportPrivateUsage]
        db2._read_only = False  # type: ignore[reportPrivateUsage]
        db2._connection_lock = asyncio.Lock()
        db2._transaction_depth = ContextVar("database_transaction_depth2", default=0)
        db2._transaction_owner = ContextVar("database_transaction_owner2", default=None)
        db2._in_transaction_context = ContextVar(
            "database_in_transaction_context2", default=False
        )
        db2._total_nested_transactions = 0  # type: ignore[reportPrivateUsage]
        db2._write_ops = 0  # type: ignore[reportPrivateUsage]
        db2._read_ops = 0  # type: ignore[reportPrivateUsage]
        db2._total_transactions = 0  # type: ignore[reportPrivateUsage]
        db2._last_operation_error_class = None  # type: ignore[reportPrivateUsage]
        db2._cumulative_lock_wait_s = 0.0  # type: ignore[reportPrivateUsage]
        db2._max_lock_wait_s = 0.0  # type: ignore[reportPrivateUsage]

        await _run_migrations(db1)

        async with db1.transaction():
            await db1.execute_write(
                "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                ("concurrent_test", "ENV"),
            )

        rows = await db2.fetch_all(
            "SELECT * FROM accounts WHERE name = ?",
            ("concurrent_test",),
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "concurrent_test"
    finally:
        await conn1.close()
        await conn2.close()


def test_idle_connection_lock_rebinds_across_event_loops(tmp_path: object) -> None:
    """A contended DB lock from one loop must not poison later app reuse."""
    import pathlib

    db_path = str(pathlib.Path(str(tmp_path)) / "loop_reuse.sqlite3")
    database = Database(path=db_path)

    async def bind_lock_to_first_loop() -> None:
        await database.connect()
        async with database.transaction():
            waiter = asyncio.create_task(database.fetch_one("SELECT 1"))
            await asyncio.sleep(0)
            assert not waiter.done()
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter

    async def reuse_from_second_loop() -> None:
        first, second = await asyncio.gather(
            database.fetch_one("SELECT 1 AS value"),
            database.fetch_one("SELECT 2 AS value"),
        )
        assert first is not None
        assert second is not None
        assert first["value"] == 1
        assert second["value"] == 2
        await database.disconnect()

    asyncio.run(bind_lock_to_first_loop())
    asyncio.run(reuse_from_second_loop())


@pytest.mark.asyncio()
async def test_price_snapshot_insert_and_retrieve() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        account_id = await _seed_account(database)
        await _seed_model(database, "claude-3")

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
                (account_id, "claude-3"),
            )

            await database.execute_write(
                "INSERT INTO model_price_snapshots "
                "(model_id, input_price_per_1k, output_price_per_1k) "
                "VALUES (?, ?, ?)",
                ("claude-3", 15.0, 75.0),
            )

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
async def test_price_snapshot_record_from_dict_normalizes_price_strings() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        await _seed_model(database, "gpt-4")

        repo = PriceSnapshotRepository(database)
        async with database.transaction():
            await repo.record_from_dict(
                "gpt-4",
                {
                    "input_price_per_1k": "$3 / 1M",
                    "output_price_per_1k": " $15/1M ",
                    "cache_read_per_million_microdollars": "$0.30 / 1M",
                    "cache_write_per_million_microdollars": "750_000",
                    "source": "upstream",
                },
            )

        row = await database.fetch_one(
            "SELECT * FROM model_price_snapshots WHERE model_id = ?",
            ("gpt-4",),
        )

        assert row is not None
        assert row["input_price_per_1k"] == pytest.approx(0.003)
        assert row["output_price_per_1k"] == pytest.approx(0.015)
        assert row["input_per_million_microdollars"] == 3_000_000
        assert row["output_per_million_microdollars"] == 15_000_000
        assert row["cache_read_per_million_microdollars"] == 300_000
        assert row["cache_write_per_million_microdollars"] == 750_000
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_price_snapshot_record_clamps_oversized_legacy_float_rates() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        await _seed_model(database, "bad-price-model")

        repo = PriceSnapshotRepository(database)
        async with database.transaction():
            await repo.record(
                "bad-price-model",
                input_price_per_1k=1e20,
                output_price_per_1k=1e20,
            )

        row = await database.fetch_one(
            "SELECT input_per_million_microdollars, "
            "output_per_million_microdollars "
            "FROM model_price_snapshots WHERE model_id = ?",
            ("bad-price-model",),
        )

        assert row is not None
        assert row["input_per_million_microdollars"] == SQLITE_INTEGER_MAX
        assert row["output_per_million_microdollars"] == SQLITE_INTEGER_MAX
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

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
                (account_id, "gpt-4"),
            )

            await database.execute_write(
                "INSERT INTO model_price_snapshots "
                "(model_id, input_price_per_1k,"
                " output_price_per_1k, captured_at) "
                "VALUES (?, ?, ?, ?)",
                ("gpt-4", 10.0, 30.0, "2025-01-01T00:00:00"),
            )

            await database.execute_write(
                "INSERT INTO model_price_snapshots "
                "(model_id, input_price_per_1k,"
                " output_price_per_1k, captured_at) "
                "VALUES (?, ?, ?, ?)",
                ("gpt-4", 12.0, 36.0, "2025-06-01T00:00:00"),
            )

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

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
                (account_id, "gpt-4"),
            )

            await database.execute_write(
                "INSERT INTO model_price_snapshots "
                "(model_id, input_price_per_1k,"
                " output_price_per_1k, captured_at) "
                "VALUES (?, ?, ?, ?)",
                ("gpt-4", 10.0, 30.0, "2025-01-01T00:00:00"),
            )

            await database.execute_write(
                "INSERT INTO model_price_snapshots "
                "(model_id, input_price_per_1k,"
                " output_price_per_1k, captured_at) "
                "VALUES (?, ?, ?, ?)",
                ("gpt-4", 12.0, 36.0, "2025-06-01T00:00:00"),
            )

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
async def test_price_repository_isolates_provider_snapshots() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        await _seed_model(database, "shared-model")
        repo = PriceRepository(database)

        async with database.transaction():
            await repo.record_snapshot(
                "shared-model", 0.001, 0.002, provider_id="provider-a"
            )
            await repo.record_snapshot(
                "shared-model", 0.009, 0.018, provider_id="provider-b"
            )

        provider_a = await repo.get_latest_snapshot(
            "shared-model", provider_id="provider-a"
        )
        provider_b = await repo.get_latest_snapshot(
            "shared-model", provider_id="provider-b"
        )

        assert provider_a is not None
        assert provider_a.input_price_per_1k == 0.001
        assert provider_b is not None
        assert provider_b.input_price_per_1k == 0.009
        assert (
            await repo.get_latest_snapshot(
                "shared-model", provider_id="provider-without-pricing"
            )
            is None
        )
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_price_history_preserves_and_filters_provider() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        await _seed_model(database, "shared-model")
        repo = PriceRepository(database)

        async with database.transaction():
            await repo.record_snapshot(
                "shared-model", 0.001, 0.002, provider_id="provider-a"
            )
            await repo.record_snapshot(
                "shared-model", 0.009, 0.018, provider_id="provider-b"
            )

        all_snapshots = await repo.get_snapshots_since("shared-model")
        provider_a = await repo.get_snapshots_since(
            "shared-model", provider_id="provider-a"
        )
        snapshot_repo = PriceSnapshotRepository(database)
        latest_a = await snapshot_repo.get_latest("shared-model", "provider-a")
        latest_missing = await snapshot_repo.get_latest("shared-model", "missing")

        assert {snapshot.provider_id for snapshot in all_snapshots} == {
            "provider-a",
            "provider-b",
        }
        assert len(provider_a) == 1
        assert provider_a[0].provider_id == "provider-a"
        assert latest_a is not None
        assert latest_a["input_price_per_1k"] == 0.001
        assert latest_missing is None
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

        async with database.transaction():
            for inp, out in [(100, 50), (200, 80), (150, 70)]:
                await database.execute_write(
                    "INSERT INTO requests "
                    "(account_id, model_id,"
                    " input_tokens, output_tokens) "
                    "VALUES (?, ?, ?, ?)",
                    (account_id, "gpt-4o", inp, out),
                )

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

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
                ("gpt-4o", "GPT-4o"),
            )

            await database.execute_write(
                "INSERT INTO models (model_id, display_name)"
                " VALUES (?, ?) "
                "ON CONFLICT(model_id) DO UPDATE "
                "SET display_name = excluded.display_name",
                ("gpt-4o", "GPT-4o Turbo"),
            )

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

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
                (account_id, "gpt-4o"),
            )

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

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO account_models (account_id, model_id) VALUES (?, ?)",
                (account_id, "gpt-4o"),
            )

        before = await database.fetch_one(
            "SELECT COUNT(*) as cnt FROM account_models WHERE account_id = ?",
            (account_id,),
        )
        assert before is not None
        assert before["cnt"] == 1

        async with database.transaction():
            await database.execute_write(
                "DELETE FROM accounts WHERE id = ?",
                (account_id,),
            )

        after = await database.fetch_one(
            "SELECT COUNT(*) as cnt FROM account_models WHERE account_id = ?",
            (account_id,),
        )
        assert after is not None
        assert after["cnt"] == 0
    finally:
        await database.disconnect()


class TestTransactionNestingAcrossTaskBoundaries:
    """Regression tests for `cannot start a transaction within a transaction`.

    Prior to the SQLite-truth nesting fix, ``db.transaction()`` used
    ``asyncio.current_task()`` identity to detect nesting. That
    failed across ``asyncio.shield()`` and ``asyncio.create_task()``
    boundaries: a shielded child entering ``db.transaction()`` while
    the parent already held ``BEGIN IMMEDIATE`` would fall through
    to acquire ``_connection_lock`` and issue a second ``BEGIN``,
    either deadlocking (parent awaits child, child awaits lock) or
    raising ``OperationalError: cannot start a transaction within
    a transaction``.

    These tests pin the fixed behavior: nesting is detected via
    SQLite's per-connection ``in_transaction`` state, so shielded
    and child tasks piggyback on the outer transaction without
    re-entering the lock or re-issuing ``BEGIN``.
    """

    @pytest.mark.asyncio
    async def test_shielded_task_inside_active_parent_tx_does_not_deadlock(
        self,
    ) -> None:
        """``asyncio.shield()`` while parent holds a transaction must
        complete without deadlocking on ``_connection_lock`` and without
        raising ``cannot start a transaction within a transaction``.
        """
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            async with db.transaction():
                # Force a write inside the parent so BEGIN IMMEDIATE is
                # actually open (not an idle transaction).
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("parent", "PARENT_KEY"),
                )

                async def shielded_child() -> None:
                    async with db.transaction():
                        await db.execute_write(
                            "INSERT INTO accounts (name, api_key_env, "
                            "enabled, weight) VALUES (?, ?, 1, 1.0)",
                            ("shielded-child", "CHILD_KEY"),
                        )

                # Regression: this used to deadlock. The shield means
                # cancellation of the outer await cannot kill the child;
                # the child used to block forever waiting for
                # ``_connection_lock`` (held by the parent) while the
                # parent awaited ``wait_for``.
                await asyncio.wait_for(
                    asyncio.shield(shielded_child()),
                    timeout=5.0,
                )

            rows = await db.fetch_all("SELECT name FROM accounts ORDER BY id")
            assert [r["name"] for r in rows] == ["parent", "shielded-child"]
            assert db.contention_snapshot()["total_nested_transactions"] >= 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_child_task_via_create_task_can_write_inside_parent_tx(
        self,
    ) -> None:
        """``asyncio.create_task`` spawned inside a parent transaction
        can open its own ``db.transaction()`` and write without raising.
        """
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("parent", "PARENT_KEY"),
                )

                async def child_writer() -> None:
                    async with db.transaction():
                        await db.execute_write(
                            "INSERT INTO accounts (name, api_key_env, "
                            "enabled, weight) VALUES (?, ?, 1, 1.0)",
                            ("create-task-child", "CHILD_KEY"),
                        )

                child = asyncio.create_task(child_writer())
                await child

            rows = await db.fetch_all("SELECT name FROM accounts ORDER BY id")
            assert [r["name"] for r in rows] == [
                "parent",
                "create-task-child",
            ]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_exception_in_shielded_child_rolls_back_outer(self) -> None:
        """An exception raised inside a shielded child transaction must
        propagate and roll back the entire outer transaction, including
        any sibling writes the shielded child performed.
        """
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:

            class ChildBoomError(Exception):
                pass

            async def shielded_boomer() -> None:
                async with db.transaction():
                    await db.execute_write(
                        "INSERT INTO accounts (name, api_key_env, "
                        "enabled, weight) VALUES (?, ?, 1, 1.0)",
                        ("will-rollback", "ROLLBACK_KEY"),
                    )
                    raise ChildBoomError

            outer_caught: list[BaseException] = []
            with suppress(ChildBoomError):
                async with db.transaction():
                    await db.execute_write(
                        "INSERT INTO accounts (name, api_key_env, "
                        "enabled, weight) VALUES (?, ?, 1, 1.0)",
                        ("parent-stays", "PARENT_KEY"),
                    )
                    await asyncio.wait_for(
                        asyncio.shield(shielded_boomer()),
                        timeout=5.0,
                    )
            outer_caught.append(ChildBoomError())

            rows = await db.fetch_all("SELECT name FROM accounts")
            assert rows == []
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_nested_in_same_task_piggybacks(self) -> None:
        """In-process nested ``async with db.transaction():`` in the
        same task must piggyback on the outer transaction's commit
        boundary; only the outer issues COMMIT.
        """
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("outer", "OUTER_KEY"),
                )
                async with db.transaction():
                    await db.execute_write(
                        "INSERT INTO accounts (name, api_key_env, "
                        "enabled, weight) VALUES (?, ?, 1, 1.0)",
                        ("inner", "INNER_KEY"),
                    )

            rows = await db.fetch_all("SELECT name FROM accounts ORDER BY id")
            assert [r["name"] for r in rows] == ["outer", "inner"]
            # Migrations run during connect, so we can't assert on absolute
            # totals — only that the nested call was observed at least once.
            assert db.contention_snapshot()["total_nested_transactions"] >= 1
        finally:
            await db.disconnect()
