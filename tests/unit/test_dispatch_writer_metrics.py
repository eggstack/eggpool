"""Dispatch writer metric semantics and sample counts.

Verifies that:

- ``transaction_ms`` is sampled once per batch, not per result.
- ``batch_size`` is sampled once per batch.
- ``enqueue_wait_ms``, ``result_delivery_ms``, and ``intent_end_to_end_ms``
  are recorded with correct semantics.
- Failed batches and failed intents are counted separately.
- Cancellation stage counters are exact.
- All sample storage is bounded by ``sample_window``.
- Snapshot p95 remains stable after many batches.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as CFuture
from typing import Any, NoReturn
from unittest.mock import patch

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.request.dispatch_intent import (
    DispatchIntent,
    DispatchIntentCancelledError,
    DispatchTransactionError,
    PersistedDispatchResult,
)
from eggpool.request.dispatch_writer import (
    DEFAULT_SAMPLE_WINDOW,
    DispatchPersistenceWriter,
    _QueuedIntent,
)

# ---------------------------------------------------------------------------
# Helpers (shared with test_dispatch_writer.py)
# ---------------------------------------------------------------------------


def _make_intent(**overrides: Any) -> DispatchIntent:
    defaults: dict[str, Any] = {
        "proxy_request_id": "req-1",
        "attempt_number": 1,
        "account_id": 1,
        "account_name": "acct-1",
        "provider_id": "openai",
        "model_id": "gpt-4",
        "protocol": "openai",
        "streamed": False,
        "estimated_tokens": 100,
        "estimated_microdollars": 1_000,
        "started_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return DispatchIntent(**defaults)


async def _fresh_db() -> Database:
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) "
            "VALUES (?, ?, 1, 1.0, ?)",
            ("acct-1", "TEST_KEY", "openai"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    return db


async def _enqueue_intent(
    writer: DispatchPersistenceWriter,
    intent: DispatchIntent,
) -> CFuture[PersistedDispatchResult]:
    future: CFuture[PersistedDispatchResult] = CFuture()
    qi = _QueuedIntent(intent=intent, future=future)
    writer._submitted_total += 1
    await writer._enqueue_from_event_loop(qi)
    return future


async def _await_submit(
    future: CFuture[PersistedDispatchResult],
    timeout: float = 5.0,
) -> PersistedDispatchResult:
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)


# ---------------------------------------------------------------------------
# Transaction and batch-size sample-count tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestOneSamplePerBatch:
    """transaction_ms and batch_size are sampled once per batch."""

    async def test_transaction_ms_sampled_once_per_batch(self) -> None:
        """After N batches, transaction_ms_samples has exactly N entries."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=1,
                max_batch_wait_ms=0.0,
                sample_window=DEFAULT_SAMPLE_WINDOW,
            )
            writer.start()
            for i in range(5):
                intent = _make_intent(proxy_request_id=f"req-tx-{i}")
                future = await _enqueue_intent(writer, intent)
                await _await_submit(future)
            snap = writer.snapshot()
            # batch_count should equal the number of batches
            assert snap["batch_count"] == 5
            assert snap["transaction_ms_p50"] is not None
            assert snap["transaction_ms_p95"] is not None
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_batch_size_sampled_once_per_batch(self) -> None:
        """After N batches, batch_sizes has exactly N entries."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=1,
                max_batch_wait_ms=0.0,
            )
            writer.start()
            for i in range(5):
                intent = _make_intent(proxy_request_id=f"req-bs-{i}")
                future = await _enqueue_intent(writer, intent)
                await _await_submit(future)
            snap = writer.snapshot()
            assert snap["batch_count"] == 5
            assert snap["batch_size_max"] == 1
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_batch_size_reflects_actual_batch_size(self) -> None:
        """A batch of 4 intents produces batch_size=4."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=8,
                max_batch_wait_ms=100.0,
            )
            writer.start()
            futures = []
            for i in range(4):
                intent = _make_intent(proxy_request_id=f"req-bulk-{i}")
                futures.append(await _enqueue_intent(writer, intent))
            await asyncio.gather(*[_await_submit(f) for f in futures])
            snap = writer.snapshot()
            assert snap["batch_size_max"] == 4
            await writer.stop()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# New metric semantics tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestNewMetricSemantics:
    """enqueue_wait_ms, result_delivery_ms, intent_end_to_end_ms are distinct."""

    async def test_enqueue_wait_ms_recorded_on_success(self) -> None:
        """enqueue_wait_ms is recorded when an intent is successfully enqueued."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-enqueue")
            future = await _enqueue_intent(writer, intent)
            await _await_submit(future)
            snap = writer.snapshot()
            assert snap["enqueue_wait_ms_p50"] is not None
            assert snap["enqueue_wait_ms_p50"] >= 0.0
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_result_delivery_ms_recorded_on_success(self) -> None:
        """result_delivery_ms is recorded after commit, before futures signaled."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-delivery")
            future = await _enqueue_intent(writer, intent)
            await _await_submit(future)
            snap = writer.snapshot()
            assert snap["result_delivery_ms_p50"] is not None
            assert snap["result_delivery_ms_p50"] >= 0.0
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_intent_end_to_end_ms_recorded_on_success(self) -> None:
        """intent_end_to_end_ms spans from submission to result delivery."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-e2e")
            future = await _enqueue_intent(writer, intent)
            await _await_submit(future)
            snap = writer.snapshot()
            assert snap["intent_end_to_end_ms_p50"] is not None
            assert snap["intent_end_to_end_ms_p50"] > 0.0
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_intent_end_to_end_ms_recorded_on_failure(self) -> None:
        """intent_end_to_end_ms is recorded even when the intent fails."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()

            async def _raise(*args: Any, **kwargs: Any) -> NoReturn:
                raise RuntimeError("simulated failure")

            with patch(
                "eggpool.request.dispatch_writer.persist_dispatch_bundles",
                side_effect=_raise,
            ):
                intent = _make_intent(proxy_request_id="req-e2e-fail")
                future = await _enqueue_intent(writer, intent)
                with pytest.raises(DispatchTransactionError):
                    await _await_submit(future)
            snap = writer.snapshot()
            assert snap["intent_end_to_end_ms_p50"] is not None
            assert snap["intent_end_to_end_ms_p50"] > 0.0
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_intent_end_to_end_ms_recorded_on_saturation(self) -> None:
        """intent_end_to_end_ms is recorded when the queue is full."""
        from eggpool.request.dispatch_intent import DispatchQueueSaturatedError

        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_queue_depth=1,
                enqueue_timeout_ms=0.0,
            )
            writer.start()
            # Fill the queue
            intent1 = _make_intent(proxy_request_id="req-sat-1")
            await _enqueue_intent(writer, intent1)
            # Now the queue is full — this should saturate immediately
            intent2 = _make_intent(proxy_request_id="req-sat-2")
            future2 = await _enqueue_intent(writer, intent2)
            with pytest.raises(DispatchQueueSaturatedError):
                await _await_submit(future2, timeout=2.0)
            snap = writer.snapshot()
            assert snap["intent_end_to_end_ms_p50"] is not None
            assert snap["saturation_count"] >= 1
            await writer.stop()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Failed batch/intent counting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestFailedBatchIntentCounting:
    """Failed batches and failed intents are counted separately."""

    async def test_failed_batch_increments_both_counters(self) -> None:
        """A failed batch increments failed_batches_total and failed_total."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=4,
                max_batch_wait_ms=50.0,
            )
            writer.start()

            async def _raise(*args: Any, **kwargs: Any) -> NoReturn:
                raise RuntimeError("simulated failure")

            with patch(
                "eggpool.request.dispatch_writer.persist_dispatch_bundles",
                side_effect=_raise,
            ):
                futures = []
                for i in range(3):
                    intent = _make_intent(proxy_request_id=f"req-fail-{i}")
                    futures.append(await _enqueue_intent(writer, intent))
                for f in futures:
                    with pytest.raises(DispatchTransactionError):
                        await _await_submit(f)
            snap = writer.snapshot()
            assert snap["failed_batches_total"] == 1
            assert snap["failed_total"] == 3
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_cancelled_before_claim_counted_separately(self) -> None:
        """Cancellation before claim increments cancelled_before_claim_total."""
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-cancel-pre")
            future = await _enqueue_intent(writer, intent)
            intent.cancelled.set()
            with pytest.raises(DispatchIntentCancelledError):
                await _await_submit(future)
            snap = writer.snapshot()
            assert snap["cancelled_total"] == 1
            assert snap["cancelled_before_claim_total"] == 1
            assert snap["cancelled_after_claim_total"] == 0
            assert snap["cancelled_after_commit_total"] == 0
            await writer.stop()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Bounded sample storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestBoundedSampleStorage:
    """All sample storage is bounded by sample_window."""

    async def test_sample_count_does_not_exceed_window(self) -> None:
        """After more batches than sample_window, retained count is bounded."""
        db = await _fresh_db()
        try:
            window = 10
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=1,
                max_batch_wait_ms=0.0,
                sample_window=window,
            )
            writer.start()
            for i in range(window + 20):
                intent = _make_intent(proxy_request_id=f"req-bound-{i}")
                future = await _enqueue_intent(writer, intent)
                await _await_submit(future)
            snap = writer.snapshot()
            # batch_count is the number of samples in the rolling window,
            # bounded by sample_window (not the total number of batches)
            assert snap["batch_count"] <= window
            assert snap["batch_count"] == window
            assert snap["sample_window"] == window
            # The p50/p95 are computed from bounded samples
            assert snap["transaction_ms_p50"] is not None
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_snapshot_p95_stable_after_many_batches(self) -> None:
        """Snapshot p95 remains stable after many batches (bounded work)."""
        import time

        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=1,
                max_batch_wait_ms=0.0,
                sample_window=100,
            )
            writer.start()
            # Process 100 batches
            for i in range(100):
                intent = _make_intent(proxy_request_id=f"req-perf-100-{i}")
                future = await _enqueue_intent(writer, intent)
                await _await_submit(future)
            t0 = time.monotonic()
            snap_100 = writer.snapshot()
            dt_100 = time.monotonic() - t0

            # Process 1000 more batches
            for i in range(1000):
                intent = _make_intent(proxy_request_id=f"req-perf-1k-{i}")
                future = await _enqueue_intent(writer, intent)
                await _await_submit(future)
            t0 = time.monotonic()
            snap_1k = writer.snapshot()
            dt_1k = time.monotonic() - t0

            # Snapshot time should not grow significantly
            # (bounded by sample_window, not by total batches)
            assert dt_1k < dt_100 * 50  # generous bound
            # Both snapshots have the same sample_window
            assert snap_100["sample_window"] == snap_1k["sample_window"]
            await writer.stop()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Snapshot schema stability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestSnapshotSchema:
    """Snapshot has a stable, test-pinned schema."""

    async def test_snapshot_has_all_expected_keys(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            snap = writer.snapshot()
            expected_keys = {
                "state",
                "queue_depth",
                "max_queue_depth",
                "occupancy_ratio",
                "submitted_total",
                "persisted_total",
                "cancelled_total",
                "cancelled_before_claim_total",
                "cancelled_after_claim_total",
                "cancelled_after_commit_total",
                "failed_total",
                "failed_batches_total",
                "reconciliation_total",
                "saturation_count",
                "submit_timeout_count",
                "batch_count",
                "batch_size_p50",
                "batch_size_p95",
                "batch_size_max",
                "transaction_ms_p50",
                "transaction_ms_p95",
                "queue_age_ms_p50",
                "queue_age_ms_p95",
                "batch_formation_wait_ms_p50",
                "batch_formation_wait_ms_p95",
                "enqueue_wait_ms_p50",
                "enqueue_wait_ms_p95",
                "result_delivery_ms_p50",
                "result_delivery_ms_p95",
                "intent_end_to_end_ms_p50",
                "intent_end_to_end_ms_p95",
                "queue_depth_p50",
                "queue_depth_max",
                "oldest_intent_age_ms",
                "last_batch_at",
                "last_batch_size",
                "sample_window",
            }
            assert set(snap.keys()) == expected_keys
        finally:
            await db.disconnect()

    async def test_empty_snapshot_returns_none_for_percentiles(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            snap = writer.snapshot()
            assert snap["transaction_ms_p50"] is None
            assert snap["transaction_ms_p95"] is None
            assert snap["batch_size_p50"] is None
            assert snap["enqueue_wait_ms_p50"] is None
            assert snap["result_delivery_ms_p50"] is None
            assert snap["intent_end_to_end_ms_p50"] is None
            assert snap["queue_age_ms_p50"] is None
            assert snap["batch_formation_wait_ms_p50"] is None
        finally:
            await db.disconnect()
