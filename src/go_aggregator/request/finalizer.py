"""Idempotent request finalizer: one call per terminal outcome."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from go_aggregator.db.repositories import (
    AccountEventRepository,
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from go_aggregator.health.health_manager import classify_failure_category
from go_aggregator.security.redaction import redact_error_detail

if TYPE_CHECKING:
    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.catalog.pricing import CostCalculator
    from go_aggregator.db.connection import Database
    from go_aggregator.health.health_manager import HealthManager
    from go_aggregator.quota.estimation import QuotaEstimator
    from go_aggregator.routing.router import Router

logger = logging.getLogger(__name__)

MAX_ERROR_DETAIL_CHARS = 2048


class FinalizationOutcome(StrEnum):
    """Terminal outcome of a request."""

    COMPLETED = "completed"
    CLIENT_ERROR = "client_error"
    UPSTREAM_ERROR = "upstream_error"
    MIDSTREAM_ERROR = "midstream_error"
    CLIENT_CANCELLED = "client_cancelled"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


@dataclass
class FinalizationData:
    """Input data for finalizing a request."""

    outcome: FinalizationOutcome
    status_code: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    thinking_characters: int = 0
    upstream_latency_ms: int | None = None
    first_byte_ms: int | None = None
    bytes_emitted: int = 0
    upstream_request_id: str | None = None
    error_class: str | None = None
    error_detail: str | None = None
    health_already_applied: bool = False


class RequestFinalizer:
    """Finalizes requests exactly once, handling all terminal outcomes.

    Receives all dependencies needed to:
    - Calculate costs
    - Update request/attempt/reservation records
    - Release in-memory reservations
    - Update live quota state
    - Update EWMA estimates
    - Update health state
    """

    def __init__(
        self,
        db: Database,
        request_repo: RequestRepository,
        attempt_repo: AttemptRepository,
        reservation_repo: ReservationRepository,
        cost_calculator: CostCalculator | None = None,
        quota_estimator: QuotaEstimator | None = None,
        router: Router | None = None,
        registry: AccountRegistry | None = None,
        health_manager: HealthManager | None = None,
    ) -> None:
        self._db = db
        self._request_repo = request_repo
        self._attempt_repo = attempt_repo
        self._reservation_repo = reservation_repo
        self._cost_calculator = cost_calculator
        self._quota_estimator = quota_estimator
        self._router = router
        self._registry = registry
        self._health_manager = health_manager

    async def finalize(
        self,
        selected: Any,
        data: FinalizationData,
    ) -> bool:
        """Finalize a request exactly once.

        Returns True if this call performed the terminal transition,
        False if the request was already finalized (idempotent).
        """
        transitioned = False
        reservation_released = False
        cost_microdollars = 0
        exactness = "unknown"

        # Redact, then truncate error_detail before any processing.
        # Redaction must happen first so the truncation cap applies to
        # the safe value, not the original secret-bearing input.
        error_detail = redact_error_detail(data.error_detail)
        if error_detail is not None and len(error_detail) > MAX_ERROR_DETAIL_CHARS:
            error_detail = error_detail[:MAX_ERROR_DETAIL_CHARS]

        async with self._db.transaction():
            # 1. Calculate cost if we have usable usage
            if self._cost_calculator is not None and any(
                (
                    data.input_tokens,
                    data.output_tokens,
                    data.cache_read_tokens,
                    data.cache_write_tokens,
                )
            ):
                (
                    cost_microdollars,
                    exactness,
                ) = await self._cost_calculator.calculate_cost(
                    _get_model_id(selected),
                    data.input_tokens,
                    data.output_tokens,
                    data.cache_read_tokens,
                    data.cache_write_tokens,
                )

            # 2. Always use reservation estimate when cost is unknown
            if cost_microdollars == 0 and exactness == "unknown":
                cost_microdollars = selected.estimated_microdollars
                exactness = "estimated"

            # 2b. Ensure estimated cost never falls below reservation estimate
            if (
                exactness == "estimated"
                and cost_microdollars < selected.estimated_microdollars
            ):
                cost_microdollars = selected.estimated_microdollars

            # 3. Finalize request only if pending (idempotent)
            db_request_id = selected.db_request_id
            status = self._outcome_to_status(data.outcome)
            retry_count = max(0, selected.attempt_number - 1)

            transitioned = await self._request_repo.finalize_if_pending(
                request_id=db_request_id,
                status=status,
                status_code=data.status_code,
                input_tokens=data.input_tokens,
                output_tokens=data.output_tokens,
                cost_microdollars=cost_microdollars,
                exactness=exactness,
                first_byte_ms=data.first_byte_ms,
                error_class=data.error_class,
                error_detail=error_detail,
                upstream_request_id=data.upstream_request_id,
                cache_read_tokens=data.cache_read_tokens,
                cache_write_tokens=data.cache_write_tokens,
                reasoning_tokens=data.reasoning_tokens,
                thinking_characters=data.thinking_characters,
                retry_count=retry_count,
                upstream_latency_ms=data.upstream_latency_ms
                if data.upstream_latency_ms is not None
                else 0,
            )

            # 4. Finalize attempt only if request transitioned and attempt
            #    is still incomplete (idempotent; preserves first terminal data)
            if transitioned:
                await self._attempt_repo.finalize_if_incomplete(
                    attempt_id=selected.attempt_id,
                    status_code=data.status_code,
                    error_class=data.error_class,
                    error_detail=error_detail,
                    upstream_request_id=data.upstream_request_id,
                    bytes_emitted=data.bytes_emitted,
                )

                # 5. Release reservation
                reservation_released = await self._reservation_repo.release(
                    selected.reservation_id, reason=status
                )

                # 6. Insert account event for significant failures
                if (
                    data.outcome
                    in (
                        FinalizationOutcome.UPSTREAM_ERROR,
                        FinalizationOutcome.INTERRUPTED,
                    )
                    and data.error_class
                ):
                    try:
                        from go_aggregator.db.repositories import AccountRepository

                        account_repo = AccountRepository(self._db)
                        account_id = await account_repo.get_id_by_name(
                            selected.account_name
                        )
                        if account_id is not None:
                            event_repo = AccountEventRepository(self._db)
                            # error_class and status_code are safe to
                            # persist; the event details deliberately do
                            # not include error_detail.
                            await event_repo.record(
                                account_id=account_id,
                                event_type=data.outcome.value,
                                details=json.dumps(
                                    {
                                        "error_class": data.error_class,
                                        "status_code": data.status_code,
                                    }
                                ),
                            )
                    except Exception:
                        logger.debug("Failed to record account event", exc_info=True)

            # Commit happens via context manager

        # Post-commit: update in-memory state only if we performed the transition
        if transitioned:
            # 1. Reservation cleanup depends on whether the reservation was
            #    actually released. If the attempt finalizer already released
            #    the reservation we must not decrement again.
            if reservation_released:
                # 1a. Remove exactly the reserved amount from in-memory tracking
                if self._quota_estimator is not None:
                    self._quota_estimator.remove_reservation(
                        selected.account_name, selected.estimated_microdollars
                    )

                # 1b. Decrement active request count
                if self._router is not None:
                    self._router.decrement_active_request_count(selected.account_name)

            # 2. Add final cost to live quota state whenever the request
            #    transitioned. This is independent of the reservation path:
            #    even when the attempt finalizer already released the
            #    reservation, the request-level cost must still be recorded
            #    so that routing decisions observe it immediately.
            if self._quota_estimator is not None and cost_microdollars > 0:
                total_tokens = data.input_tokens + data.output_tokens
                self._quota_estimator.record_usage(
                    selected.account_name,
                    tokens=total_tokens,
                    cost_microdollars=cost_microdollars,
                    model_id=_get_model_id(selected),
                )

                # 3. Refresh persisted snapshot locally
                #    Increment cached 5h/7d/30d values by final cost
                snapshot = self._quota_estimator.get_account_quota(
                    selected.account_name
                )
                if snapshot is not None and snapshot.persisted_snapshot is not None:
                    snapshot.persisted_snapshot.cost_5h += cost_microdollars
                    snapshot.persisted_snapshot.cost_7d += cost_microdollars
                    snapshot.persisted_snapshot.cost_30d += cost_microdollars

            # 4. Update health state. health_already_applied is honored to
            #    keep health transitions idempotent across retried attempts.
            if self._health_manager is not None and not data.health_already_applied:
                mid = _get_model_id(selected)
                if data.outcome == FinalizationOutcome.COMPLETED:
                    self._health_manager.record_success(selected.account_name, mid)
                elif data.outcome in (
                    FinalizationOutcome.UPSTREAM_ERROR,
                    FinalizationOutcome.TIMEOUT,
                    FinalizationOutcome.INTERRUPTED,
                ):
                    self._health_manager.record_failure(
                        selected.account_name,
                        model_id=mid,
                        reason=data.error_class,
                    )
                # CLIENT_CANCELLED and CLIENT_ERROR don't penalize health

            # 5. Update runtime state. Request-level success and terminal
            #    state must always update the runtime view, independent of
            #    the reservation path.
            if self._registry is not None:
                state = self._registry.get_state(selected.account_name)
                if state is not None:
                    if data.outcome == FinalizationOutcome.COMPLETED:
                        state.record_success()
                    elif data.outcome in (
                        FinalizationOutcome.UPSTREAM_ERROR,
                        FinalizationOutcome.TIMEOUT,
                        FinalizationOutcome.INTERRUPTED,
                    ):
                        category = classify_failure_category(
                            data.error_class, data.status_code
                        )
                        state.record_failure(category.value)

        return transitioned

    @staticmethod
    def _outcome_to_status(outcome: FinalizationOutcome) -> str:
        """Map outcome to request status string."""
        if outcome == FinalizationOutcome.COMPLETED:
            return "completed"
        if outcome == FinalizationOutcome.CLIENT_ERROR:
            return "client_error"
        if outcome == FinalizationOutcome.CLIENT_CANCELLED:
            return "cancelled"
        return "error"


def _get_model_id(selected: Any) -> str:
    """Extract model_id from SelectedAttempt if available."""
    return selected.model_id if hasattr(selected, "model_id") else ""
