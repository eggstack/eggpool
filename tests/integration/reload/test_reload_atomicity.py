"""Reload atomicity and diagnostics tests.

Tests that verify:
- Persistence/publication split behavior
- Process mutation timing relative to publication
- Semantic no-op, ignored-only, and failure diagnostics
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from eggpool.control.reload_manager import ReloadInProgressError
from tests.support.reload_faults import FaultType, ReloadFaultInjector
from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_successful_reload_preserves_generation_identity(
    reload_harness: ReloadHarness,
) -> None:
    """A successful reload produces a new generation with correct identity."""
    pre_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    result = await reload_harness.reload()

    assert result.ok is True
    assert result.generation is not None
    assert result.generation != pre_snapshot.active_generation_id

    post_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)
    assert post_snapshot.active_generation_id == result.generation
    assert post_snapshot.config_digest != pre_snapshot.config_digest


@pytest.mark.asyncio()
async def test_build_failure_preserves_active_generation(
    reload_harness: ReloadHarness,
) -> None:
    """Injecting a build failure preserves the active generation unchanged."""
    pre_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    injector = ReloadFaultInjector(
        target_stage="on_candidate_started",
        fault_type=FaultType.RECOVERABLE,
    )
    result = await reload_harness.reload(observer=injector)

    assert result.ok is False
    assert injector.fired

    post_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)
    diffs = post_snapshot.assert_same_generation(pre_snapshot)
    assert diffs == [], f"Active generation changed after build failure: {diffs}"


@pytest.mark.asyncio()
async def test_publish_failure_preserves_active_generation(
    reload_harness: ReloadHarness,
) -> None:
    """Injecting a publish failure preserves the active generation unchanged."""
    pre_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
        "simulated publish failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

    assert result.ok is False

    post_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)
    diffs = post_snapshot.assert_same_generation(pre_snapshot)
    assert diffs == [], f"Active generation changed after publish failure: {diffs}"


@pytest.mark.asyncio()
async def test_reconcile_failure_preserves_active_generation(
    reload_harness: ReloadHarness,
) -> None:
    """Injecting a reconciliation failure preserves the active generation unchanged."""
    pre_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    reload_harness.reload_manager.TEST_INJECT_RECONCILE_FAILURE = RuntimeError(
        "simulated reconcile failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_RECONCILE_FAILURE = None

    assert result.ok is False

    post_snapshot = RuntimeSnapshot.capture(reload_harness.runtime_manager)
    diffs = post_snapshot.assert_same_generation(pre_snapshot)
    assert diffs == [], f"Active generation changed after reconcile failure: {diffs}"


@pytest.mark.asyncio()
async def test_semantic_no_op_returns_success(
    reload_harness: ReloadHarness,
) -> None:
    """Reloading with the same config is a semantic no-op."""
    result = await reload_harness.reload(reload_harness.initial_config)

    assert result.ok is True
    assert "No configuration changes detected" in result.message
    assert result.generation is not None


@pytest.mark.asyncio()
async def test_restart_required_rejection(
    reload_harness: ReloadHarness,
) -> None:
    """A config with restart-required changes is rejected."""
    from eggpool.models.config import AppConfig, ServerConfig

    # server.port is RESTART_REQUIRED
    restart_config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=9999),
        providers=reload_harness.initial_config.providers,
    )

    result = await reload_harness.reload(restart_config)

    assert result.ok is False
    assert result.restart_required is not None
    assert len(result.restart_required) > 0


@pytest.mark.asyncio()
async def test_concurrent_reload_returns_busy_immediately(
    reload_harness: ReloadHarness,
) -> None:
    """Second concurrent reload gets ReloadInProgressError."""
    preparation_event = asyncio.Event()
    reload_harness.reload_manager.preparation_event = preparation_event

    first_result = None
    second_error: Exception | None = None

    async def do_first() -> None:
        nonlocal first_result
        first_result = await reload_harness.reload()

    async def do_second() -> None:
        nonlocal second_error
        try:
            await reload_harness.reload()
        except ReloadInProgressError as exc:
            second_error = exc

    t1 = asyncio.create_task(do_first())
    await asyncio.sleep(0.05)  # Let first acquire lock and enter build

    t2 = asyncio.create_task(do_second())

    preparation_event.set()
    await asyncio.gather(t1, t2, return_exceptions=True)
    reload_harness.reload_manager.preparation_event = None

    # Second reload should have been rejected
    assert isinstance(second_error, ReloadInProgressError)


@pytest.mark.asyncio()
async def test_reload_failure_records_operational_event(
    reload_harness: ReloadHarness,
) -> None:
    """A build failure records an operational event."""
    reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = RuntimeError(
        "simulated build failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = None

    assert result.ok is False
    # The reload error count should have increased
    assert reload_harness.reload_manager._reload_error_count > 0


@pytest.mark.asyncio()
async def test_reload_result_tracks_duration(
    reload_harness: ReloadHarness,
) -> None:
    """Successful reload records a positive duration."""
    result = await reload_harness.reload()

    assert result.ok is True
    # Duration is not in ReloadResult but in ReloadOperationResult
    op_result = reload_harness.reload_manager._last_reload_result
    assert op_result is not None
    assert op_result.duration_s >= 0


@pytest.mark.asyncio()
async def test_multiple_sequential_reloads_increment_generation(
    reload_harness: ReloadHarness,
) -> None:
    """Each successful reload with distinct configs increments the generation ID."""
    result1 = await reload_harness.reload()
    assert result1.ok is True
    gen1 = result1.generation

    # Use a different config for the second reload to avoid a semantic no-op
    from eggpool.models.config import (
        AccountConfig,
        AppConfig,
        ProviderConfig,
        RoutingConfig,
    )

    third_config = AppConfig(
        server=reload_harness.initial_config.server,
        providers={
            "test-provider-a": ProviderConfig(
                id="test-provider-a",
                base_url="https://a.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-a1", api_key="test-key-a1"),
                ],
            ),
            "test-provider-b": ProviderConfig(
                id="test-provider-b",
                base_url="https://b.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-b1", api_key="test-key-b1"),
                    AccountConfig(name="acct-b2", api_key="test-key-b2"),
                ],
            ),
        },
        routing=RoutingConfig(strategy="quota_fair", local_quota_mode="score_only"),
    )

    result2 = await reload_harness.reload(third_config)
    assert result2.ok is True
    gen2 = result2.generation

    assert gen2 > gen1
