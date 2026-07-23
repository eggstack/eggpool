"""Candidate visibility tests during staged swap (Plan 015 Milestone D2).

Verifies that while the lease gate is active during a staged swap, new
requests cannot acquire the candidate generation, and that after rollback
requests resume on the old generation.
"""

from __future__ import annotations

import asyncio

import pytest

from eggpool.runtime_manager import RuntimeManagerLeaseExhaustedError
from tests.support.reload_harness import ReloadHarness
from tests.support.runtime_snapshot import RuntimeSnapshot


@pytest.mark.asyncio()
async def test_acquire_blocked_during_staged_swap() -> None:
    """acquire() blocks while the lease gate is active during a staged swap."""
    async with ReloadHarness() as h:
        pre_snapshot = RuntimeSnapshot.capture(h.runtime_manager)

        from eggpool.runtime_manager import PendingGenerationSwap

        candidate_gen = h.runtime_manager.active_snapshot()
        swap = PendingGenerationSwap(
            h.runtime_manager,
            candidate_gen,
            drain_timeout_s=1.0,
        )
        await swap.stage()

        assert h.runtime_manager._lease_gate_event is not None

        acquire_error: Exception | None = None
        lease_result = None

        async def try_acquire() -> None:
            nonlocal acquire_error, lease_result
            try:
                lease_result = await asyncio.wait_for(
                    h.runtime_manager.acquire(), timeout=0.2
                )
            except (RuntimeManagerLeaseExhaustedError, TimeoutError) as exc:
                acquire_error = exc

        await try_acquire()

        assert acquire_error is not None, (
            "acquire() should have been blocked by the lease gate"
        )

        await swap.rollback()

        post_snapshot = RuntimeSnapshot.capture(h.runtime_manager)
        gen_diffs = post_snapshot.assert_same_generation(pre_snapshot)
        assert gen_diffs == [], f"Generation changed after rollback: {gen_diffs}"


@pytest.mark.asyncio()
async def test_acquire_resumes_after_rollback() -> None:
    """After rollback, acquire() succeeds on the old generation."""
    async with ReloadHarness() as h:
        old_gen_id = h.runtime_manager.active_snapshot().generation_id

        from eggpool.runtime_manager import PendingGenerationSwap

        candidate_gen = h.runtime_manager.active_snapshot()
        swap = PendingGenerationSwap(
            h.runtime_manager,
            candidate_gen,
            drain_timeout_s=1.0,
        )
        await swap.stage()

        await swap.rollback()

        lease = await h.runtime_manager.acquire()
        try:
            assert lease.generation_id == old_gen_id
        finally:
            await lease.release()


@pytest.mark.asyncio()
async def test_lease_gate_cleared_on_rollback() -> None:
    """Rollback clears the lease gate event on the runtime manager."""
    async with ReloadHarness() as h:
        from eggpool.runtime_manager import PendingGenerationSwap

        candidate_gen = h.runtime_manager.active_snapshot()
        swap = PendingGenerationSwap(
            h.runtime_manager,
            candidate_gen,
            drain_timeout_s=1.0,
        )
        await swap.stage()

        assert h.runtime_manager._lease_gate_event is not None

        await swap.rollback()

        assert h.runtime_manager._lease_gate_event is None


@pytest.mark.asyncio()
async def test_lease_gate_cleared_on_commit() -> None:
    """Commit clears the lease gate event on the runtime manager."""
    async with ReloadHarness() as h:
        from eggpool.runtime_manager import PendingGenerationSwap

        candidate_gen = h.runtime_manager.active_snapshot()
        swap = PendingGenerationSwap(
            h.runtime_manager,
            candidate_gen,
            drain_timeout_s=1.0,
        )
        await swap.stage()

        assert h.runtime_manager._lease_gate_event is not None

        await swap.commit()

        assert h.runtime_manager._lease_gate_event is None


@pytest.mark.asyncio()
async def test_old_generation_serves_requests_after_failed_swap() -> None:
    """After a staged swap is rolled back, acquire() returns the old generation."""
    async with ReloadHarness() as h:
        pre_snapshot = RuntimeSnapshot.capture(h.runtime_manager)

        from eggpool.runtime_manager import PendingGenerationSwap

        candidate_gen = h.runtime_manager.active_snapshot()
        swap = PendingGenerationSwap(
            h.runtime_manager,
            candidate_gen,
            drain_timeout_s=1.0,
        )
        await swap.stage()

        await swap.rollback()

        lease = await h.runtime_manager.acquire()
        try:
            assert lease.generation_id == pre_snapshot.active_generation_id
        finally:
            await lease.release()
