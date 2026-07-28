"""Transition cleanup and precommit ownership closure tests.

Tests transition rollback correctness, candidate resource close exactly
once, and PrecommitAbortOutcome structured diagnostics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.reload_transaction import (
    ProcessTransition,
    ProcessTransitionApplyError,
    TransitionApplyResult,
    TransitionRollbackOutcome,
)
from eggpool.runtime_manager import (
    RuntimeGenerationCandidate,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _InstrumentedTransition(ProcessTransition):
    """Transition with tracked apply/rollback/finalize calls."""

    def __init__(
        self,
        name: str,
        *,
        apply_raises: Exception | None = None,
        rollback_raises: Exception | None = None,
    ) -> None:
        super().__init__(
            name=name,
            description=f"test-{name}",
            reversible=True,
        )
        self.apply_calls = 0
        self.rollback_calls = 0
        self.finalize_calls = 0
        self._apply_raises = apply_raises
        self._rollback_raises = rollback_raises

    async def apply(self) -> None:
        self.apply_calls += 1
        if self._apply_raises is not None:
            raise self._apply_raises

    async def rollback(self) -> None:
        self.rollback_calls += 1
        if self._rollback_raises is not None:
            raise self._rollback_raises

    async def finalize(self) -> None:
        self.finalize_calls += 1


# ---------------------------------------------------------------------------
# Tests: partial transition rollback prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_partial_transition_rollback_prefix(
    reload_harness: ReloadHarness,
) -> None:
    """Three transitions where B fails; A is rolled back, C never applied."""
    trans_a = _InstrumentedTransition("A")
    trans_b = _InstrumentedTransition(
        "B",
        apply_raises=RuntimeError("B failed"),
    )
    trans_c = _InstrumentedTransition("C")

    plan = MagicMock()
    plan.transitions = [trans_a, trans_b, trans_c]

    result = TransitionApplyResult(plan)

    with pytest.raises(ProcessTransitionApplyError) as exc_info:
        await result.apply_all()

    err = exc_info.value
    assert err.failed_transition_name == "B"
    assert err.failed_transition_index == 1
    assert err.applied_transition_names == ("A",)

    # A was applied, B was attempted but failed, C was never touched.
    assert trans_a.apply_calls == 1
    assert trans_b.apply_calls == 1
    assert trans_c.apply_calls == 0

    # Now roll back — only A should be rolled back.
    outcome = await result.rollback_applied()
    assert isinstance(outcome, TransitionRollbackOutcome)
    assert outcome.attempted == ("A",)
    assert outcome.restored == ("A",)
    assert outcome.failures == ()

    assert trans_a.rollback_calls == 1


@pytest.mark.asyncio()
async def test_rollback_failure_marks_degraded(
    reload_harness: ReloadHarness,
) -> None:
    """When A rollback also raises, primary error is preserved."""
    trans_a = _InstrumentedTransition(
        "A",
        rollback_raises=RuntimeError("rollback failed"),
    )
    trans_b = _InstrumentedTransition(
        "B",
        apply_raises=RuntimeError("B failed"),
    )

    plan = MagicMock()
    plan.transitions = [trans_a, trans_b]

    result = TransitionApplyResult(plan)

    with pytest.raises(ProcessTransitionApplyError) as exc_info:
        await result.apply_all()

    assert exc_info.value.failed_transition_name == "B"

    outcome = await result.rollback_applied()
    assert len(outcome.failures) == 1
    assert outcome.failures[0][0] == "A"
    assert "rollback failed" in str(outcome.failures[0][1])
    assert outcome.restored == ()


# ---------------------------------------------------------------------------
# Tests: candidate resource close exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_candidate_resource_close_exactly_once(
    reload_harness: ReloadHarness,
) -> None:
    """Instrumented candidate with close counter, assert close() called exactly once."""
    candidate = RuntimeGenerationCandidate(generation_id=700)
    close_count = 0

    async def _close() -> None:
        nonlocal close_count
        close_count += 1

    candidate.register_resource("resource_a", _close)
    candidate.register_resource("resource_b", _close)
    candidate.mark_prepared()

    await candidate.abort(
        cause=RuntimeError("test abort"),
        failure_stage="test",
    )

    assert close_count == 2, f"expected 2 closes, got {close_count}"
    assert candidate.ownership_state.value == "aborted"

    # Abort is idempotent — second call returns cached diagnostics.
    await candidate.abort(
        cause=RuntimeError("test abort"),
        failure_stage="test",
    )
    assert close_count == 2, "second abort should not close again"


@pytest.mark.asyncio()
async def test_no_candidate_close_after_acceptance(
    reload_harness: ReloadHarness,
) -> None:
    """After acceptance (transfer), candidate resources are NOT closed."""
    candidate = RuntimeGenerationCandidate(generation_id=701)
    close_count = 0

    async def _close() -> None:
        nonlocal close_count
        close_count += 1

    candidate.register_resource("resource_a", _close)
    candidate.mark_prepared()
    candidate.transfer_to_runtime_manager()

    # Abort after transfer is a no-op.
    await candidate.abort(
        cause=RuntimeError("test"),
        failure_stage="test",
    )
    assert close_count == 0, f"expected 0 closes after transfer, got {close_count}"


@pytest.mark.asyncio()
async def test_active_generation_resources_not_closed(
    reload_harness: ReloadHarness,
) -> None:
    """Old generation resources are not closed during preacceptance failure."""
    rm = reload_harness.runtime_manager

    # Capture the active generation identity before abort.
    pre_gen_id = rm.active_snapshot().generation_id

    # Build and abort a candidate — this must not affect the active gen.
    candidate = RuntimeGenerationCandidate(generation_id=702)
    candidate.register_resource("candidate_res", AsyncMock())
    candidate.mark_prepared()
    await candidate.abort(
        cause=RuntimeError("test"),
        failure_stage="test",
    )

    # Active generation must be unchanged.
    post_gen = rm.active_snapshot()
    assert post_gen.generation_id == pre_gen_id


@pytest.mark.asyncio()
async def test_cleanup_structured_outcome(
    reload_harness: ReloadHarness,
) -> None:
    """PrecommitAbortOutcome has correct fields after precommit abort.

    Uses on_publish_started fault which fires after the swap is staged,
    triggering the shared _abort_precommit_reload cleanup path.
    """
    from tests.support.reload_faults import (
        FaultType,
        ReloadFaultInjector,
    )

    injector = ReloadFaultInjector(
        target_stage="on_publish_started",
        fault_type=FaultType.RECOVERABLE,
        message="deliberate failure",
    )

    result = await reload_harness.reload(observer=injector)

    # The reload must have failed cleanly — active generation unchanged.
    rm = reload_harness.runtime_manager
    assert rm.is_accepting_leases()
    assert result.ok is False

    # Cleanup diagnostics must have been recorded.
    snap = reload_harness.reload_manager.snapshot()
    cleanup = snap.get("last_cleanup_diagnostics")
    assert cleanup is not None, "cleanup diagnostics should be recorded"
    # cleanup may be a dict or dataclass depending on serialization
    if isinstance(cleanup, dict):
        assert cleanup.get("primary_failure") == "deliberate failure"
    else:
        assert cleanup.primary_failure == "deliberate failure"
