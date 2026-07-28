"""Plan 027 — Readiness probe integration during recovery.

Verifies that the /readyz probe correctly reports degraded status
during database recovery and returns to ready after recovery
completes.
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
from eggpool.health.writable_probe import DatabaseWritableProbe, ProbeStatus
from eggpool.models.config import DatabaseRecoveryConfig

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def test_db(tmp_path: Path) -> Database:
    """Provide a file-backed database with migrations applied."""
    db_path = str(tmp_path / "readiness_test.db")
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


@pytest.mark.integration
async def test_readiness_degraded_during_recovery(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    """Probe reports degraded during recovery and ready after."""
    probe = DatabaseWritableProbe(
        test_db, interval_s=60.0, freshness_s=120.0, initial_probe=True
    )
    controller = DatabaseRecoveryController(
        db=test_db, config=recovery_config, readiness_probe=probe
    )
    try:
        await probe.start()
        snapshot = await probe.force_probe()
        assert snapshot.status == ProbeStatus.HEALTHY

        await test_db._invalidate_connection(reason="test invalidation")  # type: ignore[reportPrivateUsage]
        await asyncio.sleep(0)

        snapshot = await probe.force_probe()
        assert snapshot.status in (
            ProbeStatus.UNHEALTHY,
            ProbeStatus.UNKNOWN,
            ProbeStatus.STALE,
        )

        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        snapshot = await probe.force_probe()
        assert snapshot.status == ProbeStatus.HEALTHY
    finally:
        await probe.stop()
        await controller.shutdown()


@pytest.mark.integration
async def test_readiness_probe_force_refresh_after_recovery(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    """force_probe_nowait() refreshes snapshot after recovery."""
    probe = DatabaseWritableProbe(
        test_db, interval_s=60.0, freshness_s=120.0, initial_probe=True
    )
    controller = DatabaseRecoveryController(
        db=test_db, config=recovery_config, readiness_probe=probe
    )
    try:
        await probe.start()

        await test_db._invalidate_connection(reason="test invalidation")  # type: ignore[reportPrivateUsage]
        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        snapshot = await probe.force_probe()
        assert snapshot.status == ProbeStatus.HEALTHY
        assert snapshot.probe_count >= 2
    finally:
        await probe.stop()
        await controller.shutdown()


@pytest.mark.integration
async def test_recovery_controller_snapshot_shows_state(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    """Recovery snapshot reflects invalidation and recovery counts."""
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    try:
        await test_db._invalidate_connection(reason="test invalidation")  # type: ignore[reportPrivateUsage]
        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        snap = controller.snapshot()
        assert snap.total_invalidation_count >= 1
        assert snap.recovery_attempts >= 1
        assert snap.active_recovery is False
        assert snap.successful_recoveries >= 1
    finally:
        await controller.shutdown()


@pytest.mark.integration
async def test_startup_integrity_check_logs_result(
    tmp_path: Path,
) -> None:
    """Database connects and migrates without error."""
    db_path = str(tmp_path / "startup_test.db")
    db = Database(path=db_path)
    try:
        await db.connect()
        assert db.lifecycle_state is DatabaseLifecycleState.READY
        await MigrationRunner(db).run()
        assert db.connection_epoch >= 1
        probe = DatabaseWritableProbe(db, interval_s=60.0, initial_probe=True)
        await probe.start()
        snapshot = await probe.force_probe()
        assert snapshot.status == ProbeStatus.HEALTHY
        await probe.stop()
    finally:
        await db.disconnect()
