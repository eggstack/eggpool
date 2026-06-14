"""Migration runner for SQLite schema management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from go_aggregator.errors import DatabaseError

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database

logger = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).parent / "schema"


def _split_statements(sql: str) -> list[str]:
    """Split a SQL file into individual statements, stripping comments and blanks."""
    statements: list[str] = []
    for block in sql.split(";"):
        cleaned = block.strip()
        # Remove leading comment lines
        lines = [
            line for line in cleaned.splitlines() if not line.strip().startswith("--")
        ]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    return statements


class MigrationRunner:
    """Reads .sql files from schema dir, tracks applied versions, applies in order."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def run(self) -> None:
        """Apply all pending migrations in order."""
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
                for stmt in statements:
                    await self._db.execute(stmt)
                await self._db.execute(
                    "INSERT INTO _migrations (version, name) VALUES (?, ?)",
                    (version, path.name),
                )
                await self._db.connection.commit()
            except DatabaseError:
                await self._db.connection.rollback()
                raise

        logger.info("Applied %d migration(s)", len(pending))

    async def _ensure_migrations_table(self) -> None:
        """Create the _migrations tracking table if it doesn't exist."""
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self._db.connection.commit()

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
