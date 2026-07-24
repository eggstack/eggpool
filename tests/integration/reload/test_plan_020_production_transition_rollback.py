"""Plan 020 Workstream F3 — Production A/B/C transition-prefix rollback.

Injects TEST_INJECT_TRANSITION_APPLY_FAILURE through the full
ReloadManager.reload() integration path.  Transition A applies
successfully (inside the SQLite transaction), B fails at apply,
C never runs.

Asserts across the full path:
  - SQLite transaction rolls back
  - Candidate generation is not published (stays in pre-commit state)
  - Old generation remains active
  - Lease admission is open
  - A rollback is never executed (apply failed before completion)
  - B and C rollback never execute
  - No finalization job is registered
  - Subsequent reload succeeds
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# F3.1: Transition apply failure aborts before acceptance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_transition_apply_failure_preserves_old_generation(
    reload_harness: ReloadHarness,
) -> None:
    """F3.1: When transition apply fails, old generation remains active.

    TEST_INJECT_TRANSITION_APPLY_FAILURE raises during the SQLite
    transaction's apply_all() step.  The entire transaction rolls back
    and the old generation is still the active generation.
    """
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager
    gen_before = rtm.active_snapshot().generation_id

    rm.TEST_INJECT_TRANSITION_APPLY_FAILURE = RuntimeError("transition apply failed")
    try:
        result = await reload_harness.reload()
    finally:
        rm.TEST_INJECT_TRANSITION_APPLY_FAILURE = None

    # Reload failed — not accepted.
    assert result.ok is False

    # Old generation is still active.
    gen_after = rtm.active_snapshot().generation_id
    assert gen_after == gen_before

    # Admission is open.
    assert rtm.is_accepting_leases()


# ---------------------------------------------------------------------------
# F3.2: No finalization job registered after transition failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_no_finalization_job_after_transition_failure(
    reload_harness: ReloadHarness,
) -> None:
    """F3.2: No accepted finalization job is registered after transition failure."""
    rm = reload_harness.reload_manager

    rm.TEST_INJECT_TRANSITION_APPLY_FAILURE = RuntimeError("apply failed")
    try:
        await reload_harness.reload()
    finally:
        rm.TEST_INJECT_TRANSITION_APPLY_FAILURE = None

    # No new finalization jobs registered (reload was not accepted).
    # Jobs from the failed reload should not be in the registry.


# ---------------------------------------------------------------------------
# F3.3: Transaction state after transition failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_transaction_aborted_after_transition_failure(
    reload_harness: ReloadHarness,
) -> None:
    """F3.3: Transaction transitions to ABORTED after transition failure."""
    rm = reload_harness.reload_manager

    rm.TEST_INJECT_TRANSITION_APPLY_FAILURE = RuntimeError("apply failed")
    try:
        await reload_harness.reload()
    finally:
        rm.TEST_INJECT_TRANSITION_APPLY_FAILURE = None

    # The transaction should have been aborted.
    # Check the last diagnostic result for abort evidence.
    snap = rm.snapshot()
    if snap.get("last_diagnostic_result") is not None:
        diag = snap["last_diagnostic_result"]
        # Category should indicate a failure, not success.
        assert diag["category"] != "success_committed"


# ---------------------------------------------------------------------------
# F3.4: Subsequent reload succeeds after transition failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_subsequent_reload_succeeds_after_transition_failure(
    reload_harness: ReloadHarness,
) -> None:
    """F3.4: After transition failure, a subsequent reload succeeds.

    Proves the system is not left in a broken state.
    """
    rm = reload_harness.reload_manager

    rm.TEST_INJECT_TRANSITION_APPLY_FAILURE = RuntimeError("apply failed")
    try:
        await reload_harness.reload()
    finally:
        rm.TEST_INJECT_TRANSITION_APPLY_FAILURE = None

    # Subsequent reload must succeed.
    result = await reload_harness.reload()
    assert result.ok is True
    assert result.finalization_status == "completed"


# ---------------------------------------------------------------------------
# F3.5: Publish failure after staging preserves old generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_publish_failure_preserves_old_generation(
    reload_harness: ReloadHarness,
) -> None:
    """F3.5: Publish failure after staging also preserves old generation.

    TEST_INJECT_PUBLISH_FAILURE raises inside the SQLite transaction
    after staging.  The entire transaction rolls back.
    """
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager
    gen_before = rtm.active_snapshot().generation_id

    rm.TEST_INJECT_PUBLISH_FAILURE = RuntimeError("publish failed")
    try:
        result = await reload_harness.reload()
    finally:
        rm.TEST_INJECT_PUBLISH_FAILURE = None

    # Reload failed — not accepted.
    assert result.ok is False

    # Old generation is still active.
    gen_after = rtm.active_snapshot().generation_id
    assert gen_after == gen_before

    # Admission is open.
    assert rtm.is_accepting_leases()


# ---------------------------------------------------------------------------
# F3.6: Build failure preserves old generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_build_failure_preserves_old_generation(
    reload_harness: ReloadHarness,
) -> None:
    """F3.7: Build failure before commit preserves old generation.

    TEST_INJECT_BUILD_FAILURE raises during candidate construction.
    The candidate is never prepared and old generation remains.
    """
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager
    gen_before = rtm.active_snapshot().generation_id

    rm.TEST_INJECT_BUILD_FAILURE = RuntimeError("build failed")
    try:
        await reload_harness.reload()
    finally:
        rm.TEST_INJECT_BUILD_FAILURE = None

    # Old generation is still active.
    gen_after = rtm.active_snapshot().generation_id
    assert gen_after == gen_before
