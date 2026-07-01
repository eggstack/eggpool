"""Tests for usage-window accounting aggregates."""

from __future__ import annotations

import pytest

from eggpool.constants import SQLITE_INTEGER_MAX
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import UsageWindowRepository


@pytest.mark.asyncio()
async def test_usage_windows_do_not_overflow_on_corrupt_historical_costs() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        runner = MigrationRunner(database)
        await runner.run()
        async with database.transaction():
            await database.execute_write(
                "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                ("acct", "KEY"),
            )
            account_id = 1
            await database.execute_write(
                "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
                ("minimax-m3", "MiniMax M3"),
            )
            await database.execute_write(
                "INSERT INTO requests ("
                "account_id, model_id, started_at, status, cost_microdollars"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    account_id,
                    "minimax-m3",
                    "2026-06-30T21:00:00Z",
                    "completed",
                    SQLITE_INTEGER_MAX,
                ),
            )
            await database.execute_write(
                "INSERT INTO requests ("
                "account_id, model_id, started_at, status, cost_microdollars"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    account_id,
                    "minimax-m3",
                    "2026-06-30T21:01:00Z",
                    "completed",
                    SQLITE_INTEGER_MAX,
                ),
            )

        repo = UsageWindowRepository(database)
        one = await repo.get_usage_windows(account_id, "2026-06-30T22:00:00Z")
        all_windows = await repo.get_all_usage_windows("2026-06-30T22:00:00Z")

        assert one == {
            "5h": SQLITE_INTEGER_MAX,
            "7d": SQLITE_INTEGER_MAX,
            "30d": SQLITE_INTEGER_MAX,
        }
        assert all_windows[account_id] == one
    finally:
        await database.disconnect()
