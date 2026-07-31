"""Per-attempt terminal lifecycle: finalize failed attempts independently."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from eggpool.security.redaction import (
    MAX_REDACTED_ERROR_DETAIL_CHARS,
    redact_error_detail,
)

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.db.repositories import (
        AttemptRepository,
        ReservationRepository,
    )

logger = logging.getLogger(__name__)

ATTEMPT_MAX_ERROR_DETAIL_CHARS = MAX_REDACTED_ERROR_DETAIL_CHARS


@dataclass(frozen=True, slots=True)
class AttemptFinalizationData:
    """Data for finalizing a single failed attempt."""

    request_id: str | None = None
    status_code: int | None = None
    error_class: str | None = None
    error_detail: str | None = None
    upstream_request_id: str | None = None
    bytes_emitted: int = 0
    bytes_received: int = 0
    latency_ms: int = 0
    retry_category: str | None = None
    release_reason: str = "attempt_failed"
    is_retry_outcome: bool = False


@dataclass(frozen=True, slots=True)
class AttemptFinalizeResult:
    """Result of finalizing a failed attempt."""

    attempt_transitioned: bool
    reservation_released: bool
    reservation_converged: bool = False
    durable_terminal: bool = False
    durable_transitioned: bool = False
    runtime_cleanup_complete: bool = True
    retryable: bool = False


class AttemptFinalizer:
    """Finalizes individual failed attempts before retry.

    This is distinct from RequestFinalizer which handles the overall
    request lifecycle. AttemptFinalizer marks a single attempt as
    terminal and releases its reservation, allowing the coordinator
    to select a new account for the next attempt.
    """

    def __init__(
        self,
        db: Database,
        attempt_repo: AttemptRepository,
        reservation_repo: ReservationRepository,
        persist_error_detail: bool = False,
    ) -> None:
        self._db = db
        self._attempt_repo = attempt_repo
        self._reservation_repo = reservation_repo
        self._persist_error_detail = persist_error_detail

    async def finalize_failed_attempt(
        self,
        attempt_id: int,
        reservation_id: str,
        data: AttemptFinalizationData,
    ) -> AttemptFinalizeResult:
        """Mark a failed attempt as terminal and release its reservation.

        Returns AttemptFinalizeResult indicating whether the attempt
        transitioned, whether this invocation released the reservation,
        and whether the reservation is durably terminal.
        """
        # Default is fail-closed: do not persist arbitrary provider
        # error detail. When ``persist_error_detail`` is enabled the
        # shared redactor already returns a bounded string.
        if self._persist_error_detail and data.error_detail is not None:
            error_detail = redact_error_detail(data.error_detail)
        else:
            error_detail = None

        transitioned = False
        reservation_released = False
        retry_flag = 1 if data.is_retry_outcome else 0
        # Plan 027: attach an ambiguous-operation descriptor so that
        # an indeterminate commit outcome is recorded for post-recovery
        # reconciliation.
        from eggpool.db.connection import (  # noqa: PLC0415
            AmbiguousDatabaseOperation,
        )

        ambiguous_operation = AmbiguousDatabaseOperation(
            operation_id=str(attempt_id),
            operation_kind="attempt_finalization",
            connection_epoch=self._db.connection_epoch,
            idempotency_keys=(
                ("request_id", str(data.request_id or "")),
                ("attempt_id", str(attempt_id)),
                ("reservation_id", reservation_id),
            ),
            intended_status="completed",
            precondition_facts=(),
            created_at_monotonic=time.monotonic(),
            reconciliation_strategy="attempt_finalization",
        )
        reservation_converged = False
        attempt_terminal = False
        async with self._db.transaction(ambiguous_operation=ambiguous_operation):
            # 1. Mark attempt completed only if not already terminal
            transitioned = bool(
                await self._db.execute_write(
                    "UPDATE request_attempts SET "
                    "status_code = ?, error_class = ?, error_detail = ?, "
                    "upstream_request_id = ?, bytes_emitted = ?, "
                    "bytes_received = ?, latency_ms = ?, "
                    "retry_category = ?, release_reason = ?, "
                    "is_retry_outcome = ?, "
                    "completed_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND completed_at IS NULL",
                    (
                        data.status_code,
                        data.error_class,
                        error_detail,
                        data.upstream_request_id,
                        data.bytes_emitted,
                        data.bytes_received,
                        data.latency_ms,
                        data.retry_category,
                        data.release_reason,
                        retry_flag,
                        attempt_id,
                    ),
                )
            )

            # 2. Release reservation only if the attempt actually transitioned
            #    to a terminal state. When the attempt was already completed
            #    (e.g. by the request finalizer racing this call), releasing
            #    the reservation here would cause a double-release.
            if transitioned and reservation_id:
                reservation_released = bool(
                    await self._db.execute_write(
                        "UPDATE reservations SET status = 'released', "
                        "released_at = CURRENT_TIMESTAMP, release_reason = ? "
                        "WHERE id = ? AND status = 'active'",
                        (data.release_reason, reservation_id),
                    )
                )
                if reservation_released:
                    reservation_converged = True

            if reservation_id and not reservation_converged:
                status = await self._reservation_repo.get_status(reservation_id)
                reservation_converged = (
                    status in self._reservation_repo.TERMINAL_STATUSES
                )
            attempt_row = await self._db.fetch_one(
                "SELECT completed_at FROM request_attempts WHERE id = ?",
                (attempt_id,),
            )
            attempt_terminal = (
                attempt_row is not None and attempt_row["completed_at"] is not None
            )

        return AttemptFinalizeResult(
            attempt_transitioned=transitioned,
            reservation_released=reservation_released,
            reservation_converged=reservation_converged,
            durable_terminal=attempt_terminal,
            durable_transitioned=transitioned,
            retryable=not reservation_converged,
        )
