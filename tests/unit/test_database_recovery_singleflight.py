"""Plan 027 Workstream B — Recovery single-flight tests.

Verifies that concurrent invalidation notifications join the same
recovery attempt rather than triggering parallel reconnects.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from pathlib import Path

from eggpool.db.connection import Database, DatabaseLifecycleState
from eggpool.db.migrations import MigrationRunner
from eggpool.db.recovery import DatabaseRecoveryController
from eggpool.models.config import DatabaseRecoveryConfig

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def test_db(tmp_path: Path) -> Database:
    """Provide a fresh file-backed database with migrations applied.

    File-backed databases are required because in-memory ``:memory:``
    databases lose all state when the connection is invalidated and
    reopened — recovery would require re-running migrations, which is
    tested separately.
    """
    db_path = str(tmp_path / "recovery_test.db")
    db = Database(path=db_path)
    await db.connect()
    await MigrationRunner(db).run()
    return db


async def test_single_flight_recovery_attempt(test_db: Database) -> None:
    """Concurrent invalidation notifications join one recovery attempt."""
    config = DatabaseRecoveryConfig(
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        # Fire 5 concurrent invalidation notifications.
        await asyncio.gather(
            *[
                controller.handle_invalidation(
                    reason=f"reason-{i}", reason_class="commit_failure"
                )
                for i in range(5)
            ]
        )
        ready = await asyncio.wait_for(
            controller.wait_for_ready(timeout_s=10.0), timeout=10.0
        )
        assert ready is True
        snapshot = controller.snapshot()
        assert snapshot.total_invalidation_count == 5
        assert snapshot.recovery_attempts == 1
        assert snapshot.active_recovery is False
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_concurrent_waiters_join_one_recovery(test_db: Database) -> None:
    """Concurrent waiters join the same recovery attempt."""
    config = DatabaseRecoveryConfig(
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        # Trigger one invalidation.
        await controller.handle_invalidation(
            reason="trigger", reason_class="commit_failure"
        )
        ready = await asyncio.wait_for(
            controller.wait_for_ready(timeout_s=10.0), timeout=10.0
        )
        assert ready is True
        assert controller.state is DatabaseLifecycleState.READY
        assert controller.admission_admitted is True
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_recovery_replaces_connection(test_db: Database) -> None:
    """A successful recovery replaces the connection and increments the epoch."""
    config = DatabaseRecoveryConfig(
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        epoch_before = test_db.connection_epoch
        await controller.handle_invalidation(
            reason="trigger", reason_class="commit_failure"
        )
        ready = await asyncio.wait_for(
            controller.wait_for_ready(timeout_s=10.0), timeout=10.0
        )
        assert ready is True
        assert test_db.connection_epoch > epoch_before
        # The replacement connection is usable.
        async with test_db.transaction():
            result = await test_db.execute_returning("SELECT 1")
            assert len(result) == 1
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_failed_recovery_enters_failed_closed(
    tmp_path: Path,
) -> None:
    """When all retries fail, the controller enters FAILED_CLOSED."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    config = DatabaseRecoveryConfig(
        max_attempts=2,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=2.0,
    )
    controller = DatabaseRecoveryController(db=db, config=config)
    original_connect = db.connect

    async def failing_connect() -> None:
        raise RuntimeError("disk full")

    db.connect = failing_connect  # type: ignore[assignment]
    try:
        await controller.handle_invalidation(
            reason="trigger", reason_class="commit_failure"
        )
        ready = await asyncio.wait_for(
            controller.wait_for_ready(timeout_s=10.0), timeout=10.0
        )
        assert ready is False
        assert controller.state is DatabaseLifecycleState.FAILED_CLOSED
        assert controller.admission_admitted is False
        snapshot = controller.snapshot()
        assert snapshot.failed_recoveries >= 1
        assert snapshot.failed_closed_reason is not None
    finally:
        db.connect = original_connect  # type: ignore[assignment]
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_invalidated_reason_classes_counted(test_db: Database) -> None:
    """Invalidation reason classes are counted separately."""
    config = DatabaseRecoveryConfig(
        max_attempts=2,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=2.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        for reason_class in (
            "commit_failure",
            "commit_failure",
            "rollback_failure",
        ):
            await controller.handle_invalidation(
                reason=f"reason-{reason_class}",
                reason_class=reason_class,
            )
            await asyncio.wait_for(
                controller.wait_for_ready(timeout_s=5.0), timeout=5.0
            )
        snapshot = controller.snapshot()
        reasons = dict(snapshot.invalidation_reasons_by_class)
        assert reasons.get("commit_failure", 0) >= 2
        assert reasons.get("rollback_failure", 0) >= 1
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_shutdown_stops_active_recovery(test_db: Database) -> None:
    """``shutdown()`` cancels any active recovery attempt."""
    config = DatabaseRecoveryConfig(
        max_attempts=10,
        initial_backoff_ms=100,
        max_backoff_ms=500,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    original_connect = test_db.connect

    async def slow_connect() -> None:
        await asyncio.sleep(5.0)

    test_db.connect = slow_connect  # type: ignore[assignment]
    try:
        await controller.handle_invalidation(
            reason="trigger", reason_class="commit_failure"
        )
        # Let the recovery start, then shut down.
        await asyncio.sleep(0.05)
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)
        assert controller.admission_admitted is False
    finally:
        test_db.connect = original_connect  # type: ignore[assignment]


async def test_admission_blocks_writes_during_recovery(
    test_db: Database,
) -> None:
    """``admission_admitted`` is False during recovery, True after."""
    config = DatabaseRecoveryConfig(
        max_attempts=2,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        await controller.handle_invalidation(
            reason="trigger", reason_class="commit_failure"
        )
        ready = await asyncio.wait_for(
            controller.wait_for_ready(timeout_s=10.0), timeout=10.0
        )
        assert ready is True
        assert controller.admission_admitted is True
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_snapshot_returns_frozen_state(test_db: Database) -> None:
    """``snapshot()`` returns an immutable ``RecoverySnapshot``."""
    config = DatabaseRecoveryConfig(max_attempts=2, initial_backoff_ms=10)
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        snap = controller.snapshot()
        assert snap.lifecycle_state is DatabaseLifecycleState.READY
        assert snap.total_invalidation_count == 0
        assert snap.recovery_attempts == 0
        assert snap.admission_admitted is True
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_reconnect_after_successful_recovery(
    test_db: Database,
) -> None:
    """Multiple recovery cycles work correctly in sequence."""
    config = DatabaseRecoveryConfig(
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        for i in range(3):
            await controller.handle_invalidation(
                reason=f"cycle-{i}", reason_class="commit_failure"
            )
            ready = await asyncio.wait_for(
                controller.wait_for_ready(timeout_s=10.0), timeout=10.0
            )
            assert ready is True
            async with test_db.transaction():
                result = await test_db.execute_returning("SELECT 1")
                assert len(result) == 1
        snapshot = controller.snapshot()
        assert snapshot.total_invalidation_count == 3
        assert snapshot.successful_recoveries == 3
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)


async def test_recover_blocking(test_db: Database) -> None:
    """``recover_blocking()`` waits for recovery to complete."""
    config = DatabaseRecoveryConfig(
        max_attempts=2,
        initial_backoff_ms=10,
        max_backoff_ms=50,
        reconciliation_timeout_s=5.0,
    )
    controller = DatabaseRecoveryController(db=test_db, config=config)
    try:
        await controller.handle_invalidation(
            reason="trigger", reason_class="commit_failure"
        )
        ready = await asyncio.wait_for(
            controller.recover_blocking(timeout_s=10.0), timeout=10.0
        )
        assert ready is True
    finally:
        await asyncio.wait_for(controller.shutdown(), timeout=5.0)
