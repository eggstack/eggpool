"""Phase 18 integration tests for the ``Database.vacuum()`` maintenance API.

Verifies the explicit maintenance entry point:

- ``vacuum()`` runs on a writable file-backed database;
- Data remains intact after vacuum;
- The connection is usable after vacuum;
- Read-only databases reject ``vacuum()``;
- ``vacuum()`` inside an active transaction raises;
- A transaction and a ``vacuum()`` are serialized through the
  connection lock;
- The CLI ``db vacuum`` command exits successfully;
- The CLI reports a clean error on failure.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from go_aggregator.cli import cli
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.errors import DatabaseError


@pytest.fixture
def writable_db_path(tmp_path: Path) -> str:
    """Return a path for a fresh file-backed database."""
    return str(tmp_path / "maintenance.sqlite3")


async def _migrate_and_seed(db: Database) -> None:
    """Run migrations and insert representative data for vacuum tests."""
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("vac-acct", "VAC_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )


class TestVacuumSucceeds:
    """``vacuum()`` runs on a writable file-backed database."""

    @pytest.mark.asyncio
    async def test_vacuum_succeeds_on_writable_file(
        self, writable_db_path: str
    ) -> None:
        db = Database(path=writable_db_path)
        await db.connect()
        try:
            await _migrate_and_seed(db)
            await db.vacuum()
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_data_intact_after_vacuum(self, writable_db_path: str) -> None:
        """All rows remain queryable after vacuum."""
        db = Database(path=writable_db_path)
        await db.connect()
        try:
            await _migrate_and_seed(db)

            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                    ("claude-3", "anthropic"),
                )

            await db.vacuum()

            accounts = await db.fetch_all("SELECT name FROM accounts")
            assert [row["name"] for row in accounts] == ["vac-acct"]
            models = await db.fetch_all("SELECT model_id FROM models ORDER BY model_id")
            assert {row["model_id"] for row in models} == {"gpt-4", "claude-3"}
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_connection_usable_after_vacuum(self, writable_db_path: str) -> None:
        """Reads and writes still work after vacuum."""
        db = Database(path=writable_db_path)
        await db.connect()
        try:
            await _migrate_and_seed(db)
            await db.vacuum()

            row = await db.fetch_one("SELECT name FROM accounts")
            assert row is not None
            assert row["name"] == "vac-acct"

            async with db.transaction():
                await db.execute_write(
                    "UPDATE accounts SET weight = 2.0 WHERE name = ?",
                    ("vac-acct",),
                )

            row = await db.fetch_one("SELECT weight FROM accounts")
            assert row is not None
            assert float(row["weight"]) == 2.0
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_vacuum_in_memory_succeeds(self) -> None:
        """VACUUM works on in-memory databases too."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _migrate_and_seed(db)
            await db.vacuum()
        finally:
            await db.disconnect()


class TestVacuumRejected:
    """``vacuum()`` enforces preconditions."""

    @pytest.mark.asyncio
    async def test_read_only_database_rejects_vacuum(self, tmp_path: Path) -> None:
        """VACUUM on a read-only database raises DatabaseError."""
        # Build a normal file DB first, then open it read-only.
        rw_path = str(tmp_path / "readonly.sqlite3")
        writer = Database(path=rw_path)
        await writer.connect()
        try:
            await _migrate_and_seed(writer)
        finally:
            await writer.disconnect()

        ro = Database(path=rw_path, read_only=True)
        await ro.connect()
        try:
            with pytest.raises(DatabaseError, match="read-only"):
                await ro.vacuum()
        finally:
            await ro.disconnect()

    @pytest.mark.asyncio
    async def test_vacuum_inside_transaction_fails(self, writable_db_path: str) -> None:
        """VACUUM cannot run while a transaction is active."""
        db = Database(path=writable_db_path)
        await db.connect()
        try:
            await _migrate_and_seed(db)
            async with db.transaction():
                with pytest.raises(DatabaseError, match="transaction"):
                    await db.vacuum()
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_vacuum_fails_with_nested_transaction_owner(
        self,
        writable_db_path: str,
    ) -> None:
        """VACUUM also rejects being called from a nested transaction context."""
        db = Database(path=writable_db_path)
        await db.connect()
        try:
            await _migrate_and_seed(db)

            async def _inner() -> None:
                with pytest.raises(DatabaseError, match="transaction"):
                    await db.vacuum()

            async with db.transaction():
                await _inner()
        finally:
            await db.disconnect()


class TestVacuumConcurrency:
    """VACUUM and transactions are serialized through the connection lock."""

    @pytest.mark.asyncio
    async def test_vacuum_waits_for_transaction(
        self,
        writable_db_path: str,
    ) -> None:
        """A transaction in progress blocks vacuum until it commits."""
        db = Database(path=writable_db_path)
        await db.connect()
        try:
            await _migrate_and_seed(db)
            order: list[str] = []
            tx_started = asyncio.Event()
            tx_can_commit = asyncio.Event()

            async def writer() -> None:
                async with db.transaction():
                    tx_started.set()
                    order.append("tx-open")
                    await tx_can_commit.wait()
                    order.append("tx-close")

            async def vacuum_runner() -> None:
                await tx_started.wait()
                order.append("vacuum-call")
                await db.vacuum()
                order.append("vacuum-done")

            writer_task = asyncio.create_task(writer())
            vacuum_task = asyncio.create_task(vacuum_runner())

            await tx_started.wait()
            # Give the vacuum task a chance to start and block on the lock.
            await asyncio.sleep(0.05)
            tx_can_commit.set()

            await asyncio.gather(writer_task, vacuum_task)

            # Vacuum cannot start its work until the transaction closes.
            assert order == [
                "tx-open",
                "vacuum-call",
                "tx-close",
                "vacuum-done",
            ]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_transaction_waits_for_vacuum(
        self,
        writable_db_path: str,
    ) -> None:
        """A transaction cannot interleave with an in-progress vacuum."""
        db = Database(path=writable_db_path)
        await db.connect()
        try:
            await _migrate_and_seed(db)

            order: list[str] = []
            vacuum_started = asyncio.Event()
            vacuum_can_finish = asyncio.Event()

            original_execute = db.connection.execute

            async def execute_wrapper(sql, *args, **kwargs):  # type: ignore[no-untyped-def]
                if "VACUUM" in str(sql).upper() and not vacuum_started.is_set():
                    vacuum_started.set()
                    await vacuum_can_finish.wait()
                return await original_execute(sql, *args, **kwargs)

            try:
                db.connection.execute = execute_wrapper  # type: ignore[method-assign]
                vacuum_task = asyncio.create_task(db.vacuum())
                await vacuum_started.wait()
                order.append("vacuum-started")

                async def writer() -> None:
                    async with db.transaction():
                        order.append("tx-open")
                        await db.execute_write(
                            "UPDATE accounts SET weight = 3.0 WHERE name = ?",
                            ("vac-acct",),
                        )

                writer_task = asyncio.create_task(writer())
                # Give the writer a chance to start. It must NOT enter
                # the transaction while vacuum holds the lock.
                await asyncio.sleep(0.05)
                assert "tx-open" not in order

                vacuum_can_finish.set()
                await vacuum_task
                order.append("vacuum-done")

                await writer_task
                order.append("tx-done")
            finally:
                db.connection.execute = original_execute  # type: ignore[method-assign]
        finally:
            await db.disconnect()


class TestDbVacuumCli:
    """The ``db vacuum`` CLI command uses the helper."""

    def _write_config(self, tmp_path: Path) -> Path:
        config_path = tmp_path / "config.toml"
        db_path = str(tmp_path / "cli_vacuum.sqlite3")
        config_path.write_text(
            f"""
[server]
api_key_env = ""

[database]
path = "{db_path}"
wal = true
synchronous = "NORMAL"

[models]
refresh_interval_s = 0
startup_refresh = false

[dashboard]
enabled = false
""",
            encoding="utf-8",
        )
        return config_path

    def test_cli_vacuum_succeeds(self, tmp_path: Path) -> None:
        config_path = self._write_config(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "db", "vacuum"],
        )
        assert result.exit_code == 0, (
            f"stdout={result.stdout} stderr={getattr(result, 'stderr', '')}"
        )
        assert "vacuum completed" in result.stdout.lower()

    def test_cli_vacuum_reports_failure_cleanly(
        self,
        tmp_path: Path,
    ) -> None:
        """A missing or unconnectable database produces a clean error."""
        # Point the config at a path that cannot be created.
        config_path = tmp_path / "config_bad.toml"
        bad_path = str(tmp_path / "no_such_dir" / "broken.sqlite3")
        config_path.write_text(
            f"""
[server]
api_key_env = ""

[database]
path = "{bad_path}"
wal = true
synchronous = "NORMAL"

[models]
refresh_interval_s = 0
startup_refresh = false

[dashboard]
enabled = false
""",
            encoding="utf-8",
        )

        # Pre-create the file as a directory so SQLite cannot open it.
        os.makedirs(bad_path, exist_ok=True)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "db", "vacuum"],
        )
        assert result.exit_code != 0
        # The CLI should print a clean error to stderr, not a traceback.
        combined = (result.stdout or "") + (getattr(result, "stderr", "") or "")
        assert "Error:" in combined
        assert "Traceback" not in combined


class TestVacuumAudit:
    """Verify that no production code uses ``.connection.execute`` for
    ``VACUUM`` outside the dedicated helper."""

    def test_cli_does_not_use_connection_execute_for_vacuum(
        self,
        tmp_path: Path,
    ) -> None:
        """The CLI must call ``db.vacuum()`` rather than direct connection use."""
        cli_path = (
            Path(__file__).parent.parent.parent / "src" / "go_aggregator" / "cli.py"
        )
        contents = cli_path.read_text(encoding="utf-8")
        # No raw VACUUM SQL string in cli.py - it must go through the
        # helper. We allow the bare keyword in docstrings/comments but
        # forbid the SQL statement "VACUUM" inside a string literal or
        # as a statement.
        forbidden_patterns = [
            'execute("VACUUM")',
            "execute('VACUUM')",
            "await db.connection.execute",
            "db.connection.execute",
        ]
        for pattern in forbidden_patterns:
            assert pattern not in contents, (
                f"cli.py must not use {pattern!r} for VACUUM; use db.vacuum() instead."
            )
        # And it must call db.vacuum() in the db vacuum command.
        assert "db.vacuum()" in contents
