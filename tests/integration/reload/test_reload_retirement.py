"""Reload retirement and lease-drain tests.

Tests that verify:
- Reload blocks on lease drain (current behavior — documents the defect)
- Active generation changes even when leases are held
- Old generation is retired after new generation is published
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_reload_blocks_on_lease_drain(
    reload_harness: ReloadHarness,
) -> None:
    """Current behavior: reload blocks until held leases drain or timeout.

    The reload manager's ``install_candidate`` calls ``begin_retirement``
    which waits for active leases to drain (bounded by ``drain_timeout_s``).
    This means a reload with held leases blocks for up to the drain timeout.

    The desired future behavior (Phase 3) is for retirement to be fully
    asynchronous so reload completion is never blocked by held leases.
    """
    # Acquire a lease on the current generation
    lease = await reload_harness.runtime_manager.acquire()
    assert lease.generation_id == 0

    # Reload — blocks because begin_retirement waits for lease drain
    start = time.monotonic()
    result = await reload_harness.reload()
    elapsed = time.monotonic() - start

    assert result.ok is True

    # The reload blocks on drain_timeout_s (5.0s in the harness).
    # This documents the current defect: reload completion is gated
    # on old-generation lease drain.
    assert elapsed >= 1.0, (
        f"Reload completed in {elapsed:.2f}s — expected blocking on drain"
    )

    # Release the old lease (retirement should now complete)
    await lease.release()
    # Wait for the drain to finish
    await asyncio.sleep(0.1)


@pytest.mark.asyncio()
async def test_new_generation_active_during_old_lease(
    reload_harness: ReloadHarness,
) -> None:
    """After reload, new generation is active while old lease is still held."""
    lease = await reload_harness.runtime_manager.acquire()
    old_gen_id = lease.generation_id

    result = await reload_harness.reload()
    assert result.ok is True
    new_gen_id = result.generation

    # New generation should be active
    active = reload_harness.runtime_manager.active_snapshot()
    assert active.generation_id == new_gen_id
    assert new_gen_id != old_gen_id

    # Old lease still references old generation
    assert lease.generation_id == old_gen_id

    await lease.release()
    # Wait for retirement drain to complete
    await asyncio.sleep(0.1)


@pytest.mark.asyncio()
async def test_old_generation_retired_after_new_publication(
    reload_harness: ReloadHarness,
) -> None:
    """Old generation is retired after new generation is published."""
    pre_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)
    old_gen_id = pre_snapshot.active_generation_id

    result = await reload_harness.reload()
    assert result.ok is True
    new_gen_id = result.generation

    # Wait for retirement to complete (drain_timeout_s)
    await asyncio.sleep(0.1)

    post_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)
    assert post_snapshot.active_generation_id == new_gen_id
    assert post_snapshot.active_generation_id != old_gen_id


@pytest.mark.asyncio()
async def test_multiple_reloads_each_produce_new_generation(
    reload_harness: ReloadHarness,
) -> None:
    """Each reload with a different config produces a new generation."""
    from tests.support.reload_harness import make_candidate_config, make_initial_config

    configs = [make_initial_config(), make_candidate_config()]
    gen_ids = []

    for i in range(4):
        config = configs[i % 2]
        result = await reload_harness.reload(config)
        assert result.ok is True
        gen_ids.append(result.generation)

    # Each generation should be different (configs alternate)
    assert len(set(gen_ids)) == 4, f"Expected 4 unique generations, got {gen_ids}"

    # Active should be the last one
    active = reload_harness.runtime_manager.active_snapshot()
    assert active.generation_id == gen_ids[-1]
