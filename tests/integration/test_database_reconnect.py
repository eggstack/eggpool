"""Plan 027 — Runtime reconnect after invalidation.

End-to-end recovery flow: connection invalidation → recovery
controller → replacement connection → resumed operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from pathlib import Path

from eggpool.db.connection import Database, DatabaseLifecycleState
from eggpool.db.migrations import MigrationRunner
from eggpool.db.recovery import DatabaseRecoveryController
from eggpool.errors import DatabaseConnectionInvalidatedError
from eggpool.models.config import DatabaseRecoveryConfig

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def test_db(tmp_path: Path) -> Database:
    db_path = str(tmp_path / "reconnect_test.db")
    db = Database(path=db_path)
    await db.connect()
    await MigrationRunner(db).run()
    return db


@pytest_asyncio.fixture()
async def recovery_config() -> DatabaseRecoveryConfig:
    return DatabaseRecoveryConfig(
        enabled=True,
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=100,
    )


async def _invalidate(test_db: Database, reason: str = "test invalidation") -> None:
    """Acquire the connection lock and invalidate (simulates commit path)."""
    async with test_db._connection_lock:
        await test_db._invalidate_connection(reason=reason)


@pytest.mark.integration
async def test_reconnect_after_invalidation(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    try:
        assert test_db.lifecycle_state == DatabaseLifecycleState.READY
        assert test_db.writes_admitted is True

        async with test_db.transaction():
            await test_db.execute_write(
                "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)"
            )
            await test_db.execute_write(
                "INSERT INTO kv (k, v) VALUES (?, ?)", ("a", "1")
            )

        row = await test_db.fetch_one("SELECT v FROM kv WHERE k = ?", ("a",))
        assert row is not None
        assert row["v"] == "1"

        await _invalidate(test_db)
        assert test_db.lifecycle_state == DatabaseLifecycleState.INVALIDATED
        assert test_db.writes_admitted is False

        with pytest.raises(DatabaseConnectionInvalidatedError):
            await test_db.fetch_one("SELECT 1")

        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        assert test_db.lifecycle_state == DatabaseLifecycleState.READY
        assert test_db.writes_admitted is True

        row = await test_db.fetch_one("SELECT v FROM kv WHERE k = ?", ("a",))
        assert row is not None
        assert row["v"] == "1"
    finally:
        await controller.shutdown()


@pytest.mark.integration
async def test_writes_admitted_event_set_after_recovery(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    try:
        await _invalidate(test_db)
        assert test_db._writes_admitted_event.is_set() is False

        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        assert test_db._writes_admitted_event.is_set() is True
    finally:
        await controller.shutdown()


@pytest.mark.integration
async def test_writes_admitted_event_cleared_on_invalidation(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    try:
        assert test_db._writes_admitted_event.is_set() is True

        await _invalidate(test_db)
        assert test_db._writes_admitted_event.is_set() is False
    finally:
        await controller.shutdown()


@pytest.mark.integration
async def test_wait_for_writes_admitted_timeout(
    test_db: Database,
) -> None:
    await _invalidate(test_db)
    assert test_db.writes_admitted is False

    result = await test_db.wait_for_writes_admitted(timeout_s=0.1)
    assert result is False


@pytest.mark.integration
async def test_epoch_increments_after_recovery(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    try:
        initial_epoch = test_db.connection_epoch
        assert initial_epoch >= 1

        await _invalidate(test_db)

        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        assert test_db.connection_epoch > initial_epoch
    finally:
        await controller.shutdown()


@pytest.mark.integration
async def test_diagnostics_after_recovery(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    try:
        await _invalidate(test_db)

        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        diag = test_db.diagnostics()
        assert diag["recovery_count"] > 0
        assert diag["lifecycle_state"] == DatabaseLifecycleState.READY.value

        snap = controller.snapshot()
        assert snap.successful_recoveries > 0
        assert snap.lifecycle_state == DatabaseLifecycleState.READY
    finally:
        await controller.shutdown()
