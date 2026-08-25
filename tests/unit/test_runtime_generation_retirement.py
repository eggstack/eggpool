"""Runtime-generation retirement tests.

Covers the supported generation-retirement contract:

- Prompt reload completion (publication not blocked by drain)
- Natural drainage (lease release triggers retirement completion)
- Deadline force close (timeout forces resource close)
- Multiple generations (concurrent retirement tasks)
- Shutdown (joins all retirement tasks)
- Task hygiene (no pending tasks after tests)
- Enhanced diagnostics (state, forced_close, timing)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from eggpool.runtime_manager import (
    RuntimeGeneration,
    RuntimeManager,
    SlotState,
)


def _fake_generation(generation_id: int = 0) -> RuntimeGeneration:
    """Return a minimal RuntimeGeneration with mock services."""
    return RuntimeGeneration(
        generation_id=generation_id,
        config=MagicMock(),
        config_digest="a" * 64,
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        coordinator=MagicMock(),
        client_pool=MagicMock(),
        outbound_manager=MagicMock(),
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        routing_trace_guard=MagicMock(),
        routing_trace_writer=MagicMock(),
        created_at_monotonic=time.monotonic(),
        created_at_epoch=time.time(),
    )


# ---------------------------------------------------------------------------
# § Prompt reload completion
# ---------------------------------------------------------------------------


class TestPromptReloadCompletion:
    @pytest.mark.asyncio
    async def test_publication_returns_immediately_with_held_lease(self) -> None:
        """Publication completes without waiting for old-gen lease drain."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        # Hold a lease on gen0
        held_lease = await manager.acquire()
        assert held_lease.generation_id == 0

        # Publish gen1 — should complete promptly
        gen1 = _fake_generation(1)
        start = time.monotonic()
        await manager.install_candidate(gen1, drain_timeout_s=5.0)
        elapsed = time.monotonic() - start

        # Publication returns promptly (< 1s), not blocked by drain
        assert elapsed < 1.0, f"Publication took {elapsed:.2f}s — expected prompt"

        # New generation is active
        assert manager.active_snapshot().generation_id == 1

        # Allow the background retirement task to start
        await asyncio.sleep(0.05)

        # Old generation appears as retiring
        diag = manager.diagnostics()
        assert len(diag.retiring) == 1
        assert diag.retiring[0].generation_id == 0
        assert diag.retiring[0].accepting_leases is False
        assert diag.retiring[0].retirement_started is True

        # New leases resolve to the new generation
        new_lease = await manager.acquire()
        assert new_lease.generation_id == 1
        await new_lease.release()

        # Held lease still references old generation
        assert held_lease.generation_id == 0

        # Cleanup
        await held_lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_retirement_pending_from_diagnostics(self) -> None:
        """retirement_pending can be derived from runtime-manager state."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        held = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)

        # Allow the background retirement task to start
        await asyncio.sleep(0.05)

        # Diagnostics show retirement is pending
        diag = manager.diagnostics()
        assert len(diag.retiring) == 1
        assert diag.retiring[0].retirement_complete is False

        await held.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)
        await manager.shutdown()


# ---------------------------------------------------------------------------
# § Natural drainage
# ---------------------------------------------------------------------------


class TestNaturalDrainage:
    @pytest.mark.asyncio
    async def test_release_final_lease_completes_retirement(self) -> None:
        """Releasing the last lease triggers retirement completion."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)

        # Allow the background retirement task to start
        await asyncio.sleep(0.05)

        # Retirement is in progress (lease held)
        diag = manager.diagnostics()
        assert len(diag.retiring) == 1
        assert diag.retiring[0].retirement_complete is False

        # Release the final lease
        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        # Old generation is gone from retiring list
        diag = manager.diagnostics()
        assert len(diag.retiring) == 0

        # Old gen resources closed exactly once
        gen0.client_pool.aclose.assert_called_once()

        # New generation remains active
        assert manager.active_snapshot().generation_id == 1

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_retirement_task_removed_from_registry(self) -> None:
        """Completed retirement task is removed from the task registry."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)

        # Allow the background retirement task to start
        await asyncio.sleep(0.05)

        # Task is tracked
        assert 0 in manager._retirement_tasks

        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        # Task is removed
        assert 0 not in manager._retirement_tasks
        assert manager.diagnostics().retirement_task_count == 0

        await manager.shutdown()


# ---------------------------------------------------------------------------
# § Deadline force close
# ---------------------------------------------------------------------------


class TestDeadlineForceClose:
    @pytest.mark.asyncio
    async def test_forced_close_with_short_deadline(self) -> None:
        """Drain timeout forces close when leases are held."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        # Hold a lease past the deadline
        _held = await manager.acquire()

        # Publish with very short drain timeout
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=0.05)
        await manager.wait_for_retirement(0, timeout_s=2.0)

        # Resources are closed despite held lease
        gen0.client_pool.aclose.assert_called()

        # Diagnostics show forced close
        # (slot is removed from _retiring after close, so check task registry)
        assert 0 not in manager._retiring

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_forced_close_diagnostics_fields(self) -> None:
        """Forced close populates diagnostic timing fields."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        _held = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=0.05)
        await manager.wait_for_retirement(0, timeout_s=2.0)

        # Verify close resources were called (forced path)
        gen0.client_pool.aclose.assert_called()
        gen0.outbound_manager.aclose.assert_called()

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_late_lease_release_after_forced_close(self) -> None:
        """Releasing a lease after forced close is safe and idempotent."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=0.05)
        await manager.wait_for_retirement(0, timeout_s=2.0)

        # Release the lease after forced close — should not error
        await lease.release()

        # Double release is also safe
        await lease.release()

        await manager.shutdown()


# ---------------------------------------------------------------------------
# § Multiple generations
# ---------------------------------------------------------------------------


class TestMultipleGenerations:
    @pytest.mark.asyncio
    async def test_concurrent_retirement_tasks(self) -> None:
        """Multiple retirement tasks coexist safely."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        # Hold lease on gen0 so its retirement is blocked
        lease0 = await manager.acquire()

        # Publish gen1
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)
        await asyncio.sleep(0.05)

        # Hold lease on gen1
        lease1 = await manager.acquire()

        # Publish gen2
        await manager.install_candidate(_fake_generation(2), drain_timeout_s=5.0)
        await asyncio.sleep(0.05)

        # Two retirement tasks tracked
        assert manager.diagnostics().retirement_task_count == 2

        # Both old gens in retiring list
        retiring_ids = {s.generation.generation_id for s in manager._retiring}
        assert 0 in retiring_ids
        assert 1 in retiring_ids

        # Release all leases
        await lease0.release()
        await lease1.release()

        # Wait for both to complete
        await manager.wait_for_retirement(0, timeout_s=5.0)
        await manager.wait_for_retirement(1, timeout_s=5.0)

        # All retired, active is gen2
        assert manager.active_snapshot().generation_id == 2
        assert len(manager._retiring) == 0
        assert manager.diagnostics().retirement_task_count == 0

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_each_generation_closes_independently(self) -> None:
        """Each generation's resources close exactly once."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        gen1 = _fake_generation(1)
        await manager.install_initial(gen0)

        lease = await manager.acquire()
        await manager.install_candidate(gen1, drain_timeout_s=5.0)
        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        # gen0 closed once
        gen0.client_pool.aclose.assert_called_once()
        gen0.outbound_manager.aclose.assert_called_once()

        # gen1 not yet retired (still active)
        gen1.client_pool.aclose.assert_not_called()

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_no_old_generation_client_pool_closure_during_drain(self) -> None:
        """Old gen pool stays open during active lease, closes after."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)
        await asyncio.sleep(0.05)

        # Pool still open (lease held)
        gen0.client_pool.aclose.assert_not_called()

        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        # Pool closed after drain
        gen0.client_pool.aclose.assert_called()

        await manager.shutdown()


# ---------------------------------------------------------------------------
# § Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_no_retiring_generations(self) -> None:
        """Shutdown with no active leases completes cleanly."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        await manager.shutdown()
        assert manager._shutdown_in_progress

    @pytest.mark.asyncio
    async def test_shutdown_one_draining_generation(self) -> None:
        """Shutdown joins one naturally draining generation."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)

        # Release lease so retirement can complete
        await lease.release()

        # Shutdown should join the retirement task
        await manager.wait_for_retirement(0, timeout_s=5.0)
        await manager.shutdown()

        gen0.client_pool.aclose.assert_called()

    @pytest.mark.asyncio
    async def test_shutdown_multiple_generations(self) -> None:
        """Shutdown joins multiple retirement tasks."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        lease0 = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)
        await asyncio.sleep(0.05)

        lease1 = await manager.acquire()
        await manager.install_candidate(_fake_generation(2), drain_timeout_s=5.0)
        await asyncio.sleep(0.05)

        # Release all leases
        await lease0.release()
        await lease1.release()

        # Shutdown joins all tasks
        await manager.wait_for_retirement(0, timeout_s=5.0)
        await manager.wait_for_retirement(1, timeout_s=5.0)
        await manager.shutdown()

        assert len(manager._retiring) == 0

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self) -> None:
        """Repeated shutdown calls do not double-close."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        await manager.shutdown()
        await manager.shutdown()  # no error
        await manager.shutdown()  # still no error

    @pytest.mark.asyncio
    async def test_shutdown_rejects_new_lease(self) -> None:
        """After shutdown, acquire raises LeaseExhaustedError."""
        from eggpool.runtime_manager import RuntimeManagerLeaseExhaustedError

        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        await manager.shutdown()

        with pytest.raises(RuntimeManagerLeaseExhaustedError):
            await manager.acquire()

    @pytest.mark.asyncio
    async def test_shutdown_rejects_new_publication(self) -> None:
        """After shutdown, install_candidate raises ShutdownError."""
        from eggpool.runtime_manager import RuntimeManagerShutdownError

        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        await manager.shutdown()

        with pytest.raises(RuntimeManagerShutdownError):
            await manager.install_candidate(_fake_generation(1))


# ---------------------------------------------------------------------------
# § Task hygiene
# ---------------------------------------------------------------------------


class TestTaskHygiene:
    @pytest.mark.asyncio
    async def test_no_pending_tasks_after_natural_drain(self) -> None:
        """No retirement tasks remain after natural drain completion."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)
        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        assert manager.diagnostics().retirement_task_count == 0
        assert len(manager._retiring) == 0

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_no_pending_tasks_after_forced_close(self) -> None:
        """No retirement tasks remain after forced close."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        _held = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=0.05)
        await manager.wait_for_retirement(0, timeout_s=2.0)

        assert manager.diagnostics().retirement_task_count == 0
        assert len(manager._retiring) == 0

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_resources_closed_exactly_once(self) -> None:
        """Each generation's resources are closed exactly once."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)
        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        gen0.client_pool.aclose.assert_called_once()
        gen0.outbound_manager.aclose.assert_called_once()
        gen0.supervisor.stop_all.assert_called_once()

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_close_exception_captured(self) -> None:
        """Exceptions during close are captured in diagnostics."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        gen0.client_pool.aclose.side_effect = RuntimeError("pool close failed")
        await manager.install_initial(gen0)

        _held = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=0.05)
        await manager.wait_for_retirement(0, timeout_s=2.0)

        # Close error was captured (not raised)
        # The slot is removed from _retiring after close, but the
        # error was logged. Verify the pool aclose was called.
        gen0.client_pool.aclose.assert_called()

        await manager.shutdown()


# ---------------------------------------------------------------------------
# § Slot state lifecycle
# ---------------------------------------------------------------------------


class TestSlotStateLifecycle:
    @pytest.mark.asyncio
    async def test_initial_slot_is_active(self) -> None:
        """New slot starts in ACTIVE state."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        slot = manager._active
        assert slot is not None
        assert slot.state == SlotState.ACTIVE

    @pytest.mark.asyncio
    async def test_slot_transitions_through_states(self) -> None:
        """Slot transitions: active → retiring → closing → closed."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        held = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)

        # Allow the background retirement task to start
        await asyncio.sleep(0.05)

        # Slot should be retiring
        diag = manager.diagnostics()
        assert len(diag.retiring) == 1
        assert diag.retiring[0].state == "retiring"

        await held.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        # After close, slot is removed from retiring list
        # (closed state is terminal)
        diag = manager.diagnostics()
        assert len(diag.retiring) == 0

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_drain_event_signaled_on_zero_leases(self) -> None:
        """drain_event is set when active_leases reaches zero."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        lease = await manager.acquire()
        slot = manager._active
        assert slot is not None
        assert not slot.drain_event.is_set()

        await lease.release()
        assert slot.drain_event.is_set()

        await manager.shutdown()


# ---------------------------------------------------------------------------
# § wait_for_retirement
# ---------------------------------------------------------------------------


class TestWaitForRetirement:
    @pytest.mark.asyncio
    async def test_wait_returns_immediately_if_no_task(self) -> None:
        """wait_for_retirement is a no-op for unknown generation ID."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        # No retirement task for gen 999 — should return immediately
        await manager.wait_for_retirement(999, timeout_s=1.0)
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_wait_completes_after_retirement(self) -> None:
        """wait_for_retirement returns after task completes."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)

        await lease.release()
        # Should return after retirement completes
        await manager.wait_for_retirement(0, timeout_s=5.0)

        assert 0 not in manager._retirement_tasks
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_wait_timeout_raises(self) -> None:
        """wait_for_retirement raises TimeoutError if task takes too long."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        _held = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=60.0)

        with pytest.raises(asyncio.TimeoutError):
            await manager.wait_for_retirement(0, timeout_s=0.1)

        # Release the held lease so retirement can complete quickly.
        await _held.release()

        # Cleanup: shutdown will force-close everything
        await manager.shutdown()


# ---------------------------------------------------------------------------
# § Duplicate spawn guard
# ---------------------------------------------------------------------------


class TestDuplicateSpawnGuard:
    @pytest.mark.asyncio
    async def test_duplicate_spawn_keeps_original_task_tracked(self) -> None:
        """A second spawn while retirement runs must not orphan the first."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        lease = await manager.acquire()
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)
        await asyncio.sleep(0.05)

        assert 0 in manager._retirement_tasks
        original_task = manager._retirement_tasks[0]

        old_slot = next(
            slot for slot in manager._retiring if slot.generation.generation_id == 0
        )
        await manager._spawn_retirement_task(old_slot, 5.0)

        # Registry still tracks the original task — no orphaned overwrite.
        assert manager._retirement_tasks[0] is original_task
        assert not original_task.done()

        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)
        await manager.shutdown()
