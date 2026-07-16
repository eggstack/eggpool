"""Lightweight event-loop lag monitor for the Granian worker.

Measures the gap between expected and actual wake time on a periodic
callback.  The monitor is designed for SBC / Raspberry Pi deployments
where heavyweight host monitoring is unwanted.  It adds negligible
overhead: no per-request allocations, a fixed-size sample buffer, and
a single background task that sleeps between measurements.

The monitor is process-local (never persisted) and exposes only
non-secret diagnostic data.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_SIZE = 200
_DEFAULT_CADENCE_S = 1.0


@dataclass(frozen=True, slots=True)
class EventLoopLagSnapshot:
    """Frozen summary of the event-loop lag monitor state."""

    window_size: int
    sample_count: int
    avg_ms: float | None
    min_ms: float | None
    max_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    loop_identity: str
    cadence_s: float
    last_sample_ts: float | None


class EventLoopLagMonitor:
    """Measures event-loop lag via periodic callback drift.

    Uses ``asyncio.get_event_loop().call_later()`` to schedule a
    callback at a fixed cadence.  Each callback records the drift
    between its *scheduled* time and the time it actually runs, which
    captures event-loop starvation without any per-request hooks.

    Thread-safety: ``record_sample()`` is called from the event-loop
    thread and acquires a ``threading.Lock`` only for the bounded
    deque append, ensuring ``snapshot()`` can be called safely from
    any thread (e.g. a diagnostics endpoint on a different thread).

    Usage::

        monitor = EventLoopLagMonitor(cadence_s=1.0)
        monitor.start()   # schedules the first callback
        # ... later, at shutdown:
        await monitor.stop()
    """

    def __init__(
        self,
        *,
        cadence_s: float = _DEFAULT_CADENCE_S,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        loop_identity: str | None = None,
    ) -> None:
        self._cadence_s = cadence_s
        self._window_size = window_size
        self._samples_ms: deque[float] = deque(maxlen=window_size)
        self._lock = threading.Lock()
        self._loop_identity = loop_identity or self._anonymize_loop_id()
        self._last_sample_ts: float | None = None
        self._handle: asyncio.TimerHandle | None = None

        # Internal scheduling state (only accessed from the event loop)
        self._expected_next: float = 0.0
        self._running = False

    @staticmethod
    def _anonymize_loop_id() -> str:
        """Return an anonymised loop identifier for diagnostics."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return "unknown"
        loop_id = id(loop)
        # Derive a short opaque number from the id so the identity is
        # stable within a process but not guessable externally.
        return f"loop-{loop_id & 0xFFFF:04x}"

    def start(self) -> None:
        """Schedule the first measurement callback.

        Idempotent: a second call while running is a no-op.
        """
        if self._running:
            return
        self._running = True
        self._expected_next = time.monotonic() + self._cadence_s
        loop = asyncio.get_event_loop()
        self._handle = loop.call_later(self._cadence_s, self._tick)

    async def stop(self) -> None:
        """Cancel the background task and mark the monitor stopped.

        Safe to call multiple times.
        """
        self._running = False
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        logger.debug(
            "EventLoopLagMonitor stopped (%d samples)",
            len(self._samples_ms),
        )

    def _tick(self) -> None:
        """Callback executed at each cadence boundary.

        Measures the drift between the expected and actual wake time,
        records it, and schedules the next tick.
        """
        if not self._running:
            return

        now = time.monotonic()
        drift_ms = (now - self._expected_next) * 1000.0
        # Clamp negative drift (early wakeup) to 0 — we only care about
        # starvation, not premature scheduling.
        drift_ms = max(0.0, drift_ms)
        self._record_sample(drift_ms)

        # Schedule the next tick.  Base the expected time on the
        # cadence, not on ``now``, so the measurement is anchored to
        # a fixed interval even when a tick is late.
        self._expected_next += self._cadence_s
        # If we've fallen behind multiple cadences (e.g. the event
        # loop was blocked for several seconds), skip ahead rather
        # than catching up with a burst of rapid ticks.
        if self._expected_next < now:
            self._expected_next = now + self._cadence_s

        loop = asyncio.get_event_loop()
        self._handle = loop.call_later(self._cadence_s, self._tick)

    def _record_sample(self, lag_ms: float) -> None:
        """Append one lag sample to the bounded deque."""
        with self._lock:
            self._samples_ms.append(lag_ms)
            self._last_sample_ts = time.time()

    def snapshot(self) -> EventLoopLagSnapshot:
        """Return a frozen summary of the lag samples.

        Safe to call from any thread.  The returned dataclass is
        immutable so callers can hold it across await points without
        races.
        """
        with self._lock:
            samples = list(self._samples_ms)
            last_ts = self._last_sample_ts

        if not samples:
            return EventLoopLagSnapshot(
                window_size=self._window_size,
                sample_count=0,
                avg_ms=None,
                min_ms=None,
                max_ms=None,
                p50_ms=None,
                p95_ms=None,
                p99_ms=None,
                loop_identity=self._loop_identity,
                cadence_s=self._cadence_s,
                last_sample_ts=last_ts,
            )

        samples.sort()
        count = len(samples)
        avg_ms = sum(samples) / count

        def percentile(p: float) -> float:
            index = min(count - 1, max(0, int(math.floor((count - 1) * p))))
            return round(samples[index], 3)

        return EventLoopLagSnapshot(
            window_size=self._window_size,
            sample_count=count,
            avg_ms=round(avg_ms, 3),
            min_ms=round(samples[0], 3),
            max_ms=round(samples[-1], 3),
            p50_ms=percentile(0.50),
            p95_ms=percentile(0.95),
            p99_ms=percentile(0.99),
            loop_identity=self._loop_identity,
            cadence_s=self._cadence_s,
            last_sample_ts=last_ts,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the snapshot as a JSON-safe dict."""
        snap = self.snapshot()
        return {
            "window_size": snap.window_size,
            "sample_count": snap.sample_count,
            "avg_ms": snap.avg_ms,
            "min_ms": snap.min_ms,
            "max_ms": snap.max_ms,
            "p50_ms": snap.p50_ms,
            "p95_ms": snap.p95_ms,
            "p99_ms": snap.p99_ms,
            "loop_identity": snap.loop_identity,
            "cadence_s": snap.cadence_s,
            "last_sample_ts": snap.last_sample_ts,
        }
