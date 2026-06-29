"""In-memory dispatch-overhead recorder for the coordinator hot path.

Stores only nanosecond durations in a bounded rolling window; never
persists, never logs, and never touches request identity, bodies, or
auth headers.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DispatchOverheadSnapshot:
    """Frozen summary of the dispatch-overhead recorder state."""

    window_size: int
    sample_count: int
    avg_ms: float | None
    min_ms: float | None
    max_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


class DispatchOverheadRecorder:
    """Bounded rolling-window recorder for upstream-dispatch overhead.

    Sample units are nanoseconds; snapshot output is in milliseconds.
    The recorder is process-local and thread-safe.
    """

    def __init__(self, window_size: int = 100) -> None:
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        self._samples_ns: deque[int] = deque(maxlen=window_size)
        self._lock = threading.Lock()
        self._window_size = window_size

    @property
    def window_size(self) -> int:
        return self._window_size

    def record_ns(self, elapsed_ns: int) -> None:
        if elapsed_ns < 0:
            return
        with self._lock:
            self._samples_ns.append(int(elapsed_ns))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self._samples_ns)
        if not samples:
            return {
                "window_size": self._window_size,
                "sample_count": 0,
                "avg_ms": None,
                "min_ms": None,
                "max_ms": None,
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
            }
        samples.sort()
        count = len(samples)
        avg_ns = sum(samples) / count

        def percentile(p: float) -> float:
            index = min(count - 1, max(0, int(round((count - 1) * p))))
            return samples[index] / 1_000_000

        return {
            "window_size": self._window_size,
            "sample_count": count,
            "avg_ms": avg_ns / 1_000_000,
            "min_ms": samples[0] / 1_000_000,
            "max_ms": samples[-1] / 1_000_000,
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
        }
