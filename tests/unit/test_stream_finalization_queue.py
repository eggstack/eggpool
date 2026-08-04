"""Unit tests for ``eggpool.request.finalization_queue``.

Covers the bounded retry queue that backs the finalization retry
drain task.  Validates:

- enqueue deduplication by ``enqueue_token``;
- max-entries overflow drops entries and increments the counter;
- max-age drops entries and increments the counter;
- drain idempotency (re-finalizing an already-finalized row is a no-op);
- snapshot contract (stable keys before any operations);
- bounded retry requeue on transient failure.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from eggpool.request.finalization_queue import (
    DEFAULT_MAX_ENTRIES,
    FinalizationRetryEntry,
    FinalizationRetryQueue,
)
from eggpool.request.finalizer import DurableFinalizationResult


class _StubFinalizer:
    """Mimics :class:`RequestFinalizer.finalize` without touching SQLite."""

    def __init__(self, transition: bool = True) -> None:
        self._transition = transition
        self.calls: list[tuple[str, str]] = []

    async def finalize(
        self,
        selected: Any,
        data: Any,
    ) -> DurableFinalizationResult:
        self.calls.append((selected.db_request_id, data.outcome.value))
        return DurableFinalizationResult(
            request_terminal=self._transition,
            request_transitioned=self._transition,
            attempt_transitioned=self._transition,
            attempt_terminal=self._transition,
            reservation_terminal=self._transition,
            reservation_transitioned=self._transition,
        )


def _make_entry(
    *,
    request_id: str = "req-1",
    db_request_id: str = "db-1",
    outcome: str = "CLIENT_CANCELLED",
    enqueued_at: float | None = None,
) -> FinalizationRetryEntry:
    return FinalizationRetryEntry(
        enqueue_token=f"{db_request_id}:{outcome}",
        request_id=request_id,
        db_request_id=db_request_id,
        attempt_id=1,
        reservation_id="res-1",
        account_id=1,
        account_name="acct-1",
        api_key="sk-test",
        model_id="m",
        estimated_tokens=100,
        estimated_microdollars=10_000,
        attempt_number=1,
        provider_id="opencode",
        protocol="openai",
        outcome=outcome,
        enqueued_at=enqueued_at if enqueued_at is not None else 0.0,
    )


@pytest.mark.asyncio()
async def test_empty_snapshot_contract() -> None:
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    snap = await queue.snapshot()
    assert snap["enabled"] is True
    assert snap["size"] == 0
    assert snap["max_entries"] == DEFAULT_MAX_ENTRIES
    assert snap["enqueued_total"] == 0
    assert snap["drained_total"] == 0
    assert snap["dropped_overflow"] == 0
    assert snap["dropped_age"] == 0
    assert snap["dropped_duplicate"] == 0


@pytest.mark.asyncio()
async def test_enqueue_and_drain_finalizes_request() -> None:
    finalizer = _StubFinalizer(transition=True)
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
        max_age_s=10.0,
    )
    entry = _make_entry(enqueued_at=time.monotonic())
    added = await queue.enqueue(entry)
    assert added is True
    snap = await queue.snapshot()
    assert snap["size"] == 1
    assert snap["enqueued_total"] == 1
    succeeded = await queue.drain_once()
    assert succeeded == 1
    assert finalizer.calls == [("db-1", "client_cancelled")]
    snap = await queue.snapshot()
    assert snap["size"] == 0
    assert snap["drained_total"] == 1


@pytest.mark.asyncio()
async def test_enqueue_dedup_by_token() -> None:
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    now = time.monotonic()
    e1 = _make_entry(db_request_id="db-1", outcome="CLIENT_CANCELLED", enqueued_at=now)
    e2 = _make_entry(db_request_id="db-1", outcome="CLIENT_CANCELLED", enqueued_at=now)
    assert await queue.enqueue(e1) is True
    assert await queue.enqueue(e2) is False
    snap = await queue.snapshot()
    assert snap["dropped_duplicate"] == 1


@pytest.mark.asyncio()
async def test_enqueue_overflow_drops() -> None:
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
        max_entries=2,
    )
    now = time.monotonic()
    for i in range(2):
        assert (
            await queue.enqueue(_make_entry(db_request_id=f"db-{i}", enqueued_at=now))
            is True
        )
    assert (
        await queue.enqueue(_make_entry(db_request_id="db-overflow", enqueued_at=now))
        is False
    )
    snap = await queue.snapshot()
    assert snap["dropped_overflow"] == 1


@pytest.mark.asyncio()
async def test_enqueue_max_age_drops() -> None:
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
        max_age_s=1.0,
    )
    stale = _make_entry(enqueued_at=time.monotonic() - 100.0)
    assert await queue.enqueue(stale) is False
    snap = await queue.snapshot()
    assert snap["dropped_age"] == 1


@pytest.mark.asyncio()
async def test_drain_idempotent_when_already_finalized() -> None:
    """Re-finalizing an already-finalized row is not counted as a transition."""
    finalizer = _StubFinalizer(transition=False)
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    await queue.enqueue(_make_entry(enqueued_at=time.monotonic()))
    succeeded = await queue.drain_once()
    assert succeeded == 0
    assert finalizer.calls == [("db-1", "client_cancelled")]


@pytest.mark.asyncio()
async def test_drain_requeues_after_transient_failure_then_gives_up() -> None:
    """Entries are requeued up to 4 attempts before being dropped."""

    class _FlakyFinalizer(_StubFinalizer):
        def __init__(self) -> None:
            super().__init__(transition=False)

        async def finalize(
            self,
            selected: Any,
            data: Any,
        ) -> bool:
            raise RuntimeError("transient DB error")

    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=_FlakyFinalizer(),  # type: ignore[arg-type]
    )
    await queue.enqueue(_make_entry(enqueued_at=time.monotonic()))
    # Run enough drains to exhaust the retry budget (4 retries).
    for _ in range(5):
        await queue.drain_once()
    snap = await queue.snapshot()
    assert snap["size"] == 0
    assert snap["dropped_age"] >= 1


@pytest.mark.asyncio()
async def test_drain_skips_stale_entries() -> None:
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
        max_age_s=1.0,
    )
    entry = _make_entry(enqueued_at=time.monotonic() - 100.0)
    queue._entries.append(entry)  # pyright: ignore[reportPrivateUsage]
    succeeded = await queue.drain_once()
    assert succeeded == 0
    snap = await queue.snapshot()
    assert snap["dropped_age"] == 1


class _AlwaysFailFinalizer(_StubFinalizer):
    """Finalizer that always raises, simulating a transient DB error."""

    def __init__(self) -> None:
        super().__init__(transition=False)

    async def finalize(
        self,
        selected: Any,
        data: Any,
    ) -> bool:
        raise RuntimeError("deterministic transient failure")


@pytest.mark.asyncio()
async def test_retry_count_exhaustion_drops_entry() -> None:
    """Entry with a flaky finalizer is dropped after retry_count > 4."""
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=_AlwaysFailFinalizer(),  # type: ignore[arg-type]
    )
    await queue.enqueue(_make_entry(enqueued_at=time.monotonic()))
    # 5 drains: retry_count goes 1,2,3,4,5 (> 4 on the 5th).
    for _ in range(5):
        await queue.drain_once()
    snap = await queue.snapshot()
    assert snap["size"] == 0
    assert snap["dropped_age"] >= 1


@pytest.mark.asyncio()
async def test_duplicate_rejection_increments_counter() -> None:
    """Duplicate enqueue_token increments dropped_duplicate."""
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    now = time.monotonic()
    e1 = _make_entry(
        db_request_id="db-dup", outcome="CLIENT_CANCELLED", enqueued_at=now
    )
    e2 = _make_entry(
        db_request_id="db-dup", outcome="CLIENT_CANCELLED", enqueued_at=now
    )
    assert await queue.enqueue(e1) is True
    assert await queue.enqueue(e2) is False
    snap = await queue.snapshot()
    assert snap["dropped_duplicate"] == 1
    assert snap["size"] == 1


@pytest.mark.asyncio()
async def test_overflow_behavior() -> None:
    """Queue with max_entries=2 drops the third entry."""
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
        max_entries=2,
    )
    now = time.monotonic()
    for i in range(3):
        assert await queue.enqueue(
            _make_entry(db_request_id=f"db-of-{i}", enqueued_at=now)
        ) is (i < 2)
    snap = await queue.snapshot()
    assert snap["dropped_overflow"] == 1
    assert snap["size"] == 2


@pytest.mark.asyncio()
async def test_drain_returns_succeeded_count() -> None:
    """drain_once returns the number of entries where the finalizer returned True."""
    finalizer = _StubFinalizer(transition=True)
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    now = time.monotonic()
    for i in range(3):
        await queue.enqueue(_make_entry(db_request_id=f"db-ok-{i}", enqueued_at=now))
    succeeded = await queue.drain_once()
    assert succeeded == 3
    snap = await queue.snapshot()
    assert snap["drained_total"] == 3
    assert snap["size"] == 0


@pytest.mark.asyncio()
async def test_drain_with_mixed_outcomes() -> None:
    """drain_once counts only entries with durable convergence."""

    class _MixedFinalizer(_StubFinalizer):
        def __init__(self) -> None:
            super().__init__(transition=True)

        async def finalize(
            self,
            selected: Any,
            data: Any,
        ) -> DurableFinalizationResult:
            self.calls.append((selected.db_request_id, data.outcome.value))
            transition = selected.db_request_id != "db-fail"
            return DurableFinalizationResult(
                request_terminal=transition,
                request_transitioned=transition,
                attempt_transitioned=transition,
                attempt_terminal=transition,
                reservation_terminal=transition,
                reservation_transitioned=transition,
            )

    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=_MixedFinalizer(),  # type: ignore[arg-type]
    )
    now = time.monotonic()
    await queue.enqueue(_make_entry(db_request_id="db-success", enqueued_at=now))
    await queue.enqueue(_make_entry(db_request_id="db-fail", enqueued_at=now))
    succeeded = await queue.drain_once()
    assert succeeded == 1


_SNAPSHOT_EXPECTED_KEYS = frozenset(
    {
        "enabled",
        "size",
        "max_entries",
        "max_age_s",
        "oldest_entry_age_s",
        "active_interval_s",
        "idle_interval_s",
        "enqueued_total",
        "drained_total",
        "dropped_overflow",
        "dropped_age",
        "dropped_duplicate",
        "last_drain_at",
        "last_drain_duration_ms",
        "last_drain_processed",
        "last_drain_succeeded",
    }
)


@pytest.mark.asyncio()
async def test_snapshot_includes_all_expected_keys() -> None:
    """Fresh queue snapshot contains every expected key."""
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    snap = await queue.snapshot()
    assert set(snap.keys()) == _SNAPSHOT_EXPECTED_KEYS


@pytest.mark.asyncio()
async def test_snapshot_json_serializable() -> None:
    """snapshot() output can be serialized to JSON without raising."""
    finalizer = _StubFinalizer()
    queue = FinalizationRetryQueue(
        db=None,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    snap = await queue.snapshot()
    serialized = json.dumps(snap, default=str)
    assert isinstance(serialized, str)
