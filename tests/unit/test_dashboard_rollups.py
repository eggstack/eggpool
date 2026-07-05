"""Dashboard parity tests: rollup totals vs request-table totals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.rollup_repository import UsageRollupRepository
from eggpool.metrics.buffer import MetricsWriteCoalescer, UsageMetricEvent
from eggpool.models.config import MetricsConfig
from eggpool.stats import queries as stats_queries
from eggpool.stats.service import StatsService, TimeRange, format_dt

pytestmark = pytest.mark.dashboard

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "rollup_parity_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest.fixture()
def rollup_repo(db: Database) -> UsageRollupRepository:
    return UsageRollupRepository(db)


@pytest_asyncio.fixture()
async def seeded_db(db: Database) -> Database:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
            ("test_acct", "TEST_ENV", 1),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            ("model_a", "openai"),
        )
    async with db.transaction():
        for i in range(5):
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at, completed_at,
                    status, input_tokens, output_tokens, cost_microdollars,
                    upstream_latency_ms, bytes_received, bytes_emitted,
                    streamed, cache_read_tokens, cache_write_tokens,
                    reasoning_tokens
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    ?, ?,
                    datetime('now', ?),
                    datetime('now', ?),
                    'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    "test_acct",
                    "model_a",
                    "provider_a",
                    f"-{i + 1} hours",
                    f"-{i + 1} hours",
                    100 * (i + 1),
                    200 * (i + 1),
                    1000 * (i + 1),
                    100.0 + i * 10,
                    1000 * (i + 1),
                    500 * (i + 1),
                    1 if i % 2 == 0 else 0,
                    10 * (i + 1),
                    5 * (i + 1),
                    0,
                ),
            )
    return db


def _make_event(
    *,
    provider_id: str = "provider_a",
    model_id: str = "model_a",
    account_id: int | None = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    cost_microdollars: int = 0,
    bytes_received: int = 0,
    bytes_emitted: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    streamed: bool = False,
    first_byte_ms: int | None = None,
) -> UsageMetricEvent:
    return UsageMetricEvent(
        timestamp=datetime.now(UTC),
        provider_id=provider_id,
        model_id=model_id,
        account_id=account_id,
        protocol="openai",
        streamed=streamed,
        status="completed",
        retry_count=0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        thinking_characters=0,
        cost_microdollars=cost_microdollars,
        bytes_received=bytes_received,
        bytes_emitted=bytes_emitted,
        latency_ms=latency_ms,
        first_byte_ms=first_byte_ms,
    )


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
) -> None:
    config = MetricsConfig(
        write_mode="balanced",
        flush_interval_s=30,
        max_buffered_events=500,
        timeseries_bucket_s=3600,
    )
    coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)
    for event in events:
        coalescer.record_usage(event)
    result = await coalescer.flush(reason="rollup_test")
    assert result.rows_flushed > 0


class TestRollupSummaryParity:
    """Verify rollup summary matches request-table summary."""

    @pytest.mark.asyncio()
    async def test_rollup_summary_matches_requests_table(
        self, seeded_db: Database
    ) -> None:
        rollup_repo = UsageRollupRepository(seeded_db)
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=30,
            max_buffered_events=500,
            timeseries_bucket_s=3600,
        )
        coalescer = MetricsWriteCoalescer(
            config=config, db=seeded_db, rollup_repo=rollup_repo
        )

        for i in range(5):
            coalescer.record_usage(
                _make_event(
                    input_tokens=100 * (i + 1),
                    output_tokens=200 * (i + 1),
                    latency_ms=100 + i * 10,
                    cost_microdollars=1000 * (i + 1),
                    bytes_received=1000 * (i + 1),
                    bytes_emitted=500 * (i + 1),
                    cache_read_tokens=10 * (i + 1),
                    cache_write_tokens=5 * (i + 1),
                )
            )

        result = await coalescer.flush(reason="parity_test")
        assert result.rows_flushed > 0

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service_with_rollups = StatsService(seeded_db, rollup_repo=rollup_repo)
        rollup_summary = await service_with_rollups.get_summary(time_range)

        assert rollup_summary["total_requests"] == 5
        expected_in = sum(100 * (i + 1) for i in range(5))
        expected_out = sum(200 * (i + 1) for i in range(5))
        assert rollup_summary["total_input_tokens"] == expected_in
        assert rollup_summary["total_output_tokens"] == expected_out
        assert rollup_summary["total_tokens"] == expected_in + expected_out
        expected_cost = sum(1000 * (i + 1) for i in range(5))
        assert rollup_summary["total_cost_microdollars"] == expected_cost
        expected_br = sum(1000 * (i + 1) for i in range(5))
        expected_be = sum(500 * (i + 1) for i in range(5))
        assert rollup_summary["total_bytes_received"] == expected_br
        assert rollup_summary["total_bytes_emitted"] == expected_be

        expected_latency_sum = sum(100 + i * 10 for i in range(5))
        expected_tps = expected_out * 1000.0 / expected_latency_sum
        assert rollup_summary["tokens_per_second"] == pytest.approx(expected_tps)
        assert rollup_summary["avg_ttft_ms"] == 0.0

    @pytest.mark.asyncio()
    async def test_rollup_summary_ttft_and_throughput_for_streamed(
        self, seeded_db: Database
    ) -> None:
        """Streamed events with first_byte_ms must surface non-zero TTFT and tps."""
        rollup_repo = UsageRollupRepository(seeded_db)
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=30,
            max_buffered_events=500,
            timeseries_bucket_s=3600,
        )
        coalescer = MetricsWriteCoalescer(
            config=config, db=seeded_db, rollup_repo=rollup_repo
        )

        ttfts = [50, 100, 150, 200, 250]
        for i in range(5):
            coalescer.record_usage(
                _make_event(
                    input_tokens=100 * (i + 1),
                    output_tokens=200 * (i + 1),
                    latency_ms=100 + i * 10,
                    cost_microdollars=1000 * (i + 1),
                    streamed=True,
                    first_byte_ms=ttfts[i],
                )
            )

        result = await coalescer.flush(reason="ttft_parity_test")
        assert result.rows_flushed > 0

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service_with_rollups = StatsService(seeded_db, rollup_repo=rollup_repo)
        rollup_summary = await service_with_rollups.get_summary(time_range)

        expected_ttft_mean = sum(ttfts) / len(ttfts)
        expected_out = sum(200 * (i + 1) for i in range(5))
        expected_latency_sum = sum(100 + i * 10 for i in range(5))
        expected_tps = expected_out * 1000.0 / expected_latency_sum

        assert rollup_summary["avg_ttft_ms"] == pytest.approx(expected_ttft_mean)
        assert rollup_summary["tokens_per_second"] == pytest.approx(expected_tps)

    @pytest.mark.asyncio()
    async def test_rollup_summary_filters_by_account(self, seeded_db: Database) -> None:
        rollup_repo = UsageRollupRepository(seeded_db)
        other_id = await _insert_account(seeded_db, "other_acct")
        await _flush_events(
            seeded_db,
            rollup_repo,
            [
                _make_event(account_id=1, input_tokens=10, output_tokens=20),
                _make_event(account_id=other_id, input_tokens=100, output_tokens=200),
            ],
        )

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(seeded_db, rollup_repo=rollup_repo)
        summary = await service.get_summary(time_range, account_name="test_acct")

        assert summary["total_requests"] == 1
        assert summary["total_input_tokens"] == 10
        assert summary["total_output_tokens"] == 20

    @pytest.mark.asyncio()
    async def test_unknown_account_does_not_return_global_rollups(
        self, seeded_db: Database
    ) -> None:
        rollup_repo = UsageRollupRepository(seeded_db)
        await _flush_events(
            seeded_db,
            rollup_repo,
            [_make_event(account_id=1, input_tokens=10, output_tokens=20)],
        )

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(seeded_db, rollup_repo=rollup_repo)
        summary = await service.get_summary(time_range, account_name="missing")

        assert summary["total_requests"] == 0
        assert summary["total_input_tokens"] == 0


class TestRollupTimeseriesParity:
    """Verify rollup timeseries matches request-table timeseries."""

    @pytest.mark.asyncio()
    async def test_rollup_timeseries_matches_requests_table(
        self, seeded_db: Database
    ) -> None:
        rollup_repo = UsageRollupRepository(seeded_db)
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=30,
            max_buffered_events=500,
            timeseries_bucket_s=3600,
        )
        coalescer = MetricsWriteCoalescer(
            config=config, db=seeded_db, rollup_repo=rollup_repo
        )

        for i in range(5):
            coalescer.record_usage(
                _make_event(
                    input_tokens=100 * (i + 1),
                    output_tokens=200 * (i + 1),
                    latency_ms=100 + i * 10,
                    cost_microdollars=1000 * (i + 1),
                    bytes_received=1000 * (i + 1),
                    bytes_emitted=500 * (i + 1),
                )
            )

        await coalescer.flush(reason="parity_test")

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(seeded_db, rollup_repo=rollup_repo)
        timeseries = await service.get_timeseries(time_range, bucket="hour")

        assert len(timeseries) > 0
        total_requests = sum(b["request_count"] for b in timeseries)
        total_in = sum(b["input_tokens"] for b in timeseries)
        total_out = sum(b["output_tokens"] for b in timeseries)
        assert total_requests == 5
        expected_in = sum(100 * (i + 1) for i in range(5))
        expected_out = sum(200 * (i + 1) for i in range(5))
        assert total_in == expected_in
        assert total_out == expected_out

    @pytest.mark.asyncio()
    async def test_rollup_timeseries_filters_by_model(self, db: Database) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "test_acct")
        await _insert_model(db, "model_a")
        await _insert_model(db, "model_b")
        await _flush_events(
            db,
            rollup_repo,
            [
                _make_event(
                    account_id=account_id,
                    model_id="model_a",
                    input_tokens=10,
                    output_tokens=20,
                ),
                _make_event(
                    account_id=account_id,
                    model_id="model_b",
                    input_tokens=100,
                    output_tokens=200,
                ),
            ],
        )

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(db, rollup_repo=rollup_repo)
        result = await service.get_timeseries(time_range, model_id="model_a")

        assert result is not None
        assert sum(int(row["request_count"]) for row in result) == 1
        assert sum(int(row["input_tokens"]) for row in result) == 10
        assert sum(int(row["output_tokens"]) for row in result) == 20


class TestRollupGroupedTimeseriesParity:
    """Verify grouped rollup timeseries matches request-table data."""

    @pytest.mark.asyncio()
    async def test_rollup_grouped_timeseries_matches_requests_table(
        self, seeded_db: Database
    ) -> None:
        rollup_repo = UsageRollupRepository(seeded_db)
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=30,
            max_buffered_events=500,
            timeseries_bucket_s=3600,
        )
        coalescer = MetricsWriteCoalescer(
            config=config, db=seeded_db, rollup_repo=rollup_repo
        )

        for i in range(5):
            coalescer.record_usage(
                _make_event(
                    input_tokens=100 * (i + 1),
                    output_tokens=200 * (i + 1),
                    latency_ms=100 + i * 10,
                    cost_microdollars=1000 * (i + 1),
                )
            )

        await coalescer.flush(reason="parity_test")

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(seeded_db, rollup_repo=rollup_repo)
        grouped = await service.get_grouped_timeseries(
            time_range, bucket="hour", group_by="provider_model"
        )

        assert len(grouped["points"]) > 0
        total_requests = sum(p["request_count"] for p in grouped["points"])
        total_in = sum(p["input_tokens"] for p in grouped["points"])
        total_out = sum(p["output_tokens"] for p in grouped["points"])
        assert total_requests == 5
        expected_in = sum(100 * (i + 1) for i in range(5))
        expected_out = sum(200 * (i + 1) for i in range(5))
        assert total_in == expected_in
        assert total_out == expected_out

    @pytest.mark.asyncio()
    async def test_rollup_grouped_timeseries_filters_by_model_under_provider_group(
        self, db: Database
    ) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "test_acct")
        await _insert_model(db, "model_a")
        await _insert_model(db, "model_b")
        await _flush_events(
            db,
            rollup_repo,
            [
                _make_event(
                    account_id=account_id,
                    provider_id="provider_a",
                    model_id="model_a",
                    input_tokens=10,
                    output_tokens=20,
                ),
                _make_event(
                    account_id=account_id,
                    provider_id="provider_a",
                    model_id="model_b",
                    input_tokens=100,
                    output_tokens=200,
                ),
            ],
        )

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(db, rollup_repo=rollup_repo)
        grouped = await service.get_grouped_timeseries(
            time_range,
            bucket="hour",
            group_by="provider",
            model_id="model_a",
        )

        assert len(grouped["points"]) > 0
        assert sum(int(point["request_count"]) for point in grouped["points"]) == 1
        assert sum(int(point["input_tokens"]) for point in grouped["points"]) == 10
        assert sum(int(point["output_tokens"]) for point in grouped["points"]) == 20


class TestRollupBandwidthParity:
    """Verify rollup bandwidth matches request-table bandwidth."""

    @pytest.mark.asyncio()
    async def test_rollup_bandwidth_matches_requests_table(
        self, seeded_db: Database
    ) -> None:
        rollup_repo = UsageRollupRepository(seeded_db)
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=30,
            max_buffered_events=500,
            timeseries_bucket_s=3600,
        )
        coalescer = MetricsWriteCoalescer(
            config=config, db=seeded_db, rollup_repo=rollup_repo
        )

        for i in range(5):
            coalescer.record_usage(
                _make_event(
                    input_tokens=100 * (i + 1),
                    output_tokens=200 * (i + 1),
                    latency_ms=100 + i * 10,
                    bytes_received=1000 * (i + 1),
                    bytes_emitted=500 * (i + 1),
                )
            )

        await coalescer.flush(reason="parity_test")

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(seeded_db, rollup_repo=rollup_repo)
        summary = await service.get_summary(time_range)

        expected_br = sum(1000 * (i + 1) for i in range(5))
        expected_be = sum(500 * (i + 1) for i in range(5))
        assert summary["total_bytes_received"] == expected_br
        assert summary["total_bytes_emitted"] == expected_be


class TestEmptyRollupsFallback:
    """When rollups are empty but requests table has data, fall back."""

    @pytest.mark.asyncio()
    async def test_empty_rollups_fallback_to_requests(
        self, seeded_db: Database
    ) -> None:
        rollup_repo = UsageRollupRepository(seeded_db)
        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(seeded_db, rollup_repo=rollup_repo)
        summary = await service.get_summary(time_range)

        assert summary["total_requests"] == 5
        expected_in = sum(100 * (i + 1) for i in range(5))
        expected_out = sum(200 * (i + 1) for i in range(5))
        assert summary["total_input_tokens"] == expected_in
        assert summary["total_output_tokens"] == expected_out


@pytest_asyncio.fixture()
async def exactness_db(db: Database) -> Database:
    """Seed requests with mixed ``exactness`` values for backfill tests.

    The migration default for ``exactness`` is ``'unknown'``, so we
    explicitly overwrite it on each row.  Cost values are chosen so
    the cost aggregates are easy to verify by hand.
    """
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
            ("test_acct", "TEST_ENV", 1),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            ("model_a", "openai"),
        )
    rows = [
        ("exact", 100),
        ("exact", 200),
        ("derived", 300),
        ("estimated", 400),
        ("provider_reported", 500),
        ("unknown", 600),
    ]
    async with db.transaction():
        for i, (exactness, cost) in enumerate(rows):
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at, completed_at,
                    status, input_tokens, output_tokens, cost_microdollars,
                    upstream_latency_ms, exactness
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    ?, ?, datetime('now', ?), datetime('now', ?),
                    'completed', ?, ?, ?, ?, ?
                )
                """,
                (
                    "test_acct",
                    "model_a",
                    "provider_a",
                    f"-{i + 1} hours",
                    f"-{i + 1} hours",
                    10,
                    20,
                    cost,
                    100.0,
                    exactness,
                ),
            )
    return db


class TestRollupExactnessBackfill:
    """``usage_rollups`` does not retain ``exactness``, so the rollup summary
    must backfill exactness counters from the requests table."""

    @pytest.mark.asyncio()
    async def test_exactness_counts_backfilled_from_requests(
        self, exactness_db: Database
    ) -> None:
        rollup_repo = UsageRollupRepository(exactness_db)
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=30,
            max_buffered_events=500,
            timeseries_bucket_s=3600,
        )
        coalescer = MetricsWriteCoalescer(
            config=config, db=exactness_db, rollup_repo=rollup_repo
        )
        # One completed event per seeded row keeps the rollup non-empty so
        # ``get_summary_from_rollups`` is taken (and not the live fallback).
        for _ in range(6):
            coalescer.record_usage(
                _make_event(
                    input_tokens=10,
                    output_tokens=20,
                    latency_ms=100,
                    cost_microdollars=100,
                )
            )
        result = await coalescer.flush(reason="exactness_parity_test")
        assert result.rows_flushed > 0

        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(exactness_db, rollup_repo=rollup_repo)
        summary = await service.get_summary(time_range)

        assert summary["exact_count"] == 2
        assert summary["derived_count"] == 1
        assert summary["partial_count"] == 0
        assert summary["estimated_count"] == 1
        assert summary["unknown_count"] == 1
        assert summary["provider_reported_count"] == 1
        assert summary["provider_reported_cost_microdollars"] == 500
        assert summary["estimated_cost_sum_microdollars"] == 400

    @pytest.mark.asyncio()
    async def test_exactness_zero_on_empty_window(self, db: Database) -> None:
        """No requests and no rollups -> all exactness counters zero."""
        rollup_repo = UsageRollupRepository(db)
        time_range = TimeRange(
            start=datetime.fromisoformat("2000-01-01"),
            end=datetime.fromisoformat("2099-12-31"),
            label="custom",
        )

        service = StatsService(db, rollup_repo=rollup_repo)
        summary = await service.get_summary(time_range)

        assert summary["exact_count"] == 0
        assert summary["derived_count"] == 0
        assert summary["partial_count"] == 0
        assert summary["estimated_count"] == 0
        assert summary["unknown_count"] == 0
        assert summary["provider_reported_count"] == 0
        assert summary["provider_reported_cost_microdollars"] == 0
        assert summary["estimated_cost_sum_microdollars"] == 0


class TestRollupBucketTimestampFormat:
    """Rollup ``bucket_start`` must match ``TimeRange.start_str/end_str``
    shape so lexicographic comparison against ``started_at`` is honest.

    Regression for the total-tokens stall: previously the coalescer
    wrote ``YYYY-MM-DDTHH:MM:SSZ`` while the time-range bounds used
    ``YYYY-MM-DD HH:MM:SS``; because ``T`` > `` `` lexicographically
    the same-day buckets compared greater than the end bound and were
    silently dropped.
    """

    @pytest.mark.asyncio()
    async def test_persisted_bucket_start_matches_format_dt(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        account_id = await _insert_account(db, "ts_acct")
        await _insert_model(db, "model_a")
        # Anchor the flush to a deterministic wall-clock minute so the
        # canonical shape has predictable values.
        anchor = datetime(2025, 6, 15, 12, 34, 56, tzinfo=UTC)
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=30,
            max_buffered_events=500,
            timeseries_bucket_s=60,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)
        coalescer.record_usage(
            UsageMetricEvent(
                timestamp=anchor,
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
                latency_ms=100,
                first_byte_ms=None,
            )
        )
        await coalescer.flush(reason="bucket_format_test")

        row = await db.fetch_one("SELECT bucket_start FROM usage_rollups LIMIT 1")
        assert row is not None
        bucket_start = str(row["bucket_start"])
        # Canonical shape — no 'T', no trailing 'Z'.
        assert "T" not in bucket_start
        assert not bucket_start.endswith("Z")
        # Same shape as TimeRange bound params so lex-comparison works.
        expected = format_dt(anchor.replace(second=0))
        assert bucket_start == expected

    @pytest.mark.asyncio()
    async def test_same_day_bucket_not_excluded_by_end_bound(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        """The original bug: a bucket at ``2025-06-15 12:00:00`` must
        be returned when the end bound is ``2025-06-15 12:34:56``.
        With the legacy ``T...Z`` shape the row compared greater than
        the bound and was silently filtered out.
        """
        account_id = await _insert_account(db, "ts_acct2")
        await _insert_model(db, "model_a")
        bucket_dt = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        events = [
            UsageMetricEvent(
                timestamp=bucket_dt + timedelta(seconds=15),
                provider_id="provider_a",
                model_id="model_a",
                account_id=account_id,
                protocol="openai",
                streamed=False,
                status="completed",
                retry_count=0,
                input_tokens=50,
                output_tokens=70,
                cache_read_tokens=0,
                cache_write_tokens=0,
                reasoning_tokens=0,
                thinking_characters=0,
                cost_microdollars=0,
                bytes_received=0,
                bytes_emitted=0,
                latency_ms=42,
                first_byte_ms=None,
            )
        ]
        await _flush_events(db, rollup_repo, events)

        # End bound covers the bucket and a few minutes beyond; the
        # same-day bucket must be returned by the summary query.
        time_range = TimeRange(
            start=bucket_dt - timedelta(hours=1),
            end=bucket_dt + timedelta(minutes=30),
            label="custom",
        )
        summary = await StatsService(db, rollup_repo=rollup_repo).get_summary(
            time_range
        )
        assert summary["total_requests"] == 1
        assert summary["total_input_tokens"] == 50
        assert summary["total_output_tokens"] == 70


class TestRollupFreshnessGuard:
    """``StatsService._get_summary_inner`` must fall back to the live
    ``requests`` table when the coalescer is stale, otherwise the
    dashboard under-reports the in-flight hour."""

    @pytest.mark.asyncio()
    async def test_stale_rollup_falls_back_to_live_requests(self, db: Database) -> None:
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "stale_acct")
        await _insert_model(db, "model_a")

        # Anchor a coalescer flush far enough in the past that the
        # freshness comparison prefers the live requests table.
        old_anchor = datetime.now(UTC) - timedelta(hours=3)
        await _flush_events(
            db,
            rollup_repo,
            [
                UsageMetricEvent(
                    timestamp=old_anchor,
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
                )
            ],
        )

        # Seed two fresh requests inside the same time window so the
        # live aggregation has fresher activity than the rollup row.
        async with db.transaction():
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms,
                    cache_read_tokens, cache_write_tokens, reasoning_tokens
                ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    account_id,
                    "model_a",
                    "provider_a",
                    format_dt(datetime.now(UTC) - timedelta(minutes=5)),
                    format_dt(datetime.now(UTC) - timedelta(minutes=5)),
                    100,
                    200,
                    0,
                    50.0,
                    0,
                    0,
                ),
            )
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms,
                    cache_read_tokens, cache_write_tokens, reasoning_tokens
                ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    account_id,
                    "model_a",
                    "provider_a",
                    format_dt(datetime.now(UTC) - timedelta(minutes=1)),
                    format_dt(datetime.now(UTC) - timedelta(minutes=1)),
                    300,
                    400,
                    0,
                    50.0,
                    0,
                    0,
                ),
            )

        # Window covers the recent activity but is too fresh to be
        # historic (end is "now", not 1h+ in the past).
        now = datetime.now(UTC)
        time_range = TimeRange(
            start=now - timedelta(hours=4),
            end=now + timedelta(minutes=1),
            label="custom",
        )

        service = StatsService(db, rollup_repo=rollup_repo)
        summary = await service.get_summary(time_range)

        # The two live requests must surface; if the freshness guard
        # had been bypassed we'd see only the single stale rollup row.
        assert summary["total_requests"] == 2
        assert summary["total_input_tokens"] == 400
        assert summary["total_output_tokens"] == 600

    @pytest.mark.asyncio()
    async def test_historic_window_trusts_rollup(self, db: Database) -> None:
        """Windows whose end is >= 1h in the past bypass the freshness
        check — the rollup is the only store."""
        rollup_repo = UsageRollupRepository(db)
        account_id = await _insert_account(db, "historic_acct")
        await _insert_model(db, "model_a")

        anchor = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
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
                    input_tokens=11,
                    output_tokens=22,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    reasoning_tokens=0,
                    thinking_characters=0,
                    cost_microdollars=0,
                    bytes_received=0,
                    bytes_emitted=0,
                    latency_ms=33,
                    first_byte_ms=None,
                )
            ],
        )

        # End bound is well in the past — historic window.
        end = datetime(2025, 1, 1, 1, 0, 0, tzinfo=UTC)
        time_range = TimeRange(
            start=end - timedelta(hours=2),
            end=end,
            label="custom",
        )

        summary = await StatsService(db, rollup_repo=rollup_repo).get_summary(
            time_range
        )
        assert summary["total_requests"] == 1
        assert summary["total_input_tokens"] == 11


class TestFreshnessHelpers:
    """Direct unit tests for the helper functions used by the guard."""

    @pytest.mark.asyncio()
    async def test_rollup_latest_bucket_start_within_bound(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        account_id = await _insert_account(db, "fresh_acct")
        await _insert_model(db, "model_a")
        anchor = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
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
                    input_tokens=1,
                    output_tokens=1,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    reasoning_tokens=0,
                    thinking_characters=0,
                    cost_microdollars=0,
                    bytes_received=0,
                    bytes_emitted=0,
                    latency_ms=1,
                    first_byte_ms=None,
                )
            ],
        )

        latest = await rollup_repo.latest_bucket_start(
            end=format_dt(anchor + timedelta(hours=1)),
            account_id=account_id,
        )
        assert latest == format_dt(anchor)

    @pytest.mark.asyncio()
    async def test_fetch_latest_started_at_filters_window(self, db: Database) -> None:
        account_id = await _insert_account(db, "fresh_acct2")
        await _insert_model(db, "model_a")
        async with db.transaction():
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms,
                    cache_read_tokens, cache_write_tokens, reasoning_tokens
                ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    account_id,
                    "model_a",
                    "provider_a",
                    "2025-06-15 11:00:00",
                    "2025-06-15 11:00:00",
                    1,
                    1,
                    0,
                    10.0,
                    0,
                    0,
                ),
            )
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms,
                    cache_read_tokens, cache_write_tokens, reasoning_tokens
                ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    account_id,
                    "model_a",
                    "provider_a",
                    "2025-06-15 12:00:00",
                    "2025-06-15 12:00:00",
                    1,
                    1,
                    0,
                    10.0,
                    0,
                    0,
                ),
            )
            # Outside the queried window.
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, started_at,
                    completed_at, status, input_tokens, output_tokens,
                    cost_microdollars, upstream_latency_ms,
                    cache_read_tokens, cache_write_tokens, reasoning_tokens
                ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    account_id,
                    "model_a",
                    "provider_a",
                    "2025-06-16 00:00:00",
                    "2025-06-16 00:00:00",
                    1,
                    1,
                    0,
                    10.0,
                    0,
                    0,
                ),
            )

        latest = await stats_queries.fetch_latest_started_at(
            db,
            "2025-06-15 10:00:00",
            "2025-06-15 13:00:00",
            account_id=account_id,
        )
        assert latest == "2025-06-15 12:00:00"
