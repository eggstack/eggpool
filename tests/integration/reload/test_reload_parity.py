"""Reload construction parity tests.

Tests that verify the candidate generation builder produces a structurally
complete generation comparable to the initial startup generation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

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
    assert active.outbound_manager is not None
    assert active.health_manager is not None
    assert active.cost_calculator is not None
    assert active.transcoder_policy is not None
    assert active.compression_policy is not None
    assert active.cache_config is not None
    assert active.compression_tuning_registry is not None
    assert active.dispatch_overhead_recorder is not None
    assert active.dispatch_span_recorder is not None
    assert active.account_backoff_repo is not None
    assert active.stats_service is not None
    assert active.supervisor is not None
    assert active.finalization_retry_queue is not None
    assert active.routing_trace_guard is not None


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
