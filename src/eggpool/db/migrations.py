"""Migration runner for SQLite schema management."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from eggpool.errors import DatabaseError

if TYPE_CHECKING:
    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).parent / "schema"


def _split_statements(sql: str) -> list[str]:
    """Split a SQL migration using SQLite's own completeness parser.

    This handles quoted semicolons, comments, and trigger bodies according to
    SQLite syntax instead of maintaining a partial SQL parser here.
    """
    statements: list[str] = []
    current: list[str] = []
    for ch in sql:
        current.append(ch)
        if ch == ";":
            candidate = "".join(current)
            if not sqlite3.complete_statement(candidate):
                continue
            statement = candidate.strip()
            current = []
            if _contains_sql(statement):
                statements.append(statement)

    trailing = "".join(current).strip()
    if _contains_sql(trailing):
        statements.append(trailing)

    return statements


def _contains_sql(candidate: str) -> bool:
    """Return whether text contains more than whitespace and line comments."""
    return any(
        line.strip() and not line.lstrip().startswith("--")
        for line in candidate.splitlines()
    )


class MigrationRunner:
    """Reads .sql files from schema dir, tracks applied versions, applies in order."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def run(self) -> None:
        """Apply all pending migrations in order.

        Each migration file is applied in a single ``db.transaction()``
        boundary so that schema and bookkeeping are atomic; on any
        statement failure the entire migration is rolled back and the
        database is left untouched.
        """
        await self._ensure_migrations_table()
        applied = await self._applied_versions()
        pending = self._pending_migrations(applied)

        if not pending:
            logger.info("No pending migrations")
            return

        for version, path in sorted(pending.items()):
            sql = path.read_text(encoding="utf-8")
            statements = _split_statements(sql)
            logger.info("Applying migration %04d: %s", version, path.name)
            try:
                async with self._db.transaction():
                    for stmt in statements:
                        await self._db._execute_cursor(stmt)  # pyright: ignore[reportPrivateUsage] -- DDL requires raw cursor, safe inside transaction
                    await self._db._execute_cursor(  # pyright: ignore[reportPrivateUsage] -- DDL requires raw cursor, safe inside transaction
                        "INSERT INTO _migrations (version, name) VALUES (?, ?)",
                        (version, path.name),
                    )
            except DatabaseError:
                raise

        logger.info("Applied %d migration(s)", len(pending))

    async def _ensure_migrations_table(self) -> None:
        """Create the _migrations tracking table if it doesn't exist."""
        async with self._db.transaction():
            await self._db._execute_cursor(  # pyright: ignore[reportPrivateUsage] -- DDL requires raw cursor, safe inside transaction
                """
                CREATE TABLE IF NOT EXISTS _migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    async def _applied_versions(self) -> set[int]:
        """Return set of already-applied migration versions."""
        rows = await self._db.fetch_all("SELECT version FROM _migrations")
        return {row["version"] for row in rows}

    def _pending_migrations(self, applied: set[int]) -> dict[int, Path]:
        """Return dict of version -> path for unapplied .sql files."""
        if not SCHEMA_DIR.exists():
            return {}
        pending: dict[int, Path] = {}
        for path in sorted(SCHEMA_DIR.glob("*.sql")):
            name = path.stem
            try:
                version = int(name.split("_")[0])
            except (ValueError, IndexError):
                logger.warning("Skipping unparseable migration file: %s", path.name)
                continue
            if version not in applied:
                pending[version] = path
        return pending
