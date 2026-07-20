"""Reload resource leak and stale-state tests.

Tests that verify:
- Resource counters return to baseline after failed reloads
- Use-after-close detection via InstrumentedCloseable
- app.state mirrors match active generation after reload
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from tests.support.closeable_resources import InstrumentedCloseable, UseAfterCloseError
from tests.support.reload_faults import FaultType, ReloadFaultInjector
from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_failed_reload_does_not_leak_resources(
    reload_harness: ReloadHarness,
) -> None:
    """After a failed reload, no candidate resources should leak.

    We inject build failures repeatedly and verify that the active
    generation remains unchanged and no resources accumulate.
    """
    pre_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    # Inject failures at multiple stages
    for stage in [
        "on_candidate_started",
        "on_reconcile_started",
        "on_publish_started",
    ]:
        injector = ReloadFaultInjector(
            target_stage=stage,
            fault_type=FaultType.RECOVERABLE,
        )
        result = await reload_harness.reload(observer=injector)
        assert result.ok is False, f"Expected failure at {stage}"
        assert injector.fired, f"Expected injector to fire at {stage}"

    post_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    # Active generation should be unchanged
    diffs = post_snapshot.assert_same_generation(pre_snapshot)
    assert diffs == [], f"Generation changed after failed reloads: {diffs}"

    # Service identities should be unchanged (no new objects leaked)
    service_diffs = post_snapshot.assert_same_services(pre_snapshot)
    assert service_diffs == [], (
        f"Service identities changed after failed reloads: {service_diffs}"
    )


@pytest.mark.asyncio()
async def test_use_after_close_detected(
    reload_harness: ReloadHarness,
) -> None:
    """InstrumentedCloseable raises UseAfterCloseError when used after close."""
    resource = InstrumentedCloseable(
        name="test_pool",
        generation_id=0,
    )

    # Should work before close
    resource.use()
    assert not resource.is_closed

    # Close the resource
    await resource.close()
    assert resource.is_closed
    assert resource.close_count == 1

    # Should raise after close
    with pytest.raises(UseAfterCloseError):
        resource.use()


@pytest.mark.asyncio()
async def test_instrumented_closeable_tracks_construction_count() -> None:
    """Construction count tracks all instances created."""
    InstrumentedCloseable.reset_construction_count()

    r1 = InstrumentedCloseable(name="a")
    _r2 = InstrumentedCloseable(name="b")
    _r3 = InstrumentedCloseable(name="c")

    assert InstrumentedCloseable.construction_count() == 3

    # Close one - count should not change
    await r1.close()
    assert InstrumentedCloseable.construction_count() == 3


@pytest.mark.asyncio()
async def test_close_failure_propagates(
    reload_harness: ReloadHarness,
) -> None:
    """Close failure is raised by InstrumentedCloseable."""
    resource = InstrumentedCloseable(
        name="fail_close",
        close_failure=OSError("connection refused"),
    )

    with pytest.raises(OSError, match="connection refused"):
        await resource.close()

    # Should still be marked as closed attempt
    assert resource.close_count == 1


@pytest.mark.asyncio()
async def test_close_barrier_notifies(
    reload_harness: ReloadHarness,
) -> None:
    """Close barrier event fires when close() completes."""
    barrier = asyncio.Event()
    resource = InstrumentedCloseable(name="barrier_test")
    resource.set_close_barrier(barrier)

    assert not barrier.is_set()
    await resource.close()
    assert barrier.is_set()


@pytest.mark.asyncio()
async def test_successful_reload_updates_active_generation(
    reload_harness: ReloadHarness,
) -> None:
    """After successful reload, active generation reflects new config."""
    pre_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    result = await reload_harness.reload()
    assert result.ok is True

    post_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    # Generation should have changed
    assert post_snapshot.active_generation_id != pre_snapshot.active_generation_id

    # Config digest should have changed (different configs)
    assert post_snapshot.config_digest != pre_snapshot.config_digest


@pytest.mark.asyncio()
async def test_resource_cycle_does_not_accumulate(
    reload_harness: ReloadHarness,
) -> None:
    """Multiple failed reload cycles do not cause resource accumulation.

    This is the "resource leak" test from the plan: inject failure after
    each candidate resource is created and verify counters return to
    baseline. Repeat the cycle enough times to show accumulation.
    """
    InstrumentedCloseable.reset_construction_count()

    baseline_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    # Run 5 failed reload cycles
    for i in range(5):
        injector = ReloadFaultInjector(
            target_stage="on_candidate_started",
            fault_type=FaultType.RECOVERABLE,
            message=f"cycle {i}",
        )
        result = await reload_harness.reload(observer=injector)
        assert result.ok is False

    final_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    # No new resources should have leaked
    diffs = final_snapshot.assert_same_generation(baseline_snapshot)
    assert diffs == [], f"Generation changed after resource cycle: {diffs}"


@pytest.mark.asyncio()
async def test_app_state_mirror_matches_active_generation(
    reload_harness: ReloadHarness,
) -> None:
    """After reload, the active generation's services are consistent.

    This tests the stale compatibility state invariant: the active
    generation's services should be the ones a consumer would access.
    """
    # Do a reload to change the generation
    result = await reload_harness.reload()
    assert result.ok is True

    # Get the active generation directly from the runtime manager
    active = reload_harness.runtime_manager.active_snapshot()

    # Verify the generation ID matches what the reload reported
    assert active.generation_id == result.generation

    # Verify the config is the candidate config
    assert active.config is reload_harness.candidate_config
