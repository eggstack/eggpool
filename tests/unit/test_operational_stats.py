"""Tests for operational_events observability.

Covers Phase 3 of the metrics-core-api plan: operational_events table
queries, summary aggregation across event types, and per-type
filtering on the recent-events endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import OperationalEventRepository
from eggpool.stats import queries
from eggpool.stats.service import StatsService, resolve_time_range

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "operational_stats_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def seeded_op_db(db: Database) -> Database:
    """Seed operational_events rows covering all three event types."""
    repo = OperationalEventRepository(db)
    async with db.transaction():
        await repo.record(
            event_type="crash_recovery",
            details={
                "interrupted_requests": 3,
                "released_reservations": 4,
                "affected_accounts": 2,
            },
        )
        await repo.record(
            event_type="stale_request_finalizer",
            details={"leaked_requests": 1, "released_reservations": 1},
        )
        await repo.record(
            event_type="reservation_reconcile",
            details={"expired_reservations": 5},
        )
        await repo.record(
            event_type="stale_request_finalizer",
            details={"leaked_requests": 2, "released_reservations": 2},
        )
    return db


@pytest.mark.asyncio
async def test_fetch_operational_event_summary_groups_by_type(
    seeded_op_db: Database,
) -> None:
    rows = await queries.fetch_operational_event_summary(
        seeded_op_db,
        start="1970-01-01 00:00:00",
        end="2999-12-31 23:59:59",
    )
    by_type = {row["event_type"]: row for row in rows}
    assert by_type["crash_recovery"]["event_count"] == 1
    assert by_type["stale_request_finalizer"]["event_count"] == 2
    assert by_type["reservation_reconcile"]["event_count"] == 1

    assert by_type["stale_request_finalizer"]["total_leaked_requests"] == 3
    assert by_type["crash_recovery"]["total_interrupted_requests"] == 3
    assert by_type["reservation_reconcile"]["total_expired_reservations"] == 5
    assert by_type["crash_recovery"]["total_affected_accounts"] == 2


@pytest.mark.asyncio
async def test_fetch_operational_event_summary_empty_window(
    seeded_op_db: Database,
) -> None:
    rows = await queries.fetch_operational_event_summary(
        seeded_op_db,
        start="2998-01-01 00:00:00",
        end="2998-01-02 00:00:00",
    )
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_recent_operational_events_returns_newest_first(
    seeded_op_db: Database,
) -> None:
    repo = OperationalEventRepository(seeded_op_db)
    async with seeded_op_db.transaction():
        await repo.record(
            event_type="crash_recovery",
            details={"interrupted_requests": 99, "released_reservations": 99},
        )
    rows = await queries.fetch_recent_operational_events(seeded_op_db, limit=10)
    assert rows
    assert rows[0]["event_type"] == "crash_recovery"
    occurred_at_values = [row["occurred_at"] for row in rows]
    assert occurred_at_values == sorted(occurred_at_values, reverse=True)


@pytest.mark.asyncio
async def test_fetch_recent_operational_events_filters_by_type(
    seeded_op_db: Database,
) -> None:
    rows = await queries.fetch_recent_operational_events(
        seeded_op_db, limit=10, event_type="crash_recovery"
    )
    assert rows
    assert all(row["event_type"] == "crash_recovery" for row in rows)


@pytest.mark.asyncio
async def test_stats_service_operational_health(seeded_op_db: Database) -> None:
    service = StatsService(seeded_op_db)
    time_range = resolve_time_range("7d")
    summary = await service.get_operational_event_summary(time_range)
    assert summary
    recent = await service.get_recent_operational_events(limit=10)
    assert len(recent) >= 4
    by_type_recent = {row["event_type"] for row in recent}
    assert {"crash_recovery", "stale_request_finalizer", "reservation_reconcile"} <= (
        by_type_recent
    )


@pytest.mark.asyncio
async def test_operational_event_repository_records_details_json(
    seeded_op_db: Database,
) -> None:
    rows = await seeded_op_db.fetch_all(
        "SELECT event_type, details_json FROM operational_events "
        "WHERE event_type = 'crash_recovery'"
    )
    assert rows
    payload = rows[0]["details_json"]
    assert isinstance(payload, str)
    assert (
        '"interrupted_requests": 3' in payload or '"interrupted_requests":3' in payload
    )


@pytest.mark.asyncio
async def test_operational_event_repository_unique_id(
    db: Database,
) -> None:
    repo = OperationalEventRepository(db)
    async with db.transaction():
        await repo.record(event_type="crash_recovery", details={})
        await repo.record(event_type="crash_recovery", details={})
    rows = await db.fetch_all("SELECT id FROM operational_events ORDER BY id")
    ids = [int(row["id"]) for row in rows]
    assert ids == [1, 2]
