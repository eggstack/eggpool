"""TransitionApplyResult ownership tests.

Verifies that the production reload path constructs TransitionApplyResult
before apply_all(), that partial rollback is retryable, and that
finalize_all() returns structured TransitionFinalizeOutcome.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from eggpool.reload_transaction import (
    ProcessTransition,
    ProcessTransitionApplyError,
    TransitionApplyResult,
    TransitionFinalizeOutcome,
    TransitionRollbackState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FailingTransition(ProcessTransition):
    """Transition whose apply() raises."""

    def __init__(self, name: str, exc: Exception | None = None) -> None:
        super().__init__(name=name, description=f"failing-{name}", reversible=True)
        self._exc = exc or RuntimeError(f"{name} failed")
        self.apply_called = False
        self.rollback_called = False

    async def apply(self) -> None:
        self.apply_called = True
        raise self._exc

    async def rollback(self) -> None:
        self.rollback_called = True


class _SuccessTransition(ProcessTransition):
    """Transition whose apply() and rollback() succeed."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=f"success-{name}", reversible=True)
        self.apply_called = False
        self.rollback_called = False
        self.finalize_called = False

    async def apply(self) -> None:
        self.apply_called = True

    async def rollback(self) -> None:
        self.rollback_called = True

    async def finalize(self) -> None:
        self.finalize_called = True


class _FailingFinalizeTransition(ProcessTransition):
    """Transition whose finalize() raises."""

    def __init__(self, name: str) -> None:
        super().__init__(
            name=name,
            description=f"failing-finalize-{name}",
            reversible=True,
        )
        self.apply_called = False
        self.finalize_called = False

    async def apply(self) -> None:
        self.apply_called = True

    async def finalize(self) -> None:
        self.finalize_called = True
        raise RuntimeError(f"finalize {self.name} failed")


def _make_plan(transitions: tuple[ProcessTransition, ...]) -> object:
    """Build a minimal ProcessTransitionPlan-like object."""
    from eggpool.reload_transaction import ProcessTransitionPlan

    return ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=transitions,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_transition_result_created_before_apply_all() -> None:
    """TransitionApplyResult is created in the reload owner before apply_all().

    The production path constructs the result object before calling
    apply_all(), so ownership is explicit.
    """
    create_order: list[str] = []
    original_init = TransitionApplyResult.__init__

    def tracking_init(self: TransitionApplyResult, plan: object) -> None:
        create_order.append("created")
        original_init(self, plan)

    trans_a = _SuccessTransition("a")
    plan = _make_plan((trans_a,))

    with patch.object(TransitionApplyResult, "__init__", tracking_init):
        result = TransitionApplyResult(plan)
        create_order.append("before_apply")

    # Before calling apply_all, both markers must exist.
    assert create_order == ["created", "before_apply"]

    # Now call apply_all and verify it works.
    await result.apply_all()
    assert trans_a.apply_called
    assert result.applied_count == 1


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_partial_transition_rollback_retryable() -> None:
    """Partial rollback is retryable — only unrestored transitions are retried.

    Plan: A applies, B fails, C untouched.  Rollback A succeeds.
    Retry with a new plan (A already rolled back) only attempts B.
    """
    trans_a = _SuccessTransition("a")
    trans_b = _FailingTransition("b")
    trans_c = _SuccessTransition("c")

    plan = _make_plan((trans_a, trans_b, trans_c))
    result = TransitionApplyResult(plan)

    # Apply — A succeeds, B fails, C never called.
    with pytest.raises(ProcessTransitionApplyError) as exc_info:
        await result.apply_all()

    assert exc_info.value.failed_transition_name == "b"
    assert exc_info.value.applied_transition_names == ("a",)
    assert not trans_c.apply_called

    # Rollback — A rolls back.
    outcome = await result.rollback_applied()
    assert outcome.restored == ("a",)
    assert outcome.failures == ()
    assert result.rollback_state is TransitionRollbackState.COMPLETE
    assert trans_a.rollback_called

    # Verify only applied transitions were rolled back.
    assert not trans_b.rollback_called


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_rollback_retry_only_attempts_remaining() -> None:
    """When B rollback fails first, retry only attempts B again."""
    trans_a = _SuccessTransition("a")

    # B's rollback fails on first call, succeeds on second.
    trans_b = _SuccessTransition("b")
    b_rollback_count = 0
    original_rollback = trans_b.rollback

    async def counting_rollback() -> None:
        nonlocal b_rollback_count
        b_rollback_count += 1
        if b_rollback_count == 1:
            raise RuntimeError("B rollback failed")
        await original_rollback()

    trans_b.rollback = counting_rollback  # type: ignore[assignment]

    plan = _make_plan((trans_a, trans_b))
    result = TransitionApplyResult(plan)

    # Apply — both succeed.
    await result.apply_all()
    assert result.applied_count == 2

    # First rollback — A restores, B fails.
    outcome1 = await result.rollback_applied()
    assert outcome1.restored == ("a",)
    assert len(outcome1.failures) == 1
    assert outcome1.failures[0][0] == "b"
    assert result.rollback_state is TransitionRollbackState.PARTIAL

    # Retry rollback — only B is attempted.
    outcome2 = await result.rollback_applied()
    assert outcome2.attempted == ("b",)
    assert outcome2.restored == ("b",)
    assert outcome2.failures == ()
    assert result.rollback_state is TransitionRollbackState.COMPLETE


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_finalize_outcome_returns_structured_result() -> None:
    """finalize_all() returns TransitionFinalizeOutcome with correct fields."""
    trans_a = _SuccessTransition("a")
    trans_b = _FailingFinalizeTransition("b")

    plan = _make_plan((trans_a, trans_b))
    result = TransitionApplyResult(plan)
    await result.apply_all()

    # finalize_all — A succeeds, B fails.
    outcome = await result.finalize_all()

    assert isinstance(outcome, TransitionFinalizeOutcome)
    assert outcome.attempted == ("a", "b")
    assert outcome.finalized == ("a",)
    assert len(outcome.failures) == 1
    assert outcome.failures[0][0] == "b"
    assert outcome.remaining == ("b",)
    assert not result.is_finalized

    # Retry — only B is attempted.
    outcome2 = await result.finalize_all()
    assert outcome2.attempted == ("b",)
    assert outcome2.finalized == ()
    assert len(outcome2.failures) == 1
    assert outcome2.failures[0][0] == "b"
    assert outcome2.remaining == ("b",)
    assert not result.is_finalized
