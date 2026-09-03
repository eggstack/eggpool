"""Reload construction parity tests.

Tests that verify the candidate generation builder produces a structurally
complete generation comparable to the initial startup generation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.model_router.affinity import (
    ModelRouterAffinity,
    session_identity_from_header,
)
from eggpool.model_router.config import ModelRouterConfig
from eggpool.model_router.registry import ModelRouterRegistry
from eggpool.model_router.selector import ModelSelection
from tests.support.reload_faults import FaultType, ReloadFaultInjector
from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_candidate_generation_has_required_services(
    reload_harness: ReloadHarness,
) -> None:
    """The candidate generation has all required service fields."""
    result = await reload_harness.reload()
    assert result.ok is True

    active = reload_harness.runtime_manager.active_snapshot()

    # Verify all required services are present (not None)
    assert active.registry is not None
    assert active.catalog is not None
    assert active.router is not None
    assert active.coordinator is not None
    assert active.client_pool is not None
    # Outbound transport is optional in the lean default generation.
    assert active.health_manager is not None
    assert active.cost_calculator is not None
    assert active.transcoder_policy is not None
    assert active.dispatch_overhead_recorder is not None
    assert active.account_backoff_repo is not None
    assert active.stats_service is not None
    assert active.supervisor is not None
    assert active.finalization_supervisor is not None
    # Compression, detailed spans, routing traces, and outbound access are
    # opt-in generation resources under the lean defaults.


@pytest.mark.asyncio()
async def test_generation_config_matches_candidate_config(
    reload_harness: ReloadHarness,
) -> None:
    """The published generation carries the candidate config."""
    result = await reload_harness.reload()
    assert result.ok is True

    active = reload_harness.runtime_manager.active_snapshot()
    assert active.config is reload_harness.candidate_config
    assert active.config_digest != ""


@pytest.mark.asyncio()
async def test_build_failure_does_not_corrupt_active_generation(
    reload_harness: ReloadHarness,
) -> None:
    """A failed candidate build leaves the active generation intact."""
    pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    injector = ReloadFaultInjector(
        target_stage="on_candidate_started",
        fault_type=FaultType.RECOVERABLE,
    )
    result = await reload_harness.reload(observer=injector)
    assert result.ok is False

    post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
    diffs = post.assert_same_generation(pre)
    assert diffs == [], f"Active generation changed after build failure: {diffs}"


@pytest.mark.asyncio()
async def test_reload_preserves_process_runtime(
    reload_harness: ReloadHarness,
) -> None:
    """Reload does not create a new ProcessRuntime — it is process-owned."""
    proc_id = id(reload_harness.process)

    result = await reload_harness.reload()
    assert result.ok is True

    # ProcessRuntime should be the same object
    assert id(reload_harness.process) == proc_id
    # db should be the same connection
    assert reload_harness.process.db is reload_harness.db


@pytest.mark.asyncio()
async def test_reload_generator_id_is_monotonic(
    reload_harness: ReloadHarness,
) -> None:
    """Each reload with a changed config produces a strictly increasing ID."""
    from tests.support.reload_harness import make_candidate_config, make_initial_config

    configs = [make_initial_config(), make_candidate_config()]
    ids = []
    for i in range(5):
        config = configs[i % 2]
        result = await reload_harness.reload(config)
        assert result.ok is True
        ids.append(result.generation)

    for i in range(1, len(ids)):
        assert ids[i] > ids[i - 1], f"Generation IDs not monotonic: {ids}"


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_staged_rehash_preserves_and_invalidates_router_affinity(
    reload_harness: ReloadHarness,
) -> None:
    """Generation publication preserves only semantically compatible affinity."""
    from tests.support.reload_harness import make_initial_config

    router_config = ModelRouterConfig.model_validate(
        {
            "selector_model": "selector/local",
            "default_model": "model-default",
            "routes": {
                "default": {"model": "model-default", "description": "Default"},
                "fast": {"model": "model-fast", "description": "Fast"},
            },
        }
    )
    configured = reload_harness.candidate_config.model_copy(
        update={"model_routers": {"virtual": router_config}}
    )
    cache = ModelRouterAffinity()
    reload_harness.process.model_router_affinity = cache
    identity = session_identity_from_header("rehash-session")
    assert identity is not None
    initial_router = ModelRouterRegistry.from_config(configured.model_routers).get(
        "virtual"
    )
    assert initial_router is not None
    route = initial_router.route_by_id["1"]
    calls = 0

    async def select_initial() -> ModelSelection:
        nonlocal calls
        calls += 1
        return ModelSelection(
            virtual_model="virtual",
            route_id=route.route_id,
            route_label=route.label,
            concrete_model=route.model,
            source="selector",
            selector_attempts=1,
            selector_latency_ms=0.1,
        )

    await cache.resolve(initial_router, identity, select_initial)
    assert calls == 1

    assert (await reload_harness.reload(configured)).ok is True
    active_router = (
        reload_harness.runtime_manager.active_snapshot().model_router_registry.get(
            "virtual"
        )
    )
    assert active_router is not None

    async def must_not_select() -> ModelSelection:
        raise AssertionError("unchanged router fingerprint should hit affinity")

    unchanged = await cache.resolve(active_router, identity, must_not_select)
    assert unchanged.cache_hit is True

    unrelated_change = configured.model_copy(
        update={"routing": make_initial_config().routing}
    )
    assert (await reload_harness.reload(unrelated_change)).ok is True
    active_router = (
        reload_harness.runtime_manager.active_snapshot().model_router_registry.get(
            "virtual"
        )
    )
    assert active_router is not None
    assert (
        await cache.resolve(active_router, identity, must_not_select)
    ).cache_hit is True

    changed_router_config = ModelRouterConfig.model_validate(
        {
            **router_config.model_dump(),
            "routes": {
                "default": {
                    "model": "model-default",
                    "description": "Changed policy",
                },
                "fast": {"model": "model-fast", "description": "Fast"},
            },
        }
    )
    changed_config = configured.model_copy(
        update={"model_routers": {"virtual": changed_router_config}}
    )
    assert (await reload_harness.reload(changed_config)).ok is True
    changed_router = (
        reload_harness.runtime_manager.active_snapshot().model_router_registry.get(
            "virtual"
        )
    )
    assert changed_router is not None
    reselected = await cache.resolve(changed_router, identity, select_initial)
    assert reselected.cache_hit is False
    assert calls == 2

    invalid_routes = {
        f"route-{index:03d}": {
            "model": "model-default",
            "description": "x" * 512,
        }
        for index in range(140)
    }
    invalid_config = changed_config.model_copy(
        update={
            "model_routers": {
                "virtual": ModelRouterConfig.model_validate(
                    {
                        "selector_model": "selector/local",
                        "default_model": "model-default",
                        "routes": invalid_routes,
                    }
                )
            }
        }
    )
    assert (await reload_harness.reload(invalid_config)).ok is False
    assert (
        reload_harness.runtime_manager.active_snapshot().model_router_registry.get(
            "virtual"
        )
        is changed_router
    )

    removed_config = changed_config.model_copy(update={"model_routers": {}})
    assert (await reload_harness.reload(removed_config)).ok is True
    assert (
        reload_harness.runtime_manager.active_snapshot().model_router_registry.get(
            "virtual"
        )
        is None
    )
