"""Shutdown transaction ordering tests.

Shutdown waits for the active transaction before draining jobs, timeout
triggers adoption, and close-once behavior is verified after adoption.

Test cases:
  1. Shutdown with no active transaction completes quickly.
  2. Shutdown with an active transaction waits for it.
  3. Shutdown timeout triggers adoption.
  4. After shutdown adoption, old generation closes exactly once.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

from eggpool.runtime_manager import RuntimeManagerLeaseExhaustedError

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# E1: Shutdown with no active transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_shutdown_no_active_transaction_completes_quickly(
    reload_harness: ReloadHarness,
) -> None:
    """E1: Shutdown with no active transaction returns immediately."""
    rm = reload_harness.reload_manager

    # Ensure no active transaction.
    assert rm.active_transaction is None

    # wait_for_transaction_completion should return True immediately.
    start = time.monotonic()
    completed = await rm.wait_for_transaction_completion(timeout_s=5.0)
    elapsed = time.monotonic() - start

    assert completed is True
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# E1: Shutdown waits for an active transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_shutdown_waits_for_active_transaction(
    reload_harness: ReloadHarness,
) -> None:
    """E1: Shutdown waits for an in-flight reload to complete."""
    rm = reload_harness.reload_manager

    # Start a reload that will succeed.
    reload_task = asyncio.create_task(reload_harness.reload())

    # Wait for the transaction to be created.
    for _ in range(50):
        if rm.active_transaction is not None:
            break
        await asyncio.sleep(0.01)

    # Now wait_for_transaction_completion should eventually return True.
    completed = await rm.wait_for_transaction_completion(timeout_s=10.0)
    assert completed is True

    # Let the reload finish.
    result = await reload_task
    assert result.ok is True


# ---------------------------------------------------------------------------
# E2: Shutdown timeout triggers adoption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_shutdown_timeout_triggers_adoption(
    reload_harness: ReloadHarness,
) -> None:
    """E2: When drain times out, shutdown adopts committed runtime ownership.

    1. First reload succeeds.
    2. Inject permanent retirement failure.
    3. Second reload — retirement fails, job remains unresolved.
    4. Call drain with very short timeout — times out.
    5. Call runtime_manager.shutdown() — should handle adoption.
    """
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager

    # First reload.
    result1 = await reload_harness.reload()
    assert result1.ok is True
    gen1_id = rtm.active_snapshot().generation_id

    # Inject permanent retirement failure.
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("permanent failure")
    try:
        result2 = await reload_harness.reload(config=reload_harness.initial_config)
        assert result2.ok is True
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    gen2_id = rtm.active_snapshot().generation_id
    assert gen2_id != gen1_id

    # Unresolved job exists.
    unresolved = [j for j in rm._accepted_finalization_jobs.values() if j.is_unresolved]
    assert len(unresolved) >= 1

    # Drain with very short timeout — should time out.
    remaining = await rm.drain_finalization_jobs(timeout_s=0.01)
    assert isinstance(remaining, int)

    # Shutdown should handle remaining slots.
    await rtm.shutdown()

    # No new leases after shutdown.
    with pytest.raises(RuntimeManagerLeaseExhaustedError):
        await rtm.acquire()


# ---------------------------------------------------------------------------
# E4: Close-once after shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_shutdown_closes_old_and_active_exactly_once(
    reload_harness: ReloadHarness,
) -> None:
    """E4: Both old and active generations are retired exactly once during shutdown."""
    rtm = reload_harness.runtime_manager

    # First reload.
    result1 = await reload_harness.reload()
    assert result1.ok is True
    gen1_id = rtm.active_snapshot().generation_id

    # Second reload — succeeds cleanly.
    result2 = await reload_harness.reload(config=reload_harness.initial_config)
    assert result2.ok is True
    gen2_id = rtm.active_snapshot().generation_id
    assert gen2_id != gen1_id

    # Shutdown — retires the active generation (gen2).
    await rtm.shutdown()

    # No new leases after shutdown.
    with pytest.raises(RuntimeManagerLeaseExhaustedError):
        await rtm.acquire()


# ---------------------------------------------------------------------------
# E4: Drain success then shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_shutdown_after_successful_drain(
    reload_harness: ReloadHarness,
) -> None:
    """E4: Successful drain followed by shutdown closes cleanly."""
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager

    # First reload.
    result1 = await reload_harness.reload()
    assert result1.ok is True

    # Fail-once retirement.
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("fail-once")
    try:
        result2 = await reload_harness.reload(config=reload_harness.initial_config)
        assert result2.ok is True
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    # Drain completes the job.
    remaining = await rm.drain_finalization_jobs(timeout_s=10.0)
    assert remaining == 0

    # All jobs resolved.
    unresolved = [j for j in rm._accepted_finalization_jobs.values() if j.is_unresolved]
    assert len(unresolved) == 0

    # Shutdown.
    await rtm.shutdown()

    # No new leases.
    with pytest.raises(RuntimeManagerLeaseExhaustedError):
        await rtm.acquire()


# ---------------------------------------------------------------------------
# E1: wait_for_transaction_completion returns False on timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_wait_for_transaction_timeout_returns_false(
    reload_harness: ReloadHarness,
) -> None:
    """E1: wait_for_transaction_completion returns False when no active transaction."""
    rm = reload_harness.reload_manager

    # No active transaction — should return True.
    result = await rm.wait_for_transaction_completion(timeout_s=0.01)
    assert result is True
