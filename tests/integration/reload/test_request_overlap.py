"""Request overlap tests (D4).

Verifies per-request generation coherence: old in-flight requests use
old-generation services, new requests use new-generation services.

These tests acquire leases from the runtime manager before and after
a reload, then verify that the leased generation's services are
consistent with the expected generation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio
async def test_old_lease_uses_old_generation_services(
    reload_harness: ReloadHarness,
) -> None:
    """A lease acquired before reload points to old generation services.

    After reload, the old lease's catalog, coordinator, and other
    services must remain the same objects — not replaced by new
    generation services.
    """
    # Acquire a lease from the initial generation.
    old_lease = await reload_harness.runtime_manager.acquire()
    old_catalog = old_lease.runtime.catalog
    old_coordinator = old_lease.runtime.coordinator
    old_gen_id = old_lease.runtime.generation_id

    # Reload with a new configuration.
    result = await reload_harness.reload()
    assert result.ok is True

    # The old lease still points to the old generation's services.
    assert old_lease.runtime.catalog is old_catalog
    assert old_lease.runtime.coordinator is old_coordinator
    assert old_lease.runtime.generation_id == old_gen_id

    # Release the old lease.
    await old_lease.release()


@pytest.mark.asyncio
async def test_new_lease_uses_new_generation_services(
    reload_harness: ReloadHarness,
) -> None:
    """A lease acquired after reload points to new generation services.

    The new lease's catalog and coordinator must be different objects
    from the old generation's services.
    """
    # Capture old generation service identities.
    old_lease = await reload_harness.runtime_manager.acquire()
    old_catalog_id = id(old_lease.runtime.catalog)
    old_coordinator_id = id(old_lease.runtime.coordinator)
    await old_lease.release()

    # Reload with a new configuration.
    result = await reload_harness.reload()
    assert result.ok is True

    # Acquire a new lease — it must point to new generation services.
    new_lease = await reload_harness.runtime_manager.acquire()
    assert new_lease.runtime.generation_id != old_lease.runtime.generation_id
    assert id(new_lease.runtime.catalog) != old_catalog_id
    assert id(new_lease.runtime.coordinator) != old_coordinator_id

    await new_lease.release()


@pytest.mark.asyncio
async def test_concurrent_old_and_new_leases_during_reload(
    reload_harness: ReloadHarness,
) -> None:
    """Old and new leases coexist during reload with different services.

    An old lease held during reload must not observe any new-generation
    service object, and a new lease must not observe any old-generation
    service object.
    """
    # Acquire old lease.
    old_lease = await reload_harness.runtime_manager.acquire()
    old_gen_id = old_lease.runtime.generation_id
    old_catalog = old_lease.runtime.catalog

    # Reload — old lease is still held.
    result = await reload_harness.reload()
    assert result.ok is True

    # New lease gets new generation.
    new_lease = await reload_harness.runtime_manager.acquire()
    new_gen_id = new_lease.runtime.generation_id

    # Generations are different.
    assert new_gen_id != old_gen_id

    # Services are different objects.
    assert new_lease.runtime.catalog is not old_catalog

    # Old lease still points to old generation.
    assert old_lease.runtime.generation_id == old_gen_id
    assert old_lease.runtime.catalog is old_catalog

    await old_lease.release()
    await new_lease.release()


@pytest.mark.asyncio
async def test_old_generation_config_values_preserved_in_lease(
    reload_harness: ReloadHarness,
) -> None:
    """Old lease preserves old generation's config values after reload.

    The old lease's config must reflect the initial configuration,
    not the reloaded configuration.
    """
    # Acquire old lease.
    old_lease = await reload_harness.runtime_manager.acquire()
    old_config = old_lease.runtime.config

    # Reload with a different configuration.
    result = await reload_harness.reload()
    assert result.ok is True

    # Old lease still has old config.
    assert old_lease.runtime.config is old_config
    assert old_lease.runtime.config.routing.strategy == "quota_fair"

    # New lease has new config.
    new_lease = await reload_harness.runtime_manager.acquire()
    assert new_lease.runtime.config is not old_config
    assert new_lease.runtime.config.routing.local_quota_mode == "score_only"

    await old_lease.release()
    await new_lease.release()
