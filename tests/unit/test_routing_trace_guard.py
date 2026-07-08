"""Unit tests for ``RoutingTraceGuard``."""

from __future__ import annotations

import pytest

from eggpool.db.connection import Database
from eggpool.request.routing_trace_guard import (
    RoutingTraceGuard,
    get_routing_trace_guard,
    reset_routing_trace_guard,
)


@pytest.fixture()
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


def test_guard_threshold_zero_disables_pressure_check() -> None:
    guard = RoutingTraceGuard(threshold_ms=0.0)
    skip, reason = guard.should_skip(db=None)
    assert skip is False
    assert reason == "ok"


def test_guard_records_written() -> None:
    guard = RoutingTraceGuard()
    guard.record_written()
    guard.record_written()
    snap = guard.snapshot()
    assert snap["written"] == 2
    assert snap["skipped_total"] == 0


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


async def test_guard_allows_when_below_threshold(db: Database) -> None:
    guard = RoutingTraceGuard(threshold_ms=500.0)
    db._lock_wait_samples_s.extend([0.001] * 16)  # pyright: ignore[reportPrivateUsage]
    db._lock_wait_count += 16  # pyright: ignore[reportPrivateUsage]
    skip, reason = guard.should_skip(db)
    assert skip is False
    assert reason == "ok"


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


def test_get_routing_trace_guard_returns_singleton() -> None:
    g1 = get_routing_trace_guard()
    g2 = get_routing_trace_guard()
    assert g1 is g2
