"""Integration tests for database transactions and proxy request identity."""

from __future__ import annotations

import uuid

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    RequestRepository,
    ReservationRepository,
)


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


@pytest.mark.asyncio
async def test_transaction_commits_on_success() -> None:
    """Changes inside transaction() should persist after commit."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    proxy_id = str(uuid.uuid4())

    async with db.transaction():
        await request_repo.create_pending(
            request_id=proxy_id,
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )

    row = await db.fetch_one(
        "SELECT * FROM requests WHERE proxy_request_id = ?", (proxy_id,)
    )
    assert row is not None
    assert row["status"] == "pending"
    assert row["proxy_request_id"] == proxy_id

    await db.disconnect()


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_exception() -> None:
    """Changes inside transaction() should be rolled back on exception."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    proxy_id = str(uuid.uuid4())

    with pytest.raises(RuntimeError, match="intentional"):
        async with db.transaction():
            await request_repo.create_pending(
                request_id=proxy_id,
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            raise RuntimeError("intentional")

    row = await db.fetch_one(
        "SELECT * FROM requests WHERE proxy_request_id = ?", (proxy_id,)
    )
    assert row is None

    await db.disconnect()


@pytest.mark.asyncio
async def test_proxy_uuid_stored_exactly() -> None:
    """The proxy UUID passed to create_pending is stored verbatim."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    proxy_id = str(uuid.uuid4())

    async with db.transaction():
        await request_repo.create_pending(
            request_id=proxy_id,
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )

    row = await db.fetch_one(
        "SELECT proxy_request_id FROM requests WHERE proxy_request_id = ?",
        (proxy_id,),
    )
    assert row is not None
    assert row["proxy_request_id"] == proxy_id

    await db.disconnect()


@pytest.mark.asyncio
async def test_duplicate_proxy_uuid_fails() -> None:
    """Inserting a request with a duplicate proxy_request_id should fail."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    proxy_id = str(uuid.uuid4())

    async with db.transaction():
        await request_repo.create_pending(
            request_id=proxy_id,
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )

    with pytest.raises(Exception, match="UNIQUE"):
        async with db.transaction():
            await request_repo.create_pending(
                request_id=proxy_id,
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )

    await db.disconnect()


@pytest.mark.asyncio
async def test_no_partial_reservation_after_rollback() -> None:
    """A rolled-back transaction must not leave partial reservation rows."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    proxy_id = str(uuid.uuid4())

    with pytest.raises(RuntimeError, match="intentional"):
        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id=proxy_id,
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
            )
            raise RuntimeError("intentional")

    req_row = await db.fetch_one(
        "SELECT * FROM requests WHERE proxy_request_id = ?", (proxy_id,)
    )
    assert req_row is None

    resv_rows = await db.fetch_all("SELECT * FROM reservations")
    assert len(resv_rows) == 0

    await db.disconnect()
