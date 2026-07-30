"""Gate repair tests.

Verifies that ensure_reload_gate_released does not increment
publication_epoch, and that a staged swap cannot be ungated directly.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from eggpool.runtime_manager import (
    PendingGenerationSwap,
    PendingSwapState,
    RuntimeGeneration,
)
from tests.support.reload_harness import ReloadHarness

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_generation(gen_id: int) -> RuntimeGeneration:
    """Build a RuntimeGeneration for tests."""
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
        finalization_retry_queue=MagicMock(),
        routing_trace_guard=MagicMock(),
        routing_trace_writer=MagicMock(),
        created_at_monotonic=now,
        created_at_epoch=now,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_ensure_gate_released_does_not_increment_epoch() -> None:
    """ensure_reload_gate_released does not increment publication_epoch.

    Defensive gate repair must not bump the publication epoch — only
    install_initial() and PendingGenerationSwap.commit() may increment
    the epoch.
    """
    async with ReloadHarness() as harness:
        rm = harness.runtime_manager

        # Record initial epoch.
        initial_epoch = rm._publication_epoch

        # Manually set the gate and call ensure_reload_gate_released.
        async with rm._lease_condition:
            rm._lease_admission_gated = True
            rm._lease_condition.notify_all()

        await rm.ensure_reload_gate_released()

        # Epoch must be unchanged.
        assert rm._publication_epoch == initial_epoch, (
            f"publication_epoch changed from {initial_epoch} "
            f"to {rm._publication_epoch} during defensive repair"
        )

        # Admission must be open.
        assert rm.is_accepting_leases()


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_staged_swap_cannot_be_ungated_directly() -> None:
    """A staged swap prevents direct gate release.

    When a staged swap is present, defensive gate repair must NOT clear
    the gate because the swap must be resolved through rollback or commit.
    """
    async with ReloadHarness() as harness:
        rm = harness.runtime_manager

        # Stage a swap — this opens the gate.
        gen = _build_generation(gen_id=9001)
        swap = PendingGenerationSwap(rm, gen, drain_timeout_s=5.0)

        # Direct construction skips the _pending_swap ownership check,
        # so we must set it manually for ensure_reload_gate_released
        # to see the staged swap.
        rm._pending_swap = swap  # type: ignore[assignment]
        await swap.stage()

        # Verify the gate is active and swap is staged.
        assert rm._lease_admission_gated
        assert swap.swap_state is PendingSwapState.STAGED

        # Call ensure_reload_gate_released — it should NOT clear the gate.
        await rm.ensure_reload_gate_released()

        # Gate must still be active.
        assert rm._lease_admission_gated, (
            "staged swap should prevent defensive gate release"
        )

        # The swap must still be staged — not rolled back.
        assert swap.swap_state is PendingSwapState.STAGED

        # Clean up by rolling back.
        await swap.rollback()


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_ensure_gate_released_noop_when_gate_clear() -> None:
    """ensure_reload_gate_released is a no-op when no gate is active."""
    async with ReloadHarness() as harness:
        rm = harness.runtime_manager

        initial_epoch = rm._publication_epoch

        # No gate is active — should be a no-op.
        assert rm.is_accepting_leases()
        await rm.ensure_reload_gate_released()

        # Epoch unchanged, admission still open.
        assert rm._publication_epoch == initial_epoch
        assert rm.is_accepting_leases()


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_commit_increments_epoch_exactly_once() -> None:
    """PendingGenerationSwap.commit() increments epoch exactly once."""
    async with ReloadHarness() as harness:
        rm = harness.runtime_manager

        initial_epoch = rm._publication_epoch

        gen = _build_generation(gen_id=9002)
        swap = PendingGenerationSwap(rm, gen, drain_timeout_s=5.0)
        await swap.stage()
        await swap.commit()

        assert rm._publication_epoch == initial_epoch + 1, (
            f"expected epoch {initial_epoch + 1}, got {rm._publication_epoch}"
        )

        # Finalize so resources are clean.
        await swap.finalize_retirement()
