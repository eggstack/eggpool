"""Routing trace write pressure guardrails.

When SQLite lock contention spikes or the async writer queue is
stressed, routing trace writes amplify the contention that downstream
finalizers face.  Because routing traces are diagnostic (not required
for billing, retry, or crash recovery) they can be skipped under
pressure without affecting correctness.

The :class:`RoutingTraceGuard` exposes:

- ``record_skip()`` / ``record_written()`` to keep a running counter
- ``should_skip(db, writer_snapshot)`` to consult DB lock-wait p95,
  writer queue occupancy, oldest event age, and recent flush failures
- hysteresis cooldown to avoid oscillation on every snapshot
- ``snapshot()`` for the runtime metrics surface

This module is intentionally stdlib-only and dependency-free so it can
be imported from the coordinator hot path with no overhead.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from numbers import Real
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)


class RoutingTraceGuard:
    """Tracks routing-trace skip events and decides when to skip."""

    def __init__(
        self,
        *,
        threshold_ms: float = 200.0,
        enabled: bool = True,
        queue_occupancy_threshold: float = 0.8,
        oldest_event_age_s: float = 30.0,
        cooldown_s: float = 5.0,
    ) -> None:
        self._threshold_ms = _validate_threshold("threshold_ms", threshold_ms)
        self._enabled = enabled
        self._queue_occupancy_threshold = _validate_threshold(
            "queue_occupancy_threshold", queue_occupancy_threshold
        )
        self._oldest_event_age_s = _validate_threshold(
            "oldest_event_age_s", oldest_event_age_s
        )
        self._cooldown_s = _validate_threshold("cooldown_s", cooldown_s)
        self._lock = threading.Lock()
        self._written = 0
        self._skipped_db_pressure = 0
        self._skipped_queue_pressure = 0
        self._skipped_flush_failure = 0
        self._skipped_disabled = 0
        self._skipped_cooldown = 0
        self._last_skip_reason: str | None = None
        self._last_lock_p95_ms: float | None = None
        self._last_lock_sample_count: int = 0
        self._last_writer_queue_depth: int | None = None
        self._last_writer_queue_capacity: int | None = None
        self._last_writer_oldest_age_s: float | None = None
        self._last_writer_flush_errors: int | None = None
        # Hysteresis: monotonic timestamp after which skipping stops
        self._cooldown_until_mono: float = 0.0

    def configure(
        self,
        *,
        threshold_ms: float | None = None,
        queue_occupancy_threshold: float | None = None,
        oldest_event_age_s: float | None = None,
        cooldown_s: float | None = None,
    ) -> None:
        """Update guard thresholds (used by tests/runtime config reload)."""
        updates: dict[str, float] = {}
        if threshold_ms is not None:
            updates["_threshold_ms"] = _validate_threshold("threshold_ms", threshold_ms)
        if queue_occupancy_threshold is not None:
            updates["_queue_occupancy_threshold"] = _validate_threshold(
                "queue_occupancy_threshold", queue_occupancy_threshold
            )
        if oldest_event_age_s is not None:
            updates["_oldest_event_age_s"] = _validate_threshold(
                "oldest_event_age_s", oldest_event_age_s
            )
        if cooldown_s is not None:
            updates["_cooldown_s"] = _validate_threshold("cooldown_s", cooldown_s)
        with self._lock:
            for name, value in updates.items():
                setattr(self, name, value)

    def should_skip(
        self,
        db: Database | None,
        writer_snapshot: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Return ``(should_skip, reason)``.

        Reasons are stable short tokens suitable for metrics labels:

        - ``"disabled"``: guard disabled (``enabled=False``)
        - ``"db_pressure"``: rolling lock-wait p95 above threshold
        - ``"queue_pressure"``: writer queue occupancy above threshold
        - ``"oldest_event_stale"``: oldest queued event too old
        - ``"flush_failure"``: recent flush errors indicate drain problems
        - ``"cooldown"``: hysteresis cooldown active after previous skip
        - ``"ok"``: trace write is allowed
        """
        now_mono = time.monotonic()
        with self._lock:
            enabled = self._enabled
            threshold = self._threshold_ms
            queue_occ_thresh = self._queue_occupancy_threshold
            oldest_age_thresh = self._oldest_event_age_s
            cooldown = self._cooldown_s
            cooldown_until = self._cooldown_until_mono

        if not enabled:
            return True, "disabled"

        # --- hysteresis: stay in skip mode for cooldown window ---
        if cooldown > 0 and now_mono < cooldown_until:
            return True, "cooldown"

        # --- DB lock-wait pressure (only checked when threshold > 0) ---
        if threshold > 0 and db is not None:
            snapshot = db.contention_snapshot()
            p95 = snapshot.get("lock_wait_p95_ms")
            sample_count = snapshot.get("lock_wait_sample_count") or 0
            with self._lock:
                self._last_lock_p95_ms = p95
                self._last_lock_sample_count = int(sample_count)
            if p95 is not None and sample_count >= 8 and float(p95) > threshold:
                self._enter_cooldown(now_mono, cooldown)
                return True, "db_pressure"

        # --- Writer queue pressure ---
        if writer_snapshot is not None:
            q_depth = writer_snapshot.get("queue_depth")
            q_cap = writer_snapshot.get("queue_capacity")
            oldest_age = writer_snapshot.get("oldest_event_age_s")
            flush_errs = writer_snapshot.get("dropped_flush_error", 0)
            with self._lock:
                self._last_writer_queue_depth = q_depth
                self._last_writer_queue_capacity = q_cap
                self._last_writer_oldest_age_s = oldest_age
                self._last_writer_flush_errors = flush_errs

            if q_depth is not None and q_cap is not None and q_cap > 0:
                occ = q_depth / q_cap
                if occ > queue_occ_thresh:
                    self._enter_cooldown(now_mono, cooldown)
                    return True, "queue_pressure"

            if oldest_age is not None and oldest_age > oldest_age_thresh:
                self._enter_cooldown(now_mono, cooldown)
                return True, "oldest_event_stale"

            if flush_errs is not None and flush_errs > 0:
                self._enter_cooldown(now_mono, cooldown)
                return True, "flush_failure"

        return False, "ok"

    def _enter_cooldown(self, now_mono: float, cooldown_s: float) -> None:
        """Activate the hysteresis cooldown window."""
        with self._lock:
            self._cooldown_until_mono = now_mono + cooldown_s

    def record_skip(self, *, reason: str) -> None:
        """Increment the skip counter for *reason*."""
        with self._lock:
            if reason == "db_pressure":
                self._skipped_db_pressure += 1
            elif reason == "queue_pressure":
                self._skipped_queue_pressure += 1
            elif reason == "flush_failure":
                self._skipped_flush_failure += 1
            elif reason == "cooldown":
                self._skipped_cooldown += 1
            else:
                self._skipped_disabled += 1
            self._last_skip_reason = reason

    def record_written(self) -> None:
        with self._lock:
            self._written += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "threshold_ms": self._threshold_ms,
                "queue_occupancy_threshold": self._queue_occupancy_threshold,
                "oldest_event_age_s": self._oldest_event_age_s,
                "cooldown_s": self._cooldown_s,
                "written": self._written,
                "skipped_db_pressure": self._skipped_db_pressure,
                "skipped_queue_pressure": self._skipped_queue_pressure,
                "skipped_flush_failure": self._skipped_flush_failure,
                "skipped_disabled": self._skipped_disabled,
                "skipped_cooldown": self._skipped_cooldown,
                "skipped_total": (
                    self._skipped_db_pressure
                    + self._skipped_queue_pressure
                    + self._skipped_flush_failure
                    + self._skipped_disabled
                    + self._skipped_cooldown
                ),
                "last_skip_reason": self._last_skip_reason,
                "last_lock_wait_p95_ms": self._last_lock_p95_ms,
                "last_lock_wait_sample_count": self._last_lock_sample_count,
                "last_writer_queue_depth": self._last_writer_queue_depth,
                "last_writer_queue_capacity": self._last_writer_queue_capacity,
                "last_writer_oldest_age_s": self._last_writer_oldest_age_s,
                "last_writer_flush_errors": self._last_writer_flush_errors,
            }


_global_guard: RoutingTraceGuard | None = None
_global_guard_lock = threading.Lock()


def _validate_threshold(name: str, value: float) -> float:
    """Return a finite, non-negative numeric guard threshold."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite non-negative number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return normalized


def get_routing_trace_guard() -> RoutingTraceGuard:
    """Return the process-local routing trace guard (lazy singleton)."""
    global _global_guard
    with _global_guard_lock:
        if _global_guard is None:
            _global_guard = RoutingTraceGuard()
        return _global_guard


def reset_routing_trace_guard() -> None:
    """Test helper: clear the global singleton so a fresh one is created."""
    global _global_guard
    with _global_guard_lock:
        _global_guard = None
