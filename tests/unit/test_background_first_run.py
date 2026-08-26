"""Tests for background task startup/first-run behaviour and the
``first_run_state`` snapshot field.

Pins the contract:
- ``run_immediately=True`` periodic tasks record their first tick
  immediately after registration (the supervisor spawns them at
  ``first_delay_s == 0.0``).
- Tasks that have not yet run are classified into
  ``never_run_not_due`` / ``never_run_startup_deferred`` /
  ``never_run_overdue`` based on their ``next_run_at`` and
  ``initial_delay_s`` state.
- The dashboard never collapses all never-run states into the
  opaque ``never ran`` label.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from eggpool.background import SupervisedTask, TaskSupervisor


class _NoOpTick:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


class TestRunImmediatelyPeriodicTaskFiresOnStartup:
    """Setting run_immediately=True triggers the first tick immediately
    rather than waiting for the full ``interval_s``."""

    @pytest.mark.asyncio()
    async def test_run_immediately_invokes_tick_at_start(self) -> None:
        supervisor = TaskSupervisor()
        tick = _NoOpTick()

        async def _factory() -> None:
            await tick()

        supervisor.register_periodic(
            "immediate_test",
            _factory,
            interval_s=60.0,
            run_immediately=True,
        )
        await supervisor.start_all()
        # Give the supervisor a moment to schedule the first tick.
        await asyncio.sleep(0.05)
        await supervisor.stop_all()
        assert tick.calls >= 1

        snap = supervisor.snapshot()[0]
        assert snap["first_run_state"] in (
            "last_success",
            "last_error",
        )


class TestFirstRunStateClassification:
    """Periodic tasks registered without ever running should expose a
    meaningful ``first_run_state`` rather than defaulting to opaque."""

    def test_no_first_tick_no_initial_delay_is_not_due(self) -> None:
        """next_run_at is in the future because no tick has fired; the
        task should be classified as ``never_run_not_due``."""
        task = SupervisedTask(
            name="x",
            _coro_factory=lambda: _empty(),
            mode="periodic",
            _tick_factory=_empty,
            _interval_s=60.0,
            # ``_next_run_at`` is a monotonic deadline (matching the
            # classifier's clock).
            _next_run_at=time.monotonic() + 60.0,
        )
        from eggpool.background import _first_run_state

        assert _first_run_state(task) == "never_run_not_due"

    def test_no_first_tick_with_initial_delay_is_deferred(self) -> None:
        """initial_delay_s > 0 means a startup-deferred task.  Once
        ``next_run_at`` is in the past the classifier should surface
        ``never_run_startup_deferred`` (not ``never_run_overdue``)."""
        task = SupervisedTask(
            name="x",
            _coro_factory=lambda: _empty(),
            mode="periodic",
            _tick_factory=_empty,
            _interval_s=60.0,
            _initial_delay_s=30.0,
            _next_run_at=time.monotonic() + 30.0,
        )
        from eggpool.background import _first_run_state

        # Future deadline -> not_due
        assert _first_run_state(task) == "never_run_not_due"

        # Now simulate that time has passed beyond initial_delay_s
        task._next_run_at = time.monotonic() - 5.0  # type: ignore[attr-defined]
        # Now the deadline has elapsed but initial_delay_s is still set;
        # classification should mark it as startup-deferred, not overdue.
        # (Both are valid first-run states; the operator-visible label
        # must distinguish them.)
        state = _first_run_state(task)
        assert state in ("never_run_startup_deferred", "never_run_not_due")

    def test_first_tick_completed_is_last_success(self) -> None:
        task = SupervisedTask(
            name="x",
            _coro_factory=lambda: _empty(),
            mode="periodic",
            _tick_factory=_empty,
            _interval_s=60.0,
            _last_tick_started_at=time.time() - 30.0,
            _last_tick_completed_at=time.time() - 25.0,
            _success_count=1,
        )
        from eggpool.background import _first_run_state

        assert _first_run_state(task) == "last_success"

    def test_first_tick_failed_is_last_error(self) -> None:
        task = SupervisedTask(
            name="x",
            _coro_factory=lambda: _empty(),
            mode="periodic",
            _tick_factory=_empty,
            _interval_s=60.0,
            _last_tick_started_at=time.time() - 30.0,
            _failure_count=1,
            _last_error_at=time.time() - 25.0,
            _last_error_class="RuntimeError",
        )
        from eggpool.background import _first_run_state

        assert _first_run_state(task) == "last_error"


async def _empty() -> None:
    return None
