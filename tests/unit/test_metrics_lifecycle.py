"""Lifecycle tests for MetricsWriteCoalescer and RequestFinalizer integration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.rollup_repository import UsageRollupRepository
from eggpool.metrics.buffer import MetricsWriteCoalescer, UsageMetricEvent
from eggpool.models.config import MetricsConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "lifecycle_test.sqlite3"))
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
    provider_id: str = "test_provider",
    model_id: str = "test_model",
    account_id: int | None = 1,
    status: str = "completed",
    input_tokens: int = 10,
    output_tokens: int = 20,
    latency_ms: int = 100,
) -> UsageMetricEvent:
    return UsageMetricEvent(
        timestamp=datetime.now(UTC),
        provider_id=provider_id,
        model_id=model_id,
        account_id=account_id,
        protocol="openai",
        streamed=False,
        status=status,
        retry_count=0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        thinking_characters=0,
        cost_microdollars=0,
        bytes_received=0,
        bytes_emitted=0,
        latency_ms=latency_ms,
        first_byte_ms=None,
    )


class TestBackgroundFlushTask:
    """Tests for the background flush run() loop."""

    @pytest.mark.asyncio()
    async def test_background_flush_task_starts_for_balanced(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=1,
            max_buffered_events=500,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)
        stop_event = asyncio.Event()

        for _ in range(3):
            coalescer.record_usage(_make_event(input_tokens=5, output_tokens=10))

        async def stop_after_delay() -> None:
            await asyncio.sleep(1.5)
            stop_event.set()

        await asyncio.gather(coalescer.run(stop_event), stop_after_delay())

        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 3
        assert snap["total_events_flushed"] == 3
        assert snap["buffered_events"] == 0

    @pytest.mark.asyncio()
    async def test_background_flush_task_starts_for_low_wear(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        config = MetricsConfig(
            write_mode="low_wear",
            flush_interval_s=1,
            max_buffered_events=500,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)
        stop_event = asyncio.Event()

        for _ in range(3):
            coalescer.record_usage(_make_event())

        async def stop_after_delay() -> None:
            await asyncio.sleep(1.5)
            stop_event.set()

        await asyncio.gather(coalescer.run(stop_event), stop_after_delay())

        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 3
        assert snap["total_events_flushed"] == 3
        assert snap["buffered_events"] == 0

    @pytest.mark.asyncio()
    async def test_shutdown_flush(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=60,
            max_buffered_events=500,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)
        stop_event = asyncio.Event()

        for _ in range(5):
            coalescer.record_usage(_make_event(input_tokens=1, output_tokens=2))

        stop_event.set()
        await coalescer.run(stop_event)

        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 5
        assert snap["total_events_flushed"] == 5
        assert snap["buffered_events"] == 0
        assert snap["last_flush_rows"] > 0

    @pytest.mark.asyncio()
    async def test_shutdown_flush_with_timeout(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=1,
            max_buffered_events=500,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)

        for _ in range(3):
            coalescer.record_usage(_make_event(input_tokens=7, output_tokens=14))

        result = await asyncio.wait_for(coalescer.flush(reason="shutdown"), timeout=5.0)

        assert result.rows_flushed > 0
        snap = coalescer.snapshot()
        assert snap["buffered_events"] == 0


class TestFinalizerEmitsUsageEvent:
    """Tests for RequestFinalizer emitting UsageMetricEvent to coalescer."""

    @pytest.mark.asyncio()
    async def test_request_finalizer_emits_usage_event(self) -> None:
        coalescer = MagicMock(spec=MetricsWriteCoalescer)
        coalescer.record_usage = MagicMock()

        from eggpool.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
            RequestFinalizer,
        )

        db = AsyncMock(spec=Database)
        request_repo = AsyncMock()
        attempt_repo = AsyncMock()
        reservation_repo = AsyncMock()

        request_repo.finalize_if_pending = AsyncMock(return_value=True)

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            metrics_coalescer=coalescer,
        )

        selected = MagicMock()
        selected.db_request_id = 1
        selected.reservation_id = 10
        selected.attempt_id = 20
        selected.account_name = "test_acct"
        selected.provider_id = "test_provider"
        selected.model_id = "test_model"
        selected.account_id = 1
        selected.attempt_number = 1
        selected.estimated_microdollars = 500

        data = FinalizationData(
            outcome=FinalizationOutcome.COMPLETED,
            input_tokens=100,
            output_tokens=200,
            upstream_latency_ms=150,
        )

        transitioned = await finalizer.finalize(selected, data)

        assert transitioned is True
        coalescer.record_usage.assert_called_once()
        event = coalescer.record_usage.call_args[0][0]
        assert isinstance(event, UsageMetricEvent)
        assert event.input_tokens == 100
        assert event.output_tokens == 200
        assert event.model_id == "test_model"
        assert event.protocol == "openai"
        assert event.streamed is False

    @pytest.mark.asyncio()
    async def test_request_finalizer_emits_protocol_and_streaming_metadata(
        self,
    ) -> None:
        coalescer = MagicMock(spec=MetricsWriteCoalescer)
        coalescer.record_usage = MagicMock()

        from eggpool.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
            RequestFinalizer,
        )

        db = AsyncMock(spec=Database)
        request_repo = AsyncMock()
        attempt_repo = AsyncMock()
        reservation_repo = AsyncMock()

        request_repo.finalize_if_pending = AsyncMock(return_value=True)

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            metrics_coalescer=coalescer,
        )

        selected = MagicMock()
        selected.db_request_id = 1
        selected.reservation_id = 10
        selected.attempt_id = 20
        selected.account_name = "test_acct"
        selected.provider_id = "test_provider"
        selected.model_id = "claude-3"
        selected.account_id = 1
        selected.attempt_number = 1
        selected.estimated_microdollars = 500
        selected.protocol = "anthropic"
        selected.streamed = True

        data = FinalizationData(
            outcome=FinalizationOutcome.COMPLETED,
            input_tokens=100,
            output_tokens=200,
            upstream_latency_ms=150,
        )

        transitioned = await finalizer.finalize(selected, data)

        assert transitioned is True
        event = coalescer.record_usage.call_args[0][0]
        assert isinstance(event, UsageMetricEvent)
        assert event.protocol == "anthropic"
        assert event.streamed is True

    @pytest.mark.asyncio()
    async def test_request_finalizer_no_double_counting(self) -> None:
        coalescer = MagicMock(spec=MetricsWriteCoalescer)
        coalescer.record_usage = MagicMock()

        from eggpool.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
            RequestFinalizer,
        )

        db = AsyncMock(spec=Database)
        request_repo = AsyncMock()
        attempt_repo = AsyncMock()
        reservation_repo = AsyncMock()

        request_repo.finalize_if_pending = AsyncMock(return_value=False)

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            metrics_coalescer=coalescer,
        )

        selected = MagicMock()
        selected.db_request_id = 1
        selected.reservation_id = 10
        selected.attempt_id = 20
        selected.account_name = "test_acct"
        selected.provider_id = "test_provider"
        selected.model_id = "test_model"
        selected.account_id = 1
        selected.attempt_number = 1
        selected.estimated_microdollars = 500

        data = FinalizationData(
            outcome=FinalizationOutcome.COMPLETED,
            input_tokens=100,
            output_tokens=200,
            upstream_latency_ms=150,
        )

        transitioned = await finalizer.finalize(selected, data)

        assert transitioned is False
        coalescer.record_usage.assert_not_called()

    @pytest.mark.asyncio()
    async def test_request_finalizer_no_coalescer(self) -> None:
        from eggpool.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
            RequestFinalizer,
        )

        db = AsyncMock(spec=Database)
        request_repo = AsyncMock()
        attempt_repo = AsyncMock()
        reservation_repo = AsyncMock()

        request_repo.finalize_if_pending = AsyncMock(return_value=True)

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            metrics_coalescer=None,
        )

        selected = MagicMock()
        selected.db_request_id = 1
        selected.reservation_id = 10
        selected.attempt_id = 20
        selected.account_name = "test_acct"
        selected.provider_id = "test_provider"
        selected.model_id = "test_model"
        selected.account_id = 1
        selected.attempt_number = 1
        selected.estimated_microdollars = 500

        data = FinalizationData(
            outcome=FinalizationOutcome.COMPLETED,
            input_tokens=100,
            output_tokens=200,
            upstream_latency_ms=150,
        )

        transitioned = await finalizer.finalize(selected, data)
        assert transitioned is True


class TestFlushErrorBehavior:
    """Tests for flush behavior when errors occur."""

    @pytest.mark.asyncio()
    async def test_flush_preserves_data_after_error(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=60,
            max_buffered_events=500,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)

        for _ in range(3):
            coalescer.record_usage(_make_event(input_tokens=5, output_tokens=10))

        with patch.object(
            rollup_repo,
            "upsert_many",
            AsyncMock(side_effect=RuntimeError("disk full")),
        ):
            result = await coalescer.flush(reason="test_error")

        assert result.error_class == "RuntimeError"
        assert result.rows_flushed == 0
        snap = coalescer.snapshot()
        assert snap["buffered_events"] == 0
        assert snap["total_events_received"] == 3
        assert snap["total_events_flushed"] == 0
        assert snap["last_flush_error"] == "RuntimeError"

    @pytest.mark.asyncio()
    async def test_flush_empty_buffer_returns_immediately(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=60,
            max_buffered_events=500,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)

        result = await coalescer.flush(reason="empty")
        assert result.rows_flushed == 0
        assert result.duration_ms >= 0

    @pytest.mark.asyncio()
    async def test_coalescer_snapshot_fields(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        config = MetricsConfig(
            write_mode="balanced",
            flush_interval_s=30,
            max_buffered_events=500,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)

        snap = coalescer.snapshot()
        assert snap["write_mode"] == "balanced"
        assert snap["flush_interval_s"] == 30
        assert snap["buffered_keys"] == 0
        assert snap["buffered_events"] == 0
        assert snap["total_events_received"] == 0
        assert snap["total_events_dropped"] == 0
        assert snap["last_flush_ts"] is None

    @pytest.mark.asyncio()
    async def test_immediate_mode_does_not_buffer(
        self, db: Database, rollup_repo: UsageRollupRepository
    ) -> None:
        config = MetricsConfig(
            write_mode="immediate",
            flush_interval_s=60,
            max_buffered_events=500,
        )
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=rollup_repo)

        for _ in range(10):
            coalescer.record_usage(_make_event())

        snap = coalescer.snapshot()
        assert snap["buffered_events"] == 0
        assert snap["total_events_received"] == 0
