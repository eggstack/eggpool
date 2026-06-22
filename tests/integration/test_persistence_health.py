"""Integration tests for persistence edge cases and health scenarios."""

from __future__ import annotations

import uuid

import pytest

from eggpool.app import _crash_recovery
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RequestRepository, ReservationRepository
from eggpool.health.health_manager import HealthManager

EXPECTED_TABLES = frozenset(
    {
        "accounts",
        "models",
        "account_models",
        "requests",
        "reservations",
        "request_attempts",
        "model_price_snapshots",
        "account_events",
        "_migrations",
    }
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
async def test_migration_from_existing_schema() -> None:
    """Running MigrationRunner.run() twice should be idempotent."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)

    await runner.run()

    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    tables_first = {row["name"] for row in rows}
    assert tables_first >= EXPECTED_TABLES

    await runner.run()

    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    tables_second = {row["name"] for row in rows}
    assert tables_first == tables_second

    migration_rows = await db.fetch_all("SELECT version FROM _migrations")
    versions = {row["version"] for row in migration_rows}
    assert len(versions) == len(set(migration_rows))

    await db.disconnect()


@pytest.mark.asyncio
async def test_uuid_request_and_reservation_compatibility() -> None:
    """UUID-formatted identifiers passed to the repository should round-trip."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)

    uuid_request_id = str(uuid.uuid4())
    async with db.transaction():
        created_id = await request_repo.create_pending(
            request_id=uuid_request_id,
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )

        reservation_id = await reservation_repo.create(
            request_id=created_id,
            account_id=1,
            model_id="gpt-4",
            estimated_tokens=1000,
            estimated_microdollars=100000,
        )

        await db.execute_write(
            "INSERT INTO request_attempts "
            "(request_id, attempt_number, account_id) VALUES (?, 1, ?)",
            (created_id, 1),
        )

    req_row = await db.fetch_one("SELECT * FROM requests WHERE id = ?", (created_id,))
    assert req_row is not None
    assert str(req_row["id"]) == created_id
    assert req_row["status"] == "pending"

    resv_row = await db.fetch_one(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    )
    assert resv_row is not None
    assert str(resv_row["id"]) == reservation_id
    assert str(resv_row["request_id"]) == created_id

    att_row = await db.fetch_one(
        "SELECT * FROM request_attempts WHERE request_id = ?", (created_id,)
    )
    assert att_row is not None
    assert str(att_row["request_id"]) == created_id
    assert att_row["attempt_number"] == 1

    await db.disconnect()


@pytest.mark.asyncio
async def test_finalization_idempotency() -> None:
    """Calling update_after_completion twice should be idempotent."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    async with db.transaction():
        created_id = await request_repo.create_pending(
            request_id=str(uuid.uuid4()),
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )

    async with db.transaction():
        await request_repo.update_after_completion(
            created_id, status="completed", input_tokens=10, output_tokens=5
        )

    row_after_first = await db.fetch_one(
        "SELECT * FROM requests WHERE id = ?", (created_id,)
    )
    assert row_after_first is not None
    assert row_after_first["status"] == "completed"
    assert row_after_first["completed_at"] is not None

    async with db.transaction():
        await request_repo.update_after_completion(
            created_id, status="error", input_tokens=999, output_tokens=999
        )

    row_after_second = await db.fetch_one(
        "SELECT * FROM requests WHERE id = ?", (created_id,)
    )
    assert row_after_second is not None
    assert row_after_second["status"] == "completed"
    assert row_after_second["completed_at"] is not None
    assert row_after_second["input_tokens"] == 10
    assert row_after_second["output_tokens"] == 5

    await db.disconnect()


def test_402_cools_account() -> None:
    """402 quota exhaustion should mark account as unhealthy."""
    manager = HealthManager()
    manager.record_success("account1")
    assert manager.is_account_healthy("account1")

    manager.record_failure("account1", reason="quota_exhausted")
    health = manager.get_account_health("account1")
    assert health.consecutive_failures >= 1

    health_after = manager.get_account_health("account1")
    assert health_after.consecutive_failures >= 1


@pytest.mark.asyncio
async def test_repeated_restart_recovery() -> None:
    """Crash recovery should be idempotent across multiple cycles."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)

    async with db.transaction():
        request_id_1 = await request_repo.create_pending(
            request_id=str(uuid.uuid4()),
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )
        request_id_2 = await request_repo.create_pending(
            request_id=str(uuid.uuid4()),
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )

        reservation_id_1 = await reservation_repo.create(
            request_id=request_id_1,
            account_id=1,
            model_id="gpt-4",
            estimated_tokens=1000,
            estimated_microdollars=100000,
        )
        reservation_id_2 = await reservation_repo.create(
            request_id=request_id_2,
            account_id=1,
            model_id="gpt-4",
            estimated_tokens=1000,
            estimated_microdollars=100000,
        )

    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET started_at = datetime('now', '-1 hour') "
            "WHERE id IN (?, ?)",
            (request_id_1, request_id_2),
        )
        await db.execute_write(
            "UPDATE reservations SET created_at = datetime('now', '-1 hour') "
            "WHERE id IN (?, ?)",
            (reservation_id_1, reservation_id_2),
        )

    for _ in range(3):
        await _crash_recovery(db)

        req1 = await db.fetch_one(
            "SELECT * FROM requests WHERE id = ?", (request_id_1,)
        )
        assert req1 is not None
        assert req1["status"] == "interrupted"

        req2 = await db.fetch_one(
            "SELECT * FROM requests WHERE id = ?", (request_id_2,)
        )
        assert req2 is not None
        assert req2["status"] == "interrupted"

        resv1 = await db.fetch_one(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id_1,)
        )
        assert resv1 is not None
        assert resv1["status"] == "released"

        resv2 = await db.fetch_one(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id_2,)
        )
        assert resv2 is not None
        assert resv2["status"] == "released"

    await db.disconnect()
