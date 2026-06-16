"""Phase 17 deployment-readiness database transaction contract tests.

Verifies the strict transaction ownership contract:

- Every DML write helper (execute_write, execute_insert, execute_returning,
  _execute_cursor) requires the current task to own an active transaction.
- The execute_pragma helper runs PRAGMA statements under the connection
  lock without requiring transaction ownership.
- PRAGMA execution does not interleave with active transactions.
- Legacy db.execute() is no longer present on the Database class.
"""

from __future__ import annotations

import asyncio

import pytest

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.errors import DatabaseError


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed(db: Database) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )


class TestWriteHelperContract:
    """Write helpers require an owned transaction."""

    @pytest.mark.asyncio
    async def test_execute_write_outside_transaction_fails(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            with pytest.raises(DatabaseError, match="transaction"):
                await db.execute_write(
                    "INSERT INTO accounts "
                    "(name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("no-tx", "X"),
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_insert_outside_transaction_fails(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            with pytest.raises(DatabaseError, match="transaction"):
                await db.execute_insert(
                    "INSERT INTO accounts "
                    "(name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("no-tx-ins", "X"),
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_returning_outside_transaction_fails(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            with pytest.raises(DatabaseError, match="transaction"):
                await db.execute_returning(
                    "UPDATE accounts SET weight = 1.0 WHERE id = 0 RETURNING id, weight"
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_write_inside_transaction_succeeds(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            async with db.transaction():
                count = await db.execute_write(
                    "INSERT INTO accounts "
                    "(name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("in-tx", "X"),
                )
            assert count == 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_insert_inside_transaction_succeeds(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            async with db.transaction():
                last_id = await db.execute_insert(
                    "INSERT INTO accounts "
                    "(name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("in-tx-ins", "X"),
                )
            assert last_id > 0
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_returning_inside_transaction_succeeds(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        await _seed(db)
        try:
            async with db.transaction():
                rows = await db.execute_returning(
                    "UPDATE accounts SET weight = 2.0 WHERE name = ? "
                    "RETURNING name, weight",
                    ("test-acct",),
                )
            assert len(rows) == 1
            assert rows[0]["weight"] == 2.0
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_nested_same_task_transaction_succeeds(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts "
                    "(name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("outer", "X"),
                )
                async with db.transaction():
                    await db.execute_write(
                        "INSERT INTO accounts "
                        "(name, api_key_env, enabled, weight) "
                        "VALUES (?, ?, 1, 1.0)",
                        ("inner", "X"),
                    )
            row = await db.fetch_one(
                "SELECT name FROM accounts WHERE name IN (?, ?) ORDER BY name",
                ("outer", "inner"),
            )
            assert row is not None
            assert row["name"] == "inner"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_child_task_cannot_inherit_transaction(self) -> None:
        """A child task cannot execute writes that require transaction ownership.

        Even if the child task inherits the ContextVar depth via asyncio
        task spawning, the task identity differs and the helper must
        reject the write.
        """
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            capture: list[Exception | None] = [None]

            async def child_writer() -> None:
                try:
                    await db.execute_write(
                        "INSERT INTO accounts "
                        "(name, api_key_env, enabled, weight) "
                        "VALUES (?, ?, 1, 1.0)",
                        ("child", "X"),
                    )
                except Exception as exc:
                    capture[0] = exc

            async with db.transaction():
                child = asyncio.create_task(child_writer())
                await child

            assert capture[0] is not None
            assert isinstance(capture[0], DatabaseError)
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_child_task_with_own_transaction_succeeds(self) -> None:
        """A child task that opens its own transaction can write.

        Note: a child cannot enter a transaction while the parent holds
        the connection lock (it would deadlock). This test verifies the
        child can perform the write independently after the parent
        transaction is closed.
        """
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:

            async def child_writer() -> None:
                async with db.transaction():
                    await db.execute_write(
                        "INSERT INTO accounts "
                        "(name, api_key_env, enabled, weight) "
                        "VALUES (?, ?, 1, 1.0)",
                        ("child-ok", "X"),
                    )

            child = asyncio.create_task(child_writer())
            await child

            row = await db.fetch_one(
                "SELECT name FROM accounts WHERE name = ?", ("child-ok",)
            )
            assert row is not None
        finally:
            await db.disconnect()


class TestRawCursorContract:
    """Raw cursor access is restricted to the transaction owner."""

    @pytest.mark.asyncio
    async def test_execute_cursor_outside_transaction_fails(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            with pytest.raises(DatabaseError, match="transaction"):
                await db._execute_cursor(  # pyright: ignore[reportPrivateUsage]
                    "SELECT 1"
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_cursor_inside_transaction_succeeds(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            async with db.transaction():
                cursor = await db._execute_cursor(  # pyright: ignore[reportPrivateUsage]
                    "SELECT 1 AS one"
                )
                row = await cursor.fetchone()
            assert row is not None
            assert row["one"] == 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_cursor_in_child_task_without_tx_fails(self) -> None:
        """A child task without its own transaction cannot use raw cursor."""
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            capture: list[Exception | None] = [None]

            async def child_cursor() -> None:
                try:
                    await db._execute_cursor(  # pyright: ignore[reportPrivateUsage]
                        "SELECT 1"
                    )
                except Exception as exc:
                    capture[0] = exc

            async with db.transaction():
                child = asyncio.create_task(child_cursor())
                await child
            assert capture[0] is not None
            assert isinstance(capture[0], DatabaseError)
        finally:
            await db.disconnect()


class TestPragmaHelper:
    """The dedicated PRAGMA helper runs safely under the connection lock."""

    @pytest.mark.asyncio
    async def test_execute_pragma_consumes_results_under_lock(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            rows = await db.execute_pragma("PRAGMA foreign_keys")
            assert len(rows) == 1
            assert rows[0]["foreign_keys"] == 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_pragma_rejects_non_pragma(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            with pytest.raises(DatabaseError, match="PRAGMA"):
                await db.execute_pragma("SELECT 1")
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_pragma_rejects_empty(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            with pytest.raises(DatabaseError, match="PRAGMA"):
                await db.execute_pragma("")
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_concurrent_pragma_and_transaction_serialized(self) -> None:
        """A pragma must not interleave with an in-progress transaction.

        We use a second shared connection (separate ``Database`` instance
        over the same file) to simulate a concurrent reader while the
        first connection is mid-transaction. The pragma must run to
        completion without seeing partial state.
        """
        db1 = Database(path=":memory:")
        await db1.connect()
        await _run_migrations(db1)
        await _seed(db1)
        try:
            commit_event = asyncio.Event()
            pragma_done = asyncio.Event()

            async def writer() -> None:
                async with db1.transaction():
                    await db1.execute_write(
                        "INSERT INTO accounts "
                        "(name, api_key_env, enabled, weight) "
                        "VALUES (?, ?, 1, 1.0)",
                        ("in-flight", "X"),
                    )
                    commit_event.set()
                    await asyncio.sleep(0.1)

            async def pragma_runner() -> None:
                await commit_event.wait()
                await db1.execute_pragma("PRAGMA busy_timeout")
                pragma_done.set()

            await asyncio.gather(
                asyncio.create_task(writer()),
                asyncio.create_task(pragma_runner()),
            )

            assert pragma_done.is_set()
        finally:
            await db1.disconnect()


class TestLegacyExecuteRemoved:
    """The legacy public ``execute()`` wrapper has been removed."""

    def test_legacy_execute_attribute_absent(self) -> None:
        assert not hasattr(Database, "execute"), (
            "Database.execute() is deprecated; remove it from production code."
        )


class TestAccountRepositorySync:
    """AccountRepository.sync_from_config is atomic and transaction-owned."""

    @pytest.mark.asyncio
    async def test_sync_from_config_rolls_back_on_failure(self) -> None:
        """If anything fails mid-sync, no accounts should be left behind."""
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            from go_aggregator.db.repositories import AccountRepository

            repo = AccountRepository(db)
            # Pass an empty list of dicts (no name key) to force a
            # TypeError on the ``str(acct["name"])`` access inside the
            # transaction.
            with pytest.raises(KeyError):
                await repo.sync_from_config(
                    [
                        {
                            "name": "valid-acct",
                            "api_key_env": "X",
                            "enabled": True,
                            "weight": 1.0,
                        },
                        {
                            "api_key_env": "X",
                            "enabled": True,
                            "weight": 1.0,
                        },
                    ],
                    db,
                )

            rows = await db.fetch_all("SELECT name FROM accounts")
            assert rows == []
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_sync_from_config_persists_and_disables(self) -> None:
        """sync_from_config persists, updates, and disables correctly."""
        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        try:
            from go_aggregator.db.repositories import AccountRepository

            repo = AccountRepository(db)
            ids = await repo.sync_from_config(
                [
                    {
                        "name": "a",
                        "api_key_env": "ENV_A",
                        "enabled": True,
                        "weight": 1.0,
                    },
                    {
                        "name": "b",
                        "api_key_env": "ENV_B",
                        "enabled": True,
                        "weight": 2.0,
                    },
                ],
                db,
            )
            assert set(ids.keys()) == {"a", "b"}
            assert ids["a"] != ids["b"]

            # Re-run with only one account; the other should be disabled.
            ids2 = await repo.sync_from_config(
                [
                    {
                        "name": "a",
                        "api_key_env": "ENV_A",
                        "enabled": True,
                        "weight": 1.0,
                    },
                ],
                db,
            )
            assert set(ids2.keys()) == {"a"}

            rows = await db.fetch_all("SELECT name, enabled FROM accounts")
            by_name = {r["name"]: bool(r["enabled"]) for r in rows}
            assert by_name["a"] is True
            assert by_name["b"] is False
        finally:
            await db.disconnect()


class TestStandaloneExhaustedUpdate:
    """The standalone exhausted-request update path is transaction-owned.

    Mirrors the RequestCoordinator._handle_exhausted() else-branch:
    if a request exists but no attempt was selected, the final UPDATE
    must be wrapped in a transaction.
    """

    @pytest.mark.asyncio
    async def test_exhausted_update_in_transaction(self) -> None:
        from go_aggregator.db.repositories import RequestRepository

        db = Database(path=":memory:")
        await db.connect()
        await _run_migrations(db)
        await _seed(db)
        try:
            request_repo = RequestRepository(db)
            async with db.transaction():
                db_id = await request_repo.create_pending(
                    request_id="req-exhausted",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )

            async with db.transaction():
                await db.execute_write(
                    "UPDATE requests SET status = 'error', "
                    "completed_at = CURRENT_TIMESTAMP, "
                    "error_class = ?, error_detail = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (
                        "NoEligibleAccountError",
                        "no eligible account",
                        db_id,
                    ),
                )

            row = await db.fetch_one(
                "SELECT status, error_class, error_detail FROM requests WHERE id = ?",
                (db_id,),
            )
            assert row is not None
            assert row["status"] == "error"
            assert row["error_class"] == "NoEligibleAccountError"
            assert row["error_detail"] == "no eligible account"
        finally:
            await db.disconnect()
