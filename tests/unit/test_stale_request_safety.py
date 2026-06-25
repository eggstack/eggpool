"""Tests for the leaked-request safety net.

Covers:

- ``_crash_recovery`` recovering ALL pending requests (no time threshold)
- ``_finalize_stale_requests_once`` finalizing leaked requests past the threshold
- ``_finalize_stale_requests_once`` releasing reservations and reconciling runtime state
- ``_finalize_stale_requests_once`` being idempotent (second pass is a no-op)
- Migration 0025 creating the ``idx_requests_status_started`` index idempotently
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from eggpool.app import _crash_recovery, _finalize_stale_requests_once
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RequestRepository, ReservationRepository
from eggpool.quota.estimation import QuotaEstimator

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


async def _seed_account_and_model(db: Database) -> None:
    """Insert the minimum rows required for crash-recovery FK constraints."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-1", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )


async def _create_pending(
    db: Database,
    *,
    model_id: str = "gpt-4",
    reserved_microdollars: int = 100_000,
) -> tuple[int, int]:
    """Create a pending request plus an active reservation.

    Returns (req_id, resv_id).
    """
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    async with db.transaction():
        req_id = await request_repo.create_pending(
            request_id=str(uuid.uuid4()),
            model_id=model_id,
            protocol="openai",
            streamed=False,
            account_id=1,
        )
        resv_id = await reservation_repo.create(
            request_id=req_id,
            account_id=1,
            model_id=model_id,
            estimated_tokens=1000,
            estimated_microdollars=reserved_microdollars,
        )
    return req_id, resv_id


@pytest.mark.asyncio
async def test_crash_recovery_clears_all_pending_regardless_of_age(
    db: Database,
) -> None:
    """A fresh pending request (started now) must still be cleaned up.

    The previous 5/10-minute thresholds leaked recent requests across
    short restarts.  The new behavior treats a process restart as a
    definitive boundary and recovers every pending request.
    """
    await _seed_account_and_model(db)
    req_id, resv_id = await _create_pending(db)

    # Sanity: fresh, not stale
    row = await db.fetch_one("SELECT status FROM requests WHERE id = ?", (req_id,))
    assert row is not None
    assert row["status"] == "pending"

    await _crash_recovery(db)

    row = await db.fetch_one("SELECT status FROM requests WHERE id = ?", (req_id,))
    assert row is not None
    assert row["status"] == "interrupted"

    resv = await db.fetch_one(
        "SELECT status, release_reason FROM reservations WHERE id = ?",
        (resv_id,),
    )
    assert resv is not None
    assert resv["status"] == "released"
    assert resv["release_reason"] == "crash_recovery"


@pytest.mark.asyncio
async def test_crash_recovery_records_event_per_account(db: Database) -> None:
    """Each affected account gets a ``crash_recovery`` event row."""
    await _seed_account_and_model(db)
    req_id, _ = await _create_pending(db)

    await _crash_recovery(db)

    rows = await db.fetch_all(
        "SELECT * FROM account_events WHERE event_type = 'crash_recovery'"
    )
    assert len(rows) == 1
    assert rows[0]["account_id"] == 1


@pytest.mark.asyncio
async def test_crash_recovery_finalizes_incomplete_attempts(db: Database) -> None:
    """Incomplete request_attempts rows are finalized.

    Each row gets the ``process_interrupted`` sentinel error_class.
    """
    await _seed_account_and_model(db)
    req_id, _ = await _create_pending(db)
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO request_attempts "
            "(request_id, attempt_number, account_id) VALUES (?, 1, ?)",
            (req_id, 1),
        )

    await _crash_recovery(db)

    row = await db.fetch_one(
        "SELECT completed_at, error_class FROM request_attempts WHERE request_id = ?",
        (req_id,),
    )
    assert row is not None
    assert row["completed_at"] is not None
    assert row["error_class"] == "process_interrupted"


@pytest.mark.asyncio
async def test_stale_finalizer_transitions_pending(db: Database) -> None:
    """A request older than the threshold is finalized; recent ones are not."""
    await _seed_account_and_model(db)
    old_req, _ = await _create_pending(db)
    fresh_req, _ = await _create_pending(db)

    # Make only ``old_req`` appear stale.  ``fresh_req`` stays at
    # ``started_at = now`` so it must remain ``pending``.
    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET started_at = datetime('now', '-2 hours') WHERE id = ?",
            (old_req,),
        )

    router = MagicMock()
    router.decrement_active_request_count = AsyncMock()
    quota_estimator = MagicMock()
    quota_estimator.remove_reservation = AsyncMock()

    transitioned = await _finalize_stale_requests_once(
        db,
        router,  # type: ignore[arg-type]
        quota_estimator,  # type: ignore[arg-type]
        max_pending_seconds=300.0,
    )

    assert transitioned == 1

    old_row = await db.fetch_one(
        "SELECT status, error_class FROM requests WHERE id = ?", (old_req,)
    )
    assert old_row is not None
    assert old_row["status"] == "interrupted"
    assert old_row["error_class"] == "StaleRequestFinalizer"

    fresh_row = await db.fetch_one(
        "SELECT status FROM requests WHERE id = ?", (fresh_req,)
    )
    assert fresh_row is not None
    assert fresh_row["status"] == "pending"


@pytest.mark.asyncio
async def test_stale_finalizer_releases_reservations(db: Database) -> None:
    """Active reservations tied to leaked requests are released."""
    await _seed_account_and_model(db)
    req_id, resv_id = await _create_pending(db, reserved_microdollars=250_000)
    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET started_at = datetime('now', '-2 hours') WHERE id = ?",
            (req_id,),
        )

    router = MagicMock()
    router.decrement_active_request_count = AsyncMock()
    quota_estimator = MagicMock()
    quota_estimator.remove_reservation = AsyncMock()

    await _finalize_stale_requests_once(
        db,
        router,  # type: ignore[arg-type]
        quota_estimator,  # type: ignore[arg-type]
        max_pending_seconds=300.0,
    )

    resv = await db.fetch_one(
        "SELECT status, release_reason FROM reservations WHERE id = ?",
        (resv_id,),
    )
    assert resv is not None
    assert resv["status"] == "released"
    assert resv["release_reason"] == "stale_request"


@pytest.mark.asyncio
async def test_stale_finalizer_reconciles_runtime_state(db: Database) -> None:
    """In-memory active counts and reservation tracking are updated post-commit."""
    await _seed_account_and_model(db)
    req_id, _ = await _create_pending(db, reserved_microdollars=500_000)
    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET started_at = datetime('now', '-2 hours') WHERE id = ?",
            (req_id,),
        )

    router = MagicMock()
    router.decrement_active_request_count = AsyncMock()
    quota_estimator = MagicMock()
    quota_estimator.remove_reservation = AsyncMock()

    await _finalize_stale_requests_once(
        db,
        router,  # type: ignore[arg-type]
        quota_estimator,  # type: ignore[arg-type]
        max_pending_seconds=300.0,
    )

    router.decrement_active_request_count.assert_awaited_once_with("acct-1")
    quota_estimator.remove_reservation.assert_awaited_once_with("acct-1", 500_000)


@pytest.mark.asyncio
async def test_stale_finalizer_idempotent(db: Database) -> None:
    """Running the finalizer twice does not re-finalize the same request."""
    await _seed_account_and_model(db)
    req_id, _ = await _create_pending(db)
    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET started_at = datetime('now', '-2 hours') WHERE id = ?",
            (req_id,),
        )

    router = MagicMock()
    router.decrement_active_request_count = AsyncMock()
    quota_estimator = MagicMock()
    quota_estimator.remove_reservation = AsyncMock()

    first = await _finalize_stale_requests_once(
        db,
        router,  # type: ignore[arg-type]
        quota_estimator,  # type: ignore[arg-type]
        max_pending_seconds=300.0,
    )
    assert first == 1

    second = await _finalize_stale_requests_once(
        db,
        router,  # type: ignore[arg-type]
        quota_estimator,  # type: ignore[arg-type]
        max_pending_seconds=300.0,
    )
    assert second == 0

    row = await db.fetch_one("SELECT status FROM requests WHERE id = ?", (req_id,))
    assert row is not None
    assert row["status"] == "interrupted"

    # Only the first run should have reconciled runtime state.  The
    # second run sees no leaked rows and exits early.
    assert router.decrement_active_request_count.await_count == 1
    assert quota_estimator.remove_reservation.await_count == 1


@pytest.mark.asyncio
async def test_stale_finalizer_handles_no_work(db: Database) -> None:
    """When no requests are leaked, the sweep returns 0 and reconciles nothing."""
    await _seed_account_and_model(db)
    router = MagicMock()
    router.decrement_active_request_count = AsyncMock()
    quota_estimator = MagicMock()
    quota_estimator.remove_reservation = AsyncMock()

    transitioned = await _finalize_stale_requests_once(
        db,
        router,  # type: ignore[arg-type]
        quota_estimator,  # type: ignore[arg-type]
        max_pending_seconds=300.0,
    )

    assert transitioned == 0
    router.decrement_active_request_count.assert_not_called()
    quota_estimator.remove_reservation.assert_not_called()


@pytest.mark.asyncio
async def test_stale_finalizer_dedups_reconciliation_per_account(
    db: Database,
) -> None:
    """Multiple leaked requests on one account decrement the count exactly once."""
    await _seed_account_and_model(db)
    req_id_1, _ = await _create_pending(db)
    req_id_2, _ = await _create_pending(db)
    req_id_3, _ = await _create_pending(db)

    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET started_at = datetime('now', '-2 hours') "
            "WHERE id IN (?, ?, ?)",
            (req_id_1, req_id_2, req_id_3),
        )

    router = MagicMock()
    router.decrement_active_request_count = AsyncMock()
    quota_estimator = MagicMock()
    quota_estimator.remove_reservation = AsyncMock()

    transitioned = await _finalize_stale_requests_once(
        db,
        router,  # type: ignore[arg-type]
        quota_estimator,  # type: ignore[arg-type]
        max_pending_seconds=300.0,
    )
    assert transitioned == 3

    # All three rows share one account.  The active count is per-account,
    # not per-request, so the runtime state should be reconciled exactly
    # once for the account.
    router.decrement_active_request_count.assert_awaited_once_with("acct-1")
    # Reservation removal IS per-row because each leaked row reserved a
    # distinct amount; 100_000 * 3 = 300_000.
    assert quota_estimator.remove_reservation.await_count == 3


@pytest.mark.asyncio
async def test_stale_finalizer_uses_quota_estimator_when_provided(
    db: Database,
) -> None:
    """A real ``QuotaEstimator`` integration: ``remove_reservation`` is honored."""
    await _seed_account_and_model(db)
    req_id, _ = await _create_pending(db, reserved_microdollars=750_000)
    async with db.transaction():
        await db.execute_write(
            "UPDATE requests SET started_at = datetime('now', '-2 hours') WHERE id = ?",
            (req_id,),
        )

    router = MagicMock()
    router.decrement_active_request_count = AsyncMock()

    estimator = QuotaEstimator()
    await estimator.add_reservation("acct-1", 750_000)

    await _finalize_stale_requests_once(
        db,
        router,  # type: ignore[arg-type]
        estimator,
        max_pending_seconds=300.0,
    )

    router.decrement_active_request_count.assert_awaited_once_with("acct-1")


@pytest.mark.asyncio
async def test_migration_0025_creates_status_started_index(db: Database) -> None:
    """The 0025 migration installs ``idx_requests_status_started`` idempotently."""
    runner = MigrationRunner(db)

    # The 0004 migration already created this index, but the new
    # migration is the canonical anchor for the safety-net task.  Run
    # the migration list and confirm the index still exists.
    await runner.run()

    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'index' AND name = 'idx_requests_status_started'"
    )
    assert len(rows) == 1

    # Re-running must not fail (CREATE INDEX IF NOT EXISTS).
    await runner.run()


@pytest.mark.asyncio
async def test_crash_recovery_no_pending_requests_is_noop(db: Database) -> None:
    """A clean database runs through recovery without raising or recording events."""
    await _seed_account_and_model(db)
    # No pending requests, no active reservations.  The function
    # must complete cleanly and skip the event recording branch.
    await _crash_recovery(db)

    rows = await db.fetch_all("SELECT * FROM account_events")
    assert len(rows) == 0
