"""Unit tests for ``RoutingTraceGuard``."""

from __future__ import annotations

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.request.routing_trace_guard import (
    RoutingTraceGuard,
    get_routing_trace_guard,
    reset_routing_trace_guard,
)


@pytest_asyncio.fixture()
async def db() -> Database:
    database = Database(path=":memory:")
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture(autouse=True)
def _reset_global_guard() -> None:
    reset_routing_trace_guard()


def test_guard_disabled_always_skips() -> None:
    guard = RoutingTraceGuard(enabled=False)
    skip, reason = guard.should_skip(db=None)
    assert skip is True
    assert reason == "disabled"
    guard.record_skip(reason=reason)
    snap = guard.snapshot()
    assert snap["skipped_disabled"] == 1
    assert snap["written"] == 0


def test_guard_threshold_zero_skips_db_check_but_checks_writer() -> None:
    """threshold_ms=0 disables DB pressure check; writer signals still apply."""
    guard = RoutingTraceGuard(threshold_ms=0.0)
    # No DB, no writer → ok
    skip, reason = guard.should_skip(db=None)
    assert skip is False
    assert reason == "ok"
    # Writer pressure still triggers even with threshold_ms=0
    writer_snap = {"queue_depth": 900, "queue_capacity": 1000}
    skip2, reason2 = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip2 is True
    assert reason2 == "queue_pressure"


def test_guard_records_written() -> None:
    guard = RoutingTraceGuard()
    guard.record_written()
    guard.record_written()
    snap = guard.snapshot()
    assert snap["written"] == 2
    assert snap["skipped_total"] == 0


@pytest.mark.asyncio()
async def test_guard_skips_on_db_pressure(db: Database) -> None:
    guard = RoutingTraceGuard(threshold_ms=10.0)
    # Seed the contention histogram with samples that exceed 10ms p95.
    samples = [0.001] * 8 + [0.200] * 8
    db._lock_wait_samples_s.extend(samples)  # pyright: ignore[reportPrivateUsage]
    db._lock_wait_count += len(samples)  # pyright: ignore[reportPrivateUsage]
    skip, reason = guard.should_skip(db)
    assert skip is True
    assert reason == "db_pressure"
    guard.record_skip(reason=reason)
    snap = guard.snapshot()
    assert snap["skipped_db_pressure"] == 1
    assert snap["last_lock_wait_p95_ms"] is not None
    assert snap["last_lock_wait_p95_ms"] > 10.0


@pytest.mark.asyncio()
async def test_guard_allows_when_below_threshold(db: Database) -> None:
    guard = RoutingTraceGuard(threshold_ms=500.0)
    db._lock_wait_samples_s.extend([0.001] * 16)  # pyright: ignore[reportPrivateUsage]
    db._lock_wait_count += 16  # pyright: ignore[reportPrivateUsage]
    skip, reason = guard.should_skip(db)
    assert skip is False
    assert reason == "ok"


@pytest.mark.asyncio()
async def test_guard_insufficient_samples_does_not_skip(db: Database) -> None:
    """Skip requires >= 8 samples to avoid tripping on cold-start spikes."""
    guard = RoutingTraceGuard(threshold_ms=10.0)
    db._lock_wait_samples_s.extend([0.200] * 4)
    db._lock_wait_count += 4
    skip, reason = guard.should_skip(db)
    assert skip is False
    assert reason == "ok"


def test_guard_configure_updates_threshold() -> None:
    guard = RoutingTraceGuard(threshold_ms=10.0)
    guard.configure(threshold_ms=500.0)
    assert guard.snapshot()["threshold_ms"] == 500.0


@pytest.mark.parametrize("value", ["not-a-number", -1.0, float("inf")])
def test_guard_rejects_invalid_thresholds(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RoutingTraceGuard(threshold_ms=value)  # type: ignore[arg-type]

    guard = RoutingTraceGuard(threshold_ms=10.0)
    with pytest.raises((TypeError, ValueError)):
        guard.configure(threshold_ms=value)  # type: ignore[arg-type]
    assert guard.snapshot()["threshold_ms"] == 10.0


def test_get_routing_trace_guard_returns_singleton() -> None:
    g1 = get_routing_trace_guard()
    g2 = get_routing_trace_guard()
    assert g1 is g2


# -- Writer queue pressure ------------------------------------------------


def test_guard_skips_on_queue_pressure() -> None:
    guard = RoutingTraceGuard(threshold_ms=0.0, queue_occupancy_threshold=0.8)
    # Queue at 90% capacity triggers skip.
    writer_snap = {"queue_depth": 900, "queue_capacity": 1000}
    skip, reason = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is True
    assert reason == "queue_pressure"
    guard.record_skip(reason=reason)
    snap = guard.snapshot()
    assert snap["skipped_queue_pressure"] == 1
    assert snap["last_writer_queue_depth"] == 900
    assert snap["last_writer_queue_capacity"] == 1000


def test_guard_allows_below_queue_occupancy() -> None:
    guard = RoutingTraceGuard(threshold_ms=0.0, queue_occupancy_threshold=0.8)
    writer_snap = {"queue_depth": 500, "queue_capacity": 1000}
    skip, reason = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is False
    assert reason == "ok"


# -- Oldest event age -----------------------------------------------------


def test_guard_skips_on_stale_oldest_event() -> None:
    guard = RoutingTraceGuard(
        threshold_ms=0.0,
        queue_occupancy_threshold=1.0,  # disable queue check
        oldest_event_age_s=30.0,
    )
    writer_snap = {
        "queue_depth": 10,
        "queue_capacity": 1000,
        "oldest_event_age_s": 60.0,  # > 30s threshold
    }
    skip, reason = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is True
    assert reason == "oldest_event_stale"
    snap = guard.snapshot()
    assert snap["last_writer_oldest_age_s"] == 60.0


def test_guard_allows_when_oldest_event_fresh() -> None:
    guard = RoutingTraceGuard(
        threshold_ms=0.0,
        queue_occupancy_threshold=1.0,
        oldest_event_age_s=30.0,
    )
    writer_snap = {
        "queue_depth": 10,
        "queue_capacity": 1000,
        "oldest_event_age_s": 5.0,
    }
    skip, reason = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is False
    assert reason == "ok"


# -- Flush failures -------------------------------------------------------


def test_guard_skips_on_flush_failure() -> None:
    guard = RoutingTraceGuard(
        threshold_ms=0.0,
        queue_occupancy_threshold=1.0,
        oldest_event_age_s=600.0,
    )
    writer_snap = {
        "queue_depth": 10,
        "queue_capacity": 1000,
        "oldest_event_age_s": 1.0,
        "dropped_flush_error": 5,
    }
    skip, reason = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is True
    assert reason == "flush_failure"
    guard.record_skip(reason=reason)
    snap = guard.snapshot()
    assert snap["skipped_flush_failure"] == 1
    assert snap["last_writer_flush_errors"] == 5


def test_guard_allows_when_no_flush_failures() -> None:
    guard = RoutingTraceGuard(
        threshold_ms=0.0,
        queue_occupancy_threshold=1.0,
        oldest_event_age_s=600.0,
    )
    writer_snap = {
        "queue_depth": 10,
        "queue_capacity": 1000,
        "oldest_event_age_s": 1.0,
        "dropped_flush_error": 0,
    }
    skip, reason = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is False
    assert reason == "ok"


# -- Hysteresis / cooldown ------------------------------------------------


def test_guard_cooldown_after_skip() -> None:
    """After a skip triggers, subsequent calls stay in cooldown."""

    guard = RoutingTraceGuard(
        threshold_ms=0.0,
        cooldown_s=10.0,
    )
    writer_snap = {"queue_depth": 900, "queue_capacity": 1000}
    # First call triggers queue_pressure
    skip, reason = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is True
    assert reason == "queue_pressure"
    guard.record_skip(reason=reason)

    # Immediately after, queue is now fine but cooldown keeps skipping
    writer_snap_ok = {"queue_depth": 10, "queue_capacity": 1000}
    skip2, reason2 = guard.should_skip(db=None, writer_snapshot=writer_snap_ok)
    assert skip2 is True
    assert reason2 == "cooldown"
    guard.record_skip(reason=reason2)
    snap = guard.snapshot()
    assert snap["skipped_cooldown"] == 1


def test_guard_cooldown_expires() -> None:
    """After cooldown_s elapses, the guard allows again."""
    import time

    guard = RoutingTraceGuard(threshold_ms=0.0, cooldown_s=0.01)
    writer_snap = {"queue_depth": 900, "queue_capacity": 1000}
    skip, _ = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is True
    # Wait for cooldown to expire
    time.sleep(0.02)
    writer_snap_ok = {"queue_depth": 10, "queue_capacity": 1000}
    skip2, reason2 = guard.should_skip(db=None, writer_snapshot=writer_snap_ok)
    assert skip2 is False
    assert reason2 == "ok"


def test_guard_cooldown_disabled_when_zero() -> None:
    """cooldown_s=0 means no hysteresis."""
    guard = RoutingTraceGuard(threshold_ms=0.0, cooldown_s=0.0)
    writer_snap = {"queue_depth": 900, "queue_capacity": 1000}
    skip, _ = guard.should_skip(db=None, writer_snapshot=writer_snap)
    assert skip is True
    # Immediately check again — no cooldown
    writer_snap_ok = {"queue_depth": 10, "queue_capacity": 1000}
    skip2, reason2 = guard.should_skip(db=None, writer_snapshot=writer_snap_ok)
    assert skip2 is False
    assert reason2 == "ok"


# -- Configure all fields -------------------------------------------------


def test_guard_configure_updates_all_fields() -> None:
    guard = RoutingTraceGuard()
    guard.configure(
        threshold_ms=100.0,
        queue_occupancy_threshold=0.9,
        oldest_event_age_s=60.0,
        cooldown_s=10.0,
    )
    snap = guard.snapshot()
    assert snap["threshold_ms"] == 100.0
    assert snap["queue_occupancy_threshold"] == 0.9
    assert snap["oldest_event_age_s"] == 60.0
    assert snap["cooldown_s"] == 10.0


# -- Snapshot includes writer diagnostics ---------------------------------


def test_snapshot_includes_writer_diagnostics() -> None:
    guard = RoutingTraceGuard(threshold_ms=0.0)
    writer_snap = {
        "queue_depth": 42,
        "queue_capacity": 1000,
        "oldest_event_age_s": 3.5,
        "dropped_flush_error": 2,
    }
    guard.should_skip(db=None, writer_snapshot=writer_snap)
    snap = guard.snapshot()
    assert snap["last_writer_queue_depth"] == 42
    assert snap["last_writer_queue_capacity"] == 1000
    assert snap["last_writer_oldest_age_s"] == 3.5
    assert snap["last_writer_flush_errors"] == 2


def test_snapshot_without_writer_returns_none_for_writer_fields() -> None:
    guard = RoutingTraceGuard(threshold_ms=0.0)
    guard.should_skip(db=None, writer_snapshot=None)
    snap = guard.snapshot()
    assert snap["last_writer_queue_depth"] is None
    assert snap["last_writer_queue_capacity"] is None
    assert snap["last_writer_oldest_age_s"] is None
    assert snap["last_writer_flush_errors"] is None
