"""Process-local stream outcome diagnostics.

Captures counters and small histograms for terminal streaming paths so
operators can correlate OpenCode-visible stream drops / `Failed to execute
statement` reports with EggPool-side state.

All counters are in-memory only; they reset on process restart.  They are
intended for runtime diagnostics surfaced through the ``/api/stats/runtime``
endpoint and the dashboard runtime page, not for billing or alerting.

The module is intentionally tiny and lock-free on the hot path so it does
not add measurable overhead to the request path under high concurrency.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Canonical stream outcome labels.  These are the only values ever
# passed to :meth:`StreamDiagnostics.record_outcome` so the dashboard
# can rely on a fixed key set.
STREAM_OUTCOME_COMPLETED = "stream_completed"
STREAM_OUTCOME_CLIENT_CANCELLED = "client_cancelled"
STREAM_OUTCOME_DOWNSTREAM_SEND_CANCELLED = "downstream_send_cancelled"
STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR = "upstream_midstream_error"
STREAM_OUTCOME_FINALIZER_TIMEOUT = "stream_finalizer_timeout"
STREAM_OUTCOME_FINALIZER_FAILED = "stream_finalizer_failed"
STREAM_OUTCOME_USAGE_MISSING_FINAL_EVENT = "stream_usage_missing_final_event"


@dataclass
class StreamOutcomeEvent:
    """Structured diagnostic event for a terminal streaming path.

    Field names match the runtime JSON contract and are kept stable for
    operator-facing log scrapers.  All fields are best-effort and never
    contain request bodies, API keys, prompts, or upstream chunks.
    """

    outcome: str
    proxy_request_id: str | None = None
    db_request_id: str | None = None
    provider_id: str | None = None
    account_name: str | None = None
    model_id: str | None = None
    protocol: str | None = None
    elapsed_ms: int | None = None
    bytes_emitted: int | None = None
    first_byte_ms: int | None = None
    upstream_connect_ms: int | None = None
    upstream_header_ms: int | None = None
    upstream_read_ms: int | None = None
    attempt: int | None = None
    exception_class: str | None = None


@dataclass
class _RingHistogram:
    """Bounded lock-free histogram of recent samples.

    A small bounded ring buffer keeps the last N samples in memory so
    p50 / p95 / p99 can be reported without persistent storage.
    Snapshots take a lock only when producing sorted output.
    """

    capacity: int = 256
    _samples_ms: deque[float] = field(default_factory=lambda: deque(maxlen=256))

    def __post_init__(self) -> None:
        # Resize the deque to honour the configured capacity (dataclass
        # default expression captured before __init__ runs).
        if self._samples_ms.maxlen != self.capacity:
            self._samples_ms = deque(self._samples_ms, maxlen=self.capacity)

    def record(self, value_ms: float) -> None:
        if value_ms < 0:
            value_ms = 0.0
        self._samples_ms.append(float(value_ms))

    def snapshot(self) -> dict[str, float | int | None]:
        size = len(self._samples_ms)
        if size == 0:
            return {
                "sample_count": 0,
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
                "max_ms": None,
            }
        samples = sorted(self._samples_ms)
        return {
            "sample_count": size,
            "p50_ms": round(samples[int(0.50 * (size - 1))], 3),
            "p95_ms": round(samples[int(0.95 * (size - 1))], 3),
            "p99_ms": round(samples[min(int(0.99 * (size - 1)), size - 1)], 3),
            "max_ms": round(samples[-1], 3),
        }


class StreamDiagnostics:
    """Process-local stream outcome and contention counter service.

    Thread-safe under a single ``threading.Lock``.  The hot-path
    :meth:`record_outcome` and :meth:`record_finalizer_timeout` paths
    only mutate in-memory counters; the lock is held briefly.

    Snapshot access is read-only and intended for
    :class:`~eggpool.runtime_metrics.RuntimeMetricsService` callers.
    """

    def __init__(self, *, histogram_capacity: int = 256) -> None:
        self._lock = threading.Lock()
        self._outcomes: dict[str, int] = {
            STREAM_OUTCOME_COMPLETED: 0,
            STREAM_OUTCOME_CLIENT_CANCELLED: 0,
            STREAM_OUTCOME_DOWNSTREAM_SEND_CANCELLED: 0,
            STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR: 0,
            STREAM_OUTCOME_FINALIZER_TIMEOUT: 0,
            STREAM_OUTCOME_FINALIZER_FAILED: 0,
            STREAM_OUTCOME_USAGE_MISSING_FINAL_EVENT: 0,
        }
        self._httpx_exception_counts: dict[str, int] = {}
        self._upstream_error_class_counts: dict[str, int] = {}
        self._last_event: StreamOutcomeEvent | None = None
        self._last_event_at_monotonic: float | None = None
        self._finalizer_timeout_ms = _RingHistogram(capacity=histogram_capacity)
        self._client_cancel_ms = _RingHistogram(capacity=histogram_capacity)
        self._completed_ms = _RingHistogram(capacity=histogram_capacity)

    def record_outcome(
        self,
        outcome: str,
        *,
        proxy_request_id: str | None = None,
        db_request_id: str | None = None,
        provider_id: str | None = None,
        account_name: str | None = None,
        model_id: str | None = None,
        protocol: str | None = None,
        elapsed_ms: int | None = None,
        bytes_emitted: int | None = None,
        first_byte_ms: int | None = None,
        upstream_connect_ms: int | None = None,
        upstream_header_ms: int | None = None,
        upstream_read_ms: int | None = None,
        attempt: int | None = None,
        exception_class: str | None = None,
    ) -> None:
        """Record a terminal streaming outcome.

        Best-effort: never raises.  Unrecognised outcome labels fall
        back to ``unknown`` so the rest of the counters stay sane.
        """
        try:
            event = StreamOutcomeEvent(
                outcome=outcome,
                proxy_request_id=proxy_request_id,
                db_request_id=db_request_id,
                provider_id=provider_id,
                account_name=account_name,
                model_id=model_id,
                protocol=protocol,
                elapsed_ms=elapsed_ms,
                bytes_emitted=bytes_emitted,
                first_byte_ms=first_byte_ms,
                upstream_connect_ms=upstream_connect_ms,
                upstream_header_ms=upstream_header_ms,
                upstream_read_ms=upstream_read_ms,
                attempt=attempt,
                exception_class=exception_class,
            )
            with self._lock:
                if outcome in self._outcomes:
                    self._outcomes[outcome] = self._outcomes[outcome] + 1
                else:
                    self._outcomes["unknown"] = self._outcomes.get("unknown", 0) + 1
                if exception_class is not None:
                    if outcome == STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR:
                        key = exception_class
                        self._upstream_error_class_counts[key] = (
                            self._upstream_error_class_counts.get(key, 0) + 1
                        )
                    else:
                        key = exception_class
                        self._httpx_exception_counts[key] = (
                            self._httpx_exception_counts.get(key, 0) + 1
                        )
                if elapsed_ms is not None and elapsed_ms >= 0:
                    if outcome == STREAM_OUTCOME_COMPLETED:
                        self._completed_ms.record(float(elapsed_ms))
                    elif outcome in (
                        STREAM_OUTCOME_CLIENT_CANCELLED,
                        STREAM_OUTCOME_DOWNSTREAM_SEND_CANCELLED,
                    ):
                        self._client_cancel_ms.record(float(elapsed_ms))
                    elif outcome == STREAM_OUTCOME_FINALIZER_TIMEOUT:
                        self._finalizer_timeout_ms.record(float(elapsed_ms))
                self._last_event = event
                self._last_event_at_monotonic = time.monotonic()
        except Exception:
            logger.debug("Failed to record stream outcome", exc_info=True)

    def record_finalizer_timeout(self, *, elapsed_ms: int | None = None) -> None:
        """Convenience helper for finalizer-timeout outcomes."""
        self.record_outcome(STREAM_OUTCOME_FINALIZER_TIMEOUT, elapsed_ms=elapsed_ms)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            outcomes = dict(self._outcomes)
            httpx_counts = dict(self._httpx_exception_counts)
            upstream_counts = dict(self._upstream_error_class_counts)
            last_event = self._last_event
            last_event_at = self._last_event_at_monotonic
            completed_hist = self._completed_ms.snapshot()
            client_cancel_hist = self._client_cancel_ms.snapshot()
            finalizer_timeout_hist = self._finalizer_timeout_ms.snapshot()
        last_event_dict: dict[str, Any] | None
        if last_event is None:
            last_event_dict = None
        else:
            last_event_dict = {
                "outcome": last_event.outcome,
                "proxy_request_id": last_event.proxy_request_id,
                "db_request_id": last_event.db_request_id,
                "provider_id": last_event.provider_id,
                "account_name": last_event.account_name,
                "model_id": last_event.model_id,
                "protocol": last_event.protocol,
                "elapsed_ms": last_event.elapsed_ms,
                "bytes_emitted": last_event.bytes_emitted,
                "first_byte_ms": last_event.first_byte_ms,
                "upstream_connect_ms": last_event.upstream_connect_ms,
                "upstream_header_ms": last_event.upstream_header_ms,
                "upstream_read_ms": last_event.upstream_read_ms,
                "attempt": last_event.attempt,
                "exception_class": last_event.exception_class,
            }
        return {
            "outcomes": outcomes,
            "httpx_exception_counts": httpx_counts,
            "upstream_error_class_counts": upstream_counts,
            "completed_ms": completed_hist,
            "client_cancel_ms": client_cancel_hist,
            "finalizer_timeout_ms": finalizer_timeout_hist,
            "last_event": last_event_dict,
            "last_event_age_ms": (
                round((time.monotonic() - last_event_at) * 1000, 1)
                if last_event_at is not None
                else None
            ),
        }


# Module-level singleton — convenient for the coordinator and tests.
_default_diagnostics: StreamDiagnostics | None = None
_default_diagnostics_lock = threading.Lock()


def get_stream_diagnostics() -> StreamDiagnostics:
    """Return the process-wide :class:`StreamDiagnostics` instance."""
    global _default_diagnostics
    with _default_diagnostics_lock:
        if _default_diagnostics is None:
            _default_diagnostics = StreamDiagnostics()
        return _default_diagnostics


def reset_stream_diagnostics_for_tests() -> StreamDiagnostics:
    """Reset the module-level singleton; for tests only."""
    global _default_diagnostics
    with _default_diagnostics_lock:
        _default_diagnostics = StreamDiagnostics()
        return _default_diagnostics
