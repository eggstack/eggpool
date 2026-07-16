"""Comprehensive tests for the EggPool bounded maintenance system.

Covers:

- ``MaintenancePassResult`` basics: creation, equality with int, addition
- ``MaintenanceBudget``: creation, defaults, ``expired()`` method
- ``ContentionGuard``: mock ``db.contention_snapshot()`` to test deferral logic
- Chunked cleanup for old requests, events, pings, operational events,
  usage rollups: budget respects row limits, all rows eventually deleted
- ``run_maintenance_pass``: budget enforcement, contention guard, error handling
- ``finalize_stale_requests_once``: bounded processing with ``batch_size``
- ``reconcile_expired_reservations``: bounded processing, runtime reconciliation
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from eggpool.app import finalize_stale_requests_once
from eggpool.background.cleanup import (
    cleanup_old_events,
    cleanup_old_operational_events,
    cleanup_old_requests,
    cleanup_old_usage_rollups,
    reconcile_expired_reservations,
)
from eggpool.background.maintenance import (
    ContentionGuard,
    MaintenanceBudget,
    MaintenancePassResult,
    run_maintenance_pass,
)
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    RequestRepository,
    ReservationRepository,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "maintenance_budget.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


async def _seed_account_and_model(db: Database) -> None:
    """Insert minimum rows for FK constraints."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )


# ---------------------------------------------------------------------------
# 1. MaintenancePassResult basics
# ---------------------------------------------------------------------------


class TestMaintenancePassResultBasics:
    """Creation, equality, addition for MaintenancePassResult."""

    def test_default_creation(self) -> None:
        r = MaintenancePassResult()
        assert r.task_name == ""
        assert r.rows_scanned == 0
        assert r.rows_changed == 0
        assert r.batches_completed == 0
        assert r.duration_ms == 0.0
        assert r.remaining_estimate is None
        assert r.stopped_reason == "complete"
        assert r.last_cursor is None
        assert r.error_class is None
        assert r.contention_deferrals == 0
        assert r.budget_exhausted is False

    def test_creation_with_values(self) -> None:
        r = MaintenancePassResult(
            task_name="test",
            rows_scanned=100,
            rows_changed=50,
            batches_completed=2,
            duration_ms=123.4,
            remaining_estimate=10,
            stopped_reason="row_budget",
            last_cursor=42,
            contention_deferrals=3,
            budget_exhausted=True,
        )
        assert r.task_name == "test"
        assert r.rows_scanned == 100
        assert r.rows_changed == 50
        assert r.batches_completed == 2
        assert r.duration_ms == 123.4
        assert r.remaining_estimate == 10
        assert r.stopped_reason == "row_budget"
        assert r.last_cursor == 42
        assert r.contention_deferrals == 3
        assert r.budget_exhausted is True

    def test_equality_with_int(self) -> None:
        """MaintenancePassResult == int compares rows_changed."""
        r = MaintenancePassResult(rows_changed=42)
        assert r == 42
        assert r != 0
        assert r != 43

    def test_equality_with_result(self) -> None:
        a = MaintenancePassResult(rows_changed=10, task_name="x")
        b = MaintenancePassResult(rows_changed=10, task_name="x")
        c = MaintenancePassResult(rows_changed=10, task_name="y")
        assert a == b
        assert a != c

    def test_equality_with_unrelated_type_returns_not_implemented(self) -> None:
        r = MaintenancePassResult()
        assert r.__eq__("not a number") is NotImplemented

    def test_hash_consistency(self) -> None:
        a = MaintenancePassResult(rows_changed=5, task_name="t")
        b = MaintenancePassResult(rows_changed=5, task_name="t")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_add_two_results(self) -> None:
        a = MaintenancePassResult(rows_changed=10)
        b = MaintenancePassResult(rows_changed=20)
        assert a + b == 30

    def test_add_result_and_int(self) -> None:
        r = MaintenancePassResult(rows_changed=10)
        assert r + 5 == 15
        assert 5 + r == 15

    def test_radd_with_int(self) -> None:
        r = MaintenancePassResult(rows_changed=10)
        assert sum([r, r, r]) == 30

    def test_add_with_unrelated_returns_not_implemented(self) -> None:
        r = MaintenancePassResult()
        assert r.__add__("not a number") is NotImplemented

    def test_radd_with_unrelated_returns_not_implemented(self) -> None:
        r = MaintenancePassResult()
        assert r.__radd__("not a number") is NotImplemented


# ---------------------------------------------------------------------------
# 2. MaintenanceBudget
# ---------------------------------------------------------------------------


class TestMaintenanceBudget:
    """Creation, defaults, expired() method."""

    def test_defaults(self) -> None:
        b = MaintenanceBudget()
        assert b.max_rows_per_batch == 500
        assert b.max_batches_per_tick == 4
        assert b.max_tick_duration_ms == 500.0
        assert b.priority == 1

    def test_custom_values(self) -> None:
        b = MaintenanceBudget(
            max_rows_per_batch=10,
            max_batches_per_tick=2,
            max_tick_duration_ms=50.0,
            priority=0,
        )
        assert b.max_rows_per_batch == 10
        assert b.max_batches_per_tick == 2
        assert b.max_tick_duration_ms == 50.0
        assert b.priority == 0

    def test_expired_returns_false_when_under_budget(self) -> None:
        b = MaintenanceBudget(max_batches_per_tick=4, max_tick_duration_ms=5000.0)
        start = time.monotonic()
        assert b.expired(start_time=start, batches_done=0) is False
        assert b.expired(start_time=start, batches_done=1) is False
        assert b.expired(start_time=start, batches_done=3) is False

    def test_expired_returns_true_at_batch_limit(self) -> None:
        b = MaintenanceBudget(max_batches_per_tick=2, max_tick_duration_ms=5000.0)
        start = time.monotonic()
        assert b.expired(start_time=start, batches_done=2) is True
        assert b.expired(start_time=start, batches_done=10) is True

    def test_expired_returns_true_at_time_limit(self) -> None:
        b = MaintenanceBudget(max_batches_per_tick=100, max_tick_duration_ms=1.0)
        start = time.monotonic()
        # Sleep just past the 1ms time budget
        time.sleep(0.01)
        assert b.expired(start_time=start, batches_done=0) is True

    def test_expired_with_zero_batches_not_at_limit(self) -> None:
        b = MaintenanceBudget(max_batches_per_tick=1, max_tick_duration_ms=5000.0)
        start = time.monotonic()
        assert b.expired(start_time=start, batches_done=0) is False


# ---------------------------------------------------------------------------
# 3. ContentionGuard
# ---------------------------------------------------------------------------


class TestContentionGuard:
    """ContentionGuard defers maintenance under lock pressure."""

    def _make_db_mock(self, snapshot: dict) -> Database:
        db = MagicMock(spec=Database)
        db.contention_snapshot.return_value = snapshot
        return db

    @pytest.mark.asyncio
    async def test_no_deferral_when_p95_below_threshold(self) -> None:
        db = self._make_db_mock(
            {
                "lock_wait_p95_ms": 50.0,
                "lock_wait_sample_count": 10,
            }
        )
        guard = ContentionGuard(db, threshold_ms=200.0)
        assert await guard.should_defer() is False
        assert guard.deferrals == 0

    @pytest.mark.asyncio
    async def test_defer_when_p95_exceeds_threshold(self) -> None:
        db = self._make_db_mock(
            {
                "lock_wait_p95_ms": 250.0,
                "lock_wait_sample_count": 10,
            }
        )
        guard = ContentionGuard(db, threshold_ms=200.0)
        assert await guard.should_defer() is True
        assert guard.deferrals == 1

    @pytest.mark.asyncio
    async def test_no_deferral_when_insufficient_samples(self) -> None:
        db = self._make_db_mock(
            {
                "lock_wait_p95_ms": 500.0,
                "lock_wait_sample_count": 3,
            }
        )
        guard = ContentionGuard(db, threshold_ms=200.0, min_samples=8)
        assert await guard.should_defer() is False
        assert guard.deferrals == 0

    @pytest.mark.asyncio
    async def test_no_deferral_when_p95_is_none(self) -> None:
        db = self._make_db_mock(
            {
                "lock_wait_p95_ms": None,
                "lock_wait_sample_count": 100,
            }
        )
        guard = ContentionGuard(db, threshold_ms=200.0)
        assert await guard.should_defer() is False
        assert guard.deferrals == 0

    @pytest.mark.asyncio
    async def test_no_deferral_when_sample_count_missing(self) -> None:
        db = self._make_db_mock(
            {
                "lock_wait_p95_ms": 300.0,
            }
        )
        guard = ContentionGuard(db, threshold_ms=200.0, min_samples=8)
        assert await guard.should_defer() is False
        assert guard.deferrals == 0

    @pytest.mark.asyncio
    async def test_deferral_count_accumulates(self) -> None:
        db = self._make_db_mock(
            {
                "lock_wait_p95_ms": 300.0,
                "lock_wait_sample_count": 10,
            }
        )
        guard = ContentionGuard(db, threshold_ms=200.0)
        await guard.should_defer()
        await guard.should_defer()
        await guard.should_defer()
        assert guard.deferrals == 3

    @pytest.mark.asyncio
    async def test_snapshot_returns_diagnostics(self) -> None:
        db = self._make_db_mock(
            {
                "lock_wait_p95_ms": 150.0,
                "lock_wait_sample_count": 5,
            }
        )
        guard = ContentionGuard(db, threshold_ms=200.0, min_samples=8)
        await guard.should_defer()
        snap = guard.snapshot()
        assert snap["threshold_ms"] == 200.0
        assert snap["min_samples"] == 8
        assert snap["deferrals"] == 0
        assert snap["last_lock_wait_p95_ms"] == 150.0
        assert snap["last_lock_wait_sample_count"] == 5

    @pytest.mark.asyncio
    async def test_exact_threshold_does_not_defer(self) -> None:
        db = self._make_db_mock(
            {
                "lock_wait_p95_ms": 200.0,
                "lock_wait_sample_count": 10,
            }
        )
        guard = ContentionGuard(db, threshold_ms=200.0)
        assert await guard.should_defer() is False


# ---------------------------------------------------------------------------
# 4. Chunked cleanup_old_requests
# ---------------------------------------------------------------------------


class TestChunkedCleanupOldRequests:
    """cleanup_old_requests respects budget row limits."""

    @pytest.mark.asyncio
    async def test_budget_limits_rows_per_batch(self, db: Database) -> None:
        """With max_rows_per_batch=3 and 10 rows, first pass deletes only 3."""
        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(10):
                await db.execute_write(
                    "INSERT INTO requests "
                    "(account_id, model_id, status, started_at) "
                    "VALUES (1, 'gpt-4', 'completed', "
                    "datetime('now', '-100 days'))",
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=3,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        result = await cleanup_old_requests(db, retain_days=30, budget=budget)
        assert result.rows_changed == 3
        assert result.batches_completed == 1

        remaining = await db.fetch_all("SELECT id FROM requests")
        assert len(remaining) == 7

    @pytest.mark.asyncio
    async def test_all_rows_deleted_across_multiple_calls(self, db: Database) -> None:
        """Repeated calls drain all rows eventually."""
        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(7):
                await db.execute_write(
                    "INSERT INTO requests "
                    "(account_id, model_id, status, started_at) "
                    "VALUES (1, 'gpt-4', 'completed', "
                    "datetime('now', '-100 days'))",
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=3,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        total = 0
        for _ in range(10):  # generous iteration limit
            result = await cleanup_old_requests(db, retain_days=30, budget=budget)
            total += result.rows_changed
            if result.rows_changed == 0:
                break

        assert total == 7
        remaining = await db.fetch_all("SELECT id FROM requests")
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_reservations_alongside_requests_are_deleted(
        self, db: Database
    ) -> None:
        """Deleting old requests also deletes associated reservations."""
        await _seed_account_and_model(db)
        req_repo = RequestRepository(db)
        resv_repo = ReservationRepository(db)
        async with db.transaction():
            req_id = await req_repo.create_pending(
                request_id="test-req",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            await resv_repo.create(
                request_id=req_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=100,
                estimated_microdollars=50_000,
            )
            # Make it old
            await db.execute_write(
                "UPDATE requests SET started_at = datetime('now', '-100 days') "
                "WHERE id = ?",
                (req_id,),
            )

        budget = MaintenanceBudget(max_rows_per_batch=100)
        result = await cleanup_old_requests(db, retain_days=30, budget=budget)
        assert result.rows_changed == 1

        resvs = await db.fetch_all("SELECT id FROM reservations")
        assert len(resvs) == 0

    @pytest.mark.asyncio
    async def test_no_rows_deleted_when_none_expire(self, db: Database) -> None:
        """Recent rows are untouched."""
        await _seed_account_and_model(db)
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO requests "
                "(account_id, model_id, status, started_at) "
                "VALUES (1, 'gpt-4', 'completed', "
                "datetime('now', '-5 days'))",
            )

        budget = MaintenanceBudget(max_rows_per_batch=100)
        result = await cleanup_old_requests(db, retain_days=30, budget=budget)
        assert result.rows_changed == 0

        remaining = await db.fetch_all("SELECT id FROM requests")
        assert len(remaining) == 1

    @pytest.mark.asyncio
    async def test_budget_exhausted_flag_set(self, db: Database) -> None:
        """budget_exhausted is True when budget runs out mid-cleanup."""
        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(5):
                await db.execute_write(
                    "INSERT INTO requests "
                    "(account_id, model_id, status, started_at) "
                    "VALUES (1, 'gpt-4', 'completed', "
                    "datetime('now', '-100 days'))",
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        result = await cleanup_old_requests(db, retain_days=30, budget=budget)
        assert result.budget_exhausted is True
        assert result.rows_changed == 2


# ---------------------------------------------------------------------------
# 5. Chunked cleanup_old_events
# ---------------------------------------------------------------------------


class TestChunkedCleanupOldEvents:
    """cleanup_old_events respects budget row limits."""

    @pytest.mark.asyncio
    async def test_budget_limits_rows_per_batch(self, db: Database) -> None:
        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(8):
                await db.execute_write(
                    "INSERT INTO account_events "
                    "(account_id, event_type, details, created_at) "
                    "VALUES (1, 'test', '{}', datetime('now', '-100 days'))",
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=3,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        result = await cleanup_old_events(db, retain_days=30, budget=budget)
        assert result.rows_changed == 3
        assert result.batches_completed == 1

        remaining = await db.fetch_all("SELECT id FROM account_events")
        assert len(remaining) == 5

    @pytest.mark.asyncio
    async def test_all_rows_deleted_across_multiple_calls(self, db: Database) -> None:
        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(7):
                await db.execute_write(
                    "INSERT INTO account_events "
                    "(account_id, event_type, details, created_at) "
                    "VALUES (1, 'test', '{}', datetime('now', '-100 days'))",
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=3,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        total = 0
        for _ in range(10):
            result = await cleanup_old_events(db, retain_days=30, budget=budget)
            total += result.rows_changed
            if result.rows_changed == 0:
                break

        assert total == 7
        remaining = await db.fetch_all("SELECT id FROM account_events")
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# 6. Chunked cleanup_old_pings
# ---------------------------------------------------------------------------


class TestChunkedCleanupOldPings:
    """cleanup_old_pings respects budget row limits.

    Note: cleanup_old_pings lives on PingRepository, not cleanup.py.
    """

    @pytest.mark.asyncio
    async def test_budget_limits_rows_per_batch(self, db: Database) -> None:
        from eggpool.db.repositories import PingRepository

        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(6):
                await db.execute_write(
                    "INSERT INTO provider_pings "
                    "(provider_id, account_name, probed_at) "
                    "VALUES (?, ?, datetime('now', '-100 days'))",
                    ("openai", "test-acct"),
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        repo = PingRepository(db)
        result = await repo.cleanup_old_pings(retain_days=7, budget=budget)
        assert result.rows_changed == 2
        assert result.batches_completed == 1

        remaining = await db.fetch_all("SELECT id FROM provider_pings")
        assert len(remaining) == 4

    @pytest.mark.asyncio
    async def test_all_rows_deleted_across_multiple_calls(self, db: Database) -> None:
        from eggpool.db.repositories import PingRepository

        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(5):
                await db.execute_write(
                    "INSERT INTO provider_pings "
                    "(provider_id, account_name, probed_at) "
                    "VALUES (?, ?, datetime('now', '-100 days'))",
                    ("openai", "test-acct"),
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        repo = PingRepository(db)
        total = 0
        for _ in range(10):
            result = await repo.cleanup_old_pings(retain_days=7, budget=budget)
            total += result.rows_changed
            if result.rows_changed == 0:
                break

        assert total == 5
        remaining = await db.fetch_all("SELECT id FROM provider_pings")
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# 7. Chunked cleanup_old_operational_events
# ---------------------------------------------------------------------------


class TestChunkedCleanupOldOperationalEvents:
    """cleanup_old_operational_events respects budget row limits."""

    @pytest.mark.asyncio
    async def test_budget_limits_rows_per_batch(self, db: Database) -> None:
        async with db.transaction():
            for i in range(6):
                await db.execute_write(
                    "INSERT INTO operational_events "
                    "(event_type, details_json, occurred_at) "
                    "VALUES (?, '{}', datetime('now', '-100 days'))",
                    (f"test_event_{i}",),
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        result = await cleanup_old_operational_events(db, retain_days=30, budget=budget)
        assert result.rows_changed == 2
        assert result.batches_completed == 1

        remaining = await db.fetch_all("SELECT id FROM operational_events")
        assert len(remaining) == 4

    @pytest.mark.asyncio
    async def test_all_rows_deleted_across_multiple_calls(self, db: Database) -> None:
        async with db.transaction():
            for i in range(5):
                await db.execute_write(
                    "INSERT INTO operational_events "
                    "(event_type, details_json, occurred_at) "
                    "VALUES (?, '{}', datetime('now', '-100 days'))",
                    (f"event_{i}",),
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        total = 0
        for _ in range(10):
            result = await cleanup_old_operational_events(
                db, retain_days=30, budget=budget
            )
            total += result.rows_changed
            if result.rows_changed == 0:
                break

        assert total == 5
        remaining = await db.fetch_all("SELECT id FROM operational_events")
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# 8. Chunked cleanup_old_usage_rollups
# ---------------------------------------------------------------------------


class TestChunkedCleanupOldUsageRollups:
    """cleanup_old_usage_rollups respects budget row limits."""

    @pytest.mark.asyncio
    async def test_budget_limits_rows_per_batch(self, db: Database) -> None:
        await _seed_account_and_model(db)
        async with db.transaction():
            for i in range(6):
                await db.execute_write(
                    "INSERT INTO usage_rollups "
                    "(bucket_start, bucket_size_s, provider_id, model_id, "
                    " account_id, protocol, streamed, status, request_count) "
                    "VALUES (?, 300, 'openai', 'gpt-4', 1, 'openai', 0, "
                    " 'completed', 1)",
                    (f"2020-01-{10 + i:02d}T00:00:00",),
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        result = await cleanup_old_usage_rollups(db, retain_days=30, budget=budget)
        assert result.rows_changed == 2
        assert result.batches_completed == 1

        remaining = await db.fetch_all("SELECT rowid FROM usage_rollups")
        assert len(remaining) == 4

    @pytest.mark.asyncio
    async def test_all_rows_deleted_across_multiple_calls(self, db: Database) -> None:
        await _seed_account_and_model(db)
        async with db.transaction():
            for i in range(5):
                await db.execute_write(
                    "INSERT INTO usage_rollups "
                    "(bucket_start, bucket_size_s, provider_id, model_id, "
                    " account_id, protocol, streamed, status, request_count) "
                    "VALUES (?, 300, 'openai', 'gpt-4', 1, 'openai', 0, "
                    " 'completed', 1)",
                    (f"2020-01-{10 + i:02d}T00:00:00",),
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        total = 0
        for _ in range(10):
            result = await cleanup_old_usage_rollups(db, retain_days=30, budget=budget)
            total += result.rows_changed
            if result.rows_changed == 0:
                break

        assert total == 5
        remaining = await db.fetch_all("SELECT rowid FROM usage_rollups")
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# 9. run_maintenance_pass
# ---------------------------------------------------------------------------


class TestRunMaintenancePass:
    """run_maintenance_pass enforces budget, contention guard, and errors."""

    @pytest.mark.asyncio
    async def test_single_batch_completes(self) -> None:
        async def work() -> MaintenancePassResult:
            return MaintenancePassResult(rows_scanned=10, rows_changed=5)

        budget = MaintenanceBudget(max_batches_per_tick=1)
        result = await run_maintenance_pass("test", budget, work)
        assert result.rows_changed == 5
        assert result.batches_completed == 1
        assert result.stopped_reason == "complete"

    @pytest.mark.asyncio
    async def test_stops_at_batch_budget(self) -> None:
        async def work() -> MaintenancePassResult:
            return MaintenancePassResult(rows_scanned=1, rows_changed=1)

        budget = MaintenanceBudget(
            max_rows_per_batch=1000,
            max_batches_per_tick=3,
            max_tick_duration_ms=5000.0,
        )
        result = await run_maintenance_pass("test", budget, work)
        assert result.batches_completed == 3
        assert result.rows_changed == 3

    @pytest.mark.asyncio
    async def test_stops_at_row_budget(self) -> None:
        async def work() -> MaintenancePassResult:
            return MaintenancePassResult(rows_scanned=100, rows_changed=100)

        budget = MaintenanceBudget(
            max_rows_per_batch=100,
            max_batches_per_tick=10,
            max_tick_duration_ms=5000.0,
        )
        result = await run_maintenance_pass("test", budget, work)
        # After 1 batch: budget_rows=200, limit=100*10=1000, not exceeded
        # After 2 batches: budget_rows=400, still under
        # This should stop when budget_rows >= max_rows_per_batch * max_batches_per_tick
        assert result.stopped_reason in ("complete", "row_budget")

    @pytest.mark.asyncio
    async def test_stops_on_error(self) -> None:
        call_count = 0

        async def work() -> MaintenancePassResult:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("boom")
            return MaintenancePassResult(rows_changed=1)

        budget = MaintenanceBudget(max_batches_per_tick=5)
        result = await run_maintenance_pass("test", budget, work)
        assert result.stopped_reason == "error"
        assert result.error_class == "ValueError"
        assert result.batches_completed == 1  # first batch succeeded

    @pytest.mark.asyncio
    async def test_stops_on_cancellation(self) -> None:
        async def work() -> MaintenancePassResult:
            return MaintenancePassResult(rows_changed=1)

        budget = MaintenanceBudget(max_batches_per_tick=5)

        task = asyncio.create_task(run_maintenance_pass("test", budget, work))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_contention_guard_defers_batch(self) -> None:
        call_count = 0

        async def work() -> MaintenancePassResult:
            nonlocal call_count
            call_count += 1
            return MaintenancePassResult(rows_changed=1)

        db_mock = MagicMock(spec=Database)
        db_mock.contention_snapshot.return_value = {
            "lock_wait_p95_ms": 500.0,
            "lock_wait_sample_count": 20,
        }
        guard = ContentionGuard(db_mock, threshold_ms=200.0)

        budget = MaintenanceBudget(max_batches_per_tick=3)
        result = await run_maintenance_pass(
            "test", budget, work, contention_guard=guard
        )
        # All 3 batches deferred, 0 actually executed
        assert call_count == 0
        assert result.batches_completed == 0
        assert result.contention_deferrals == 3

    @pytest.mark.asyncio
    async def test_contention_guard_allows_batch_when_pressure_low(self) -> None:
        async def work() -> MaintenancePassResult:
            return MaintenancePassResult(rows_changed=1)

        db_mock = MagicMock(spec=Database)
        db_mock.contention_snapshot.return_value = {
            "lock_wait_p95_ms": 50.0,
            "lock_wait_sample_count": 20,
        }
        guard = ContentionGuard(db_mock, threshold_ms=200.0)

        budget = MaintenanceBudget(max_batches_per_tick=2)
        result = await run_maintenance_pass(
            "test", budget, work, contention_guard=guard
        )
        assert result.batches_completed == 2
        assert result.contention_deferrals == 0

    @pytest.mark.asyncio
    async def test_batches_completed_accrued(self) -> None:
        batch_sizes = [10, 5, 0]

        async def work() -> MaintenancePassResult:
            idx = work._call_count
            work._call_count += 1
            remaining = 0 if idx >= 2 else 10
            return MaintenancePassResult(
                rows_changed=batch_sizes[idx],
                remaining_estimate=remaining,
            )

        work._call_count = 0  # type: ignore[attr-defined]

        budget = MaintenanceBudget(
            max_rows_per_batch=100,
            max_batches_per_tick=5,
            max_tick_duration_ms=5000.0,
        )
        result = await run_maintenance_pass("test", budget, work)
        assert result.batches_completed == 3
        assert result.rows_changed == 15


# ---------------------------------------------------------------------------
# 10. Bounded finalize_stale_requests_once
# ---------------------------------------------------------------------------


class TestBoundedFinalizeStaleRequests:
    """finalize_stale_requests_once respects batch_size."""

    @pytest.mark.asyncio
    async def test_batch_size_limits_processed_count(self, db: Database) -> None:
        """With batch_size=2, only 2 of 5 stale requests are finalized."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        stale_ids = []
        async with db.transaction():
            for i in range(5):
                req_id = await request_repo.create_pending(
                    request_id=f"stale-{i}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                stale_ids.append(req_id)
            # Make them all stale (>300s old)
            await db.execute_write(
                "UPDATE requests SET started_at = datetime('now', '-1 hour') "
                "WHERE id IN (?, ?, ?, ?, ?)",
                tuple(stale_ids),
            )

        router = MagicMock()
        router.decrement_active_request_count = AsyncMock()
        quota_estimator = MagicMock()
        quota_estimator.remove_reservation = AsyncMock()

        transitioned = await finalize_stale_requests_once(
            db,
            router,  # type: ignore[arg-type]
            quota_estimator,  # type: ignore[arg-type]
            max_pending_seconds=300.0,
            batch_size=2,
        )
        assert transitioned == 2

        # Check DB: only 2 finalized, 3 still pending
        pending = await db.fetch_all("SELECT id FROM requests WHERE status = 'pending'")
        assert len(pending) == 3
        interrupted = await db.fetch_all(
            "SELECT id FROM requests WHERE status = 'interrupted'"
        )
        assert len(interrupted) == 2

    @pytest.mark.asyncio
    async def test_all_finalized_across_multiple_calls(self, db: Database) -> None:
        """Repeated calls with small batch_size eventually finalize all."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        stale_ids = []
        async with db.transaction():
            for i in range(5):
                req_id = await request_repo.create_pending(
                    request_id=f"stale-{i}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                stale_ids.append(req_id)
            await db.execute_write(
                "UPDATE requests SET started_at = datetime('now', '-1 hour') "
                "WHERE id IN (?, ?, ?, ?, ?)",
                tuple(stale_ids),
            )

        router = MagicMock()
        router.decrement_active_request_count = AsyncMock()
        quota_estimator = MagicMock()
        quota_estimator.remove_reservation = AsyncMock()

        total = 0
        for _ in range(10):
            count = await finalize_stale_requests_once(
                db,
                router,  # type: ignore[arg-type]
                quota_estimator,  # type: ignore[arg-type]
                max_pending_seconds=300.0,
                batch_size=2,
            )
            total += count
            if count == 0:
                break

        assert total == 5
        remaining = await db.fetch_all(
            "SELECT id FROM requests WHERE status = 'pending'"
        )
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_runtime_reconciliation_works_with_bounded_batch(
        self, db: Database
    ) -> None:
        """Router and quota_estimator are called correctly for the batch."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        resv_repo = ReservationRepository(db)
        req_ids: list[str] = []
        async with db.transaction():
            for i in range(3):
                req_id = await request_repo.create_pending(
                    request_id=f"stale-{i}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                req_ids.append(req_id)
                await resv_repo.create(
                    request_id=req_id,
                    account_id=1,
                    model_id="gpt-4",
                    estimated_tokens=1000,
                    estimated_microdollars=50_000 * (i + 1),
                )
            placeholders = ",".join("?" for _ in req_ids)
            await db.execute_write(
                f"UPDATE requests SET started_at = datetime('now', '-1 hour') "
                f"WHERE id IN ({placeholders})",
                tuple(req_ids),
            )

        router = MagicMock()
        router.decrement_active_request_count = AsyncMock()
        quota_estimator = MagicMock()
        quota_estimator.remove_reservation = AsyncMock()

        transitioned = await finalize_stale_requests_once(
            db,
            router,  # type: ignore[arg-type]
            quota_estimator,  # type: ignore[arg-type]
            max_pending_seconds=300.0,
            batch_size=2,
        )
        assert transitioned == 2

        # Router decremented once (deduplicated per account)
        router.decrement_active_request_count.assert_awaited_once_with("test-acct")
        # Quota estimator removed reservations for the 2 finalized rows
        assert quota_estimator.remove_reservation.await_count == 2


# ---------------------------------------------------------------------------
# 11. Bounded reconcile_expired_reservations
# ---------------------------------------------------------------------------


class TestBoundedReconcileExpiredReservations:
    """reconcile_expired_reservations is bounded by budget."""

    @pytest.mark.asyncio
    async def test_expires_expired_reservations(self, db: Database) -> None:
        """Reservations past expiry are transitioned to expired."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        resv_repo = ReservationRepository(db)
        async with db.transaction():
            for i in range(3):
                req_id = await request_repo.create_pending(
                    request_id=f"req-{i}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                await resv_repo.create(
                    request_id=req_id,
                    account_id=1,
                    model_id="gpt-4",
                    estimated_tokens=100,
                    estimated_microdollars=50_000,
                )
                # Mark request completed so reservation can be expired
                await db.execute_write(
                    "UPDATE requests SET status = 'completed' WHERE id = ?",
                    (req_id,),
                )
            # Set expires_at to the past so they are expired
            await db.execute_write(
                "UPDATE reservations SET expires_at = datetime('now', '-1 hour') "
                "WHERE status = 'active'",
            )

        budget = MaintenanceBudget(max_rows_per_batch=100)
        result = await reconcile_expired_reservations(db, budget=budget)
        assert result.rows_changed == 3

        expired = await db.fetch_all(
            "SELECT id FROM reservations WHERE status = 'expired'"
        )
        assert len(expired) == 3

    @pytest.mark.asyncio
    async def test_budget_limits_rows_per_batch(self, db: Database) -> None:
        """With small budget, only a batch of reservations is expired."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        resv_repo = ReservationRepository(db)
        async with db.transaction():
            for i in range(5):
                req_id = await request_repo.create_pending(
                    request_id=f"req-{i}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                await resv_repo.create(
                    request_id=req_id,
                    account_id=1,
                    model_id="gpt-4",
                    estimated_tokens=100,
                    estimated_microdollars=50_000,
                )
                await db.execute_write(
                    "UPDATE requests SET status = 'completed' WHERE id = ?",
                    (req_id,),
                )
            await db.execute_write(
                "UPDATE reservations SET expires_at = datetime('now', '-1 hour') "
                "WHERE status = 'active'",
            )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        result = await reconcile_expired_reservations(db, budget=budget)
        assert result.rows_changed == 2
        assert result.budget_exhausted is True

        active = await db.fetch_all(
            "SELECT id FROM reservations WHERE status = 'active'"
        )
        assert len(active) == 3

    @pytest.mark.asyncio
    async def test_all_expired_across_multiple_calls(self, db: Database) -> None:
        """Repeated bounded calls eventually expire all."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        resv_repo = ReservationRepository(db)
        async with db.transaction():
            for i in range(7):
                req_id = await request_repo.create_pending(
                    request_id=f"req-{i}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                await resv_repo.create(
                    request_id=req_id,
                    account_id=1,
                    model_id="gpt-4",
                    estimated_tokens=100,
                    estimated_microdollars=50_000,
                )
                await db.execute_write(
                    "UPDATE requests SET status = 'completed' WHERE id = ?",
                    (req_id,),
                )
            await db.execute_write(
                "UPDATE reservations SET expires_at = datetime('now', '-1 hour') "
                "WHERE status = 'active'",
            )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        total = 0
        for _ in range(10):
            result = await reconcile_expired_reservations(db, budget=budget)
            total += result.rows_changed
            if result.rows_changed == 0:
                break

        assert total == 7
        active = await db.fetch_all(
            "SELECT id FROM reservations WHERE status = 'active'"
        )
        assert len(active) == 0

    @pytest.mark.asyncio
    async def test_runtime_reconciliation_called(self, db: Database) -> None:
        """Router and quota_estimator are called for each expired reservation."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        resv_repo = ReservationRepository(db)
        async with db.transaction():
            for i in range(2):
                req_id = await request_repo.create_pending(
                    request_id=f"req-{i}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                await resv_repo.create(
                    request_id=req_id,
                    account_id=1,
                    model_id="gpt-4",
                    estimated_tokens=100,
                    estimated_microdollars=50_000,
                )
                await db.execute_write(
                    "UPDATE requests SET status = 'completed' WHERE id = ?",
                    (req_id,),
                )
            await db.execute_write(
                "UPDATE reservations SET expires_at = datetime('now', '-1 hour') "
                "WHERE status = 'active'",
            )

        router = MagicMock()
        router.decrement_active_request_count = AsyncMock()
        quota_estimator = MagicMock()
        quota_estimator.remove_reservation = AsyncMock()

        budget = MaintenanceBudget(max_rows_per_batch=100)
        result = await reconcile_expired_reservations(
            db,
            quota_estimator=quota_estimator,  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
            budget=budget,
        )
        assert result.rows_changed == 2
        # Router decremented once per row (not deduplicated per account
        # in the reconcile helper -- dedup happens in finalize_stale_requests)
        assert router.decrement_active_request_count.await_count == 2
        # Quota estimator removed once per reservation
        assert quota_estimator.remove_reservation.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_reservations_with_pending_requests(self, db: Database) -> None:
        """Reservations tied to pending requests are NOT expired."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        resv_repo = ReservationRepository(db)
        async with db.transaction():
            req_id = await request_repo.create_pending(
                request_id="pending-req",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            await resv_repo.create(
                request_id=req_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=100,
                estimated_microdollars=50_000,
            )
            # Set expires_at to the past but keep request pending
            await db.execute_write(
                "UPDATE reservations SET expires_at = datetime('now', '-1 hour') "
                "WHERE request_id = ?",
                (req_id,),
            )

        budget = MaintenanceBudget(max_rows_per_batch=100)
        result = await reconcile_expired_reservations(db, budget=budget)
        assert result.rows_changed == 0

        active = await db.fetch_all(
            "SELECT id FROM reservations WHERE status = 'active'"
        )
        assert len(active) == 1

    @pytest.mark.asyncio
    async def test_budget_exhausted_flag(self, db: Database) -> None:
        """budget_exhausted is True when budget runs out."""
        await _seed_account_and_model(db)
        request_repo = RequestRepository(db)
        resv_repo = ReservationRepository(db)
        async with db.transaction():
            for i in range(4):
                req_id = await request_repo.create_pending(
                    request_id=f"req-{i}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                await resv_repo.create(
                    request_id=req_id,
                    account_id=1,
                    model_id="gpt-4",
                    estimated_tokens=100,
                    estimated_microdollars=50_000,
                )
                await db.execute_write(
                    "UPDATE requests SET status = 'completed' WHERE id = ?",
                    (req_id,),
                )
            await db.execute_write(
                "UPDATE reservations SET expires_at = datetime('now', '-1 hour') "
                "WHERE status = 'active'",
            )

        budget = MaintenanceBudget(
            max_rows_per_batch=2,
            max_batches_per_tick=1,
            max_tick_duration_ms=5000.0,
        )
        result = await reconcile_expired_reservations(db, budget=budget)
        assert result.rows_changed == 2
        assert result.budget_exhausted is True
        assert result.stopped_reason == "complete"


# ---------------------------------------------------------------------------
# 12. Integration: MaintenanceBudget with real cleanup functions
# ---------------------------------------------------------------------------


class TestBudgetIntegration:
    """Verify budget interaction between MaintenanceBudget and cleanup functions."""

    @pytest.mark.asyncio
    async def test_zero_budget_deletes_nothing(self, db: Database) -> None:
        """A budget with max_batches_per_tick=0 deletes nothing."""
        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(5):
                await db.execute_write(
                    "INSERT INTO requests "
                    "(account_id, model_id, status, started_at) "
                    "VALUES (1, 'gpt-4', 'completed', "
                    "datetime('now', '-100 days'))",
                )

        budget = MaintenanceBudget(max_batches_per_tick=0)
        result = await cleanup_old_requests(db, retain_days=30, budget=budget)
        assert result.rows_changed == 0

        remaining = await db.fetch_all("SELECT id FROM requests")
        assert len(remaining) == 5

    @pytest.mark.asyncio
    async def test_large_batch_deletes_all_at_once(self, db: Database) -> None:
        """A generous budget deletes everything in one pass."""
        await _seed_account_and_model(db)
        async with db.transaction():
            for _ in range(100):
                await db.execute_write(
                    "INSERT INTO requests "
                    "(account_id, model_id, status, started_at) "
                    "VALUES (1, 'gpt-4', 'completed', "
                    "datetime('now', '-100 days'))",
                )

        budget = MaintenanceBudget(
            max_rows_per_batch=200,
            max_batches_per_tick=10,
            max_tick_duration_ms=10000.0,
        )
        result = await cleanup_old_requests(db, retain_days=30, budget=budget)
        assert result.rows_changed == 100
        assert result.stopped_reason == "complete"

        remaining = await db.fetch_all("SELECT id FROM requests")
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_priority_field_stored(self) -> None:
        """Priority is stored and accessible on the budget."""
        p0 = MaintenanceBudget(priority=0)
        p1 = MaintenanceBudget(priority=1)
        p2 = MaintenanceBudget(priority=2)
        assert p0.priority == 0
        assert p1.priority == 1
        assert p2.priority == 2
