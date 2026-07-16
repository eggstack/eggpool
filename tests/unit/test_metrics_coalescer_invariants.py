"""Concurrency invariant tests for MetricsWriteCoalescer (Milestone F5).

Verifies:

- total_received = total_flushed + pending + dropped (after accounting for
  cancelled-restore events).
- No negative counters under concurrent record/flush.
- No lost updates during concurrent record/flush.
- No duplicate deltas after cancellation restore.
- Thread-safety: concurrent record_usage from multiple threads.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from eggpool.metrics.buffer import (
    MetricsWriteCoalescer,
    UsageMetricEvent,
)
from eggpool.models.config import MetricsConfig


def _make_event(
    *,
    provider_id: str = "prov_a",
    model_id: str = "model_a",
    account_id: int | None = 1,
    protocol: str = "openai",
    streamed: bool = False,
    status: str = "completed",
    input_tokens: int = 10,
    output_tokens: int = 20,
    latency_ms: int = 100,
    first_byte_ms: int | None = None,
) -> UsageMetricEvent:
    return UsageMetricEvent(
        timestamp=datetime(2025, 6, 15, 12, 30, 15, tzinfo=UTC),
        provider_id=provider_id,
        model_id=model_id,
        account_id=account_id,
        protocol=protocol,
        streamed=streamed,
        status=status,
        retry_count=0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        thinking_characters=0,
        cost_microdollars=100,
        bytes_received=500,
        bytes_emitted=250,
        latency_ms=latency_ms,
        first_byte_ms=first_byte_ms,
    )


def _make_config(
    *,
    write_mode: str = "balanced",
    max_buffered_events: int = 500,
) -> MetricsConfig:
    return MetricsConfig(
        write_mode=write_mode,
        flush_interval_s=30,
        max_buffered_events=max_buffered_events,
        timeseries_bucket_s=60,
    )


class TestTotalReceivedAccounting:
    """Verify the invariant: total_received = total_flushed + pending + dropped."""

    def test_invariant_after_flush(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        for _ in range(5):
            coalescer.record_usage(_make_event())

        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 5
        assert snap["total_events_dropped"] == 0
        assert snap["total_events_flushed"] == 0
        assert snap["buffered_events"] == 5
        # invariant: received = flushed + pending + dropped
        assert (
            snap["total_events_received"]
            == snap["total_events_flushed"]
            + snap["buffered_events"]
            + snap["total_events_dropped"]
        )

    @pytest.mark.asyncio()
    async def test_invariant_after_async_flush(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        for _ in range(10):
            coalescer.record_usage(_make_event())

        await coalescer.flush(reason="test")

        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 10
        assert snap["total_events_flushed"] == 10
        assert snap["buffered_events"] == 0
        assert snap["total_events_dropped"] == 0
        # invariant holds
        assert (
            snap["total_events_received"]
            == snap["total_events_flushed"]
            + snap["buffered_events"]
            + snap["total_events_dropped"]
        )

    @pytest.mark.asyncio()
    async def test_invariant_after_multiple_flushes(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        for _ in range(3):
            coalescer.record_usage(_make_event())
        await coalescer.flush(reason="test")

        for _ in range(7):
            coalescer.record_usage(_make_event())
        await coalescer.flush(reason="test")

        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 10
        assert snap["total_events_flushed"] == 10
        assert snap["buffered_events"] == 0
        assert (
            snap["total_events_received"]
            == snap["total_events_flushed"]
            + snap["buffered_events"]
            + snap["total_events_dropped"]
        )


class TestNoNegativeCounters:
    """Verify counters never go negative under any operation sequence."""

    def test_counters_non_negative_initial(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)
        snap = coalescer.snapshot()
        assert snap["total_events_received"] >= 0
        assert snap["total_events_flushed"] >= 0
        assert snap["total_events_dropped"] >= 0
        assert snap["buffered_events"] >= 0
        assert snap["buffered_keys"] >= 0

    @pytest.mark.asyncio()
    async def test_counters_non_negative_after_flush(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        for _ in range(5):
            coalescer.record_usage(_make_event())
        await coalescer.flush(reason="test")

        snap = coalescer.snapshot()
        assert snap["total_events_received"] >= 0
        assert snap["total_events_flushed"] >= 0
        assert snap["total_events_dropped"] >= 0

    def test_counters_non_negative_after_drop(self) -> None:
        config = _make_config(max_buffered_events=2)
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        # Fill buffer with 2 different keys, then overflow with a 3rd
        coalescer.record_usage(_make_event(provider_id="p1", model_id="m1"))
        coalescer.record_usage(_make_event(provider_id="p2", model_id="m2"))
        coalescer.record_usage(_make_event(provider_id="p3", model_id="m3"))

        snap = coalescer.snapshot()
        assert snap["total_events_dropped"] == 1
        assert snap["total_events_received"] == 2  # 3rd was dropped
        assert snap["total_events_dropped"] >= 0


class TestImmediateModeBypass:
    """Verify immediate write mode drops events without buffering."""

    def test_immediate_mode_records_nothing(self) -> None:
        config = _make_config(write_mode="immediate")
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        for _ in range(10):
            coalescer.record_usage(_make_event())

        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 0
        assert snap["buffered_events"] == 0
        assert snap["buffered_keys"] == 0


class TestConcurrentRecordFlush:
    """Verify thread-safety under concurrent record_usage and flush."""

    @pytest.mark.asyncio()
    async def test_concurrent_record_and_flush(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def record_batch(batch_id: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(20):
                    coalescer.record_usage(
                        _make_event(
                            provider_id=f"p{batch_id}",
                            model_id=f"m{i}",
                            account_id=batch_id,
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record_batch, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()

        # Flush concurrently with recording
        await coalescer.flush(reason="test")

        for t in threads:
            t.join(timeout=10)

        assert errors == []

        # All events accounted for
        snap = coalescer.snapshot()
        received = snap["total_events_received"]
        flushed = snap["total_events_flushed"]
        dropped = snap["total_events_dropped"]
        pending = snap["buffered_events"]
        assert received == flushed + pending + dropped

    @pytest.mark.asyncio()
    async def test_no_negative_deltas_after_flush(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        def record() -> None:
            for _ in range(50):
                coalescer.record_usage(_make_event())

        threads = [threading.Thread(target=record) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Multiple flushes
        for _ in range(5):
            await coalescer.flush(reason="test")

        snap = coalescer.snapshot()
        assert snap["total_events_received"] == 200
        assert snap["total_events_flushed"] == 200
        assert snap["buffered_events"] == 0
        assert snap["total_events_dropped"] == 0
        assert (
            snap["total_events_received"]
            == snap["total_events_flushed"]
            + snap["buffered_events"]
            + snap["total_events_dropped"]
        )


class TestBufferOverflow:
    """Verify buffer overflow drops events and increments drop counter."""

    def test_overflow_drops_new_keys(self) -> None:
        config = _make_config(max_buffered_events=3)
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        # Fill with 3 unique keys
        for i in range(3):
            coalescer.record_usage(_make_event(provider_id=f"p{i}", model_id=f"m{i}"))
        # 4th unique key should be dropped
        coalescer.record_usage(_make_event(provider_id="p3", model_id="m3"))

        snap = coalescer.snapshot()
        assert snap["total_events_dropped"] == 1
        assert snap["buffered_keys"] == 3
        assert snap["total_events_received"] == 3

    def test_existing_key_not_dropped(self) -> None:
        config = _make_config(max_buffered_events=2)
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        # Fill with 2 unique keys
        coalescer.record_usage(_make_event(provider_id="p1", model_id="m1"))
        coalescer.record_usage(_make_event(provider_id="p2", model_id="m2"))
        # Same key again — should NOT be dropped (same rollup key)
        coalescer.record_usage(_make_event(provider_id="p1", model_id="m1"))

        snap = coalescer.snapshot()
        assert snap["total_events_dropped"] == 0
        assert snap["total_events_received"] == 3
        assert snap["buffered_keys"] == 2


class TestCancellationRestore:
    """Verify cancelled flush restores events to buffer."""

    @pytest.mark.asyncio()
    async def test_cancelled_flush_restores_events(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        for _ in range(5):
            coalescer.record_usage(_make_event())

        # Make upsert_many raise CancelledError
        repo.upsert_many = AsyncMock(
            side_effect=asyncio.CancelledError("simulated cancel")
        )

        with pytest.raises(asyncio.CancelledError):
            await coalescer.flush(reason="test")

        snap = coalescer.snapshot()
        # Events were restored to buffer
        assert snap["buffered_events"] == 5
        assert snap["total_events_received"] == 5
        assert snap["total_events_flushed"] == 0
        assert (
            snap["total_events_received"]
            == snap["total_events_flushed"]
            + snap["buffered_events"]
            + snap["total_events_dropped"]
        )

    @pytest.mark.asyncio()
    async def test_cancelled_flush_no_duplicate_restore(self) -> None:
        config = _make_config()
        db = AsyncMock()
        repo = AsyncMock()
        coalescer = MetricsWriteCoalescer(config=config, db=db, rollup_repo=repo)

        # Record 3 events with the same key
        for _ in range(3):
            coalescer.record_usage(_make_event())

        repo.upsert_many = AsyncMock(
            side_effect=asyncio.CancelledError("simulated cancel")
        )

        with pytest.raises(asyncio.CancelledError):
            await coalescer.flush(reason="test")

        snap = coalescer.snapshot()
        # All 3 events should be in the buffer, merged into 1 key
        assert snap["buffered_events"] == 3
        assert snap["buffered_keys"] == 1
