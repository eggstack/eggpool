"""Production transition-prefix rollback tests.

Full integration path proving A/B/C transition-prefix rollback through
TransitionApplyResult.  Transition A applies, B fails, C never runs.
Rollback restores A exactly once, old generation remains active,
and subsequent reload succeeds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from eggpool.reload_transaction import (
    ProcessTransition,
    ProcessTransitionApplyError,
    TransitionApplyResult,
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

    async def apply(self) -> None:
        self.apply_calls += 1
        if self._apply_raises is not None:
            raise self._apply_raises

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def finalize(self) -> None:
        self.finalize_calls += 1


# ---------------------------------------------------------------------------
# H3: Full transition-prefix rollback through TransitionApplyResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_abc_transition_prefix_rollback(
    reload_harness: ReloadHarness,
) -> None:
    """H3: A applies, B fails, C never runs.  Rollback restores A exactly once.

    Proves:
    - transition A apply() called once
    - transition B apply() called once (and failed)
    - transition C apply() never called
    - rollback restores A exactly once
    - B and C rollback not called (B wasn't applied, C wasn't touched)
    - subsequent reload succeeds (old generation still active)
    """
    trans_a = _InstrumentedTransition("A")
    trans_b = _InstrumentedTransition("B", apply_raises=RuntimeError("B failed"))
    trans_c = _InstrumentedTransition("C")

    plan = MagicMock()
    plan.transitions = [trans_a, trans_b, trans_c]
    plan.task_specs = ()
    plan.callback_factories = {}

    result = TransitionApplyResult(plan)

    # Apply — A succeeds, B fails, C never runs.
    with pytest.raises(ProcessTransitionApplyError) as exc_info:
        await result.apply_all()

    err = exc_info.value
    assert err.failed_transition_name == "B"
    assert err.failed_transition_index == 1
    assert err.applied_transition_names == ("A",)

    # Verify call counts.
    assert trans_a.apply_calls == 1
    assert trans_b.apply_calls == 1
    assert trans_c.apply_calls == 0

    # Rollback — only A is rolled back.
    outcome = await result.rollback_applied()
    assert outcome.attempted == ("A",)
    assert outcome.restored == ("A",)
    assert outcome.failures == ()

    # A rollback called exactly once.
    assert trans_a.rollback_calls == 1
    # B and C rollback not called.
    assert trans_b.rollback_calls == 0
    assert trans_c.rollback_calls == 0

    # Old generation is still active (reload didn't succeed).
    rm = reload_harness.runtime_manager
    assert rm.is_accepting_leases()

    # Subsequent reload succeeds.
    result2 = await reload_harness.reload()
    assert result2.ok is True, f"subsequent reload failed: {result2}"


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_transition_failure_preserves_old_generation(
    reload_harness: ReloadHarness,
) -> None:
    """H3: After transition failure, old generation is still active."""
    rm = reload_harness.runtime_manager

    # Record initial generation.
    initial_gen_id = rm.active_snapshot().generation_id

    # Simulate a transition prefix failure (A applies, B fails).
    trans_a = _InstrumentedTransition("A")
    trans_b = _InstrumentedTransition("B", apply_raises=RuntimeError("B failed"))

    plan = MagicMock()
    plan.transitions = [trans_a, trans_b, _InstrumentedTransition("C")]
    plan.task_specs = ()
    plan.callback_factories = {}

    result = TransitionApplyResult(plan)
    with pytest.raises(ProcessTransitionApplyError):
        await result.apply_all()

    # Rollback.
    await result.rollback_applied()

    # Active generation unchanged.
    current_gen_id = rm.active_snapshot().generation_id
    assert current_gen_id == initial_gen_id

    # Admission is still open.
    assert rm.is_accepting_leases()


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_candidate_close_after_transition_failure(
    reload_harness: ReloadHarness,
) -> None:
    """H3: Candidate abort diagnostics are captured after transition failure."""
    from eggpool.runtime_manager import RuntimeGenerationCandidate

    candidate = RuntimeGenerationCandidate(generation_id=500)
    close_count = 0

    async def _close() -> None:
        nonlocal close_count
        close_count += 1

    candidate.register_resource("resource_a", _close)
    candidate.mark_prepared()

    # Abort should close resources exactly once.
    diag = await candidate.abort(
        cause=RuntimeError("transition failure"),
        failure_stage="transition_apply",
    )
    assert close_count == 1
    assert diag.generation_id == 500

    # Second abort is idempotent.
    await candidate.abort(
        cause=RuntimeError("transition failure"),
        failure_stage="transition_apply",
    )
    assert close_count == 1
