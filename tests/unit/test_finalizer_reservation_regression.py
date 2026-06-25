"""Regression tests for in-memory reservation accounting on the finalize path.

Phase 7 audit found that the non-retryable error path leaked the
in-memory reservation cost held by ``QuotaEstimator`` because
``RequestFinalizer.finalize`` guarded its in-memory cleanup on
``not data.health_already_applied`` — a flag that the coordinator
also sets on the non-retryable 429/402/5xx paths where it had not
cleaned up the in-memory reservation itself.

These tests exercise the relevant finalize behavior directly so a
future regression is caught before it can inflate the in-memory
reserved-cost counters seen by the scorer.
"""

from __future__ import annotations

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.quota.estimation import QuotaEstimator
from eggpool.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)


class _MockSelected:
    """Minimal selected-attempt shim mirroring the test_health_idempotency one."""

    def __init__(
        self,
        db_request_id: str,
        account_name: str,
        model_id: str,
        attempt_id: int,
        reservation_id: str,
        estimated_microdollars: int,
    ) -> None:
        self.db_request_id = db_request_id
        self.account_name = account_name
        self.model_id = model_id
        self.attempt_id = attempt_id
        self.reservation_id = reservation_id
        self.estimated_microdollars = estimated_microdollars
        self.attempt_number = 1
        self.provider_id = "default"


async def _seed_db(db: Database) -> tuple[int, str, int, str]:
    """Seed account/request/attempt/reservation rows for finalize tests."""
    async with db.transaction():
        account_id = await db.execute_insert(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )

    request_repo = RequestRepository(db)
    async with db.transaction():
        request_db_id = await request_repo.create_pending(
            request_id="inflate-req-1",
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=account_id,
        )

    attempt_repo = AttemptRepository(db)
    async with db.transaction():
        attempt_id = await attempt_repo.create(
            request_id=request_db_id,
            attempt_number=1,
            account_id=account_id,
        )

    reservation_repo = ReservationRepository(db)
    async with db.transaction():
        reservation_id = await reservation_repo.create(
            request_id=request_db_id,
            account_id=account_id,
            model_id="gpt-4",
            estimated_tokens=1000,
            estimated_microdollars=100000,
        )

    return account_id, request_db_id, attempt_id, reservation_id


@pytest.mark.asyncio
async def test_non_retryable_failure_releases_in_memory_reservation() -> None:
    """A 429 with health_already_applied=True must still clear the in-memory cost.

    Without this, repeated 429s from a single account would accumulate
    phantom reserved cost forever and make the account appear over-budget
    in the scorer even though no real work was performed.
    """
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    _account_id, request_db_id, attempt_id, reservation_id = await _seed_db(db)

    request_repo = RequestRepository(db)
    quota_estimator = QuotaEstimator()
    await quota_estimator.add_reservation("test-acct", 100000)

    assert await quota_estimator.get_account_reserved_cost("test-acct") == 100000

    finalizer = RequestFinalizer(
        db=db,
        request_repo=request_repo,
        attempt_repo=AttemptRepository(db),
        reservation_repo=ReservationRepository(db),
        health_manager=HealthManager(),
        quota_estimator=quota_estimator,
    )

    selected = _MockSelected(
        db_request_id=request_db_id,
        account_name="test-acct",
        model_id="gpt-4",
        attempt_id=attempt_id,
        reservation_id=reservation_id,
        estimated_microdollars=100000,
    )

    transitioned = await finalizer.finalize(
        selected,
        FinalizationData(
            outcome=FinalizationOutcome.UPSTREAM_ERROR,
            status_code=429,
            error_class="RateLimitError",
            health_already_applied=True,
        ),
    )

    assert transitioned is True
    assert await quota_estimator.get_account_reserved_cost("test-acct") == 0

    resv_row = await db.fetch_one(
        "SELECT status FROM reservations WHERE id = ?", (reservation_id,)
    )
    assert resv_row is not None
    assert resv_row["status"] == "released"

    await db.disconnect()


@pytest.mark.asyncio
async def test_idempotent_finalize_does_not_underflow_reservation() -> None:
    """Calling finalize twice must not drive the in-memory cost negative.

    The second call observes ``reservation_released=False`` because
    the SQLite row is already released, so the in-memory decrement
    must not run a second time.
    """
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    _account_id, request_db_id, attempt_id, reservation_id = await _seed_db(db)

    request_repo = RequestRepository(db)
    quota_estimator = QuotaEstimator()
    await quota_estimator.add_reservation("test-acct", 100000)

    finalizer = RequestFinalizer(
        db=db,
        request_repo=request_repo,
        attempt_repo=AttemptRepository(db),
        reservation_repo=ReservationRepository(db),
        health_manager=HealthManager(),
        quota_estimator=quota_estimator,
    )

    selected = _MockSelected(
        db_request_id=request_db_id,
        account_name="test-acct",
        model_id="gpt-4",
        attempt_id=attempt_id,
        reservation_id=reservation_id,
        estimated_microdollars=100000,
    )

    first = await finalizer.finalize(
        selected,
        FinalizationData(
            outcome=FinalizationOutcome.CLIENT_ERROR,
            status_code=429,
            error_class="RateLimitError",
            health_already_applied=True,
        ),
    )
    assert first is True
    assert await quota_estimator.get_account_reserved_cost("test-acct") == 0

    second = await finalizer.finalize(
        selected,
        FinalizationData(
            outcome=FinalizationOutcome.CLIENT_ERROR,
            status_code=429,
            error_class="RateLimitError",
            health_already_applied=True,
        ),
    )
    assert second is False
    assert await quota_estimator.get_account_reserved_cost("test-acct") == 0

    await db.disconnect()
