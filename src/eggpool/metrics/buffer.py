"""In-memory metrics buffer that coalesces analytics events for batch flush."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eggpool.constants import clamp_sqlite_integer

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.db.rollup_repository import UsageRollupRepository
    from eggpool.models.config import MetricsConfig

logger = logging.getLogger(__name__)


def _compute_bucket_start(ts: datetime, bucket_size_s: int) -> str:
    """Compute the bucket start ISO timestamp for a given event timestamp."""
    epoch = int(ts.timestamp())
    bucket_epoch = (epoch // bucket_size_s) * bucket_size_s
    return datetime.fromtimestamp(bucket_epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class UsageMetricEvent:
    """Immutable analytics event emitted by the request finalizer."""

    timestamp: datetime
    provider_id: str
    model_id: str
    account_id: int | None
    protocol: str
    streamed: bool
    status: str
    retry_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    thinking_characters: int
    cost_microdollars: int
    bytes_received: int
    bytes_emitted: int
    latency_ms: int
    first_byte_ms: int | None


@dataclass(slots=True)
class _AggregatedDelta:
    """Mutable accumulator for a single rollup key."""

    request_count: int = 0
    error_count: int = 0
    retry_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    thinking_characters: int = 0
    cost_microdollars: int = 0
    bytes_received: int = 0
    bytes_emitted: int = 0
    latency_ms_sum: int = 0
    latency_ms_min: int | None = None
    latency_ms_max: int | None = None
    first_byte_ms_sum: int = 0
    first_byte_ms_count: int = 0

    def merge(self, event: UsageMetricEvent) -> None:
        """Merge a single event into this accumulator."""
        self.request_count += 1
        if event.status == "error":
            self.error_count += 1
        self.retry_count += event.retry_count
        self.input_tokens += event.input_tokens
        self.output_tokens += event.output_tokens
        self.cache_read_tokens += event.cache_read_tokens
        self.cache_write_tokens += event.cache_write_tokens
        self.reasoning_tokens += event.reasoning_tokens
        self.thinking_characters += event.thinking_characters
        self.cost_microdollars = clamp_sqlite_integer(
            self.cost_microdollars + event.cost_microdollars
        )
        self.bytes_received += event.bytes_received
        self.bytes_emitted += event.bytes_emitted
        self.latency_ms_sum += event.latency_ms

        if self.latency_ms_min is None or event.latency_ms < self.latency_ms_min:
            self.latency_ms_min = event.latency_ms
        if self.latency_ms_max is None or event.latency_ms > self.latency_ms_max:
            self.latency_ms_max = event.latency_ms

        if event.first_byte_ms is not None:
            self.first_byte_ms_sum += event.first_byte_ms
            self.first_byte_ms_count += 1


@dataclass(frozen=True, slots=True)
class _RollupKey:
    """Hashable key identifying one rollup row."""

    bucket_start: str
    bucket_size_s: int
    provider_id: str
    model_id: str
    account_id: int
    protocol: str
    streamed: int
    status: str


@dataclass(slots=True)
class FlushResult:
    """Result of a flush operation."""

    rows_flushed: int = 0
    duration_ms: int = 0
    error_class: str | None = None


@dataclass(slots=True)
class MetricsBufferSnapshot:
    """Diagnostic snapshot of the coalescer's in-memory state."""

    write_mode: str
    flush_interval_s: int
    buffered_keys: int
    buffered_events: int
    total_events_received: int
    total_events_flushed: int
    total_events_dropped: int
    last_flush_ts: float | None
    last_flush_rows: int
    last_flush_duration_ms: int
    last_flush_error: str | None


class MetricsWriteCoalescer:
    """Buffers analytics events in memory and flushes to usage_rollups.

    Thread-safety: ``record_usage()`` is safe to call from any async task.
    It acquires a lock only to update the in-memory buffer (no I/O).
    ``flush()`` acquires the lock briefly to snapshot and clear the buffer,
    then performs database I/O outside the lock so ``record_usage()`` is
    never blocked by slow writes.
    """

    def __init__(
        self,
        *,
        config: MetricsConfig,
        db: Database,
        rollup_repo: UsageRollupRepository,
    ) -> None:
        self._write_mode = config.write_mode
        self._flush_interval_s = config.flush_interval_s
        self._max_buffered = config.max_buffered_events
        self._bucket_size_s = config.timeseries_bucket_s
        self._aggregate_only = config.aggregate_only
        self._db = db
        self._rollup_repo = rollup_repo

        self._buffer: dict[_RollupKey, _AggregatedDelta] = {}
        self._pending_events = 0
        self._lock = asyncio.Lock()

        # Diagnostics
        self._total_received = 0
        self._total_flushed = 0
        self._total_dropped = 0
        self._last_flush_ts: float | None = None
        self._last_flush_rows = 0
        self._last_flush_duration_ms = 0
        self._last_flush_error: str | None = None

    @property
    def write_mode(self) -> str:
        return self._write_mode

    def record_usage(self, event: UsageMetricEvent) -> None:
        """Record an analytics event into the in-memory buffer.

        This is non-blocking and never awaits database I/O. If the buffer
        is full, the event is dropped and counted in diagnostics.
        """
        if self._write_mode == "immediate":
            return

        bucket_start = _compute_bucket_start(event.timestamp, self._bucket_size_s)
        key = _RollupKey(
            bucket_start=bucket_start,
            bucket_size_s=self._bucket_size_s,
            provider_id=event.provider_id,
            model_id=event.model_id,
            account_id=event.account_id if event.account_id is not None else 0,
            protocol=event.protocol,
            streamed=1 if event.streamed else 0,
            status=event.status,
        )

        # Use a simple non-blocking check: if buffer is oversized, drop.
        # The lock is only held briefly for the dict update.
        if len(self._buffer) >= self._max_buffered and key not in self._buffer:
            self._total_dropped += 1
            logger.debug(
                "Metrics buffer full (%d keys), dropping event for %s/%s",
                len(self._buffer),
                event.provider_id,
                event.model_id,
            )
            return

        if key not in self._buffer:
            self._buffer[key] = _AggregatedDelta()
        self._buffer[key].merge(event)
        self._pending_events += 1
        self._total_received += 1

    async def flush(self, reason: str = "periodic") -> FlushResult:
        """Flush buffered analytics to the database.

        Returns a FlushResult with diagnostics. Errors are caught and
        reported — the caller is never expected to handle flush failures
        for lossy analytics.
        """
        if not self._buffer:
            return FlushResult()

        # Snapshot and clear the buffer under the lock
        async with self._lock:
            buffer_snapshot = self._buffer
            event_count = self._pending_events
            self._buffer = {}
            self._pending_events = 0

        start = time.monotonic()
        try:
            rows = self._build_rollup_rows(buffer_snapshot)
            if rows:
                await self._rollup_repo.upsert_many(rows)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._last_flush_ts = time.time()
            self._last_flush_rows = len(rows)
            self._last_flush_duration_ms = elapsed_ms
            self._last_flush_error = None
            self._total_flushed += event_count
            logger.debug(
                "Metrics flush (%s): %d rows, %d events, %dms",
                reason,
                len(rows),
                event_count,
                elapsed_ms,
            )
            return FlushResult(
                rows_flushed=len(rows),
                duration_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            error_class = type(exc).__name__
            self._last_flush_ts = time.time()
            self._last_flush_rows = 0
            self._last_flush_duration_ms = elapsed_ms
            self._last_flush_error = error_class
            logger.exception("Metrics flush failed (%s)", reason)
            return FlushResult(
                rows_flushed=0,
                duration_ms=elapsed_ms,
                error_class=error_class,
            )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Background loop: flush periodically until stop_event is set."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._flush_interval_s
                )
                break  # stop_event was set
            except TimeoutError:
                pass
            await self.flush(reason="periodic")

        # Final flush on shutdown
        await self.flush(reason="shutdown")

    def snapshot(self) -> dict[str, object]:
        """Return a diagnostic snapshot of the coalescer's state."""
        return {
            "write_mode": self._write_mode,
            "flush_interval_s": self._flush_interval_s,
            "aggregate_only": self._aggregate_only,
            "buffered_keys": len(self._buffer),
            "buffered_events": self._pending_events,
            "total_events_received": self._total_received,
            "total_events_flushed": self._total_flushed,
            "total_events_dropped": self._total_dropped,
            "last_flush_ts": self._last_flush_ts,
            "last_flush_rows": self._last_flush_rows,
            "last_flush_duration_ms": self._last_flush_duration_ms,
            "last_flush_error": self._last_flush_error,
        }

    def _build_rollup_rows(
        self, buffer: dict[_RollupKey, _AggregatedDelta]
    ) -> list[dict[str, object]]:
        """Convert aggregated deltas into rollup row dicts for upsert."""
        rows: list[dict[str, object]] = []
        for key, delta in buffer.items():
            rows.append(
                {
                    "bucket_start": key.bucket_start,
                    "bucket_size_s": key.bucket_size_s,
                    "provider_id": key.provider_id,
                    "model_id": key.model_id,
                    "account_id": key.account_id,
                    "protocol": key.protocol,
                    "streamed": key.streamed,
                    "status": key.status,
                    "request_count": delta.request_count,
                    "error_count": delta.error_count,
                    "retry_count": delta.retry_count,
                    "input_tokens": delta.input_tokens,
                    "output_tokens": delta.output_tokens,
                    "cache_read_tokens": delta.cache_read_tokens,
                    "cache_write_tokens": delta.cache_write_tokens,
                    "reasoning_tokens": delta.reasoning_tokens,
                    "thinking_characters": delta.thinking_characters,
                    "cost_microdollars": clamp_sqlite_integer(delta.cost_microdollars),
                    "bytes_received": delta.bytes_received,
                    "bytes_emitted": delta.bytes_emitted,
                    "latency_ms_sum": delta.latency_ms_sum,
                    "latency_ms_min": delta.latency_ms_min,
                    "latency_ms_max": delta.latency_ms_max,
                    "first_byte_ms_sum": delta.first_byte_ms_sum,
                    "first_byte_ms_count": delta.first_byte_ms_count,
                }
            )
        return rows
