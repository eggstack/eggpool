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
        # The all-accounts variant also carries request/token counts
        # so the scorer can drive load balancing off request and
        # token volume instead of unreliable cost.
        assert all_windows[account_id] == {
            **one,
            "request_count_5h": 2,
            "request_count_7d": 2,
            "request_count_30d": 2,
            "token_count_5h": 0,
            "token_count_7d": 0,
            "token_count_30d": 0,
        }
    finally:
        await database.disconnect()
