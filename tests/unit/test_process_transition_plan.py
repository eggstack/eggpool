"""ProcessTransitionPlan regression tests.

Verifies that writer/guard/effective transitions apply even when task
specs are empty and the process supervisor is absent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.reload_transaction import (
    EffectiveStateTransition,
    ProcessTransitionPlan,
    RoutingTraceGuardTransition,
    RoutingTraceWriterTransition,
    TransitionApplyResult,
    TransitionRollbackOutcome,
)


def _make_writer_transition(*, writer: Any = None) -> RoutingTraceWriterTransition:
    return RoutingTraceWriterTransition(
        writer=writer,
        mode="live",
        sample_rate=0.5,
    )


def _make_guard_transition(*, guard: Any = None) -> RoutingTraceGuardTransition:
    return RoutingTraceGuardTransition(
        guard=guard,
        threshold_ms=100.0,
        queue_occupancy_threshold=0.8,
        oldest_event_age_s=5.0,
        cooldown_s=2.0,
    )


def _make_effective_transition(*, app_state: Any = None) -> EffectiveStateTransition:
    return EffectiveStateTransition(
        app_state=app_state,
        config=MagicMock(),
        config_digest="abc123",
        generation_id=1,
    )


@pytest.mark.asyncio()
async def test_empty_task_specs_plan_applies_writer_and_guard_transitions() -> None:
    """Transitions apply even when task_specs is empty and supervisor is absent."""
    plan = ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=(
            _make_writer_transition(),
            _make_guard_transition(),
        ),
    )

    result = TransitionApplyResult(_plan=plan)
    await result.apply_all()

    assert result.is_fully_applied
    assert result.applied_count == 2


@pytest.mark.asyncio()
async def test_empty_task_specs_plan_with_effective_transition() -> None:
    """EffectiveStateTransition applies with empty task specs."""
    app_state = MagicMock()
    app_state.config = None
    app_state.config_digest = ""
    app_state.generation_id = 0

    plan = ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=(_make_effective_transition(app_state=app_state),),
    )

    result = TransitionApplyResult(_plan=plan)
    await result.apply_all()

    assert result.is_fully_applied
    assert result.applied_count == 1
    assert app_state.config_digest == "abc123"
    assert app_state.generation_id == 1


@pytest.mark.asyncio()
async def test_rollback_restores_effective_state_on_missing_attributes() -> None:
    """EffectiveStateTransition rollback handles attributes that were absent."""
    app_state = MagicMock(spec=[])
    plan = ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=(_make_effective_transition(app_state=app_state),),
    )

    result = TransitionApplyResult(_plan=plan)
    await result.apply_all()
    assert result.is_fully_applied

    assert hasattr(app_state, "config")
    assert app_state.config_digest == "abc123"

    outcome = await result.rollback_applied()
    assert isinstance(outcome, TransitionRollbackOutcome)
    assert outcome.attempted == ("effective_state",)
    assert outcome.restored == ("effective_state",)
    assert outcome.failures == ()


@pytest.mark.asyncio()
async def test_writer_transition_preflight_apply_rollback_finalize() -> None:
    """RoutingTraceWriterTransition lifecycle methods work correctly."""
    writer = MagicMock()
    writer._mode = "off"
    writer._sample_rate = 0.0
    writer.sample_rate = 0.0

    transition = RoutingTraceWriterTransition(
        writer=writer,
        mode="live",
        sample_rate=0.5,
    )

    await transition.preflight()
    assert transition._old_mode == "off"
    assert transition._old_sample_rate == 0.0

    await transition.apply()
    writer.configure.assert_called_once_with(mode="live", sample_rate=0.5)
    assert transition._applied is True

    await transition.rollback()
    writer.configure.assert_called_with(mode="off", sample_rate=0.0)
    assert transition._applied is False

    await transition.finalize()


@pytest.mark.asyncio()
async def test_guard_transition_preflight_apply_rollback() -> None:
    """RoutingTraceGuardTransition lifecycle methods work correctly."""
    guard = MagicMock()
    guard._threshold_ms = 50.0
    guard._queue_occupancy_threshold = 0.5
    guard._oldest_event_age_s = 2.0
    guard._cooldown_s = 1.0

    transition = RoutingTraceGuardTransition(
        guard=guard,
        threshold_ms=100.0,
        queue_occupancy_threshold=0.8,
        oldest_event_age_s=5.0,
        cooldown_s=2.0,
    )

    await transition.preflight()
    assert transition._old_settings is not None
    assert transition._old_settings["threshold_ms"] == 50.0

    await transition.apply()
    guard.configure.assert_called_once_with(
        threshold_ms=100.0,
        queue_occupancy_threshold=0.8,
        oldest_event_age_s=5.0,
        cooldown_s=2.0,
    )
    assert transition._applied is True

    await transition.rollback()
    guard.configure.assert_called_with(
        threshold_ms=50.0,
        queue_occupancy_threshold=0.5,
        oldest_event_age_s=2.0,
        cooldown_s=1.0,
    )
    assert transition._applied is False


@pytest.mark.asyncio()
async def test_transition_apply_result_finalize_all_is_idempotent() -> None:
    """finalize_all() can be called multiple times safely."""
    plan = ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=(
            _make_writer_transition(),
            _make_guard_transition(),
        ),
    )

    result = TransitionApplyResult(_plan=plan)
    await result.apply_all()

    await result.finalize_all()
    await result.finalize_all()
    assert result._finalized is True


@pytest.mark.asyncio()
async def test_transition_apply_result_rollback_applied_in_reverse_order() -> None:
    """rollback_applied() undoes transitions in reverse application order."""
    call_order: list[str] = []

    t1 = MagicMock()
    t1.name = "first"
    t1.rollback = AsyncMock(side_effect=lambda: call_order.append("first"))
    t2 = MagicMock()
    t2.name = "second"
    t2.rollback = AsyncMock(side_effect=lambda: call_order.append("second"))
    t3 = MagicMock()
    t3.name = "third"
    t3.rollback = AsyncMock(side_effect=lambda: call_order.append("third"))

    plan = ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=(t1, t2, t3),
    )

    result = TransitionApplyResult(_plan=plan)
    result._applied = [t1, t2, t3]

    outcome = await result.rollback_applied()
    assert isinstance(outcome, TransitionRollbackOutcome)
    assert outcome.attempted == ("third", "second", "first")
    assert outcome.restored == ("third", "second", "first")
    assert outcome.failures == ()
    assert call_order == ["third", "second", "first"]


@pytest.mark.asyncio()
async def test_transition_apply_result_rollback_collects_errors() -> None:
    """rollback_applied() collects errors without masking other rollbacks."""
    t1 = MagicMock()
    t1.name = "ok_transition"
    t1.rollback = AsyncMock()

    t2 = MagicMock()
    t2.name = "failing_transition"
    t2.rollback = AsyncMock(side_effect=RuntimeError("rollback failed"))

    plan = ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=(t1, t2),
    )

    result = TransitionApplyResult(_plan=plan)
    result._applied = [t1, t2]

    outcome = await result.rollback_applied()
    assert isinstance(outcome, TransitionRollbackOutcome)
    assert len(outcome.failures) == 1
    assert outcome.failures[0][0] == "failing_transition"
    assert "rollback failed" in str(outcome.failures[0][1])
    assert outcome.restored == ("ok_transition",)
    assert outcome.attempted == ("failing_transition", "ok_transition")

    t1.rollback.assert_awaited_once()


@pytest.mark.asyncio()
async def test_no_transitions_plan_applies_zero() -> None:
    """A plan with no transitions applies successfully with count zero."""
    plan = ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=(),
    )

    result = TransitionApplyResult(_plan=plan)
    await result.apply_all()

    assert result.applied_count == 0
    assert result.is_fully_applied
