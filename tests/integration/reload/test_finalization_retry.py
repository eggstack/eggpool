"""Finalization retry with transition failure.

Fail-once production test with two applied transitions.  A succeeds, B fails
on first finalize and succeeds on second.  Proves: (1) first run stops at
transition finalization, (2) observer and retirement not invoked, (3) job
unresolved, (4) retry calls only B, (5) job advances, (6) COMPLETED only
after B succeeds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.control.accepted_finalization import (
    AcceptedFinalizationHealth,
    AcceptedFinalizationStep,
    AcceptedReloadFinalizationJob,
    TransitionFinalizationPendingError,
)
from eggpool.reload_transaction import (
    ProcessTransition,
    TransitionApplyResult,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SuccessTransition(ProcessTransition):
    """Transition whose apply() and finalize() succeed."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=f"success-{name}", reversible=True)
        self.apply_called = False
        self.finalize_called = False

    async def apply(self) -> None:
        self.apply_called = True

    async def finalize(self) -> None:
        self.finalize_called = True


class _FailOnceFinalizeTransition(ProcessTransition):
    """Transition whose finalize() fails on first call, succeeds on second."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=f"fail-once-{name}", reversible=True)
        self.apply_called = False
        self.finalize_called = 0

    async def apply(self) -> None:
        self.apply_called = True

    async def finalize(self) -> None:
        self.finalize_called += 1
        if self.finalize_called == 1:
            raise RuntimeError(f"{self.name} finalize failed on first call")


def _make_plan(transitions: tuple[ProcessTransition, ...]) -> MagicMock:
    plan = MagicMock()
    plan.transitions = list(transitions)
    plan.task_specs = ()
    plan.callback_factories = {}
    return plan


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_fail_once_transition_blocks_advancement(
    reload_harness: ReloadHarness,
) -> None:
    """B3: First run stops at transition finalization when B fails.

    A finalizes successfully; B fails on first call.  Observer and
    retirement steps must NOT be invoked.
    """
    trans_a = _SuccessTransition("a")
    trans_b = _FailOnceFinalizeTransition("b")

    plan = _make_plan((trans_a, trans_b))
    transition_result = TransitionApplyResult(plan)
    await transition_result.apply_all()

    fake_txn = MagicMock()
    fake_txn.accepted_finalization = MagicMock()
    fake_txn.digest_prefix = "test"
    fake_gen = MagicMock()
    fake_gen.generation_id = 42

    observer = MagicMock()
    observer.on_publish_complete = AsyncMock()

    job = AcceptedReloadFinalizationJob(
        request_id="fail-once-test",
        generation_id=42,
        old_generation_id=41,
        transaction=fake_txn,
        candidate=MagicMock(),
        pending_swap=MagicMock(),
        transition_result=transition_result,
        published_generation=fake_gen,
        app=None,
        observer=observer,
        _step=AcceptedFinalizationStep.MIRROR_UPDATED,
    )

    # First run — B fails at transition finalization.
    result1 = await job.run()

    # Step should remain at MIRROR_UPDATED (transition finalization failed).
    assert result1.next_step == AcceptedFinalizationStep.MIRROR_UPDATED.value
    assert not job.is_complete
    assert job.health is AcceptedFinalizationHealth.RETRY_PENDING
    assert job.last_error_class == "TransitionFinalizationPendingError"

    # Observer must NOT have been called — retirement step was not reached.
    observer.on_publish_complete.assert_not_called()

    # A's finalize was called (it succeeded).
    assert trans_a.finalize_called == 1
    # B's finalize was called once (and failed).
    assert trans_b.finalize_called == 1


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_retry_advances_after_b_succeeds(
    reload_harness: ReloadHarness,
) -> None:
    """B3: Retry calls only B's finalize, then job advances to COMPLETED.

    After B's second finalize succeeds, the job should advance through
    observer, retirement, and completion.
    """
    trans_a = _SuccessTransition("a")
    trans_b = _FailOnceFinalizeTransition("b")

    plan = _make_plan((trans_a, trans_b))
    transition_result = TransitionApplyResult(plan)
    await transition_result.apply_all()

    fake_txn = MagicMock()
    fake_txn.accepted_finalization = MagicMock()
    fake_txn.digest_prefix = "test"
    fake_gen = MagicMock()
    fake_gen.generation_id = 42

    observer = MagicMock()
    observer.on_publish_complete = AsyncMock()

    pending_swap = MagicMock()
    pending_swap.finalize_retirement = AsyncMock()

    job = AcceptedReloadFinalizationJob(
        request_id="retry-advance-test",
        generation_id=42,
        old_generation_id=41,
        transaction=fake_txn,
        candidate=MagicMock(),
        pending_swap=pending_swap,
        transition_result=transition_result,
        published_generation=fake_gen,
        app=None,
        observer=observer,
        _step=AcceptedFinalizationStep.MIRROR_UPDATED,
    )

    # First run — B fails.
    await job.run()
    assert not job.is_complete

    # Second run — B succeeds on retry, job completes.
    result2 = await job.run()
    assert result2.completed
    assert job.is_complete
    assert job.health is AcceptedFinalizationHealth.COMPLETED

    # Observer was called (after transition finalization succeeded).
    observer.on_publish_complete.assert_called_once()

    # Retirement was called.
    pending_swap.finalize_retirement.assert_called_once()

    # A's finalize was called only once (during first run).
    assert trans_a.finalize_called == 1
    # B's finalize was called twice (first fail, then success).
    assert trans_b.finalize_called == 2


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_transition_finalization_pending_error_fields() -> None:
    """TransitionFinalizationPendingError carries correct diagnostic fields."""
    exc = TransitionFinalizationPendingError(
        "Transitions still pending: ('b',)",
        attempted=("a", "b"),
        finalized=("a",),
        failures=[("b", RuntimeError("b failed"))],
        remaining=("b",),
    )
    assert exc.attempted == ("a", "b")
    assert exc.finalized == ("a",)
    assert len(exc.failures) == 1
    assert exc.failures[0][0] == "b"
    assert exc.remaining == ("b",)
