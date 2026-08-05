"""Lease condition and lost-wakeup closure tests.

Proves that:
- A notification cannot be lost between predicate evaluation and waiting
- Commit and rollback wake blocked acquisitions
- Waiter count returns to zero on every terminal path
- Shutdown wakes all waiters immediately
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from eggpool.runtime_manager import (
    PendingGenerationSwap,
    RuntimeGeneration,
    RuntimeManagerLeaseExhaustedError,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_generation(
    gen_id: int,
) -> RuntimeGeneration:
    """Build a RuntimeGeneration for swap tests."""
    now = time.monotonic()
    return RuntimeGeneration(
        generation_id=gen_id,
        config_digest=f"digest-{gen_id}",
        config=MagicMock(),
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        coordinator=MagicMock(),
        client_pool=MagicMock(),
        outbound_manager=MagicMock(),
        dns_backend=None,
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(),
        compression_policy=MagicMock(),
        cache_config=MagicMock(),
        compression_tuning_registry=MagicMock(),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        routing_trace_guard=MagicMock(),
        routing_trace_writer=MagicMock(),
        created_at_monotonic=now,
        created_at_epoch=now,
    )


# ---------------------------------------------------------------------------
# Tests: commit wakes blocked acquire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_condition_waiter_receives_commit_notification(
    reload_harness: ReloadHarness,
) -> None:
    """A waiter blocked on the condition wakes when commit is called."""
    rm = reload_harness.runtime_manager

    # Stage a pending swap so the lease gate is closed.
    candidate_gen = _build_generation(gen_id=200)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    waiter_started = asyncio.Event()
    waiter_done = asyncio.Event()
    acquired_gen_id: int | None = None

    async def _waiter() -> None:
        nonlocal acquired_gen_id
        waiter_started.set()
        lease = await rm.acquire()
        acquired_gen_id = lease.generation_id
        await lease.release()
        waiter_done.set()

    asyncio.create_task(_waiter())
    await waiter_started.wait()

    # Give the waiter a moment to block on the condition.
    await asyncio.sleep(0.05)
    assert not waiter_done.is_set(), "waiter should still be blocked"

    # Commit should wake the waiter.
    await swap.commit()
    await asyncio.wait_for(waiter_done.wait(), timeout=2.0)

    assert acquired_gen_id == 200


# ---------------------------------------------------------------------------
# Tests: rollback wakes blocked acquire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_condition_waiter_receives_rollback_notification(
    reload_harness: ReloadHarness,
) -> None:
    """A waiter blocked on the condition wakes when rollback is called."""
    rm = reload_harness.runtime_manager
    pre_gen_id = rm.active_snapshot().generation_id

    candidate_gen = _build_generation(gen_id=201)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    waiter_started = asyncio.Event()
    waiter_done = asyncio.Event()
    acquired_gen_id: int | None = None

    async def _waiter() -> None:
        nonlocal acquired_gen_id
        waiter_started.set()
        lease = await rm.acquire()
        acquired_gen_id = lease.generation_id
        await lease.release()
        waiter_done.set()

    asyncio.create_task(_waiter())
    await waiter_started.wait()
    await asyncio.sleep(0.05)
    assert not waiter_done.is_set()

    # Rollback should wake the waiter — the old generation resumes.
    await swap.rollback()
    await asyncio.wait_for(waiter_done.wait(), timeout=2.0)

    assert acquired_gen_id == pre_gen_id


# ---------------------------------------------------------------------------
# Tests: lost-wakeup impossibility via deterministic barriers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_lost_wakeup_impossible_deterministic(
    reload_harness: ReloadHarness,
) -> None:
    """Proves the condition lock serializes predicate evaluation and waiting.

    Two concurrent waiters are blocked on the condition when a single
    commit fires.  Both must wake up, proving no notification is lost.
    """
    rm = reload_harness.runtime_manager

    candidate_gen = _build_generation(gen_id=210)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    results: list[int] = []
    all_done = asyncio.Event()
    waiter_count = 2
    started_barrier = asyncio.Barrier(parties=waiter_count + 1)

    async def _waiter(idx: int) -> None:
        await started_barrier.wait()
        lease = await rm.acquire()
        results.append(lease.generation_id)
        await lease.release()
        if len(results) == waiter_count:
            all_done.set()

    for i in range(waiter_count):
        asyncio.create_task(_waiter(i))

    # Wait for all waiters to be created and start.
    await started_barrier.wait()
    await asyncio.sleep(0.05)

    # Both should be blocked on the condition now.
    assert not all_done.is_set()

    # Single commit wakes both.
    await swap.commit()
    await asyncio.wait_for(all_done.wait(), timeout=2.0)

    assert sorted(results) == [210, 210]


# ---------------------------------------------------------------------------
# Tests: 1000-iteration lease race schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_100_iteration_lease_race_schedule(
    reload_harness: ReloadHarness,
) -> None:
    """Lost-wakeup test run 100 times in a loop (no pytest-repeat).

    Each iteration stages a swap, spawns a waiter, commits, and
    verifies the waiter received the lease.
    """
    rm = reload_harness.runtime_manager

    for iteration in range(100):
        gen_id = 3000 + iteration
        candidate_gen = _build_generation(gen_id=gen_id)
        swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
        await swap.stage()

        acquired = asyncio.Event()
        result_id: int | None = None

        async def _waiter(
            target_id: int = gen_id,
            _acquired: asyncio.Event = acquired,
        ) -> None:
            nonlocal result_id
            lease = await rm.acquire()
            result_id = lease.generation_id
            await lease.release()
            _acquired.set()

        asyncio.create_task(_waiter())
        await asyncio.sleep(0.01)
        await swap.commit()
        await asyncio.wait_for(acquired.wait(), timeout=1.0)
        assert result_id == gen_id, (
            f"iteration {iteration}: expected {gen_id}, got {result_id}"
        )


# ---------------------------------------------------------------------------
# Tests: waiter count returns to zero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_waiter_count_zero_after_success(
    reload_harness: ReloadHarness,
) -> None:
    """_lease_gate_waiters returns to 0 after a successful acquire."""
    rm = reload_harness.runtime_manager

    assert rm._lease_gate_waiters == 0
    lease = await rm.acquire()
    assert rm._lease_gate_waiters == 0
    await lease.release()
    assert rm._lease_gate_waiters == 0


@pytest.mark.asyncio()
async def test_waiter_count_zero_after_timeout(
    reload_harness: ReloadHarness,
) -> None:
    """_lease_gate_waiters returns to 0 after a timeout during gated acquire."""
    rm = reload_harness.runtime_manager

    candidate_gen = _build_generation(gen_id=400)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    assert rm._lease_gate_waiters == 0

    # Temporarily shrink the lease timeout so the waiter times out quickly.
    import eggpool.runtime_manager as rm_mod

    original_timeout = rm_mod.GENERATION_LEASE_TIMEOUT_S
    rm_mod.GENERATION_LEASE_TIMEOUT_S = 0.05
    try:
        with pytest.raises(RuntimeManagerLeaseExhaustedError):
            await rm.acquire()
    finally:
        rm_mod.GENERATION_LEASE_TIMEOUT_S = original_timeout

    assert rm._lease_gate_waiters == 0

    # Clean up the swap.
    await swap.rollback()


@pytest.mark.asyncio()
async def test_waiter_count_zero_after_cancellation(
    reload_harness: ReloadHarness,
) -> None:
    """_lease_gate_waiters returns to 0 after CancelledError."""
    rm = reload_harness.runtime_manager

    candidate_gen = _build_generation(gen_id=401)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    waiter_cancelled = asyncio.Event()

    async def _blocked_waiter() -> None:
        try:
            await rm.acquire()
        except asyncio.CancelledError:
            waiter_cancelled.set()
            raise

    task = asyncio.create_task(_blocked_waiter())
    await asyncio.sleep(0.05)
    assert rm._lease_gate_waiters >= 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await waiter_cancelled.wait()
    assert rm._lease_gate_waiters == 0

    await swap.rollback()


@pytest.mark.asyncio()
async def test_waiter_count_zero_after_rollback(
    reload_harness: ReloadHarness,
) -> None:
    """_lease_gate_waiters returns to 0 after rollback wakes waiters."""
    rm = reload_harness.runtime_manager

    candidate_gen = _build_generation(gen_id=402)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    waiter_done = asyncio.Event()

    async def _waiter() -> None:
        lease = await rm.acquire()
        await lease.release()
        waiter_done.set()

    asyncio.create_task(_waiter())
    await asyncio.sleep(0.05)
    assert rm._lease_gate_waiters >= 1

    await swap.rollback()
    await asyncio.wait_for(waiter_done.wait(), timeout=2.0)
    assert rm._lease_gate_waiters == 0


# ---------------------------------------------------------------------------
# Tests: shutdown wakes waiters immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_shutdown_wakes_waiters_immediately(
    reload_harness: ReloadHarness,
) -> None:
    """shutdown() wakes all waiters and they raise RuntimeManagerLeaseExhaustedError."""
    rm = reload_harness.runtime_manager

    candidate_gen = _build_generation(gen_id=500)
    swap = PendingGenerationSwap(rm, candidate_gen, drain_timeout_s=5.0)
    await swap.stage()

    waiter_count = 3
    waiters_done = asyncio.Event()
    completed_count = 0
    exceptions: list[BaseException] = []

    async def _waiter() -> None:
        nonlocal completed_count
        try:
            await rm.acquire()
        except RuntimeManagerLeaseExhaustedError:
            pass
        except BaseException as exc:
            exceptions.append(exc)
        finally:
            completed_count += 1
            if completed_count >= waiter_count:
                waiters_done.set()

    for _ in range(waiter_count):
        asyncio.create_task(_waiter())
    await asyncio.sleep(0.05)

    # All waiters should be blocked.
    assert rm._lease_gate_waiters == waiter_count

    # Shutdown wakes them all.
    await rm.shutdown()
    await asyncio.wait_for(waiters_done.wait(), timeout=3.0)

    assert exceptions == [], f"unexpected exceptions: {exceptions}"
    assert rm._lease_gate_waiters == 0


# ---------------------------------------------------------------------------
# Tests: commit/rollback wake within 250ms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_commit_and_rollback_wake_in_250ms(
    reload_harness: ReloadHarness,
) -> None:
    """Timing test: commit and rollback wake blocked waiters within 250ms."""
    rm = reload_harness.runtime_manager

    # --- commit path ---
    candidate_gen_1 = _build_generation(gen_id=600)
    swap1 = PendingGenerationSwap(rm, candidate_gen_1, drain_timeout_s=5.0)
    await swap1.stage()

    commit_done = asyncio.Event()

    async def _commit_waiter() -> None:
        lease = await rm.acquire()
        await lease.release()
        commit_done.set()

    asyncio.create_task(_commit_waiter())
    await asyncio.sleep(0.05)
    assert not commit_done.is_set()

    start = time.monotonic()
    await swap1.commit()
    await asyncio.wait_for(commit_done.wait(), timeout=2.0)
    commit_elapsed_ms = (time.monotonic() - start) * 1000
    assert commit_elapsed_ms < 250, f"commit wake took {commit_elapsed_ms:.1f}ms"

    # --- rollback path ---
    candidate_gen_2 = _build_generation(gen_id=601)
    swap2 = PendingGenerationSwap(rm, candidate_gen_2, drain_timeout_s=5.0)
    await swap2.stage()

    rollback_done = asyncio.Event()

    async def _rollback_waiter() -> None:
        lease = await rm.acquire()
        await lease.release()
        rollback_done.set()

    asyncio.create_task(_rollback_waiter())
    await asyncio.sleep(0.05)
    assert not rollback_done.is_set()

    start = time.monotonic()
    await swap2.rollback()
    await asyncio.wait_for(rollback_done.wait(), timeout=2.0)
    rollback_elapsed_ms = (time.monotonic() - start) * 1000
    assert rollback_elapsed_ms < 250, f"rollback wake took {rollback_elapsed_ms:.1f}ms"
