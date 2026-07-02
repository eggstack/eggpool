"""Idempotent request finalizer: one call per terminal outcome."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from eggpool.constants import (
    MAX_REQUEST_COST_MICRODOLLARS,
    clamp_request_cost_microdollars,
)
from eggpool.db.repositories import (
    AccountEventRepository,
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from eggpool.health.health_manager import classify_failure_category
from eggpool.security.redaction import (
    MAX_REDACTED_ERROR_DETAIL_CHARS,
    redact_error_detail,
)

if TYPE_CHECKING:
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.catalog.pricing import CostCalculator
    from eggpool.db.connection import Database
    from eggpool.health.health_manager import HealthManager
    from eggpool.quota.estimation import QuotaEstimator
    from eggpool.routing.router import Router

logger = logging.getLogger(__name__)

MAX_ERROR_DETAIL_CHARS = MAX_REDACTED_ERROR_DETAIL_CHARS


class FinalizationOutcome(StrEnum):
    """Terminal outcome of a request."""

    COMPLETED = "completed"
    CLIENT_ERROR = "client_error"
    UPSTREAM_ERROR = "upstream_error"
    MIDSTREAM_ERROR = "midstream_error"
    CLIENT_CANCELLED = "client_cancelled"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
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
    bytes_received: int = 0
    upstream_request_id: str | None = None
    error_class: str | None = None
    error_detail: str | None = None
    health_already_applied: bool = False
    upstream_connect_ms: int | None = None
    upstream_read_ms: int | None = None
    coordinator_overhead_ms: int | None = None
    # Authoritative provider-reported billed cost, in microdollars.
    # ``None`` when the upstream did not surface an unambiguous value.
    # When set, this value overrides any locally derived cost so the
    # dashboard reflects actual spend rather than a reservation
    # estimate. ``provider_cost_source`` records the JSON path that
    # produced the value for audit/observability.
    provider_cost_microdollars: int | None = None
    provider_cost_source: str | None = None
    upstream_protocol: str | None = None
    thinking_trace_json: str | None = None
    # Provider-neutral usage from
    # :class:`eggpool.proxy.normalized_usage.NormalizedUsage`.  When
    # ``None`` the legacy zero-vs-``None`` distinction is unavailable
    # and the database renders ``cache_counter_status =
    # 'not_reported'`` with cache counters stored as zero (matching the
    # historical behaviour).  When supplied, every cache counter is
    # stored verbatim and ``cache_counter_status`` records whether the
    # upstream actually surfaced cache fields, parsed cleanly with no
    # cache fields, or returned a shape EggPool could not parse.
    normalized_usage: Any | None = None
    transcoded: bool = False
    # Phase 2 segmentation summary.  When ``None`` the database renders
    # ``segmentation_status = 'empty_request'`` with all segment fields
    # left as ``None`` (preserving historical behaviour).  When supplied,
    # the stable-prefix hash, request-shape hash, segment-kind token and
    # byte estimates, and a compact JSON summary are persisted so later
    # phases can drive observe-mode compression accounting without
    # reclassifying the request.
    segmentation: Any | None = None


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
        persist_error_detail: bool = False,
        metrics_coalescer: Any | None = None,  # noqa: ANN401
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
        self._persist_error_detail = persist_error_detail
        self._metrics_coalescer = metrics_coalescer

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

        # Cost-precedence ladder for the canonical ``cost_microdollars``
        # value persisted to the requests table:
        #
        #   1. ``provider_reported``  — authoritative upstream-reported
        #      billed cost from the response payload.
        #   2. ``derived`` / ``partial`` / ``exact`` — EggPool's local
        #      CostCalculator produced a value from a trusted price
        #      snapshot. Preserve the calculator's exactness label.
        #   3. ``estimated`` — the calculator returned zero/unknown
        #      but billable work likely occurred; fall back to the
        #      conservative reservation estimate. Distinct from a real
        #      provider figure so the dashboard can attribute spend
        #      correctly.
        #   4. ``unknown`` — no usage and no billable work, so cost
        #      stays at zero.
        #
        # The reservation estimate is recorded for routing/failover
        # scoring and is preserved as a separate audit field. It MUST
        # NOT inflate provider-reported or derived cost — those values
        # already reflect actual or near-actual spend.
        has_usage = any(
            (
                data.input_tokens,
                data.output_tokens,
                data.cache_read_tokens,
                data.cache_write_tokens,
            )
        )
        may_have_billable_work = data.outcome in (FinalizationOutcome.COMPLETED,) or (
            data.outcome
            in (
                FinalizationOutcome.CLIENT_CANCELLED,
                FinalizationOutcome.MIDSTREAM_ERROR,
            )
            and (has_usage or data.bytes_emitted > 0)
        )

        # Local calculator result — preserved as an audit field even
        # when the canonical value is overridden by a provider report.
        local_cost_microdollars: int | None = None
        local_cost_exactness: str | None = None
        if self._cost_calculator is not None and has_usage:
            (
                local_cost_microdollars,
                local_cost_exactness,
            ) = await self._cost_calculator.calculate_cost(
                _get_model_id(selected),
                data.input_tokens,
                data.output_tokens,
                data.cache_read_tokens,
                data.cache_write_tokens,
                provider_id=selected.provider_id,
            )

        # 1. Provider-reported cost wins outright when present.
        if data.provider_cost_microdollars is not None:
            cost_microdollars = data.provider_cost_microdollars
            exactness = "provider_reported"
        # 2. Trusted local calculation: derived / partial / exact.
        elif local_cost_microdollars is not None and (
            local_cost_microdollars > 0
            or local_cost_exactness in {"derived", "partial", "exact"}
        ):
            cost_microdollars = local_cost_microdollars
            exactness = local_cost_exactness or "derived"
        # 3. Conservative reservation fallback only when billable work
        #    is plausible AND no trusted value is available.
        elif may_have_billable_work:
            cost_microdollars = selected.estimated_microdollars
            exactness = "estimated"
            local_cost_microdollars = cost_microdollars
            local_cost_exactness = "estimated"
        else:
            cost_microdollars = 0
            exactness = "unknown"

        # 4. Estimated-cost floor: even when a calculator produced a
        #    positive-but-trivially-small value under the ``estimated``
        #    label, the dashboard must reflect at least the conservative
        #    reservation amount so quota accounting never reports less
        #    spend than the routing layer pre-reserved. This applies
        #    only when exactness stayed at ``estimated``; derived /
        #    partial / exact values reflect real pricing and must not
        #    be inflated by a reservation floor.
        if (
            exactness == "estimated"
            and cost_microdollars < selected.estimated_microdollars
        ):
            cost_microdollars = selected.estimated_microdollars

        capped_cost_microdollars = clamp_request_cost_microdollars(cost_microdollars)
        if capped_cost_microdollars != cost_microdollars:
            logger.warning(
                "Capping request cost for %s from %s to %s microdollars",
                getattr(selected, "request_id", "<unknown>"),
                cost_microdollars,
                MAX_REQUEST_COST_MICRODOLLARS,
            )
            cost_microdollars = capped_cost_microdollars
        provider_cost_microdollars = (
            clamp_request_cost_microdollars(data.provider_cost_microdollars)
            if data.provider_cost_microdollars is not None
            else None
        )
        local_cost_microdollars = (
            clamp_request_cost_microdollars(local_cost_microdollars)
            if local_cost_microdollars is not None
            else None
        )

        # Default is fail-closed: do not persist arbitrary provider
        # error detail. When ``persist_error_detail`` is enabled the
        # shared redactor already returns a bounded string.
        if self._persist_error_detail and data.error_detail is not None:
            error_detail = redact_error_detail(data.error_detail)
        else:
            error_detail = None

        # Cache-observability fields sourced from the normalized usage
        # record.  When the coordinator produced a
        # :class:`NormalizedUsage` we persist every counter verbatim
        # plus the cache_counter_status enum so the dashboard can
        # distinguish reported counters from a null parse.  When the
        # coordinator only had a legacy ``StreamUsageResult`` (older
        # tests, error paths), the database renders
        # ``cache_counter_status = 'not_reported'`` and falls back to
        # the historical zero-token columns — preserving full backward
        # compatibility.
        normalized = data.normalized_usage
        cache_counter_status_value = "not_reported"
        cached_input_tokens_value: int | None = None
        cache_read_input_tokens_value: int | None = None
        cache_creation_input_tokens_value: int | None = None
        cache_write_input_tokens_value: int | None = None
        cache_write_input_reported_value: int | None = None
        input_tokens_reported_value: int | None = None
        output_tokens_reported_value: int | None = None
        total_tokens_reported_value: int | None = None
        raw_usage_json_value: str | None = None
        if normalized is not None:
            cache_counter_status_value = str(
                getattr(normalized, "cache_counter_status", "not_reported")
            )
            cached_input_tokens_value = getattr(normalized, "cached_input_tokens", None)
            cache_read_input_tokens_value = getattr(
                normalized, "cache_read_input_tokens", None
            )
            cache_creation_input_tokens_value = getattr(
                normalized, "cache_creation_input_tokens", None
            )
            cache_write_input_tokens_value = getattr(
                normalized, "cache_write_input_tokens", None
            )
            # ``cache_write_input_reported`` mirrors
            # ``cache_creation_input_tokens`` for Anthropic and stays
            # ``None`` for OpenAI.  It exists so the stats layer can
            # render a single "writes reported" column without
            # branching on protocol.
            if cache_creation_input_tokens_value is not None:
                cache_write_input_reported_value = cache_creation_input_tokens_value
            input_tokens_reported_value = getattr(normalized, "input_tokens", None)
            output_tokens_reported_value = getattr(normalized, "output_tokens", None)
            total_tokens_reported_value = getattr(normalized, "total_tokens", None)
            raw_usage = getattr(normalized, "raw_usage", None)
            if raw_usage is not None:
                try:
                    raw_usage_json_value = json.dumps(raw_usage, default=str)
                except (TypeError, ValueError):
                    raw_usage_json_value = None

        async with self._db.transaction():
            # 3. Finalize request only if pending (idempotent)
            db_request_id = selected.db_request_id
            status = self._outcome_to_status(data.outcome)
            retry_count = max(0, selected.attempt_number - 1)

            # Phase 2 segmentation summary.  The finalizer is the single
            # source of truth for persistence of the segmentation
            # fields; the coordinator attaches the SegmentationResult
            # to ``data.segmentation`` (or leaves it ``None`` for
            # callers that did not run the segmenter — historical
            # behaviour).  Field names mirror migration 0041.
            segmentation_obj = data.segmentation
            segmentation_status_value = "empty_request"
            stable_prefix_hash_value: str | None = None
            request_shape_hash_value: str | None = None
            stable_prefix_estimated_tokens_value: int | None = None
            semi_stable_estimated_tokens_value: int | None = None
            volatile_estimated_tokens_value: int | None = None
            stable_prefix_bytes_value: int | None = None
            semi_stable_bytes_value: int | None = None
            volatile_bytes_value: int | None = None
            segmentation_summary_json_value: str | None = None
            if segmentation_obj is not None:
                segmentation_status_value = str(
                    getattr(segmentation_obj, "status", "empty_request")
                )
                stable_prefix_hash_value = getattr(
                    segmentation_obj, "stable_prefix_hash", None
                )
                request_shape_hash_value = getattr(
                    segmentation_obj, "request_shape_hash", None
                )
                stable_prefix_estimated_tokens_value = getattr(
                    segmentation_obj, "stable_prefix_estimated_tokens", None
                )
                semi_stable_estimated_tokens_value = getattr(
                    segmentation_obj, "semi_stable_estimated_tokens", None
                )
                volatile_estimated_tokens_value = getattr(
                    segmentation_obj, "volatile_estimated_tokens", None
                )
                stable_prefix_bytes_value = getattr(
                    segmentation_obj, "stable_prefix_bytes", None
                )
                semi_stable_bytes_value = getattr(
                    segmentation_obj, "semi_stable_bytes", None
                )
                volatile_bytes_value = getattr(segmentation_obj, "volatile_bytes", None)
                try:
                    from eggpool.transcoder.segmentation import (
                        segmentation_summary_json,
                    )

                    segmentation_summary_json_value = segmentation_summary_json(
                        segmentation_obj
                    )
                except (TypeError, ValueError):
                    segmentation_summary_json_value = None

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
                bytes_received=data.bytes_received,
                bytes_emitted=data.bytes_emitted,
                upstream_latency_ms=data.upstream_latency_ms
                if data.upstream_latency_ms is not None
                else 0,
                upstream_connect_ms=data.upstream_connect_ms,
                upstream_read_ms=data.upstream_read_ms,
                coordinator_overhead_ms=data.coordinator_overhead_ms,
                provider_cost_microdollars=provider_cost_microdollars,
                provider_cost_source=data.provider_cost_source,
                local_cost_microdollars=local_cost_microdollars,
                local_cost_exactness=local_cost_exactness,
                upstream_protocol=data.upstream_protocol,
                thinking_trace_json=data.thinking_trace_json,
                cache_counter_status=cache_counter_status_value,
                cached_input_tokens=cached_input_tokens_value,
                cache_read_input_tokens=cache_read_input_tokens_value,
                cache_creation_input_tokens=cache_creation_input_tokens_value,
                cache_write_input_tokens=cache_write_input_tokens_value,
                cache_write_input_reported=cache_write_input_reported_value,
                input_tokens_reported=input_tokens_reported_value,
                output_tokens_reported=output_tokens_reported_value,
                total_tokens_reported=total_tokens_reported_value,
                # Phase 2 segmentation: ``request_shape_hash`` /
                # ``stable_prefix_hash`` are also Phase 1 placeholders
                # that the segmenter now populates.  ``transcoded`` and
                # ``raw_usage_json`` close the existing positional
                # argument list.
                request_shape_hash=request_shape_hash_value,
                stable_prefix_hash=stable_prefix_hash_value,
                segmentation_status=segmentation_status_value,
                stable_prefix_estimated_tokens=stable_prefix_estimated_tokens_value,
                semi_stable_estimated_tokens=semi_stable_estimated_tokens_value,
                volatile_estimated_tokens=volatile_estimated_tokens_value,
                stable_prefix_bytes=stable_prefix_bytes_value,
                semi_stable_bytes=semi_stable_bytes_value,
                volatile_bytes=volatile_bytes_value,
                segmentation_summary_json=segmentation_summary_json_value,
                transcoded=1 if data.transcoded else 0,
                raw_usage_json=raw_usage_json_value,
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
                        from eggpool.db.repositories import AccountRepository

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
                    except (
                        asyncio.CancelledError,
                        SystemExit,
                        KeyboardInterrupt,
                    ):
                        raise
                    except Exception:
                        logger.exception("Failed to record account event")

            # Commit happens via context manager

        # Post-commit: update in-memory state only if we performed the transition
        if transitioned:
            if reservation_released:
                if self._quota_estimator is not None:
                    # Reverse the in-memory reservation: cost (audit),
                    # one in-flight request, and the projected token
                    # volume for the request.
                    await self._quota_estimator.remove_reservation(
                        selected.account_name,
                        selected.estimated_microdollars,
                        requests=1,
                        tokens=selected.estimated_tokens,
                    )

                if self._router is not None:
                    await self._router.decrement_active_request_count(
                        selected.account_name
                    )

            # 2. Add final cost to live quota state whenever the request
            #    transitioned. This is independent of the reservation path:
            #    even when the attempt finalizer already released the
            #    reservation, the request-level cost must still be recorded
            #    so that routing decisions observe it immediately.
            if self._quota_estimator is not None and cost_microdollars > 0:
                total_tokens = (
                    data.input_tokens
                    + data.output_tokens
                    + data.cache_read_tokens
                    + data.cache_write_tokens
                )
                # record_usage + persisted snapshot increment must be
                # atomic so concurrent finalizers cannot interleave.
                await self._quota_estimator.record_usage_and_snapshot(
                    selected.account_name,
                    tokens=total_tokens,
                    cost_microdollars=cost_microdollars,
                    model_id=_get_model_id(selected),
                )

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
                    category = classify_failure_category(
                        data.error_class, data.status_code
                    )
                    self._health_manager.record_failure(
                        selected.account_name,
                        model_id=mid,
                        reason=category.value,
                    )
                elif data.outcome in (
                    FinalizationOutcome.CLIENT_CANCELLED,
                    FinalizationOutcome.CLIENT_ERROR,
                    FinalizationOutcome.MIDSTREAM_ERROR,
                ):
                    # These outcomes don't penalize health but must
                    # release any consumed half-open probe slot.
                    self._health_manager.release_request(selected.account_name)

            # 5. Update runtime state. Request-level success and terminal
            #    state must always update the runtime view, independent of
            #    the reservation path.  health_already_applied also
            #    guards runtime state to prevent duplicate failure records
            #    when the coordinator already applied the health transition.
            if self._registry is not None and not data.health_already_applied:
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

        # 6. Emit analytics event to the metrics coalescer (non-blocking).
        #    Only emit when this call performed the terminal transition to
        #    avoid double-counting from stale/crash-recovery finalizers.
        if transitioned and self._metrics_coalescer is not None:
            try:
                from datetime import UTC, datetime

                from eggpool.metrics.buffer import UsageMetricEvent

                event = UsageMetricEvent(
                    timestamp=datetime.now(UTC),
                    provider_id=getattr(selected, "provider_id", "unknown"),
                    model_id=_get_model_id(selected),
                    account_id=getattr(selected, "account_id", None),
                    protocol=_get_protocol(selected, data),
                    streamed=_get_streamed(selected),
                    status=self._outcome_to_status(data.outcome),
                    retry_count=max(0, getattr(selected, "attempt_number", 1) - 1),
                    input_tokens=data.input_tokens,
                    output_tokens=data.output_tokens,
                    cache_read_tokens=data.cache_read_tokens,
                    cache_write_tokens=data.cache_write_tokens,
                    reasoning_tokens=data.reasoning_tokens,
                    thinking_characters=data.thinking_characters,
                    cost_microdollars=cost_microdollars,
                    bytes_received=data.bytes_received,
                    bytes_emitted=data.bytes_emitted,
                    latency_ms=data.upstream_latency_ms or 0,
                    first_byte_ms=data.first_byte_ms,
                )
                self._metrics_coalescer.record_usage(event)
            except Exception:
                logger.debug("Failed to emit usage metric event", exc_info=True)

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
    if hasattr(selected, "model_id") and selected.model_id:
        return selected.model_id
    logger.warning(
        "selected object has no model_id attribute or it is empty "
        "(type=%s). Cost and health tracking may be inaccurate.",
        type(selected).__name__,
    )
    return ""


def _get_protocol(selected: Any, data: FinalizationData) -> str:
    """Extract the client protocol for analytics, falling back safely."""
    protocol = getattr(selected, "protocol", None)
    if isinstance(protocol, str) and protocol:
        return protocol
    if data.upstream_protocol:
        return data.upstream_protocol
    return "openai"


def _get_streamed(selected: Any) -> bool:
    """Extract the streaming flag without trusting loose test doubles."""
    streamed = getattr(selected, "streamed", None)
    return streamed if isinstance(streamed, bool) else False
