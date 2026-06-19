"""Integration tests for health and event idempotency (Phase 14)."""

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
from eggpool.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)


async def _seed_db(
    db: Database,
) -> tuple[int, str, int, str]:
    """Insert required rows for FK constraints.

    Returns (account_id, request_db_id, attempt_id, reservation_id).
    """
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
            request_id="idem-req-1",
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


class _MockSelected:
    """Minimal selected-attempt shim for finalizer tests."""

    def __init__(
        self,
        db_request_id: str,
        account_name: str,
        model_id: str,
        attempt_id: int,
        reservation_id: str,
    ) -> None:
        self.db_request_id = db_request_id
        self.account_name = account_name
        self.model_id = model_id
        self.attempt_id = attempt_id
        self.reservation_id = reservation_id
        self.estimated_microdollars = 100000
        self.attempt_number = 1


@pytest.mark.asyncio
async def test_duplicate_finalization_no_duplicate_event() -> None:
    """Calling request finalizer twice does not create duplicate events."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    account_id, request_db_id, attempt_id, reservation_id = await _seed_db(db)

    request_repo = RequestRepository(db)
    health_manager = HealthManager()

    finalizer = RequestFinalizer(
        db=db,
        request_repo=request_repo,
        attempt_repo=AttemptRepository(db),
        reservation_repo=ReservationRepository(db),
        health_manager=health_manager,
    )

    selected = _MockSelected(
        db_request_id=request_db_id,
        account_name="test-acct",
        model_id="gpt-4",
        attempt_id=attempt_id,
        reservation_id=reservation_id,
    )

    # First finalization
    transitioned1 = await finalizer.finalize(
        selected,
        FinalizationData(
            outcome=FinalizationOutcome.UPSTREAM_ERROR,
            status_code=500,
            error_class="InternalServerError",
        ),
    )
    assert transitioned1 is True

    # Second finalization (duplicate)
    transitioned2 = await finalizer.finalize(
        selected,
        FinalizationData(
            outcome=FinalizationOutcome.UPSTREAM_ERROR,
            status_code=500,
            error_class="InternalServerError",
        ),
    )
    assert transitioned2 is False

    # Only one account event should exist
    events = await db.fetch_all(
        "SELECT * FROM account_events WHERE event_type = 'upstream_error'"
    )
    assert len(events) == 1

    # Health should only have been recorded once
    health = health_manager.get_account_health("test-acct")
    assert health.consecutive_failures == 1

    await db.disconnect()


@pytest.mark.asyncio
async def test_health_already_applied_prevents_double_count() -> None:
    """health_already_applied=True prevents RequestFinalizer from updating health."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    account_id, request_db_id, attempt_id, reservation_id = await _seed_db(db)

    request_repo = RequestRepository(db)
    health_manager = HealthManager()

    finalizer = RequestFinalizer(
        db=db,
        request_repo=request_repo,
        attempt_repo=AttemptRepository(db),
        reservation_repo=ReservationRepository(db),
        health_manager=health_manager,
    )

    selected = _MockSelected(
        db_request_id=request_db_id,
        account_name="test-acct",
        model_id="gpt-4",
        attempt_id=attempt_id,
        reservation_id=reservation_id,
    )

    # Finalize with health_already_applied=True
    await finalizer.finalize(
        selected,
        FinalizationData(
            outcome=FinalizationOutcome.UPSTREAM_ERROR,
            status_code=500,
            error_class="InternalServerError",
            health_already_applied=True,
        ),
    )

    # Health should NOT have been updated (consecutive_failures stays 0)
    health = health_manager.get_account_health("test-acct")
    assert health.consecutive_failures == 0

    await db.disconnect()
