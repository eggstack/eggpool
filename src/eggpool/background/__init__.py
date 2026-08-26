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

# Task names owned by the process (not by a generation lease).
# Used by :meth:`TaskSupervisor.apply_spec_diff` to classify specs.
_PROCESS_OWNED_TASKS: frozenset[str] = frozenset(
    {
        "checkpoint",
        "metrics_flush",
        "update_checker",
        "automatic_backup",
    }
)

# Process-owned tasks that are never part of a reload diff.
# Registered once at startup; ``apply_spec_diff`` skips them on both
# the active and candidate sides so they survive generation swaps.
_PERSISTENT_PROCESS_TASKS: frozenset[str] = frozenset({"update_checker"})


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


def _first_run_state(
    task: SupervisedTask,
) -> str:
    """Classify a supervised task for the dashboard's first-run column.

    Returns one of:

    - ``never_run_not_due``: registered, no tick yet, first run scheduled
      in the future (i.e. ``next_run_at`` is in the future).
    - ``never_run_startup_deferred``: registered with ``initial_delay_s``
      still in flight -- the periodic loop has not yet slept past the
      first delay.
    - ``never_run_overdue``: registered but no tick yet, and the deadline
      has already passed.
    - ``last_success``: at least one successful tick.
    - ``last_error``: the last attempt failed and no successful tick has
      been recorded.

    Daemon tasks always return ``last_success`` (they have no first-run
    semantics worth surfacing).
    """
    if task.mode != "periodic":
        return "last_success"
    # ``_next_run_at`` is a monotonic deadline; compare on the same clock.
    now = time.monotonic()
    next_run_at = task._next_run_at if task._next_run_at > 0 else None  # pyright: ignore[reportPrivateUsage]
    last_tick_started = (
        task._last_tick_started_at if task._last_tick_started_at > 0 else None  # pyright: ignore[reportPrivateUsage]
    )
    has_run = last_tick_started is not None

    if task._failure_count > 0 and (  # pyright: ignore[reportPrivateUsage]
        task._success_count == 0  # pyright: ignore[reportPrivateUsage]
    ):
        return "last_error"
    if has_run:
        return "last_success"
    if next_run_at is None:
        return "never_run_not_due"
    if now < next_run_at:
        return "never_run_not_due"
    if task._initial_delay_s is not None and task._initial_delay_s > 0:  # pyright: ignore[reportPrivateUsage]
        return "never_run_startup_deferred"
    return "never_run_overdue"


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

    Cadence diagnostics (Milestone A3):
    - ``_configured_interval_s`` / ``_configured_initial_delay_s``:
      mirrors of the schedule values as last applied; useful for
      comparing the live ``_interval_s`` to the original configuration.
    - ``_initial_delay_consumed``: ``True`` once the one-time initial
      delay (if any) has been waited out before the first tick.
    - ``_previous_tick_started_at`` / ``_last_tick_started_at``: two
      consecutive tick-start timestamps so callers can compute the
      observed interval between ticks.
    - ``_last_tick_drift_s``: ``actual_tick_start - scheduled_tick_start``
      for the most recent tick; positive drift means the scheduler
      began late (event-loop starvation), negative drift means it
      began early.  Drift does NOT conflate tick duration.  Both
      operands are measured on ``time.monotonic()`` so wall-clock
      steps (NTP, manual adjustment) are never reported as drift.
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
    # Monotonic deadline for the next tick; wall-clock projections for
    # display are derived in ``snapshot()``.
    _next_run_at: float = 0.0
    _tick_in_progress: bool = False
    _iteration_count: int = 0
    _success_count: int = 0
    _failure_count: int = 0
    _consecutive_failure_count: int = 0
    _initial_delay_s: float | None = None
    _last_tick_duration_ms: float | None = None
    _timeout_s: float | None = None
    # Milestone A3 cadence diagnostics
    _configured_interval_s: float | None = None
    _configured_initial_delay_s: float | None = None
    _initial_delay_consumed: bool = False
    _previous_tick_started_at: float = 0.0
    _last_tick_drift_s: float | None = None

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

        Initial-delay semantics (Milestone A1): the initial sleep
        before the very first tick is resolved ONCE before the loop
        and is consumed exactly once.  After the first tick completes
        (success or failure) the loop switches to ``_interval_s`` for
        every subsequent sleep, regardless of how many times the
        per-tick body fails.  The state machine is::

            registered
              -> optional one-time initial delay
              -> tick 1
              -> interval delay
              -> tick 2
              -> interval delay
              -> ...

        The interval is re-read each iteration from
        ``self._interval_s`` so live reloads via ``update_task_spec``
        or ``apply_spec_diff`` take effect at the next tick boundary
        without a task restart.  Mutating ``self._initial_delay_s``
        after the first tick has fired has no effect (the initial
        delay is already consumed); ``stop()`` followed by ``start()``
        re-applies the initial delay because that is a new supervisor
        lifecycle.

        The scheduler is fixed-delay: the next interval begins after
        the previous tick completes.  This prevents overlapping ticks
        on database maintenance tasks and is the documented policy.
        """
        assert self._tick_factory is not None
        self._last_started_at = time.time()
        # Resolve the one-time initial delay BEFORE the loop.  Re-reading
        # ``self._initial_delay_s`` on every iteration was the original
        # defect: tasks with a short initial delay kept running at that
        # delay forever instead of their configured interval.  The
        # effective first sleep is resolved once:
        #   * ``run_immediately=True`` or ``initial_delay_s == 0`` -> 0
        #     (the loop yields once via ``asyncio.sleep(0)`` so the
        #     event loop can service other tasks before the first tick)
        #   * ``initial_delay_s > 0`` -> the configured delay
        #   * ``initial_delay_s is None`` -> ``interval_s`` (legacy
        #     sleep-first semantics; the first tick fires one interval
        #     after start)
        # After this single sleep the loop always uses ``interval_s``.
        initial_delay_explicit = (
            float(self._initial_delay_s) if self._initial_delay_s is not None else None
        )
        first_sleep_consumed = False
        try:
            while self._running:
                # Re-read the interval each iteration so live reloads
                # take effect at the next tick boundary.  The initial
                # delay is intentionally NOT re-read here.
                interval_s = float(self._interval_s) if self._interval_s else 0.0
                if not first_sleep_consumed:
                    if initial_delay_explicit is None:
                        first_sleep_s = interval_s
                    elif initial_delay_explicit <= 0.0:
                        first_sleep_s = 0.0
                    else:
                        first_sleep_s = initial_delay_explicit
                    first_sleep_consumed = True
                    sleep_s = first_sleep_s
                else:
                    sleep_s = interval_s
                # Project the scheduled tick start so we can compute
                # drift on resume.  Drift is defined as
                # ``actual_tick_start - scheduled_tick_start``; it
                # measures event-loop latency / scheduler delay, not
                # tick duration.  Both operands use the monotonic clock
                # so wall-clock adjustments during ``asyncio.sleep()``
                # never masquerade as scheduler drift.
                scheduled_tick_start = time.monotonic() + sleep_s
                try:
                    await asyncio.sleep(sleep_s)
                except asyncio.CancelledError:
                    break
                # The one-time initial delay is now behind us.  Mark
                # it consumed so the dashboard can distinguish
                # healthy startup-deferred tasks from never-run tasks.
                self._initial_delay_consumed = True
                self._tick_in_progress = True
                tick_started = time.time()
                # Drift = actual start - scheduled start.  Sleep may
                # return slightly early or late depending on event
                # loop scheduling; both are surfaced for the operator.
                self._last_tick_drift_s = time.monotonic() - scheduled_tick_start
                # Preserve the previous tick-start timestamp so the
                # snapshot can compute the observed interval between
                # ticks (configured_interval_s vs observed interval).
                if self._last_tick_started_at > 0:
                    self._previous_tick_started_at = self._last_tick_started_at
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
                    self._next_run_at = time.monotonic() + interval_s
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
                    self._next_run_at = time.monotonic() + interval_s
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
            # ``_next_run_at`` is a monotonic deadline.  Overdue is
            # computed against the same clock so wall-clock steps never
            # produce false alerts; the exposed timestamp is projected
            # back onto the wall clock for display only.
            overdue_seconds = (
                None
                if self._tick_in_progress
                else _compute_overdue_seconds(
                    now=time.monotonic(),
                    next_run_at=self._next_run_at,
                    interval_s=self._interval_s,
                )
            )
            next_run_at = time.time() + (self._next_run_at - time.monotonic())
        first_run_state = _first_run_state(self)
        # Milestone A3: surface observed interval and drift so
        # operators can compare the live schedule to the
        # configured cadence.  Drift is sign-preserving (positive
        # = scheduler began late, negative = began early); the
        # caller decides what counts as an actionable excursion.
        observed_last_interval_s: float | None = None
        if (
            self._previous_tick_started_at > 0
            and self._last_tick_started_at > 0
            and self._last_tick_started_at > self._previous_tick_started_at
        ):
            observed_last_interval_s = (
                self._last_tick_started_at - self._previous_tick_started_at
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
            "configured_interval_s": self._configured_interval_s,
            "configured_initial_delay_s": self._configured_initial_delay_s,
            "initial_delay_consumed": self._initial_delay_consumed,
            "previous_tick_started_at": self._previous_tick_started_at or None,
            "observed_last_interval_s": observed_last_interval_s,
            "last_tick_drift_s": self._last_tick_drift_s,
            "last_started_at": self._last_started_at or None,
            "last_completed_at": self._last_completed_at or None,
            "last_failure_at": self._last_failure or None,
            "last_tick_started_at": self._last_tick_started_at or None,
            "last_tick_completed_at": self._last_tick_completed_at or None,
            "last_tick_duration_ms": self._last_tick_duration_ms,
            "next_run_at": next_run_at,
            "overdue_seconds": overdue_seconds,
            "tick_in_progress": self._tick_in_progress,
            "last_error_at": self._last_error_at or None,
            "last_error_class": self._last_error_class,
            "first_run_state": first_run_state,
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
            # Milestone A3: snapshot the schedule as-configured so
            # operators can compare the live ``interval_s`` /
            # ``initial_delay_s`` to the original configuration.  A
            # live reload that mutates these fields leaves the
            # ``_configured_*`` snapshot intact; the operator can
            # tell "what was applied" from "what is currently live".
            _configured_interval_s=float(interval_s),
            _configured_initial_delay_s=(
                float(initial_delay_s) if initial_delay_s is not None else None
            ),
        )
        # Prime the first next-run window so the dashboard can show
        # "in <interval>" before the very first tick lands.  Same
        # module so private field assignment is intentional.  The
        # deadline is monotonic; ``snapshot()`` projects it onto the
        # wall clock for display.
        task._next_run_at = time.monotonic() + first_delay_s  # pyright: ignore[reportPrivateUsage]
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

    async def update_task_spec(
        self,
        name: str,
        *,
        tick_factory: Callable[[], Coroutine[Any, Any, None]] | None = None,
        interval_s: float | None = None,
        initial_delay_s: float | None = None,
        run_immediately: bool | None = None,
        timeout_s: float | None = None,
        max_restarts: int = 10,
    ) -> SupervisedTask | None:
        """Atomically replace a registered periodic task with a fresh one.

        The existing task under ``name`` (if any) is stopped and removed
        from the registry before a new :class:`SupervisedTask` is
        constructed using the supplied attributes plus the existing
        values for any attributes not provided.  The new task is
        started so callers see a single live schedule swap.

        Returns the new supervised task on success, or ``None`` when no
        task is registered under ``name``.
        """
        existing = self._tasks.get(name)
        if existing is None:
            return None
        if existing.mode != "periodic":
            return None

        new_interval = (
            float(interval_s)
            if interval_s is not None
            else float(existing._interval_s or 0.0)  # pyright: ignore[reportPrivateUsage]
        )
        new_factory = (
            tick_factory if tick_factory is not None else existing._tick_factory  # pyright: ignore[reportPrivateUsage]
        )
        new_timeout = (
            float(timeout_s) if timeout_s is not None else existing._timeout_s  # pyright: ignore[reportPrivateUsage]
        )
        new_run_immediately = (
            bool(run_immediately) if run_immediately is not None else False
        )
        # For a fresh replacement task, the first-tick delay defaults to the
        # new interval unless the caller explicitly overrides it.  We do not
        # carry over the previous task's initial_delay_s because that was the
        # schedule chosen for the *old* lifecycle, not this one.
        new_initial_delay: float | None
        if initial_delay_s is not None:
            new_initial_delay = float(initial_delay_s)
        elif new_run_immediately:
            new_initial_delay = 0.0
        else:
            new_initial_delay = new_interval

        await existing.stop()
        self._tasks.pop(name, None)

        if new_run_immediately:
            new_initial_delay = 0.0

        new_task = self.register_periodic(
            name,
            new_factory,  # type: ignore[arg-type]
            interval_s=new_interval,
            run_immediately=new_run_immediately,
            initial_delay_s=new_initial_delay,
            timeout_s=new_timeout,
            max_restarts=max_restarts,
        )
        await new_task.start()
        return new_task

    def snapshot(self) -> list[dict[str, Any]]:
        """Return runtime snapshots for all registered tasks."""
        tasks: list[dict[str, Any]] = []
        for supervised in self._tasks.values():
            with contextlib.suppress(Exception):
                tasks.append(supervised.snapshot())
        return tasks

    async def apply_spec_diff(
        self,
        candidate_specs: tuple[Any, ...],
        *,
        callback_factories: dict[str, Callable[[], Coroutine[Any, Any, None]]],
        process: Any | None = None,  # noqa: ANN401 — ProcessRuntime, avoids circular import
    ) -> Any:
        """Apply a task-spec diff against the current registration state.

        Builds the active spec tuple from the current ``_tasks`` dict,
        computes the diff against ``candidate_specs``, and applies it.
        Returns a :class:`~eggpool.runtime_tasks.TaskTransitionResult`.

        When ``process`` is a :class:`~eggpool.runtime_manager.ProcessRuntime`,
        the method increments ``process.task_spec_version`` and records
        the transition summary in ``process.last_task_transition``.
        """
        from eggpool.runtime_task_inventory import (  # noqa: PLC0415
            RuntimeTaskSpec,
            TaskOwnership,
        )
        from eggpool.runtime_tasks import (  # noqa: PLC0415
            TaskTransitionResult,
            compute_spec_diff,
        )

        active_specs: list[RuntimeTaskSpec] = []
        for name, task in self._tasks.items():
            if name in _PERSISTENT_PROCESS_TASKS:
                continue  # persistent task; not part of the reload diff
            if task.mode == "periodic":
                interval_val = float(task._interval_s) if task._interval_s else 0.0  # pyright: ignore[reportPrivateUsage]
                delay_val = task._initial_delay_s  # pyright: ignore[reportPrivateUsage]
                run_immed = (delay_val == 0.0) if delay_val is not None else False
                active_specs.append(
                    RuntimeTaskSpec(
                        name=name,
                        interval_s=interval_val,
                        initial_delay_s=delay_val,
                        run_immediately=run_immed,
                        timeout_s=task._timeout_s,  # pyright: ignore[reportPrivateUsage]
                        ownership=TaskOwnership.PROCESS
                        if name in _PROCESS_OWNED_TASKS
                        else TaskOwnership.GENERATION_LEASED,
                        enabled=True,
                        description="",
                        reloadable_fields=(),
                        generation_dependencies=(),
                        process_dependencies=(),
                        callback_kind=name,
                    )
                )

        diff = compute_spec_diff(tuple(active_specs), candidate_specs)

        added_names: list[str] = []
        removed_names: list[str] = []
        changed_details: list[tuple[str, tuple[float, float]]] = []
        unchanged_names: list[str] = [s.name for s in diff.unchanged]
        duplicates_rejected: list[str] = []

        for spec in diff.removed:
            if spec.name in _PERSISTENT_PROCESS_TASKS:
                continue  # persistent task — never removed during reload
            existing = self._tasks.get(spec.name)
            if existing is not None:
                await existing.stop()
                self._tasks.pop(spec.name, None)  # noqa: SLF001
                removed_names.append(spec.name)

        for spec in diff.added:
            factory = callback_factories.get(spec.name)
            if factory is None:
                logger.warning("No callback factory for added task %r", spec.name)
                continue
            if spec.name in self._tasks:  # noqa: SLF001
                duplicates_rejected.append(spec.name)
                continue
            self.register_periodic(
                spec.name,
                factory,
                interval_s=spec.interval_s,
                run_immediately=spec.run_immediately,
                initial_delay_s=spec.initial_delay_s,
                timeout_s=spec.timeout_s,
            )
            task = self.get_task(spec.name)
            if task is not None:
                await task.start()
            added_names.append(spec.name)

        for active_spec, candidate_spec in diff.changed:
            factory = callback_factories.get(candidate_spec.name)
            if factory is None:
                logger.warning(
                    "No callback factory for changed task %r", candidate_spec.name
                )
                unchanged_names.append(candidate_spec.name)
                continue
            existing = self._tasks.get(candidate_spec.name)
            old_interval = active_spec.interval_s
            if existing is not None:
                await existing.stop()
                self._tasks.pop(candidate_spec.name, None)  # noqa: SLF001
            self.register_periodic(
                candidate_spec.name,
                factory,
                interval_s=candidate_spec.interval_s,
                run_immediately=candidate_spec.run_immediately,
                initial_delay_s=candidate_spec.initial_delay_s,
                timeout_s=candidate_spec.timeout_s,
            )
            task = self.get_task(candidate_spec.name)
            if task is not None:
                await task.start()
            changed_details.append(
                (candidate_spec.name, (old_interval, candidate_spec.interval_s))
            )

        result = TaskTransitionResult(
            added=tuple(added_names),
            removed=tuple(removed_names),
            changed=tuple(changed_details),
            unchanged=tuple(unchanged_names),
            duplicates_rejected=tuple(duplicates_rejected),
        )

        # Operational event logging for task transitions.
        if added_names:
            logger.info(
                "Task transition: added %s",
                ", ".join(added_names),
            )
        if removed_names:
            logger.info(
                "Task transition: removed %s",
                ", ".join(removed_names),
            )
        if changed_details:
            for task_name, (old_int, new_int) in changed_details:
                logger.info(
                    "Task transition: %s schedule updated %.1fs -> %.1fs",
                    task_name,
                    old_int,
                    new_int,
                )
        if duplicates_rejected:
            logger.warning(
                "Task transition: duplicate schedule prevented for %s",
                ", ".join(duplicates_rejected),
            )

        if process is not None:
            process.task_spec_version += 1  # pyright: ignore[reportOptionalMemberAccess]
            process.last_task_transition = {  # pyright: ignore[reportOptionalMemberAccess]
                "last_reload_monotonic": time.monotonic(),
                "added": tuple(added_names),
                "removed": tuple(removed_names),
                "changed": tuple(
                    (name, old_int, new_int)
                    for name, (old_int, new_int) in changed_details
                ),
                "unchanged": tuple(unchanged_names),
            }

        return result

    def unregister(self, name: str) -> SupervisedTask | None:
        """Stop and remove a task by name.

        Returns the removed supervised task, or ``None`` if no task was
        registered.  Cancel is best-effort; the caller should ``await``
        any tasks they want a clean shutdown for before this call.
        """
        existing = self._tasks.pop(name, None)
        if existing is None:
            return None
        if existing._task is not None and not existing._task.done():  # pyright: ignore[reportPrivateUsage]
            existing._running = False  # pyright: ignore[reportPrivateUsage]
            existing._task.cancel()  # pyright: ignore[reportPrivateUsage]
        return existing

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
