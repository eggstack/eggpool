"""Low-overhead dashboard performance telemetry.

Maintains a fixed-size rolling buffer of recent render durations
per route and exposes percentile summaries for the runtime metrics
snapshot.  Designed to add negligible overhead to the hot path:
``record_render`` is a single deque append and ``snapshot`` reads
at most 100 floats.
"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Any

_BUFFER_MAXLEN = 100


class DashboardTelemetry:
    """In-memory rolling buffer of recent dashboard render durations.

    Thread-safe via the GIL — ``deque.append`` and ``list()`` are
    atomic in CPython.  No locks required for the intended single-
    event-loop usage pattern.
    """

    def __init__(self) -> None:
        self._durations_ms: deque[float] = deque(maxlen=_BUFFER_MAXLEN)
        self._route_durations: dict[str, deque[float]] = {}

    def record_render(self, route: str, duration_ms: float) -> None:
        """Record a single render duration for *route*."""
        self._durations_ms.append(duration_ms)
        if route not in self._route_durations:
            self._route_durations[route] = deque(maxlen=_BUFFER_MAXLEN)
        self._route_durations[route].append(duration_ms)

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot of recent render performance.

        Returns a dict with:
        - ``recent_render_ms_p50``: median of all recent renders
        - ``recent_render_ms_p95``: 95th percentile of all recent renders
        - ``slowest_recent_route``: route with highest recent average
        """
        values = list(self._durations_ms)
        if not values:
            return {
                "recent_render_ms_p50": None,
                "recent_render_ms_p95": None,
                "slowest_recent_route": None,
            }

        sorted_vals = sorted(values)
        p50 = statistics.median(sorted_vals)
        p95_idx = max(0, int(len(sorted_vals) * 0.95) - 1)
        p95 = sorted_vals[p95_idx]

        # Find the route with the highest average render time
        slowest_route: str | None = None
        slowest_avg: float = -1.0
        for route, durations in self._route_durations.items():
            if durations:
                avg = sum(durations) / len(durations)
                if avg > slowest_avg:
                    slowest_avg = avg
                    slowest_route = route

        return {
            "recent_render_ms_p50": round(p50, 2),
            "recent_render_ms_p95": round(p95, 2),
            "slowest_recent_route": slowest_route,
        }
