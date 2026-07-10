"""Regression tests for dashboard first-paint latency fix.

Locks the intended behavior from Phases 1-7 of the dashboard
first-paint latency fix plan. These tests run in the default CI suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.dashboard.render import render_overview
from eggpool.dashboard.telemetry import DashboardTelemetry
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.rollup_repository import UsageRollupRepository
from eggpool.metrics.buffer import MetricsWriteCoalescer, UsageMetricEvent
from eggpool.models.config import MetricsConfig
from eggpool.stats.service import StatsService, TimeRange

pytestmark = pytest.mark.dashboard

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "first_paint.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest.fixture()
def rollup_repo(db: Database) -> UsageRollupRepository:
    return UsageRollupRepository(db)


async def _insert_account(db: Database, name: str) -> int:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
            (name, f"{name.upper()}_ENV", 1),
        )
    row = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", (name,))
    assert row is not None
    return int(row["id"])


async def _insert_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            (model_id, "openai"),
        )


async def _flush_events(
    db: Database,
    rollup_repo: UsageRollupRepository,
    events: list[UsageMetricEvent],
    bucket_size_s: int = 3600,
) -> None:
    config = MetricsConfig(
        write_mode="balanced",
        flush_interval_s=30,
        max_buffered_events=500,
        timeseries_bucket_s=bucket_size_s,
    )
    coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)
    for event in events:
        coalescer.record_usage(event)
    result = await coalescer.flush(reason="first_paint_test")
    assert result.rows_flushed > 0


# ---------------------------------------------------------------------------
# Part B.1: Overview doesn't require flat timeseries for shell render
# ---------------------------------------------------------------------------


class TestProgressiveOverviewShell:
    """Overview renders shell without blocking timeseries data."""

    def test_overview_progressive_has_chart_endpoint(self) -> None:
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            progressive_timeseries=True,
        )
        assert 'data-chart-endpoint="/api/timeseries?period=' in html

    def test_overview_progressive_no_blocking_data_island(self) -> None:
        timeseries = [
            {"bucket": "2024-01-01 12:00:00", "request_count": 3, "error_count": 1}
        ]
        html = render_overview(
            overview={
                "summary": {"total_requests": 3},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            timeseries=timeseries,
            progressive_timeseries=True,
        )
        noscript_start = html.index("<noscript>")
        before_noscript = html[:noscript_start]
        assert 'id="timeseries-initial-data"' not in before_noscript

    def test_overview_progressive_noscript_has_data_island(self) -> None:
        timeseries = [
            {"bucket": "2024-01-01 12:00:00", "request_count": 3, "error_count": 1}
        ]
        html = render_overview(
            overview={
                "summary": {"total_requests": 3},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            timeseries=timeseries,
            progressive_timeseries=True,
        )
        noscript_start = html.index("<noscript>")
        noscript_end = html.index("</noscript>", noscript_start)
        noscript_block = html[noscript_start:noscript_end]
        assert 'id="timeseries-initial-data"' in noscript_block
        assert "2024-01-01 12:00:00" in noscript_block

    def test_overview_progressive_has_loading_shell(self) -> None:
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            progressive_timeseries=True,
        )
        assert 'data-chart-canvas="timeseries-chart"' in html
        assert 'data-chart-state="loading"' in html
        assert "Loading chart data" in html

    def test_overview_progressive_has_canvas_in_noscript(self) -> None:
        html = render_overview(
            overview={
                "summary": {"total_requests": 0},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            progressive_timeseries=True,
        )
        noscript_start = html.index("<noscript>")
        noscript_end = html.index("</noscript>", noscript_start)
        noscript_block = html[noscript_start:noscript_end]
        assert 'id="timeseries-chart"' in noscript_block

    def test_non_progressive_still_has_data_island(self) -> None:
        timeseries = [
            {"bucket": "2024-01-01 12:00:00", "request_count": 3, "error_count": 1}
        ]
        html = render_overview(
            overview={
                "summary": {"total_requests": 3},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            timeseries=timeseries,
        )
        assert 'id="timeseries-initial-data"' in html
        assert "2024-01-01 12:00:00" in html


# ---------------------------------------------------------------------------
# Part B.2: get_timeseries prefers rollups
# ---------------------------------------------------------------------------


class TestTimeseriesRollupPreference:
    """get_timeseries uses rollups for 24h and falls back for short windows."""

    @pytest.mark.asyncio()
    async def test_rollup_preferred_for_24h(self, db: Database) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "ts_acct")
        await _insert_model(db, "model_a")
        now = datetime.now(UTC)
        anchor = now - timedelta(hours=6)
        await _flush_events(
            db,
            rollup_repo,
            [
                UsageMetricEvent(
                    timestamp=anchor,
                    provider_id="provider_a",
                    model_id="model_a",
                    account_id=account_id,
                    protocol="openai",
                    streamed=False,
                    status="completed",
                    retry_count=0,
                    input_tokens=100,
                    output_tokens=200,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    reasoning_tokens=0,
                    thinking_characters=0,
                    cost_microdollars=500,
                    bytes_received=1000,
                    bytes_emitted=500,
                    latency_ms=50,
                    first_byte_ms=None,
                ),
            ],
        )
        time_range = TimeRange(start=now - timedelta(hours=24), end=now, label="24h")
        service = StatsService(db, rollup_repo=rollup_repo)
        result = await service.get_timeseries(time_range, bucket="hour")
        assert result is not None
        assert len(result) > 0
        total_requests = sum(int(r.get("request_count", 0)) for r in result)
        assert total_requests == 1

    @pytest.mark.asyncio()
    async def test_fine_rollups_are_resampled_for_long_windows(
        self, db: Database
    ) -> None:
        """The default 60-second rollups must feed hourly dashboard charts."""
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "ts_fine_acct")
        await _insert_model(db, "model_a")
        now = datetime.now(UTC)
        await _flush_events(
            db,
            rollup_repo,
            [
                UsageMetricEvent(
                    timestamp=now - timedelta(hours=6, minutes=1),
                    provider_id="provider_a",
                    model_id="model_a",
                    account_id=account_id,
                    protocol="openai",
                    streamed=False,
                    status="completed",
                    retry_count=0,
                    input_tokens=100,
                    output_tokens=200,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    reasoning_tokens=0,
                    thinking_characters=0,
                    cost_microdollars=500,
                    bytes_received=1000,
                    bytes_emitted=500,
                    latency_ms=50,
                    first_byte_ms=None,
                ),
            ],
            bucket_size_s=60,
        )
        time_range = TimeRange(start=now - timedelta(hours=24), end=now, label="24h")
        service = StatsService(db, rollup_repo=rollup_repo)

        flat = await service.get_timeseries(time_range, bucket="hour")
        assert flat is not None
        assert sum(int(row["request_count"]) for row in flat) == 1

        grouped = await service.get_grouped_timeseries(
            time_range, bucket="hour", group_by="provider_model"
        )
        assert sum(int(point["request_count"]) for point in grouped["points"]) == 1

    @pytest.mark.asyncio()
    async def test_empty_rollups_24h_returns_empty(self, db: Database) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "ts_empty_acct")
        await _insert_model(db, "model_a")
        async with db.transaction():
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms
                ) VALUES (?, ?, ?, datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'), 'completed', 100, 200, 100, 50.0)
                """,
                (account_id, "model_a", "provider_a"),
            )
        now = datetime.now(UTC)
        time_range = TimeRange(start=now - timedelta(hours=24), end=now, label="24h")
        service = StatsService(db, rollup_repo=rollup_repo)
        result = await service.get_timeseries(time_range, bucket="hour")
        assert result is not None
        assert len(result) == 0

    @pytest.mark.asyncio()
    async def test_empty_rollups_1h_falls_back_to_raw(self, db: Database) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "ts_1h_acct")
        await _insert_model(db, "model_a")
        async with db.transaction():
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms
                ) VALUES (?, ?, ?, datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'), 'completed', 50, 60, 25, 20.0)
                """,
                (account_id, "model_a", "provider_a"),
            )
        now = datetime.now(UTC)
        time_range = TimeRange(start=now - timedelta(hours=1), end=now, label="1h")
        service = StatsService(db, rollup_repo=rollup_repo)
        result = await service.get_timeseries(time_range, bucket="hour")
        assert result is not None
        total = sum(int(r.get("request_count", 0)) for r in result)
        assert total == 1


# ---------------------------------------------------------------------------
# Part B.3: get_grouped_timeseries avoids full raw fallback for long windows
# ---------------------------------------------------------------------------


class TestGroupedTimeseriesRollupPreference:
    """get_grouped_timeseries uses rollups for large windows."""

    @pytest.mark.asyncio()
    async def test_empty_rollups_7d_returns_empty(self, db: Database) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "grp_7d_acct")
        await _insert_model(db, "model_a")
        async with db.transaction():
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms
                ) VALUES (?, ?, ?, datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'), 'completed', 10, 20, 5, 10.0)
                """,
                (account_id, "model_a", "provider_a"),
            )
        now = datetime.now(UTC)
        time_range = TimeRange(start=now - timedelta(days=7), end=now, label="7d")
        service = StatsService(db, rollup_repo=rollup_repo)
        result = await service.get_grouped_timeseries(
            time_range, bucket="hour", group_by="provider_model"
        )
        assert result["source"] == "empty"
        assert result["degraded_reason"] == "rollup_empty"
        assert result["points"] == []

    @pytest.mark.asyncio()
    async def test_empty_rollups_1h_falls_back_to_raw(self, db: Database) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "grp_1h_acct")
        await _insert_model(db, "model_a")
        async with db.transaction():
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms
                ) VALUES (?, ?, ?, datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'), 'completed', 10, 20, 5, 10.0)
                """,
                (account_id, "model_a", "provider_a"),
            )
        now = datetime.now(UTC)
        time_range = TimeRange(start=now - timedelta(hours=1), end=now, label="1h")
        service = StatsService(db, rollup_repo=rollup_repo)
        result = await service.get_grouped_timeseries(
            time_range, bucket="hour", group_by="provider_model"
        )
        assert result["source"] == "raw"
        assert result["degraded_reason"] == "rollup_empty"

    @pytest.mark.asyncio()
    async def test_rollup_populated_returns_source_rollup(self, db: Database) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "grp_rollup_acct")
        await _insert_model(db, "model_a")
        await _flush_events(
            db,
            rollup_repo,
            [
                UsageMetricEvent(
                    timestamp=datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
                    provider_id="provider_a",
                    model_id="model_a",
                    account_id=account_id,
                    protocol="openai",
                    streamed=False,
                    status="completed",
                    retry_count=0,
                    input_tokens=10,
                    output_tokens=20,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    reasoning_tokens=0,
                    thinking_characters=0,
                    cost_microdollars=0,
                    bytes_received=0,
                    bytes_emitted=0,
                    latency_ms=10,
                    first_byte_ms=None,
                ),
            ],
        )
        time_range = TimeRange(
            start=datetime(2025, 1, 1), end=datetime(2099, 12, 31), label="custom"
        )
        service = StatsService(db, rollup_repo=rollup_repo)
        result = await service.get_grouped_timeseries(
            time_range, bucket="hour", group_by="provider_model"
        )
        assert result["source"] == "rollup"
        assert result["degraded_reason"] == "none"
        assert len(result["points"]) > 0


# ---------------------------------------------------------------------------
# Part B.4: Telemetry records per-stage timings
# ---------------------------------------------------------------------------


class TestTelemetryPerStageTimings:
    """DashboardTelemetry records and ranks per-stage timings."""

    def test_record_stage_accumulates(self) -> None:
        t = DashboardTelemetry()
        t.record_stage("overview", "account_stats", 10.0)
        t.record_stage("overview", "account_stats", 20.0)
        snap = t.stage_snapshot()
        stages = snap["slow_stages"]
        assert len(stages) == 1
        assert stages[0]["page"] == "overview"
        assert stages[0]["stage"] == "account_stats"
        assert stages[0]["sample_count"] == 2
        assert stages[0]["p95_ms"] > 0

    def test_slow_stages_ranked_by_p95(self) -> None:
        t = DashboardTelemetry()
        t.record_stage("overview", "fast_stage", 1.0)
        t.record_stage("overview", "slow_stage", 50.0)
        t.record_stage("overview", "medium_stage", 25.0)
        snap = t.stage_snapshot()
        stages = snap["slow_stages"]
        assert len(stages) == 3
        assert stages[0]["stage"] == "slow_stage"
        assert stages[1]["stage"] == "medium_stage"
        assert stages[2]["stage"] == "fast_stage"

    def test_multiple_pages_tracked(self) -> None:
        t = DashboardTelemetry()
        t.record_stage("overview", "account_stats", 5.0)
        t.record_stage("timeseries", "timeseries_flat", 25.0)
        snap = t.stage_snapshot()
        pages = {s["page"] for s in snap["slow_stages"]}
        assert pages == {"overview", "timeseries"}


# ---------------------------------------------------------------------------
# Part B.5: Cache TTLs differ by namespace
# ---------------------------------------------------------------------------


class TestCacheTTLDiffersByNamespace:
    """Dashboard cache TTLs differ by namespace (Phase 4 regression guard)."""

    @pytest.mark.asyncio()
    async def test_timeseries_24h_longer_ttl_than_1h(self, db: Database) -> None:
        from eggpool.stats.service import _dashboard_cache_ttl

        ttl_24h = _dashboard_cache_ttl("timeseries", "24h")
        ttl_1h = _dashboard_cache_ttl("timeseries", "1h")
        assert ttl_24h > ttl_1h
        assert ttl_24h >= 60.0
        assert ttl_1h == 30.0

    def test_grouped_timeseries_24h_longer_ttl(self) -> None:
        from eggpool.stats.service import _dashboard_cache_ttl

        ttl_24h = _dashboard_cache_ttl("grouped_timeseries", "24h")
        ttl_1h = _dashboard_cache_ttl("grouped_timeseries", "1h")
        assert ttl_24h > ttl_1h


# ---------------------------------------------------------------------------
# Part B.6: Chart hydration doesn't stack intervals
# ---------------------------------------------------------------------------


class TestChartHydrationIntervalCleanup:
    """dashboard.js clears prior interval handles at namespace scope."""

    @staticmethod
    def _load_js() -> str:
        from pathlib import Path

        js_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "eggpool"
            / "dashboard"
            / "static"
            / "dashboard.js"
        )
        return js_path.read_text(encoding="utf-8")

    def test_init_chart_loading_shells_clears_prior_handles(self) -> None:
        js = self._load_js()
        start = js.index("namespace.initChartLoadingShells")
        block = js[start : start + 4000]
        assert "namespace.__chartHydrationHandles" in block
        assert "window.clearInterval" in block

    def test_clears_before_setting_new_interval(self) -> None:
        js = self._load_js()
        start = js.index("namespace.initChartLoadingShells")
        block = js[start : start + 4000]
        assert "if (namespace.__chartHydrationHandles[canvasId])" in block
        assert (
            "window.clearInterval(namespace.__chartHydrationHandles[canvasId])" in block
        )

    def test_deduplicates_inflight_fetches(self) -> None:
        js = self._load_js()
        start = js.index("namespace.initChartLoadingShells")
        block = js[start : start + 4000]
        assert "namespace.__chartHydrationInflight" in block
        assert "__chartHydrationInflight[decoded]" in block


# ---------------------------------------------------------------------------
# Part B.7: No-JS fallback still has the data island
# ---------------------------------------------------------------------------


class TestNoJSFallback:
    """No-JS fallback retains the data island inside <noscript>."""

    def test_progressive_overview_noscript_has_island(self) -> None:
        timeseries = [
            {"bucket": "2024-01-01 12:00:00", "request_count": 3, "error_count": 1}
        ]
        html = render_overview(
            overview={
                "summary": {"total_requests": 3},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            timeseries=timeseries,
            progressive_timeseries=True,
        )
        noscript_start = html.index("<noscript>")
        noscript_end = html.index("</noscript>", noscript_start)
        noscript_block = html[noscript_start:noscript_end]
        assert 'id="timeseries-initial-data"' in noscript_block
        assert 'type="application/json"' in noscript_block

    def test_non_progressive_overview_has_island_outside_noscript(self) -> None:
        timeseries = [
            {"bucket": "2024-01-01 12:00:00", "request_count": 3, "error_count": 1}
        ]
        html = render_overview(
            overview={
                "summary": {"total_requests": 3},
                "imbalance": {"imbalance_ratio": 0.0},
            },
            accounts=[],
            timeseries=timeseries,
        )
        assert 'id="timeseries-initial-data"' in html
        assert "2024-01-01 12:00:00" in html
        noscript_count = html.count("<noscript>")
        if noscript_count > 0:
            noscript_start = html.index("<noscript>")
            before_noscript = html[:noscript_start]
            assert 'id="timeseries-initial-data"' in before_noscript
