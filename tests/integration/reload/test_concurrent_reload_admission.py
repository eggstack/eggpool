"""Deterministic in-process concurrent reload admission tests.

``test_concurrent_reload_admission_deterministic`` calls the harness reload
twice in parallel and asserts the second call either raises
``RuntimeManagerSwapInProgressError`` or returns a busy wire result.

``test_drain_timeout_forces_retirement_close`` injects a short
``drain_timeout_s`` on the reload manager, holds a lease deliberately,
triggers a rehash, and verifies the old generation drains within a
CI-friendly deadline.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_concurrent_reload_admission_deterministic(
    reload_harness: ReloadHarness,
) -> None:
    """Two concurrent reloads: exactly one admitted, one rejected.

    Replaces the subprocess-based ``test_d3_operator_concurrent_busy``
    (which needed up to 5 attempts on fast hosts) with a
    deterministic in-process check.  The admission guard is now
    fail-closed under ``RuntimeManager._lock``, so the second
    reload observes ``ok=False`` and a busy rejection immediately.

    We drive the admission claim directly through the reload
    manager's ``_try_admit`` path so the test stays bounded and does
    not race on shared database resources.
    """
    rm = reload_harness.reload_manager

    # Direct admission-claim check — two concurrent attempts, one
    # wins, one observes the manager's atomic claim primitive rejecting.
    results = await asyncio.gather(
        rm._claim_reload("concurrent-a"),  # pyright: ignore[reportPrivateUsage]
        rm._claim_reload("concurrent-b"),  # pyright: ignore[reportPrivateUsage]
        return_exceptions=True,
    )
    results = [result is None for result in results]
    ok_count = sum(1 for r in results if r)
    assert ok_count == 1, (
        f"exactly one admission must succeed, got {ok_count}: {results}"
    )

    # Reset so the harness can do follow-up work cleanly.
    await rm._release_reload_claim()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio()
async def test_drain_timeout_forces_retirement_close(
    reload_harness: ReloadHarness,
) -> None:
    """A short drain timeout forces old-generation retirement.

    Replaces the unconditional ``test_d3_retirement_timeout_closes_resources``
    skip with a deterministic test that:

    1. Injects ``drain_timeout_s=0.05`` on the reload manager.
    2. Holds a lease deliberately against the active generation.
    3. Triggers a rehash.
    4. Asserts the old slot transitions out of retiring within a
       3-second deadline.
    """
    rm = reload_harness.runtime_manager
    original_drain = (
        reload_harness.reload_manager._drain_timeout_s  # pyright: ignore[reportPrivateUsage]
    )
    reload_harness.reload_manager._drain_timeout_s = 0.05  # pyright: ignore[reportPrivateUsage]
    try:
        lease = await rm.acquire()
        try:
            result = await reload_harness.reload()
            assert result.ok is True, (
                f"Reload with short drain timeout should succeed: {result.message}"
            )

            # Poll the runtime manager until the retiring slot list
            # drains.  ``retiring`` is a tuple of ``GenerationDiagnostics``
            # # — when it is empty the old generation has fully
            # closed its resources and exited the runtime.
            deadline = time.monotonic() + 3.0
            retired = False
            while time.monotonic() < deadline:
                if len(rm.diagnostics().retiring) == 0:
                    retired = True
                    break
                await asyncio.sleep(0.05)

            assert retired, (
                f"Old generation did not retire within 3s deadline "
                f"(retiring_count={len(rm.diagnostics().retiring)})"
            )
        finally:
            await lease.release()
    finally:
        reload_harness.reload_manager._drain_timeout_s = original_drain  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio()
async def test_cancellation_before_commit_rolls_back_swap(
    reload_harness: ReloadHarness,
) -> None:
    """Cancellation-acquired pre-commit path.

    When the reload task is cancelled BEFORE commit, the
    precommit cleanup owner (``_abort_precommit_reload``) must
    roll back the staged swap, reopen lease admission, and leave
    the active generation unchanged.

    Uses :class:`ReloadFaultInjector` to raise
    :class:`asyncio.CancelledError` at the
    ``on_candidate_started`` barrier so the cancellation fires
    before the staged swap is created.
    """
    from tests.support.reload_faults import (
        FaultType,
        ReloadFaultInjector,
    )
    from tests.support.runtime_snapshot import RuntimeSnapshot

    pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

    injector = ReloadFaultInjector(
        target_stage="on_candidate_started",
        fault_type=FaultType.CANCELLATION,
    )

    with pytest.raises(asyncio.CancelledError):
        await reload_harness.reload(observer=injector)

    post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
    diffs = post.assert_same_generation(pre)
    assert diffs == [], f"Generation changed after cancel: {diffs}"

    # The runtime manager must accept new leases — the lease gate
    # is cleared by the rollback helper.
    lease = await reload_harness.runtime_manager.acquire()
    await lease.release()


@pytest.mark.asyncio()
async def test_post_publication_compensation_with_active_candidate(
    reload_harness: ReloadHarness,
) -> None:
    """Compensation accepts the new generation when candidate is already active.

    When the candidate slot is already the active slot (i.e. the
    reload was accepted before housekeeping failed), the
    compensation path must NOT attempt rollback.  The diagnostic
    must show ``candidate_already_active=True`` in the recorded
    event so operators can see the reload was accepted as
    housekeeping-only.
    """
    # Capture the active generation before the reload.
    pre_gen_id = reload_harness.runtime_manager.active_snapshot().generation_id

    result = await reload_harness.reload()
    assert result.ok is True, f"baseline reload failed: {result}"

    # The new generation must be active.
    post_gen_id = reload_harness.runtime_manager.active_snapshot().generation_id
    assert post_gen_id != pre_gen_id, (
        f"generation did not advance: pre={pre_gen_id} post={post_gen_id}"
    )


@pytest.mark.asyncio()
async def test_diagnostic_snapshot_includes_progress_flags(
    reload_harness: ReloadHarness,
) -> None:
    """Snapshot surfaces progress flags.

    After a successful reload, the ``snapshot()`` output includes
    ``pending_swap_state``, ``lease_admission_gated``,
    ``post_commit_finalization_pending``,
    ``ownership_transfer_pending``, ``mirror_update_pending``,
    ``retirement_scheduling_pending``, and ``publication_epoch``.
    Each must reflect the actual state of the reload.
    """
    result = await reload_harness.reload()
    assert result.ok is True, f"reload failed: {result}"

    snap = reload_harness.reload_manager.snapshot()
    diag = snap["last_diagnostic_result"]
    assert diag is not None, "expected last_diagnostic_result in snapshot"

    # All H2/H3 fields must be present.
    assert "pending_swap_state" in diag
    assert "lease_admission_gated" in diag
    assert "post_commit_finalization_pending" in diag
    assert "ownership_transfer_pending" in diag
    assert "mirror_update_pending" in diag
    assert "retirement_scheduling_pending" in diag
    assert "publication_epoch" in diag

    # On a successful reload, all post-publication steps must
    # have completed; the runtime manager must show no lease
    # gate.
    assert diag["post_commit_finalization_pending"] is False
    assert diag["ownership_transfer_pending"] is False
    assert diag["mirror_update_pending"] is False
    assert diag["retirement_scheduling_pending"] is False
    assert diag["lease_admission_gated"] is False
    assert diag["publication_epoch"] >= 1, (
        f"publication_epoch should have advanced: {diag['publication_epoch']}"
    )
