"""Tests for background task supervision."""

from __future__ import annotations

import asyncio

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
