"""Per-attempt terminal lifecycle: finalize failed attempts independently."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from go_aggregator.security.redaction import redact_error_detail

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database
    from go_aggregator.db.repositories import (
        AttemptRepository,
        ReservationRepository,
    )

logger = logging.getLogger(__name__)

ATTEMPT_MAX_ERROR_DETAIL_CHARS = 2048


@dataclass(frozen=True)
class AttemptFinalizationData:
    """Data for finalizing a single failed attempt."""

    status_code: int | None = None
    error_class: str | None = None
    error_detail: str | None = None
    upstream_request_id: str | None = None
    bytes_emitted: int = 0
    release_reason: str = "attempt_failed"


@dataclass(frozen=True)
class AttemptFinalizeResult:
    """Result of finalizing a failed attempt."""

    attempt_transitioned: bool
    reservation_released: bool


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
    ) -> None:
        self._db = db
        self._attempt_repo = attempt_repo
        self._reservation_repo = reservation_repo

    async def finalize_failed_attempt(
        self,
        attempt_id: int,
        reservation_id: str,
        data: AttemptFinalizationData,
    ) -> AttemptFinalizeResult:
        """Mark a failed attempt as terminal and release its reservation.

        Returns AttemptFinalizeResult indicating whether the attempt
        transitioned and whether the reservation was actually released.
        """
        # Redact, then truncate error_detail. Redaction first ensures the
        # 2048-char cap applies to the safe value, not the raw input.
        error_detail = redact_error_detail(data.error_detail)
        if (
            error_detail is not None
            and len(error_detail) > ATTEMPT_MAX_ERROR_DETAIL_CHARS
        ):
            error_detail = error_detail[:ATTEMPT_MAX_ERROR_DETAIL_CHARS]

        transitioned = False
        reservation_released = False
        async with self._db.transaction():
            # 1. Mark attempt completed only if not already terminal
            transitioned = bool(
                await self._db.execute_write(
                    "UPDATE request_attempts SET "
                    "status_code = ?, error_class = ?, error_detail = ?, "
                    "upstream_request_id = ?, bytes_emitted = ?, "
                    "completed_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND completed_at IS NULL",
                    (
                        data.status_code,
                        data.error_class,
                        error_detail,
                        data.upstream_request_id,
                        data.bytes_emitted,
                        attempt_id,
                    ),
                )
            )

            # 2. Release reservation if still active
            if reservation_id:
                reservation_released = bool(
                    await self._db.execute_write(
                        "UPDATE reservations SET status = 'released', "
                        "released_at = CURRENT_TIMESTAMP, release_reason = ? "
                        "WHERE id = ? AND status = 'active'",
                        (data.release_reason, reservation_id),
                    )
                )

        return AttemptFinalizeResult(
            attempt_transitioned=transitioned,
            reservation_released=reservation_released,
        )
