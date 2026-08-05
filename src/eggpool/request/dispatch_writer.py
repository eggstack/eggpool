"""Process-owned microbatching persistence writer for dispatch intents.

Milestone C replaces per-request correctness-critical dispatch
transactions with a bounded in-process persistence pipeline.  This
module owns the :class:`DispatchPersistenceWriter` — a process-local,
single-drain-task writer that collects incoming
:class:`DispatchIntent` objects and persists them in microbatches.

Plan 029 — Dispatch Writer and Observability Bounds:

- All sample storage uses bounded ``deque(maxlen=sample_window)``.
- Metric semantics are precise: ``queue_age_ms`` (enqueue→claim),
  ``batch_formation_wait_ms`` (first intent→batch close),
  ``transaction_ms`` (one per batch, not per result), ``batch_size``
  (one per batch).
- Adaptive batching: low-pressure fast path (0–2 ms coalescing),
  moderate-pressure bounded wait, high-pressure max-size batch.
- Backpressure diagnostics: saturation count, submit timeout count,
  failed batches/intents, occupancy ratio, oldest intent age.
- Snapshot is bounded by ``sample_window`` and non-blocking.

Key design decisions:

- **Single drain task**: one long-running coroutine pulls from the
  queue, microbatches intents, and commits them atomically.
- **Adaptive microbatching**: waits for the first intent, then
  immediately drains all queued intents.  Under low pressure the
  single intent is persisted after a short coalescing delay;
  under moderate/high pressure the drain waits proportionally
  longer to reach a useful batch size.
- **Cancellation semantics**: each intent carries an ``asyncio.Event``.
  The caller may cancel before the writer claims the intent (skipped),
  after claim but before commit (completed then compensated), or after
  commit (result delivered, caller compensates).
- **Backpressure**: ``submit_intent`` blocks up to
  ``enqueue_timeout_ms`` when the queue is full, then raises
  :class:`DispatchQueueSaturatedError`.
- **Single-loop submission**: ``submit_intent`` accepts calls only from
  the event loop that started the writer. The canonical runtime has one
  event-loop thread, so no cross-loop adapter is required.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.db.dispatch_repository import persist_dispatch_bundles
from eggpool.request.dispatch_intent import (
    DispatchIntentCancelledError,
    DispatchQueueClosedError,
    DispatchQueueSaturatedError,
    DispatchTransactionError,
    DispatchWriterLoopError,
    DispatchWriterShutdownError,
    PersistedDispatchResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eggpool.db.connection import Database
    from eggpool.request.dispatch_intent import DispatchIntent

logger = logging.getLogger(__name__)

DEFAULT_MAX_QUEUE_DEPTH = 256
DEFAULT_MAX_BATCH_SIZE = 32
DEFAULT_MAX_BATCH_WAIT_MS = 50.0
DEFAULT_ENQUEUE_TIMEOUT_MS = 5_000.0
DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_S = 5.0
DEFAULT_SAMPLE_WINDOW = 2048
DEFAULT_LOW_PRESSURE_BATCH_WAIT_MS = 0.5
DEFAULT_HIGH_PRESSURE_BATCH_WAIT_MS = 5.0
_LOW_PRESSURE_THRESHOLD = 4
_HIGH_PRESSURE_THRESHOLD = 0.75


class _WriterState:
    """Internal lifecycle state for the writer."""

    INIT = "init"
    RUNNING = "running"
    DRAINING = "draining"
    CLOSED = "closed"


@dataclass(slots=True)
class _QueuedIntent:
    """Wrapper pairing an intent with its result future and metadata."""

    intent: DispatchIntent
    future: Future[PersistedDispatchResult]
    enqueue_mono_ns: int = field(default_factory=time.perf_counter_ns)


class DispatchPersistenceWriter:
    """Process-owned microbatching persistence writer.

    Constructed with a :class:`Database` reference (process-owned).
    The drain task runs on the event loop where :meth:`start` is called.

    The writer uses EggPool's canonical single-loop model. The drain task,
    queue, and all state mutations belong to the event loop where ``start``
    is called. Submission from another loop is rejected immediately.

    Shutdown behavior
    -----------------
    1. ``stop()`` sets state to DRAINING and awaits the drain task
       with a configurable timeout (default 5s).
    2. If the drain task does not complete, it is cancelled.
    3. All remaining queued intents are failed with
       ``DispatchWriterShutdownError``.
    4. The state transitions to CLOSED; further submissions raise
       ``DispatchQueueClosedError``.
    """

    def __init__(
        self,
        db: Database,
        *,
        max_queue_depth: int = DEFAULT_MAX_QUEUE_DEPTH,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        max_batch_wait_ms: float = DEFAULT_MAX_BATCH_WAIT_MS,
        enqueue_timeout_ms: float = DEFAULT_ENQUEUE_TIMEOUT_MS,
        shutdown_drain_timeout_s: float = DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_S,
        sample_window: int = DEFAULT_SAMPLE_WINDOW,
        low_pressure_batch_wait_ms: float = DEFAULT_LOW_PRESSURE_BATCH_WAIT_MS,
        high_pressure_batch_wait_ms: float = DEFAULT_HIGH_PRESSURE_BATCH_WAIT_MS,
    ) -> None:
        self._db = db
        self._max_queue_depth = max_queue_depth
        self._max_batch_size = max_batch_size
        self._max_batch_wait_ms = max_batch_wait_ms
        self._low_pressure_batch_wait_ms = low_pressure_batch_wait_ms
        self._high_pressure_batch_wait_ms = high_pressure_batch_wait_ms
        self._enqueue_timeout_ms = enqueue_timeout_ms
        self._shutdown_drain_timeout_s = shutdown_drain_timeout_s
        self._queue: asyncio.Queue[_QueuedIntent] = asyncio.Queue(
            maxsize=max_queue_depth
        )
        self._state = _WriterState.INIT
        self._drain_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._batch_counter = 0
        # Plan 029: bounded sample storage (Workstream B)
        self._sample_window = sample_window
        self._batch_sizes: deque[int] = deque(maxlen=sample_window)
        self._transaction_ms_samples: deque[float] = deque(maxlen=sample_window)
        self._queue_age_ms_samples: deque[float] = deque(maxlen=sample_window)
        self._batch_formation_wait_ms_samples: deque[float] = deque(
            maxlen=sample_window
        )
        self._queue_depth_samples: deque[int] = deque(maxlen=sample_window)
        # Plan 029 Workstream A: additional precise timing metrics
        self._enqueue_wait_ms_samples: deque[float] = deque(maxlen=sample_window)
        self._result_delivery_ms_samples: deque[float] = deque(maxlen=sample_window)
        self._intent_end_to_end_ms_samples: deque[float] = deque(maxlen=sample_window)
        # Plan 029: counters (Workstream C — one-sample-per-event)
        self._submitted_total = 0
        self._persisted_total = 0
        self._cancelled_total = 0
        self._cancelled_before_claim_total = 0
        self._cancelled_after_claim_total = 0
        self._cancelled_after_commit_total = 0
        self._failed_total = 0
        self._failed_batches_total = 0
        self._reconciliation_total = 0
        self._saturation_count = 0
        self._submit_timeout_count = 0
        self._last_batch_at: float | None = None
        self._last_batch_size: int | None = None
        self._oldest_intent_enqueued_at: float | None = None

    @property
    def state(self) -> str:
        return self._state

    def start(self) -> None:
        """Start the drain task on the current event loop."""
        if self._state != _WriterState.INIT:
            raise DispatchWriterShutdownError(
                f"Cannot start writer in state {self._state}"
            )
        self._state = _WriterState.RUNNING
        self._loop = asyncio.get_running_loop()
        self._drain_task = self._loop.create_task(self._drain_loop())

    async def stop(self) -> None:
        """Signal shutdown and wait for the drain task to finish."""
        if self._state not in (_WriterState.RUNNING, _WriterState.DRAINING):
            return
        self._state = _WriterState.DRAINING
        if self._drain_task is not None:
            # A drain loop blocked on an empty queue has no producer left
            # to wake it after the state transition.  Cancel that idle wait
            # immediately instead of consuming the full shutdown timeout.
            if self._queue.empty() and not self._drain_task.done():
                self._drain_task.cancel()
            try:
                await asyncio.wait_for(
                    self._drain_task,
                    timeout=self._shutdown_drain_timeout_s,
                )
            except TimeoutError:
                logger.warning(
                    "Dispatch writer drain task did not complete within %.1fs",
                    self._shutdown_drain_timeout_s,
                )
                self._drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._drain_task
            except asyncio.CancelledError:
                self._drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._drain_task
        self._state = _WriterState.CLOSED
        self._fail_all_queued(DispatchWriterShutdownError("Writer shut down"))

    def submit_intent(self, intent: DispatchIntent) -> Future[PersistedDispatchResult]:
        """Enqueue an intent and return a Future for its result.

        Requires the event loop that started the writer. Raises
        :class:`DispatchWriterLoopError` on cross-loop use and
        :class:`DispatchQueueClosedError` if the writer is not running, or
        :class:`DispatchQueueSaturatedError` if the queue is full
        and the enqueue timeout elapses.
        """
        if self._state != _WriterState.RUNNING:
            raise DispatchQueueClosedError(
                f"Writer is not running (state={self._state})"
            )
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            raise DispatchWriterLoopError(
                "DispatchPersistenceWriter must be submitted from its owner loop"
            )
        future: Future[PersistedDispatchResult] = Future()
        qi = _QueuedIntent(intent=intent, future=future)
        self._submitted_total += 1

        try:
            loop.call_soon(self._schedule_enqueue, qi)
        except RuntimeError as exc:
            self._failed_total += 1
            qi.future.set_exception(DispatchQueueClosedError("Event loop is closed"))
            raise DispatchQueueClosedError("Event loop is closed") from exc
        return future

    def _schedule_enqueue(
        self,
        qi: _QueuedIntent,
    ) -> None:
        """Create the async enqueue task on the writer's event loop."""
        assert self._loop is not None
        self._loop.create_task(self._enqueue_from_event_loop(qi))

    async def _enqueue_from_event_loop(self, qi: _QueuedIntent) -> None:
        """Actually place the intent on the queue from the event loop."""
        if self._state != _WriterState.RUNNING:
            qi.future.set_exception(
                DispatchQueueClosedError(f"Writer is not running (state={self._state})")
            )
            return
        enqueue_wait_start_ms = qi.enqueue_mono_ns / 1e6
        try:
            self._queue.put_nowait(qi)
            self._record_enqueue_wait(enqueue_wait_start_ms)
            self._update_oldest_intent(qi)
        except asyncio.QueueFull:
            self._saturation_count += 1
            enqueue_mono_ns = qi.enqueue_mono_ns
            deadline_s = (enqueue_mono_ns / 1e6) + self._enqueue_timeout_ms
            now_s = time.monotonic() * 1000
            remaining_ms = deadline_s - now_s
            if remaining_ms <= 0:
                self._submit_timeout_count += 1
                self._failed_total += 1
                self._record_intent_end_to_end(qi)
                qi.future.set_exception(
                    DispatchQueueSaturatedError(
                        f"Queue full (depth={self._queue.maxsize})"
                    )
                )
                return
            try:
                await asyncio.wait_for(
                    self._queue.put(qi),
                    timeout=remaining_ms / 1000.0,
                )
                self._record_enqueue_wait(enqueue_wait_start_ms)
                self._update_oldest_intent(qi)
            except TimeoutError:
                self._submit_timeout_count += 1
                self._failed_total += 1
                self._record_intent_end_to_end(qi)
                qi.future.set_exception(
                    DispatchQueueSaturatedError(
                        f"Queue full after {remaining_ms:.0f}ms"
                    )
                )

    def _update_oldest_intent(self, qi: _QueuedIntent) -> None:
        """Track the oldest enqueued intent for diagnostics."""
        if self._oldest_intent_enqueued_at is None:
            self._oldest_intent_enqueued_at = qi.enqueue_mono_ns / 1e6

    async def _drain_loop(self) -> None:
        """Main drain loop: collect intents and persist in microbatches."""
        while self._state == _WriterState.RUNNING:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                break

            if first.intent.cancelled.is_set():
                self._cancelled_total += 1
                self._cancelled_before_claim_total += 1
                first.future.set_exception(
                    DispatchIntentCancelledError(
                        f"Intent {first.intent.proxy_request_id} cancelled before claim"
                    )
                )
                continue

            batch = [first]
            formation_start = time.monotonic()
            self._record_queue_depth()
            self._drain_remaining(batch)
            await self._wait_for_batch(batch)
            batch_formation_ms = (time.monotonic() - formation_start) * 1000.0
            self._batch_formation_wait_ms_samples.append(batch_formation_ms)
            await self._persist_batch(batch)

        while not self._queue.empty():
            qi = self._queue.get_nowait()
            self._fail_one(qi, DispatchWriterShutdownError("Drain complete"))

    def _drain_remaining(self, batch: list[_QueuedIntent]) -> None:
        """Drain currently queued intents without waiting (non-blocking)."""
        while len(batch) < self._max_batch_size:
            try:
                qi = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if qi.intent.cancelled.is_set():
                self._cancelled_total += 1
                self._cancelled_before_claim_total += 1
                qi.future.set_exception(
                    DispatchIntentCancelledError(
                        f"Intent {qi.intent.proxy_request_id} cancelled before claim"
                    )
                )
                continue
            batch.append(qi)
            # Track queue age for each claimed intent (Workstream A)
            now_ms = time.monotonic() * 1000.0
            age_ms = now_ms - (qi.enqueue_mono_ns / 1e6)
            self._queue_age_ms_samples.append(age_ms)

    async def _wait_for_batch(self, batch: list[_QueuedIntent]) -> None:
        """Adaptive wait: low-pressure fast path, high-pressure max-size.

        Workstream F: adaptive batching policy.

        - Queue empty/low pressure: persist immediately or after a very
          small coalescing delay of 0–2 ms.
        - Moderate pressure: drain currently queued work and allow a short
          bounded wait to reach a useful batch size.
        - High pressure: batch up to maximum size without extra wait.
        - Near queue saturation: prioritize drain.
        """
        qsize = self._queue.qsize()
        occupancy = qsize / max(self._max_queue_depth, 1)

        # Low pressure: single intent, no more queued → persist immediately
        if len(batch) <= 1 and qsize < _LOW_PRESSURE_THRESHOLD:
            return

        # Near saturation: no extra wait, just drain what we can
        if occupancy >= _HIGH_PRESSURE_THRESHOLD:
            return

        # High pressure: batch to max size without extra wait
        if len(batch) >= self._max_batch_size:
            return

        # Adaptive wait selection (Workstream F)
        if occupancy < 0.25:
            wait_ms = self._low_pressure_batch_wait_ms
        elif occupancy < 0.5:
            wait_ms = (
                self._low_pressure_batch_wait_ms + self._high_pressure_batch_wait_ms
            ) / 2.0
        else:
            wait_ms = self._high_pressure_batch_wait_ms

        wait_s = wait_ms / 1000.0
        deadline = time.monotonic() + wait_s
        while len(batch) < self._max_batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                qi = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except TimeoutError:
                break
            if qi.intent.cancelled.is_set():
                self._cancelled_total += 1
                self._cancelled_before_claim_total += 1
                qi.future.set_exception(
                    DispatchIntentCancelledError(
                        f"Intent {qi.intent.proxy_request_id} cancelled before claim"
                    )
                )
                continue
            batch.append(qi)
            now_ms = time.monotonic() * 1000.0
            age_ms = now_ms - (qi.enqueue_mono_ns / 1e6)
            self._queue_age_ms_samples.append(age_ms)

    async def _persist_batch(self, batch: list[_QueuedIntent]) -> None:
        """Persist a batch of intents in a single transaction."""
        if not batch:
            return

        # Dispatch persistence is correctness-critical, but a failed-closed
        # worker cannot recover in process. Fail every waiter immediately so
        # request ownership remains explicit and no task waits for admission.
        if not self._db.writes_admitted:
            logger.warning(
                "Batch %d (%d intents) failed: writes not admitted",
                self._batch_counter + 1,
                len(batch),
            )
            error = DispatchTransactionError(
                "Writes not admitted; worker failed closed"
            )
            for qi in batch:
                self._record_intent_end_to_end(qi)
                self._fail_one(qi, error)
            return

        self._batch_counter += 1
        batch_id = self._batch_counter
        batch_size = len(batch)
        t0 = time.monotonic()

        intents = [qi.intent for qi in batch]

        try:
            results = await persist_dispatch_bundles(
                self._db, intents, batch_id=batch_id
            )
        except Exception as exc:
            self._failed_total += batch_size
            self._failed_batches_total += 1
            logger.warning(
                "Batch %d (%d intents) persistence failed: %s",
                batch_id,
                batch_size,
                exc,
            )
            error = DispatchTransactionError(f"Batch {batch_id} failed: {exc}")
            for qi in batch:
                self._record_intent_end_to_end(qi)
                self._fail_one(qi, error)
            return

        commit_complete_s = time.monotonic()
        commit_complete_ms = commit_complete_s * 1000.0
        batch_ms = (commit_complete_s - t0) * 1000.0
        self._last_batch_at = commit_complete_s
        self._last_batch_size = batch_size
        # Plan 029 Workstream C: one sample per batch, not per result
        self._batch_sizes.append(batch_size)
        self._transaction_ms_samples.append(batch_ms)

        # Reset oldest intent tracker after successful commit
        self._oldest_intent_enqueued_at = None

        for qi, result in zip(batch, results, strict=True):
            if qi.intent.cancelled.is_set():
                self._cancelled_total += 1
                self._cancelled_after_commit_total += 1
                self._record_intent_end_to_end(qi)
                qi.future.set_exception(
                    DispatchIntentCancelledError(
                        f"Intent {qi.intent.proxy_request_id} cancelled after commit"
                    )
                )
                continue

            self._persisted_total += 1
            qi.future.set_result(result)
            self._record_intent_end_to_end(qi)

        # Plan 029 Workstream A: result_delivery_ms — time from commit
        # completion to all result futures signaled.
        result_delivery_ms = time.monotonic() * 1000.0 - commit_complete_ms
        self._result_delivery_ms_samples.append(result_delivery_ms)

    def _fail_one(self, qi: _QueuedIntent, exc: BaseException) -> None:
        if not qi.future.done():
            qi.future.set_exception(exc)

    def _fail_all_queued(self, exc: BaseException) -> None:
        while not self._queue.empty():
            try:
                qi = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._fail_one(qi, exc)

    def _record_queue_depth(self) -> None:
        self._queue_depth_samples.append(self._queue.qsize())

    def _record_enqueue_wait(self, enqueue_wait_start_ms: float) -> None:
        """Record enqueue_wait_ms: time from submission to queue acceptance."""
        wait_ms = time.monotonic() * 1000.0 - enqueue_wait_start_ms
        if wait_ms >= 0:
            self._enqueue_wait_ms_samples.append(wait_ms)

    def _record_intent_end_to_end(self, qi: _QueuedIntent) -> None:
        """Record intent_end_to_end_ms: time from submission to result delivery.

        Called when the intent's future is resolved (success or failure).
        Uses the intent's enqueue_monotonic_ns as the submission anchor.
        """
        end_to_end_ms = time.monotonic() * 1000.0 - (qi.enqueue_mono_ns / 1e6)
        if end_to_end_ms >= 0:
            self._intent_end_to_end_ms_samples.append(end_to_end_ms)

    def record_reconciliation(self) -> None:
        """Record that an ambiguous commit reconciliation was attempted."""
        self._reconciliation_total += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostics snapshot of the writer state.

        Plan 029: all sample data comes from bounded deques.  Snapshot
        copies bounded data under the deque's natural ordering (no lock
        needed — single-loop model).  Percentile computation runs on
        the copied list and is O(sample_window) at worst.
        """
        batch_sizes = self._batch_sizes
        queue_depths = self._queue_depth_samples
        tx_ms = self._transaction_ms_samples
        queue_ages = self._queue_age_ms_samples
        formation_waits = self._batch_formation_wait_ms_samples
        enqueue_waits = self._enqueue_wait_ms_samples
        result_deliveries = self._result_delivery_ms_samples
        end_to_ends = self._intent_end_to_end_ms_samples
        qsize = self._queue.qsize()
        occupancy = qsize / max(self._max_queue_depth, 1)

        return {
            "state": self._state,
            "queue_depth": qsize,
            "max_queue_depth": self._max_queue_depth,
            "occupancy_ratio": round(occupancy, 4),
            "submitted_total": self._submitted_total,
            "persisted_total": self._persisted_total,
            "cancelled_total": self._cancelled_total,
            "cancelled_before_claim_total": self._cancelled_before_claim_total,
            "cancelled_after_claim_total": self._cancelled_after_claim_total,
            "cancelled_after_commit_total": self._cancelled_after_commit_total,
            "failed_total": self._failed_total,
            "failed_batches_total": self._failed_batches_total,
            "reconciliation_total": self._reconciliation_total,
            "saturation_count": self._saturation_count,
            "submit_timeout_count": self._submit_timeout_count,
            "batch_count": len(batch_sizes),
            "batch_size_p50": (_percentile(batch_sizes, 0.50) if batch_sizes else None),
            "batch_size_p95": (_percentile(batch_sizes, 0.95) if batch_sizes else None),
            "batch_size_max": (max(batch_sizes) if batch_sizes else None),
            "transaction_ms_p50": (_percentile(tx_ms, 0.50) if tx_ms else None),
            "transaction_ms_p95": (_percentile(tx_ms, 0.95) if tx_ms else None),
            "queue_age_ms_p50": (_percentile(queue_ages, 0.50) if queue_ages else None),
            "queue_age_ms_p95": (_percentile(queue_ages, 0.95) if queue_ages else None),
            "batch_formation_wait_ms_p50": (
                _percentile(formation_waits, 0.50) if formation_waits else None
            ),
            "batch_formation_wait_ms_p95": (
                _percentile(formation_waits, 0.95) if formation_waits else None
            ),
            "enqueue_wait_ms_p50": (
                _percentile(enqueue_waits, 0.50) if enqueue_waits else None
            ),
            "enqueue_wait_ms_p95": (
                _percentile(enqueue_waits, 0.95) if enqueue_waits else None
            ),
            "result_delivery_ms_p50": (
                _percentile(result_deliveries, 0.50) if result_deliveries else None
            ),
            "result_delivery_ms_p95": (
                _percentile(result_deliveries, 0.95) if result_deliveries else None
            ),
            "intent_end_to_end_ms_p50": (
                _percentile(end_to_ends, 0.50) if end_to_ends else None
            ),
            "intent_end_to_end_ms_p95": (
                _percentile(end_to_ends, 0.95) if end_to_ends else None
            ),
            "queue_depth_p50": (
                _percentile(queue_depths, 0.50) if queue_depths else None
            ),
            "queue_depth_max": (max(queue_depths) if queue_depths else None),
            "oldest_intent_age_ms": (
                (time.monotonic() * 1000.0 - self._oldest_intent_enqueued_at)
                if self._oldest_intent_enqueued_at is not None
                else None
            ),
            "last_batch_at": self._last_batch_at,
            "last_batch_size": self._last_batch_size,
            "sample_window": self._sample_window,
        }


def _percentile(samples: Sequence[int | float], pct: float) -> float:
    if not samples:
        return 0.0
    sorted_samples = sorted(samples)
    idx = int(pct * (len(sorted_samples) - 1))
    return float(sorted_samples[idx])
