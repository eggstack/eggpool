"""Plan 019 closure gates #17, #18 — Finalization status and counter assertions.

G1/G2/G3: ReloadResult.finalization_status and counters are truthful.
- retry_pending when finalization is unresolved
- completed when fully finalized
- counters advance only on actual completion
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Closure gate #17: finalization_status distinguishes states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_completed_reload_has_finalization_status_completed(
    reload_harness: ReloadHarness,
) -> None:
    """A successful reload with no failures has finalization_status='completed'."""
    result = await reload_harness.reload()
    assert result.ok is True
    assert result.finalization_status == "completed"
    assert result.old_generation_id is not None


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_retry_pending_reload_has_finalization_status(
    reload_harness: ReloadHarness,
) -> None:
    """A reload with persistent retirement failure has retry_pending status."""
    rm = reload_harness.reload_manager

    # First reload succeeds.
    result1 = await reload_harness.reload()
    assert result1.ok is True

    # Inject permanent retirement failure.
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("permanent failure")
    try:
        result2 = await reload_harness.reload(config=reload_harness.initial_config)
        assert result2.ok is True
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    # The result should indicate retry_pending finalization.
    assert result2.finalization_status == "retry_pending"
    assert result2.finalization_next_step is not None
    assert result2.finalization_attempt_count >= 1
    assert result2.old_generation_id is not None
    assert result2.pending_swap_committed is True


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_finalization_status_after_successful_retry(
    reload_harness: ReloadHarness,
) -> None:
    """After retry resolves the job, subsequent reload has completed status."""
    rm = reload_harness.reload_manager

    # First reload.
    result1 = await reload_harness.reload()
    assert result1.ok is True

    # Fail-once retirement.
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("fail-once")
    try:
        result2 = await reload_harness.reload(config=reload_harness.initial_config)
        assert result2.ok is True
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    # Second reload — admission retries the pending job and succeeds.
    result3 = await reload_harness.reload(config=reload_harness.candidate_config)
    assert result3.ok is True
    assert result3.finalization_status == "completed"


# ---------------------------------------------------------------------------
# Closure gate #18: counter assertions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_accepted_reloads_counter_increments(
    reload_harness: ReloadHarness,
) -> None:
    """accepted_reloads counter increments on each accepted reload."""
    rm = reload_harness.reload_manager
    snap_before = rm.snapshot()
    accepted_before = snap_before["counters"]["accepted_reloads"]

    await reload_harness.reload()

    snap_after = rm.snapshot()
    assert snap_after["counters"]["accepted_reloads"] == accepted_before + 1


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_fully_finalized_reloads_counter_increments(
    reload_harness: ReloadHarness,
) -> None:
    """fully_finalized_reloads increments only on complete finalization."""
    rm = reload_harness.reload_manager
    snap_before = rm.snapshot()
    finalized_before = snap_before["counters"]["fully_finalized_reloads"]

    result = await reload_harness.reload()
    assert result.ok is True

    snap_after = rm.snapshot()
    assert snap_after["counters"]["fully_finalized_reloads"] == finalized_before + 1


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_fully_finalized_not_incremented_on_retry_pending(
    reload_harness: ReloadHarness,
) -> None:
    """fully_finalized_reloads does NOT increment when finalization is retry_pending."""
    rm = reload_harness.reload_manager

    # First reload — completed.
    await reload_harness.reload()
    snap1 = rm.snapshot()
    finalized_after_first = snap1["counters"]["fully_finalized_reloads"]

    # Inject permanent retirement failure.
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("permanent failure")
    try:
        await reload_harness.reload(config=reload_harness.initial_config)
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    snap2 = rm.snapshot()
    # fully_finalized should NOT have incremented for the failed-finalization reload.
    assert snap2["counters"]["fully_finalized_reloads"] == finalized_after_first


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_accepted_finalization_failures_counter(
    reload_harness: ReloadHarness,
) -> None:
    """accepted_finalization_failures increments when finalization is retry_pending."""
    rm = reload_harness.reload_manager

    # First reload to establish candidate as active.
    await reload_harness.reload()
    snap_before = rm.snapshot()
    failures_before = snap_before["counters"]["accepted_finalization_failures"]

    # Inject permanent retirement failure — second reload to initial_config.
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("failure")
    try:
        await reload_harness.reload(config=reload_harness.initial_config)
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    snap_after = rm.snapshot()
    assert (
        snap_after["counters"]["accepted_finalization_failures"] == failures_before + 1
    )


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_accepted_finalization_retries_counter(
    reload_harness: ReloadHarness,
) -> None:
    """accepted_finalization_retries accumulates retry_count from jobs."""
    rm = reload_harness.reload_manager

    # First reload to establish candidate as active.
    await reload_harness.reload()
    snap_before = rm.snapshot()
    retries_before = snap_before["counters"]["accepted_finalization_retries"]

    # Inject permanent retirement failure (1 retry attempt).
    rm.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError("retry test")
    try:
        await reload_harness.reload(config=reload_harness.initial_config)
    finally:
        rm.TEST_INJECT_RETIREMENT_FAILURE = None

    snap_after = rm.snapshot()
    # At least one retry should have occurred.
    assert snap_after["counters"]["accepted_finalization_retries"] > retries_before
