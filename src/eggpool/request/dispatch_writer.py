"""Process-owned microbatching persistence writer for dispatch intents.

Milestone C replaces per-request correctness-critical dispatch
transactions with a bounded in-process persistence pipeline.  This
module owns the :class:`DispatchPersistenceWriter` — a process-local,
single-drain-task writer that collects incoming
:class:`DispatchIntent` objects and persists them in microbatches.

Key design decisions:

- **Single drain task**: one long-running coroutine pulls from the
  queue, microbatches intents, and commits them atomically.
- **Adaptive microbatching**: waits for the first intent, then
  immediately drains all queued intents.  If the batch size is 1
  and queue pressure is low the single intent is persisted immediately;
  otherwise the drain waits up to ``max_batch_wait_ms`` to accumulate
  more work.
- **Cancellation semantics**: each intent carries an ``asyncio.Event``.
  The caller may cancel before the writer claims the intent (skipped),
  after claim but before commit (completed then compensated), or after
  commit (result delivered, caller compensates).
- **Backpressure**: ``submit_intent`` blocks up to
  ``enqueue_timeout_ms`` when the queue is full, then raises
  :class:`DispatchQueueSaturatedError`.
- **Thread-safe submission**: ``submit_intent`` bridges
  ``concurrent.futures.Future`` so callers from different event loops
  (e.g. Granian worker threads) can safely enqueue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.db.dispatch_repository import persist_dispatch_bundles
from eggpool.request.dispatch_intent import (
    DispatchIntentCancelledError,
    DispatchQueueClosedError,
    DispatchQueueSaturatedError,
    DispatchTransactionError,
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
_LOW_PRESSURE_THRESHOLD = 4


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
    ) -> None:
        self._db = db
        self._max_queue_depth = max_queue_depth
        self._max_batch_size = max_batch_size
        self._max_batch_wait_ms = max_batch_wait_ms
        self._enqueue_timeout_ms = enqueue_timeout_ms
        self._shutdown_drain_timeout_s = shutdown_drain_timeout_s
        self._queue: asyncio.Queue[_QueuedIntent] = asyncio.Queue(
            maxsize=max_queue_depth
        )
        self._state = _WriterState.INIT
        self._drain_task: asyncio.Task[None] | None = None
        self._batch_counter = 0
        # Diagnostics counters
        self._submitted_total = 0
        self._persisted_total = 0
        self._cancelled_total = 0
        self._cancelled_before_claim_total = 0
        self._cancelled_after_commit_total = 0
        self._failed_total = 0
        self._reconciliation_total = 0
        self._batch_sizes: list[int] = []
        self._batch_wait_ms_samples: list[float] = []
        self._transaction_ms_samples: list[float] = []
        self._queue_depth_samples: list[int] = []
        self._last_batch_at: float | None = None
        self._last_batch_size: int | None = None

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
        self._drain_task = asyncio.get_running_loop().create_task(self._drain_loop())

    async def stop(self) -> None:
        """Signal shutdown and wait for the drain task to finish."""
        if self._state not in (_WriterState.RUNNING, _WriterState.DRAINING):
            return
        self._state = _WriterState.DRAINING
        if self._drain_task is not None:
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

        Thread-safe: uses ``loop.call_soon_threadsafe`` to bridge
        from any thread.  Raises :class:`DispatchQueueClosedError` if
        the writer is not running, or
        :class:`DispatchQueueSaturatedError` if the queue is full
        and the enqueue timeout elapses.
        """
        if self._state != _WriterState.RUNNING:
            raise DispatchQueueClosedError(
                f"Writer is not running (state={self._state})"
            )
        loop = asyncio.get_running_loop()
        future: Future[PersistedDispatchResult] = Future()
        qi = _QueuedIntent(intent=intent, future=future)
        self._submitted_total += 1

        try:
            loop.call_soon_threadsafe(self._enqueue_from_event_loop, qi)
        except RuntimeError as exc:
            raise DispatchQueueClosedError("Event loop is closed") from exc
        return future

    async def _enqueue_from_event_loop(self, qi: _QueuedIntent) -> None:
        """Actually place the intent on the queue from the event loop."""
        if self._state != _WriterState.RUNNING:
            qi.future.set_exception(
                DispatchQueueClosedError(f"Writer is not running (state={self._state})")
            )
            return
        try:
            self._queue.put_nowait(qi)
        except asyncio.QueueFull:
            enqueue_mono_ns = qi.enqueue_mono_ns
            deadline_s = (enqueue_mono_ns / 1e6) + self._enqueue_timeout_ms
            now_s = time.monotonic() * 1000
            remaining_ms = deadline_s - now_s
            if remaining_ms <= 0:
                self._failed_total += 1
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
            except TimeoutError:
                self._failed_total += 1
                qi.future.set_exception(
                    DispatchQueueSaturatedError(
                        f"Queue full after {remaining_ms:.0f}ms"
                    )
                )

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
            self._record_queue_depth()
            await self._drain_remaining(batch)
            await self._persist_batch(batch)

        while not self._queue.empty():
            qi = self._queue.get_nowait()
            self._fail_one(qi, DispatchWriterShutdownError("Drain complete"))

    async def _drain_remaining(self, batch: list[_QueuedIntent]) -> None:
        """Drain queued intents up to max_batch_size."""
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

        if len(batch) <= 1 and self._queue.qsize() < _LOW_PRESSURE_THRESHOLD:
            return

        if len(batch) < self._max_batch_size:
            wait_s = self._max_batch_wait_ms / 1000.0
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
                            f"Intent {qi.intent.proxy_request_id} "
                            "cancelled before claim"
                        )
                    )
                    continue
                batch.append(qi)

    async def _persist_batch(self, batch: list[_QueuedIntent]) -> None:
        """Persist a batch of intents in a single transaction."""
        if not batch:
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
            logger.warning(
                "Batch %d (%d intents) persistence failed: %s",
                batch_id,
                batch_size,
                exc,
            )
            error = DispatchTransactionError(f"Batch {batch_id} failed: {exc}")
            for qi in batch:
                self._fail_one(qi, error)
            return

        batch_ms = (time.monotonic() - t0) * 1000.0
        self._last_batch_at = time.monotonic()
        self._last_batch_size = batch_size
        self._batch_sizes.append(batch_size)
        self._batch_wait_ms_samples.append(batch_ms)

        for qi, result in zip(batch, results, strict=True):
            if qi.intent.cancelled.is_set():
                self._cancelled_total += 1
                self._cancelled_after_commit_total += 1
                qi.future.set_exception(
                    DispatchIntentCancelledError(
                        f"Intent {qi.intent.proxy_request_id} cancelled after commit"
                    )
                )
                continue

            self._persisted_total += 1
            tx_ms = result.transaction_ms
            self._transaction_ms_samples.append(tx_ms)
            qi.future.set_result(result)

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

    def record_reconciliation(self) -> None:
        """Record that an ambiguous commit reconciliation was attempted."""
        self._reconciliation_total += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostics snapshot of the writer state."""
        batch_sizes = self._batch_sizes
        queue_depths = self._queue_depth_samples
        tx_ms = self._transaction_ms_samples
        return {
            "state": self._state,
            "queue_depth": self._queue.qsize(),
            "max_queue_depth": self._max_queue_depth,
            "submitted_total": self._submitted_total,
            "persisted_total": self._persisted_total,
            "cancelled_total": self._cancelled_total,
            "cancelled_before_claim_total": self._cancelled_before_claim_total,
            "cancelled_after_commit_total": self._cancelled_after_commit_total,
            "failed_total": self._failed_total,
            "reconciliation_total": self._reconciliation_total,
            "batch_count": len(batch_sizes),
            "batch_size_p50": (_percentile(batch_sizes, 0.50) if batch_sizes else None),
            "batch_size_p95": (_percentile(batch_sizes, 0.95) if batch_sizes else None),
            "batch_size_max": (max(batch_sizes) if batch_sizes else None),
            "batch_wait_ms_p50": (
                _percentile(self._batch_wait_ms_samples, 0.50)
                if self._batch_wait_ms_samples
                else None
            ),
            "batch_wait_ms_p95": (
                _percentile(self._batch_wait_ms_samples, 0.95)
                if self._batch_wait_ms_samples
                else None
            ),
            "transaction_ms_p50": (_percentile(tx_ms, 0.50) if tx_ms else None),
            "transaction_ms_p95": (_percentile(tx_ms, 0.95) if tx_ms else None),
            "queue_depth_p50": (
                _percentile(queue_depths, 0.50) if queue_depths else None
            ),
            "queue_depth_max": (max(queue_depths) if queue_depths else None),
            "last_batch_at": self._last_batch_at,
            "last_batch_size": self._last_batch_size,
        }


def _percentile(samples: Sequence[int | float], pct: float) -> float:
    if not samples:
        return 0.0
    sorted_samples = sorted(samples)
    idx = int(pct * (len(sorted_samples) - 1))
    return float(sorted_samples[idx])
