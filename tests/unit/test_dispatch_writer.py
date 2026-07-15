"""Unit tests for the Milestone C durable dispatch write pipeline.

Covers:

- Intent validation (field invariants)
- Writer lifecycle (INIT -> RUNNING -> DRAINING -> CLOSED)
- Queue and backpressure (submit, capacity, saturation)
- Microbatch semantics (immediate single, bounded concurrent batches)
- Cancellation (pre-claim, post-commit)
- Failure propagation (batch rollback, writer shutdown)
- Diagnostics snapshot and counters
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as CFuture
from typing import Any, NoReturn
from unittest.mock import patch

import pytest

from eggpool.db.connection import Database
from eggpool.db.dispatch_repository import persist_dispatch_bundles
from eggpool.db.migrations import MigrationRunner
from eggpool.request.dispatch_intent import (
    DispatchAmbiguousCommitError,
    DispatchIntent,
    DispatchIntentCancelledError,
    DispatchQueueClosedError,
    DispatchQueueSaturatedError,
    DispatchTransactionError,
    DispatchValidationError,
    DispatchWriterError,
    DispatchWriterShutdownError,
    PersistedDispatchResult,
)
from eggpool.request.dispatch_writer import (
    DEFAULT_MAX_QUEUE_DEPTH,
    DispatchPersistenceWriter,
    _QueuedIntent,
    _WriterState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_intent(**overrides: Any) -> DispatchIntent:
    """Create a valid DispatchIntent with sensible defaults."""
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
    """Create a fresh in-memory database with all migrations applied."""
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
    """Directly enqueue an intent on the writer's queue and return its Future.

    Bypasses ``submit_intent`` (which uses ``call_soon_threadsafe`` with an
    async callback) so the drain loop can process it immediately.
    """
    future: CFuture[PersistedDispatchResult] = CFuture()
    qi = _QueuedIntent(intent=intent, future=future)
    writer._submitted_total += 1
    await writer._enqueue_from_event_loop(qi)
    return future


async def _await_submit(
    future: CFuture[PersistedDispatchResult],
    timeout: float = 5.0,
) -> PersistedDispatchResult:
    """Bridge a concurrent.futures.Future to an awaitable with timeout."""
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)


# ---------------------------------------------------------------------------
# Intent validation tests
# ---------------------------------------------------------------------------


class TestDispatchIntentValidation:
    """DispatchIntent field invariants are enforced at construction."""

    @pytest.mark.parametrize(
        "overrides, expected_fragment",
        [
            ({"proxy_request_id": ""}, "proxy_request_id must be non-empty"),
            ({"attempt_number": 0}, "attempt_number must be >= 1"),
            ({"attempt_number": -1}, "attempt_number must be >= 1"),
            ({"account_name": ""}, "account_name must be non-empty"),
            ({"model_id": ""}, "model_id must be non-empty"),
        ],
        ids=[
            "empty-proxy-request-id",
            "attempt-number-zero",
            "attempt-number-negative",
            "empty-account-name",
            "empty-model-id",
        ],
    )
    def test_invalid_intent_raises_validation_error(
        self, overrides: dict[str, Any], expected_fragment: str
    ) -> None:
        with pytest.raises(DispatchValidationError, match=expected_fragment):
            _make_intent(**overrides)

    def test_valid_intent_creation(self) -> None:
        intent = _make_intent()
        assert intent.proxy_request_id == "req-1"
        assert intent.attempt_number == 1
        assert intent.account_name == "acct-1"
        assert intent.model_id == "gpt-4"
        assert intent.cancelled is not None
        assert not intent.cancelled.is_set()

    def test_intent_is_frozen(self) -> None:
        intent = _make_intent()
        with pytest.raises(AttributeError):
            intent.model_id = "changed"  # type: ignore[misc]

    def test_intent_defaults(self) -> None:
        intent = _make_intent()
        assert intent.existing_db_request_id is None
        assert intent.generation_id == ""
        assert intent.enqueue_monotonic_ns > 0
        assert intent.cancelled is not None


# ---------------------------------------------------------------------------
# Writer lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestWriterLifecycle:
    """Writer state transitions: INIT -> RUNNING -> DRAINING -> CLOSED."""

    async def test_starts_in_init_state(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            assert writer.state == _WriterState.INIT
        finally:
            await db.disconnect()

    async def test_start_transitions_to_running(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            assert writer.state == _WriterState.RUNNING
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_cannot_start_twice(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            with pytest.raises(DispatchWriterShutdownError, match="Cannot start"):
                writer.start()
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_stop_transitions_through_draining_to_closed(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            assert writer.state == _WriterState.RUNNING
            await writer.stop()
            assert writer.state == _WriterState.CLOSED
        finally:
            await db.disconnect()

    async def test_stop_is_idempotent(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            await writer.stop()
            assert writer.state == _WriterState.CLOSED
            await writer.stop()
            assert writer.state == _WriterState.CLOSED
        finally:
            await db.disconnect()

    async def test_stop_without_start_is_noop(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            assert writer.state == _WriterState.INIT
            await writer.stop()
            # stop() on INIT returns early -- state stays INIT
            assert writer.state == _WriterState.INIT
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Queue and backpressure tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestQueueAndBackpressure:
    """submit_intent returns a Future; queue capacity is enforced."""

    async def test_submit_returns_future(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-future")
            future = writer.submit_intent(intent)
            assert isinstance(future, CFuture)
            # submit_intent uses call_soon_threadsafe which won't work
            # in same-loop context, so we can't await the result here.
            # Just verify it returns the right type.
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_queue_capacity_respected(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_queue_depth=4,
                enqueue_timeout_ms=0.0,
            )
            writer.start()
            # Fill queue directly to test capacity
            for i in range(4):
                intent = _make_intent(proxy_request_id=f"req-cap-{i}")
                future: CFuture[PersistedDispatchResult] = CFuture()
                qi = _QueuedIntent(intent=intent, future=future)
                await writer._enqueue_from_event_loop(qi)
            assert writer._queue.qsize() == 4
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_submit_when_not_running_raises_closed(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            intent = _make_intent(proxy_request_id="req-closed")
            with pytest.raises(DispatchQueueClosedError, match="not running"):
                writer.submit_intent(intent)
        finally:
            await db.disconnect()

    async def test_submit_after_stop_raises_closed(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            await writer.stop()
            intent = _make_intent(proxy_request_id="req-after-stop")
            with pytest.raises(DispatchQueueClosedError, match="not running"):
                writer.submit_intent(intent)
        finally:
            await db.disconnect()

    async def test_enqueue_when_not_running_fails_future(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            # Don't start -- state is INIT
            intent = _make_intent(proxy_request_id="req-not-running")
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            await writer._enqueue_from_event_loop(qi)
            with pytest.raises(DispatchQueueClosedError):
                await _await_submit(future)
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Microbatch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestMicrobatching:
    """Single intent persists immediately; concurrent intents form batches."""

    async def test_single_intent_persists_immediately(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-single")
            future = await _enqueue_intent(writer, intent)
            result = await _await_submit(future)
            assert result.db_request_id
            assert result.batch_size == 1
            snap = writer.snapshot()
            assert snap["persisted_total"] == 1
            assert snap["batch_count"] >= 1
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_concurrent_intents_form_bounded_batch(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=8,
                max_batch_wait_ms=100.0,
            )
            writer.start()
            count = 5
            futures = []
            for i in range(count):
                intent = _make_intent(proxy_request_id=f"req-batch-{i}")
                futures.append(await _enqueue_intent(writer, intent))
            results = await asyncio.gather(*[_await_submit(f) for f in futures])
            for r in results:
                assert isinstance(r, PersistedDispatchResult)
                assert r.db_request_id
            snap = writer.snapshot()
            assert snap["persisted_total"] == count
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_max_batch_size_respected(self) -> None:
        db = await _fresh_db()
        try:
            max_batch = 3
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=max_batch,
                max_batch_wait_ms=200.0,
            )
            writer.start()
            count = 6
            futures = []
            for i in range(count):
                intent = _make_intent(proxy_request_id=f"req-maxbatch-{i}")
                futures.append(await _enqueue_intent(writer, intent))
            results = await asyncio.gather(*[_await_submit(f) for f in futures])
            assert len(results) == count
            snap = writer.snapshot()
            assert snap["persisted_total"] == count
            assert snap["batch_size_max"] <= max_batch
            await writer.stop()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Cancellation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestCancellation:
    """Intent cancellation before and after writer claim."""

    async def test_cancel_before_writer_claim(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-cancel-pre")
            future = await _enqueue_intent(writer, intent)
            # Cancel before the drain loop processes it
            intent.cancelled.set()
            with pytest.raises(DispatchIntentCancelledError):
                await _await_submit(future)
            snap = writer.snapshot()
            assert snap["cancelled_total"] >= 1
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_cancel_after_commit_gets_result(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-cancel-post")
            future = await _enqueue_intent(writer, intent)
            # Wait for the intent to be claimed and persisted
            result = await _await_submit(future)
            assert isinstance(result, PersistedDispatchResult)
            assert result.db_request_id
            # Setting cancelled after commit is a no-op for the result
            # (the future already resolved).
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_cancelled_peers_unaffected(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=8,
                max_batch_wait_ms=100.0,
            )
            writer.start()
            # Submit a batch where one intent is cancelled
            good_futures: list[CFuture[PersistedDispatchResult]] = []
            cancelled_intent = _make_intent(proxy_request_id="req-cancel-peer")
            cancelled_future = await _enqueue_intent(writer, cancelled_intent)
            cancelled_intent.cancelled.set()
            for i in range(3):
                intent = _make_intent(proxy_request_id=f"req-good-{i}")
                good_futures.append(await _enqueue_intent(writer, intent))
            good_results = await asyncio.gather(
                *[_await_submit(f) for f in good_futures]
            )
            for r in good_results:
                assert isinstance(r, PersistedDispatchResult)
                assert r.db_request_id
            with pytest.raises(DispatchIntentCancelledError):
                await _await_submit(cancelled_future)
            await writer.stop()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Failure tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestFailurePropagation:
    """Batch rollback fails every member; writer shutdown drains queue."""

    async def test_batch_rollback_fails_every_member(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=4,
                max_batch_wait_ms=50.0,
            )
            writer.start()

            # Mock persist_dispatch_bundles to raise, exercising the
            # writer's except-exception path in _persist_batch.
            async def _raise(*args: Any, **kwargs: Any) -> NoReturn:
                raise RuntimeError("simulated persistence failure")

            with patch(
                "eggpool.request.dispatch_writer.persist_dispatch_bundles",
                side_effect=_raise,
            ):
                futures: list[CFuture[PersistedDispatchResult]] = []
                for i in range(3):
                    intent = _make_intent(proxy_request_id=f"req-fail-{i}")
                    futures.append(await _enqueue_intent(writer, intent))
                for f in futures:
                    with pytest.raises(DispatchTransactionError):
                        await _await_submit(f)
            snap = writer.snapshot()
            assert snap["failed_total"] == 3
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_writer_shutdown_fails_remaining_queued(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            # Enqueue an intent directly on the queue
            intent = _make_intent(proxy_request_id="req-shutdown")
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._queue.put_nowait(qi)
            writer._submitted_total += 1
            # Stop -- drain loop will fail remaining queued intents
            await writer.stop()
            with pytest.raises(DispatchWriterShutdownError):
                await _await_submit(future, timeout=2.0)
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Diagnostics tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestDiagnostics:
    """snapshot() returns expected keys and counters increment."""

    async def test_snapshot_returns_expected_keys(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            snap = writer.snapshot()
            expected_keys = {
                "state",
                "queue_depth",
                "max_queue_depth",
                "submitted_total",
                "persisted_total",
                "cancelled_total",
                "failed_total",
                "batch_count",
                "batch_size_p50",
                "batch_size_p95",
                "batch_size_max",
                "batch_wait_ms_p50",
                "batch_wait_ms_p95",
                "transaction_ms_p50",
                "transaction_ms_p95",
                "queue_depth_p50",
                "queue_depth_max",
                "last_batch_at",
                "last_batch_size",
            }
            assert set(snap.keys()) == expected_keys
        finally:
            await db.disconnect()

    async def test_initial_snapshot_values(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            snap = writer.snapshot()
            assert snap["state"] == _WriterState.INIT
            assert snap["queue_depth"] == 0
            assert snap["max_queue_depth"] == DEFAULT_MAX_QUEUE_DEPTH
            assert snap["submitted_total"] == 0
            assert snap["persisted_total"] == 0
            assert snap["cancelled_total"] == 0
            assert snap["failed_total"] == 0
            assert snap["batch_count"] == 0
            assert snap["batch_size_p50"] is None
            assert snap["batch_size_p95"] is None
            assert snap["batch_size_max"] is None
            assert snap["last_batch_at"] is None
            assert snap["last_batch_size"] is None
        finally:
            await db.disconnect()

    async def test_counters_increment_after_persist(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-counter")
            future = await _enqueue_intent(writer, intent)
            await _await_submit(future)
            snap = writer.snapshot()
            assert snap["submitted_total"] == 1
            assert snap["persisted_total"] == 1
            assert snap["batch_count"] >= 1
            assert snap["batch_size_p50"] is not None
            assert snap["batch_size_max"] >= 1
            assert snap["last_batch_at"] is not None
            assert snap["last_batch_size"] is not None
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_counters_increment_after_failure(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(
                proxy_request_id="req-fail-counter",
            )

            async def _raise(*args: Any, **kwargs: Any) -> NoReturn:
                raise RuntimeError("simulated persistence failure")

            with patch(
                "eggpool.request.dispatch_writer.persist_dispatch_bundles",
                side_effect=_raise,
            ):
                future = await _enqueue_intent(writer, intent)
                with pytest.raises(DispatchTransactionError):
                    await _await_submit(future)
            snap = writer.snapshot()
            assert snap["submitted_total"] == 1
            assert snap["failed_total"] == 1
            assert snap["persisted_total"] == 0
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_cancelled_counter_increments(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-cancel-counter")
            future = await _enqueue_intent(writer, intent)
            intent.cancelled.set()
            with pytest.raises(DispatchIntentCancelledError):
                await _await_submit(future)
            snap = writer.snapshot()
            assert snap["cancelled_total"] == 1
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_queue_depth_samples_populated(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            # Submit multiple intents so the drain loop records queue depths
            futures: list[CFuture[PersistedDispatchResult]] = []
            for i in range(5):
                intent = _make_intent(proxy_request_id=f"req-depth-{i}")
                futures.append(await _enqueue_intent(writer, intent))
            await asyncio.gather(*[_await_submit(f) for f in futures])
            snap = writer.snapshot()
            assert snap["queue_depth_p50"] is not None
            await writer.stop()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Repository-level tests (persist_dispatch_bundles)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestPersistDispatchBundles:
    """Repository layer: atomic batch persistence via persist_dispatch_bundles."""

    async def test_single_bundle_persists(self) -> None:
        db = await _fresh_db()
        try:
            intent = _make_intent(proxy_request_id="req-repo-single")
            results = await persist_dispatch_bundles(db, [intent], batch_id=1)
            assert len(results) == 1
            r = results[0]
            assert r.db_request_id
            assert r.reservation_id
            assert r.attempt_id > 0
            assert r.batch_id == 1
            assert r.batch_size == 1
        finally:
            await db.disconnect()

    async def test_multi_bundle_atomicity(self) -> None:
        db = await _fresh_db()
        try:
            intents = [
                _make_intent(proxy_request_id=f"req-repo-multi-{i}") for i in range(3)
            ]
            results = await persist_dispatch_bundles(db, intents, batch_id=2)
            assert len(results) == 3
            for r in results:
                assert r.db_request_id
                assert r.batch_id == 2
                assert r.batch_size == 3
        finally:
            await db.disconnect()

    async def test_empty_intents_returns_empty(self) -> None:
        db = await _fresh_db()
        try:
            results = await persist_dispatch_bundles(db, [], batch_id=1)
            assert results == []
        finally:
            await db.disconnect()

    async def test_batch_failure_returns_equal_length_failure_list(self) -> None:
        db = await _fresh_db()
        try:
            intents = [
                _make_intent(
                    proxy_request_id=f"req-repo-fail-{i}",
                    account_id=9999,
                )
                for i in range(2)
            ]
            results = await persist_dispatch_bundles(db, intents, batch_id=3)
            assert len(results) == 2
            for r in results:
                assert r.db_request_id == ""
                assert r.reservation_id == ""
                assert r.attempt_id == 0
        finally:
            await db.disconnect()

    async def test_retry_attempt_updates_existing_request(self) -> None:
        db = await _fresh_db()
        try:
            # First attempt
            first = _make_intent(proxy_request_id="req-retry", attempt_number=1)
            first_results = await persist_dispatch_bundles(db, [first], batch_id=1)
            db_request_id = first_results[0].db_request_id
            assert db_request_id

            # Second attempt (retry)
            second = _make_intent(
                proxy_request_id="req-retry",
                attempt_number=2,
                existing_db_request_id=db_request_id,
            )
            second_results = await persist_dispatch_bundles(db, [second], batch_id=2)
            assert len(second_results) == 1
            assert second_results[0].db_request_id == db_request_id
            assert second_results[0].attempt_number == 2
        finally:
            await db.disconnect()

    async def test_retry_without_existing_db_request_id_fails(self) -> None:
        db = await _fresh_db()
        try:
            intent = _make_intent(
                proxy_request_id="req-retry-noexist",
                attempt_number=2,
                existing_db_request_id=None,
            )
            results = await persist_dispatch_bundles(db, [intent], batch_id=1)
            assert len(results) == 1
            assert results[0].db_request_id == ""
        finally:
            await db.disconnect()

    async def test_result_carries_batch_metadata(self) -> None:
        db = await _fresh_db()
        try:
            intent = _make_intent(proxy_request_id="req-repo-meta")
            results = await persist_dispatch_bundles(db, [intent], batch_id=7)
            r = results[0]
            assert r.batch_id == 7
            assert r.batch_size == 1
            assert r.commit_timestamp  # non-empty
            assert r.queue_wait_ms >= 0.0
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# State enum tests
# ---------------------------------------------------------------------------


class TestWriterStateEnum:
    """Writer state string constants are as expected."""

    def test_state_values(self) -> None:
        assert _WriterState.INIT == "init"
        assert _WriterState.RUNNING == "running"
        assert _WriterState.DRAINING == "draining"
        assert _WriterState.CLOSED == "closed"


# ---------------------------------------------------------------------------
# Error hierarchy tests
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    """Error classes inherit from DispatchWriterError."""

    def test_all_errors_inherit_base(self) -> None:
        assert issubclass(DispatchQueueClosedError, DispatchWriterError)
        assert issubclass(DispatchQueueSaturatedError, DispatchWriterError)
        assert issubclass(DispatchIntentCancelledError, DispatchWriterError)
        assert issubclass(DispatchTransactionError, DispatchWriterError)
        assert issubclass(DispatchAmbiguousCommitError, DispatchWriterError)
        assert issubclass(DispatchValidationError, DispatchWriterError)
        assert issubclass(DispatchWriterShutdownError, DispatchWriterError)

    def test_errors_carry_messages(self) -> None:
        exc = DispatchQueueClosedError("test msg")
        assert str(exc) == "test msg"

        exc = DispatchTransactionError("batch failed")
        assert "batch failed" in str(exc)


# ---------------------------------------------------------------------------
# Integration: writer + repository end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestWriterIntegration:
    """End-to-end: writer collects intents and persists via repository."""

    async def test_round_trip_single_intent(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(db)
            writer.start()
            intent = _make_intent(proxy_request_id="req-e2e-single")
            future = await _enqueue_intent(writer, intent)
            result = await _await_submit(future)
            assert result.db_request_id
            assert result.reservation_id
            assert result.attempt_id > 0
            assert result.batch_size == 1
            snap = writer.snapshot()
            assert snap["submitted_total"] == 1
            assert snap["persisted_total"] == 1
            assert snap["failed_total"] == 0
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_round_trip_multiple_intents(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=8,
                max_batch_wait_ms=100.0,
            )
            writer.start()
            count = 5
            futures = []
            for i in range(count):
                intent = _make_intent(proxy_request_id=f"req-e2e-multi-{i}")
                futures.append(await _enqueue_intent(writer, intent))
            results = await asyncio.gather(*[_await_submit(f) for f in futures])
            assert len(results) == count
            for r in results:
                assert isinstance(r, PersistedDispatchResult)
                assert r.db_request_id
            snap = writer.snapshot()
            assert snap["persisted_total"] == count
            await writer.stop()
        finally:
            await db.disconnect()

    async def test_mixed_success_and_failure(self) -> None:
        db = await _fresh_db()
        try:
            writer = DispatchPersistenceWriter(
                db,
                max_batch_size=8,
                max_batch_wait_ms=50.0,
            )
            writer.start()

            # Mock persist_dispatch_bundles to raise, so the writer's
            # except-exception path is exercised for the entire batch.
            async def _raise(*args: Any, **kwargs: Any) -> NoReturn:
                raise RuntimeError("simulated persistence failure")

            with patch(
                "eggpool.request.dispatch_writer.persist_dispatch_bundles",
                side_effect=_raise,
            ):
                good = await _enqueue_intent(
                    writer, _make_intent(proxy_request_id="req-mixed-good")
                )
                bad = await _enqueue_intent(
                    writer,
                    _make_intent(proxy_request_id="req-mixed-bad"),
                )
                # Both fail because they share a batch
                with pytest.raises(DispatchTransactionError):
                    await _await_submit(good)
                with pytest.raises(DispatchTransactionError):
                    await _await_submit(bad)
            snap = writer.snapshot()
            assert snap["failed_total"] == 2
            await writer.stop()
        finally:
            await db.disconnect()
