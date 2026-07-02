"""Tests for the read-only database invariant checker."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from scripts import check_database

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    """Create a temporary SQLite database with the schema applied.

    Uses the same migration runner as the application so the
    schema is real, not a hand-rolled subset.
    """
    from eggpool.db.connection import Database
    from eggpool.db.migrations import MigrationRunner

    async def _setup() -> None:
        db = Database(path=str(tmp_path / "usage.sqlite3"))
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await db.disconnect()

    asyncio.run(_setup())
    return tmp_path / "usage.sqlite3"


def _run_main_with_path(db_path: Path) -> int:
    """Run check_database.main with GOROUTER_DB_PATH set, restoring the env."""
    old = os.environ.get("GOROUTER_DB_PATH")
    os.environ["GOROUTER_DB_PATH"] = str(db_path)
    try:
        return asyncio.run(check_database.main())
    finally:
        if old is None:
            os.environ.pop("GOROUTER_DB_PATH", None)
        else:
            os.environ["GOROUTER_DB_PATH"] = old


class TestExitCodes:
    def test_missing_db_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        missing = tmp_path / "nope.sqlite3"
        code = _run_main_with_path(missing)
        assert code == 2
        captured = capsys.readouterr()
        assert "Database not found" in captured.err

    def test_healthy_db_returns_0(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        code = _run_main_with_path(fresh_db)
        assert code == 0
        captured = capsys.readouterr()
        assert "Database invariants OK" in captured.out


class TestSchemaVersionMismatch:
    def test_older_schema_returns_2(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        """Roll the ``_migrations`` version back on a real production
        schema so the only failure is the version mismatch.
        """
        conn = sqlite3.connect(str(fresh_db))
        try:
            # Wipe all _migrations rows then insert a single v1 row
            # to simulate an older-schema database. The production
            # tables stay in place, so the preflight only fails on
            # the version check.
            conn.execute("DELETE FROM _migrations")
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) "
                "VALUES (1, '0001_initial', CURRENT_TIMESTAMP)"
            )
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(fresh_db)
        assert code == 2
        captured = capsys.readouterr()
        assert "older than this checker expects" in captured.err

    def test_newer_schema_returns_2(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute("DELETE FROM _migrations")
            future_version = check_database.EXPECTED_SCHEMA_VERSION + 5
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) "
                "VALUES (?, '0099_future', CURRENT_TIMESTAMP)",
                (future_version,),
            )
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(fresh_db)
        assert code == 2
        captured = capsys.readouterr()
        assert "newer than this checker expects" in captured.err


class TestReadOnly:
    def test_checker_leaves_database_unchanged(
        self, fresh_db: Path, tmp_path: Path
    ) -> None:
        """The checker must not modify the database file contents.

        SQLite's WAL/SHM files are auxiliary and may be created or
        updated by SQLite itself even in read-only mode, so we
        compare the main database file's contents instead.
        """
        target = tmp_path / "checker_target.sqlite3"
        target.write_bytes(fresh_db.read_bytes())

        before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        code = _run_main_with_path(target)
        after_hash = hashlib.sha256(target.read_bytes()).hexdigest()

        assert code == 0
        assert before_hash == after_hash, "Checker modified the database file contents"

    def test_checker_refuses_writes_on_read_only(self, fresh_db: Path) -> None:
        from eggpool.db.connection import Database
        from eggpool.errors import DatabaseError

        async def _exercise() -> None:
            db = Database(path=str(fresh_db), read_only=True)
            await db.connect()
            try:
                with pytest.raises(DatabaseError, match="read-only"):
                    async with db.transaction():
                        await db.execute_write(
                            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                            ("writable-test", "ENV"),
                        )
            finally:
                await db.disconnect()

        asyncio.run(_exercise())


class TestFailClosed:
    """Section 1: fail-closed behavior for the invariant checker."""

    def test_missing_database_file_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        missing = tmp_path / "absent.sqlite3"
        code = _run_main_with_path(missing)
        assert code == 2
        captured = capsys.readouterr()
        assert "Database not found" in captured.err

    def test_existing_empty_sqlite_file_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        empty = tmp_path / "empty.sqlite3"
        empty.write_bytes(b"")
        code = _run_main_with_path(empty)
        assert code == 2
        captured = capsys.readouterr()
        # A zero-byte file opens as a valid empty SQLite database;
        # the preflight must report the missing tables.
        assert "missing required tables" in captured.err

    def test_database_without_migrations_table_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        no_mig = tmp_path / "no_migrations.sqlite3"
        conn = sqlite3.connect(str(no_mig))
        try:
            conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(no_mig)
        assert code == 2
        captured = capsys.readouterr()
        assert "missing required tables" in captured.err

    def test_migrations_but_no_required_tables_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        partial = tmp_path / "partial.sqlite3"
        conn = sqlite3.connect(str(partial))
        try:
            conn.execute(
                "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) "
                "VALUES (?, '0001_initial', CURRENT_TIMESTAMP)",
                (check_database.EXPECTED_SCHEMA_VERSION,),
            )
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(partial)
        assert code == 2
        captured = capsys.readouterr()
        assert "missing required tables" in captured.err

    def test_older_schema_version_returns_2(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        """Older version with full production tables. Exercises the
        version-mismatch path in :func:`_check_schema_version`.
        """
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute("DELETE FROM _migrations")
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) "
                "VALUES (1, '0001_initial', CURRENT_TIMESTAMP)"
            )
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(fresh_db)
        assert code == 2
        captured = capsys.readouterr()
        assert "older than this checker expects" in captured.err

    def test_newer_schema_version_returns_2(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute("DELETE FROM _migrations")
            future_version = check_database.EXPECTED_SCHEMA_VERSION + 5
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) "
                "VALUES (?, '0099_future', CURRENT_TIMESTAMP)",
                (future_version,),
            )
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(fresh_db)
        assert code == 2
        captured = capsys.readouterr()
        assert "newer than this checker expects" in captured.err

    def test_required_column_missing_returns_2(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        """Drop a required column from a real production schema.

        The preflight must detect the missing column and return 2.
        """
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute(
                "CREATE TABLE _drop_cost AS "
                "SELECT id, status, started_at, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, reasoning_tokens "
                "FROM requests"
            )
            conn.execute("DROP TABLE requests")
            conn.execute("ALTER TABLE _drop_cost RENAME TO requests")
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(fresh_db)
        assert code == 2
        captured = capsys.readouterr()
        assert "missing required columns" in captured.err

    def test_valid_empty_production_schema_returns_0(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        code = _run_main_with_path(fresh_db)
        assert code == 0
        captured = capsys.readouterr()
        assert "Database invariants OK" in captured.out

    def test_migration_0043_columns_present(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        """After all migrations run the 0043 safe-suffix-compression
        columns must be present on the ``requests`` table."""
        expected_columns = {
            "compression_applied",
            "compression_transform_count",
            "compression_transforms_by_reason_json",
            "compression_original_tokens",
            "compression_compressed_tokens",
            "compression_savings_tokens",
            "compression_pre_stable_prefix_hash",
            "compression_post_stable_prefix_hash",
            "compression_stable_prefix_preserved",
            "compression_warnings_json",
            "compression_latency_ms",
            "compression_failed_fallback",
            "compression_applied_summary_json",
        }
        conn = sqlite3.connect(str(fresh_db))
        try:
            rows = conn.execute("PRAGMA table_info(requests)").fetchall()
            columns = {row[1] for row in rows}
        finally:
            conn.close()

        missing = expected_columns - columns
        assert not missing, f"Missing 0043 columns: {sorted(missing)}"

        code = _run_main_with_path(fresh_db)
        assert code == 0

    def test_stale_pending_request_returns_1(
        self, fresh_db: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        """A pending request older than the threshold is a real
        invariant violation: exit code 1.
        """
        from eggpool.db.connection import Database

        async def _seed() -> None:
            db = Database(path=str(fresh_db))
            await db.connect()
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                    ("stale-acct", "TEST_ENV"),
                )
                await db.execute_write(
                    "INSERT OR IGNORE INTO models (model_id, protocol) "
                    "VALUES (?, 'openai')",
                    ("stale-model",),
                )
                await db.execute_write(
                    "INSERT INTO requests (account_id, model_id, status, "
                    "protocol, streamed, started_at) "
                    "VALUES (1, 'stale-model', 'pending', 'openai', 0, "
                    "datetime('now', '-1 hour'))",
                )
            await db.disconnect()

        asyncio.run(_seed())
        code = _run_main_with_path(fresh_db)
        assert code == 1
        captured = capsys.readouterr()
        assert "stale pending request" in captured.err

    def test_simulated_invariant_query_failure_returns_2(
        self,
        fresh_db: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[Any],
    ) -> None:
        """Force an invariant query to fail.

        The checker must not silently report zero violations; it must
        surface the error and exit 2.
        """
        from eggpool.errors import DatabaseError

        async def _boom(
            db: Any,
            threshold_seconds: int = 600,
        ) -> list[str]:
            raise check_database.InvariantQueryError(
                "Invariant query failed: requests:stale_pending"
            ) from DatabaseError("simulated invariant failure")

        monkeypatch.setattr(check_database, "_check_no_orphan_pending", _boom)
        code = _run_main_with_path(fresh_db)
        assert code == 2
        captured = capsys.readouterr()
        assert "Invariant query failed" in captured.err

    def test_checker_does_not_change_db_hash_wal_or_schema(
        self, fresh_db: Path, tmp_path: Path
    ) -> None:
        """The checker must be observably side-effect-free.

        The main database file hash, WAL/SHM existence, and sqlite
        schema (via a sha256 of the canonical DDL) must all match
        before and after running the checker on a healthy database.
        """
        target = tmp_path / "side_effect_target.sqlite3"
        target.write_bytes(fresh_db.read_bytes())

        wal = target.with_suffix(".sqlite3-wal")
        shm = target.with_suffix(".sqlite3-shm")

        def _canonical_schema(path: Path) -> str:
            conn = sqlite3.connect(str(path))
            try:
                rows = conn.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
            finally:
                conn.close()
            return "\n".join(f"{r[0]}|{r[1]}|{r[2]}" for r in rows)

        before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        before_schema = hashlib.sha256(
            _canonical_schema(target).encode("utf-8")
        ).hexdigest()
        before_wal_exists = wal.exists()
        before_shm_exists = shm.exists()

        code = _run_main_with_path(target)

        after_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        after_schema = hashlib.sha256(
            _canonical_schema(target).encode("utf-8")
        ).hexdigest()
        after_wal_exists = wal.exists()
        after_shm_exists = shm.exists()

        assert code == 0
        assert before_hash == after_hash, "Checker mutated the database file"
        assert before_schema == after_schema, "Checker mutated the schema"
        assert before_wal_exists == after_wal_exists, "Checker toggled WAL file"
        assert before_shm_exists == after_shm_exists, "Checker toggled SHM file"


class TestImportSafety:
    """Importing the script must be side-effect-free."""

    def test_does_not_read_env_at_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOROUTER_DB_PATH", raising=False)
        import importlib

        module = importlib.import_module("scripts.check_database")
        importlib.reload(module)
        assert "GOROUTER_DB_PATH" not in os.environ
