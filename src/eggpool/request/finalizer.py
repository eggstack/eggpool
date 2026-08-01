"""Idempotent request finalizer: one call per terminal outcome."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from eggpool.catalog.pricing import choose_bounded_estimated_cost
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
from eggpool.failure import (
    EffectsApplier,
    FailureObservation,
    ModelQuarantine,
)
from eggpool.failure.classifier import classify_failure_effects
from eggpool.health.health_manager import classify_failure_category
from eggpool.request.terminal_status import REQUEST_TERMINAL_STATUSES
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

# Exactness labels the request finalizer treats as canonical local
# ``cost_microdollars`` candidates. ``estimated`` is explicitly excluded
# so a positive-but-inflated heuristic value cannot become dashboard
# spend; the finalizer falls through to the reservation estimate when
# the local calculator flagged its result as ``estimated``.
_TRUSTED_LOCAL_EXACTNESS = frozenset({"derived", "partial", "exact"})


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
class _FinalizationDiagnosticSnapshot:
    """Precomputed diagnostic fields for the finalization transaction.

    Plan 028 Workstream G: all JSON serialization and attribute
    extraction happens BEFORE the ``BEGIN IMMEDIATE`` transaction so
    the SQLite write-lock is held only for the actual DML statements.
    """

    # Segmentation
    segmentation_status: str = "empty_request"
    stable_prefix_hash: str | None = None
    request_shape_hash: str | None = None
    stable_prefix_estimated_tokens: int | None = None
    semi_stable_estimated_tokens: int | None = None
    volatile_estimated_tokens: int | None = None
    stable_prefix_bytes: int | None = None
    semi_stable_bytes: int | None = None
    volatile_bytes: int | None = None
    segmentation_summary_json: str | None = None
    # Compression observation
    compression_status: str = "disabled"
    compression_mode: str | None = None
    compression_candidate_count: int = 0
    compression_eligible_candidate_count: int = 0
    compression_suppressed_candidate_count: int = 0
    compression_estimated_original_tokens: int | None = None
    compression_estimated_compressed_tokens: int | None = None
    compression_estimated_savings_tokens: int | None = None
    compression_analyzer_latency_ms: float | None = None
    compression_warning_count: int = 0
    compression_reason_code_counts_json: str | None = None
    compression_summary_json: str | None = None
    # Compression result
    compression_applied: int = 0
    compression_transform_count: int = 0
    compression_transforms_by_reason_json: str | None = None
    compression_original_tokens: int | None = None
    compression_compressed_tokens: int | None = None
    compression_savings_tokens: int | None = None
    compression_pre_stable_prefix_hash: str | None = None
    compression_post_stable_prefix_hash: str | None = None
    compression_stable_prefix_preserved: int = 1
    compression_warnings_json: str | None = None
    compression_latency_ms: float = 0.0
    compression_failed_fallback: int = 0
    compression_applied_summary_json: str | None = None
    # Resolved compression policy
    compression_policy_name: str | None = None
    compression_policy_source: str | None = None
    compression_policy_warnings_json: str | None = None
    # Synthetic cache
    synthetic_cache_status: str | None = None
    synthetic_cache_dry_run: int = 1
    synthetic_cache_candidate_count: int = 0
    synthetic_cache_applied_count: int = 0
    synthetic_cache_warning_count: int = 0
    synthetic_cache_warnings_json: str | None = None
    synthetic_cache_policy_name: str | None = None
    synthetic_cache_policy_source: str | None = None
    synthetic_cache_summary_json: str | None = None


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
    release_reason: str | None = None
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
    # When ``True`` the segmentation phase was intentionally skipped
    # (no consumer needed the output).  The finalizer stores
    # ``segmentation_status = 'not_collected'`` instead of
    # ``'empty_request'`` so the dashboard can distinguish
    # "segmentation was not run" from "segmentation ran on an empty
    # request".
    segmentation_not_collected: bool = False
    # Phase 4 compression observation.  When ``None`` the database
    # renders ``compression_status = 'disabled'`` with all compression
    # columns left as ``None`` (preserving historical behaviour and
    # respecting the default ``enabled = false`` policy).  When
    # supplied, the analyzer's per-request roll-up — candidate
    # counts, eligible vs suppressed token totals, analyzer latency,
    # warning list, reason-code tallies, and a compact JSON summary —
    # is persisted so operators can see what a future phase would
    # compress without re-parsing the request.
    compression_observation: Any | None = None
    # Phase 5 compression result.  When ``None`` the Phase 5
    # compression columns stay at their safe defaults (all zeros /
    # None).  When supplied, the applier's per-request roll-up —
    # applied flag, transform counts, token savings, stable-prefix
    # hash comparison, warnings, and a compact JSON summary — is
    # persisted so the dashboard can show actual compression outcomes.
    compression_result: Any | None = None
    # Phase 6 resolved compression policy.  Computed in
    # :mod:`eggpool.api.proxy_request` by merging the global
    # ``[compression]`` config with any matching
    # ``[[compression.policies]]`` entries.  Carries the resolved
    # :class:`CompressionConfig`, the audit name / source, the
    # list of matched override names (file order), and any
    # resolution warnings.  The finalizer extracts the name,
    # source, and warnings into dedicated columns so dashboards
    # can filter on resolved policy without re-running the
    # resolver.  ``None`` when compression is disabled, when the
    # resolver import / call failed, or on legacy / error paths.
    resolved_compression_policy: Any | None = None
    # Phase 9 synthetic cache-controls result.  Produced by
    # :func:`eggpool.transcoder.cache_synthesis.run_synthetic_cache_synthesis`
    # in :mod:`eggpool.api.proxy_request`.  Carries the selector plan
    # (status, dry_run, candidate/applied/warning counts, audit policy
    # name/source), the mutated payload when apply mode ran, and the
    # synthetic boundary annotations recorded against the
    # ``CacheBoundaryTracker``.  ``None`` when synthetic cache
    # controls are disabled, when the call failed, or on legacy
    # paths; the finalizer falls back to safe defaults
    # (``synthetic_cache_status = NULL``,
    # ``synthetic_cache_dry_run = 1``) for backwards compatibility.
    synthetic_cache_result: Any | None = None


@dataclass(frozen=True, slots=True)
class DurableFinalizationResult:
    """Facts proven by one durable finalization transaction."""

    request_terminal: bool
    request_transitioned: bool
    attempt_transitioned: bool
    attempt_terminal: bool
    reservation_terminal: bool
    reservation_transitioned: bool
    retryable: bool = False
    detail: str = ""

    @property
    def durable_converged(self) -> bool:
        """Whether every durable component required by the job is terminal."""
        return (
            self.request_terminal
            and self.attempt_terminal
            and self.reservation_terminal
        )


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
        effects_applier: EffectsApplier | None = None,
        quarantine: ModelQuarantine | None = None,
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
        # Plan 025: typed failure effects applier.  When the
        # coordinator has already applied effects for the same
        # attempt identity, ``effects_applier.apply_once`` is a
        # no-op so the finalizer never double-penalizes health.
        self._effects_applier = effects_applier
        self._quarantine = quarantine

    async def finalize(
        self,
        selected: Any,
        data: FinalizationData,
    ) -> DurableFinalizationResult:
        """Finalize a request exactly once.

        Returns the independently observed request, attempt, and
        reservation convergence facts.  This remains idempotent while
        distinguishing an already-terminal request from incomplete
        durable components.
        """
        transitioned = False
        attempt_transitioned = False
        reservation_released = False
        attempt_terminal = False
        reservation_terminal = False
        request_terminal = False
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
        #   3. ``estimated`` — the calculator returned a value but
        #      flagged its per-token rate as implausible or used a
        #      heuristic fallback.  When the value is plausible we
        #      trust it; when both the local estimate and the
        #      reservation are available we pick the lower plausible
        #      one via :func:`choose_bounded_estimated_cost`. A
        #      generous reservation MUST NOT silently override a
        #      tighter local estimate (see plans/2026-07-03-*), and
        #      nothing later in this method may floor the selected
        #      estimate back to the reservation amount.
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
        may_have_billable_work = (
            has_usage
            or data.outcome in (FinalizationOutcome.COMPLETED,)
            or (
                data.outcome
                in (
                    FinalizationOutcome.CLIENT_CANCELLED,
                    FinalizationOutcome.MIDSTREAM_ERROR,
                )
                and data.bytes_emitted > 0
            )
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

        reservation_microdollars = int(
            getattr(selected, "estimated_microdollars", 0) or 0
        )

        # 1. Provider-reported cost wins outright when present.
        if data.provider_cost_microdollars is not None:
            cost_microdollars = data.provider_cost_microdollars
            exactness = "provider_reported"
        # 2. Trusted local calculation: derived / partial / exact.
        elif (
            local_cost_microdollars is not None
            and local_cost_exactness in _TRUSTED_LOCAL_EXACTNESS
        ):
            cost_microdollars = local_cost_microdollars
            exactness = local_cost_exactness
        # 3. ``estimated`` path.  A positive local estimate is NOT
        #    discarded outright — instead :func:`choose_bounded_estimated_cost`
        #    picks the lower plausible value between the local
        #    estimate and the reservation, with the reservation as
        #    fallback when only one is plausible.  When the local
        #    estimate is implausibly high (the historical unit-
        #    misclassification class of bug) we still drop it so it
        #    cannot become canonical.
        elif (
            local_cost_microdollars is not None
            and local_cost_exactness == "estimated"
            and may_have_billable_work
        ):
            chosen, provenance = choose_bounded_estimated_cost(
                local_estimate_microdollars=local_cost_microdollars,
                reservation_microdollars=reservation_microdollars,
                input_tokens=data.input_tokens,
                output_tokens=data.output_tokens,
                cache_read_tokens=data.cache_read_tokens,
                cache_write_tokens=data.cache_write_tokens,
            )
            cost_microdollars = chosen
            exactness = "estimated"
            if (
                provenance == "reservation_estimated"
                and local_cost_microdollars > cost_microdollars
            ):
                logger.info(
                    "cost.reservation_fallback_suppressed "
                    "request_id=%s provider=%s model=%s input_tokens=%s "
                    "output_tokens=%s cache_read_tokens=%s cache_write_tokens=%s "
                    "local_microdollars=%s reservation_microdollars=%s "
                    "canonical_microdollars=%s provenance=%s "
                    "reason=reservation_lower_than_estimated_local",
                    getattr(selected, "request_id", "<unknown>"),
                    getattr(selected, "provider_id", "<unknown>"),
                    _get_model_id(selected),
                    data.input_tokens,
                    data.output_tokens,
                    data.cache_read_tokens,
                    data.cache_write_tokens,
                    local_cost_microdollars,
                    reservation_microdollars,
                    cost_microdollars,
                    provenance,
                )
            elif (
                provenance == "min_local_reservation_estimated"
                and reservation_microdollars > max(local_cost_microdollars * 4, 0)
            ):
                logger.info(
                    "cost.reservation_fallback_suppressed "
                    "request_id=%s provider=%s model=%s input_tokens=%s "
                    "output_tokens=%s cache_read_tokens=%s cache_write_tokens=%s "
                    "local_microdollars=%s reservation_microdollars=%s "
                    "canonical_microdollars=%s provenance=%s "
                    "reason=reservation_above_plausible_local_estimate",
                    getattr(selected, "request_id", "<unknown>"),
                    getattr(selected, "provider_id", "<unknown>"),
                    _get_model_id(selected),
                    data.input_tokens,
                    data.output_tokens,
                    data.cache_read_tokens,
                    data.cache_write_tokens,
                    local_cost_microdollars,
                    reservation_microdollars,
                    cost_microdollars,
                    provenance,
                )
        # 4. No trusted calculator value but billable work exists.
        #    ``choose_bounded_estimated_cost`` applies the same
        #    plausibility checks to the reservation-only path and may
        #    return a generic bounded estimate when neither estimate is
        #    trustworthy. When there are no billable tokens at all we
        #    still preserve the reservation estimate so zero-usage
        #    successes and emitted-byte-only failures keep a nonzero
        #    billable signal.
        elif may_have_billable_work:
            chosen, _provenance = choose_bounded_estimated_cost(
                local_estimate_microdollars=None,
                reservation_microdollars=reservation_microdollars,
                input_tokens=data.input_tokens,
                output_tokens=data.output_tokens,
                cache_read_tokens=data.cache_read_tokens,
                cache_write_tokens=data.cache_write_tokens,
            )
            if not has_usage and reservation_microdollars > 0:
                chosen = reservation_microdollars
            cost_microdollars = chosen
            exactness = "estimated"
            local_cost_microdollars = cost_microdollars
            local_cost_exactness = "estimated"
        else:
            cost_microdollars = 0
            exactness = "unknown"

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

        # Plan 027: attach an ambiguous-operation descriptor so that
        # an indeterminate commit outcome is recorded for post-recovery
        # reconciliation.
        from eggpool.db.connection import (  # noqa: PLC0415
            AmbiguousDatabaseOperation,
        )

        ambiguous_operation = AmbiguousDatabaseOperation(
            operation_id=selected.db_request_id,
            operation_kind="request_finalization",
            connection_epoch=self._db.connection_epoch,
            idempotency_keys=(
                ("request_id", str(selected.db_request_id)),
                ("attempt_id", str(selected.attempt_id)),
                ("reservation_id", str(selected.reservation_id)),
                ("attempt_number", str(selected.attempt_number)),
            ),
            intended_status=self._outcome_to_status(data.outcome),
            precondition_facts=(),
            created_at_monotonic=time.monotonic(),
            reconciliation_strategy="request_finalization",
        )

        # Plan 028 Workstream G: precompute ALL diagnostic serialization
        # outside the BEGIN IMMEDIATE critical section.  This keeps the
        # SQLite write-lock held only for the actual DML statements,
        # reducing contention on the single writer connection.
        diag = self._precompute_finalization_diagnostics(data)
        db_request_id = selected.db_request_id
        status = self._outcome_to_status(data.outcome)
        retry_count = max(0, selected.attempt_number - 1)

        async with self._db.transaction(ambiguous_operation=ambiguous_operation):
            # 3. Finalize request only if pending (idempotent)
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
                request_shape_hash=diag.request_shape_hash,
                stable_prefix_hash=diag.stable_prefix_hash,
                segmentation_status=diag.segmentation_status,
                stable_prefix_estimated_tokens=diag.stable_prefix_estimated_tokens,
                semi_stable_estimated_tokens=diag.semi_stable_estimated_tokens,
                volatile_estimated_tokens=diag.volatile_estimated_tokens,
                stable_prefix_bytes=diag.stable_prefix_bytes,
                semi_stable_bytes=diag.semi_stable_bytes,
                volatile_bytes=diag.volatile_bytes,
                segmentation_summary_json=diag.segmentation_summary_json,
                transcoded=1 if data.transcoded else 0,
                raw_usage_json=raw_usage_json_value,
                compression_status=diag.compression_status,
                compression_mode=diag.compression_mode,
                compression_candidate_count=diag.compression_candidate_count,
                compression_eligible_candidate_count=diag.compression_eligible_candidate_count,
                compression_suppressed_candidate_count=diag.compression_suppressed_candidate_count,
                compression_estimated_original_tokens=diag.compression_estimated_original_tokens,
                compression_estimated_compressed_tokens=diag.compression_estimated_compressed_tokens,
                compression_estimated_savings_tokens=diag.compression_estimated_savings_tokens,
                compression_analyzer_latency_ms=diag.compression_analyzer_latency_ms,
                compression_warning_count=diag.compression_warning_count,
                compression_reason_code_counts_json=diag.compression_reason_code_counts_json,
                compression_summary_json=diag.compression_summary_json,
                compression_applied=diag.compression_applied,
                compression_transform_count=diag.compression_transform_count,
                compression_transforms_by_reason_json=diag.compression_transforms_by_reason_json,
                compression_original_tokens=diag.compression_original_tokens,
                compression_compressed_tokens=diag.compression_compressed_tokens,
                compression_savings_tokens=diag.compression_savings_tokens,
                compression_pre_stable_prefix_hash=diag.compression_pre_stable_prefix_hash,
                compression_post_stable_prefix_hash=diag.compression_post_stable_prefix_hash,
                compression_stable_prefix_preserved=diag.compression_stable_prefix_preserved,
                compression_warnings_json=diag.compression_warnings_json,
                compression_latency_ms=diag.compression_latency_ms,
                compression_failed_fallback=diag.compression_failed_fallback,
                compression_applied_summary_json=diag.compression_applied_summary_json,
                compression_policy_name=diag.compression_policy_name,
                compression_policy_source=diag.compression_policy_source,
                compression_policy_warnings_json=diag.compression_policy_warnings_json,
                synthetic_cache_status=diag.synthetic_cache_status,
                synthetic_cache_dry_run=diag.synthetic_cache_dry_run,
                synthetic_cache_candidate_count=diag.synthetic_cache_candidate_count,
                synthetic_cache_applied_count=diag.synthetic_cache_applied_count,
                synthetic_cache_warning_count=diag.synthetic_cache_warning_count,
                synthetic_cache_warnings_json=diag.synthetic_cache_warnings_json,
                synthetic_cache_policy_name=diag.synthetic_cache_policy_name,
                synthetic_cache_policy_source=diag.synthetic_cache_policy_source,
                synthetic_cache_summary_json=diag.synthetic_cache_summary_json,
            )

            # 4. Finalize attempt only if request transitioned and attempt
            #    is still incomplete (idempotent; preserves first terminal data)
            request_row = await self._request_repo.get_by_id(db_request_id)
            request_terminal = transitioned or bool(
                isinstance(request_row, dict)
                and request_row.get("status") in REQUEST_TERMINAL_STATUSES
            )
            if request_terminal:
                attempt_transitioned = bool(
                    await self._attempt_repo.finalize_if_incomplete(
                        attempt_id=selected.attempt_id,
                        status_code=data.status_code,
                        error_class=data.error_class,
                        error_detail=error_detail,
                        upstream_request_id=data.upstream_request_id,
                        bytes_emitted=data.bytes_emitted,
                        retry_category=self._retry_category_for_outcome(data.outcome),
                        release_reason=data.release_reason
                        or self._release_reason_for_outcome(data.outcome),
                    )
                )

                # 5. Release reservation
                reservation_released = bool(
                    await self._reservation_repo.release(
                        selected.reservation_id, reason=status
                    )
                )

                attempt_row = await self._attempt_repo.get_by_id(selected.attempt_id)
                attempt_terminal = attempt_transitioned or bool(
                    isinstance(attempt_row, dict)
                    and attempt_row.get("completed_at") is not None
                )
                reservation_status = await self._reservation_repo.get_status(
                    selected.reservation_id
                )
                reservation_terminal = reservation_released or reservation_status in {
                    "released",
                    "expired",
                }

                # 6. Insert account event for significant failures
                if (
                    data.outcome
                    in (
                        FinalizationOutcome.UPSTREAM_ERROR,
                        FinalizationOutcome.INTERRUPTED,
                    )
                    and data.error_class
                ):
                    # Plan 028 Workstream G: account event enrichment
                    # is best-effort and moved outside the correctness
                    # transaction to avoid extending the write-lock.
                    pass

            # Commit happens via context manager

        # Plan 028 Workstream G: best-effort account event enrichment
        # runs AFTER the correctness transaction commits so it cannot
        # extend the SQLite write-lock duration.
        if (
            transitioned
            and data.outcome
            in (
                FinalizationOutcome.UPSTREAM_ERROR,
                FinalizationOutcome.INTERRUPTED,
            )
            and data.error_class
        ):
            try:
                # Plan 028 Workstream G: reuse the integer account_id
                # already resolved during account selection instead of
                # issuing a redundant SELECT by name.
                account_id: int | None = getattr(selected, "account_id", None)
                if account_id is not None:
                    event_repo = AccountEventRepository(self._db)
                    async with self._db.transaction():
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
                    self._clear_quarantine_on_success(selected, mid)
                elif data.outcome in (
                    FinalizationOutcome.UPSTREAM_ERROR,
                    FinalizationOutcome.TIMEOUT,
                    FinalizationOutcome.INTERRUPTED,
                ):
                    applied = self._apply_finalizer_failure_effects(
                        selected=selected,
                        mid=mid,
                        error_class=data.error_class,
                        status_code=data.status_code,
                    )
                    if not applied:
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

        return DurableFinalizationResult(
            request_terminal=request_terminal,
            request_transitioned=transitioned,
            attempt_transitioned=attempt_transitioned,
            attempt_terminal=attempt_terminal,
            reservation_terminal=reservation_terminal,
            reservation_transitioned=reservation_released,
            retryable=not (
                request_terminal and attempt_terminal and reservation_terminal
            ),
            detail=(
                "durable finalization incomplete"
                if not (request_terminal and attempt_terminal and reservation_terminal)
                else ""
            ),
        )

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

    @staticmethod
    def _release_reason_for_outcome(outcome: FinalizationOutcome) -> str:
        """Return the durable release reason for terminal request outcomes."""
        if outcome == FinalizationOutcome.CLIENT_ERROR:
            return "capability_rejected"
        if outcome == FinalizationOutcome.CLIENT_CANCELLED:
            return "client_cancelled"
        if outcome == FinalizationOutcome.COMPLETED:
            return "completed"
        return "attempt_failed"

    @staticmethod
    def _retry_category_for_outcome(outcome: FinalizationOutcome) -> str:
        """Return the persisted retry classification for terminal outcomes."""
        if outcome == FinalizationOutcome.CLIENT_ERROR:
            return "never"
        return "none"

    def _precompute_finalization_diagnostics(
        self,
        data: FinalizationData,
    ) -> _FinalizationDiagnosticSnapshot:
        """Precompute all diagnostic serialization BEFORE the DB transaction.

        Plan 028 Workstream G: moves segmentation summary JSON,
        compression observation/result JSON, synthetic cache JSON, and
        resolved policy JSON construction outside the ``BEGIN IMMEDIATE``
        critical section so they do not extend the SQLite write-lock
        duration.

        This method is pure (no I/O) — all inputs come from the
        ``FinalizationData`` argument.
        """
        # --- segmentation fields ---
        segmentation_obj = data.segmentation
        if data.segmentation_not_collected:
            seg_status = "not_collected"
        else:
            seg_status = "empty_request"
        seg_stable_hash: str | None = None
        seg_shape_hash: str | None = None
        seg_stable_tokens: int | None = None
        seg_semi_tokens: int | None = None
        seg_volatile_tokens: int | None = None
        seg_stable_bytes: int | None = None
        seg_semi_bytes: int | None = None
        seg_volatile_bytes: int | None = None
        seg_summary_json: str | None = None
        if segmentation_obj is not None:
            seg_status = str(getattr(segmentation_obj, "status", "empty_request"))
            seg_stable_hash = getattr(segmentation_obj, "stable_prefix_hash", None)
            seg_shape_hash = getattr(segmentation_obj, "request_shape_hash", None)
            seg_stable_tokens = getattr(
                segmentation_obj, "stable_prefix_estimated_tokens", None
            )
            seg_semi_tokens = getattr(
                segmentation_obj, "semi_stable_estimated_tokens", None
            )
            seg_volatile_tokens = getattr(
                segmentation_obj, "volatile_estimated_tokens", None
            )
            seg_stable_bytes = getattr(segmentation_obj, "stable_prefix_bytes", None)
            seg_semi_bytes = getattr(segmentation_obj, "semi_stable_bytes", None)
            seg_volatile_bytes = getattr(segmentation_obj, "volatile_bytes", None)
            try:
                from eggpool.transcoder.segmentation import (
                    segmentation_summary_json,
                )

                seg_summary_json = segmentation_summary_json(segmentation_obj)
            except (TypeError, ValueError):
                seg_summary_json = None

        # --- compression observation fields ---
        compression_obj = data.compression_observation
        comp_status = "disabled"
        comp_mode: str | None = None
        comp_candidate_count = 0
        comp_eligible_count = 0
        comp_suppressed_count = 0
        comp_orig_tokens: int | None = None
        comp_comp_tokens: int | None = None
        comp_savings_tokens: int | None = None
        comp_latency_ms: float | None = None
        comp_warning_count = 0
        comp_reason_counts_json: str | None = None
        comp_summary_json: str | None = None
        if compression_obj is not None:
            comp_status = "observed"
            comp_mode = str(getattr(compression_obj, "mode", "observe"))
            comp_candidate_count = int(
                getattr(compression_obj, "candidate_count", 0) or 0
            )
            comp_eligible_count = int(
                getattr(compression_obj, "eligible_candidate_count", 0) or 0
            )
            comp_suppressed_count = int(
                getattr(compression_obj, "suppressed_candidate_count", 0) or 0
            )
            comp_orig_tokens = getattr(
                compression_obj, "estimated_original_tokens", None
            )
            comp_comp_tokens = getattr(
                compression_obj, "estimated_compressed_tokens", None
            )
            comp_savings_tokens = getattr(
                compression_obj, "estimated_savings_tokens", None
            )
            latency_value = getattr(compression_obj, "analyzer_latency_ms", None)
            if isinstance(latency_value, (int, float)):
                comp_latency_ms = float(latency_value)
            warnings_value = getattr(compression_obj, "warnings", None)
            if isinstance(warnings_value, (list, tuple)):
                comp_warning_count = len(list(warnings_value))  # type: ignore[arg-type]
            try:
                reason_counts = getattr(compression_obj, "reason_code_counts", None)
                if reason_counts is not None:
                    comp_reason_counts_json = json.dumps(
                        dict(reason_counts), default=str, sort_keys=True
                    )
            except (TypeError, ValueError):
                comp_reason_counts_json = None
            try:
                to_json = getattr(compression_obj, "to_summary_json", None)
                if callable(to_json):
                    summary_value = to_json()
                    if isinstance(summary_value, str):
                        comp_summary_json = summary_value
            except (TypeError, ValueError):
                comp_summary_json = None

        # --- compression result fields ---
        compression_result_obj = data.compression_result
        comp_applied = 0
        comp_transform_count = 0
        comp_transforms_by_reason_json: str | None = None
        comp_orig_tok: int | None = None
        comp_comp_tok: int | None = None
        comp_savings_tok: int | None = None
        comp_pre_hash: str | None = None
        comp_post_hash: str | None = None
        comp_stable_preserved = 1
        comp_warnings_json: str | None = None
        comp_result_latency_ms = 0.0
        comp_failed_fallback = 0
        comp_applied_summary_json: str | None = None
        if compression_result_obj is not None:
            comp_applied = 1 if getattr(compression_result_obj, "applied", False) else 0
            comp_transform_count = int(
                getattr(compression_result_obj, "transform_count", 0) or 0
            )
            transforms_by_reason = getattr(
                compression_result_obj, "transforms_by_reason", None
            )
            if transforms_by_reason is not None:
                try:
                    comp_transforms_by_reason_json = json.dumps(
                        dict(transforms_by_reason),
                        default=str,
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                except (TypeError, ValueError):
                    comp_transforms_by_reason_json = None
            comp_orig_tok = getattr(compression_result_obj, "original_tokens", None)
            if isinstance(comp_orig_tok, (int, float)):
                comp_orig_tok = int(comp_orig_tok)
            else:
                comp_orig_tok = None
            comp_comp_tok = getattr(compression_result_obj, "compressed_tokens", None)
            if isinstance(comp_comp_tok, (int, float)):
                comp_comp_tok = int(comp_comp_tok)
            else:
                comp_comp_tok = None
            comp_savings_tok = getattr(compression_result_obj, "savings_tokens", None)
            if isinstance(comp_savings_tok, (int, float)):
                comp_savings_tok = int(comp_savings_tok)
            else:
                comp_savings_tok = None
            comp_pre_hash = getattr(
                compression_result_obj, "pre_stable_prefix_hash", None
            )
            comp_post_hash = getattr(
                compression_result_obj, "post_stable_prefix_hash", None
            )
            comp_stable_preserved = (
                1
                if getattr(compression_result_obj, "stable_prefix_preserved", True)
                else 0
            )
            warnings_raw = getattr(compression_result_obj, "warnings", None)
            if isinstance(warnings_raw, (list, tuple)):
                try:
                    comp_warnings_json = json.dumps(
                        list(warnings_raw),  # type: ignore[arg-type]
                        default=str,
                        ensure_ascii=False,
                    )
                except (TypeError, ValueError):
                    comp_warnings_json = None
            latency_val = getattr(compression_result_obj, "latency_ms", 0.0)
            if isinstance(latency_val, (int, float)):
                comp_result_latency_ms = float(latency_val)
            comp_failed_fallback = (
                1 if getattr(compression_result_obj, "failed_fallback", False) else 0
            )
            if comp_applied:
                to_json = getattr(compression_result_obj, "summary_json", None)
                if to_json is not None:
                    comp_applied_summary_json = (
                        to_json if isinstance(to_json, str) else None
                    )

        # --- resolved compression policy fields ---
        resolved_policy_obj = data.resolved_compression_policy
        policy_name: str | None = None
        policy_source: str | None = None
        policy_warnings_json: str | None = None
        if resolved_policy_obj is not None:
            name_attr = getattr(resolved_policy_obj, "name", None)
            if isinstance(name_attr, str) and name_attr:
                policy_name = name_attr
            source_attr = getattr(resolved_policy_obj, "source", None)
            if isinstance(source_attr, str) and source_attr:
                policy_source = source_attr
            warnings_attr = getattr(resolved_policy_obj, "warnings", None)
            if isinstance(warnings_attr, (list, tuple)):
                try:
                    policy_warnings_json = json.dumps(
                        [str(w) for w in warnings_attr],  # type: ignore[arg-type]
                        ensure_ascii=False,
                    )
                except (TypeError, ValueError):
                    policy_warnings_json = None

        # --- synthetic cache fields ---
        synthetic_cache_obj = getattr(data, "synthetic_cache_result", None)
        sc_status: str | None = None
        sc_dry_run = 1
        sc_candidate_count = 0
        sc_applied_count = 0
        sc_warning_count = 0
        sc_warnings_json: str | None = None
        sc_policy_name: str | None = None
        sc_policy_source: str | None = None
        sc_summary_json: str | None = None
        if synthetic_cache_obj is not None:
            status_attr = getattr(synthetic_cache_obj, "status", None)
            if isinstance(status_attr, str) and status_attr:
                sc_status = status_attr
            dry_run_attr = getattr(synthetic_cache_obj, "dry_run", None)
            if isinstance(dry_run_attr, bool):
                sc_dry_run = 1 if dry_run_attr else 0
            candidate_count_attr = getattr(synthetic_cache_obj, "candidate_count", 0)
            if isinstance(candidate_count_attr, int):
                sc_candidate_count = candidate_count_attr
            applied_count_attr = getattr(synthetic_cache_obj, "applied_count", 0)
            if isinstance(applied_count_attr, int):
                sc_applied_count = applied_count_attr
            warning_count_attr = getattr(synthetic_cache_obj, "warning_count", 0)
            if isinstance(warning_count_attr, int):
                sc_warning_count = warning_count_attr
            warnings_attr = getattr(synthetic_cache_obj, "warnings", None)
            if isinstance(warnings_attr, (list, tuple)):
                try:
                    sc_warnings_json = json.dumps(
                        [
                            str(w)
                            for w in cast(
                                "list[Any] | tuple[Any, ...]",
                                warnings_attr,
                            )
                        ],
                        ensure_ascii=False,
                    )
                except (TypeError, ValueError):
                    sc_warnings_json = None
            plan_attr = getattr(synthetic_cache_obj, "plan", None)
            if plan_attr is not None:
                pname_attr = getattr(plan_attr, "policy_name", None)
                if isinstance(pname_attr, str) and pname_attr:
                    sc_policy_name = pname_attr
                psource_attr = getattr(plan_attr, "policy_source", None)
                if isinstance(psource_attr, str) and psource_attr:
                    sc_policy_source = psource_attr
            summary_attr = getattr(synthetic_cache_obj, "summary_json", None)
            if isinstance(summary_attr, str):
                sc_summary_json = summary_attr

        return _FinalizationDiagnosticSnapshot(
            segmentation_status=seg_status,
            stable_prefix_hash=seg_stable_hash,
            request_shape_hash=seg_shape_hash,
            stable_prefix_estimated_tokens=seg_stable_tokens,
            semi_stable_estimated_tokens=seg_semi_tokens,
            volatile_estimated_tokens=seg_volatile_tokens,
            stable_prefix_bytes=seg_stable_bytes,
            semi_stable_bytes=seg_semi_bytes,
            volatile_bytes=seg_volatile_bytes,
            segmentation_summary_json=seg_summary_json,
            compression_status=comp_status,
            compression_mode=comp_mode,
            compression_candidate_count=comp_candidate_count,
            compression_eligible_candidate_count=comp_eligible_count,
            compression_suppressed_candidate_count=comp_suppressed_count,
            compression_estimated_original_tokens=comp_orig_tokens,
            compression_estimated_compressed_tokens=comp_comp_tokens,
            compression_estimated_savings_tokens=comp_savings_tokens,
            compression_analyzer_latency_ms=comp_latency_ms,
            compression_warning_count=comp_warning_count,
            compression_reason_code_counts_json=comp_reason_counts_json,
            compression_summary_json=comp_summary_json,
            compression_applied=comp_applied,
            compression_transform_count=comp_transform_count,
            compression_transforms_by_reason_json=comp_transforms_by_reason_json,
            compression_original_tokens=comp_orig_tok,
            compression_compressed_tokens=comp_comp_tok,
            compression_savings_tokens=comp_savings_tok,
            compression_pre_stable_prefix_hash=comp_pre_hash,
            compression_post_stable_prefix_hash=comp_post_hash,
            compression_stable_prefix_preserved=comp_stable_preserved,
            compression_warnings_json=comp_warnings_json,
            compression_latency_ms=comp_result_latency_ms,
            compression_failed_fallback=comp_failed_fallback,
            compression_applied_summary_json=comp_applied_summary_json,
            compression_policy_name=policy_name,
            compression_policy_source=policy_source,
            compression_policy_warnings_json=policy_warnings_json,
            synthetic_cache_status=sc_status,
            synthetic_cache_dry_run=sc_dry_run,
            synthetic_cache_candidate_count=sc_candidate_count,
            synthetic_cache_applied_count=sc_applied_count,
            synthetic_cache_warning_count=sc_warning_count,
            synthetic_cache_warnings_json=sc_warnings_json,
            synthetic_cache_policy_name=sc_policy_name,
            synthetic_cache_policy_source=sc_policy_source,
            synthetic_cache_summary_json=sc_summary_json,
        )

    def _apply_finalizer_failure_effects(
        self,
        *,
        selected: Any,
        mid: str,
        error_class: str | None,
        status_code: int | None,
    ) -> bool:
        """Apply Plan 025 typed effects for a finalization failure.

        Returns ``True`` when the effects applier consumed the
        failure (whether or not it mutated state), ``False`` when
        no applier is wired — the caller falls back to the legacy
        :func:`classify_failure_category` path.
        """
        if self._effects_applier is None or self._health_manager is None:
            return False
        provider_id = getattr(selected, "provider_id", None) or "unknown"
        upstream_protocol = getattr(selected, "protocol", None) or "openai"
        client_protocol = upstream_protocol
        observation = FailureObservation(
            source="upstream_http",
            status_code=status_code,
            error_class=error_class,
            provider_id=provider_id,
            account_name=selected.account_name,
            model_id=mid,
            upstream_model_id=mid,
            client_protocol=client_protocol,
            upstream_protocol=upstream_protocol,
            response_signal=None,
            retry_after_s=None,
            response_started=False,
        )
        effects = classify_failure_effects(observation)
        attempt_key = (
            f"finalize|{selected.account_name}|{mid}|{provider_id}"
            f"|{upstream_protocol}|{status_code}|{error_class or ''}"
        )
        self._effects_applier.apply_once(
            attempt_key=attempt_key,
            observation=observation,
            effects=effects,
        )
        return True

    def _clear_quarantine_on_success(self, selected: Any, mid: str) -> None:
        """Clear bounded quarantine on successful completion.

        Successful traffic demonstrates recovery from bounded
        quarantine; the finalizer is the authoritative place to
        record that.  Operator-disabled entries and terminal
        withdrawals remain unaffected.
        """
        if self._effects_applier is None:
            return
        provider_id = getattr(selected, "provider_id", None)
        upstream_protocol = getattr(selected, "protocol", None) or "openai"
        if not provider_id or not mid:
            return
        self._effects_applier.clear_on_success(
            provider_id=provider_id,
            account_id=selected.account_name,
            canonical_model_id=mid,
            upstream_model_id=mid,
            upstream_protocol=upstream_protocol,
        )


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
