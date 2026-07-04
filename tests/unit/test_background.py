"""Tests for background task supervision."""

from __future__ import annotations

import asyncio
import time

import pytest

from eggpool.background import SupervisedTask, TaskSupervisor


@pytest.mark.asyncio
async def test_unexpected_completion_does_not_count_as_restart() -> None:
    """Periodic tasks that return normally must not be killed by the
    restart-budget. The supervisor must keep cycling even after more
    than ``_max_restarts`` successful iterations, and ``_last_failure``
    must remain at 0 because there was no failure."""
    calls = 0
    stop_after = 5

    async def completes() -> None:
        nonlocal calls
        calls += 1
        if calls >= stop_after:
            raise asyncio.CancelledError()

    task = SupervisedTask(
        name="completes",
        _coro_factory=completes,
        _max_restarts=3,
        _base_delay=0,
    )
    await task.start()
    assert task._task is not None
    await task._task

    assert calls == stop_after
    assert task._restart_count == 0
    assert task._last_failure == 0.0


@pytest.mark.asyncio
async def test_exhausted_task_can_be_started_again() -> None:
    calls = 0

    async def fails() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    task = SupervisedTask(
        name="fails",
        _coro_factory=fails,
        _max_restarts=1,
        _base_delay=0,
    )
    await task.start()
    assert task._task is not None
    await task._task
    await task.start()
    assert task._task is not None
    await task._task

    assert calls == 2


@pytest.mark.asyncio
async def test_stop_clears_task_reference() -> None:
    async def waits() -> None:
        await asyncio.Event().wait()

    task = SupervisedTask(name="waits", _coro_factory=waits)
    await task.start()
    await task.stop()

    assert task._task is None
    assert task.is_running is False


def test_supervisor_rejects_duplicate_task_names() -> None:
    async def waits() -> None:
        await asyncio.Event().wait()

    supervisor = TaskSupervisor()
    supervisor.register("duplicate", waits)

    with pytest.raises(ValueError, match="already registered"):
        supervisor.register("duplicate", waits)


# ---------------------------------------------------------------------------
# Periodic scheduling API (background-task-overdue-remediation plan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_periodic_runs_tick_and_updates_heartbeat() -> None:
    """A periodic task runs its tick on the supervisor's cadence and
    exposes fresh ``last_tick_*`` and ``next_run_at`` fields so the
    runtime dashboard can render a coherent countdown rather than
    inferring from outer-coroutine ``last_started_at``."""

    tick_count = 0

    async def tick() -> None:
        nonlocal tick_count
        tick_count += 1
        await asyncio.sleep(0)

    supervisor = TaskSupervisor()
    task = supervisor.register_periodic(
        "periodic_heartbeat",
        tick,
        interval_s=0.05,
    )

    assert task.mode == "periodic"
    assert task._interval_s == pytest.approx(0.05)

    await supervisor.start_all()
    # Let the loop fire several ticks.
    for _ in range(40):
        if tick_count >= 2:
            break
        await asyncio.sleep(0.02)

    snap = task.snapshot()
    await supervisor.stop_all()

    assert tick_count >= 2
    assert snap["mode"] == "periodic"
    assert snap["last_tick_started_at"] is not None
    assert snap["last_tick_completed_at"] is not None
    assert snap["next_run_at"] is not None
    assert snap["success_count"] >= 1
    assert snap["iteration_count"] >= 1
    # ``last_started_at`` reflects the outer-coroutine lifecycle and
    # must NOT be used to project the next-run window.
    assert snap["overdue_seconds"] in (None, 0.0)
    # Outer lifecycle timestamp is preserved for backward compatibility.
    assert snap["last_started_at"] is not None


@pytest.mark.asyncio
async def test_periodic_task_failure_records_error_and_continues() -> None:
    """One tick failure does not stop the supervisor; subsequent ticks
    succeed and reset the consecutive-failure counter."""
    tick_count = 0
    error_class_seen: list[str | None] = []

    async def tick() -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count == 1:
            raise RuntimeError("first-tick boom")
        await asyncio.sleep(0)

    supervisor = TaskSupervisor()
    task = supervisor.register_periodic(
        "periodic_failure",
        tick,
        interval_s=0.05,
    )

    await supervisor.start_all()
    # Wait for both ticks (success_count >= 1 and failure_count >= 1).
    for _ in range(80):
        snap = task.snapshot()
        if snap["success_count"] >= 1 and snap["failure_count"] >= 1:
            break
        if snap["last_error_class"] and not error_class_seen:
            error_class_seen.append(snap["last_error_class"])
        await asyncio.sleep(0.02)
    snap = task.snapshot()
    await supervisor.stop_all()

    assert snap["failure_count"] >= 1
    assert snap["success_count"] >= 1
    # ``last_error_class`` is the type's qualname; the supervisor
    # records ``RuntimeError`` here.
    assert snap["last_error_class"] == "RuntimeError"
    assert snap["consecutive_failure_count"] == 0  # reset by success
    assert task.is_running is False  # stopped cleanly


def test_daemon_task_does_not_project_next_run() -> None:
    """Legacy ``register()`` keeps daemon semantics: no ``next_run_at``,
    no ``overdue_seconds`` even when ``interval_s`` is provided."""

    async def tick() -> None:
        return None

    supervisor = TaskSupervisor()
    task = supervisor.register(
        "daemon_legacy",
        tick,
        interval_s=60.0,
    )

    snap = task.snapshot()

    assert snap["mode"] == "daemon"
    assert snap["next_run_at"] is None
    assert snap["overdue_seconds"] is None


def test_periodic_overdue_only_when_deadline_exceeded() -> None:
    """Supervisor snapshot computes ``overdue_seconds`` from the
    deadline + grace band; a healthy sleeping task reports zero, a
    forced-stale deadline reports a positive age."""
    supervisor = TaskSupervisor()

    async def tick() -> None:
        return None

    healthy = supervisor.register_periodic(
        "healthy",
        tick,
        interval_s=60.0,
    )
    assert healthy.snapshot()["overdue_seconds"] in (None, 0.0)

    # Prime the deadline into the past so overdue_seconds is positive.
    healthy._next_run_at = time.time() - 600  # pyright: ignore[reportPrivateUsage]
    overdue_age = healthy.snapshot()["overdue_seconds"]
    assert overdue_age is not None and overdue_age > 0


def test_register_periodic_rejects_zero_or_negative_interval() -> None:
    supervisor = TaskSupervisor()

    async def tick() -> None:
        return None

    with pytest.raises(ValueError, match="interval_s > 0"):
        supervisor.register_periodic("zero", tick, interval_s=0)
    with pytest.raises(ValueError, match="interval_s > 0"):
        supervisor.register_periodic("neg", tick, interval_s=-1.0)


def test_register_periodic_rejects_duplicate_name() -> None:
    supervisor = TaskSupervisor()

    async def tick() -> None:
        return None

    supervisor.register_periodic("dup", tick, interval_s=10.0)
    with pytest.raises(ValueError, match="already registered"):
        supervisor.register_periodic("dup", tick, interval_s=20.0)


@pytest.mark.asyncio
async def test_supervisor_snapshot_exposes_periodic_fields() -> None:
    """The supervisor-level snapshot includes ``mode`` and the
    supervisor-owned periodic fields for every registered task."""

    async def tick() -> None:
        return None

    supervisor = TaskSupervisor()
    supervisor.register("daemon", tick)
    supervisor.register_periodic("periodic", tick, interval_s=30.0)

    snaps = supervisor.snapshot()
    by_name = {s["name"]: s for s in snaps}

    assert by_name["daemon"]["mode"] == "daemon"
    assert by_name["daemon"]["next_run_at"] is None

    assert by_name["periodic"]["mode"] == "periodic"
    assert by_name["periodic"]["interval_s"] == 30.0
    assert by_name["periodic"]["next_run_at"] is not None
