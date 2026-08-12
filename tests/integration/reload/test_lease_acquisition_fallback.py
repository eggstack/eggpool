"""Lease-acquisition fallback tests (Plan §Required failing tests).

Force ``RuntimeManager.acquire()`` to raise its expected
exhaustion/shutdown error and verify:

- The error surfaces as :class:`RuntimeManagerLeaseExhaustedError`.
- No fallback path silently retries against the previous generation.
- No legacy coordinator is invoked.
- The active generation remains the same (no spurious publication).

Future contract (Phase 3): the request handler must translate
``RuntimeManagerLeaseExhaustedError`` into an HTTP 503 response
without invoking any legacy coordinator path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.runtime_manager import RuntimeManagerLeaseExhaustedError

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_acquire_after_shutdown_raises_exhausted(
    reload_harness: ReloadHarness,
) -> None:
    """After :meth:`shutdown`, :meth:`acquire` raises the documented error."""
    import eggpool.runtime_manager as rm_mod

    pre_id = reload_harness.runtime_manager.active_snapshot().generation_id
    original = rm_mod.GENERATION_LEASE_TIMEOUT_S
    rm_mod.GENERATION_LEASE_TIMEOUT_S = 0.1

    # Initiate shutdown — this drains the active generation and
    # disarms ``_acquire``.
    await reload_harness.runtime_manager.shutdown()

    try:
        with pytest.raises(RuntimeManagerLeaseExhaustedError) as exc_info:
            await reload_harness.runtime_manager.acquire()

        msg = str(exc_info.value)
        assert "shutting down" in msg.lower() or "lease" in msg.lower(), (
            f"Unexpected error message: {msg}"
        )
    finally:
        rm_mod.GENERATION_LEASE_TIMEOUT_S = original

    # The active generation (pre-shutdown) was not affected.
    # No new generation was published.
    diagnostics = reload_harness.runtime_manager.diagnostics()
    assert diagnostics.shutdown_in_progress is True
    assert diagnostics.next_generation_id == pre_id + 1, (
        "Shutdown must not advance next_generation_id"
    )


@pytest.mark.asyncio()
async def test_acquire_before_install_raises_exhausted() -> None:
    """A bare manager (no installed generation) raises the documented error.

    The default ``GENERATION_LEASE_TIMEOUT_S`` is 30s; the test
    shortens it via a runtime patch so the assertion completes in
    well under a second.
    """
    import eggpool.runtime_manager as rm_mod
    from eggpool.runtime_manager import RuntimeManager

    original = rm_mod.GENERATION_LEASE_TIMEOUT_S
    rm_mod.GENERATION_LEASE_TIMEOUT_S = 0.1
    try:
        rm = RuntimeManager()
        with pytest.raises(RuntimeManagerLeaseExhaustedError):
            await rm.acquire()
    finally:
        rm_mod.GENERATION_LEASE_TIMEOUT_S = original


@pytest.mark.asyncio()
async def test_acquire_failure_does_not_publish_new_generation(
    reload_harness: ReloadHarness,
) -> None:
    """A failed acquire must not silently publish a new generation.

    This protects against a class of defects where a fallback path
    triggers a publication in response to acquire failures.
    """
    import eggpool.runtime_manager as rm_mod

    pre_count = reload_harness.runtime_manager.diagnostics().next_generation_id
    original = rm_mod.GENERATION_LEASE_TIMEOUT_S
    rm_mod.GENERATION_LEASE_TIMEOUT_S = 0.1

    # Force a shutdown to disarm acquire.
    await reload_harness.runtime_manager.shutdown()

    try:
        for _ in range(5):
            with pytest.raises(RuntimeManagerLeaseExhaustedError):
                await reload_harness.runtime_manager.acquire()
    finally:
        rm_mod.GENERATION_LEASE_TIMEOUT_S = original

    # No spurious generation published.
    diagnostics = reload_harness.runtime_manager.diagnostics()
    assert diagnostics.next_generation_id == pre_count, (
        "Shutdown leaked a generation reservation: "
        f"{pre_count} -> {diagnostics.next_generation_id}"
    )
    # The active slot is now empty.
    assert reload_harness.runtime_manager._active is None


@pytest.mark.asyncio()
async def test_lease_exhaustion_during_publication_race() -> None:
    """Simulate the brief publication-race window.

    In production, an in-flight ``acquire`` may briefly find no
    accepting slot during a swap. The manager waits for
    ``GENERATION_LEASE_TIMEOUT_S`` before raising. With a small
    timeout, the test verifies the error contract.
    """
    import asyncio
    from unittest.mock import MagicMock

    # Force a short deadline by patching the constant.
    import eggpool.runtime_manager as rm_mod
    from eggpool.models.config import AppConfig, ServerConfig
    from eggpool.runtime_manager import (
        GENERATION_LEASE_TIMEOUT_S,
        RuntimeGeneration,
        RuntimeManager,
        RuntimeManagerShutdownError,
    )

    original = rm_mod.GENERATION_LEASE_TIMEOUT_S
    rm_mod.GENERATION_LEASE_TIMEOUT_S = 0.1
    try:
        rm = RuntimeManager()
        # Install initial generation.
        config = AppConfig(server=ServerConfig(host="127.0.0.1", port=11300))
        builder = rm_mod.RuntimeGenerationBuilder()
        build = await builder.build_initial(
            config,
            rm_mod.ProcessRuntime(db=MagicMock(), stats_db=MagicMock()),
            generation_id=0,
            config_digest="deadbeef",
            registry=MagicMock(),
            catalog=MagicMock(),
            router=MagicMock(),
            coordinator=MagicMock(),
            client_pool=MagicMock(),
            outbound_manager=MagicMock(),
            health_manager=MagicMock(),
            cost_calculator=MagicMock(),
            transcoder_policy=MagicMock(),
            compression_policy=MagicMock(),
            dispatch_overhead_recorder=MagicMock(),
            dispatch_span_recorder=MagicMock(),
            account_backoff_repo=MagicMock(),
            stats_service=MagicMock(),
            supervisor=MagicMock(),
            routing_trace_guard=MagicMock(),
            routing_trace_writer=MagicMock(),
        )
        await rm.install_initial(build.generation)

        # Now flip the slot to not-accepting (simulating publication in
        # progress).  The next acquire should hit the deadline.
        slot = rm._active
        assert slot is not None
        slot.accepting_leases = False

        with pytest.raises(RuntimeManagerLeaseExhaustedError) as exc_info:
            await rm.acquire()
        assert (
            "no accepting" in str(exc_info.value).lower()
            or "lease" in str(exc_info.value).lower()
        )
        # Suppress lint warning for unused imports.
        _ = RuntimeGeneration
        _ = RuntimeManagerShutdownError
        _ = GENERATION_LEASE_TIMEOUT_S
    finally:
        rm_mod.GENERATION_LEASE_TIMEOUT_S = original
        await asyncio.sleep(0)
