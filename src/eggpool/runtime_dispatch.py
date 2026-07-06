"""In-memory dispatch-overhead recorder for the coordinator hot path.

Stores only nanosecond durations in a bounded rolling window; never
persists, never logs, and never touches request identity, bodies, or
auth headers.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_SpanKey = str


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


# ---------------------------------------------------------------------------
# Fine-grained per-span recorder (Phase 1, hot-path dispatch optimization)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DispatchSpanStats:
    """Bounded rolling-window stats for a single named dispatch span."""

    span: str
    window_size: int
    sample_count: int
    avg_ms: float | None
    min_ms: float | None
    max_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "span": self.span,
            "window_size": self.window_size,
            "sample_count": self.sample_count,
            "avg_ms": self.avg_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
        }


@dataclass(slots=True)
class _PerSpanState:
    """Mutable per-span rolling buffer (nanoseconds)."""

    window_size: int
    samples: deque[int] = field(default_factory=lambda: deque(maxlen=0))

    def __post_init__(self) -> None:
        if self.samples.maxlen == 0:
            self.samples = deque(maxlen=self.window_size)


class DispatchSpanRecorder:
    """Bounded rolling-window recorder for named dispatch spans.

    Tracks per-span nanosecond durations in a bounded rolling window;
    snapshots return per-span avg/min/max/p50/p95/p99 in milliseconds.
    Spans with no recorded samples appear in the snapshot as ``None``
    sample fields so callers can distinguish "span did not run" from
    "span ran in zero nanoseconds".

    The recorder is process-local and thread-safe.  Capture uses
    ``time.perf_counter_ns`` for monotonic nanosecond precision.
    Hot-path additions use a single lock to append, and snapshots
    avoid copying buffers that were never touched (cheap empty
    short-circuit).
    """

    def __init__(self, window_size: int = 200) -> None:
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        self._spans: dict[str, _PerSpanState] = {}
        self._lock = threading.Lock()
        self._window_size = window_size

    @property
    def window_size(self) -> int:
        return self._window_size

    def record_ns(self, span: str, elapsed_ns: int) -> None:
        """Record an elapsed duration in nanoseconds for ``span``.

        Negative or zero values are ignored so callers can pass through
        uninitialised timers without scrubbing.
        """
        if elapsed_ns <= 0:
            return
        with self._lock:
            state = self._spans.get(span)
            if state is None:
                state = _PerSpanState(window_size=self._window_size)
                self._spans[span] = state
            state.samples.append(int(elapsed_ns))

    def measure(self, span: str) -> DispatchSpanTimer:
        """Return a context manager that records ``span`` on exit."""
        return DispatchSpanTimer(self, span)

    def spans(self) -> list[str]:
        """Return the set of span keys that have at least one sample.

        Snapshot is best-effort: callers can use this for diagnostics,
        iteration, or to seed list views.
        """
        with self._lock:
            return [s for s, state in self._spans.items() if state.samples]

    def snapshot(self) -> dict[str, Any]:
        """Return a per-span summary dict.

        Spans with no recorded samples in the current window appear
        with all numeric fields ``None``.  Span order is sorted for
        deterministic output.
        """
        with self._lock:
            keys = sorted(self._spans.keys())
            captured: list[tuple[_SpanKey, _PerSpanState]] = [
                (key, self._spans[key]) for key in keys
            ]
        rows: list[dict[str, Any]] = []
        for key, state in captured:
            rows.append(_summarize(key, state).as_dict())
        return {"window_size": self._window_size, "spans": rows}

    def snapshot_for_spans(self, spans: list[str]) -> dict[str, Any]:
        """Snapshot only the requested span keys (safer for fixed schemas).

        Spans not present in the recorder appear with all numeric
        fields ``None`` and ``sample_count == 0`` so the caller can
        rely on every requested key being represented.
        """
        with self._lock:
            captured: list[tuple[_SpanKey, _PerSpanState]] = []
            for key in sorted(set(spans)):
                state = self._spans.get(key)
                if state is None:
                    captured.append((key, _PerSpanState(window_size=self._window_size)))
                else:
                    captured.append((key, state))
            copied: dict[_SpanKey, _PerSpanState | None] = {}
            for key, state in captured:
                copied[key] = (
                    _PerSpanState(
                        window_size=state.window_size,
                        samples=deque(state.samples, maxlen=state.window_size),
                    )
                    if state.samples
                    else state
                )
        rows: list[dict[str, Any]] = []
        for key, state in copied.items():
            if state is None:
                continue
            rows.append(_summarize(key, state).as_dict())
        return {"window_size": self._window_size, "spans": rows}


def _summarize(span: str, state: _PerSpanState) -> _DispatchSpanStats:
    """Compute per-span percentile summary without mutating the buffer."""
    samples = list(state.samples)
    if not samples:
        return _DispatchSpanStats(
            span=span,
            window_size=state.window_size,
            sample_count=0,
            avg_ms=None,
            min_ms=None,
            max_ms=None,
            p50_ms=None,
            p95_ms=None,
            p99_ms=None,
        )
    samples.sort()
    count = len(samples)
    avg_ns = sum(samples) / count

    def percentile(p: float) -> float:
        index = min(count - 1, max(0, int(round((count - 1) * p))))
        return samples[index] / 1_000_000

    return _DispatchSpanStats(
        span=span,
        window_size=state.window_size,
        sample_count=count,
        avg_ms=avg_ns / 1_000_000,
        min_ms=samples[0] / 1_000_000,
        max_ms=samples[-1] / 1_000_000,
        p50_ms=percentile(0.50),
        p95_ms=percentile(0.95),
        p99_ms=percentile(0.99),
    )


class DispatchSpanTimer:
    """Context manager that records a named span on exit."""

    __slots__ = ("_recorder", "_span", "_start_ns")

    def __init__(self, recorder: DispatchSpanRecorder, span: str) -> None:
        self._recorder = recorder
        self._span = span
        self._start_ns: int = 0

    def __enter__(self) -> DispatchSpanTimer:
        self._start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        elapsed = time.perf_counter_ns() - self._start_ns
        self._recorder.record_ns(self._span, elapsed)


# Span keys used by the proxy request and selection paths.
SPAN_BODY_READ = "body_read"
SPAN_JSON_PARSE = "json_parse"
SPAN_MODEL_PARSE = "model_parse"
SPAN_CONTEXT_LIMIT = "context_limit"
SPAN_TRANSCODE_PREFLIGHT = "transcode_preflight"
SPAN_COMPRESSION_POLICY = "compression_policy"
SPAN_SEGMENTATION = "segmentation"
SPAN_COMPRESSION_ANALYZE = "compression_analyze"
SPAN_COMPRESSION_APPLY = "compression_apply"
SPAN_CONTEXT_BUILD = "context_build"
SPAN_COORDINATOR_PRE_UPSTREAM = "coordinator_pre_upstream"
SPAN_SELECTION_LOCK_WAIT = "selection_lock_wait"
SPAN_SELECTION_LOCKED = "selection_locked"
SPAN_THINKING_CLASSIFICATION = "thinking_classification"
SPAN_RESERVATION_ESTIMATE = "reservation_estimate"
SPAN_ROUTING_PLAN = "routing_plan"
SPAN_ACCOUNT_LOOKUP = "account_lookup"
SPAN_DB_WRITE_REQUEST = "db_write_request"
SPAN_DB_WRITE_RESERVATION = "db_write_reservation"
SPAN_DB_WRITE_ATTEMPT = "db_write_attempt"
SPAN_ROUTING_TRACE_BUILD = "routing_trace_build"
SPAN_ROUTING_TRACE_WRITE = "routing_trace_write"
SPAN_RUNTIME_PUBLICATION = "runtime_publication"


ALL_SPAN_KEYS: tuple[str, ...] = (
    SPAN_BODY_READ,
    SPAN_JSON_PARSE,
    SPAN_MODEL_PARSE,
    SPAN_CONTEXT_LIMIT,
    SPAN_TRANSCODE_PREFLIGHT,
    SPAN_COMPRESSION_POLICY,
    SPAN_SEGMENTATION,
    SPAN_COMPRESSION_ANALYZE,
    SPAN_COMPRESSION_APPLY,
    SPAN_CONTEXT_BUILD,
    SPAN_COORDINATOR_PRE_UPSTREAM,
    SPAN_SELECTION_LOCK_WAIT,
    SPAN_SELECTION_LOCKED,
    SPAN_THINKING_CLASSIFICATION,
    SPAN_RESERVATION_ESTIMATE,
    SPAN_ROUTING_PLAN,
    SPAN_ACCOUNT_LOOKUP,
    SPAN_DB_WRITE_REQUEST,
    SPAN_DB_WRITE_RESERVATION,
    SPAN_DB_WRITE_ATTEMPT,
    SPAN_ROUTING_TRACE_BUILD,
    SPAN_ROUTING_TRACE_WRITE,
    SPAN_RUNTIME_PUBLICATION,
)


__all__ = [
    "ALL_SPAN_KEYS",
    "DispatchOverheadRecorder",
    "DispatchOverheadSnapshot",
    "DispatchSpanRecorder",
    "DispatchSpanTimer",
    "SPAN_ACCOUNT_LOOKUP",
    "SPAN_BODY_READ",
    "SPAN_COMPRESSION_ANALYZE",
    "SPAN_COMPRESSION_APPLY",
    "SPAN_COMPRESSION_POLICY",
    "SPAN_CONTEXT_BUILD",
    "SPAN_CONTEXT_LIMIT",
    "SPAN_COORDINATOR_PRE_UPSTREAM",
    "SPAN_DB_WRITE_ATTEMPT",
    "SPAN_DB_WRITE_REQUEST",
    "SPAN_DB_WRITE_RESERVATION",
    "SPAN_JSON_PARSE",
    "SPAN_MODEL_PARSE",
    "SPAN_RESERVATION_ESTIMATE",
    "SPAN_ROUTING_PLAN",
    "SPAN_ROUTING_TRACE_BUILD",
    "SPAN_ROUTING_TRACE_WRITE",
    "SPAN_RUNTIME_PUBLICATION",
    "SPAN_SEGMENTATION",
    "SPAN_SELECTION_LOCKED",
    "SPAN_SELECTION_LOCK_WAIT",
    "SPAN_THINKING_CLASSIFICATION",
    "SPAN_TRANSCODE_PREFLIGHT",
]
