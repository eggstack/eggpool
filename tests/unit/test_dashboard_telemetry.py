"""Tests for dashboard telemetry per-surface instrumentation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from eggpool.dashboard.telemetry import DashboardTelemetry
from eggpool.db.connection import Database
from eggpool.stats.service import StatsService, TimeRange


@pytest_asyncio.fixture()
async def db() -> Database:
    database = Database(path=":memory:")
    await database.connect()
    yield database
    await database.disconnect()


class TestDashboardTelemetryRecordStage:
    def test_record_stage_accumulates_timings(self) -> None:
        t = DashboardTelemetry()
        t.record_stage("overview", "account_stats", 10.5)
        t.record_stage("overview", "account_stats", 20.3)
        snap = t.stage_snapshot()
        stages = snap["slow_stages"]
        assert len(stages) == 1
        assert stages[0]["page"] == "overview"
        assert stages[0]["stage"] == "account_stats"
        assert stages[0]["sample_count"] == 2
        assert stages[0]["p95_ms"] > 0

    def test_record_stage_cache_hit_none_does_not_crash(self) -> None:
        t = DashboardTelemetry()
        t.record_stage("overview", "x", 1.0, cache_hit=None)
        t.record_stage("overview", "x", 2.0, cache_hit=True)
        t.record_stage("overview", "x", 3.0, cache_hit=False)
        snap = t.stage_snapshot()
        assert len(snap["slow_stages"]) == 1
        assert snap["slow_stages"][0]["sample_count"] == 3

    def test_ring_buffer_bounded_to_100(self) -> None:
        t = DashboardTelemetry()
        for i in range(150):
            t.record_stage("p", "s", float(i))
        snap = t.stage_snapshot()
        assert snap["slow_stages"][0]["sample_count"] == 100

    def test_empty_buffer_returns_empty_slow_stages(self) -> None:
        t = DashboardTelemetry()
        snap = t.stage_snapshot()
        assert snap["slow_stages"] == []

    def test_multiple_pages_and_stages(self) -> None:
        t = DashboardTelemetry()
        t.record_stage("overview", "account_stats", 5.0)
        t.record_stage("overview", "model_stats", 15.0)
        t.record_stage("timeseries", "timeseries_flat", 25.0)
        snap = t.stage_snapshot()
        stages = snap["slow_stages"]
        assert len(stages) == 3
        assert stages[0]["stage"] == "timeseries_flat"
        assert stages[0]["p95_ms"] >= stages[1]["p95_ms"]


class TestDashboardTelemetrySnapshot:
    def test_record_render_still_works(self) -> None:
        t = DashboardTelemetry()
        t.record_render("overview", 100.0)
        t.record_render("overview", 200.0)
        snap = t.snapshot()
        assert snap["recent_render_ms_p50"] is not None
        assert snap["recent_render_ms_p95"] is not None
        assert snap["slowest_recent_route"] == "overview"

    def test_empty_snapshot_returns_none_fields(self) -> None:
        t = DashboardTelemetry()
        snap = t.snapshot()
        assert snap["recent_render_ms_p50"] is None
        assert snap["recent_render_ms_p95"] is None
        assert snap["slowest_recent_route"] is None

    def test_snapshot_includes_slow_stages_key(self) -> None:
        t = DashboardTelemetry()
        t.record_render("overview", 50.0)
        snap = t.snapshot()
        assert "recent_render_ms_p50" in snap


class TestStatsServiceCacheCounters:
    @pytest.mark.asyncio()
    async def test_first_cache_call_is_miss(self, db: Database) -> None:
        service = StatsService(db)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="24h",
        )
        with patch(
            "eggpool.stats.service.fetch_summary",
            new=AsyncMock(return_value={"total_requests": 5}),
        ):
            await service.get_summary(time_range, use_cache=True)
        snap = service.cache_snapshot()
        assert snap["misses"] == 1
        assert snap["hits"] == 0

    @pytest.mark.asyncio()
    async def test_second_cache_call_is_hit(self, db: Database) -> None:
        service = StatsService(db)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="24h",
        )
        with patch(
            "eggpool.stats.service.fetch_summary",
            new=AsyncMock(return_value={"total_requests": 5}),
        ):
            await service.get_summary(time_range, use_cache=True)
            await service.get_summary(time_range, use_cache=True)
        snap = service.cache_snapshot()
        assert snap["hits"] == 1
        assert snap["misses"] == 1

    @pytest.mark.asyncio()
    async def test_use_cache_false_does_not_increment(self, db: Database) -> None:
        service = StatsService(db)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="24h",
        )
        with patch(
            "eggpool.stats.service.fetch_summary",
            new=AsyncMock(return_value={"total_requests": 5}),
        ):
            await service.get_summary(time_range, use_cache=False)
            await service.get_summary(time_range, use_cache=False)
        snap = service.cache_snapshot()
        assert snap["hits"] == 0
        assert snap["misses"] == 0

    def test_cache_snapshot_returns_expected_keys(self, db: Database) -> None:
        service = StatsService(db)
        snap = service.cache_snapshot()
        assert set(snap.keys()) == {"hits", "misses", "hit_rate", "entries"}
        assert snap["hit_rate"] == 0.0
        assert snap["entries"] == 0
