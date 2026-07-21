"""Reload retirement and lease-drain tests.

Tests that verify:
- Reload completes promptly without blocking on lease drain (Phase 3)
- Active generation changes even when leases are held
- Old generation is retired asynchronously after new generation is published
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
async def test_reload_completes_promptly_with_held_lease(
    reload_harness: ReloadHarness,
) -> None:
    """Phase 3: reload completes promptly even with held leases.

    The reload manager's ``install_candidate`` spawns a background
    retirement task and returns immediately.  Publication is no longer
    gated on old-generation lease drain.
    """
    # Acquire a lease on the current generation
    lease = await reload_harness.runtime_manager.acquire()
    assert lease.generation_id == 0

    # Reload — should complete promptly (not block on drain)
    start = time.monotonic()
    result = await reload_harness.reload()
    elapsed = time.monotonic() - start

    assert result.ok is True

    # Reload completes promptly — not gated on drain timeout
    assert elapsed < 1.0, (
        f"Reload took {elapsed:.2f}s — expected prompt completion (Phase 3)"
    )

    # Release the old lease (retirement should now complete)
    await lease.release()
    # Wait for the retirement task to finish
    await asyncio.sleep(0.2)


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
    # Wait for retirement to complete
    await asyncio.sleep(0.2)


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

    # Wait for retirement to complete
    await asyncio.sleep(0.2)

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
