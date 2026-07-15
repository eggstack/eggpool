"""Repository-level dispatch bundle persistence.

Milestone C replaces per-request correctness-critical dispatch
transactions with a bounded in-process persistence pipeline.
This module provides the repository layer that persists one or
more :class:`DispatchIntent` objects in a single transaction.

Batch persistence guarantees atomicity: if any intent fails,
the entire batch is rolled back and every result carries the
failure.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import TYPE_CHECKING, Any

from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from eggpool.errors import DatabaseError
from eggpool.request.dispatch_intent import (
    DispatchAmbiguousCommitError,
    DispatchTransactionError,
    PersistedDispatchResult,
)

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.request.dispatch_intent import DispatchIntent

logger = logging.getLogger(__name__)


def _start_timestamp_ms() -> float:
    return time.monotonic() * 1000


def _make_result(
    *,
    db_request_id: str,
    reservation_id: str,
    attempt_id: int,
    attempt_number: int,
    batch_id: int,
    batch_size: int,
    commit_timestamp: str,
    queue_wait_ms: float,
    transaction_ms: float,
) -> PersistedDispatchResult:
    return PersistedDispatchResult(
        db_request_id=db_request_id,
        reservation_id=reservation_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        batch_id=batch_id,
        batch_size=batch_size,
        commit_timestamp=commit_timestamp,
        queue_wait_ms=queue_wait_ms,
        transaction_ms=transaction_ms,
    )


def _intent_queue_wait_ms(intent: DispatchIntent) -> float:
    return max(0.0, (_start_timestamp_ms() - intent.enqueue_monotonic_ns / 1e6))


async def _persist_one(
    db: Database,
    intent: DispatchIntent,
    batch_id: int,
    batch_size: int,
    *,
    request_repo: RequestRepository,
    reservation_repo: ReservationRepository,
    attempt_repo: AttemptRepository,
) -> PersistedDispatchResult:
    """Persist a single dispatch bundle inside the caller's transaction.

    For first attempts (``attempt_number == 1``), a new request row
    is inserted.  For retries (``attempt_number > 1``), the existing
    request is updated with the newly selected account.
    """
    queue_wait_ms = _intent_queue_wait_ms(intent)

    if intent.attempt_number == 1:
        db_request_id = await request_repo.create_pending(
            request_id=intent.proxy_request_id,
            model_id=intent.model_id,
            protocol=intent.protocol,
            streamed=intent.streamed,
            account_id=intent.account_id,
            reserved_microdollars=intent.estimated_microdollars,
            started_at=None,
            provider_id=intent.provider_id,
            client_ip=intent.client_ip or "",
        )
    else:
        if not intent.existing_db_request_id:
            raise DatabaseError("attempt_number > 1 requires existing_db_request_id")
        db_request_id = intent.existing_db_request_id
        await request_repo.update_after_selection(
            request_id=db_request_id,
            account_id=intent.account_id,
            reserved_microdollars=intent.estimated_microdollars,
        )

    reservation_id = await reservation_repo.create(
        request_id=db_request_id,
        account_id=intent.account_id,
        model_id=intent.model_id,
        estimated_tokens=intent.estimated_tokens,
        estimated_microdollars=intent.estimated_microdollars,
    )

    attempt_id = await attempt_repo.create(
        request_id=db_request_id,
        attempt_number=intent.attempt_number,
        account_id=intent.account_id,
        provider_id=intent.provider_id,
        model_id=intent.model_id,
        protocol=intent.protocol,
        streamed=intent.streamed,
    )

    commit_timestamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%d %H:%M:%S")

    return _make_result(
        db_request_id=db_request_id,
        reservation_id=reservation_id,
        attempt_id=attempt_id,
        attempt_number=intent.attempt_number,
        batch_id=batch_id,
        batch_size=batch_size,
        commit_timestamp=commit_timestamp,
        queue_wait_ms=queue_wait_ms,
        transaction_ms=0.0,
    )


async def persist_dispatch_bundle(
    db: Database,
    intent: DispatchIntent,
    batch_id: int = 1,
) -> PersistedDispatchResult:
    """Persist a single dispatch bundle in its own transaction.

    Returns a :class:`PersistedDispatchResult` on success.
    Raises :class:`DispatchTransactionError` wrapping the underlying
    :class:`DatabaseError` on failure.
    """
    t0 = _start_timestamp_ms()
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)

    try:
        async with db.transaction():
            result = await _persist_one(
                db,
                intent,
                batch_id=batch_id,
                batch_size=1,
                request_repo=request_repo,
                reservation_repo=reservation_repo,
                attempt_repo=attempt_repo,
            )
    except DispatchTransactionError:
        raise
    except Exception as exc:
        raise DispatchTransactionError(
            f"Failed to persist dispatch bundle: {exc}"
        ) from exc

    tx_ms = _start_timestamp_ms() - t0
    return _make_result(
        db_request_id=result.db_request_id,
        reservation_id=result.reservation_id,
        attempt_id=result.attempt_id,
        attempt_number=result.attempt_number,
        batch_id=result.batch_id,
        batch_size=result.batch_size,
        commit_timestamp=result.commit_timestamp,
        queue_wait_ms=result.queue_wait_ms,
        transaction_ms=tx_ms,
    )


async def persist_dispatch_bundles(
    db: Database,
    intents: list[DispatchIntent],
    batch_id: int = 1,
) -> list[PersistedDispatchResult]:
    """Persist one or more dispatch bundles in a single transaction.

    All intents are committed atomically.  On failure the entire
    batch is rolled back and every result carries the failure.

    Returns a list of :class:`PersistedResult` with the same
    ordering as the input ``intents``.
    """
    if not intents:
        return []

    t0 = _start_timestamp_ms()
    batch_size = len(intents)
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    results: list[PersistedDispatchResult] = []

    try:
        async with db.transaction():
            for intent in intents:
                result = await _persist_one(
                    db,
                    intent,
                    batch_id=batch_id,
                    batch_size=batch_size,
                    request_repo=request_repo,
                    reservation_repo=reservation_repo,
                    attempt_repo=attempt_repo,
                )
                results.append(result)
    except Exception as exc:
        logger.warning(
            "Batch %d persistence failed after %d/%d intents: %s",
            batch_id,
            len(results),
            batch_size,
            exc,
        )
        failure = _make_result(
            db_request_id="",
            reservation_id="",
            attempt_id=0,
            attempt_number=0,
            batch_id=batch_id,
            batch_size=batch_size,
            commit_timestamp="",
            queue_wait_ms=0.0,
            transaction_ms=0.0,
        )
        return [failure] * batch_size

    tx_ms = _start_timestamp_ms() - t0
    return [
        _make_result(
            db_request_id=r.db_request_id,
            reservation_id=r.reservation_id,
            attempt_id=r.attempt_id,
            attempt_number=r.attempt_number,
            batch_id=r.batch_id,
            batch_size=r.batch_size,
            commit_timestamp=r.commit_timestamp,
            queue_wait_ms=r.queue_wait_ms,
            transaction_ms=tx_ms,
        )
        for r in results
    ]


async def reconcile_ambiguous_commit(
    db: Database,
    *,
    proxy_request_id: str,
    attempt_number: int,
) -> PersistedDispatchResult:
    """Reconcile an ambiguous commit by querying durable state.

    After a connection error during commit, the outcome is unknown.
    This function queries the database to determine whether the intent
    was actually committed:

    - If request, reservation, and attempt all exist consistently,
      treat as committed and return the result.
    - If no rows exist, raise :class:`DispatchAmbiguousCommitError`.
    - If partial/inconsistent rows exist, raise
      :class:`DispatchAmbiguousCommitError` (fail closed).
    """
    attempt_repo = AttemptRepository(db)

    # Query by proxy_request_id (the client-supplied request ID), not
    # by the auto-generated rowid.
    request_row_raw = await db.fetch_one(
        "SELECT * FROM requests WHERE proxy_request_id = ?",
        (proxy_request_id,),
    )
    request_row = dict(request_row_raw) if request_row_raw is not None else None
    if request_row is None:
        raise DispatchAmbiguousCommitError(
            f"No request row for proxy_request_id={proxy_request_id!r}; "
            f"intent was not committed"
        )

    db_request_id = str(request_row["id"])

    # Query attempts for this request
    attempt_rows = await attempt_repo.get_for_request(db_request_id)
    matching_attempt: dict[str, Any] | None = None
    for row in attempt_rows:
        if int(row["attempt_number"]) == attempt_number:
            matching_attempt = row
            break

    if matching_attempt is None:
        raise DispatchAmbiguousCommitError(
            f"Request {proxy_request_id!r} exists but attempt_number="
            f"{attempt_number} not found; partial commit"
        )

    # Query reservations for this request via raw SQL
    reservation_rows = await db.fetch_all(
        "SELECT * FROM reservations WHERE request_id = ? ORDER BY id",
        (db_request_id,),
    )
    if not reservation_rows:
        raise DispatchAmbiguousCommitError(
            f"Request {proxy_request_id!r} and attempt exist but no "
            f"reservation found; partial commit"
        )

    # Use the most recent reservation (the one we just created)
    latest_reservation: dict[str, Any] = dict(reservation_rows[-1])

    return PersistedDispatchResult(
        db_request_id=db_request_id,
        reservation_id=str(latest_reservation["id"]),
        attempt_id=int(matching_attempt["id"]),
        attempt_number=attempt_number,
        batch_id=0,
        batch_size=1,
        commit_timestamp=str(request_row.get("started_at", "")),
        queue_wait_ms=0.0,
        transaction_ms=0.0,
    )
