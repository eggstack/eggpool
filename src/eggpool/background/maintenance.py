"""Bounded maintenance pass contract and contention guard.

Background maintenance tasks (retention cleanup, rollup, backfill) run
under a per-tick budget that limits rows processed, batch count, and
wall-clock time.  This keeps SQLite write-lock pressure bounded on
small-board deployments and prevents any single maintenance pass from
monopolising the database connection.

The :func:`run_maintenance_pass` helper enforces the budget and yields
to the event loop between batches.  :class:`ContentionGuard` defers
low-priority work when lock-wait p95 is elevated.

This module is intentionally stdlib-only and dependency-free so it can
be imported from background-task registration paths with no overhead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)

StopReason = Literal[
    "complete",
    "row_budget",
    "batch_budget",
    "time_budget",
    "contention_guard",
    "cancelled",
    "error",
]


@dataclass(frozen=True)
class MaintenancePassResult:
    """Outcome of a single bounded maintenance tick.

    Attributes:
        task_name: Name of the maintenance task that ran.
        rows_scanned: Rows inspected (read) during this pass.
        rows_changed: Rows inserted, updated, or deleted.
        batches_completed: Number of batches executed before stopping.
        duration_ms: Wall-clock time for the entire pass.
        remaining_estimate: Optional estimate of rows left to process
            (``None`` when unknown or fully drained).
        stopped_reason: Why the pass stopped.  One of ``"complete"``,
            ``"row_budget"``, ``"batch_budget"``, ``"time_budget"``,
            ``"contention_guard"``, ``"cancelled"``, ``"error"``.
        last_cursor: Opaque cursor for resumption (row ID, timestamp,
            or ``None`` when the pass completed or has no cursor).
        error_class: Qualified exception class name when
            ``stopped_reason == "error"``, else ``None``.
        contention_deferrals: How many times the contention guard
            prevented a batch from running.
    """

    task_name: str = ""
    rows_scanned: int = 0
    rows_changed: int = 0
    batches_completed: int = 0
    duration_ms: float = 0.0
    remaining_estimate: int | None = None
    stopped_reason: StopReason = "complete"
    last_cursor: str | int | None = None
    error_class: str | None = None
    contention_deferrals: int = 0
    budget_exhausted: bool = False

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.rows_changed == other
        if isinstance(other, MaintenancePassResult):
            return (
                self.task_name == other.task_name
                and self.rows_scanned == other.rows_scanned
                and self.rows_changed == other.rows_changed
                and self.batches_completed == other.batches_completed
                and self.duration_ms == other.duration_ms
                and self.remaining_estimate == other.remaining_estimate
                and self.stopped_reason == other.stopped_reason
                and self.last_cursor == other.last_cursor
                and self.error_class == other.error_class
                and self.contention_deferrals == other.contention_deferrals
                and self.budget_exhausted == other.budget_exhausted
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash(
            (
                self.task_name,
                self.rows_scanned,
                self.rows_changed,
                self.batches_completed,
                self.duration_ms,
                self.remaining_estimate,
                self.stopped_reason,
                self.last_cursor,
                self.error_class,
                self.contention_deferrals,
                self.budget_exhausted,
            )
        )

    def __add__(self, other: object) -> int:
        if isinstance(other, MaintenancePassResult):
            return self.rows_changed + other.rows_changed
        if isinstance(other, int):
            return self.rows_changed + other
        return NotImplemented

    def __radd__(self, other: object) -> int:
        if isinstance(other, int):
            return other + self.rows_changed
        return NotImplemented


@dataclass
class MaintenanceBudget:
    """Per-tick resource limits for a maintenance pass.

    Defaults are conservative single-board-computer settings.  P0 tasks
    (correctness recovery) receive higher budgets via the dedicated
    ``p0_*`` fields on
    :class:`~eggpool.models.config.MaintenanceBudgetConfig`.

    Attributes:
        max_rows_per_batch: Maximum rows to process in one batch.
        max_batches_per_tick: Maximum batches before yielding.
        max_tick_duration_ms: Wall-clock ceiling for the entire tick.
        priority: 0=P0 (correctness), 1=P1 (normal), 2=P2 (background).
    """

    max_rows_per_batch: int = 500
    max_batches_per_tick: int = 4
    max_tick_duration_ms: float = 500.0
    priority: int = 1

    def expired(self, *, start_time: float, batches_done: int) -> bool:
        """Return ``True`` when the budget is exhausted."""
        if batches_done >= self.max_batches_per_tick:
            return True
        elapsed_ms = (time.monotonic() - start_time) * 1000
        return elapsed_ms >= self.max_tick_duration_ms


class ContentionGuard:
    """Defers maintenance when SQLite lock-wait pressure is high.

    Consults :meth:`Database.contention_snapshot` and compares the
    rolling ``lock_wait_p95_ms`` against a configurable threshold.
    A starvation cap (``max_deferral_age_s``) ensures P1/P2 tasks are
    not deferred indefinitely when contention is persistently elevated.

    Args:
        db: Database connection to monitor.
        threshold_ms: Lock-wait p95 threshold above which deferral
            is triggered.  Default ``200.0`` matches the routing
            trace guard convention.
        min_samples: Minimum sample count required before the
            threshold is evaluated.  Fewer samples means not enough
            data to make a deferral decision (returns ``False``).
        max_deferral_age_s: Maximum seconds since last successful
            (non-deferred) check before forcing execution regardless
            of contention.  ``None`` disables the starvation cap.
    """

    def __init__(
        self,
        db: Database,
        *,
        threshold_ms: float = 200.0,
        min_samples: int = 8,
        max_deferral_age_s: float | None = None,
    ) -> None:
        self._db = db
        self._threshold_ms = threshold_ms
        self._min_samples = min_samples
        self._max_deferral_age_s = max_deferral_age_s
        self._deferrals: int = 0
        self._forced_by_starvation: int = 0
        self._last_p95_ms: float | None = None
        self._last_sample_count: int = 0
        self._last_successful_time: float = time.monotonic()

    @property
    def deferrals(self) -> int:
        """Total deferral count since construction."""
        return self._deferrals

    def record_success(self) -> None:
        """Record that maintenance actually ran (not deferred).

        Call after a successful non-deferred batch so the starvation
        cap resets.
        """
        self._last_successful_time = time.monotonic()

    async def should_defer(self) -> bool:
        """Return ``True`` when maintenance should be deferred.

        Checks the database contention snapshot for lock-wait pressure.
        If the rolling p95 exceeds the threshold with enough samples,
        the caller should skip the maintenance batch and try again later.

        When the starvation cap (``max_deferral_age_s``) is set and the
        time since last successful run exceeds it, deferral is bypassed
        to prevent indefinite starvation.
        """
        snapshot = self._db.contention_snapshot()
        p95 = snapshot.get("lock_wait_p95_ms")
        sample_count = int(snapshot.get("lock_wait_sample_count") or 0)
        self._last_p95_ms = p95
        self._last_sample_count = sample_count

        if p95 is None or sample_count < self._min_samples:
            return False

        if float(p95) > self._threshold_ms:
            # Check starvation cap before deferring.
            if self._max_deferral_age_s is not None:
                elapsed = time.monotonic() - self._last_successful_time
                if elapsed >= self._max_deferral_age_s:
                    self._forced_by_starvation += 1
                    logger.warning(
                        "ContentionGuard: starvation cap hit (%.1fs >= %.1fs), "
                        "forcing maintenance despite p95=%.1fms",
                        elapsed,
                        self._max_deferral_age_s,
                        float(p95),
                    )
                    return False

            self._deferrals += 1
            logger.debug(
                "ContentionGuard: deferring (p95=%.1fms > %.1fms threshold)",
                float(p95),
                self._threshold_ms,
            )
            return True

        return False

    def snapshot(self) -> dict[str, Any]:
        """Return diagnostic counters for runtime metrics."""
        elapsed_since_success = time.monotonic() - self._last_successful_time
        return {
            "threshold_ms": self._threshold_ms,
            "min_samples": self._min_samples,
            "max_deferral_age_s": self._max_deferral_age_s,
            "deferrals": self._deferrals,
            "forced_by_starvation": self._forced_by_starvation,
            "elapsed_since_last_success_s": round(elapsed_since_success, 3),
            "last_lock_wait_p95_ms": self._last_p95_ms,
            "last_lock_wait_sample_count": self._last_sample_count,
        }


async def run_maintenance_pass(
    task_name: str,
    budget: MaintenanceBudget,
    fn: Callable[[], Awaitable[MaintenancePassResult]],
    *,
    contention_guard: ContentionGuard | None = None,
) -> MaintenancePassResult:
    """Execute a maintenance function up to the budget limits.

    Calls *fn* up to ``budget.max_batches_per_tick`` times, tracking
    elapsed wall-clock time against ``budget.max_tick_duration_ms``.
    The event loop is yielded between batches via
    ``await asyncio.sleep(0)`` so other tasks can make progress.

    When a *contention_guard* is provided and ``should_defer()``
    returns ``True``, the batch is skipped and a deferral is counted.

    Args:
        task_name: Human-readable task name for the result.
        budget: Per-tick resource limits.
        fn: Async callable that performs one batch of work and returns
            a :class:`MaintenancePassResult` for that batch.
        contention_guard: Optional guard that may defer batches.

    Returns:
        A merged :class:`MaintenancePassResult` summarising the full tick.
    """
    tick_start = time.monotonic()
    total_rows_scanned = 0
    total_rows_changed = 0
    total_batches = 0
    total_deferrals = 0
    last_cursor: str | int | None = None
    remaining: int | None = None
    stopped_reason: StopReason = "complete"
    error_class: str | None = None
    budget_rows = 0

    for _batch_idx in range(budget.max_batches_per_tick):
        # --- time budget check ---
        elapsed_ms = (time.monotonic() - tick_start) * 1000
        if elapsed_ms >= budget.max_tick_duration_ms:
            stopped_reason = "time_budget"
            break

        # --- contention guard ---
        if contention_guard is not None and await contention_guard.should_defer():
            total_deferrals += 1
            continue

        # --- yield to event loop ---
        await asyncio.sleep(0)

        # --- execute one batch ---
        try:
            result = await fn()
        except asyncio.CancelledError:
            stopped_reason = "cancelled"
            break
        except Exception as exc:
            stopped_reason = "error"
            error_class = type(exc).__qualname__
            logger.exception("Maintenance pass %r batch failed", task_name)
            break

        total_rows_scanned += result.rows_scanned
        total_rows_changed += result.rows_changed
        budget_rows += result.rows_scanned + result.rows_changed
        total_batches += 1
        last_cursor = result.last_cursor
        remaining = result.remaining_estimate

        # --- row budget check ---
        if budget_rows >= budget.max_rows_per_batch * budget.max_batches_per_tick:
            stopped_reason = "row_budget"
            break

        # --- upstream indicated early stop ---
        if result.stopped_reason not in ("complete",):
            stopped_reason = result.stopped_reason
            break

        # --- nothing left to do ---
        if result.remaining_estimate is not None and result.remaining_estimate <= 0:
            stopped_reason = "complete"
            break

    total_duration_ms = (time.monotonic() - tick_start) * 1000

    return MaintenancePassResult(
        task_name=task_name,
        rows_scanned=total_rows_scanned,
        rows_changed=total_rows_changed,
        batches_completed=total_batches,
        duration_ms=round(total_duration_ms, 3),
        remaining_estimate=remaining,
        stopped_reason=stopped_reason,
        last_cursor=last_cursor,
        error_class=error_class,
        contention_deferrals=total_deferrals,
    )


class MaintenanceState:
    """Process-wide aggregator for maintenance task diagnostics.

    Holds the latest :class:`MaintenancePassResult` per task and a
    reference to the active :class:`ContentionGuard` so
    :class:`~eggpool.runtime_metrics.RuntimeMetricsService` can expose
    per-task diagnostics through ``/api/stats/runtime``.

    Tracks per-task cumulative ``rows_changed`` across ticks and
    ``last_success_at`` / ``last_error_at`` timestamps so operators can
    distinguish a freshly started task from one that is failing
    repeatedly.
    """

    def __init__(self) -> None:
        self._last_results: dict[str, MaintenancePassResult] = {}
        self._contention_guard: ContentionGuard | None = None
        self._cumulative_rows: dict[str, int] = {}
        self._last_success_at: dict[str, float] = {}
        self._last_error_at: dict[str, float] = {}
        self._configured_budgets: dict[str, dict[str, Any]] = {}

    def set_contention_guard(self, guard: ContentionGuard) -> None:
        """Bind the contention guard for diagnostics."""
        self._contention_guard = guard

    def record_result(self, result: MaintenancePassResult) -> None:
        """Store the latest pass result for a task."""
        self._last_results[result.task_name] = result
        name = result.task_name
        self._cumulative_rows[name] = (
            self._cumulative_rows.get(name, 0) + result.rows_changed
        )
        now = time.monotonic()
        if result.stopped_reason == "error":
            self._last_error_at[name] = now
        elif result.batches_completed > 0:
            self._last_success_at[name] = now

    def set_configured_budget(self, task_name: str, budget: dict[str, Any]) -> None:
        """Store the configured budget for a task."""
        self._configured_budgets[task_name] = budget

    def snapshot(self) -> dict[str, Any]:
        """Return the full diagnostics snapshot for runtime metrics."""
        tasks: dict[str, Any] = {}
        for name, result in self._last_results.items():
            snap: dict[str, Any] = {
                "rows_changed": result.rows_changed,
                "rows_scanned": result.rows_scanned,
                "batches_completed": result.batches_completed,
                "duration_ms": result.duration_ms,
                "stopped_reason": result.stopped_reason,
                "remaining_estimate": result.remaining_estimate,
                "contention_deferrals": result.contention_deferrals,
                "budget_exhausted": result.budget_exhausted,
                "error_class": result.error_class,
                "cumulative_rows_changed": self._cumulative_rows.get(name, 0),
            }
            if name in self._last_success_at:
                snap["last_success_at"] = round(self._last_success_at[name], 3)
            if name in self._last_error_at:
                snap["last_error_at"] = round(self._last_error_at[name], 3)
            if name in self._configured_budgets:
                snap["configured_budget"] = self._configured_budgets[name]
            tasks[name] = snap
        contention = (
            self._contention_guard.snapshot()
            if self._contention_guard is not None
            else None
        )
        return {
            "tasks": tasks,
            "contention_guard": contention,
        }
