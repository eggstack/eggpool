"""Phase 15 end-to-end integration tests.

Covers: shared connection serialization, exhausted retry cleanup,
cooldown recovery, long-running reservation protection, cancelled
accounting, cache-only price updates, cache-only cost calculation,
and health consistency.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from eggpool.accounts.state import AccountRuntimeState
from eggpool.background.cleanup import reconcile_expired_reservations
from eggpool.catalog.pricing import CostCalculator, PriceRepository
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_db(db: Database) -> None:
    """Insert required account and model rows for FK constraints."""
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


async def _seed_db_two_accounts(db: Database) -> None:
    """Insert two accounts for multi-account tests."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-a", "KEY_A"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-b", "KEY_B"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )


# ===========================================================================
# A. Shared connection serialization
# ===========================================================================


class TestSharedConnectionSerialization:
    """A. A read waits for a transaction to commit."""

    @pytest.mark.asyncio
    async def test_read_waits_for_transaction_commit(self) -> None:
        """Two tasks sharing a DB: Task B's read waits for Task A's commit."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()

        commit_event = asyncio.Event()
        read_result: list[str | None] = []

        async def task_a() -> None:
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("delayed-acct", "DUMMY"),
                )
                # Signal that we've inserted but not committed yet
                commit_event.set()
                # Small delay to ensure Task B tries to read before commit
                await asyncio.sleep(0.1)

        async def task_b() -> None:
            # Wait until Task A has inserted but not committed
            await commit_event.wait()
            # This read should wait until Task A's transaction commits
            row = await db.fetch_one(
                "SELECT name FROM accounts WHERE name = ?",
                ("delayed-acct",),
            )
            read_result.append(row["name"] if row else None)

        # Run both tasks concurrently
        task_a_coro = asyncio.create_task(task_a())
        task_b_coro = asyncio.create_task(task_b())

        await asyncio.gather(task_a_coro, task_b_coro)

        # Task B should have seen the committed data
        assert read_result == ["delayed-acct"]

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_read_sees_committed_data_after_write(self) -> None:
        """After Task A commits, Task B's read returns the data immediately."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()

        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                ("committed-acct", "DUMMY"),
            )

        # Now a read outside a transaction should see the committed data
        row = await db.fetch_one(
            "SELECT name FROM accounts WHERE name = ?",
            ("committed-acct",),
        )
        assert row is not None
        assert row["name"] == "committed-acct"

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_write_waits_for_transaction_commit(self) -> None:
        """Task B's write outside a transaction waits for Task A's commit."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()

        commit_event = asyncio.Event()
        write_result: list[int] = []

        async def task_a() -> None:
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("tx-acct", "DUMMY"),
                )
                commit_event.set()
                await asyncio.sleep(0.1)

        async def task_b() -> None:
            await commit_event.wait()
            # This write should wait until Task A's transaction commits.
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("b-acct", "DUMMY"),
                )
            row = await db.fetch_one(
                "SELECT name FROM accounts WHERE name = ?", ("tx-acct",)
            )
            # Task B can only see tx-acct after Task A's commit
            write_result.append(1 if row is not None else 0)

        task_a_coro = asyncio.create_task(task_a())
        task_b_coro = asyncio.create_task(task_b())
        await asyncio.gather(task_a_coro, task_b_coro)

        assert write_result == [1]
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_child_task_cannot_inherit_transaction(self) -> None:
        """A child task spawned inside a transaction must wait for the lock."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()

        parent_in_transaction = asyncio.Event()
        child_entered = asyncio.Event()
        child_saw_data: list[bool] = []

        async def parent_task() -> None:
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    ("parent-acct", "DUMMY"),
                )
                parent_in_transaction.set()
                # Spawn child while holding the transaction lock but do NOT
                # await it here – that would deadlock.
                child_task = asyncio.create_task(child_task_fn())
                await asyncio.sleep(0.1)
            # Transaction committed, lock released. Now await the child.
            await child_task

        async def child_task_fn() -> None:
            # Child should NOT enter until parent completes
            await parent_in_transaction.wait()
            # Small delay to ensure we're scheduled while parent holds the lock
            await asyncio.sleep(0.05)
            # Child entering transaction must wait on the lock
            async with db.transaction():
                child_entered.set()
                row = await db.fetch_one(
                    "SELECT name FROM accounts WHERE name = ?",
                    ("parent-acct",),
                )
                child_saw_data.append(row is not None)

        await parent_task()

        # Child entered and could see committed data
        assert child_entered.is_set()
        assert child_saw_data == [True]
        await db.disconnect()


# ===========================================================================
# B. Exhausted retry cleanup
# ===========================================================================


class TestExhaustedRetryCleanup:
    """B. Exhausted retries don't corrupt another request's state."""

    @pytest.mark.asyncio
    async def test_finalize_a_does_not_corrupt_b(self) -> None:
        """Finalizing request A's reservation leaves request B's intact."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db_two_accounts(db)

        request_repo = RequestRepository(db)
        attempt_repo = AttemptRepository(db)
        reservation_repo = ReservationRepository(db)
        health_manager = HealthManager()

        # Create request A and B for same account
        async with db.transaction():
            db_id_a = await request_repo.create_pending(
                request_id="req-a",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id_a = await attempt_repo.create(
                request_id=db_id_a,
                attempt_number=1,
                account_id=1,
            )
            reservation_id_a = await reservation_repo.create(
                request_id=db_id_a,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100000,
                ttl_seconds=300,
            )

            db_id_b = await request_repo.create_pending(
                request_id="req-b",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            await attempt_repo.create(
                request_id=db_id_b,
                attempt_number=1,
                account_id=1,
            )
            reservation_id_b = await reservation_repo.create(
                request_id=db_id_b,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=500,
                estimated_microdollars=50000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            health_manager=health_manager,
        )

        class MockSelected:
            db_request_id = db_id_a
            account_name = "acct-a"
            model_id = "gpt-4"
            attempt_id = attempt_id_a
            reservation_id = reservation_id_a
            estimated_microdollars = 100000
            attempt_number = 1

        # Finalize A with upstream error (simulating attempt failure)
        transitioned = await finalizer.finalize(
            MockSelected(),
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                status_code=500,
                error_class="InternalServerError",
            ),
        )
        assert transitioned is True

        # Verify A is now terminal
        row_a = await db.fetch_one(
            "SELECT status FROM requests WHERE id = ?", (db_id_a,)
        )
        assert row_a is not None
        assert row_a["status"] == "error"

        # Verify B's reservation is still active
        reservation_b = await db.fetch_one(
            "SELECT status FROM reservations WHERE id = ?", (reservation_id_b,)
        )
        assert reservation_b is not None
        assert reservation_b["status"] == "active"

        # Verify B's request is still pending
        row_b = await db.fetch_one(
            "SELECT status FROM requests WHERE id = ?", (db_id_b,)
        )
        assert row_b is not None
        assert row_b["status"] == "pending"

        # Verify active reservation counts
        active_a = await reservation_repo.get_active_for_account(1)
        assert len(active_a) == 1
        assert str(active_a[0]["id"]) == reservation_id_b

        await db.disconnect()


# ===========================================================================
# C. Cooldown recovery
# ===========================================================================


class TestCooldownRecovery:
    """C. Quota-exhausted accounts recover after cooldown."""

    def test_cooldown_recovery(self) -> None:
        """Account A is ineligible during cooldown, eligible after."""
        hm = HealthManager()

        # Record quota exhaustion with short cooldown
        hm.record_quota_exhausted("acct-a", cooldown_seconds=0.1)

        # Verify A is not healthy during cooldown
        assert not hm.is_account_healthy("acct-a")
        health = hm.get_account_health("acct-a")
        assert health.health_state == "quota_exhausted"
        assert not health.is_healthy

        # Wait for cooldown to expire
        time.sleep(0.2)

        # Verify A is now healthy
        assert hm.is_account_healthy("acct-a")
        health = hm.get_account_health("acct-a")
        assert health.health_state == "healthy"
        assert health.is_healthy

    def test_runtime_state_cooldown_recovery(self) -> None:
        """AccountRuntimeState recovers after cooldown."""
        state = AccountRuntimeState(name="acct-a", enabled=True)

        # Simulate quota exhausted
        state.health_state = "quota_exhausted"
        state.cooldown_until = time.time() + 0.1

        # Verify not eligible during cooldown
        assert not state.is_eligible()

        # Wait for cooldown
        time.sleep(0.2)

        # Verify eligible after cooldown
        assert state.is_eligible()
        assert state.health_state == "healthy"

    def test_health_and_runtime_state_agree_on_cooldown(self) -> None:
        """Both HealthManager and AccountRuntimeState agree on cooldown."""
        hm = HealthManager()
        state = AccountRuntimeState(name="acct-a", enabled=True)

        # Record exhaustion on both
        hm.record_quota_exhausted("acct-a", cooldown_seconds=0.1)
        state.health_state = "quota_exhausted"
        state.cooldown_until = time.time() + 0.1

        # Both should report ineligible
        assert not hm.is_account_healthy("acct-a")
        assert not state.is_eligible()

        # Wait for cooldown
        time.sleep(0.2)

        # Both should report eligible
        assert hm.is_account_healthy("acct-a")
        assert state.is_eligible()


# ===========================================================================
# D. Long-running reservation protection
# ===========================================================================


class TestLongRunningReservationProtection:
    """D. Pending requests aren't expired by reconcile_expired_reservations."""

    @pytest.mark.asyncio
    async def test_pending_request_protected_from_expiry(self) -> None:
        """A reservation with past expiry is kept if request is still pending."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        reservation_repo = ReservationRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="long-running-req",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            # Create reservation with very short TTL (1 second)
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100000,
                ttl_seconds=1,
            )

        # Wait for reservation to expire
        await asyncio.sleep(2.0)

        # Run reconcile_expired_reservations
        count = await reconcile_expired_reservations(db)

        # The reservation should NOT be expired because the request is still pending
        # The NOT EXISTS clause in the SQL protects pending requests
        assert count == 0

        # Verify reservation is still active
        row = await db.fetch_one(
            "SELECT status FROM reservations WHERE id = ?", (reservation_id,)
        )
        assert row is not None
        assert row["status"] == "active"

        # Verify request is still pending
        req_row = await db.fetch_one(
            "SELECT status FROM requests WHERE id = ?", (db_id,)
        )
        assert req_row is not None
        assert req_row["status"] == "pending"

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_non_pending_request_expired_normally(self) -> None:
        """A completed request's reservation is expired normally."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        reservation_repo = ReservationRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="completed-req",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100000,
                ttl_seconds=1,
            )
            # Mark request as completed (not pending)
            await request_repo.update_after_completion(
                db_id, status="succeeded", status_code=200
            )

        # Wait for reservation to expire
        await asyncio.sleep(2.0)

        # Run reconcile_expired_reservations
        count = await reconcile_expired_reservations(db)

        # The reservation should be expired because the request is not pending
        assert count == 1

        row = await db.fetch_one(
            "SELECT status FROM reservations WHERE id = ?", (reservation_id,)
        )
        assert row is not None
        assert row["status"] == "expired"

        await db.disconnect()


# ===========================================================================
# E. Cancelled accounting
# ===========================================================================


class TestCancelledAccounting:
    """E. Cancelled requests with cost remain in usage windows."""

    @pytest.mark.asyncio
    async def test_cancelled_request_cost_in_usage_windows(self) -> None:
        """A cancelled request with cost_microdollars > 0 appears in windows."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        usage_window_repo = UsageWindowRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="cancelled-req",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )

        # Finalize as cancelled with cost
        async with db.transaction():
            await request_repo.update_after_completion(
                db_id,
                status="cancelled",
                status_code=499,
                cost_microdollars=50000,
            )

        # Query usage windows
        windows = await usage_window_repo.get_usage_windows(
            account_id=1,
            now_iso="2026-01-01 00:00:00",
        )

        # The cost should appear in the 5h window
        # (and consequently in 7d and 30d windows)
        assert windows["5h"] == 50000
        assert windows["7d"] == 50000
        assert windows["30d"] == 50000

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_zero_cost_cancelled_not_counted(self) -> None:
        """A cancelled request with zero cost is not counted in windows."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        usage_window_repo = UsageWindowRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="cancelled-zero-cost",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )

        # Finalize as cancelled with zero cost
        async with db.transaction():
            await request_repo.update_after_completion(
                db_id,
                status="cancelled",
                status_code=499,
                cost_microdollars=0,
            )

        # Query usage windows
        windows = await usage_window_repo.get_usage_windows(
            account_id=1,
            now_iso="2026-01-01 00:00:00",
        )

        # Zero cost should not be counted
        assert windows["5h"] == 0
        assert windows["7d"] == 0
        assert windows["30d"] == 0

        await db.disconnect()


# ===========================================================================
# F. Cache-only price update
# ===========================================================================


class TestCacheOnlyPriceUpdate:
    """F. Cache-only rate snapshots are persisted."""

    @pytest.mark.asyncio
    async def test_cache_only_snapshot_persisted(self) -> None:
        """A price snapshot with only cache rates is stored correctly."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        price_repo = PriceRepository(db)

        async with db.transaction():
            await price_repo.record_snapshot(
                model_id="gpt-4",
                input_price_per_1k=None,
                output_price_per_1k=None,
                input_per_million_microdollars=None,
                output_per_million_microdollars=None,
                cache_read_per_million_microdollars=500000,
                cache_write_per_million_microdollars=1000000,
                source="cache-only",
            )

        snapshot = await price_repo.get_latest_snapshot("gpt-4")
        assert snapshot is not None
        assert snapshot.cache_read_per_million_microdollars == 500000
        assert snapshot.cache_write_per_million_microdollars == 1000000
        assert snapshot.input_per_million_microdollars is None
        assert snapshot.output_per_million_microdollars is None
        assert snapshot.source == "cache-only"

        await db.disconnect()


# ===========================================================================
# G. Cache-only cost calculation
# ===========================================================================


class TestCacheOnlyCostCalculation:
    """G. Cache-only tokens produce nonzero cost."""

    @pytest.mark.asyncio
    async def test_cache_read_only_cost(self) -> None:
        """Only cache_read_tokens > 0 produces nonzero cost."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        price_repo = PriceRepository(db)
        cost_calculator = CostCalculator(price_repo)

        async with db.transaction():
            await price_repo.record_snapshot(
                model_id="gpt-4",
                input_price_per_1k=None,
                output_price_per_1k=None,
                input_per_million_microdollars=None,
                output_per_million_microdollars=None,
                cache_read_per_million_microdollars=500000,
                cache_write_per_million_microdollars=1000000,
                source="cache-only",
            )

        cost, exactness = await cost_calculator.calculate_cost(
            model_id="gpt-4",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1000,
            cache_write_tokens=0,
        )

        # Should be nonzero cost from cache reads
        assert cost > 0
        # Exactness should be "derived" since all required rates are present
        assert exactness == "derived"

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_cache_write_only_cost(self) -> None:
        """Only cache_write_tokens > 0 produces nonzero cost."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        price_repo = PriceRepository(db)
        cost_calculator = CostCalculator(price_repo)

        async with db.transaction():
            await price_repo.record_snapshot(
                model_id="gpt-4",
                input_price_per_1k=None,
                output_price_per_1k=None,
                input_per_million_microdollars=None,
                output_per_million_microdollars=None,
                cache_read_per_million_microdollars=500000,
                cache_write_per_million_microdollars=1000000,
                source="cache-only",
            )

        cost, exactness = await cost_calculator.calculate_cost(
            model_id="gpt-4",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=1000,
        )

        # Should be nonzero cost from cache writes
        assert cost > 0
        assert exactness == "derived"

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_cache_read_and_write_cost(self) -> None:
        """Both cache_read and cache_write tokens produce nonzero cost."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        price_repo = PriceRepository(db)
        cost_calculator = CostCalculator(price_repo)

        async with db.transaction():
            await price_repo.record_snapshot(
                model_id="gpt-4",
                input_price_per_1k=None,
                output_price_per_1k=None,
                input_per_million_microdollars=None,
                output_per_million_microdollars=None,
                cache_read_per_million_microdollars=500000,
                cache_write_per_million_microdollars=1000000,
                source="cache-only",
            )

        cost, exactness = await cost_calculator.calculate_cost(
            model_id="gpt-4",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1000,
            cache_write_tokens=500,
        )

        # Should be nonzero cost from both
        assert cost > 0
        assert exactness == "derived"

        await db.disconnect()


# ===========================================================================
# H. Health consistency
# ===========================================================================


class TestHealthConsistency:
    """H. Health manager and runtime state agree."""

    @pytest.mark.asyncio
    async def test_health_and_runtime_state_consistency(self) -> None:
        """Both HealthManager and AccountRuntimeState agree on eligibility."""
        hm = HealthManager()
        state = AccountRuntimeState(name="acct-a", enabled=True)

        # Record quota exhaustion on both
        hm.record_quota_exhausted("acct-a", cooldown_seconds=0.1)
        state.health_state = "quota_exhausted"
        state.cooldown_until = time.time() + 0.1

        # Both should report ineligible
        assert not hm.is_account_healthy("acct-a")
        assert not state.is_eligible()

        # Wait for cooldown to expire
        time.sleep(0.2)

        # is_account_healthy calls _refresh_transient_state internally
        assert hm.is_account_healthy("acct-a")
        state.refresh_transient_state()
        assert state.is_eligible()

    @pytest.mark.asyncio
    async def test_record_success_syncs_health(self) -> None:
        """Recording success on HealthManager makes state healthy."""
        hm = HealthManager()
        state = AccountRuntimeState(name="acct-a", enabled=True)

        # Put in failure state with short cooldown
        hm.record_quota_exhausted("acct-a", cooldown_seconds=0.1)
        state.health_state = "quota_exhausted"
        state.cooldown_until = time.time() + 0.1

        # Wait for cooldown to expire
        time.sleep(0.2)

        # Record success
        hm.record_success("acct-a", "gpt-4")
        state.record_success()

        # Both should be healthy
        assert hm.is_account_healthy("acct-a")
        assert state.is_eligible()
        assert state.health_state == "healthy"

    @pytest.mark.asyncio
    async def test_failure_state_agrees(self) -> None:
        """Both HealthManager and AccountRuntimeState agree on failure state."""
        hm = HealthManager()
        state = AccountRuntimeState(name="acct-a", enabled=True)

        # Record failure on both
        hm.record_failure("acct-a", model_id="gpt-4", reason="authentication_failed")
        state.record_failure("authentication_failed")

        # Both should be unhealthy
        assert not hm.is_account_healthy("acct-a")
        assert not state.is_eligible()

        # Health states should match
        health = hm.get_account_health("acct-a")
        assert health.health_state == "authentication_failed"
        assert state.health_state == "authentication_failed"


# ===========================================================================
# I. Privacy regression
# ===========================================================================


class TestPrivacyRegression:
    """I. No request content or secrets appear in persisted data."""

    @pytest.mark.asyncio
    async def test_no_secrets_in_database(self) -> None:
        """Known markers for prompts, completions, API keys, and auth
        headers must not appear anywhere in the persisted database."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        forbidden = [
            "sk-",
            "Bearer ",
            "Authorization",
            '"prompt":',
            '"completion":',
            "password",
            "secret",
        ]

        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE '\\_%' ESCAPE '\\'"
        )
        for table in rows:
            tbl = table["name"]
            cols = await db.fetch_all(f"PRAGMA table_info({tbl})")
            for col in cols:
                col_name = col["name"]
                cell_rows = await db.fetch_all(f"SELECT {col_name} FROM {tbl}")
                for cell in cell_rows:
                    val = str(cell[0]) if cell[0] is not None else ""
                    for marker in forbidden:
                        assert marker not in val, (
                            f"Privacy violation: '{marker}' found in "
                            f"{tbl}.{col_name} = {val!r}"
                        )

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_no_secrets_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Known markers for prompts, completions, API keys, and auth
        headers must not appear in application log output during request
        finalization.  (aiosqlite DEBUG logs are excluded since they
        echo SQL parameters by design; the database itself is checked
        by test_no_secrets_in_database.)"""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        attempt_repo = AttemptRepository(db)
        reservation_repo = ReservationRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="log-privacy-test",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )

        _attempt_id = attempt_id
        _reservation_id = reservation_id
        _db_id = db_id

        class MockSelected:
            db_request_id = _db_id
            account_name = "test-acct"
            model_id = "gpt-4"
            attempt_id = _attempt_id
            reservation_id = _reservation_id
            estimated_microdollars = 100000
            attempt_number = 1

        forbidden = [
            "sk-",
            "Bearer ",
            "Authorization",
            '"prompt":',
            '"completion":',
            "password",
            "secret",
        ]

        # INFO level avoids aiosqlite DEBUG logs which echo SQL parameters
        with caplog.at_level(logging.INFO):
            await finalizer.finalize(
                MockSelected(),
                FinalizationData(
                    outcome=FinalizationOutcome.UPSTREAM_ERROR,
                    error_class="AuthenticationError",
                    status_code=401,
                    error_detail=(
                        "sk-FAKE_API_KEY Authorization Bearer "
                        '"prompt": "secret prompt content" '
                        '"completion": "secret completion" '
                        "password=secret123 secret_value"
                    ),
                ),
            )

        for record in caplog.records:
            msg = record.getMessage()
            for marker in forbidden:
                assert marker not in msg, (
                    f"Privacy violation: '{marker}' found in log: {msg[:200]}"
                )

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_error_detail_is_truncated(self) -> None:
        """Error details longer than 2048 chars are truncated.

        Phase 17 makes ``persist_redacted_error_detail`` opt-in. The
        default (fail-closed) writes ``NULL`` for ``error_detail``
        and truncates only when persistence is explicitly enabled.
        """
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        attempt_repo = AttemptRepository(db)
        reservation_repo = ReservationRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="truncate-test",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100000,
                ttl_seconds=300,
            )

        from eggpool.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
            RequestFinalizer,
        )

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )

        _attempt_id = attempt_id
        _reservation_id = reservation_id
        _db_id = db_id

        class MockSelected:
            db_request_id = _db_id
            account_name = "test-acct"
            model_id = "gpt-4"
            attempt_id = _attempt_id
            reservation_id = _reservation_id
            estimated_microdollars = 100000
            attempt_number = 1

        long_error = "x" * 5000
        await finalizer.finalize(
            MockSelected(),
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_detail=long_error,
            ),
        )

        row = await db.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        assert row["error_detail"] is not None
        assert len(row["error_detail"]) <= 2048

        await db.disconnect()
