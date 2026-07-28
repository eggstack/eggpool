"""Smoke: database creation and migration."""

from __future__ import annotations

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner


@pytest_asyncio.fixture()
async def db() -> Database:
    database = Database(path=":memory:")
    await database.connect()
    yield database
    await database.disconnect()


@pytest.mark.asyncio()
async def test_database_connects(db: Database) -> None:
    async with db.transaction():
        result = await db.execute_write("SELECT 1")
    assert result is not None


@pytest.mark.asyncio()
async def test_migrations_run(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        tables = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    table_names = {row[0] for row in tables}
    assert "accounts" in table_names
    assert "requests" in table_names
