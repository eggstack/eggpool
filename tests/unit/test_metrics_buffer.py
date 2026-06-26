"""Tests for the MetricsWriteCoalescer and related buffer primitives."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.rollup_repository import UsageRollupRepository
from eggpool.metrics.buffer import (
    MetricsWriteCoalescer,
    UsageMetricEvent,
    _compute_bucket_start,
)
from eggpool.models.config import MetricsConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "buffer_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest.fixture()
def rollup_repo(db: Database) -> UsageRollupRepository:
    return UsageRollupRepository(db)


def _make_event(
    *,
    timestamp: datetime | None = None,
    provider_id: str = "prov_a",
    model_id: str = "model_a",
    account_id: int | None = 1,
    protocol: str = "openai",
    streamed: bool = False,
    status: str = "completed",
    retry_count: int = 0,
    input_tokens: int = 10,
    output_tokens: int = 20,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    thinking_characters: int = 0,
    cost_microdollars: int = 100,
    bytes_received: int = 500,
    bytes_emitted: int = 250,
    latency_ms: int = 100,
    first_byte_ms: int | None = None,
) -> UsageMetricEvent:
    if timestamp is None:
        timestamp = datetime(2025, 6, 15, 12, 30, 15, tzinfo=UTC)
    return UsageMetricEvent(
        timestamp=timestamp,
        provider_id=provider_id,
        model_id=model_id,
        account_id=account_id,
        protocol=protocol,
        streamed=streamed,
        status=status,
        retry_count=retry_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        thinking_characters=thinking_characters,
        cost_microdollars=cost_microdollars,
        bytes_received=bytes_received,
        bytes_emitted=bytes_emitted,
        latency_ms=latency_ms,
        first_byte_ms=first_byte_ms,
    )


def _make_config(
    *,
    write_mode: str = "balanced",
    flush_interval_s: int = 30,
    max_buffered_events: int = 500,
    timeseries_bucket_s: int = 60,
) -> MetricsConfig:
    return MetricsConfig(
        write_mode=write_mode,
        flush_interval_s=flush_interval_s,
        max_buffered_events=max_buffered_events,
        timeseries_bucket_s=timeseries_bucket_s,
    )


class TestComputeBucketStart:
    def test_rounds_down_to_60s_bucket(self) -> None:
        ts = datetime(2025, 6, 15, 12, 30, 45, tzinfo=UTC)
        result = _compute_bucket_start(ts, 60)
        assert result == "2025-06-15T12:30:00Z"

    def test_already_on_bucket_boundary(self) -> None:
        ts = datetime(2025, 6, 15, 12, 30, 0, tzinfo=UTC)
        result = _compute_bucket_start(ts, 60)
        assert result == "2025-06-15T12:30:00Z"

    def test_3600s_bucket(self) -> None:
        ts = datetime(2025, 6, 15, 12, 45, 30, tzinfo=UTC)
        result = _compute_bucket_start(ts, 3600)
        assert result == "2025-06-15T12:00:00Z"

    def test_86400s_bucket(self) -> None:
        ts = datetime(2025, 6, 15, 23, 59, 59, tzinfo=UTC)
        result = _compute_bucket_start(ts, 86400)
        assert result == "2025-06-15T00:00:00Z"

    def test_midnight_boundary(self) -> None:
        ts = datetime(2025, 6, 16, 0, 0, 0, tzinfo=UTC)
        result = _compute_bucket_start(ts, 86400)
        assert result == "2025-06-16T00:00:00Z"


class TestRecordUsageImmediateMode:
    def test_immediate_mode_is_noop(self) -> None:
        config = _make_config(write_mode="immediate")
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        event = _make_event()
        coalescer.record_usage(event)
        snap = coalescer.snapshot()
        assert snap["buffered_events"] == 0
        assert snap["total_events_received"] == 0


class TestRecordUsageBuffersEvents:
    def test_balanced_mode_buffers(self) -> None:
        config = _make_config(write_mode="balanced")
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        coalescer.record_usage(_make_event())
        coalescer.record_usage(_make_event(input_tokens=20, output_tokens=30))
        snap = coalescer.snapshot()
        assert snap["buffered_events"] == 2
        assert snap["total_events_received"] == 2

    def test_low_wear_mode_buffers(self) -> None:
        config = _make_config(write_mode="low_wear")
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        coalescer.record_usage(_make_event())
        snap = coalescer.snapshot()
        assert snap["buffered_events"] == 1


class TestRecordUsageDropsWhenFull:
    def test_drops_new_keys_when_full(self) -> None:
        config = _make_config(max_buffered_events=2)
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        coalescer.record_usage(_make_event(model_id="m1"))
        coalescer.record_usage(_make_event(model_id="m2"))
        coalescer.record_usage(_make_event(model_id="m3"))
        snap = coalescer.snapshot()
        assert snap["buffered_events"] == 2
        assert snap["total_events_dropped"] == 1

    def test_existing_key_not_dropped(self) -> None:
        config = _make_config(max_buffered_events=2)
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        coalescer.record_usage(_make_event(model_id="m1"))
        coalescer.record_usage(_make_event(model_id="m2"))
        coalescer.record_usage(_make_event(model_id="m1", input_tokens=50))
        snap = coalescer.snapshot()
        assert snap["buffered_events"] == 3
        assert snap["total_events_dropped"] == 0


class TestFlushEmptyBuffer:
    @pytest.mark.asyncio()
    async def test_flush_empty_returns_zero(self) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        result = await coalescer.flush()
        assert result.rows_flushed == 0
        assert result.duration_ms == 0
        assert result.error_class is None


class TestFlushWritesToRollups:
    @pytest.mark.asyncio()
    async def test_flush_calls_upsert_many(
        self, rollup_repo: UsageRollupRepository
    ) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=rollup_repo,
        )
        coalescer.record_usage(_make_event())
        result = await coalescer.flush()
        assert result.rows_flushed == 1

        rows = await rollup_repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["request_count"] == 1


class TestFlushClearsBuffer:
    @pytest.mark.asyncio()
    async def test_buffer_empty_after_flush(self) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        coalescer.record_usage(_make_event())
        await coalescer.flush()
        snap = coalescer.snapshot()
        assert snap["buffered_keys"] == 0
        assert snap["buffered_events"] == 0


class TestFlushErrorIsCaught:
    @pytest.mark.asyncio()
    async def test_upsert_error_returns_error_class(self) -> None:
        mock_repo = AsyncMock(spec=UsageRollupRepository)
        mock_repo.upsert_many.side_effect = RuntimeError("db failure")
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=mock_repo,
        )
        coalescer.record_usage(_make_event())
        result = await coalescer.flush()
        assert result.error_class == "RuntimeError"
        assert result.rows_flushed == 0


class TestSnapshotDiagnostics:
    def test_initial_snapshot(self) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        snap = coalescer.snapshot()
        assert snap["write_mode"] == "balanced"
        assert snap["flush_interval_s"] == 30
        assert snap["buffered_keys"] == 0
        assert snap["buffered_events"] == 0
        assert snap["total_events_received"] == 0
        assert snap["total_events_flushed"] == 0
        assert snap["total_events_dropped"] == 0
        assert snap["last_flush_ts"] is None

    @pytest.mark.asyncio()
    async def test_snapshot_after_record_and_flush(self) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=AsyncMock(spec=UsageRollupRepository),
        )
        coalescer.record_usage(_make_event())
        coalescer.record_usage(_make_event(model_id="m2"))
        await coalescer.flush()
        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 2
        assert snap["total_events_flushed"] == 2
        assert snap["last_flush_ts"] is not None
        assert snap["last_flush_rows"] == 2


class TestAggregationSameKey:
    @pytest.mark.asyncio()
    async def test_same_key_aggregates_counters(
        self, rollup_repo: UsageRollupRepository
    ) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=rollup_repo,
        )
        coalescer.record_usage(_make_event(input_tokens=10, output_tokens=20))
        coalescer.record_usage(_make_event(input_tokens=30, output_tokens=40))
        await coalescer.flush()

        rows = await rollup_repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["request_count"] == 2
        assert rows[0]["input_tokens"] == 40
        assert rows[0]["output_tokens"] == 60


class TestAggregationDifferentKeys:
    @pytest.mark.asyncio()
    async def test_different_keys_produce_separate_rows(
        self, rollup_repo: UsageRollupRepository
    ) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=rollup_repo,
        )
        coalescer.record_usage(_make_event(model_id="m1"))
        coalescer.record_usage(_make_event(model_id="m2"))
        await coalescer.flush()

        rows = await rollup_repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 2


class TestFirstByteMsNone:
    @pytest.mark.asyncio()
    async def test_none_first_byte_ms_does_not_corrupt_count(
        self, rollup_repo: UsageRollupRepository
    ) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=rollup_repo,
        )
        coalescer.record_usage(_make_event(first_byte_ms=None))
        coalescer.record_usage(_make_event(first_byte_ms=200))
        coalescer.record_usage(_make_event(first_byte_ms=None))
        await coalescer.flush()

        rows = await rollup_repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["first_byte_ms_count"] == 1
        assert rows[0]["first_byte_ms_sum"] == 200


class TestLatencyMinMaxTracking:
    @pytest.mark.asyncio()
    async def test_min_max_converge_correctly(
        self, rollup_repo: UsageRollupRepository
    ) -> None:
        config = _make_config()
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=rollup_repo,
        )
        coalescer.record_usage(_make_event(latency_ms=100))
        coalescer.record_usage(_make_event(latency_ms=50))
        coalescer.record_usage(_make_event(latency_ms=200))
        coalescer.record_usage(_make_event(latency_ms=75))
        await coalescer.flush()

        rows = await rollup_repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["latency_ms_min"] == 50
        assert rows[0]["latency_ms_max"] == 200


class TestRunPeriodicFlush:
    @pytest.mark.asyncio()
    async def test_run_flushes_periodically(self) -> None:
        config = _make_config(flush_interval_s=1)
        mock_repo = AsyncMock(spec=UsageRollupRepository)
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=mock_repo,
        )
        coalescer.record_usage(_make_event())

        stop_event = asyncio.Event()

        async def stop_after_delay() -> None:
            await asyncio.sleep(2.5)
            stop_event.set()

        asyncio.create_task(stop_after_delay())
        await coalescer.run(stop_event)

        snap = coalescer.snapshot()
        assert snap["total_events_flushed"] >= 1
        assert snap["last_flush_ts"] is not None


class TestRunShutdownFlush:
    @pytest.mark.asyncio()
    async def test_stop_event_triggers_final_flush(self) -> None:
        config = _make_config(flush_interval_s=600)
        mock_repo = AsyncMock(spec=UsageRollupRepository)
        coalescer = MetricsWriteCoalescer(
            config=config,
            db=AsyncMock(spec=Database),
            rollup_repo=mock_repo,
        )
        coalescer.record_usage(_make_event())

        stop_event = asyncio.Event()
        stop_event.set()
        await coalescer.run(stop_event)
        assert mock_repo.upsert_many.await_count == 1
