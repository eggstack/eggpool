"""Request coordinator: central orchestration boundary for proxy lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

from eggpool.constants import DEFAULT_PROVIDER_ID
from eggpool.db.repositories import (
    AccountBackoffRepository,
    AccountRepository,
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    RoutingDecisionRepository,
    UsageWindowRepository,
)
from eggpool.errors import (
    AggregatorError,
    AuthenticationError,
    CapabilityError,
    DatabaseError,
    ModelNotFoundError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitError,
    TemporaryUpstreamError,
    TransientUpstreamError,
    UpstreamError,
    UpstreamExhaustedError,
)
from eggpool.health.health_manager import (
    FailureCategory,
    classify_failure_category,
)
from eggpool.metrics.thinking import get_counter
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.providers.contract import (
    build_auth_headers,
    build_static_headers,
    build_upstream_headers,
    compose_provider_url,
)
from eggpool.proxy.client import filter_response_headers
from eggpool.proxy.sse_observer import IncrementalSSEObserver
from eggpool.proxy.usage import (
    StreamUsageResult,
    extract_anthropic_response_usage,
    extract_openai_response_usage,
)
from eggpool.request.attempt_finalizer import (
    AttemptFinalizationData,
    AttemptFinalizer,
)
from eggpool.request.body import encode_json_body
from eggpool.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)
from eggpool.request.limits import estimate_reservation_tokens
from eggpool.retry.classification import RetryCategory, RetryClassifier
from eggpool.routing.router import RoutingDecisionTrace, RoutingExclusion
from eggpool.security.redaction import redact_error_detail
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.protocol import BodyTranscoder, select_transcoder
from eggpool.transcoder.streaming import select_streaming_transcoder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from eggpool.accounts.registry import AccountRegistry
    from eggpool.catalog.capabilities import ThinkingCapability
    from eggpool.catalog.pricing import CostCalculator
    from eggpool.catalog.service import CatalogService
    from eggpool.db.connection import Database
    from eggpool.health.health_manager import HealthManager
    from eggpool.models.config import AppConfig
    from eggpool.quota.estimation import QuotaEstimator
    from eggpool.routing.router import Router
    from eggpool.transcoder.policy import TranscoderPolicy

logger = logging.getLogger(__name__)


def _redact_auth_shape(auth_headers: dict[str, str]) -> str:
    """Return a redacted representation of auth headers for debug logging."""
    if not auth_headers:
        return "none"
    parts: list[str] = []
    for name, value in auth_headers.items():
        if len(value) > 10:
            parts.append(f"{name}: {value[:4]}***{value[-4:]}")
        else:
            parts.append(f"{name}: ***")
    return ", ".join(parts)


# Default maximum retry attempts for pre-body failures
DEFAULT_MAX_RETRY_ATTEMPTS = 3

# Ordered list of upstream request-ID header names checked during
# finalization.  The first non-empty match wins.
_UPSTREAM_REQUEST_ID_HEADERS: list[str] = [
    "x-request-id",
    "request-id",
    "anthropic-request-id",
    "x-amzn-requestid",
]

_TRANSIENT_BACKOFF_REASONS: tuple[str, ...] = (
    "quota_exhausted",
    "rate_limited",
    "upstream_server_error",
    "connect_timeout",
    "connection_failure",
    "protocol_error",
)


def _prepare_error_detail(value: object | None, persist: bool) -> str | None:
    """Redact error detail only when persistence is enabled."""
    if not persist or value is None:
        return None
    return redact_error_detail(str(value))


def _serialize_thinking_trace(trace: dict[str, Any] | None) -> str | None:
    """Serialize thinking trace to JSON for persistence."""
    return json.dumps(trace) if trace else None


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    """Return ``numerator / denominator`` as a utilization ratio.

    Returns ``None`` when the denominator is zero or negative so the
    payload distinguishes "no capacity configured" from "0 % used".
    Used by ``_build_score_components`` to surface per-window
    utilization alongside the raw cost and capacity values.
    """
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


@dataclass(slots=True)
class ProxyRequestContext:
    """Input context for a proxy request."""

    request_id: str
    protocol: str  # 'openai' or 'anthropic'
    model_id: str
    streaming: bool
    original_body: bytes
    incoming_headers: dict[str, str]
    started_at: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.monotonic)
    started_monotonic_ns: int = field(default_factory=time.perf_counter_ns)
    client_metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    attempted_accounts: set[str] = field(default_factory=set[str])
    provider_id: str | None = None
    client_ip: str = ""
    upstream_body: bytes | None = None
    upstream_connect_ms: int | None = None
    upstream_headers_ms: int | None = None
    upstream_protocol: str = ""
    transcode_required: bool = False
    transcode_context: TranscodeContext | None = None
    thinking_trace: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.upstream_protocol:
            self.upstream_protocol = self.protocol

    @property
    def body_for_upstream(self) -> bytes:
        """Return the dispatch body, preserving original client bytes separately."""
        return self.original_body if self.upstream_body is None else self.upstream_body


@dataclass(frozen=True, slots=True)
class SelectedAttempt:
    """Result of atomic pre-dispatch selection.

    Contains all data needed to execute an upstream request and finalize later.
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
    provider_id: str = DEFAULT_PROVIDER_ID
    requires_transcode: bool = False
    protocol: str = "openai"
    streamed: bool = False


@dataclass(slots=True)
class PreparedProxyResponse:
    """Result of executing a proxy request through the coordinator."""

    status_code: int
    headers: list[tuple[str, str]]
    body: bytes | None = None  # for non-streaming
    stream_iterator: AsyncIterator[bytes] | None = None  # for streaming
    request_id: str = ""
    account_name: str = ""
    usage: StreamUsageResult | None = None
    latency_ms: int = 0
    attempt_count: int = 1


class RequestCoordinator:
    """Orchestrates the full proxy request lifecycle.

    Responsibilities:
    - Create pending request records
    - Select accounts via router
    - Create reservations and attempt records atomically before upstream dispatch
    - Open upstream connections
    - For non-streaming: read body, extract usage, calculate cost, finalize
    - For streaming: build streaming response with usage extraction
    - On error: finalize via RequestFinalizer, release reservation, update health
    - Pre-body failures retry on another account (excluding failed accounts)
    """

    def __init__(
        self,
        registry: AccountRegistry,
        catalog: CatalogService,
        router: Router,
        db: Database,
        client_pool: ProviderClientPool | httpx.AsyncClient,
        request_repo: RequestRepository | None = None,
        reservation_repo: ReservationRepository | None = None,
        attempt_repo: AttemptRepository | None = None,
        usage_window_repo: UsageWindowRepository | None = None,
        health_manager: HealthManager | None = None,
        cost_calculator: CostCalculator | None = None,
        quota_estimator: QuotaEstimator | None = None,
        max_retry_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS,
        quota_exhausted_cooldown_seconds: float = 300.0,
        persist_error_detail: bool = False,
        config: AppConfig | None = None,
        account_backoff_repo: AccountBackoffRepository | None = None,
        routing_decision_repo: RoutingDecisionRepository | None = None,
        metrics_coalescer: Any | None = None,  # noqa: ANN401
        dispatch_overhead_recorder: Any | None = None,  # noqa: ANN401
        transcoder_policy: TranscoderPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._router = router
        self._db = db
        self._config = config
        if isinstance(client_pool, ProviderClientPool):
            self._client_pool: ProviderClientPool | None = client_pool
            self._client = client_pool.get_default_client()
        else:
            self._client_pool = None
            self._client = client_pool
        self._request_repo = request_repo
        self._reservation_repo = reservation_repo
        self._attempt_repo = attempt_repo
        self._usage_window_repo = usage_window_repo
        self._health_manager = health_manager
        self._cost_calculator = cost_calculator
        self._quota_estimator = quota_estimator
        self._classifier = RetryClassifier()
        self._select_lock = asyncio.Lock()
        self._account_id_cache: dict[str, int] = {}
        self._max_retry_attempts = max_retry_attempts
        self._quota_exhausted_cooldown_seconds = quota_exhausted_cooldown_seconds
        self._persist_error_detail = persist_error_detail
        self._account_backoff_repo = account_backoff_repo
        self._routing_decision_repo = (
            routing_decision_repo
            if routing_decision_repo is not None
            else RoutingDecisionRepository(db)
        )
        self._metrics_coalescer = metrics_coalescer
        self._dispatch_overhead_recorder = dispatch_overhead_recorder
        self._transcoder_policy = transcoder_policy

        # Build the attempt finalizer with all dependencies
        self._attempt_finalizer = AttemptFinalizer(
            db=db,
            attempt_repo=attempt_repo or AttemptRepository(db),
            reservation_repo=reservation_repo or ReservationRepository(db),
            persist_error_detail=persist_error_detail,
        )

        # Build the finalizer with all dependencies
        self._finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo or RequestRepository(db),
            attempt_repo=attempt_repo or AttemptRepository(db),
            reservation_repo=reservation_repo or ReservationRepository(db),
            cost_calculator=cost_calculator,
            quota_estimator=quota_estimator,
            router=router,
            registry=registry,
            health_manager=health_manager,
            persist_error_detail=persist_error_detail,
            metrics_coalescer=metrics_coalescer,
        )

    def _get_client(
        self,
        provider_id: str | None = None,
        account_name: str | None = None,
    ) -> httpx.AsyncClient:
        """Return the exact provider client selected for this request.

        A provider-aware pool must fail closed when the selected provider is
        missing. Falling back to another provider can send credentials and
        payloads to the wrong upstream.
        """
        if provider_id and self._client_pool is not None:
            return self._client_pool.get_client(provider_id, account_name)
        if self._client is None:
            raise UpstreamError("No HTTP client available for upstream requests")
        return self._client

    def _log_transcode_warnings(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt | None = None,
    ) -> None:
        """Emit structured logs for transcoded requests and loss warnings."""
        if context.transcode_context is None:
            return
        warnings = context.transcode_context.loss_warnings
        if warnings:
            logger.info(
                "transcode.loss_warnings request_id=%s "
                "client=%s upstream=%s warnings=%s",
                context.request_id,
                context.protocol,
                context.upstream_protocol,
                warnings,
            )
        # Phase 5: per-request structured log for every transcoded request
        if selected is not None:
            logger.info(
                "transcoded_request request_id=%s client=%s upstream=%s "
                "account=%s provider=%s native_match=%s "
                "loss_warnings=%d bytes_in=%d bytes_out=%d",
                context.request_id,
                context.protocol,
                context.upstream_protocol,
                selected.account_name,
                selected.provider_id,
                context.protocol == context.upstream_protocol,
                len(warnings),
                len(context.original_body),
                # v1: the coordinator does not track response bytes
                # consistently across non-streaming, streaming, and
                # error paths, so this is a constant for now. Future
                # work: thread bytes_emitted through to here.
                0,
            )

    async def execute(self, context: ProxyRequestContext) -> PreparedProxyResponse:
        """Execute a request through the full lifecycle.

        Returns a PreparedProxyResponse with either body (non-streaming)
        or stream_iterator (streaming). On retryable pre-body failures,
        retries on different accounts (excluding previously attempted ones).
        """
        # Section 10.5: Validate endpoint before durable selection.
        # Reject mismatched protocol endpoints before creating any
        # request, reservation, or attempt row.
        self._validate_endpoint_or_transcode(context)

        # Phase 2: select the body transcoder when client and upstream
        # protocols differ and transcoding is enabled.
        transcoder: BodyTranscoder | None = None
        if (
            context.transcode_context is not None
            and not context.transcode_context.is_native()
        ):
            transcoder = select_transcoder(
                client_protocol=context.transcode_context.client_protocol,
                upstream_protocol=context.transcode_context.upstream_protocol,
            )
            if transcoder is not None:
                try:
                    payload = json.loads(context.body_for_upstream)
                except (json.JSONDecodeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    _features = (
                        self._transcoder_policy.features
                        if self._transcoder_policy is not None
                        else None
                    )
                    _thinking_cap: ThinkingCapability | None = None
                    _budget_defaults: dict[str, int] | None = None
                    _budget_policy = "lenient"
                    if self._transcoder_policy is not None:
                        _budget_defaults = (
                            self._transcoder_policy.thinking_budget_defaults.as_dict()
                        )
                        _budget_policy = (
                            self._transcoder_policy.budget_resolution_policy
                        )
                    # Look up thinking capability from catalog cache for
                    # budget resolution (best-effort; None is safe).
                    try:
                        from eggpool.catalog.capabilities import (
                            dict_to_model_capabilities,
                        )

                        model_info = self._catalog.cache.get_model(
                            context.model_id,
                        )
                        if model_info is not None:
                            caps_raw: dict[str, Any] = model_info.get(
                                "capabilities",
                                {},
                            )  # type: ignore[assignment]
                            caps = dict_to_model_capabilities(caps_raw)
                            _thinking_cap = caps.thinking
                    except Exception:  # noqa: BLE001
                        pass  # best-effort; resolver has its own fallbacks
                    translated, warnings = transcoder.encode_request(
                        cast("dict[str, Any]", payload),
                        context.transcode_context,
                        features=_features,
                        thinking_capability=_thinking_cap,
                        budget_defaults=_budget_defaults,
                        budget_resolution_policy=_budget_policy,
                    )
                    context.upstream_body = encode_json_body(translated)
                    context.transcode_context.loss_warnings.extend(warnings)

                    # Determine thinking decision from transcoder warnings
                    if context.thinking_trace is not None:
                        thinking_warnings = [
                            w
                            for w in context.transcode_context.loss_warnings
                            if w.get("kind")
                            in (
                                "thinking_signature_dropped",
                                "reasoning_content_dropped",
                                "budget_clamped",
                                "unknown_effort",
                                "budget_rejected",
                                "budget_resolution_no_input",
                                "dropped_field",
                            )
                            and "thinking" in str(w.get("field", ""))
                        ]
                        if any(
                            w.get("kind") == "reasoning_content_dropped"
                            for w in thinking_warnings
                        ):
                            context.thinking_trace["decision"] = "dropped"
                        elif any(
                            w.get("kind") == "budget_clamped" for w in thinking_warnings
                        ):
                            context.thinking_trace["decision"] = "clamped"
                            context.thinking_trace["budget_clamped"] = True
                        elif any(
                            w.get("kind") == "budget_rejected"
                            for w in thinking_warnings
                        ):
                            context.thinking_trace["decision"] = "rejected"
                        else:
                            context.thinking_trace["decision"] = "transcoded"

                        # Record the final thinking decision
                        _thinking_counter = get_counter()
                        decision = context.thinking_trace["decision"]
                        if decision == "transcoded":
                            await _thinking_counter.increment_transcoded(
                                client_protocol=context.thinking_trace[
                                    "client_protocol"
                                ],
                                upstream_protocol=context.upstream_protocol,
                                provider_id="unknown",
                            )
                        elif decision == "dropped":
                            await _thinking_counter.increment_dropped(
                                client_protocol=context.thinking_trace[
                                    "client_protocol"
                                ],
                                upstream_protocol=context.upstream_protocol,
                                reason="reasoning_content_dropped",
                            )
                        elif decision == "clamped":
                            await _thinking_counter.increment_budget_clamped(
                                client_protocol=context.thinking_trace[
                                    "client_protocol"
                                ],
                                provider_id="unknown",
                            )
                        elif decision == "rejected":
                            await _thinking_counter.increment_rejected(
                                client_protocol=context.thinking_trace[
                                    "client_protocol"
                                ],
                                capability_status="budget_rejected",
                            )

                # Native path: thinking controls pass through unchanged
                if (
                    context.thinking_trace is not None
                    and context.thinking_trace["decision"] == "none"
                ):
                    context.thinking_trace["decision"] = "passthrough"
                    context.thinking_trace["upstream_protocol"] = (
                        context.upstream_protocol
                    )

        last_error: Exception | None = None
        last_upstream_response: tuple[int, list[tuple[str, str]], bytes] | None = None
        attempt_num = 0
        last_selected: SelectedAttempt | None = None
        health_applied = False

        for attempt_num in range(1, self._max_retry_attempts + 1):
            try:
                selected = await self._select_and_persist_attempt(context, attempt_num)
            except asyncio.CancelledError:
                raise
            except ModelUnavailableError as err:
                # Only overwrite last_error if we don't have an upstream error
                if last_error is None or not isinstance(
                    last_error, (_RetryableUpstreamError, _NonRetryableUpstreamError)
                ):
                    last_error = err
                # If no upstream attempt was dispatched yet, finalize the
                # request directly so it does not remain pending.  When
                # last_selected exists, upstream attempts already ran; break
                # and let _handle_exhausted() finalize from the last
                # upstream error/response.
                if last_selected is None:
                    db_request_id = context.client_metadata.get("db_request_id")
                    if db_request_id is not None and self._request_repo is not None:
                        async with self._db.transaction():
                            await self._request_repo.finalize_if_pending(
                                request_id=db_request_id,
                                status="error",
                                error_class=type(err).__name__,
                                thinking_trace_json=_serialize_thinking_trace(
                                    context.thinking_trace
                                ),
                            )
                break
            except AuthenticationError as err:
                last_error = err
                logger.warning(
                    "Auth failure on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                # Auth failure on account selection - health already
                # updated by finalizer or selection. Retry with another
                # account if available.
                continue
            except Exception as err:
                last_error = err
                logger.warning(
                    "Selection failed on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                # PostCommitInterrupted means the attempt was finalized
                # and reservation released by the compensation block, but
                # health was never updated. Since this is a system-level
                # interruption (not an upstream error), we mark health as
                # already applied to prevent the finalizer from double-
                # applying it.
                if context.client_metadata.get("post_commit_interrupted"):
                    health_applied = True
                break

            last_selected = selected
            try:
                result = await self._execute_upstream(
                    context, selected, attempt_num, transcoder=transcoder
                )
                self._log_transcode_warnings(context, selected=selected)
                return result
            except _RetryableUpstreamError as err:
                last_error = err
                # Track the last useful upstream response
                if err.upstream_response is not None:
                    last_upstream_response = err.upstream_response
                logger.warning(
                    "Retryable upstream error on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                # Finalize the failed attempt before retrying
                result = await self._attempt_finalizer.finalize_failed_attempt(
                    attempt_id=selected.attempt_id,
                    reservation_id=selected.reservation_id,
                    data=AttemptFinalizationData(
                        status_code=err.status_code,
                        error_class=err.error_class,
                        release_reason="attempt_retryable",
                        retry_category=(
                            err.retry_category.value
                            if err.retry_category is not None
                            else None
                        ),
                        bytes_received=len(context.original_body),
                        latency_ms=self._elapsed_ms(context),
                        is_retry_outcome=True,
                    ),
                )
                # Clean up in-memory state when the attempt transitioned
                if result.attempt_transitioned:
                    if (
                        self._quota_estimator is not None
                        and result.reservation_released
                    ):
                        await self._quota_estimator.remove_reservation(
                            selected.account_name,
                            selected.estimated_microdollars,
                        )
                    if result.reservation_released:
                        await self._router.decrement_active_request_count(
                            selected.account_name
                        )
                # Apply health transitions only when the attempt transitioned
                if result.attempt_transitioned:
                    await self._apply_health_transition(
                        selected.account_name, err, context.model_id
                    )
                    health_applied = True
                # If no other accounts are eligible, don't retry — pass
                # the error directly to the client.
                remaining = self._router.get_eligible_account_names(
                    context.model_id,
                    exclude_accounts=context.attempted_accounts
                    if context.attempted_accounts
                    else None,
                    provider_id=context.provider_id,
                    protocol=context.upstream_protocol,
                    transcode_eligibility=(
                        {context.protocol, context.upstream_protocol}
                        if context.transcode_required
                        else None
                    ),
                )
                if not remaining:
                    break
                if attempt_num >= self._max_retry_attempts:
                    break
                continue
            except _NonRetryableUpstreamError as err:
                last_error = err
                # Track the upstream response for pass-through
                if err.upstream_response is not None:
                    last_upstream_response = err.upstream_response
                logger.warning(
                    "Non-retryable upstream error on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                # Apply health transition for non-retryable errors that
                # indicate account-level problems (e.g., 401/403 auth
                # failures, 429 rate limits, 402 quota exhausted) so the
                # circuit breaker can open. Mark ``health_applied`` so
                # ``_handle_exhausted`` does not double-apply the same
                # failure through the finalizer.
                if self._health_manager is not None:
                    category = classify_failure_category(None, err.status_code)
                    if category == FailureCategory.AUTHENTICATION_FAILED:
                        self._health_manager.record_failure(
                            selected.account_name,
                            model_id=context.model_id,
                            reason="authentication_failed",
                        )
                        health_applied = True
                    elif category == FailureCategory.QUOTA_EXHAUSTED:
                        self._health_manager.record_quota_exhausted(
                            selected.account_name,
                            self._quota_exhausted_cooldown_seconds,
                        )
                        health_applied = True
                    elif category == FailureCategory.RATE_LIMITED:
                        # Non-retryable 429s are propagated to the
                        # client but still indicate upstream pressure.
                        # ``_NonRetryableUpstreamError`` does not carry
                        # ``retry_after`` so default to a 60 s cooldown.
                        self._health_manager.record_rate_limit(
                            selected.account_name, 60.0
                        )
                        health_applied = True
                break

        # All retries exhausted or non-retryable error
        actual_attempts = (
            last_selected.attempt_number if last_selected is not None else 0
        )
        result = await self._handle_exhausted(
            context,
            last_error,
            last_upstream_response,
            actual_attempts,
            last_selected,
            health_applied=health_applied,
        )
        self._log_transcode_warnings(context, selected=last_selected)
        return result

    async def _select_and_persist_attempt(
        self,
        context: ProxyRequestContext,
        attempt_number: int,
    ) -> SelectedAttempt:
        """Atomically select an account, create request/reservation/attempt.

        Ordering invariants:

        1. ``_select_lock`` is held across the durable selection
           transaction AND the runtime publication step.
        2. The inner ``db.transaction()`` context manager EXITS before
           publication runs so SQLite has committed the request /
           reservation / attempt / routing_decision rows. Nested
           ``async with`` blocks guarantee this; the prior
           ``async with self._select_lock, self._db.transaction()``
           shape only released the lock AFTER the transaction, which
           meant the transaction was still open while publication ran.
        3. ``_execute_upstream`` and all upstream I/O happen OUTSIDE
           ``_select_lock``.

        Compensation: if publication fails after the durable commit,
        active count is decremented, the reservation is removed, the
        attempt is finalized as ``PostCommitInterrupted``, and the
        health slot is released. The outer ``except BaseException``
        catches ``CancelledError`` / ``SystemExit`` /
        ``KeyboardInterrupt`` and re-raises them after compensation so
        they cannot be swallowed.
        """
        if (
            self._request_repo is None
            or self._reservation_repo is None
            or self._attempt_repo is None
        ):
            raise DatabaseError("Cannot persist: database repositories unavailable")

        async with self._select_lock:
            # Inner transaction MUST close before publication runs so
            # SQLite has committed the durable rows.  See the docstring
            # above for the ordering invariant.
            async with self._db.transaction():
                # 0. Classify thinking requirement from request body
                from eggpool.catalog.capabilities import classify_thinking_request

                body_dict: dict[str, object] = {}
                if context.original_body:
                    try:
                        parsed: object = json.loads(context.original_body)
                        if isinstance(parsed, dict):
                            body_dict = parsed  # type: ignore[assignment]
                    except (json.JSONDecodeError, ValueError):
                        pass
                thinking_req = classify_thinking_request(body_dict, context.protocol)
                # Record thinking observability trace
                if thinking_req.required:
                    _thinking_counter = get_counter()
                    await _thinking_counter.increment_requested(
                        client_protocol=thinking_req.client_protocol,
                    )
                    context.thinking_trace = {
                        "requested": True,
                        "client_protocol": thinking_req.client_protocol,
                        "request_fields": list(thinking_req.fields),
                        "requested_effort": thinking_req.requested_effort,
                        "resolved_budget_tokens": None,
                        "budget_clamped": False,
                        "capability_status": None,
                        "capability_source": None,
                        "upstream_protocol": None,
                        "upstream_fields": [],
                        "decision": "none",
                    }
                _capability_policy: dict[str, str] | None = None
                if self._transcoder_policy is not None and hasattr(
                    self._transcoder_policy, "capability_policy"
                ):
                    cp = self._transcoder_policy.capability_policy
                    _capability_policy = {
                        "unsupported_thinking": cp.unsupported_thinking,
                        "unknown_thinking": cp.unknown_thinking,
                        "mixed_collapsed_thinking": cp.mixed_collapsed_thinking,
                    }

                # 1. Get eligible account names excluding attempted ones
                eligible_account_names = self._router.get_eligible_account_names(
                    context.model_id,
                    exclude_accounts=context.attempted_accounts
                    if context.attempted_accounts
                    else None,
                    provider_id=context.provider_id,
                    protocol=context.upstream_protocol,
                    transcode_eligibility=(
                        {context.protocol, context.upstream_protocol}
                        if context.transcode_required
                        else None
                    ),
                    thinking_requirement=thinking_req
                    if thinking_req.required
                    else None,
                    capability_policy=_capability_policy,
                )

                if not eligible_account_names:
                    # Phase 5: distinguish pre-dispatch unavailability
                    # from post-retry exhaustion. ``get_eligible_account_names``
                    # already excludes ``context.attempted_accounts``; an
                    # empty result on the first attempt means no enabled
                    # accounts at all (503). An empty result after at
                    # least one attempt means every eligible candidate has
                    # been tried in this request (502).
                    if thinking_req.required:
                        # Record thinking rejection
                        _thinking_counter = get_counter()
                        await _thinking_counter.increment_rejected(
                            client_protocol=thinking_req.client_protocol,
                            capability_status="no_eligible_providers",
                        )
                        if context.thinking_trace is not None:
                            context.thinking_trace["decision"] = "rejected"
                            context.thinking_trace["capability_status"] = (
                                "no_eligible_providers"
                            )
                        raise CapabilityError(
                            model_id=context.model_id,
                            capability="thinking",
                            requested_fields=thinking_req.fields,
                            message=(
                                f"Model {context.model_id!r} is available, "
                                f"but no eligible provider is known to "
                                f"support requested thinking controls."
                            ),
                        )
                    if context.attempted_accounts:
                        raise UpstreamExhaustedError(
                            f"All eligible accounts attempted for model "
                            f"{context.model_id!r}"
                        )
                    raise ModelUnavailableError(
                        f"No accounts available for model {context.model_id!r}"
                    )

                # 2. Calculate projected request tokens once
                estimated_tokens = estimate_reservation_tokens(context.original_body)

                # 3. Build per-account estimate map for scoring
                request_estimates: dict[str, int] = {}
                if self._quota_estimator is not None:
                    for acct_name in eligible_account_names:
                        request_estimates[acct_name] = (
                            self._quota_estimator.estimate_cost(
                                acct_name, context.model_id, estimated_tokens
                            )
                        )

                # 4. Rank accounts once using projected estimates, then
                #    acquire the circuit-breaker probe slot atomically.
                exclude: set[str] = (
                    set(context.attempted_accounts)
                    if context.attempted_accounts
                    else set()
                )
                selected_state = None
                selected_score: float | None = None
                selected_tier: int | None = None
                exclusions: list[RoutingExclusion] = []
                ranked_candidates = await self._router.select_accounts_for_failover(
                    context.model_id,
                    max_accounts=len(eligible_account_names),
                    request_estimates=request_estimates,
                    exclude_accounts=exclude if exclude else None,
                    provider_id=context.provider_id,
                    protocol=context.upstream_protocol,
                    transcode_eligibility=(
                        {context.protocol, context.upstream_protocol}
                        if context.transcode_required
                        else None
                    ),
                    client_protocol=context.protocol,
                    thinking_requirement=thinking_req
                    if thinking_req.required
                    else None,
                    capability_policy=_capability_policy,
                )
                for candidate_state, score in ranked_candidates:
                    # Acquire the circuit-breaker probe slot. If the
                    # breaker rejects this account (half-open slot
                    # consumed or still open), try the next ranked
                    # account without rebuilding and rescoring the
                    # whole candidate list.
                    if (
                        self._health_manager is not None
                        and not self._health_manager.try_acquire_request(
                            candidate_state.name, context.model_id
                        )
                    ):
                        exclusions.append(
                            RoutingExclusion(
                                account_name=candidate_state.name,
                                reason="circuit_breaker",
                            )
                        )
                        continue
                    selected_state = candidate_state
                    selected_score = float(score.final_score)
                    selected_tier = score.tier
                    break

                if selected_state is None and not ranked_candidates:
                    selected_state = await self._router.select_account(
                        model_id=context.model_id,
                        request_estimates=request_estimates,
                        exclude_accounts=exclude if exclude else None,
                        provider_id=context.provider_id,
                        protocol=context.upstream_protocol,
                        transcode_eligibility=(
                            {context.protocol, context.upstream_protocol}
                            if context.transcode_required
                            else None
                        ),
                        client_protocol=context.protocol,
                        thinking_requirement=thinking_req
                        if thinking_req.required
                        else None,
                        capability_policy=_capability_policy,
                    )
                    if (
                        selected_state is not None
                        and self._health_manager is not None
                        and not self._health_manager.try_acquire_request(
                            selected_state.name, context.model_id
                        )
                    ):
                        exclusions.append(
                            RoutingExclusion(
                                account_name=selected_state.name,
                                reason="circuit_breaker",
                            )
                        )
                        selected_state = None

                if selected_state is None:
                    # Distinguish "all enabled accounts already attempted in
                    # this request" (502 UpstreamExhaustedError) from "no
                    # enabled accounts at all" (503 ModelUnavailableError).
                    # The retry loop only reaches this point after at
                    # least one attempt has been recorded in
                    # ``context.attempted_accounts``; an empty candidate
                    # list while the registry still has enabled states
                    # means the eligible subset was exhausted mid-request.
                    if thinking_req.required:
                        # Record thinking rejection
                        _thinking_counter = get_counter()
                        await _thinking_counter.increment_rejected(
                            client_protocol=thinking_req.client_protocol,
                            capability_status="no_eligible_providers",
                        )
                        if context.thinking_trace is not None:
                            context.thinking_trace["decision"] = "rejected"
                            context.thinking_trace["capability_status"] = (
                                "no_eligible_providers"
                            )
                        raise CapabilityError(
                            model_id=context.model_id,
                            capability="thinking",
                            requested_fields=thinking_req.fields,
                            message=(
                                f"Model {context.model_id!r} is available, "
                                f"but no eligible provider is known to "
                                f"support requested thinking controls."
                            ),
                        )
                    if self._all_accounts_attempted(context):
                        raise UpstreamExhaustedError(
                            f"All eligible accounts attempted for model "
                            f"{context.model_id!r}"
                        )
                    raise ModelUnavailableError(
                        f"No accounts available for model {context.model_id!r}"
                    )

                account_name = selected_state.name
                try:
                    api_key = self._registry.get_api_key(account_name)
                    if api_key is None or not self._registry.has_usable_credentials(
                        account_name
                    ):
                        raise AuthenticationError(
                            f"API key not available for account {account_name!r}"
                        )

                    # 5. Resolve the immutable account ID once per process.
                    account_id = self._account_id_cache.get(account_name)
                    if account_id is None:
                        account_repo = AccountRepository(self._db)
                        account_id = await account_repo.get_id_by_name(account_name)
                        if account_id is not None:
                            self._account_id_cache[account_name] = account_id
                    if account_id is None:
                        raise DatabaseError(
                            f"Account {account_name!r} not found in database"
                        )

                    # 6. Resolve the selected account's provider
                    resolved_provider_id = (
                        self._catalog.cache.get_provider_for_account(account_name)
                        or self._registry.get_provider_for_account(account_name)
                        or context.provider_id
                        or DEFAULT_PROVIDER_ID
                    )

                    # 7. Use the exact estimate for the selected account.
                    estimated_microdollars = request_estimates.get(account_name, 0)
                    if (
                        estimated_microdollars == 0
                        and self._quota_estimator is not None
                    ):
                        estimated_microdollars = self._quota_estimator.estimate_cost(
                            account_name, context.model_id, estimated_tokens
                        )

                    # 8. Create pending request if first attempt. Store the
                    # reservation estimate in the INSERT so the common path
                    # does not immediately UPDATE the same row.
                    created_request = "db_request_id" not in context.client_metadata
                    if created_request:
                        db_request_id = await self._request_repo.create_pending(
                            request_id=context.request_id,
                            model_id=context.model_id,
                            protocol=context.protocol,
                            streamed=context.streaming,
                            account_id=account_id,
                            reserved_microdollars=estimated_microdollars,
                            started_at=context.started_at,
                            provider_id=resolved_provider_id,
                            client_ip=context.client_ip,
                        )
                        context.client_metadata["db_request_id"] = db_request_id
                    db_request_id = context.client_metadata["db_request_id"]

                    # 9. Create reservation
                    reservation_id = await self._reservation_repo.create(
                        request_id=db_request_id,
                        account_id=account_id,
                        model_id=context.model_id,
                        estimated_tokens=estimated_tokens,
                        estimated_microdollars=estimated_microdollars,
                    )

                    # 10. Create attempt row
                    attempt_id = await self._attempt_repo.create(
                        request_id=db_request_id,
                        attempt_number=attempt_number,
                        account_id=account_id,
                        provider_id=resolved_provider_id,
                        model_id=context.model_id,
                        protocol=context.protocol,
                        streamed=context.streaming,
                    )

                    # 10a. Persist the routing-decision trace alongside the
                    # attempt so the dashboard can answer "why this account?"
                    # without rescoring from quota tables.
                    top_score_value: float | None = None
                    top_score_account_name: str | None = None
                    if ranked_candidates:
                        top_state, top_score_obj = ranked_candidates[0]
                        top_score_value = float(top_score_obj.final_score)
                        top_score_account_name = top_state.name
                    score_components = self._build_score_components(
                        ranked_candidates=ranked_candidates,
                        selected_account_name=account_name,
                        selected_state=selected_state,
                        selected_score=selected_score,
                        selected_tier=selected_tier,
                        fairness_decision=self._router.last_fairness_decision,
                        fairness_band_names=self._router.last_fairness_band_names,
                    )
                    trace = RoutingDecisionTrace(
                        model_id=context.model_id,
                        provider_id=resolved_provider_id,
                        protocol=context.protocol,
                        selected_account_name=account_name,
                        selected_account_id=account_id,
                        selected_tier=selected_tier,
                        selected_score=selected_score,
                        eligible_count=len(eligible_account_names),
                        scored_count=len(ranked_candidates),
                        attempted_excluded_count=len(exclude),
                        top_score=top_score_value,
                        top_score_account_name=top_score_account_name,
                        exclusions=tuple(exclusions),
                        score_components=score_components,
                    )
                    await self._routing_decision_repo.create(
                        request_id=int(db_request_id),
                        attempt_number=attempt_number,
                        model_id=trace.model_id,
                        provider_id=trace.provider_id,
                        protocol=trace.protocol,
                        selected_account_id=trace.selected_account_id,
                        selected_account_name=trace.selected_account_name,
                        selected_tier=trace.selected_tier,
                        selected_score=trace.selected_score,
                        eligible_count=trace.eligible_count,
                        scored_count=trace.scored_count,
                        attempted_excluded_count=trace.attempted_excluded_count,
                        top_score=trace.top_score,
                        top_score_account_name=trace.top_score_account_name,
                        exclude_reasons_json=trace.to_exclude_reasons_json(),
                        score_components_json=trace.to_score_components_json(),
                    )

                    # Retries select a new account and reservation estimate.
                    if not created_request:
                        await self._request_repo.update_after_selection(
                            request_id=db_request_id,
                            account_id=account_id,
                            reserved_microdollars=estimated_microdollars,
                        )
                except BaseException:
                    if self._health_manager is not None:
                        self._health_manager.release_request(account_name)
                    raise

                # Record the account under the same select lock so a
                # concurrent caller observing the same context cannot
                # race on attempted_accounts before this attempt is
                # fully persisted and committed.
                context.attempted_accounts.add(account_name)
                context.client_metadata["account_name"] = account_name

            # Durable transaction has committed; rows for the request,
            # reservation, attempt, and routing_decision are visible.
            # ``_select_lock`` is still held — publication runs BEFORE
            # the lock releases so a concurrent selector entering the
            # lock after this returns observes this attempt's runtime
            # state. The publish is fast (in-process counter + cache
            # mutation) so the lock-hold stays tight while still
            # closing the race where the prior revision released the
            # lock first and a selector could therefore score on stale
            # zero-penalty counters.
            active_count_increased = False
            try:
                # 11. Increment runtime active count
                await self._router.increment_active_request_count(account_name)
                active_count_increased = True

                # 12. Add exact reserved amount to in-memory cache
                if self._quota_estimator is not None:
                    await self._quota_estimator.add_reservation(
                        account_name, estimated_microdollars
                    )
            except BaseException:
                # Compensate: undo the active count increment so
                # runtime state stays consistent with the durable row.
                if active_count_increased:
                    await self._router.decrement_active_request_count(account_name)
                # Finalize the just-created attempt as cancelled so
                # normal finalization has no stale durable IDs.
                await asyncio.shield(
                    self._attempt_finalizer.finalize_failed_attempt(
                        attempt_id=attempt_id,
                        reservation_id=reservation_id,
                        data=AttemptFinalizationData(
                            status_code=None,
                            error_class="PostCommitInterrupted",
                            release_reason="post_commit_interrupted",
                            retry_category=RetryCategory.NEVER.value,
                            bytes_received=len(context.original_body),
                            latency_ms=self._elapsed_ms(context),
                            is_retry_outcome=False,
                        ),
                    )
                )
                if self._health_manager is not None:
                    self._health_manager.release_request(account_name)
                context.client_metadata["post_commit_interrupted"] = True
                raise

        return SelectedAttempt(
            proxy_request_id=context.request_id,
            db_request_id=db_request_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            account_id=account_id,
            account_name=account_name,
            api_key=api_key,
            model_id=context.model_id,
            estimated_tokens=estimated_tokens,
            estimated_microdollars=estimated_microdollars,
            attempt_number=attempt_number,
            provider_id=resolved_provider_id,
            requires_transcode=context.transcode_required,
            protocol=context.protocol,
            streamed=context.streaming,
        )

    async def _execute_upstream(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        attempt_num: int,
        *,
        transcoder: BodyTranscoder | None = None,
    ) -> PreparedProxyResponse:
        """Execute the upstream HTTP call using the selected attempt."""
        try:
            if context.streaming:
                return await self._execute_streaming(
                    context, selected, attempt_num, transcoder=transcoder
                )
            else:
                return await self._execute_non_streaming(
                    context, selected, attempt_num, transcoder=transcoder
                )
        except asyncio.CancelledError:
            # Client cancellation after selection - finalize the attempt
            elapsed_ms = self._elapsed_ms(context)
            context.client_metadata["_cancelled_finalized"] = True
            await self._finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.CLIENT_CANCELLED,
                    error_class="CancelledError",
                    upstream_latency_ms=elapsed_ms,
                    bytes_received=len(context.original_body),
                    upstream_protocol=context.upstream_protocol,
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace
                    ),
                ),
            )
            raise

    async def _execute_non_streaming(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        attempt_num: int,
        *,
        transcoder: BodyTranscoder | None = None,
    ) -> PreparedProxyResponse:
        """Execute a non-streaming request."""
        headers = self._build_upstream_headers(context, selected)
        upstream_url = self._get_upstream_url(
            context.upstream_protocol, selected.provider_id
        )

        response: httpx.Response | None = None
        try:
            client = self._get_client(selected.provider_id, selected.account_name)
            upstream_request = client.build_request(
                "POST",
                upstream_url,
                headers=headers,
                content=context.body_for_upstream,
            )
            # Phase 4 (latency): record how long the connect+send round
            # took.  ``client.send`` returns once the response headers
            # are available, so this window includes DNS, TCP, TLS,
            # and the upstream handler accept — everything before the
            # upstream has produced any output.
            response = await self._send_upstream_request(
                client, upstream_request, context
            )
            # Headers available immediately after send(); capture
            # first-byte time before reading the body.
            first_byte_ms = self._elapsed_ms(context)
            await response.aread()
        except httpx.ConnectError as err:
            if response is not None:
                await response.aclose()
            raise _RetryableUpstreamError(
                f"Connection failed: {err}",
                status_code=None,
                error_class="ConnectError",
            ) from err
        except httpx.TimeoutException as err:
            if response is not None:
                await response.aclose()
            raise _RetryableUpstreamError(
                f"Timeout: {err}",
                status_code=504,
                error_class="TimeoutException",
            ) from err
        except AggregatorError:
            if response is not None:
                await response.aclose()
            raise
        except Exception as err:
            if response is not None:
                await response.aclose()
            raise _RetryableUpstreamError(
                f"Upstream error: {err}",
                status_code=None,
                error_class=type(err).__name__,
            ) from err

        # Check for upstream errors before consuming body
        try:
            if response.status_code >= 400:
                resp_headers = filter_response_headers(response.headers)
                resp_body = response.content

                # Check if this is retryable
                error = self._classify_upstream_error(
                    response.status_code, resp_headers, body=resp_body
                )
                if error is not None:
                    # Retryable error - raise for retry
                    raise _RetryableUpstreamError(
                        str(error),
                        status_code=response.status_code,
                        error_class=type(error).__name__,
                        retry_after=getattr(error, "retry_after", None),
                        upstream_response=(
                            response.status_code,
                            resp_headers,
                            resp_body,
                        ),
                        retry_category=self._classifier.classify(
                            response.status_code,
                            {k.lower(): v for k, v in resp_headers},
                            body=resp_body,
                        ).category,
                    ) from error

                # Non-retryable client error (400, 404) - finalize and pass through
                await self._finalize_non_retryable(
                    context, selected, response.status_code, resp_headers, resp_body
                )
                # Phase 2: re-render upstream error in client protocol
                if transcoder is not None and context.transcode_context is not None:
                    try:
                        err_payload = json.loads(resp_body)
                    except (json.JSONDecodeError, ValueError):
                        err_payload = None
                    if isinstance(err_payload, dict) or err_payload is None:
                        _status, err_body, err_warnings = transcoder.reencode_error(
                            response.status_code,
                            cast("dict[str, Any] | None", err_payload),
                            context.transcode_context,
                        )
                        resp_body = encode_json_body(err_body)
                        context.transcode_context.loss_warnings.extend(err_warnings)
                elapsed_ms = self._elapsed_ms(context)
                resp_headers.append(("x-proxy-request-id", context.request_id))
                resp_headers.append(("x-proxy-attempt-count", str(attempt_num)))
                return PreparedProxyResponse(
                    status_code=response.status_code,
                    headers=resp_headers,
                    body=resp_body,
                    request_id=context.request_id,
                    account_name=selected.account_name,
                    latency_ms=elapsed_ms,
                    attempt_count=attempt_num,
                )

            # Success path
            body = response.content
            resp_headers = filter_response_headers(response.headers)
            elapsed_ms = self._elapsed_ms(context)

            usage = self._extract_non_stream_usage(
                context.upstream_protocol, body, provider_id=selected.provider_id
            )
            upstream_req_id = self._get_header_value(
                resp_headers, _UPSTREAM_REQUEST_ID_HEADERS
            )
            upstream_connect_ms = context.upstream_connect_ms
            upstream_read_ms = self._upstream_read_ms(context, elapsed_ms)
            coordinator_overhead_ms = self._coordinator_overhead_ms(
                total_ms=elapsed_ms,
                connect_ms=upstream_connect_ms,
                read_ms=upstream_read_ms,
            )
            # Finalize via RequestFinalizer
            await self._finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=response.status_code,
                    input_tokens=usage.input_tokens if usage else 0,
                    output_tokens=usage.output_tokens if usage else 0,
                    cache_read_tokens=usage.cache_read_tokens if usage else 0,
                    cache_write_tokens=usage.cache_creation_tokens if usage else 0,
                    reasoning_tokens=usage.reasoning_tokens if usage else 0,
                    thinking_characters=usage.thinking_characters if usage else 0,
                    first_byte_ms=first_byte_ms,
                    upstream_latency_ms=elapsed_ms,
                    bytes_emitted=len(body),
                    upstream_request_id=upstream_req_id,
                    upstream_connect_ms=upstream_connect_ms,
                    upstream_read_ms=upstream_read_ms,
                    coordinator_overhead_ms=coordinator_overhead_ms,
                    bytes_received=len(context.original_body),
                    provider_cost_microdollars=(
                        usage.reported_cost_microdollars if usage else None
                    ),
                    provider_cost_source=(
                        usage.reported_cost_source if usage else None
                    ),
                    upstream_protocol=context.upstream_protocol,
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace
                    ),
                ),
            )

            # Clear persisted backoff rows on a successful request so
            # restart-time hydration starts from a clean slate for
            # this account. Only transient reasons are cleared;
            # terminal ones (authentication_failed, model_unavailable)
            # are preserved.
            await self._clear_backoff(
                selected.account_name,
                model_id=None,
                reasons=list(_TRANSIENT_BACKOFF_REASONS),
            )

            # Phase 2: decode upstream success response to client protocol
            if transcoder is not None and context.transcode_context is not None:
                try:
                    upstream_payload = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    upstream_payload = None
                if isinstance(upstream_payload, dict):
                    _features = (
                        self._transcoder_policy.features
                        if self._transcoder_policy is not None
                        else None
                    )
                    translated, decode_warnings = transcoder.decode_response(
                        cast("dict[str, Any]", upstream_payload),
                        context.transcode_context,
                        features=_features,
                        reasoning_field_names=(
                            self._transcoder_policy.openai_reasoning_fields.non_stream
                            if self._transcoder_policy is not None
                            else None
                        ),
                        emit_compat_aliases=(
                            self._transcoder_policy.openai_reasoning_fields.emit_compat_aliases
                            if self._transcoder_policy is not None
                            else False
                        ),
                    )
                    body = encode_json_body(translated)
                    context.transcode_context.loss_warnings.extend(decode_warnings)

            resp_headers.append(("x-proxy-request-id", context.request_id))
            resp_headers.append(("x-proxy-attempt-count", str(attempt_num)))
            return PreparedProxyResponse(
                status_code=response.status_code,
                headers=resp_headers,
                body=body,
                request_id=context.request_id,
                account_name=selected.account_name,
                usage=usage,
                latency_ms=elapsed_ms,
                attempt_count=attempt_num,
            )
        finally:
            if response is not None:  # type: ignore[unnecessary-comparison]
                try:
                    await response.aclose()
                except Exception:
                    logger.debug("Error closing upstream response", exc_info=True)

    async def _execute_streaming(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        attempt_num: int,
        *,
        transcoder: BodyTranscoder | None = None,
    ) -> PreparedProxyResponse:
        """Execute a streaming request."""
        headers = self._build_upstream_headers(context, selected)
        upstream_url = self._get_upstream_url(
            context.upstream_protocol, selected.provider_id
        )

        # Inject stream_options.include_usage for OpenAI
        body_to_send = context.body_for_upstream
        if context.upstream_protocol == "openai":
            payload_obj: object
            try:
                payload_obj = json.loads(body_to_send)
            except (json.JSONDecodeError, ValueError):
                pass
            else:
                if isinstance(payload_obj, dict):
                    payload = cast("dict[str, Any]", payload_obj)
                    stream_opts_value: Any = payload.get("stream_options")
                    if isinstance(stream_opts_value, dict):
                        if "include_usage" not in stream_opts_value:
                            stream_opts_value["include_usage"] = True
                            body_to_send = encode_json_body(payload)
                    elif stream_opts_value is None:
                        payload["stream_options"] = {"include_usage": True}
                        body_to_send = encode_json_body(payload)
                    else:
                        # Non-dict stream_options (list, str, bool, etc.) —
                        # leave the body unchanged and let upstream reject it.
                        pass

        client = self._get_client(selected.provider_id, selected.account_name)
        request = client.build_request(
            "POST",
            upstream_url,
            headers=headers,
            content=body_to_send,
        )

        response = None
        generator_created = False
        try:
            try:
                response = await self._send_upstream_request(client, request, context)
            except httpx.ConnectError as err:
                raise _RetryableUpstreamError(
                    f"Connection failed: {err}",
                    status_code=None,
                    error_class="ConnectError",
                ) from err
            except httpx.TimeoutException as err:
                raise _RetryableUpstreamError(
                    f"Timeout: {err}",
                    status_code=504,
                    error_class="TimeoutException",
                ) from err
            except Exception as err:
                raise _RetryableUpstreamError(
                    f"Upstream error: {err}",
                    status_code=None,
                    error_class=type(err).__name__,
                ) from err

            if response is None:  # type: ignore[reportUnnecessaryComparison]
                raise DatabaseError("Upstream response is None")

            # Check upstream status before creating downstream response
            if response.status_code >= 400:
                await response.aread()
                resp_headers = filter_response_headers(response.headers)
                resp_body = response.content

                error = self._classify_upstream_error(
                    response.status_code, resp_headers, body=resp_body
                )
                if error is not None:
                    raise _RetryableUpstreamError(
                        str(error),
                        status_code=response.status_code,
                        error_class=type(error).__name__,
                        retry_after=getattr(error, "retry_after", None),
                        upstream_response=(
                            response.status_code,
                            resp_headers,
                            resp_body,
                        ),
                        retry_category=self._classifier.classify(
                            response.status_code,
                            {k.lower(): v for k, v in resp_headers},
                            body=resp_body,
                        ).category,
                    ) from error

                # Non-retryable client error - finalize and raise for pass-through
                await self._finalize_non_retryable(
                    context, selected, response.status_code, resp_headers, resp_body
                )
                raise _NonRetryableUpstreamError(
                    f"Upstream returned {response.status_code}",
                    status_code=response.status_code,
                    upstream_response=(
                        response.status_code,
                        resp_headers,
                        resp_body,
                    ),
                )

            # Build the response headers
            resp_headers = filter_response_headers(response.headers)
            resp_headers.append(("x-proxy-request-id", context.request_id))
            resp_headers.append(("x-proxy-attempt-count", str(attempt_num)))

            # Build streaming generator
            stream_iter = self._build_stream_generator(
                context=context,
                upstream_response=response,
                selected=selected,
                resp_headers=resp_headers,
                request_started_monotonic=context.started_monotonic,
            )
            generator_created = True
        finally:
            # Close the upstream response when we are NOT handing the
            # stream off to the generator.  When ``generator_created``
            # is True, the generator's own ``finally`` block closes
            # the response after the stream is fully consumed (or
            # cancelled) - closing it here would eagerly tear down the
            # stream and break the lazy ``aiter_bytes`` consumer.  The
            # ``response.status_code >= 400`` branch covers upstream
            # error responses (already read into memory above) and
            # ``not generator_created`` covers construction failures
            # so the upstream connection is never leaked in those
            # paths.
            if response is not None and (
                response.status_code >= 400 or not generator_created
            ):
                try:
                    await response.aclose()
                except Exception:
                    logger.debug("Error closing upstream response", exc_info=True)

        if response is None:
            raise DatabaseError("Upstream response is None")

        return PreparedProxyResponse(
            status_code=response.status_code,
            headers=resp_headers,
            stream_iterator=stream_iter,
            request_id=context.request_id,
            account_name=selected.account_name,
            latency_ms=self._elapsed_ms(context),
            attempt_count=attempt_num,
        )

    def _build_stream_generator(
        self,
        context: ProxyRequestContext,
        upstream_response: httpx.Response,
        selected: SelectedAttempt,
        resp_headers: list[tuple[str, str]],
        request_started_monotonic: float | None = None,
    ) -> AsyncIterator[bytes]:
        """Build an async generator that streams upstream bytes downstream,
        extracts usage via IncrementalSSEObserver, and finalizes the request
        on completion."""
        observer = IncrementalSSEObserver(
            context.upstream_protocol, provider_id=selected.provider_id
        )
        bytes_emitted = 0
        first_byte_ms = 0.0
        started = time.monotonic()
        # Use the caller-provided request start time so first_byte_ms
        # and upstream_latency_ms include routing, persistence, and
        # upstream connection/header time.
        reference = (
            request_started_monotonic
            if request_started_monotonic is not None
            else started
        )
        finalizer = self._finalizer
        persist_error_detail = self._persist_error_detail
        account_backoff_repo = self._account_backoff_repo
        clear_backoff = self._clear_backoff

        async def _stream() -> AsyncIterator[bytes]:
            nonlocal bytes_emitted, first_byte_ms
            try:
                # Determine include_usage from the request body's
                # stream_options (already injected by _execute_streaming).
                include_usage = True
                if context.upstream_protocol == "openai":
                    try:
                        payload_obj = json.loads(context.body_for_upstream)
                    except (json.JSONDecodeError, ValueError):
                        pass
                    else:
                        if isinstance(payload_obj, dict):
                            payload = cast("dict[str, Any]", payload_obj)
                            so_raw: Any = payload.get("stream_options")
                            if isinstance(so_raw, dict):
                                so = cast("dict[str, Any]", so_raw)
                                include_usage = bool(so.get("include_usage", True))

                streaming_transcoder = select_streaming_transcoder(
                    client_protocol=context.protocol,
                    upstream_protocol=context.upstream_protocol,
                    include_usage=include_usage,
                    transcode_context=context.transcode_context,
                    features=(
                        self._transcoder_policy.features
                        if self._transcoder_policy is not None
                        else None
                    ),
                    reasoning_field_names=(
                        self._transcoder_policy.openai_reasoning_fields.stream_delta
                        if self._transcoder_policy is not None
                        else None
                    ),
                    emit_compat_aliases=(
                        self._transcoder_policy.openai_reasoning_fields.emit_compat_aliases
                        if self._transcoder_policy is not None
                        else False
                    ),
                )
                async for chunk in upstream_response.aiter_bytes():
                    if first_byte_ms == 0.0:
                        first_byte_ms = (time.monotonic() - reference) * 1000

                    observer.observe(chunk)
                    bytes_emitted = observer.bytes_emitted

                    if streaming_transcoder is not None:
                        for out_chunk in await streaming_transcoder.feed(chunk):
                            yield out_chunk
                    else:
                        yield chunk

                # Stream completed - flush observer and transcoder
                observer.flush()
                if streaming_transcoder is not None:
                    for out_chunk in await streaming_transcoder.flush():
                        yield out_chunk
                usage_result = observer.usage

                upstream_latency_total = int((time.monotonic() - reference) * 1000)
                upstream_connect_ms_value = context.upstream_connect_ms
                upstream_read_ms_value = self._upstream_read_ms(
                    context, upstream_latency_total
                )
                coordinator_overhead_ms_value = self._coordinator_overhead_ms(
                    total_ms=upstream_latency_total,
                    connect_ms=upstream_connect_ms_value,
                    read_ms=upstream_read_ms_value,
                )

                # Finalize via RequestFinalizer
                await finalizer.finalize(
                    selected,
                    FinalizationData(
                        outcome=FinalizationOutcome.COMPLETED,
                        status_code=upstream_response.status_code,
                        input_tokens=usage_result.input_tokens,
                        output_tokens=usage_result.output_tokens,
                        cache_read_tokens=usage_result.cache_read_tokens,
                        cache_write_tokens=usage_result.cache_creation_tokens,
                        reasoning_tokens=usage_result.reasoning_tokens,
                        thinking_characters=usage_result.thinking_characters,
                        first_byte_ms=int(first_byte_ms) if first_byte_ms > 0 else None,
                        upstream_latency_ms=upstream_latency_total,
                        bytes_emitted=bytes_emitted,
                        upstream_request_id=self._get_header_value(
                            resp_headers, _UPSTREAM_REQUEST_ID_HEADERS
                        ),
                        bytes_received=len(context.original_body),
                        upstream_connect_ms=upstream_connect_ms_value,
                        upstream_read_ms=upstream_read_ms_value,
                        coordinator_overhead_ms=coordinator_overhead_ms_value,
                        provider_cost_microdollars=usage_result.reported_cost_microdollars,
                        provider_cost_source=usage_result.reported_cost_source,
                        upstream_protocol=context.upstream_protocol,
                        thinking_trace_json=_serialize_thinking_trace(
                            context.thinking_trace
                        ),
                    ),
                )

                # Clear persisted transient backoff rows on a
                # successful streaming request so restart-time
                # hydration starts clean for this account. Local
                # estimate quota overage is never persisted, so this
                # call only touches real upstream backoffs.
                if account_backoff_repo is not None:
                    await clear_backoff(
                        selected.account_name,
                        model_id=None,
                        reasons=list(_TRANSIENT_BACKOFF_REASONS),
                    )

            except asyncio.CancelledError:
                # Client cancellation - finalize but don't penalize health.
                # Skip if _execute_upstream already finalized (the CancelledError
                # propagates here after the outer handler runs).
                #
                # Shield the finalizer from ASGI task cancellation and
                # cap the wait with a short timeout.  When the client
                # disconnects mid-stream the generator is cancelled;
                # without shielding, the finalizer task is killed
                # while waiting on the SQLite connection lock and the
                # request leaks as ``pending`` with an active
                # reservation.  The 10 s ceiling guarantees we do not
                # block the event loop indefinitely even if the lock
                # is heavily contended; the periodic stale-request
                # finalizer in ``app._finalize_stale_requests`` is the
                # outer safety net for anything that escapes this path.
                observer.flush()
                usage_result = observer.usage
                if not context.client_metadata.get("_cancelled_finalized"):
                    cancel_latency_total = int((time.monotonic() - reference) * 1000)
                    cancel_connect_ms_value = context.upstream_connect_ms
                    cancel_read_ms_value = self._upstream_read_ms(
                        context, cancel_latency_total
                    )
                    cancel_overhead_ms_value = self._coordinator_overhead_ms(
                        total_ms=cancel_latency_total,
                        connect_ms=cancel_connect_ms_value,
                        read_ms=cancel_read_ms_value,
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(
                                finalizer.finalize(
                                    selected,
                                    FinalizationData(
                                        outcome=FinalizationOutcome.CLIENT_CANCELLED,
                                        first_byte_ms=(
                                            int(first_byte_ms)
                                            if first_byte_ms > 0
                                            else None
                                        ),
                                        upstream_latency_ms=cancel_latency_total,
                                        bytes_emitted=bytes_emitted,
                                        input_tokens=usage_result.input_tokens,
                                        output_tokens=usage_result.output_tokens,
                                        cache_read_tokens=usage_result.cache_read_tokens,
                                        cache_write_tokens=(
                                            usage_result.cache_creation_tokens
                                        ),
                                        reasoning_tokens=usage_result.reasoning_tokens,
                                        thinking_characters=(
                                            usage_result.thinking_characters
                                        ),
                                        bytes_received=len(context.original_body),
                                        upstream_connect_ms=cancel_connect_ms_value,
                                        upstream_read_ms=cancel_read_ms_value,
                                        coordinator_overhead_ms=cancel_overhead_ms_value,
                                        provider_cost_microdollars=(
                                            usage_result.reported_cost_microdollars
                                        ),
                                        provider_cost_source=(
                                            usage_result.reported_cost_source
                                        ),
                                        upstream_protocol=context.upstream_protocol,
                                        thinking_trace_json=_serialize_thinking_trace(
                                            context.thinking_trace
                                        ),
                                    ),
                                )
                            ),
                            timeout=10.0,
                        )
                    except TimeoutError:
                        logger.error(
                            "Finalizer timed out for cancelled stream %s; "
                            "request %s may leak as pending",
                            context.request_id,
                            selected.db_request_id,
                        )
                    except Exception:
                        logger.exception(
                            "Finalizer failed for cancelled stream %s",
                            context.request_id,
                        )
                raise
            except Exception as exc:
                # Midstream error - finalize, no retry
                observer.flush()
                usage_result = observer.usage
                error_detail_value = _prepare_error_detail(exc, persist_error_detail)
                mid_latency_total = int((time.monotonic() - reference) * 1000)
                mid_connect_ms_value = context.upstream_connect_ms
                mid_read_ms_value = self._upstream_read_ms(context, mid_latency_total)
                mid_overhead_ms_value = self._coordinator_overhead_ms(
                    total_ms=mid_latency_total,
                    connect_ms=mid_connect_ms_value,
                    read_ms=mid_read_ms_value,
                )
                await finalizer.finalize(
                    selected,
                    FinalizationData(
                        outcome=FinalizationOutcome.MIDSTREAM_ERROR,
                        first_byte_ms=int(first_byte_ms) if first_byte_ms > 0 else None,
                        upstream_latency_ms=mid_latency_total,
                        bytes_emitted=bytes_emitted,
                        input_tokens=usage_result.input_tokens,
                        output_tokens=usage_result.output_tokens,
                        cache_read_tokens=usage_result.cache_read_tokens,
                        cache_write_tokens=usage_result.cache_creation_tokens,
                        reasoning_tokens=usage_result.reasoning_tokens,
                        thinking_characters=usage_result.thinking_characters,
                        error_class=type(exc).__name__,
                        error_detail=error_detail_value,
                        bytes_received=len(context.original_body),
                        upstream_connect_ms=mid_connect_ms_value,
                        upstream_read_ms=mid_read_ms_value,
                        coordinator_overhead_ms=mid_overhead_ms_value,
                        provider_cost_microdollars=(
                            usage_result.reported_cost_microdollars
                        ),
                        provider_cost_source=usage_result.reported_cost_source,
                        upstream_protocol=context.upstream_protocol,
                        thinking_trace_json=_serialize_thinking_trace(
                            context.thinking_trace
                        ),
                    ),
                )
                raise
            finally:
                try:
                    await upstream_response.aclose()
                except Exception:
                    logger.debug("Error closing upstream response", exc_info=True)

        return _stream()

    def _extract_non_stream_usage(
        self,
        protocol: str,
        body: bytes,
        *,
        provider_id: str | None = None,
    ) -> StreamUsageResult | None:
        """Extract usage from a non-streaming response body.

        ``provider_id`` enables provider-specific aliases when parsing
        an authoritative cost field (e.g. OpenCode Go's bare
        ``usage.cost`` field). The parser is defensive and returns
        ``None`` for absent or unparseable cost values; the finalizer
        will fall back to locally derived cost in that case.
        """
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "Non-streaming upstream response body is not valid JSON; "
                "usage will not be extracted (body_len=%d)",
                len(body),
            )
            return None

        if not isinstance(data, dict):
            logger.debug(
                "Non-streaming upstream response is not a JSON object "
                "(type=%s); usage will not be extracted",
                type(data).__name__,
            )
            return None

        data_dict = cast("dict[str, Any]", data)

        if protocol == "anthropic":
            return extract_anthropic_response_usage(
                data_dict,
                provider_id=provider_id,
            )

        return extract_openai_response_usage(
            data_dict,
            provider_id=provider_id,
        )

    @staticmethod
    def _get_header_value(
        headers: list[tuple[str, str]],
        name: str | list[str],
    ) -> str | None:
        """Return the value for a header, or None.

        Accepts a single name or a list of names tried in order
        (case-insensitive).
        """
        names = [name] if isinstance(name, str) else name
        lower_names = [n.lower() for n in names]
        for key, value in headers:
            if key.lower() in lower_names:
                return value
        return None

    @staticmethod
    def _elapsed_ms(context: ProxyRequestContext) -> int:
        """Return request latency from a clock unaffected by wall-clock jumps."""
        return max(0, int((time.monotonic() - context.started_monotonic) * 1000))

    async def _send_upstream_request(
        self,
        client: httpx.AsyncClient,
        request: httpx.Request,
        context: ProxyRequestContext,
    ) -> httpx.Response:
        """Send an upstream request and capture shared dispatch timing."""
        if self._dispatch_overhead_recorder is not None:
            self._dispatch_overhead_recorder.record_ns(
                time.perf_counter_ns() - context.started_monotonic_ns
            )
        connect_start = time.monotonic()
        response = await client.send(request, stream=True)
        context.upstream_connect_ms = int((time.monotonic() - connect_start) * 1000)
        context.upstream_headers_ms = self._elapsed_ms(context)
        return response

    @staticmethod
    def _upstream_read_ms(
        context: ProxyRequestContext,
        observed_elapsed_ms: int,
    ) -> int | None:
        """Return elapsed upstream body/stream read time after response headers."""
        if context.upstream_headers_ms is None:
            return None
        return max(0, observed_elapsed_ms - context.upstream_headers_ms)

    @staticmethod
    def _coordinator_overhead_ms(
        *,
        total_ms: int,
        connect_ms: int | None,
        read_ms: int | None,
    ) -> int | None:
        """Return elapsed time not attributed to upstream connect or read phases."""
        if connect_ms is None or read_ms is None:
            return None
        return max(0, total_ms - connect_ms - read_ms)

    def _classify_upstream_error(
        self,
        status_code: int,
        headers: list[tuple[str, str]],
        body: bytes | None = None,
    ) -> UpstreamError | None:
        """Classify an upstream error status code into an exception.

        Returns None for non-retryable client errors (400, non-model-specific 404)
        where the response body should be passed through as-is.
        """
        headers_dict = {k.lower(): v for k, v in headers}
        error = self._classifier.classify(status_code, headers_dict, body=body)

        if error.category == RetryCategory.AUTH_FAILURE:
            return AuthenticationError(error.message, status_code=status_code)
        if error.category == RetryCategory.QUOTA_EXCEEDED:
            if status_code == 429:
                retry_after = error.retry_after
                return RateLimitError(
                    error.message,
                    status_code=status_code,
                    retry_after=retry_after if retry_after is not None else 60.0,
                )
            return QuotaExhaustedError(error.message, status_code=status_code)
        if error.category == RetryCategory.MODEL_UNAVAILABLE:
            return ModelUnavailableError(error.message, status_code=status_code)
        if error.category == RetryCategory.BAD_REQUEST:
            return None
        if error.category in (RetryCategory.TEMPORARY, RetryCategory.TRANSIENT):
            if error.category == RetryCategory.TEMPORARY:
                return TemporaryUpstreamError(error.message, status_code=status_code)
            return TransientUpstreamError(error.message, status_code=status_code)

        if error.category in (RetryCategory.FATAL, RetryCategory.NEVER):
            return UpstreamError(error.message, status_code=status_code)

        return None

    def _get_upstream_url(self, protocol: str, provider_id: str | None = None) -> str:
        """Get the absolute upstream URL for a protocol and provider.

        When a provider configuration is available, uses
        ``compose_provider_url()`` to combine ``base_url`` with the
        configured protocol-specific path so all outbound dispatch
        paths share the same URL composition rules as catalog fetch.
        Falls back to bare paths when no provider config is loaded.
        """
        if provider_id and self._config is not None:
            provider_cfg = self._config.providers.get(provider_id)
            if provider_cfg is not None:
                path = (
                    provider_cfg.anthropic_path
                    if protocol == "anthropic"
                    else provider_cfg.openai_path
                )
                return compose_provider_url(provider_cfg, path)
        if protocol == "anthropic":
            return "/messages"
        return "/chat/completions"

    def _build_upstream_headers(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
    ) -> dict[str, str]:
        """Build upstream headers using provider contract when available."""
        from eggpool.proxy.client import sanitize_request_headers

        sanitized = sanitize_request_headers(dict(context.incoming_headers))
        provider_cfg = (
            self._config.providers.get(selected.provider_id)
            if self._config is not None
            else None
        )
        if provider_cfg is not None:
            auth_headers = build_upstream_headers(
                provider_cfg,
                selected.api_key,
                protocol=context.upstream_protocol,
            )
            sanitized.update(auth_headers)
            if logger.isEnabledFor(logging.DEBUG):
                auth_shape = build_auth_headers(provider_cfg, selected.api_key)
                static_names = list(build_static_headers(provider_cfg).keys())
                logger.debug(
                    "provider=%s account=%s auth=%s static_headers=%s",
                    selected.provider_id,
                    selected.account_name,
                    _redact_auth_shape(auth_shape),
                    static_names or None,
                )
        else:
            # Fallback: legacy Bearer auth
            from eggpool.proxy.client import build_upstream_auth_headers

            sanitized.update(
                build_upstream_auth_headers(
                    protocol="", upstream_api_key=selected.api_key
                )
            )
        return sanitized

    async def _apply_health_transition(
        self,
        account_name: str,
        err: _RetryableUpstreamError,
        model_id: str,
    ) -> None:
        """Apply health transitions for a failed account."""
        if self._health_manager is None:
            return

        category = classify_failure_category(err.error_class, err.status_code)
        rate_limit_retry_after: float | None = None
        backoff_until_epoch: float | None = None
        if category == FailureCategory.AUTHENTICATION_FAILED:
            self._health_manager.record_failure(
                account_name, model_id=model_id, reason="authentication_failed"
            )
            # Terminal; persist a long-ish backoff so restarts honor it.
            backoff_until_epoch = time.time() + 365 * 86400
        elif category == FailureCategory.RATE_LIMITED:
            rate_limit_retry_after = (
                60.0 if err.retry_after is None else err.retry_after
            )
            self._health_manager.record_rate_limit(account_name, rate_limit_retry_after)
            self._health_manager.release_request(account_name)
            backoff_until_epoch = time.time() + rate_limit_retry_after
        elif category == FailureCategory.QUOTA_EXHAUSTED:
            self._health_manager.record_quota_exhausted(
                account_name,
                self._quota_exhausted_cooldown_seconds,
            )
            self._health_manager.release_request(account_name)
            backoff_until_epoch = time.time() + self._quota_exhausted_cooldown_seconds
        elif category == FailureCategory.MODEL_UNAVAILABLE:
            self._health_manager.disable_model(account_name, model_id)
            self._health_manager.release_request(account_name)
            self._catalog.cache.mark_model_unavailable(account_name, model_id)
            # model_unavailable rows with NULL backoff_until are
            # terminal in the hydration path.
            backoff_until_epoch = None
        else:
            self._health_manager.record_failure(
                account_name, model_id=model_id, reason=category.value
            )
            # Transient reasons get a short exponential cooldown so a
            # restart does not silently clear them.
            from eggpool.health.backoff import compute_backoff_seconds

            delay = compute_backoff_seconds(
                category.value,
                consecutive_failures=self._health_manager.get_account_health(
                    account_name
                ).consecutive_failures,
                jitter=False,
            )
            if delay is not None and delay > 0:
                backoff_until_epoch = time.time() + delay

        # Also update runtime state with normalized category
        state = self._registry.get_state(account_name)
        if state is not None:
            state.record_failure(
                category.value,
                cooldown_seconds=self._quota_exhausted_cooldown_seconds,
                rate_limit_retry_after=rate_limit_retry_after,
            )

        # Persist authoritative backoff to SQLite so the suppression
        # survives restart. ``model_unavailable`` is scoped to the
        # (account, model) pair; everything else is account-wide.
        await self._persist_backoff(
            account_name=account_name,
            model_id=model_id
            if category == FailureCategory.MODEL_UNAVAILABLE
            else None,
            reason=category.value,
            status_code=err.status_code,
            error_class=err.error_class,
            backoff_until=backoff_until_epoch,
            consecutive_failures=self._health_manager.get_account_health(
                account_name
            ).consecutive_failures,
        )

    async def _persist_backoff(
        self,
        *,
        account_name: str,
        model_id: str | None,
        reason: str,
        status_code: int | None,
        error_class: str | None,
        backoff_until: float | None,
        consecutive_failures: int,
    ) -> None:
        """Write the authoritative backoff to ``account_backoff_repo``.

        Silently skips when no repository was injected (e.g. legacy
        tests) or when the reason has no policy (e.g. client 4xx).
        """
        if self._account_backoff_repo is None:
            return
        from eggpool.health.backoff import is_backoff_reason

        if not is_backoff_reason(reason):
            return
        account_id = self._account_id_cache.get(account_name)
        if account_id is None:
            try:
                account_repo = AccountRepository(self._db)
                account_id = await account_repo.get_id_by_name(account_name)
            except Exception:
                logger.exception(
                    "Failed to resolve account_id for backoff persistence (account=%r)",
                    account_name,
                )
                return
            if account_id is None:
                return
            self._account_id_cache[account_name] = account_id
        try:
            await self._account_backoff_repo.upsert_failure(
                account_id=account_id,
                model_id=model_id,
                reason=reason,
                status_code=status_code,
                error_class=error_class,
                backoff_until=backoff_until,
                consecutive_failures=consecutive_failures,
            )
        except Exception:
            logger.exception(
                "Failed to persist backoff (account=%r reason=%r)",
                account_name,
                reason,
            )

    async def _clear_backoff(
        self,
        account_name: str,
        *,
        model_id: str | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        """Remove persisted backoff rows for a successful request.

        Errors are logged and swallowed so the request lifecycle
        continues; the in-memory health manager is the source of
        truth for the current process and the repository is purely
        durable state.
        """
        if self._account_backoff_repo is None:
            return
        account_id = self._account_id_cache.get(account_name)
        if account_id is None:
            try:
                account_repo = AccountRepository(self._db)
                account_id = await account_repo.get_id_by_name(account_name)
            except Exception:
                logger.exception(
                    "Failed to resolve account_id for backoff cleanup (account=%r)",
                    account_name,
                )
                return
            if account_id is None:
                return
            self._account_id_cache[account_name] = account_id
        try:
            await self._account_backoff_repo.clear_success(
                account_id=account_id,
                model_id=model_id,
                reasons=reasons,
            )
        except Exception:
            logger.exception(
                "Failed to clear backoff rows (account=%r)",
                account_name,
            )

    async def _finalize_non_retryable(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        status_code: int,
        resp_headers: list[tuple[str, str]],
        resp_body: bytes,
    ) -> None:
        """Finalize a non-retryable client error (4xx)."""
        elapsed_ms = self._elapsed_ms(context)
        await self._finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.CLIENT_ERROR,
                status_code=status_code,
                upstream_latency_ms=elapsed_ms,
                bytes_emitted=len(resp_body),
                upstream_request_id=self._get_header_value(
                    resp_headers, _UPSTREAM_REQUEST_ID_HEADERS
                ),
                bytes_received=len(context.original_body),
                upstream_protocol=context.upstream_protocol,
                thinking_trace_json=_serialize_thinking_trace(context.thinking_trace),
            ),
        )

    async def _handle_exhausted(
        self,
        context: ProxyRequestContext,
        last_error: Exception | None,
        last_upstream_response: tuple[int, list[tuple[str, str]], bytes] | None,
        attempt_num: int,
        last_selected: SelectedAttempt | None = None,
        health_applied: bool = False,
    ) -> PreparedProxyResponse:
        """Handle exhausted retries or non-retryable errors.

        Uses last_selected for finalization instead of reconstructing from DB.
        Preserves the last upstream response when available.
        """
        elapsed_ms = self._elapsed_ms(context)

        # Finalize the request if we have a selected attempt
        if last_selected is not None:
            # Determine outcome based on error type
            outcome = FinalizationOutcome.UPSTREAM_ERROR
            status_code = None
            error_class = None
            error_detail: str | None = None
            health_already_applied = False

            if last_upstream_response is not None:
                status_code = last_upstream_response[0]
            if last_error is not None:
                # Prefer the classified error_class carried by the
                # wrapper over the wrapper's own class name so that
                # operational diagnostics report the root cause
                # (e.g. RateLimitError) instead of _RetryableUpstreamError.
                if (
                    isinstance(last_error, _RetryableUpstreamError)
                    and last_error.error_class is not None
                ):
                    error_class = last_error.error_class
                else:
                    error_class = type(last_error).__name__
                error_detail = _prepare_error_detail(
                    last_error, self._persist_error_detail
                )
                if isinstance(last_error, _NonRetryableUpstreamError):
                    outcome = FinalizationOutcome.CLIENT_ERROR
                elif isinstance(last_error, _RetryableUpstreamError):
                    outcome = FinalizationOutcome.UPSTREAM_ERROR
                    health_already_applied = health_applied

            await self._finalizer.finalize(
                last_selected,
                FinalizationData(
                    outcome=outcome,
                    status_code=status_code,
                    error_class=error_class,
                    error_detail=error_detail,
                    upstream_latency_ms=elapsed_ms,
                    health_already_applied=health_already_applied,
                    bytes_received=len(context.original_body),
                    upstream_protocol=context.upstream_protocol,
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace
                    ),
                ),
            )
        elif context.client_metadata.get("db_request_id") is not None:
            # No selected attempt but request exists - synthesize a
            # SelectedAttempt so the existing finalizer path populates
            # every request column and records the audit event. The
            # synthetic attempt_id/reservation_id have no matching
            # rows so the attempt and reservation steps no-op.
            db_request_id = context.client_metadata["db_request_id"]
            account_name = str(context.client_metadata.get("account_name", ""))
            error_class = type(last_error).__name__ if last_error else "exhausted"
            status_code: int | None = None
            if last_upstream_response is not None:
                status_code = last_upstream_response[0]
            error_detail = _prepare_error_detail(last_error, self._persist_error_detail)
            synthetic = SelectedAttempt(
                proxy_request_id=context.request_id,
                db_request_id=db_request_id,
                attempt_id=0,
                reservation_id="0",
                account_id=0,
                account_name=account_name,
                api_key="",
                model_id=context.model_id,
                estimated_tokens=0,
                estimated_microdollars=0,
                attempt_number=0,
                provider_id=context.provider_id or DEFAULT_PROVIDER_ID,
            )
            await self._finalizer.finalize(
                synthetic,
                FinalizationData(
                    outcome=FinalizationOutcome.UPSTREAM_ERROR,
                    status_code=status_code,
                    error_class=error_class,
                    error_detail=error_detail,
                    upstream_latency_ms=elapsed_ms,
                    bytes_received=len(context.original_body),
                    upstream_protocol=context.upstream_protocol,
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace
                    ),
                ),
            )

        # Use last upstream response if available (Phase 5 pass-through).
        # When at least one upstream dispatch returned a status/body,
        # we prefer that real upstream error over a synthetic proxy
        # envelope. This ensures single-account upstream errors (e.g.
        # 429, 402) propagate as the same status the client would
        # have received against the upstream directly, instead of
        # being converted into a synthetic 503.
        if last_upstream_response is not None:
            status, headers, body = last_upstream_response
            resp_headers = list(headers) + [
                ("x-proxy-request-id", context.request_id),
                ("x-proxy-attempt-count", str(attempt_num)),
            ]
            # Phase 2: re-render upstream error in client protocol when
            # transcoding is active. The streaming pre-stream 4xx path
            # raises ``_NonRetryableUpstreamError`` with the raw upstream
            # body and never reaches the per-execution reencode branch
            # in ``_execute_non_streaming`` / ``_execute_streaming``.
            if context.transcode_required and (
                context.upstream_protocol != context.protocol
            ):
                transcoder = select_transcoder(
                    client_protocol=context.protocol,
                    upstream_protocol=context.upstream_protocol,
                )
                if transcoder is not None:
                    try:
                        err_payload_obj: object = json.loads(body)
                    except (json.JSONDecodeError, ValueError):
                        err_payload_obj = None
                    err_payload: dict[str, Any] | None
                    if isinstance(err_payload_obj, dict):
                        err_payload = cast("dict[str, Any]", err_payload_obj)
                    else:
                        err_payload = None
                    transcode_ctx = context.transcode_context or TranscodeContext(
                        request_id=context.request_id,
                        client_protocol=context.protocol,
                        upstream_protocol=context.upstream_protocol,
                    )
                    _status, err_body, err_warnings = transcoder.reencode_error(
                        status, err_payload, transcode_ctx
                    )
                    body = encode_json_body(err_body)
                    transcode_ctx.loss_warnings.extend(err_warnings)
            return PreparedProxyResponse(
                status_code=status,
                headers=resp_headers,
                body=body,
                request_id=context.request_id,
                account_name=context.client_metadata.get("account_name", ""),
                latency_ms=elapsed_ms,
                attempt_count=attempt_num,
            )

        # No upstream was ever reached. The status code is derived
        # from the categorized exception: an ``UpstreamExhaustedError``
        # surfaces as 502, an ``AuthenticationError`` as 502, a
        # ``RateLimitError`` as 429, a ``QuotaExhaustedError`` as 503,
        # and ``ModelUnavailableError`` (pre-dispatch) as 503. This
        # distinction is enforced by the proxy_request error handler.
        status_code = self._error_status_code(last_error)
        error_msg = str(last_error or "Request failed")
        if context.protocol == "anthropic":
            error_body = json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": error_msg,
                    },
                }
            ).encode()
        else:
            error_body = json.dumps(
                {
                    "error": {
                        "message": error_msg,
                        "type": "server_error",
                        "code": status_code,
                    }
                }
            ).encode()
        return PreparedProxyResponse(
            status_code=status_code,
            headers=[
                ("content-type", "application/json"),
                ("x-proxy-request-id", context.request_id),
                ("x-proxy-attempt-count", str(attempt_num)),
            ],
            body=error_body,
            request_id=context.request_id,
            account_name=context.client_metadata.get("account_name", ""),
            latency_ms=elapsed_ms,
            attempt_count=attempt_num,
        )

    def _all_accounts_attempted(self, context: ProxyRequestContext) -> bool:
        """Return whether every enabled account has been attempted.

        Used by the retry loop to distinguish pre-dispatch
        unavailability (genuine 503) from post-retry exhaustion
        (502 ``UpstreamExhaustedError``). ``True`` when the
        registered account set is non-empty and every name is
        already in ``context.attempted_accounts``.
        """
        enabled = self._registry.get_enabled_states()
        if not enabled:
            return False
        attempted = context.attempted_accounts
        return all(state.name in attempted for state in enabled)

    def _build_score_components(
        self,
        *,
        ranked_candidates: list[tuple[Any, Any]],
        selected_account_name: str,
        selected_state: Any,
        selected_score: float | None,
        selected_tier: int | None,
        fairness_decision: Any | None = None,
        fairness_band_names: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Build the score_components_json payload for one routing decision.

        Includes the full breakdown for the selected account plus
        the top near-tie candidates so the dashboard can answer
        "why account A over account B?" without rescoring.

        The payload also carries utilization ratios for each quota
        window (5h/7d/30d) and a short ``tie_break`` summary
        identifying the decisive factor between the chosen account
        and the runner-up (``tier``, ``quota``, ``inflight``,
        ``transcode``, ``near_tie``) so an operator can correlate
        visible skew against a concrete cause.
        """
        # Find the score for the selected account from ranked_candidates
        # if present; else synthesize the bare minimum from the trace.
        selected_score_obj: Any | None = None
        for state, score in ranked_candidates:
            if state.name == selected_account_name:
                selected_score_obj = score
                break

        top_candidates_payload = self._build_top_candidates(
            ranked_candidates, fairness_band_names=fairness_band_names
        )
        tie_break = self._derive_tie_break_summary(
            ranked_candidates=ranked_candidates,
            selected_score_obj=selected_score_obj,
        )

        if selected_score_obj is not None:
            payload: dict[str, Any] = {
                "account_name": selected_account_name,
                "quota_score": selected_score_obj.quota_score,
                "inflight_penalty": selected_score_obj.inflight_penalty,
                "health_penalty": selected_score_obj.health_penalty,
                "final_score": selected_score_obj.final_score,
                "weight": selected_score_obj.weight,
                "active_request_count": (selected_score_obj.active_request_count),
                "reserved_microdollars": (selected_score_obj.reserved_microdollars),
                "cost_5h_microdollars": (selected_score_obj.cost_5h_microdollars),
                "cost_7d_microdollars": (selected_score_obj.cost_7d_microdollars),
                "cost_30d_microdollars": (selected_score_obj.cost_30d_microdollars),
                "capacity_5h_microdollars": (
                    selected_score_obj.capacity_5h_microdollars
                ),
                "capacity_7d_microdollars": (
                    selected_score_obj.capacity_7d_microdollars
                ),
                "capacity_30d_microdollars": (
                    selected_score_obj.capacity_30d_microdollars
                ),
                "tier": selected_score_obj.tier,
                "requires_transcode": selected_score_obj.requires_transcode,
                "util_5h": _safe_ratio(
                    selected_score_obj.cost_5h_microdollars,
                    selected_score_obj.capacity_5h_microdollars,
                ),
                "util_7d": _safe_ratio(
                    selected_score_obj.cost_7d_microdollars,
                    selected_score_obj.capacity_7d_microdollars,
                ),
                "util_30d": _safe_ratio(
                    selected_score_obj.cost_30d_microdollars,
                    selected_score_obj.capacity_30d_microdollars,
                ),
                "tie_break": tie_break,
                "top_candidates": top_candidates_payload,
                "fairness": (
                    fairness_decision.to_dict()
                    if fairness_decision is not None
                    else None
                ),
            }
        else:
            payload = {
                "account_name": selected_account_name,
                "quota_score": 0.0,
                "inflight_penalty": 0.0,
                "health_penalty": 0.0,
                "final_score": (
                    float(selected_score) if selected_score is not None else 0.0
                ),
                "weight": 0.0,
                "active_request_count": 0,
                "reserved_microdollars": 0,
                "cost_5h_microdollars": 0,
                "cost_7d_microdollars": 0,
                "cost_30d_microdollars": 0,
                "capacity_5h_microdollars": 0,
                "capacity_7d_microdollars": 0,
                "capacity_30d_microdollars": 0,
                "tier": int(selected_tier) if selected_tier is not None else 0,
                "requires_transcode": False,
                "util_5h": None,
                "util_7d": None,
                "util_30d": None,
                "tie_break": tie_break,
                "top_candidates": top_candidates_payload,
                "fairness": (
                    fairness_decision.to_dict()
                    if fairness_decision is not None
                    else None
                ),
            }
        return payload

    @staticmethod
    def _build_top_candidates(
        ranked_candidates: list[tuple[Any, Any]],
        *,
        limit: int = 5,
        fairness_band_names: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Render the top-N ranked candidates for the dashboard table.

        Each entry includes ``rank_before_fairness`` (the candidate's
        position in the score-ordered list before the fairness rotor
        reordered the band), ``rank_after_fairness`` (the candidate's
        position in the final list), and ``fairness_band_member`` (True
        when the candidate was part of the fairness band eligible for
        rotation).
        """
        band = fairness_band_names or frozenset()

        # Build the pre-fairness ordering.  Non-band members keep their
        # post-fairness rank.  Band members are restored to the sorted
        # (by account name) order the rotor used before rotation.
        band_entries = [
            (state, score) for state, score in ranked_candidates if state.name in band
        ]
        band_sorted = sorted(band_entries, key=lambda pair: pair[0].name)
        pre_fairness_rank: dict[str, int] = {}
        band_idx = 0
        for rank, (state, _score) in enumerate(ranked_candidates):
            if state.name in band:
                # Place this band member at its sorted position
                if band_idx < len(band_sorted):
                    pre_name = band_sorted[band_idx][0].name
                    pre_fairness_rank[pre_name] = rank
                    band_idx += 1
            else:
                pre_fairness_rank[state.name] = rank

        out: list[dict[str, Any]] = []
        for rank_after, (state, score) in enumerate(ranked_candidates[:limit]):
            entry: dict[str, Any] = {
                "account_name": state.name,
                "final_score": float(score.final_score),
                "quota_score": score.quota_score,
                "inflight_penalty": score.inflight_penalty,
                "health_penalty": score.health_penalty,
                "tier": int(score.tier),
                "requires_transcode": bool(score.requires_transcode),
                "rank_before_fairness": pre_fairness_rank.get(state.name, rank_after),
                "rank_after_fairness": rank_after,
                "fairness_band_member": state.name in band,
            }
            out.append(entry)
        return out

    @staticmethod
    def _derive_tie_break_summary(
        *,
        ranked_candidates: list[tuple[Any, Any]],
        selected_score_obj: Any | None,
    ) -> dict[str, Any]:
        """Summarise why the selected account won over its runner-up.

        Returns a small dict the dashboard can render inline so
        operators do not have to recompute scores to see whether
        skew was driven by tier, quota utilization, in-flight
        pressure, or a near-tie within the scorer's tiebreaker
        range.
        """
        summary: dict[str, Any] = {
            "factor": "no_runner_up",
            "margin": None,
            "runner_up": None,
        }
        if selected_score_obj is None or len(ranked_candidates) < 2:
            return summary
        # Skip the selected account when searching for the runner-up;
        # the selected entry may not be ranked first if the caller
        # passed a list that does not start with the selected account.
        runner_up: tuple[Any, Any] | None = None
        for state, score in ranked_candidates:
            if state.name == selected_score_obj.account_name:
                continue
            runner_up = (state, score)
            break
        if runner_up is None:
            return summary
        ru_state, ru_score = runner_up
        selected_final = float(selected_score_obj.final_score)
        runner_final = float(ru_score.final_score)
        margin = runner_final - selected_final
        summary["margin"] = margin
        summary["runner_up"] = {
            "account_name": ru_state.name,
            "final_score": runner_final,
            "tier": int(ru_score.tier),
            "requires_transcode": bool(ru_score.requires_transcode),
        }
        if selected_score_obj.requires_transcode != ru_score.requires_transcode:
            summary["factor"] = (
                "transcode"
                if not selected_score_obj.requires_transcode
                else "transcode"
            )
            return summary
        if selected_score_obj.tier != ru_score.tier:
            summary["factor"] = "tier"
            return summary
        if margin == 0.0:
            summary["factor"] = "exact_tie"
            return summary
        # Score margins within the scorer's tiebreaker range are not
        # signal — they are deterministic or random noise.  Anything
        # outside the band is a real utilization / penalty delta.
        if abs(margin) <= 0.01:
            summary["factor"] = "near_tie"
            return summary
        if abs(selected_score_obj.inflight_penalty - ru_score.inflight_penalty) > abs(
            selected_score_obj.quota_score - ru_score.quota_score
        ):
            summary["factor"] = "inflight"
            return summary
        summary["factor"] = "quota"
        return summary

    def _resolve_upstream_protocol(
        self,
        context: ProxyRequestContext,
    ) -> str | None:
        """Determine the upstream protocol for transcoding.

        Returns the protocol to use upstream, or None when no
        transcodable route exists. When the client protocol matches
        a resolved model protocol, returns that protocol directly
        (native match, no transcoding needed).

        Translation is on by default. ``_transcoder_policy.enabled`` is
        a deprecated escape hatch — only an explicit ``False`` disables
        translation (restoring the legacy protocol-exact routing). ``None``
        and ``True`` both allow transcoding, so a missing policy object
        never silently disables it.
        """
        model_protocols = self._catalog.cache.get_model_protocols(
            context.model_id,
            provider_id=context.provider_id,
        )
        if context.protocol in model_protocols:
            return context.protocol  # native match

        if (
            self._transcoder_policy is not None
            and self._transcoder_policy.enabled is False
        ):
            return None  # legacy protocol-exact behaviour (escape hatch)

        # Find transcodable protocols among all eligible accounts.
        candidates = self._catalog.cache.get_transcodable_protocols(
            context.model_id,
            client_protocol=context.protocol,
            provider_id=context.provider_id,
        )
        if not candidates:
            return None

        # Choose the protocol with the largest eligible-account set.
        counts = {
            p: self._catalog.cache.count_eligible_accounts_for_protocol(
                context.model_id,
                p,
                provider_id=context.provider_id,
            )
            for p in candidates
        }
        # Prefer the protocol with the most eligible accounts;
        # ties broken by alphabetical order.
        return max(sorted(counts), key=lambda p: counts[p])

    def _validate_endpoint_or_transcode(self, context: ProxyRequestContext) -> None:
        """Validate that the endpoint matches the model's protocol.

        When the client protocol does not match the model's native
        protocol but a transcodable route exists (transcoder enabled and
        an account supports the native protocol), the mismatch is
        accepted and ``upstream_protocol`` / ``transcode_required`` are
        set on the context.

        Raises ProtocolMismatchError (which callers render as 400) when
        the wrong endpoint is used for a known model and no transcodable
        route exists.
        """
        if not self._catalog.cache.has_model(context.model_id):
            raise ModelNotFoundError(context.model_id)

        model_protocols = self._catalog.cache.get_model_protocols(
            context.model_id,
            provider_id=context.provider_id,
        )
        if not model_protocols:
            # Unresolved protocol - fail closed
            raise ModelUnavailableError(
                f"Model {context.model_id!r} has unresolved protocol"
            )

        if context.protocol in model_protocols:
            return

        # Check if transcoding can bridge the protocol gap.
        upstream_protocol = self._resolve_upstream_protocol(context)
        if upstream_protocol is not None:
            context.upstream_protocol = upstream_protocol
            context.transcode_required = True
            # Sync the transcode_context so execute() can detect the
            # protocol mismatch and select the correct transcoder.
            if context.transcode_context is not None:
                context.transcode_context.upstream_protocol = upstream_protocol
            return

        from eggpool.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        model_protocol = sorted(model_protocols)[0]
        resolver.validate_endpoint(model_protocol, context.protocol, context.model_id)

    def invalidate_account_id_cache(self, account_name: str | None = None) -> None:
        """Clear cached account IDs.

        Call after an account is removed or re-added so stale IDs are
        not reused.  Pass *account_name* to invalidate a single entry,
        or ``None`` to clear the entire cache.
        """
        if account_name is None:
            self._account_id_cache.clear()
        else:
            self._account_id_cache.pop(account_name, None)

    @staticmethod
    def _error_status_code(err: Exception | None) -> int:
        """Map an exception to an HTTP status code."""
        if err is None:
            return 500
        if isinstance(err, AuthenticationError):
            return 502
        if isinstance(err, RateLimitError):
            return 429
        if isinstance(err, QuotaExhaustedError):
            return 503
        if isinstance(err, ModelUnavailableError):
            return 503
        if isinstance(err, UpstreamError) and err.status_code is not None:
            return err.status_code
        if isinstance(err, _RetryableUpstreamError):
            if err.status_code is not None:
                return err.status_code
            return 502
        if isinstance(err, _NonRetryableUpstreamError):
            if err.status_code is not None:
                return err.status_code
            return 502
        return 502


class _RetryableUpstreamError(Exception):
    """An upstream error that can be retried on another account."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        error_class: str | None = None,
        retry_after: float | None = None,
        upstream_response: tuple[int, list[tuple[str, str]], bytes] | None = None,
        retry_category: RetryCategory | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_class = error_class
        self.retry_after = retry_after
        self.upstream_response = upstream_response
        self.retry_category = retry_category


class _NonRetryableUpstreamError(Exception):
    """An upstream error that should not be retried."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        upstream_response: tuple[int, list[tuple[str, str]], bytes] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_response = upstream_response
