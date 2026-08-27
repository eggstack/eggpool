"""Asynchronous background writer for routing-decision trace events.

:class:`RoutingTraceWriter` is a process-owned, single-drain-task writer
that collects immutable :class:`RoutingTraceEvent` objects via a
non-blocking :meth:`submit` and persists them in micro-batches using
:class:`RoutingDecisionRepository`.

Key design decisions:

- **Bounded queue**: ``collections.deque(maxlen=queue_capacity)``
  drops the *newest* event when full (checked before append).
- **Thread-safe submission**: ``submit`` uses :class:`threading.Lock`
  so callers from any thread or event loop can safely enqueue.
- **Single drain task**: one long-running coroutine pulls from the
  queue and writes bounded batches to the database.
- **Silent failures**: every exception is swallowed and its counter
  incremented — the writer never raises.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eggpool import jsonx

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eggpool.db.connection import Database
    from eggpool.db.repositories import RoutingDecisionRepository

logger = logging.getLogger(__name__)

__all__ = [
    "RoutingTraceEvent",
    "RoutingTraceWriter",
]


# ---------------------------------------------------------------------------
# Frozen event payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutingTraceEvent:
    """Immutable trace payload submitted to :class:`RoutingTraceWriter`."""

    request_id: str
    db_request_id: int
    attempt_number: int
    model_id: str
    provider_id: str | None
    protocol: str | None
    selected_account_name: str | None
    selected_account_id: int | None
    selected_tier: int | None
    selected_score: float | None
    eligible_count: int
    scored_count: int
    attempted_excluded_count: int
    top_score: float | None
    top_score_account_name: str | None
    exclude_reasons_json: str
    score_components_json: str | None
    created_at_mono_ns: int
    created_at_epoch: float
    generation_id: int | None
    payload_version: int = 1

    def to_row_tuple(self) -> tuple[Any, ...]:
        """Return the 16-value tuple for ``INSERT INTO routing_decisions``."""
        return (
            self.db_request_id,
            self.attempt_number,
            self.model_id,
            self.provider_id,
            self.protocol,
            self.selected_account_id,
            self.selected_account_name,
            self.selected_tier,
            self.selected_score,
            self.eligible_count,
            self.scored_count,
            self.attempted_excluded_count,
            self.top_score,
            self.top_score_account_name,
            self.exclude_reasons_json,
            self.score_components_json
            if self.score_components_json is not None
            else "{}",
        )

    def to_json_bytes(self) -> bytes:
        """Compact JSON serialization via ``eggpool.jsonx``."""
        return jsonx.dumps_bytes(
            {
                "request_id": self.request_id,
                "db_request_id": self.db_request_id,
                "attempt_number": self.attempt_number,
                "model_id": self.model_id,
                "provider_id": self.provider_id,
                "protocol": self.protocol,
                "selected_account_name": self.selected_account_name,
                "selected_account_id": self.selected_account_id,
                "selected_tier": self.selected_tier,
                "selected_score": self.selected_score,
                "eligible_count": self.eligible_count,
                "scored_count": self.scored_count,
                "attempted_excluded_count": self.attempted_excluded_count,
                "top_score": self.top_score,
                "top_score_account_name": self.top_score_account_name,
                "exclude_reasons_json": self.exclude_reasons_json,
                "score_components_json": self.score_components_json,
                "created_at_mono_ns": self.created_at_mono_ns,
                "created_at_epoch": self.created_at_epoch,
                "generation_id": self.generation_id,
                "payload_version": self.payload_version,
            }
        )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

_STATE_INIT = "init"
_STATE_RUNNING = "running"
_STATE_STOPPING = "stopping"
_STATE_CLOSED = "closed"


class RoutingTraceWriter:
    """Process-owned background writer for routing-decision traces.

    Constructed with a :class:`Database` and a
    :class:`RoutingDecisionRepository`.  Call :meth:`start` on the
    running event loop to launch the drain task.

    Thread-safety strategy (Milestone F)
    -------------------------------------
    The writer uses a ``threading.Lock``-protected ``collections.deque``
    for the submission queue and a ``threading.Event`` for shutdown
    signalling.  Both are thread-safe by construction.

    ``submit()`` is safe to call from any thread or event loop — it
    acquires the ``threading.Lock`` only for the brief deque append.
    The drain task runs on the event loop where ``start()`` was called.

    Single-loop assumption
    ----------------------
    Under Model 1 (single Granian worker, ``runtime_threads=1``),
    the drain task runs on the single event loop.  The
    ``threading.Lock`` protects against concurrent ``submit()`` calls
    from Granian's runtime threads (e.g. the coordinator's
    ``call_soon_threadsafe`` path).

    When ``runtime_threads > 1``, the drain task still runs on one
    specific loop; ``submit()`` remains safe from any thread because
    it only touches the lock-protected deque (no asyncio primitives
    in the submission path).

    Shutdown behavior
    -----------------
    1. ``stop()`` sets the shutdown flag and wakes the drain task.
    2. The drain task finishes its current batch, then exits.
    3. Any remaining events are flushed with a bounded timeout.
    4. Events that cannot be flushed within the timeout are dropped
       and counted in ``_dropped_shutdown_timeout``.
    5. The state transitions to CLOSED; further submissions return
       ``'dropped_writer_unavailable'`` without raising.
    """

    def __init__(
        self,
        db: Database,
        routing_decision_repo: RoutingDecisionRepository,
        *,
        queue_capacity: int = 1000,
        flush_interval_s: float = 1.0,
        max_batch_size: int = 50,
        shutdown_flush_timeout_s: float = 5.0,
    ) -> None:
        self._db = db
        self._repo = routing_decision_repo
        self._queue_capacity = queue_capacity
        self._flush_interval_s = flush_interval_s
        self._max_batch_size = max_batch_size
        self._shutdown_flush_timeout_s = shutdown_flush_timeout_s

        self._queue: collections.deque[RoutingTraceEvent] = collections.deque(
            maxlen=queue_capacity,
        )
        self._lock = threading.Lock()
        self._state = _STATE_INIT
        self._drain_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._shutdown_flag = threading.Event()

        # Configurable tracing knobs
        self._mode: str | None = None
        self._sample_rate: float | None = None

        # Counters
        self._accepted = 0
        self._written = 0
        self._dropped_queue_full = 0
        self._dropped_writer_unavailable = 0
        self._dropped_flush_error = 0
        self._dropped_shutdown_timeout = 0
        self._dropped_serialization = 0
        self._dropped_stale_parent = 0
        self._dropped_mode_off = 0
        self._dropped_sampling_exclusion = 0

    # -- lifecycle ----------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        """Return ``True`` when the drain task is running."""
        with self._lock:
            return self._state == _STATE_RUNNING

    def start(self) -> None:
        """Start the drain task on the current event loop."""
        with self._lock:
            if self._state != _STATE_INIT:
                return
            self._state = _STATE_RUNNING
            self._loop = asyncio.get_running_loop()
            self._drain_task = self._loop.create_task(self._drain_loop())

    async def stop(self, *, timeout_s: float | None = None) -> None:
        """Signal shutdown, flush remaining events, then close."""
        with self._lock:
            if self._state not in (_STATE_RUNNING,):
                return
            self._state = _STATE_STOPPING
            self._shutdown_flag.set()

        self._wake.set()

        if self._drain_task is not None:
            try:
                await asyncio.wait_for(
                    self._drain_task,
                    timeout=timeout_s or 2.0,
                )
            except (TimeoutError, asyncio.CancelledError):
                self._drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._drain_task

        remaining = self._drain_queue()
        if remaining:
            try:
                await asyncio.wait_for(
                    self._write_batch(remaining),
                    timeout=self._shutdown_flush_timeout_s,
                )
            except (TimeoutError, asyncio.CancelledError):
                self._dropped_shutdown_timeout += len(remaining)

        with self._lock:
            self._state = _STATE_CLOSED

    # -- public API ---------------------------------------------------------

    def submit(self, event: RoutingTraceEvent) -> str:
        """Submit a trace event for async persistence.

        Returns one of ``'accepted'``, ``'dropped_queue_full'``, or
        ``'dropped_writer_unavailable'``.
        """
        with self._lock:
            state = self._state
            if state != _STATE_RUNNING:
                self._dropped_writer_unavailable += 1
                return "dropped_writer_unavailable"
            if self._mode == "off":
                self._dropped_mode_off += 1
                return "dropped_mode_off"
            if len(self._queue) >= self._queue_capacity:
                self._dropped_queue_full += 1
                return "dropped_queue_full"
            self._queue.append(event)
            self._accepted += 1
            loop = self._loop
            wake = self._wake

        if loop is not None and loop.is_running():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(wake.set)
        return "accepted"

    def configure(
        self,
        *,
        mode: str | None = None,
        sample_rate: float | None = None,
    ) -> None:
        """Update tracing configuration at runtime."""
        with self._lock:
            if mode is not None:
                self._mode = mode
            if sample_rate is not None:
                self._sample_rate = sample_rate

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostics snapshot."""
        with self._lock:
            queue_depth = len(self._queue)
            oldest_mono = self._queue[0].created_at_mono_ns if queue_depth else None
            state = self._state

        now_mono = time.monotonic_ns()
        oldest_age_s: float | None = None
        if oldest_mono is not None:
            oldest_age_s = (now_mono - oldest_mono) / 1e9

        return {
            "alive": state == _STATE_RUNNING,
            "state": state,
            "queue_capacity": self._queue_capacity,
            "queue_depth": queue_depth,
            "oldest_event_age_s": oldest_age_s,
            "accepted": self._accepted,
            "written": self._written,
            "dropped_queue_full": self._dropped_queue_full,
            "dropped_writer_unavailable": self._dropped_writer_unavailable,
            "dropped_flush_error": self._dropped_flush_error,
            "dropped_shutdown_timeout": self._dropped_shutdown_timeout,
            "dropped_serialization": self._dropped_serialization,
            "dropped_stale_parent": self._dropped_stale_parent,
            "dropped_mode_off": self._dropped_mode_off,
            "dropped_sampling_exclusion": self._dropped_sampling_exclusion,
        }

    # -- internals ----------------------------------------------------------

    def _drain_queue(self) -> list[RoutingTraceEvent]:
        """Non-blocking drain of up to ``_max_batch_size`` events."""
        batch: list[RoutingTraceEvent] = []
        with self._lock:
            while self._queue and len(batch) < self._max_batch_size:
                batch.append(self._queue.popleft())
        return batch

    async def _drain_loop(self) -> None:
        """Main drain loop: wait for wake signals, then batch-write."""
        while not self._shutdown_flag.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._flush_interval_s,
                )
            self._wake.clear()

            batch = self._drain_queue()
            if batch:
                await self._write_batch(batch)

        # Drain anything left after shutdown flag
        while True:
            batch = self._drain_queue()
            if not batch:
                break
            try:
                await asyncio.wait_for(
                    self._write_batch(batch),
                    timeout=self._shutdown_flush_timeout_s,
                )
            except (TimeoutError, asyncio.CancelledError):
                self._dropped_shutdown_timeout += len(batch)
                break

    async def _write_batch(self, batch: Sequence[RoutingTraceEvent]) -> None:
        """Persist a batch of events via ``RoutingDecisionRepository``."""
        if not batch:
            return
        # Traces are diagnostic and must be dropped immediately when the
        # worker has failed closed.
        if not self._db.writes_admitted:
            self._dropped_flush_error += len(batch)
            return
        rows: list[tuple[Any, ...]] = []
        for event in batch:
            try:
                rows.append(event.to_row_tuple())
            except Exception:  # noqa: BLE001
                self._dropped_serialization += 1
        if not rows:
            return
        try:
            count = await self._repo.create_many(rows)
            self._written += count
        except Exception:  # noqa: BLE001
            self._dropped_flush_error += len(rows)
            logger.debug(
                "routing_trace_write_batch_failed count=%d",
                len(rows),
                exc_info=True,
            )
