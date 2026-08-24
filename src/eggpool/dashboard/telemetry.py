"""Low-overhead dashboard performance telemetry.

Maintains a fixed-size rolling buffer of recent render durations
per route and exposes percentile summaries for the runtime metrics
snapshot.  Designed to add negligible overhead to the hot path:
``record_render`` is a single deque append and ``snapshot`` reads
at most 100 floats.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Any

_BUFFER_MAXLEN = 100


def _percentile(sorted_vals: list[float], pct: float) -> float:
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(len(sorted_vals) * pct) - 1))
    return sorted_vals[idx]


class DashboardTelemetry:
    """In-memory rolling buffer of recent dashboard render durations.

    Thread-safe via the GIL — ``deque.append`` and ``list()`` are
    atomic in CPython.  No locks required for the intended single-
    event-loop usage pattern.
    """

    def __init__(self) -> None:
        self._durations_ms: deque[float] = deque(maxlen=_BUFFER_MAXLEN)
        self._route_durations: dict[str, deque[float]] = {}
        self._stage_durations: dict[tuple[str, str], deque[float]] = {}
        self._stage_cache_hits: dict[tuple[str, str], deque[bool | None]] = {}
        self.cache_stats: Any = None

    def record_render(self, route: str, duration_ms: float) -> None:
        self._durations_ms.append(duration_ms)
        if route not in self._route_durations:
            self._route_durations[route] = deque(maxlen=_BUFFER_MAXLEN)
        self._route_durations[route].append(duration_ms)

    def record_stage(
        self,
        page: str,
        stage: str,
        elapsed_ms: float,
        cache_hit: bool | None = None,
    ) -> None:
        key = (page, stage)
        if key not in self._stage_durations:
            self._stage_durations[key] = deque(maxlen=_BUFFER_MAXLEN)
        self._stage_durations[key].append(elapsed_ms)
        if key not in self._stage_cache_hits:
            self._stage_cache_hits[key] = deque(maxlen=_BUFFER_MAXLEN)
        self._stage_cache_hits[key].append(cache_hit)

    def snapshot(self) -> dict[str, Any]:
        values = list(self._durations_ms)
        if not values:
            return {
                "recent_render_ms_p50": None,
                "recent_render_ms_p95": None,
                "slowest_recent_route": None,
            }

        sorted_vals = sorted(values)
        p50 = statistics.median(sorted_vals)
        p95 = _percentile(sorted_vals, 0.95)

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

    def stage_snapshot(self) -> dict[str, Any]:
        if not self._stage_durations:
            return {"slow_stages": []}
        entries: list[dict[str, Any]] = []
        for (page, stage), durations in self._stage_durations.items():
            vals = sorted(durations)
            sample_count = len(vals)
            avg = sum(vals) / sample_count if sample_count else 0.0
            p95_val = _percentile(vals, 0.95) if vals else 0.0
            entries.append(
                {
                    "page": page,
                    "stage": stage,
                    "p95_ms": round(p95_val, 2),
                    "avg_ms": round(avg, 2),
                    "sample_count": sample_count,
                }
            )
        entries.sort(key=lambda e: e["p95_ms"], reverse=True)
        return {"slow_stages": entries[:10]}
