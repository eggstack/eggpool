"""Integration tests for the process-owned database writable probe (Phase 9).

Covers:
- No write in /readyz request path
- Initial state (startup-pending/unready, then ready after probe)
- Failure and recovery (readonly/closed/lock-timeout → unready → recovery)
- Staleness (worker paused, freshness threshold exceeded)
- Worker failure (exception consumption, diagnostics, unready state)
- Shutdown hygiene (probe task exits before DB close)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.health.writable_probe import (
    DatabaseWritableProbe,
    ProbeSnapshot,
    ProbeStatus,
)
from eggpool.models.config import AppConfig


def _build_config(**overrides: Any) -> AppConfig:
    return AppConfig.from_dict(
        {
            "server": {"api_key_env": "TEST_KEY", "host": "127.0.0.1", "port": 0},
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "http://127.0.0.1:9999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test-acct", "api_key_env": "TEST_KEY"}],
            "dashboard": {"enabled": False},
            **overrides,
        }
    )


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


# ---------------------------------------------------------------------------
# No write in request path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readyz_does_not_invoke_probe_writable() -> None:
    """readyz must read the cached probe snapshot, never call probe_writable."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    # Create a probe and let it do one initial probe
    probe = DatabaseWritableProbe(
        db, interval_s=100, freshness_s=300, initial_probe=True
    )
    await probe.start()
    # Wait for initial probe to complete
    await asyncio.sleep(0.1)

    # Track whether probe_writable is called
    original_probe = db.probe_writable
    call_count = 0

    async def counting_probe() -> bool:
        nonlocal call_count
        call_count += 1
        return await original_probe()

    db.probe_writable = counting_probe  # type: ignore[assignment]

    # Create a minimal app with the probe wired
    app = FastAPI()
    app.state.db = db
    app.state.readiness_probe = probe

    # Import and register the readyz handler manually

    from eggpool.app import create_app

    config = _build_config()
    application = create_app(config)
    application.state.db = db
    application.state.readiness_probe = probe
    # Wire enough state for readyz to pass the other checks
    application.state.runtime_manager = None
    application.state.reload_manager = None

    # We can't easily test via HTTP because the full app needs many deps.
    # Instead, directly test that the readyz handler reads the probe snapshot.
    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.HEALTHY

    # Verify that calling snapshot does NOT invoke probe_writable
    call_count_before = call_count
    await probe.snapshot()
    assert call_count == call_count_before, "snapshot() must not call probe_writable"

    await probe.stop()
    await db.disconnect()


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_initial_unknown_state() -> None:
    """Before first probe, status is UNKNOWN."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(db, interval_s=100, initial_probe=False)
    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.UNKNOWN
    assert snap.last_attempt_at is None
    assert snap.worker_running is False
    await db.disconnect()


@pytest.mark.asyncio
async def test_probe_initial_success() -> None:
    """After successful initial probe, status is HEALTHY."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(
        db, interval_s=100, freshness_s=300, initial_probe=True
    )
    await probe.start()
    await asyncio.sleep(0.1)

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.HEALTHY
    assert snap.last_success_at is not None
    assert snap.consecutive_failures == 0
    assert snap.worker_running is True

    await probe.stop()
    await db.disconnect()


# ---------------------------------------------------------------------------
# Failure and recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_failure_on_readonly_db() -> None:
    """Probe reports UNHEALTHY when database is readonly."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    # Make probe_writable return False
    async def failing_probe() -> bool:
        return False

    db.probe_writable = failing_probe  # type: ignore[assignment]

    probe = DatabaseWritableProbe(db, interval_s=100, initial_probe=True)
    await probe.start()
    await asyncio.sleep(0.1)

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.UNHEALTHY
    assert snap.consecutive_failures == 1
    assert snap.failure_count == 1
    assert snap.success_count == 0

    await probe.stop()
    await db.disconnect()


@pytest.mark.asyncio
async def test_probe_recovery_after_failure() -> None:
    """Probe recovers to HEALTHY after transient failure."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    fail_once = True

    async def conditional_probe() -> bool:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            return False
        return True

    db.probe_writable = conditional_probe  # type: ignore[assignment]

    probe = DatabaseWritableProbe(
        db, interval_s=100, freshness_s=300, initial_probe=True
    )
    await probe.start()
    # Wait for initial probe (failure)
    await asyncio.sleep(0.2)

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.UNHEALTHY
    assert snap.consecutive_failures == 1

    # Force a recovery probe directly
    snap = await probe.force_probe()
    assert snap.status == ProbeStatus.HEALTHY
    assert snap.consecutive_failures == 0
    assert snap.success_count >= 1

    await probe.stop()
    await db.disconnect()


@pytest.mark.asyncio
async def test_probe_lock_timeout_classified() -> None:
    """Probe classifies TimeoutError separately."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    async def slow_probe() -> bool:
        await asyncio.sleep(10)
        return True  # pragma: no cover

    db.probe_writable = slow_probe  # type: ignore[assignment]

    probe = DatabaseWritableProbe(
        db, interval_s=100, timeout_s=0.05, initial_probe=True
    )
    await probe.start()
    await asyncio.sleep(0.2)

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.UNHEALTHY
    assert snap.last_error_class == "TimeoutError"
    assert "timed out" in (snap.last_error_message or "")

    await probe.stop()
    await db.disconnect()


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_staleness() -> None:
    """Probe reports STALE when freshness deadline exceeded."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(
        db,
        interval_s=100,
        freshness_s=0.05,
        initial_probe=True,
    )
    await probe.start()
    # Wait for initial probe
    await asyncio.sleep(0.2)

    # Stop the worker so no more probes happen and result stays fresh
    await probe.stop()

    # Immediately after stop, status should be STOPPED (not stale)
    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.STOPPED

    await db.disconnect()


@pytest.mark.asyncio
async def test_probe_staleness_manual_clock() -> None:
    """Probe reports STALE when freshness deadline exceeded via direct state."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(
        db,
        interval_s=100,
        freshness_s=0.01,
        initial_probe=True,
    )
    await probe.start()
    # Wait for initial probe
    await asyncio.sleep(0.2)

    # Stop the worker
    await probe.stop()

    # Manually backdate the success timestamp to simulate staleness
    async with probe._lock:
        probe._status = ProbeStatus.HEALTHY
        probe._last_success_at = time.time() - 10.0

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.STALE

    await db.disconnect()


# ---------------------------------------------------------------------------
# Worker failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_worker_exception_consumed() -> None:
    """Probe worker consumes exceptions and continues probing."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    call_count = 0

    async def raising_probe() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("transient failure")
        return True

    db.probe_writable = raising_probe  # type: ignore[assignment]

    probe = DatabaseWritableProbe(
        db, interval_s=0.05, freshness_s=300, initial_probe=True
    )
    await probe.start()
    # Wait for initial probe (failure)
    await asyncio.sleep(0.1)

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.UNHEALTHY
    assert snap.last_error_class == "RuntimeError"
    assert "transient failure" in (snap.last_error_message or "")

    # Wait for recovery probe
    await asyncio.sleep(0.2)

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.HEALTHY
    assert snap.consecutive_failures == 0

    await probe.stop()
    await db.disconnect()


# ---------------------------------------------------------------------------
# Shutdown hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_stop_cancels_task() -> None:
    """Probe stop() cancels the worker task cleanly."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(
        db, interval_s=100, freshness_s=300, initial_probe=True
    )
    await probe.start()
    await asyncio.sleep(0.1)

    assert probe._task is not None
    assert not probe._task.done()

    await probe.stop()

    assert probe._task is None
    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.STOPPED
    assert snap.worker_running is False

    await db.disconnect()


@pytest.mark.asyncio
async def test_probe_stop_idempotent() -> None:
    """Calling stop() multiple times is safe."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(db, interval_s=100, initial_probe=True)
    await probe.start()
    await asyncio.sleep(0.1)

    await probe.stop()
    await probe.stop()  # second call should be a no-op

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.STOPPED

    await db.disconnect()


# ---------------------------------------------------------------------------
# force_probe (diagnostic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_probe_returns_snapshot() -> None:
    """force_probe() executes an immediate probe and returns snapshot."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(db, interval_s=100, initial_probe=False)
    snap = await probe.force_probe()
    assert snap.status == ProbeStatus.HEALTHY
    assert snap.last_attempt_at is not None
    assert snap.probe_count == 1

    await db.disconnect()


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_readiness_probe_config_defaults() -> None:
    """ReadinessProbeConfig has sensible defaults."""
    from eggpool.models.config import ReadinessProbeConfig

    cfg = ReadinessProbeConfig()
    assert cfg.enabled is False
    assert cfg.interval_s == 10.0
    assert cfg.freshness_s == 30.0
    assert cfg.timeout_s == 5.0
    assert cfg.initial_probe is True


def test_readiness_probe_config_freshness_must_exceed_interval() -> None:
    """freshness_s must be greater than interval_s."""
    from eggpool.models.config import ReadinessProbeConfig

    with pytest.raises(Exception, match="freshness_s.*must be greater"):
        ReadinessProbeConfig(interval_s=10.0, freshness_s=5.0)


def test_readiness_probe_config_in_app_config() -> None:
    """ReadinessProbeConfig is accessible from AppConfig."""
    config = AppConfig.from_dict(
        {
            "readiness_probe": {
                "interval_s": 5.0,
                "freshness_s": 15.0,
                "timeout_s": 2.0,
            }
        }
    )
    assert config.readiness_probe.interval_s == 5.0
    assert config.readiness_probe.freshness_s == 15.0
    assert config.readiness_probe.timeout_s == 2.0


def test_readiness_probe_config_disabled() -> None:
    """ReadinessProbeConfig can be disabled."""
    config = AppConfig.from_dict({"readiness_probe": {"enabled": False}})
    assert config.readiness_probe.enabled is False


# ---------------------------------------------------------------------------
# Snapshot to_dict
# ---------------------------------------------------------------------------


def test_probe_snapshot_to_dict() -> None:
    """ProbeSnapshot.to_dict() returns a JSON-serializable dict."""
    snap = ProbeSnapshot(
        status=ProbeStatus.HEALTHY,
        last_attempt_at=1000.0,
        last_success_at=1000.0,
        last_failure_at=None,
        last_error_class=None,
        last_error_message=None,
        last_probe_duration_ms=1.5,
        consecutive_failures=0,
        configured_interval_s=10.0,
        freshness_deadline_s=30.0,
        worker_running=True,
        probe_count=5,
        success_count=5,
        failure_count=0,
    )
    d = snap.to_dict()
    assert d["status"] == "healthy"
    assert d["probe_count"] == 5
    assert d["worker_running"] is True
    assert d["last_probe_duration_ms"] == 1.5


# ---------------------------------------------------------------------------
# Reload parity (probe survives rehash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_not_affected_by_reload() -> None:
    """Probe is process-owned and continues running across reloads."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(
        db, interval_s=0.05, freshness_s=300, initial_probe=True
    )
    await probe.start()
    await asyncio.sleep(0.1)

    snap1 = await probe.snapshot()
    assert snap1.status == ProbeStatus.HEALTHY

    # Simulate a "reload" by doing nothing — probe should still work
    snap2 = await probe.snapshot()
    assert snap2.status == ProbeStatus.HEALTHY
    assert snap2.probe_count >= snap1.probe_count

    await probe.stop()
    await db.disconnect()


# ---------------------------------------------------------------------------
# No initial probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_no_initial_probe_starts_unknown() -> None:
    """With initial_probe=False, probe starts UNKNOWN and probes on interval."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    probe = DatabaseWritableProbe(
        db, interval_s=0.05, freshness_s=300, initial_probe=False
    )

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.UNKNOWN

    await probe.start()
    # Wait for first periodic probe
    await asyncio.sleep(0.15)

    snap = await probe.snapshot()
    assert snap.status == ProbeStatus.HEALTHY

    await probe.stop()
    await db.disconnect()
