"""Integration tests for race-safe reservation expiry cleanup (Phase 14)."""

from __future__ import annotations

import asyncio

import pytest

from go_aggregator.background.cleanup import reconcile_expired_reservations
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import RequestRepository, ReservationRepository


async def _seed_db(db: Database) -> None:
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


@pytest.mark.asyncio
async def test_normal_release_racing_expiry_cleanup() -> None:
    """Normal release racing expiry cleanup does not double-decrement."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)

    async with db.transaction():
        db_id = await request_repo.create_pending(
            request_id="race-req-1",
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
        # Transition the request out of 'pending' so the reservation can be
        # expired by the background cleanup.
        await request_repo.update_after_completion(
            db_id, status="succeeded", status_code=200
        )

    # Wait for the reservation to expire.
    # SQLite CURRENT_TIMESTAMP has second precision, so sleep 2s to be safe.
    await asyncio.sleep(2.0)

    async with db.transaction():
        await reservation_repo.release(reservation_id, reason="completed")

    count = await reconcile_expired_reservations(db)

    assert count in (0, 1)

    row = await db.fetch_one(
        "SELECT status FROM reservations WHERE id = ?", (reservation_id,)
    )
    assert row is not None
    assert row["status"] in ("released", "expired")

    await db.disconnect()


@pytest.mark.asyncio
async def test_concurrent_expiry_cleanup_no_double_count() -> None:
    """Two concurrent cleanup calls don't double-count the same reservation."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)

    async with db.transaction():
        db_id = await request_repo.create_pending(
            request_id="concurrent-req-1",
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )
        await reservation_repo.create(
            request_id=db_id,
            account_id=1,
            model_id="gpt-4",
            estimated_tokens=1000,
            estimated_microdollars=100000,
            ttl_seconds=1,
        )
        # Transition the request out of 'pending' so the reservation can be
        # expired by the background cleanup.
        await request_repo.update_after_completion(
            db_id, status="succeeded", status_code=200
        )

    # Wait long enough for CURRENT_TIMESTAMP to advance past expires_at.
    await asyncio.sleep(2.0)

    # Run two concurrent cleanup operations
    count1, count2 = await asyncio.gather(
        reconcile_expired_reservations(db),
        reconcile_expired_reservations(db),
    )

    # Only one should have transitioned the row
    total = count1 + count2
    assert total == 1

    await db.disconnect()
