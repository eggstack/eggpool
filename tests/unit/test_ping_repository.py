"""Tests for provider ping repository and stats queries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import PingRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "ping_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest.mark.asyncio
async def test_record_ping(db: Database) -> None:
    """Recording a ping inserts a row into provider_pings."""
    repo = PingRepository(db)
    async with db.transaction():
        await repo.record_ping(
            provider_id="test-provider",
            account_name="test-acct",
            latency_ms=142,
            status_code=200,
            error=None,
            model_count=10,
        )
    recent = await repo.get_ping_recent(limit=1)
    assert len(recent) == 1
    assert recent[0]["provider_id"] == "test-provider"
    assert recent[0]["account_name"] == "test-acct"
    assert recent[0]["latency_ms"] == 142
    assert recent[0]["status_code"] == 200
    assert recent[0]["error"] is None
    assert recent[0]["model_count"] == 10


@pytest.mark.asyncio
async def test_record_ping_with_error(db: Database) -> None:
    """Recording a failed ping stores the error."""
    repo = PingRepository(db)
    async with db.transaction():
        await repo.record_ping(
            provider_id="test-provider",
            account_name="test-acct",
            latency_ms=500,
            status_code=403,
            error="HTTP 403",
            model_count=0,
        )
    recent = await repo.get_ping_recent(limit=1)
    assert len(recent) == 1
    assert recent[0]["status_code"] == 403
    assert recent[0]["error"] == "HTTP 403"
    assert recent[0]["model_count"] == 0


@pytest.mark.asyncio
async def test_get_ping_recent_filtered(db: Database) -> None:
    """get_ping_recent filters by provider_id."""
    repo = PingRepository(db)
    async with db.transaction():
        await repo.record_ping("provider-a", "acct1", 100, 200, None, 5)
        await repo.record_ping("provider-b", "acct2", 200, 200, None, 8)
    result_a = await repo.get_ping_recent(provider_id="provider-a", limit=10)
    assert len(result_a) == 1
    assert result_a[0]["provider_id"] == "provider-a"
    result_b = await repo.get_ping_recent(provider_id="provider-b", limit=10)
    assert len(result_b) == 1
    assert result_b[0]["provider_id"] == "provider-b"


@pytest.mark.asyncio
async def test_get_provider_ping_summary(db: Database) -> None:
    """get_provider_ping_summary aggregates correctly."""
    repo = PingRepository(db)
    async with db.transaction():
        await repo.record_ping("provider-a", "acct1", 100, 200, None, 5)
        await repo.record_ping("provider-a", "acct1", 200, 200, None, 5)
        await repo.record_ping("provider-a", "acct1", 300, 500, "HTTP 500", 0)

    # Use a wide time range to match any timestamp format
    summary = await repo.get_provider_ping_summary("2000-01-01", "2100-01-01")
    assert len(summary) == 1
    row = summary[0]
    assert row["provider_id"] == "provider-a"
    assert row["ping_count"] == 3
    assert float(row["avg_latency_ms"]) == pytest.approx(200.0)
    assert row["min_latency_ms"] == 100
    assert row["max_latency_ms"] == 300
    assert row["success_count"] == 2
    assert row["failure_count"] == 1


@pytest.mark.asyncio
async def test_cleanup_old_pings(db: Database) -> None:
    """cleanup_old_pings removes old rows."""
    repo = PingRepository(db)
    async with db.transaction():
        await repo.record_ping("provider-a", "acct1", 100, 200, None, 5)
    # All pings are new, so cleanup with retain_days=1 should not delete
    deleted = await repo.cleanup_old_pings(retain_days=1)
    assert deleted == 0


@pytest.mark.asyncio
async def test_get_ping_recent_empty(db: Database) -> None:
    """get_ping_recent returns empty list when no data."""
    repo = PingRepository(db)
    result = await repo.get_ping_recent(limit=10)
    assert result == []


@pytest.mark.asyncio
async def test_ping_timeseries(db: Database) -> None:
    """get_ping_timeseries returns bucketed data."""
    repo = PingRepository(db)
    async with db.transaction():
        await repo.record_ping("provider-a", "acct1", 100, 200, None, 5)

    ts = await repo.get_ping_timeseries(
        "provider-a", "2000-01-01", "2100-01-01", bucket="hour"
    )
    assert len(ts) >= 1
    assert ts[0]["ping_count"] >= 1
    assert float(ts[0]["avg_latency_ms"]) == pytest.approx(100.0)
