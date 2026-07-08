"""Routing trace write pressure guardrails.

When SQLite lock contention spikes, routing trace writes amplify the
contention that downstream finalizers face.  Because routing traces
are diagnostic (not required for billing, retry, or crash recovery)
they can be skipped under pressure without affecting correctness.

The :class:`RoutingTraceGuard` exposes:

- ``record_skip()`` / ``record_written()`` to keep a running counter
- ``should_skip(db)`` to consult the current DB lock-wait p95 against
  the configured threshold
- ``snapshot()`` for the runtime metrics surface

This module is intentionally stdlib-only and dependency-free so it can
be imported from the coordinator hot path with no overhead.
"""

from __future__ import annotations

import logging
import threading
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
    ) -> None:
        self._threshold_ms = threshold_ms
        self._enabled = enabled
        self._lock = threading.Lock()
        self._written = 0
        self._skipped_db_pressure = 0
        self._skipped_disabled = 0
        self._last_skip_reason: str | None = None
        self._last_lock_p95_ms: float | None = None
        self._last_lock_sample_count: int = 0

    def configure(self, *, threshold_ms: float | None = None) -> None:
        """Update the skip threshold (used by tests/runtime config reload)."""
        with self._lock:
            if threshold_ms is not None:
                self._threshold_ms = threshold_ms

    def should_skip(self, db: Database | None) -> tuple[bool, str]:
        """Return ``(should_skip, reason)``.

        Reasons are stable short tokens suitable for metrics labels:

        - ``"disabled"``: guard disabled (``enabled=False``)
        - ``"db_pressure"``: rolling lock-wait p95 above threshold
        - ``"ok"``: trace write is allowed
        """
        with self._lock:
            enabled = self._enabled
            threshold = self._threshold_ms
        if not enabled:
            return True, "disabled"
        if threshold <= 0:
            return False, "ok"
        if db is None:
            return False, "ok"
        snapshot = db.contention_snapshot()
        p95 = snapshot.get("lock_wait_p95_ms")
        sample_count = snapshot.get("lock_wait_sample_count") or 0
        with self._lock:
            self._last_lock_p95_ms = p95
            self._last_lock_sample_count = int(sample_count)
        if p95 is not None and sample_count >= 8 and float(p95) > threshold:
            return True, "db_pressure"
        return False, "ok"

    def record_skip(self, *, reason: str) -> None:
        """Increment the skip counter for *reason*."""
        with self._lock:
            if reason == "db_pressure":
                self._skipped_db_pressure += 1
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
                "written": self._written,
                "skipped_db_pressure": self._skipped_db_pressure,
                "skipped_disabled": self._skipped_disabled,
                "skipped_total": (self._skipped_db_pressure + self._skipped_disabled),
                "last_skip_reason": self._last_skip_reason,
                "last_lock_wait_p95_ms": self._last_lock_p95_ms,
                "last_lock_wait_sample_count": self._last_lock_sample_count,
            }


_global_guard: RoutingTraceGuard | None = None
_global_guard_lock = threading.Lock()


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
