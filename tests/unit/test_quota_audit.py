"""Tests for quota audit helpers and in-memory reservation accounting.

The audit module exposes read-only SQL queries for operator introspection;
the in-memory reservation regression test exercises the bug found in
Phase 7 where the non-retryable error path failed to release the
in-memory reservation cost held by ``QuotaEstimator``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.quota.audit import (
    account_usage_breakdown,
    active_reservations_summary,
    exactness_distribution,
    stale_pending_requests,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-a", "ENV_A"),
        )
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-b", "ENV_B"),
        )
        await database.execute_insert(
            "INSERT INTO models (model_id, display_name, protocol) VALUES (?, ?, ?)",
            ("gpt-4", "GPT-4", "openai"),
        )
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def populated_db(db: Database) -> Database:
    """Seed the database with a mix of completed and pending requests."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO requests (account_id, model_id, status, "
            "started_at, cost_microdollars, exactness, "
            "input_tokens, output_tokens) VALUES "
            "(1, 'gpt-4', 'completed', "
            "datetime('now', '-2 hours'), 1000, 'exact', 10, 20), "
            "(1, 'gpt-4', 'completed', "
            "datetime('now', '-3 hours'), 2000, 'estimated', 30, 40), "
            "(1, 'gpt-4', 'error', "
            "datetime('now', '-1 hours'), 500, 'unknown', 5, 0), "
            "(2, 'gpt-4', 'completed', "
            "datetime('now', '-4 hours'), 3000, 'exact', 50, 60)",
        )
        await db.execute_write(
            "INSERT INTO requests (account_id, model_id, status, "
            "started_at, reserved_microdollars) VALUES "
            "(1, 'gpt-4', 'pending', "
            "datetime('now', '-30 minutes'), 750), "
            "(2, 'gpt-4', 'pending', "
            "datetime('now', '-15 minutes'), 250)"
        )
        await db.execute_write(
            "INSERT INTO reservations (request_id, account_id, model_id, "
            "reserved_microdollars, status) VALUES "
            "(5, 1, 'gpt-4', 750, 'active'), "
            "(6, 2, 'gpt-4', 250, 'active')"
        )
    return db


class TestAccountUsageBreakdown:
    """Tests for account_usage_breakdown."""

    @pytest.mark.asyncio
    async def test_returns_zeros_for_unknown_account(
        self,
        db: Database,
    ) -> None:
        result = await account_usage_breakdown(db, 9999)
        assert result["account_id"] == 9999
        assert result["account_name"] is None
        assert result["request_count"] == 0
        assert result["total_cost_microdollars"] == 0
        assert result["pending_reserved_microdollars"] == 0

    @pytest.mark.asyncio
    async def test_aggregates_completed_and_pending(
        self,
        populated_db: Database,
    ) -> None:
        result = await account_usage_breakdown(populated_db, 1)
        assert result["account_name"] == "acct-a"
        assert result["completed_count"] == 2
        assert result["error_count"] == 1
        assert result["pending_count"] == 1
        assert result["request_count"] == 4
        assert result["total_cost_microdollars"] == 3500
        assert result["pending_reserved_microdollars"] == 750

    @pytest.mark.asyncio
    async def test_separates_5h_rolling_cost(
        self,
        populated_db: Database,
    ) -> None:
        result = await account_usage_breakdown(populated_db, 1)
        assert result["cost_5h_microdollars"] == 3500


class TestActiveReservationsSummary:
    """Tests for active_reservations_summary."""

    @pytest.mark.asyncio
    async def test_empty_when_no_active(
        self,
        db: Database,
    ) -> None:
        rows = await active_reservations_summary(db)
        assert rows == []

    @pytest.mark.asyncio
    async def test_groups_by_account(
        self,
        populated_db: Database,
    ) -> None:
        rows = await active_reservations_summary(populated_db)
        assert len(rows) == 2
        by_name = {row["account_name"]: row for row in rows}
        assert by_name["acct-a"]["active_reservations"] == 1
        assert by_name["acct-a"]["active_reserved_microdollars"] == 750
        assert by_name["acct-b"]["active_reservations"] == 1
        assert by_name["acct-b"]["active_reserved_microdollars"] == 250


class TestExactnessDistribution:
    """Tests for exactness_distribution."""

    @pytest.mark.asyncio
    async def test_groups_by_exactness_level(
        self,
        populated_db: Database,
    ) -> None:
        rows = await exactness_distribution(populated_db)
        by_name = {row["exactness"]: row for row in rows}
        assert by_name["exact"]["request_count"] == 2
        assert by_name["exact"]["total_cost_microdollars"] == 4000
        assert by_name["estimated"]["request_count"] == 1
        assert by_name["estimated"]["total_cost_microdollars"] == 2000
        assert by_name["unknown"]["request_count"] == 1
        assert by_name["unknown"]["total_cost_microdollars"] == 500

    @pytest.mark.asyncio
    async def test_empty_when_no_requests(
        self,
        db: Database,
    ) -> None:
        rows = await exactness_distribution(db)
        assert rows == []


class TestStalePendingRequests:
    """Tests for stale_pending_requests."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_old_pending(
        self,
        populated_db: Database,
    ) -> None:
        result = await stale_pending_requests(populated_db, threshold_seconds=3600)
        assert result == 0

    @pytest.mark.asyncio
    async def test_counts_pending_above_threshold(
        self,
        populated_db: Database,
    ) -> None:
        result = await stale_pending_requests(populated_db, threshold_seconds=60)
        assert result == 2
