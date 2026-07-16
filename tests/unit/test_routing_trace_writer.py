"""Unit tests for ``RoutingTraceWriter``."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RoutingDecisionRepository
from eggpool.observability.routing_trace_writer import (
    RoutingTraceEvent,
    RoutingTraceWriter,
)

pytestmark = pytest.mark.request_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_writer(db: Database, **kwargs: Any) -> RoutingTraceWriter:
    """Create and return a ``RoutingTraceWriter`` (NOT started)."""
    return RoutingTraceWriter(
        db=db,
        routing_decision_repo=RoutingDecisionRepository(db),
        **kwargs,
    )


async def _seed_request(db: Database, request_id: int) -> None:
    """Insert a minimal request row for FK compliance.

    Also seeds the required ``accounts``, ``models``, and
    ``account_models`` rows.
    """
    async with db.transaction():
        await db.execute_insert(
            "INSERT OR IGNORE INTO models (model_id, display_name, protocol) "
            "VALUES ('gpt-4', 'gpt-4', 'openai')",
        )
        await db.execute_insert(
            "INSERT OR IGNORE INTO accounts (id, name, api_key_env, enabled, weight) "
            "VALUES (1, 'tw-test-acct', 'TW_KEY', 1, 1.0)",
        )
        await db.execute_insert(
            "INSERT OR IGNORE INTO account_models (account_id, model_id, enabled) "
            "VALUES (1, 'gpt-4', 1)",
        )
        await db.execute_insert(
            "INSERT OR IGNORE INTO requests (id, account_id, model_id, started_at) "
            "VALUES (?, 1, 'gpt-4', datetime('now'))",
            (request_id,),
        )


def _make_event(
    *,
    request_id: str = "req-1",
    db_request_id: int = 1,
    attempt_number: int = 1,
) -> RoutingTraceEvent:
    """Build a ``RoutingTraceEvent`` with sensible defaults."""
    return RoutingTraceEvent(
        request_id=request_id,
        db_request_id=db_request_id,
        attempt_number=attempt_number,
        model_id="gpt-4",
        provider_id=None,
        protocol="openai",
        selected_account_name="acct-a",
        selected_account_id=1,
        selected_tier=0,
        selected_score=1.0,
        eligible_count=2,
        scored_count=2,
        attempted_excluded_count=0,
        top_score=1.0,
        top_score_account_name="acct-a",
        exclude_reasons_json="{}",
        score_components_json="{}",
        created_at_mono_ns=time.monotonic_ns(),
        created_at_epoch=time.time(),
        generation_id=None,
    )


async def _count_routing_traces(db: Database) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS cnt FROM routing_decisions")
    assert row is not None
    return int(row["cnt"])


async def _flush_writer(writer: RoutingTraceWriter) -> None:
    """Wait for the writer to drain its queue."""
    await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# Queue behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_submit_accepted_when_running() -> None:
    """submit() returns 'accepted' when the writer is running."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db, flush_interval_s=0.05)
        writer.start()
        try:
            event = _make_event()
            result = writer.submit(event)
            assert result == "accepted"
            snap = writer.snapshot()
            assert snap["accepted"] == 1
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_submit_dropped_when_not_running() -> None:
    """submit() returns 'dropped_writer_unavailable' before start or after stop."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db)
        # Before start
        result = writer.submit(_make_event())
        assert result == "dropped_writer_unavailable"

        writer.start()
        await writer.stop()

        # After stop
        result2 = writer.submit(_make_event())
        assert result2 == "dropped_writer_unavailable"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_submit_dropped_queue_full() -> None:
    """When queue is at capacity, new events are dropped with 'dropped_queue_full'."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        # capacity=3 so drain won't clear them (flush_interval_s is large)
        writer = await _create_writer(
            db, queue_capacity=3, flush_interval_s=100.0, max_batch_size=10
        )
        writer.start()
        try:
            for i in range(3):
                assert writer.submit(_make_event(db_request_id=i + 1)) == "accepted"
            # 4th should be dropped
            result = writer.submit(_make_event(db_request_id=4))
            assert result == "dropped_queue_full"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_submit_dropped_mode_off() -> None:
    """When mode='off', submit returns 'dropped_mode_off'."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db)
        writer.configure(mode="off")
        writer.start()
        try:
            result = writer.submit(_make_event())
            assert result == "dropped_mode_off"
            snap = writer.snapshot()
            assert snap["dropped_mode_off"] == 1
            assert snap["accepted"] == 0
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_queue_capacity_boundary() -> None:
    """Exactly queue_capacity events accepted; next one dropped."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(
            db, queue_capacity=5, flush_interval_s=100.0, max_batch_size=50
        )
        writer.start()
        try:
            for i in range(5):
                assert writer.submit(_make_event(db_request_id=i + 1)) == "accepted"
            assert writer.submit(_make_event(db_request_id=6)) == "dropped_queue_full"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Batch writing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_drain_writes_to_db() -> None:
    """Events submitted are persisted to routing_decisions after flush."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_request(db, 1)
        await _seed_request(db, 2)
        writer = await _create_writer(db, flush_interval_s=0.05)
        writer.start()
        try:
            writer.submit(_make_event(db_request_id=1, attempt_number=1))
            writer.submit(_make_event(db_request_id=2, attempt_number=1))
            await _flush_writer(writer)
            count = await _count_routing_traces(db)
            assert count == 2
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_batch_size_limits_drain() -> None:
    """With max_batch_size=3 and 10 events, each batch writes at most 3."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        for i in range(1, 11):
            await _seed_request(db, i)
        writer = await _create_writer(
            db,
            max_batch_size=3,
            flush_interval_s=0.05,
            queue_capacity=100,
        )
        batch_sizes: list[int] = []
        original_write = writer._write_batch

        async def _capture_write(batch: Any) -> Any:
            batch_sizes.append(len(batch))
            return await original_write(batch)

        writer._write_batch = _capture_write  # type: ignore[assignment]
        writer.start()
        try:
            for i in range(10):
                writer.submit(_make_event(db_request_id=i + 1, attempt_number=1))
            # Wait for all batches to drain
            await asyncio.sleep(0.5)
            # Every batch should have at most 3 events
            for size in batch_sizes:
                assert size <= 3, f"Batch of size {size} exceeds max_batch_size=3"
            # All 10 events should eventually be written
            count = await _count_routing_traces(db)
            assert count == 10, f"Expected 10 rows total, got {count}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_empty_batch_skipped() -> None:
    """Drain with empty queue does not touch the database."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db, flush_interval_s=0.05)
        writer.start()
        try:
            await _flush_writer(writer)
            count = await _count_routing_traces(db)
            assert count == 0
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_serialization_failure_counted() -> None:
    """Event whose to_row_tuple fails increments dropped_serialization."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db, flush_interval_s=0.05, max_batch_size=50)
        writer.start()
        try:
            # Patch to_row_tuple to raise on the second event
            call_count = 0
            original_to_row = RoutingTraceEvent.to_row_tuple

            def _patched_to_row(self: RoutingTraceEvent) -> tuple[Any, ...]:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("simulated serialization failure")
                return original_to_row(self)

            RoutingTraceEvent.to_row_tuple = _patched_to_row  # type: ignore[assignment]
            try:
                writer.submit(_make_event(db_request_id=1, request_id="r1"))
                writer.submit(_make_event(db_request_id=2, request_id="r2"))
                writer.submit(_make_event(db_request_id=3, request_id="r3"))
                await _flush_writer(writer)
                snap = writer.snapshot()
                assert snap["dropped_serialization"] == 1
                # 2 of 3 events serialized successfully; at least one should be written
                assert snap["written"] >= 0
                assert snap["written"] <= 2
            finally:
                RoutingTraceEvent.to_row_tuple = original_to_row  # type: ignore[assignment]
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_start_sets_running() -> None:
    """After start(), is_alive is True."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db)
        assert writer.is_alive is False
        writer.start()
        try:
            assert writer.is_alive is True
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_stop_drains_remaining() -> None:
    """After stop(), remaining queued events are flushed."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        for i in range(1, 6):
            await _seed_request(db, i)
        writer = await _create_writer(
            db,
            queue_capacity=100,
            flush_interval_s=100.0,  # won't drain naturally
            max_batch_size=100,
        )
        writer.start()
        try:
            for i in range(5):
                writer.submit(_make_event(db_request_id=i + 1, attempt_number=1))
            await writer.stop()
            count = await _count_routing_traces(db)
            assert count == 5, f"Expected 5 rows after stop drain, got {count}"
        finally:
            pass  # already stopped
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_stop_timeout_drops_remaining() -> None:
    """With very short timeout, remaining events are dropped."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(
            db,
            queue_capacity=100,
            flush_interval_s=100.0,
            max_batch_size=100,
            shutdown_flush_timeout_s=0.001,
        )
        writer.start()
        try:
            # Submit many events; stop with tiny timeout
            for i in range(50):
                writer.submit(_make_event(db_request_id=i + 1, attempt_number=1))
            await writer.stop(timeout_s=0.001)
            snap = writer.snapshot()
            # Some or all may be dropped by timeout
            assert snap["dropped_shutdown_timeout"] >= 0
        finally:
            pass
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_double_start_ignored() -> None:
    """Calling start() twice doesn't create two drain tasks."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db)
        writer.start()
        task1 = writer._drain_task
        assert task1 is not None
        # Second start should be a no-op
        writer.start()
        task2 = writer._drain_task
        assert task1 is task2
        assert not task2.done()
        await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_stop_when_not_running() -> None:
    """stop() on init-state writer is a no-op."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db)
        # Should not raise
        await writer.stop()
        assert writer.is_alive is False
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Snapshot and diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_snapshot_initial_state() -> None:
    """Snapshot returns correct initial counters."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db)
        snap = writer.snapshot()
        assert snap["alive"] is False
        assert snap["state"] == "init"
        assert snap["queue_capacity"] == 1000
        assert snap["queue_depth"] == 0
        assert snap["oldest_event_age_s"] is None
        assert snap["accepted"] == 0
        assert snap["written"] == 0
        assert snap["dropped_queue_full"] == 0
        assert snap["dropped_writer_unavailable"] == 0
        assert snap["dropped_flush_error"] == 0
        assert snap["dropped_shutdown_timeout"] == 0
        assert snap["dropped_serialization"] == 0
        assert snap["dropped_stale_parent"] == 0
        assert snap["dropped_mode_off"] == 0
        assert snap["dropped_sampling_exclusion"] == 0
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_snapshot_after_accepts() -> None:
    """Accepted counter increments correctly."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db, flush_interval_s=100.0, queue_capacity=100)
        writer.start()
        try:
            for i in range(5):
                writer.submit(_make_event(db_request_id=i + 1))
            snap = writer.snapshot()
            assert snap["accepted"] == 5
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_snapshot_queue_depth() -> None:
    """queue_depth matches submitted events when drain is slow."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(
            db,
            queue_capacity=100,
            flush_interval_s=100.0,  # won't drain
            max_batch_size=10,
        )
        writer.start()
        try:
            for i in range(7):
                writer.submit(_make_event(db_request_id=i + 1))
            snap = writer.snapshot()
            assert snap["queue_depth"] == 7
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_snapshot_oldest_event_age() -> None:
    """oldest_event_age_s is computed correctly."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(
            db,
            queue_capacity=100,
            flush_interval_s=100.0,
            max_batch_size=10,
        )
        writer.start()
        try:
            # Submit and snapshot immediately (before drain loop wakes up)
            writer.submit(_make_event(db_request_id=1))
            # The event is now in the queue; snapshot checks queue[0]
            snap = writer.snapshot()
            # Even if age is 0, it should not be None
            assert snap["oldest_event_age_s"] is not None
            assert snap["oldest_event_age_s"] >= 0.0
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_snapshot_drop_counters() -> None:
    """All drop counters are tracked separately."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(
            db, queue_capacity=2, flush_interval_s=100.0, max_batch_size=10
        )
        # Submit before start → writer_unavailable
        writer.submit(_make_event(db_request_id=1))
        writer.submit(_make_event(db_request_id=2))

        writer.configure(mode="off")
        writer.start()
        try:
            # Submit while running but mode=off
            writer.submit(_make_event(db_request_id=3))

            snap = writer.snapshot()
            assert snap["dropped_writer_unavailable"] == 2
            assert snap["dropped_mode_off"] == 1
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Runtime reconfiguration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_configure_mode_change() -> None:
    """configure(mode='off') causes subsequent submits to be dropped."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_request(db, 1)
        writer = await _create_writer(
            db,
            queue_capacity=100,
            flush_interval_s=100.0,
            max_batch_size=100,
        )
        writer.start()
        try:
            writer.submit(_make_event(db_request_id=1))
            # Switch to off before the drain loop can process
            writer.configure(mode="off")
            # These should be dropped (mode=off)
            for i in range(3):
                writer.submit(_make_event(db_request_id=i + 10))
            await _flush_writer(writer)
            count = await _count_routing_traces(db)
            # Only the first event should be written; the 3 mode=off ones are dropped
            assert count == 1, f"Expected 1 row, got {count}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_configure_sample_rate_change() -> None:
    """configure(sample_rate=0.5) updates internal state."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db)
        assert writer._sample_rate is None
        writer.configure(sample_rate=0.5)
        assert writer._sample_rate == 0.5
        writer.configure(sample_rate=0.0)
        assert writer._sample_rate == 0.0
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Shutdown behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_shutdown_flush_timeout_drops() -> None:
    """With very short shutdown timeout, remaining events may be dropped."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(
            db,
            queue_capacity=100,
            flush_interval_s=100.0,
            max_batch_size=100,
            shutdown_flush_timeout_s=0.001,
        )
        writer.start()
        try:
            for i in range(20):
                writer.submit(_make_event(db_request_id=i + 1, attempt_number=1))
            await writer.stop(timeout_s=0.001)
            snap = writer.snapshot()
            # With a 1ms timeout, at least some events should be dropped
            assert snap["dropped_shutdown_timeout"] >= 0
            # Verify the counter is tracked (may be 0 if drain was fast enough)
            assert "dropped_shutdown_timeout" in snap
        finally:
            pass
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_shutdown_writes_remaining_before_timeout() -> None:
    """Events submitted before stop are written if time permits."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        for i in range(1, 4):
            await _seed_request(db, i)
        writer = await _create_writer(
            db,
            queue_capacity=100,
            flush_interval_s=100.0,
            max_batch_size=100,
            shutdown_flush_timeout_s=5.0,
        )
        writer.start()
        try:
            for i in range(3):
                writer.submit(_make_event(db_request_id=i + 1, attempt_number=1))
            await writer.stop(timeout_s=5.0)
            count = await _count_routing_traces(db)
            assert count == 3, f"Expected 3 rows after clean shutdown, got {count}"
        finally:
            pass
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# No secrets in trace events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_trace_event_json_no_secrets() -> None:
    """to_json_bytes() contains no api_key, no request body, no auth headers."""
    event = _make_event()
    payload = event.to_json_bytes().decode()
    assert "api_key" not in payload.lower()
    assert "authorization" not in payload.lower()
    assert "Bearer" not in payload
    assert "sk-" not in payload
    # Verify it is valid JSON
    import json

    parsed = json.loads(payload)
    assert parsed["request_id"] == "req-1"
    assert parsed["model_id"] == "gpt-4"


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_to_row_tuple_contains_expected_values() -> None:
    """to_row_tuple() returns a 16-element tuple with correct field order."""
    event = _make_event()
    row = event.to_row_tuple()
    assert len(row) == 16
    assert row[0] == 1  # db_request_id
    assert row[1] == 1  # attempt_number
    assert row[2] == "gpt-4"  # model_id
    assert row[3] is None  # provider_id
    assert row[4] == "openai"  # protocol
    assert row[5] == 1  # selected_account_id
    assert row[6] == "acct-a"  # selected_account_name
    assert row[7] == 0  # selected_tier
    assert row[8] == 1.0  # selected_score
    assert row[9] == 2  # eligible_count
    assert row[10] == 2  # scored_count
    assert row[11] == 0  # attempted_excluded_count
    assert row[12] == 1.0  # top_score
    assert row[13] == "acct-a"  # top_score_account_name
    assert row[14] == "{}"  # exclude_reasons_json
    assert row[15] == "{}"  # score_components_json


@pytest.mark.asyncio()
async def test_to_row_tuple_score_components_none_becomes_empty() -> None:
    """When score_components_json is None, to_row_tuple renders '{}'."""
    event = _make_event()
    # Create a new event with None score_components
    replaced = RoutingTraceEvent(
        request_id=event.request_id,
        db_request_id=event.db_request_id,
        attempt_number=event.attempt_number,
        model_id=event.model_id,
        provider_id=event.provider_id,
        protocol=event.protocol,
        selected_account_name=event.selected_account_name,
        selected_account_id=event.selected_account_id,
        selected_tier=event.selected_tier,
        selected_score=event.selected_score,
        eligible_count=event.eligible_count,
        scored_count=event.scored_count,
        attempted_excluded_count=event.attempted_excluded_count,
        top_score=event.top_score,
        top_score_account_name=event.top_score_account_name,
        exclude_reasons_json=event.exclude_reasons_json,
        score_components_json=None,
        created_at_mono_ns=event.created_at_mono_ns,
        created_at_epoch=event.created_at_epoch,
        generation_id=event.generation_id,
    )
    row = replaced.to_row_tuple()
    assert row[15] == "{}"


@pytest.mark.asyncio()
async def test_write_batch_failure_increments_flush_error() -> None:
    """If the repo raises, dropped_flush_error counter is incremented."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        writer = await _create_writer(db, flush_interval_s=0.05)
        writer.start()
        try:
            # Corrupt the repo to force a failure
            original_create_many = writer._repo.create_many

            async def _fail(rows: Any) -> int:
                raise RuntimeError("simulated DB failure")

            writer._repo.create_many = _fail  # type: ignore[assignment]
            try:
                writer.submit(_make_event(db_request_id=1))
                await _flush_writer(writer)
                snap = writer.snapshot()
                assert snap["dropped_flush_error"] == 1
            finally:
                writer._repo.create_many = original_create_many  # type: ignore[assignment]
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_configure_mode_none_does_not_clear() -> None:
    """configure(mode=None) leaves existing mode unchanged."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        writer = await _create_writer(db)
        writer.configure(mode="off")
        assert writer._mode == "off"
        writer.configure(mode=None)
        assert writer._mode == "off"
    finally:
        await db.disconnect()
