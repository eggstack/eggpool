"""In-memory dispatch-overhead recorder for the coordinator hot path.

Stores only nanosecond durations in a bounded rolling window; never
persists, never logs, and never touches request identity, bodies, or
auth headers.

Plan 029 — Workstream H: fine-grained spans use deterministic
request-level sampling.  ``should_sample_request`` makes a stable
decision per request ID so that one sampled request records all
relevant spans (coherent trace), rather than an independent decision
per span (which produces partial traces).  Coarse metrics
(``DispatchOverheadRecorder``, ``LocalPreUpstreamRecorder``) remain
always-on and bounded.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_SpanKey = str

# Plan 029, Workstream H: per-request sampling decision propagated via
# a ContextVar so the coordinator's span recording (which uses the
# shared ``DispatchSpanRecorder`` instance) can respect the decision
# made in ``handle_proxy_request``.  ``None`` means "not set" (e.g.
# direct unit-test calls to ``record_ns``); ``False`` means the
# current request was not sampled; ``True`` means it was.
_request_sampled: ContextVar[bool | None] = ContextVar(
    "eggpool_dispatch_span_sampled", default=None
)


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
class LocalPreUpstreamSnapshot:
    """Frozen summary of the local-pre-upstream recorder state.

    Distinct from :class:`DispatchOverheadSnapshot`:
    - ``local_pre_upstream_ms`` covers the entire EggPool-side window
      from the earliest ASGI handler entry (after auth / body-limit
      middleware) to the dispatch boundary just before ``client.send``.
    - ``dispatch_overhead`` (coarse recorder) covers only the slice
      from :class:`ProxyRequestContext` construction to dispatch.
    """

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
            "window_size": self.window_size,
            "sample_count": self.sample_count,
            "avg_ms": self.avg_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
        }


class LocalPreUpstreamRecorder:
    """Bounded rolling-window recorder for total local pre-upstream latency.

    Records the full EggPool-side window from ASGI handler entry (after
    auth / body-limit middleware) to the dispatch boundary
    (``_send_upstream_request`` immediately before ``client.send``).
    Sample units are milliseconds; the recorder is process-local and
    thread-safe.  Negative values are ignored so callers can pass
    through uninitialised timers without scrubbing.

    Distinct from :class:`DispatchOverheadRecorder` which covers only
    the coordinator-internal slice (context build -> dispatch).
    """

    def __init__(self, window_size: int = 100) -> None:
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        self._samples_ms: deque[float] = deque(maxlen=window_size)
        self._lock = threading.Lock()
        self._window_size = window_size

    @property
    def window_size(self) -> int:
        return self._window_size

    def record_ms(self, elapsed_ms: int | float) -> None:
        if elapsed_ms < 0:
            return
        with self._lock:
            self._samples_ms.append(float(elapsed_ms))

    def snapshot(self) -> LocalPreUpstreamSnapshot:
        with self._lock:
            samples = list(self._samples_ms)
        if not samples:
            return LocalPreUpstreamSnapshot(
                window_size=self._window_size,
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
        avg_ms = sum(samples) / count

        def percentile(p: float) -> float:
            index = min(count - 1, max(0, int(round((count - 1) * p))))
            return float(samples[index])

        return LocalPreUpstreamSnapshot(
            window_size=self._window_size,
            sample_count=count,
            avg_ms=float(avg_ms),
            min_ms=float(samples[0]),
            max_ms=float(samples[-1]),
            p50_ms=percentile(0.50),
            p95_ms=percentile(0.95),
            p99_ms=percentile(0.99),
        )


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

    Thread-safety strategy:

    - ``_lock`` serialises append / snapshot to prevent concurrent
      deque mutation.  The lock is held only for the brief append
      (``deque.append``) or the snapshot copy (``list(state.samples)``).
    - Per-span state is lazily created under the lock so concurrent
      first-touch for different span keys does not leak.

    Request-coherent sampling (Plan 029, Workstream H)
    --------------------------------------------------
    ``should_sample_request`` makes a deterministic, stable decision
    per request ID using a SHA-256 hash.  When the rate is < 1.0,
    only sampled requests have their spans recorded, preserving a
    coherent trace.  The decision does not use per-span random number
    generation.  ``sampled_count`` and ``unsampled_count`` are
    incremented so operators can interpret distributions.
    """

    def __init__(
        self, window_size: int = 200, detailed_span_sample_rate: float = 0.05
    ) -> None:
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        if not 0.0 <= detailed_span_sample_rate <= 1.0:
            raise ValueError("detailed_span_sample_rate must be between 0.0 and 1.0")
        self._spans: dict[str, _PerSpanState] = {}
        self._lock = threading.Lock()
        self._window_size = window_size
        self._detailed_span_sample_rate = detailed_span_sample_rate
        self._sampled_count = 0
        self._unsampled_count = 0

    @property
    def window_size(self) -> int:
        return self._window_size

    @property
    def detailed_span_sample_rate(self) -> float:
        return self._detailed_span_sample_rate

    def should_sample_request(self, request_id: str) -> bool:
        """Deterministic, request-coherent sampling decision.

        Returns ``True`` when all spans for this request should be
        recorded, ``False`` when the request should be skipped.
        The decision is stable for a given ``request_id`` and does
        not use per-span random number generation.

        When ``detailed_span_sample_rate`` is 1.0 every request is
        sampled; when 0.0 none are.  Otherwise a SHA-256 hash of
        the request ID is normalised to [0, 1) and compared against
        the rate.

        ``sampled_count`` and ``unsampled_count`` are incremented
        so operators can interpret the sampling distribution.

        The decision is also stored in a :class:`ContextVar` so
        that :meth:`record_ns` (called from the coordinator's
        shared recorder instance) can respect it without each
        caller needing to pass the flag explicitly.
        """
        rate = self._detailed_span_sample_rate
        if rate >= 1.0:
            self._sampled_count += 1
            _request_sampled.set(True)
            return True
        if rate <= 0.0:
            self._unsampled_count += 1
            _request_sampled.set(False)
            return False
        digest = hashlib.sha256(request_id.encode("utf-8")).digest()
        # Use first 8 bytes as a uint64, normalise to [0, 1).
        val = int.from_bytes(digest[:8], "big") / (2**64)
        if val < rate:
            self._sampled_count += 1
            _request_sampled.set(True)
            return True
        self._unsampled_count += 1
        _request_sampled.set(False)
        return False

    def sampled_unsampled_counts(self) -> tuple[int, int]:
        """Return ``(sampled_count, unsampled_count)`` for diagnostics."""
        return self._sampled_count, self._unsampled_count

    def record_ns(self, span: str, elapsed_ns: int) -> None:
        """Record an elapsed duration in nanoseconds for ``span``.

        Negative or zero values are ignored so callers can pass through
        uninitialised timers without scrubbing.  Request-coherent
        sampling is applied via the :class:`ContextVar` set by
        :meth:`should_sample_request`: if the current request was not
        sampled, this method is a no-op.  When the ContextVar is
        unset (``None`` — e.g. direct unit-test calls), recording
        proceeds unconditionally for backward compatibility.
        """
        if elapsed_ns <= 0:
            return
        if _request_sampled.get() is False:
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
        deterministic output.  All sample lists are copied under the
        lock so concurrent appends/evictions cannot mutate the
        snapshot during percentile computation.

        Includes ``sampled_count`` and ``unsampled_count`` so operators
        can interpret the sampling distribution (Plan 029, Workstream H).
        """
        with self._lock:
            keys = sorted(self._spans.keys())
            copied: list[tuple[str, list[int]]] = []
            for key in keys:
                state = self._spans[key]
                copied.append((key, list(state.samples)))
        rows: list[dict[str, Any]] = []
        for key, samples in copied:
            rows.append(
                _summarize_from_samples(key, self._window_size, samples).as_dict()
            )
        return {
            "window_size": self._window_size,
            "spans": rows,
            "sampled_count": self._sampled_count,
            "unsampled_count": self._unsampled_count,
        }

    def snapshot_for_spans(self, spans: list[str]) -> dict[str, Any]:
        """Snapshot only the requested span keys (safer for fixed schemas).

        Spans not present in the recorder appear with all numeric
        fields ``None`` and ``sample_count == 0`` so the caller can
        rely on every requested key being represented.  All sample
        lists are copied under the lock.
        """
        with self._lock:
            copied: dict[str, list[int]] = {}
            for key in sorted(set(spans)):
                state = self._spans.get(key)
                if state is not None:
                    copied[key] = list(state.samples)
                else:
                    copied[key] = []
        rows: list[dict[str, Any]] = []
        for key, samples in copied.items():
            rows.append(
                _summarize_from_samples(key, self._window_size, samples).as_dict()
            )
        return {
            "window_size": self._window_size,
            "spans": rows,
            "sampled_count": self._sampled_count,
            "unsampled_count": self._unsampled_count,
        }


def _summarize_from_samples(
    span: str, window_size: int, samples: list[int]
) -> _DispatchSpanStats:
    """Compute per-span percentile summary from a pre-copied sample list."""
    if not samples:
        return _DispatchSpanStats(
            span=span,
            window_size=window_size,
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
        window_size=window_size,
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
SPAN_AUTH = "auth"
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
SPAN_SELECTION_CLAIM_WAIT = "selection_claim_wait"
SPAN_SELECTION_CLAIM_HELD = "selection_claim_held"
SPAN_SELECTION_REVALIDATION = "selection_revalidation"
SPAN_DISPATCH_PERSISTENCE_TRANSACTION = "dispatch_persistence_transaction"
SPAN_DISPATCH_PERSISTENCE_COMMIT = "dispatch_persistence_commit"
SPAN_POST_COMMIT_PUBLICATION = "post_commit_publication"
SPAN_CLAIM_ROLLBACK = "claim_rollback"
SPAN_POST_COMMIT_COMPENSATION = "post_commit_compensation"
SPAN_THINKING_CLASSIFICATION = "thinking_classification"
SPAN_RESERVATION_ESTIMATE = "reservation_estimate"
SPAN_ROUTING_PLAN = "routing_plan"
SPAN_CIRCUIT_PROBE = "circuit_probe"
SPAN_ACCOUNT_LOOKUP = "account_lookup"
SPAN_DB_WRITE_REQUEST = "db_write_request"
SPAN_DB_WRITE_RESERVATION = "db_write_reservation"
SPAN_DB_WRITE_ATTEMPT = "db_write_attempt"
SPAN_ROUTING_TRACE_BUILD = "routing_trace_build"
SPAN_ROUTING_TRACE_WRITE = "routing_trace_write"
SPAN_RUNTIME_PUBLICATION = "runtime_publication"


ALL_SPAN_KEYS: tuple[str, ...] = (
    SPAN_AUTH,
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
    SPAN_SELECTION_CLAIM_WAIT,
    SPAN_SELECTION_CLAIM_HELD,
    SPAN_SELECTION_REVALIDATION,
    SPAN_DISPATCH_PERSISTENCE_TRANSACTION,
    SPAN_DISPATCH_PERSISTENCE_COMMIT,
    SPAN_POST_COMMIT_PUBLICATION,
    SPAN_CLAIM_ROLLBACK,
    SPAN_POST_COMMIT_COMPENSATION,
    SPAN_THINKING_CLASSIFICATION,
    SPAN_RESERVATION_ESTIMATE,
    SPAN_ROUTING_PLAN,
    SPAN_CIRCUIT_PROBE,
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
    "LocalPreUpstreamRecorder",
    "LocalPreUpstreamSnapshot",
    "SPAN_ACCOUNT_LOOKUP",
    "SPAN_AUTH",
    "SPAN_BODY_READ",
    "SPAN_CIRCUIT_PROBE",
    "SPAN_CLAIM_ROLLBACK",
    "SPAN_COMPRESSION_ANALYZE",
    "SPAN_COMPRESSION_APPLY",
    "SPAN_COMPRESSION_POLICY",
    "SPAN_CONTEXT_BUILD",
    "SPAN_CONTEXT_LIMIT",
    "SPAN_COORDINATOR_PRE_UPSTREAM",
    "SPAN_DB_WRITE_ATTEMPT",
    "SPAN_DB_WRITE_REQUEST",
    "SPAN_DB_WRITE_RESERVATION",
    "SPAN_DISPATCH_PERSISTENCE_COMMIT",
    "SPAN_DISPATCH_PERSISTENCE_TRANSACTION",
    "SPAN_JSON_PARSE",
    "SPAN_MODEL_PARSE",
    "SPAN_POST_COMMIT_COMPENSATION",
    "SPAN_POST_COMMIT_PUBLICATION",
    "SPAN_RESERVATION_ESTIMATE",
    "SPAN_ROUTING_PLAN",
    "SPAN_ROUTING_TRACE_BUILD",
    "SPAN_ROUTING_TRACE_WRITE",
    "SPAN_RUNTIME_PUBLICATION",
    "SPAN_SEGMENTATION",
    "SPAN_SELECTION_CLAIM_HELD",
    "SPAN_SELECTION_CLAIM_WAIT",
    "SPAN_SELECTION_REVALIDATION",
    "SPAN_THINKING_CLASSIFICATION",
    "SPAN_TRANSCODE_PREFLIGHT",
]
