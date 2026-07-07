"""Background task supervisor with restart, backoff, and periodic scheduling."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

TaskMode = Literal["daemon", "periodic"]


def periodic_initial_offset(
    name: str, interval_s: float, *, max_fraction: float = 0.5
) -> float:
    """Return a deterministic initial delay for a periodic task.

    The offset is in ``[0, interval_s * max_fraction]`` and is derived
    from a stable hash of *name* so tests remain deterministic.  Use
    this to stagger short-cadence tasks that would otherwise wake
    together and briefly contend for the SQLite lock on low-power
    devices.
    """
    digest = hashlib.sha256(name.encode()).digest()
    # Use the first 8 bytes as a uint64, normalize to [0, 1).
    bucket = int.from_bytes(digest[:8], "big") / (2**64)
    return bucket * interval_s * max_fraction


def _compute_overdue_seconds(
    *, now: float, next_run_at: float | None, interval_s: float | None
) -> float | None:
    """Return overdue age in seconds, or ``None`` when not overdue.

    A 25%-of-interval (capped at 60s, minimum 5s) grace band suppresses
    flicker for tasks that wake a few hundred ms past their deadline
    because of the scheduler's poll granularity.
    """
    if next_run_at is None:
        return None
    delta_s = now - float(next_run_at)
    if delta_s <= 0:
        return 0.0
    if interval_s is None or interval_s <= 0:
        grace_s = 5.0
    else:
        grace_s = max(5.0, min(float(interval_s) * 0.25, 60.0))
    if delta_s <= grace_s:
        return 0.0
    return float(delta_s)


@dataclass
class SupervisedTask:
    """A supervised background task.

    Two modes are supported:

    - ``daemon``: a long-running coroutine supervised by the supervisor.
      ``last_started_at`` reflects outer-coroutine lifecycle; ``next_run``
      is ``None`` because daemons don't project a periodic next run.
    - ``periodic``: the supervisor owns the ``while running`` outer loop
      and delegates the per-tick work to ``tick_factory``.  The task
      records explicit per-tick heartbeat fields so the dashboard can
      distinguish running ticks from a healthy sleeping schedule.
    """

    name: str
    _coro_factory: Callable[[], Coroutine[Any, Any, None]]
    mode: TaskMode = "daemon"
    _tick_factory: Callable[[], Coroutine[Any, Any, None]] | None = field(
        default=None, repr=False
    )
    _interval_s: float | None = None
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _restart_count: int = 0
    _max_restarts: int = 10
    _base_delay: float = 1.0
    _max_delay: float = 300.0
    _last_failure: float = 0.0
    _running: bool = False
    # Outer-coroutine lifecycle (kept for backward compatibility on daemon tasks).
    _last_started_at: float = 0.0
    _last_completed_at: float = 0.0
    _last_error_at: float = 0.0
    _last_error_class: str | None = None
    # Periodic heartbeat fields (only populated when ``mode == "periodic"``).
    _last_tick_started_at: float = 0.0
    _last_tick_completed_at: float = 0.0
    _next_run_at: float = 0.0
    _tick_in_progress: bool = False
    _iteration_count: int = 0
    _success_count: int = 0
    _failure_count: int = 0
    _consecutive_failure_count: int = 0
    _initial_delay_s: float | None = None
    _last_tick_duration_ms: float | None = None
    _timeout_s: float | None = None

    async def start(self) -> None:
        """Start the supervised task."""
        if self._running:
            return
        self._restart_count = 0
        self._running = True
        if self.mode == "periodic" and self._tick_factory is not None:
            runner = self._run_periodic_loop
        else:
            runner = self._run_daemon_loop
        self._task = asyncio.create_task(runner(), name=f"eggpool:{self.name}")
        logger.info("Started supervised task %r (mode=%s)", self.name, self.mode)

    async def stop(self) -> None:
        """Stop the supervised task."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("Stopped supervised task %r", self.name)

    async def _run_daemon_loop(self) -> None:
        """Run a daemon-style long-lived coroutine with restart on failure.

        Failure path uses exponential backoff bounded by ``_max_restarts``.
        """
        try:
            while self._running:
                self._last_started_at = time.time()
                try:
                    await self._coro_factory()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self._last_error_at = time.time()
                    self._last_error_class = type(exc).__qualname__
                    self._restart_count += 1
                    self._last_failure = time.time()
                    logger.exception("Supervised task %r failed", self.name)
                    if self._restart_count >= self._max_restarts:
                        logger.error(
                            "Supervised task %r exceeded max restarts, giving up",
                            self.name,
                        )
                        break

                    delay = min(
                        self._base_delay * (2 ** (self._restart_count - 1)),
                        self._max_delay,
                    )
                    logger.info(
                        "Restarting task %r in %.1fs (restart %d/%d)",
                        self.name,
                        delay,
                        self._restart_count,
                        self._max_restarts,
                    )
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        break
                else:
                    self._last_completed_at = time.time()
                    self._iteration_count += 1
                    if not self._running:
                        break
                    logger.warning(
                        "Supervised task %r completed unexpectedly",
                        self.name,
                    )
                    if self._interval_s is not None and self._interval_s > 0:
                        try:
                            await asyncio.sleep(self._interval_s)
                        except asyncio.CancelledError:
                            break
                    continue
        finally:
            self._running = False

    async def _run_periodic_loop(self) -> None:
        """Run the periodic scheduler loop around a one-shot tick coroutine.

        The supervisor drives the outer ``while self._running`` cadence:
        compute the next run timestamp, sleep until then, record
        ``last_tick_started_at``, invoke the tick, then record
        completion (or tick-level failure).  ``tick_factory`` is the
        one-shot coroutine factory; the supervisor owns the wait loop
        so dashboards stop confusing outer-coroutine startup time
        with tick completion time.

        When ``_initial_delay_s`` is set, the first sleep uses that
        value instead of ``_interval_s`` so that startup-delayed
        periodic tasks (e.g. automatic backups) honour their
        configured delay before the first tick fires.
        """
        assert self._tick_factory is not None
        interval_s = float(self._interval_s) if self._interval_s else 0.0
        first_sleep = (
            float(self._initial_delay_s)
            if self._initial_delay_s is not None
            else interval_s
        )
        self._last_started_at = time.time()
        try:
            while self._running:
                try:
                    await asyncio.sleep(first_sleep)
                except asyncio.CancelledError:
                    break
                first_sleep = interval_s  # subsequent sleeps use the regular interval
                self._tick_in_progress = True
                tick_started = time.time()
                self._last_tick_started_at = tick_started
                tick_duration_ms: float | None = None
                try:
                    tick_coro = self._tick_factory()
                    if self._timeout_s is not None:
                        await asyncio.wait_for(tick_coro, timeout=self._timeout_s)
                    else:
                        await tick_coro
                except asyncio.CancelledError:
                    self._tick_in_progress = False
                    break
                except Exception as exc:  # noqa: BLE001
                    tick_completed = time.time()
                    tick_duration_ms = (tick_completed - tick_started) * 1000
                    self._last_tick_completed_at = tick_completed
                    self._last_tick_duration_ms = tick_duration_ms
                    self._tick_in_progress = False
                    self._iteration_count += 1
                    self._failure_count += 1
                    self._consecutive_failure_count += 1
                    self._restart_count += 1
                    self._last_error_at = time.time()
                    self._last_error_class = type(exc).__qualname__
                    self._last_failure = time.time()
                    self._next_run_at = tick_completed + interval_s
                    logger.exception(
                        "Supervised periodic task %r tick failed",
                        self.name,
                    )
                    if self._consecutive_failure_count >= self._max_restarts:
                        logger.error(
                            "Supervised periodic task %r exceeded max restarts, "
                            "giving up",
                            self.name,
                        )
                        break
                else:
                    tick_completed = time.time()
                    tick_duration_ms = (tick_completed - tick_started) * 1000
                    self._last_tick_completed_at = tick_completed
                    self._last_tick_duration_ms = tick_duration_ms
                    self._tick_in_progress = False
                    self._iteration_count += 1
                    self._success_count += 1
                    self._consecutive_failure_count = 0
                    self._next_run_at = tick_completed + interval_s
        finally:
            self._tick_in_progress = False
            self._running = False

    @property
    def is_running(self) -> bool:
        """Check if the task is currently running."""
        return self._running and self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        """Return the stable runtime-metrics payload for this task."""
        next_run_at: float | None = None
        overdue_seconds: float | None = None
        if self.mode == "periodic" and self._next_run_at > 0:
            next_run_at = self._next_run_at
            if self._tick_in_progress:
                overdue_seconds = None
            else:
                overdue_seconds = _compute_overdue_seconds(
                    now=time.time(),
                    next_run_at=next_run_at,
                    interval_s=self._interval_s,
                )
        return {
            "name": self.name,
            "registered": True,
            "mode": self.mode,
            "running": self.is_running,
            "done": self._task is not None and self._task.done(),
            "cancelled": self._task is not None and self._task.cancelled(),
            "iteration_count": self._iteration_count,
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "consecutive_failure_count": self._consecutive_failure_count,
            "restart_count": self._restart_count,
            "max_restarts": self._max_restarts,
            "interval_s": self._interval_s,
            "last_started_at": self._last_started_at or None,
            "last_completed_at": self._last_completed_at or None,
            "last_failure_at": self._last_failure or None,
            "last_tick_started_at": self._last_tick_started_at or None,
            "last_tick_completed_at": self._last_tick_completed_at or None,
            "last_tick_duration_ms": self._last_tick_duration_ms,
            "next_run_at": next_run_at,
            "overdue_seconds": overdue_seconds,
            "last_error_at": self._last_error_at or None,
            "last_error_class": self._last_error_class,
        }


class TaskSupervisor:
    """Manages multiple supervised background tasks.

    Tasks can be registered in two flavors:

    - :meth:`register` for true daemon-style long-lived coroutines.
    - :meth:`register_periodic` for supervisor-owned periodic scheduling
      around a one-shot tick factory.  Heartbeat fields
      (``last_tick_started_at``, ``last_tick_completed_at``,
      ``next_run_at``, ``overdue_seconds``) are populated so the
      runtime dashboard can distinguish a healthy sleeping task from
      an overdue one without inferring from outer-coroutine lifecycle
      timestamps.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, SupervisedTask] = {}

    def register(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, None]],
        max_restarts: int = 10,
        interval_s: float | None = None,
    ) -> SupervisedTask:
        """Register a daemon-style supervised task.

        ``interval_s`` is the wall-clock cadence between successive
        iterations of *coro_factory* (i.e. the ``asyncio.sleep`` the
        loop awaits between runs). It is exposed in the runtime-metrics
        snapshot for legacy callers and is not used to project a
        next-run timestamp because daemon tasks don't follow a fixed
        schedule.
        """
        if name in self._tasks:
            raise ValueError(f"Task {name!r} is already registered")
        task = SupervisedTask(
            name=name,
            _coro_factory=coro_factory,
            mode="daemon",
            _max_restarts=max_restarts,
            _interval_s=interval_s,
        )
        self._tasks[name] = task
        return task

    def register_periodic(
        self,
        name: str,
        tick_factory: Callable[[], Coroutine[Any, Any, None]],
        *,
        interval_s: float,
        run_immediately: bool = False,
        initial_delay_s: float | None = None,
        timeout_s: float | None = None,
        max_restarts: int = 10,
    ) -> SupervisedTask:
        """Register a periodic task whose cadence is owned by the supervisor.

        The supervisor owns the outer ``while self._running`` loop and
        delegates the per-tick work to *tick_factory*.  Heartbeat
        fields are populated on every tick (success or failure) so
        the runtime dashboard can show last/next run, duration, and
        overdue state without inspecting outer-coroutine lifecycle
        timestamps.

        Args:
            name: Unique task name.
            tick_factory: A no-arg coroutine factory returning the
                one-shot tick work.  Exceptions raised by the tick
                count as failures; the supervisor continues to schedule
                subsequent ticks unless the failure budget is exhausted.
            interval_s: Seconds between scheduled ticks.  Required.
            run_immediately: When ``True``, the first tick fires
                immediately (``first_delay_s=0.0``) instead of
                sleeping for ``interval_s``.  Mutually exclusive
                with ``initial_delay_s`` — supplying both raises
                ``ValueError``.
            initial_delay_s: Optional override for the first-tick
                delay.  Defaults to ``interval_s`` when omitted
                (preserves current sleep-first semantics).
            timeout_s: Optional per-tick timeout in seconds.  When set,
                each tick is awaited via ``asyncio.wait_for``; a
                ``TimeoutError`` is recorded as a tick failure and the
                loop continues to the next interval.
            max_restarts: Failure budget.  Defaults to 10; consumed
                by ``_consecutive_failure_count``.  When exhausted the
                task exits and the supervisor stops rescheduling.
        """
        if name in self._tasks:
            raise ValueError(f"Task {name!r} is already registered")
        if interval_s <= 0:
            raise ValueError(
                f"Periodic task {name!r} requires interval_s > 0 (got {interval_s!r})"
            )
        if initial_delay_s is not None and initial_delay_s < 0:
            raise ValueError(
                f"Periodic task {name!r} requires initial_delay_s >= 0"
                f" (got {initial_delay_s!r})"
            )
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError(
                f"Periodic task {name!r} requires timeout_s > 0 when set"
                f" (got {timeout_s!r})"
            )
        if run_immediately and initial_delay_s is not None:
            raise ValueError(
                f"Periodic task {name!r} cannot set both run_immediately=True "
                f"and initial_delay_s (got initial_delay_s={initial_delay_s!r})"
            )
        first_delay_s = (
            0.0
            if run_immediately
            else (
                float(interval_s) if initial_delay_s is None else float(initial_delay_s)
            )
        )
        task = SupervisedTask(
            name=name,
            _coro_factory=tick_factory,
            mode="periodic",
            _tick_factory=tick_factory,
            _max_restarts=max_restarts,
            _interval_s=float(interval_s),
            _initial_delay_s=first_delay_s,
            _timeout_s=float(timeout_s) if timeout_s is not None else None,
        )
        # Prime the first next-run window so the dashboard can show
        # "in <interval>" before the very first tick lands.  Same
        # module so private field assignment is intentional.
        task._next_run_at = time.time() + first_delay_s  # pyright: ignore[reportPrivateUsage]
        self._tasks[name] = task
        return task

    async def start_all(self) -> None:
        """Start all registered tasks."""
        for task in self._tasks.values():
            await task.start()

    async def stop_all(self) -> None:
        """Stop all registered tasks."""
        for task in self._tasks.values():
            await task.stop()

    def get_task(self, name: str) -> SupervisedTask | None:
        """Get a task by name."""
        return self._tasks.get(name)

    def snapshot(self) -> list[dict[str, Any]]:
        """Return runtime snapshots for all registered tasks."""
        tasks: list[dict[str, Any]] = []
        for supervised in self._tasks.values():
            with contextlib.suppress(Exception):
                tasks.append(supervised.snapshot())
        return tasks

    @property
    def all_healthy(self) -> bool:
        """Check if all tasks are running."""
        if not self._tasks:
            return False
        return all(t.is_running for t in self._tasks.values())


class BackgroundTaskMonitor:
    """Read-only heartbeat snapshot for background tasks.

    Stores a reference to the :class:`TaskSupervisor` and exposes a
    :meth:`snapshot` method that collects per-task heartbeat data
    without touching SQLite.  Designed to live on ``app.state`` and be
    consumed by :class:`~eggpool.runtime_metrics.RuntimeMetricsService`.
    """

    def __init__(self, supervisor: TaskSupervisor) -> None:
        self._supervisor = supervisor

    def snapshot(self) -> list[dict[str, Any]]:
        """Return per-task heartbeat data from the supervisor.

        Each entry mirrors the :class:`SupervisedTask` fields plus the
        heartbeat timestamps added during ``_run_loop``.  Failed probes
        never raise — malformed tasks are silently skipped.
        """
        return self._supervisor.snapshot()
