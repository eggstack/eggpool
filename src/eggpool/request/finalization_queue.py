"""Compatibility adapter for legacy finalization retry submissions.

The shielded immediate finalizer inside
:meth:`eggpool.request.coordinator.RequestCoordinator._build_stream_generator`
caps at 10 seconds.  When the SQLite connection lock is heavily contended
the immediate finalizer can hit that ceiling and the cancellation
finalization is left to the broad periodic stale finalizer.  This queue
remembers the metadata needed to retry that finalization quickly and
idempotently so the runtime state (active counts, reservations,
backoff) reconciles faster than a 60-second stale sweep.

Historically this module owned a second periodic retry policy.  Terminal
ownership now belongs to :class:`RequestFinalizationSupervisor`; this adapter
is retained only for older integrations that still submit an entry.  It never
interprets an idempotent no-op as failure and never applies an independent
retry count or drop policy.

The adapter remains:

* **bounded**: a maximum number of in-flight entries protects memory
  under sustained overload.  New entries past the cap are dropped and a
  ``dropped_overflow`` counter is incremented so operators can spot a
  sustained pattern via the runtime diagnostics endpoint.
* **idempotent**: re-enqueuing an entry that is already enqueued is a
  no-op (same ``enqueue_token``).  Re-running finalization on a row
  that already transitioned is also a no-op because
  :meth:`RequestFinalizer.finalize` returns ``False`` for already
  finalized rows.
* **bounded**: old callers still receive explicit saturation diagnostics;
  accepted entries are handed to the finalizer once by the caller's drain.

The queue does NOT apply provider health penalties for
``CLIENT_CANCELLED`` outcomes — the retry simply re-runs the same
idempotent finalization, and provider health is only ever updated by
:func:`RequestFinalizer.finalize` itself for non-cancellation outcomes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.quota.estimation import QuotaEstimator
    from eggpool.request.finalizer import RequestFinalizer
    from eggpool.routing.router import Router

logger = logging.getLogger(__name__)


DEFAULT_MAX_ENTRIES = 1024
DEFAULT_MAX_AGE_S = 120.0
DEFAULT_ACTIVE_INTERVAL_S = 1.5
DEFAULT_IDLE_INTERVAL_S = 15.0


@dataclass
class FinalizationRetryEntry:
    """In-memory record of a finalization that escaped the immediate path."""

    enqueue_token: str
    request_id: str
    db_request_id: str
    attempt_id: int
    reservation_id: str
    account_id: int
    account_name: str
    api_key: str  # stored only for retry finalization; never logged
    model_id: str
    estimated_tokens: int
    estimated_microdollars: int
    attempt_number: int
    provider_id: str
    protocol: str
    outcome: str  # FinalizationOutcome name
    enqueued_at: float = field(default_factory=time.monotonic)
    retry_count: int = 0


@dataclass
class _RetryStubAttempt:
    """Minimal SelectedAttempt-like view for finalizer.finalize.

    The retry queue does not have a real SelectedAttempt (the original
    selection is over).  The finalizer only reads a small handful of
    fields, so a frozen dataclass with the right attributes is
    sufficient and stays type-safe.
    """

    proxy_request_id: str
    db_request_id: str
    attempt_id: int
    reservation_id: str
    account_id: int
    account_name: str
    api_key: str
    model_id: str
    estimated_tokens: int
    estimated_microdollars: int
    attempt_number: int
    provider_id: str = "opencode"
    requires_transcode: bool = False
    protocol: str = "openai"
    streamed: bool = True


class FinalizationRetryQueue:
    """Process-local retry queue for cancelled / midstream finalizations."""

    def __init__(
        self,
        *,
        db: Database,
        finalizer: RequestFinalizer,
        router: Router | None = None,
        quota_estimator: QuotaEstimator | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        active_interval_s: float = DEFAULT_ACTIVE_INTERVAL_S,
        idle_interval_s: float = DEFAULT_IDLE_INTERVAL_S,
    ) -> None:
        self._db = db
        self._finalizer = finalizer
        self._router = router
        self._quota_estimator = quota_estimator
        self._max_entries = max_entries
        self._max_age_s = max_age_s
        self._active_interval_s = active_interval_s
        self._idle_interval_s = idle_interval_s
        self._entries: deque[FinalizationRetryEntry] = deque()
        self._lock = asyncio.Lock()
        # Counters surfaced via the runtime diagnostics endpoint.
        self._enqueued_total = 0
        self._drained_total = 0
        self._dropped_overflow = 0
        self._dropped_age = 0
        self._dropped_duplicate = 0
        self._last_drain_at: float | None = None
        self._last_drain_duration_ms: float | None = None
        self._last_drain_processed: int | None = None
        self._last_drain_succeeded: int | None = None

    @property
    def active_interval_s(self) -> float:
        return self._active_interval_s

    @property
    def idle_interval_s(self) -> float:
        return self._idle_interval_s

    @property
    def size(self) -> int:
        return len(self._entries)

    async def enqueue(
        self,
        entry: FinalizationRetryEntry,
    ) -> bool:
        """Add *entry* to the queue.

        Returns ``True`` when the entry was added, ``False`` when it
        was a duplicate token, when the queue was at capacity, or when
        the entry was already past its max age.
        """
        async with self._lock:
            # Reject duplicates by enqueue_token.
            for existing in self._entries:
                if existing.enqueue_token == entry.enqueue_token:
                    self._dropped_duplicate += 1
                    return False
            now = time.monotonic()
            if now - entry.enqueued_at > self._max_age_s:
                self._dropped_age += 1
                return False
            if len(self._entries) >= self._max_entries:
                self._dropped_overflow += 1
                logger.warning(
                    "Finalization retry queue full; dropping entry for request %s",
                    entry.request_id,
                )
                return False
            self._entries.append(entry)
            self._enqueued_total += 1
            return True

    async def drain_once(self) -> int:
        """Process every entry currently in the queue.

        Returns the number of entries that succeeded (finalizer returned
        ``True`` for the request/attempt transition).
        """
        async with self._lock:
            if not self._entries:
                self._last_drain_at = time.monotonic()
                self._last_drain_processed = 0
                self._last_drain_succeeded = 0
                self._last_drain_duration_ms = 0.0
                return 0
            # Snapshot the deque under the lock; processing happens
            # outside the lock so the hot finalizer does not block new
            # enqueues.
            snapshot = list(self._entries)
            self._entries.clear()

        now = time.monotonic()
        succeeded = 0
        processed = 0
        # Drop entries that exceeded the compatibility adapter's age bound.
        survivors: list[FinalizationRetryEntry] = []
        for entry in snapshot:
            if now - entry.enqueued_at > self._max_age_s:
                self._dropped_age += 1
                continue
            processed += 1
            try:
                ok = await self._finalize_entry(entry)
                if ok:
                    succeeded += 1
            except Exception:
                self._dropped_age += 1
                logger.exception(
                    "Legacy finalization adapter failed for request %s; "
                    "the process-owned supervisor must own any retry",
                    entry.request_id,
                )

        async with self._lock:
            self._entries.extendleft(reversed(survivors))
            self._drained_total += succeeded
            self._last_drain_at = time.monotonic()
            self._last_drain_duration_ms = (
                self._last_drain_at - now  # type: ignore[operator]
            ) * 1000.0
            self._last_drain_processed = processed
            self._last_drain_succeeded = succeeded
        return succeeded

    async def _finalize_entry(self, entry: FinalizationRetryEntry) -> bool:
        """Re-run the finalizer for one entry.

        Returns whether the finalizer transitioned the row.  An already
        terminal row is converged and is intentionally not requeued.
        """
        # Imported lazily to avoid an import cycle with finalizer.py.
        from eggpool.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
        )

        try:
            outcome = FinalizationOutcome(entry.outcome)
        except ValueError:
            logger.warning(
                "Unknown finalization outcome %r for request %s; "
                "treating as CLIENT_CANCELLED",
                entry.outcome,
                entry.request_id,
            )
            outcome = FinalizationOutcome.CLIENT_CANCELLED

        stub = _RetryStubAttempt(
            proxy_request_id=entry.request_id,
            db_request_id=entry.db_request_id,
            attempt_id=entry.attempt_id,
            reservation_id=entry.reservation_id,
            account_id=entry.account_id,
            account_name=entry.account_name,
            api_key=entry.api_key,
            model_id=entry.model_id,
            estimated_tokens=entry.estimated_tokens,
            estimated_microdollars=entry.estimated_microdollars,
            attempt_number=entry.attempt_number,
            provider_id=entry.provider_id,
            streamed=True,
        )

        transitioned = await self._finalizer.finalize(
            stub,
            FinalizationData(
                outcome=outcome,
                upstream_protocol=entry.protocol,
            ),
        )
        return bool(transitioned)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            size = len(self._entries)
            oldest_age = (
                time.monotonic() - self._entries[0].enqueued_at
                if self._entries
                else None
            )
            return {
                "enabled": True,
                "size": size,
                "max_entries": self._max_entries,
                "max_age_s": self._max_age_s,
                "oldest_entry_age_s": (
                    round(oldest_age, 3) if oldest_age is not None else None
                ),
                "active_interval_s": self._active_interval_s,
                "idle_interval_s": self._idle_interval_s,
                "enqueued_total": self._enqueued_total,
                "drained_total": self._drained_total,
                "dropped_overflow": self._dropped_overflow,
                "dropped_age": self._dropped_age,
                "dropped_duplicate": self._dropped_duplicate,
                "last_drain_at": self._last_drain_at,
                "last_drain_duration_ms": self._last_drain_duration_ms,
                "last_drain_processed": self._last_drain_processed,
                "last_drain_succeeded": self._last_drain_succeeded,
            }


async def retry_queue_tick(queue: FinalizationRetryQueue) -> int:
    """Periodic tick factory that drains a finalization retry queue."""
    return await queue.drain_once()


async def finalization_tick_factory(
    queue: FinalizationRetryQueue,
) -> Any:
    """Supervisor-compatible periodic tick factory.

    Returns a coroutine that drains the queue once.  Designed to be
    passed to :meth:`TaskSupervisor.register_periodic`.
    """

    async def _tick() -> int:
        return await queue.drain_once()

    return _tick()
