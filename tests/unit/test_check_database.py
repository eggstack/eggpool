"""Tests for the read-only database invariant checker."""

from __future__ import annotations

import asyncio
import os
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
    from go_aggregator.db.connection import Database
    from go_aggregator.db.migrations import MigrationRunner

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


def _migrate_to(target_version: int) -> None:
    """Helper: apply migrations up to (but not past) the target version."""
    # The migration runner is keyed by the file order. The test
    # only needs to bring the on-disk schema to a known state, so
    # we use the same runner as the application.
    raise NotImplementedError  # covered by fresh_db fixture


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
        self, tmp_path: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        # Build a hand-rolled older-schema database with a
        # _migrations table reporting version 1.
        import sqlite3

        old_db = tmp_path / "old.sqlite3"
        conn = sqlite3.connect(str(old_db))
        try:
            conn.execute(
                "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) "
                "VALUES (1, '0001_initial', CURRENT_TIMESTAMP)"
            )
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(old_db)
        assert code == 2
        captured = capsys.readouterr()
        assert "older than this checker expects" in captured.err

    def test_newer_schema_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[Any]
    ) -> None:
        import sqlite3

        new_db = tmp_path / "new.sqlite3"
        conn = sqlite3.connect(str(new_db))
        try:
            conn.execute(
                "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) "
                "VALUES (?, '0099_future', CURRENT_TIMESTAMP)",
                (check_database.EXPECTED_SCHEMA_VERSION + 5,),
            )
            conn.commit()
        finally:
            conn.close()

        code = _run_main_with_path(new_db)
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
        import hashlib

        target = tmp_path / "checker_target.sqlite3"
        target.write_bytes(fresh_db.read_bytes())

        before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        code = _run_main_with_path(target)
        after_hash = hashlib.sha256(target.read_bytes()).hexdigest()

        assert code == 0
        assert before_hash == after_hash, "Checker modified the database file contents"

    def test_checker_refuses_writes_on_read_only(self, fresh_db: Path) -> None:
        from go_aggregator.db.connection import Database
        from go_aggregator.errors import DatabaseError

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
